"""notifications/alerts.py — Moteur d'alertes, DANS le processus principal.

Les règles (`evaluate_rules`) sont des fonctions pures, évaluées à chaque instantané de mesures
(15 s) et toutes les 30 s par un chien de garde (perte de télémétrie). Le moteur ne dépend
d'AUCUN broker MQTT : une alerte critique (température au-delà du seuil, pompe sans débit, passerelle
muette) part même si le broker est absent ou en panne.

Toute alerte passe par `deliver_alert` : une ligne dans `alerts_log` (visible dans l'interface)
et un e-mail si SMTP est configuré. Confirmation sur plusieurs échantillons et délai entre
deux e-mails d'une même règle évitent le spam ; un e-mail en échec est retenté sous 60 s.
"""

from __future__ import annotations

import logging
import math
import queue
import smtplib
import sqlite3
import ssl
import threading
import time
from collections.abc import Callable
from email.message import EmailMessage
from pathlib import Path

import config
from database import sqlite_client

logger = logging.getLogger("voltaneum.alerts")

# Délai avant de retenter une alerte dont l'envoi a échoué (secondes). Sans lui, un
# échec SMTP serait « couvert » par le délai entre e-mails et l'alerte perdue 30 min.
SEND_RETRY_SECONDS = 60
# Délai entre deux e-mails propre à certaines règles (minutes). Un défaut DURABLE (sonde surveillée
# figée) n'a pas à être rappelé toutes les 30 min : une fois par jour.
# Constante de temps (s) de la moyenne glissante de PT01 utilisée par la règle « PT02 figée ».
FROZEN_REFERENCE_SMOOTHING_SECONDS = 600.0

RULE_COOLDOWN_MINUTES: dict[str, int] = {
    "oil_sensor_frozen": 24 * 60,
    # Contrôles de l'installation (notifications/plant_checks.py) : situations durables.
    "safety_bypass_active": 24 * 60, "automate_setpoint_mismatch": 24 * 60, "fan_short_cycling": 24 * 60,
    # Chaque changement de réglage est un événement distinct : pas de délai (un e-mail par contrôle
    # de 15 min au plus, qui regroupe les changements).
    "automate_mode_changed": 0,
}


def cooldown_seconds(rule_name: str) -> float:
    return RULE_COOLDOWN_MINUTES.get(rule_name, config.ALERT_COOLDOWN_MINUTES) * 60


def smtp_configured() -> bool:
    """Vrai si la configuration e-mail est complète (sinon les alertes sont désactivées)."""
    return all((config.SMTP_HOST, config.SMTP_USER, config.SMTP_PASSWORD,
                config.ALERT_EMAIL_FROM, config.ALERT_EMAIL_TO))


# ---------------------------------------------------------------------------
# Envoi SMTP — contrat 5.5
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str, attachment: Path | None = None) -> bool:
    """Envoie un e-mail (STARTTLS vérifié). Ne lève jamais d'exception : retourne False en cas
    d'échec ou de configuration incomplète (une panne SMTP n'interrompt jamais le système).
    Voir `send_email_detailed` pour la cause d'un échec."""
    return send_email_detailed(subject, body, attachment) is None


def send_email_detailed(subject: str, body: str, attachment: Path | None = None) -> str | None:
    """Envoie un e-mail à ALERT_EMAIL_TO avec le SMTP des alertes, éventuellement avec un fichier
    joint (utilisé par `./voltaneum copie --mail`). Retourne None si l'envoi a réussi, sinon la
    cause en clair. Ne lève jamais d'exception.

    Sécurité : STARTTLS avec certificat ET nom d'hôte du serveur vérifiés ; aucun saut de ligne dans
    le sujet (pas d'injection d'en-têtes) ; destinataire fixé par la configuration, jamais par
    l'appelant."""
    smtp_host, smtp_user, smtp_password = config.SMTP_HOST, config.SMTP_USER, config.SMTP_PASSWORD
    sender, recipient = config.ALERT_EMAIL_FROM, config.ALERT_EMAIL_TO
    if not (smtp_host and smtp_user and smtp_password and sender and recipient):
        return "configuration e-mail incomplète (SMTP_HOST, SMTP_USER, SMTP_PASSWORD, ALERT_EMAIL_FROM, ALERT_EMAIL_TO)"

    msg = EmailMessage()
    # Les en-têtes ne doivent jamais contenir de saut de ligne (injection d'en-têtes).
    msg["Subject"] = " ".join(subject.split())
    msg["From"] = sender
    msg["To"] = recipient
    msg.set_content(body)
    if attachment is not None:
        try:
            data = Path(attachment).read_bytes()
        except OSError as exc:
            return f"pièce jointe illisible : {exc}"
        msg.add_attachment(data, maintype="application", subtype="octet-stream", filename=Path(attachment).name)
    try:
        with smtplib.SMTP(smtp_host, config.SMTP_PORT, timeout=60 if attachment else 15) as server:
            # Contexte par défaut : certificat ET nom d'hôte du serveur vérifiés.
            server.starttls(context=ssl.create_default_context())
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logger.info("E-mail envoyé : %s", msg["Subject"])
        return None
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Échec d'envoi SMTP (%s) : %s", msg["Subject"], exc)
        return f"échec SMTP : {exc}"


_smtp_missing_reported = False


def _snapshot_for_email() -> dict | None:
    """Mesures du moment pour l'e-mail (dernière ligne en base) ; None si la base est illisible :
    l'alerte part quand même."""
    try:
        from dashboard import data

        return data.latest_snapshot(config.SQLITE_DB_PATH)
    except Exception:
        logger.exception("Mesures indisponibles pour l'e-mail d'alerte")
        return None


def deliver_alert(level: str, rule_name: str, message: str) -> bool:
    """Point de sortie UNIQUE de toutes les alertes (procédé, stockage, modèles) :
    une ligne dans `alerts_log` sous le vrai nom de règle, plus un e-mail si SMTP est configuré.
    Retourne True si l'e-mail est parti."""
    global _smtp_missing_reported
    sent = False
    if smtp_configured():
        # E-mail explicite et autonome (notifications/alert_texts.py) : titre en clair, explication,
        # actions, mesures du moment, commandes de vérification. Le journal garde le message court.
        from notifications.alert_texts import compose

        subject, body = compose(level, rule_name, message, _snapshot_for_email(),
                                reminder_minutes=cooldown_seconds(rule_name) / 60)
        sent = send_email(subject, body)
    elif not _smtp_missing_reported:
        logger.warning("E-mail non configuré (SMTP_*, ALERT_EMAIL_*) : les alertes sont seulement "
                       "journalisées en base et visibles dans l'interface.")
        _smtp_missing_reported = True
    log = logger.error if level == "CRITIQUE" else logger.warning
    log("ALERTE %s %s : %s", level, rule_name, message)
    sqlite_client.log_alert(level, rule_name, message, email_sent=sent, db_path=config.SQLITE_DB_PATH)
    return sent


# ---------------------------------------------------------------------------
# Règles
# ---------------------------------------------------------------------------
def _cooldown_elapsed(rule_name: str, state: dict, now: float) -> bool:
    last_sent = state["last_sent_at"].get(rule_name)
    if last_sent is None:
        return True
    return (now - last_sent) >= cooldown_seconds(rule_name)


def _number(payload: dict | None, key: str) -> float | None:
    """Lit `payload[key]` comme un nombre fini, sinon None.

    Les charges MQTT sont des entrées non fiables : une valeur absente, textuelle,
    NaN ou infinie ne doit jamais faire planter l'évaluation des règles.
    """
    if not isinstance(payload, dict):
        return None
    value = payload.get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(value) else None


def _compute_efficiency(telemetry: dict) -> float | None:
    """Efficacité de l'échangeur (définition usuelle, entre 0 et 1) :

        ε = plus grand des deux écarts de circuit / écart maximal disponible
          = max(T diélectrique chaud − froid, T refroidissement chaud − froid)
            / (T diélectrique chaud − T refroidissement froid)

    Les sondes chaude / froide de chaque circuit viennent de config (DIELECTRIC_HOT_COLUMN…).
    None si une mesure manque ou si l'écart disponible est trop faible pour être interprété (pas
    d'échange mesurable, division non pertinente). Bornes attendues NON validées : règle
    désactivée par défaut (config.EFFICIENCY_ALERT_ENABLED).
    """
    diel_hot, diel_cold = _number(telemetry, config.DIELECTRIC_HOT_COLUMN), _number(telemetry, config.DIELECTRIC_COLD_COLUMN)
    cool_hot, cool_cold = _number(telemetry, config.COOLANT_HOT_COLUMN), _number(telemetry, config.COOLANT_COLD_COLUMN)
    if diel_hot is None or diel_cold is None or cool_hot is None or cool_cold is None:
        return None
    available = diel_hot - cool_cold
    if available < 0.5:
        return None
    return max(diel_hot - diel_cold, cool_hot - cool_cold) / available


def _is_new_sample(state: dict, key: str, payload: dict | None) -> bool:
    """Vrai si `payload` est un échantillon pas encore compté pour la règle `key`.

    Un même échantillon ne doit être compté qu'UNE fois : une prédiction publiée
    toutes les 60 s, ou un écart capteur/météo publié toutes les 15 min, serait
    sinon recompté à chaque message de télémétrie (15 s) et la « confirmation
    multi-cycles » deviendrait illusoire. On se base sur l'horodatage `ts` de la
    charge ; sans `ts`, chaque appel compte comme un nouvel échantillon.
    """
    ts = _number(payload, "ts")
    if ts is None:
        return True
    last_seen = state.setdefault("last_sample_ts", {})
    if last_seen.get(key) == ts:
        return False
    last_seen[key] = ts
    return True


def _update_counter(state: dict, rule_name: str, condition_met: bool) -> int:
    """Incrémente le compteur de cycles consécutifs si la condition est vraie,
    sinon le remet à zéro (et non « en pause ») ; retourne sa nouvelle valeur."""
    counts = state["consecutive_counts"]
    counts[rule_name] = counts.get(rule_name, 0) + 1 if condition_met else 0
    return counts[rule_name]


def evaluate_rules(telemetry: dict | None, prediction: dict | None, state: dict,
                    outdoor_consistency: dict | None = None) -> list[dict]:
    """Évalue toutes les règles d'alerte et retourne les alertes à envoyer.

    Fonction sans effet de bord réseau (facile à tester). `state` est mutée en
    place : compteurs de cycles consécutifs, horodatage du dernier envoi par
    règle (cooldown) et dernier échantillon compté par règle.

    `telemetry=None` signifie « évaluation périodique sans nouvelle mesure »
    (chien de garde) : seules les règles liées au temps (perte de télémétrie)
    peuvent alors se déclencher, les compteurs basés sur des échantillons ne
    sont PAS touchés.
    """
    now = time.time()
    state.setdefault("consecutive_counts", {
        "efficiency_drift": 0, "model_disagreement": 0, "outdoor_sensor_weather_mismatch": 0,
        "pump_without_flow": 0,
    })
    state.setdefault("last_sent_at", {})
    alerts: list[dict] = []

    def emit(level: str, rule_name: str, message: str) -> None:
        """Ajoute l'alerte si le cooldown de la règle est écoulé."""
        if _cooldown_elapsed(rule_name, state, now):
            alerts.append({"level": level, "rule_name": rule_name, "message": message})
            state["last_sent_at"][rule_name] = now

    # --- CRITIQUE : températures du diélectrique au-delà de leur seuil -------------------------
    # Deux règles, comptées sur des MESURES DISTINCTES (une même mesure relue ne compte qu'une fois) :
    #   - critical_temp            : PT02, grandeur surveillée (état de l'armoire) > CRITICAL_TEMP ;
    #   - dielectric_critical_temp : l'autre sonde du diélectrique (PT01, sortie CDU) >
    #                                DIELECTRIC_CRITICAL_TEMP — la CDU ne refroidit plus, ou secours
    #                                si PT02 est absente.
    # Chacune exige CRITICAL_CONFIRMATION_SAMPLES mesures CONSÉCUTIVES au-delà du seuil (2 par défaut,
    # soit 15 s). Pourquoi : les relevés contiennent de rares pics isolés d'une seule mesure (+2 °C
    # pendant 15 s, database/quality.py) ; sans confirmation, un pic près du seuil enverrait un e-mail
    # CRITIQUE à tort. Un vrai dépassement dure : il est signalé avec 15 s de retard au plus. Une mesure
    # absente ou sous le seuil remet le compteur à zéro. La RÉGULATION, elle, reste immédiate
    # (control/supervisor.py) : en cas de doute, elle refroidit.
    regulated = config.TARGET_COLUMN.upper()
    t_oil = _number(telemetry, config.TARGET_COLUMN)
    dielectric = [c for c in config.DIELECTRIC_COLUMNS if c != config.TARGET_COLUMN]
    overheated = [(column.upper(), value) for column in dielectric
                  if (value := _number(telemetry, column)) is not None and value > config.DIELECTRIC_CRITICAL_TEMP]
    if telemetry is not None and _is_new_sample(state, "critical_temperatures", telemetry):
        _update_counter(state, "critical_temp", t_oil is not None and t_oil > config.CRITICAL_TEMP)
        _update_counter(state, "dielectric_critical_temp", bool(overheated))
    needed = max(1, config.CRITICAL_CONFIRMATION_SAMPLES)
    confirmed = "" if needed == 1 else f" ({needed} mesures consécutives)"
    if t_oil is not None and state["consecutive_counts"].get("critical_temp", 0) >= needed:
        emit("CRITIQUE", "critical_temp",
             f"{regulated} (diélectrique de l'armoire) = {t_oil:.1f} °C > seuil {config.CRITICAL_TEMP:.0f} °C"
             + confirmed)
    if overheated and state["consecutive_counts"].get("dielectric_critical_temp", 0) >= needed:
        detail = ", ".join(f"{name} = {value:.1f} °C" for name, value in overheated)
        emit("CRITIQUE", "dielectric_critical_temp",
             f"Liquide diélectrique trop chaud : {detail} > seuil {config.DIELECTRIC_CRITICAL_TEMP:.0f} °C"
             f"{confirmed} — équipements immergés exposés. Vérifier le refroidissement sur site.")

    # --- CRITIQUE : pompe en marche (fréquence non nulle) sans débit ------------
    # Une pompe qui tourne sans débit (vanne fermée, circuit bouché, désamorçage, variateur ou
    # capteur de débit défaillant) risque la marche à sec. Seuils : config.PUMP_RUNNING_MIN_HZ
    # (au-dessus : « en marche ») et config.FLOW_MIN_M3H (en dessous : « pas de débit »).
    # Confirmation sur plusieurs échantillons distincts : au démarrage de la pompe, le débit met
    # quelques secondes à monter. Une valeur absente ne déclenche JAMAIS l'alerte (un capteur
    # muet est un autre problème, signalé par la règle de perte de télémétrie) et remet le
    # compteur à zéro.
    if telemetry is not None and _is_new_sample(state, "pump_flow", telemetry):
        flow = _number(telemetry, "ht01")
        frequency = _number(telemetry, "freq_pompe")
        no_flow = (flow is not None and frequency is not None
                   and frequency > config.PUMP_RUNNING_MIN_HZ and flow < config.FLOW_MIN_M3H)
        _update_counter(state, "pump_without_flow", no_flow)
    pump_count = state["consecutive_counts"].get("pump_without_flow", 0)
    if pump_count >= config.ALERT_CONFIRMATION_CYCLES:
        flow, frequency = _number(telemetry, "ht01"), _number(telemetry, "freq_pompe")
        detail = (f"fréquence = {frequency:.1f} Hz, débit = {flow:.2f} m³/h"
                  if flow is not None and frequency is not None else "valeurs indisponibles")
        emit("CRITIQUE", "pump_without_flow",
             f"Pompe en marche sans débit sur {pump_count} mesures consécutives "
             f"({detail}) — vérifier sur site : vanne, circuit, amorçage, capteur de débit.")

    # --- CRITIQUE : perte de communication --------------------------------
    # Référence du silence : le dernier message reçu, ou, si aucun n'est jamais
    # arrivé, l'heure de démarrage (state["started_at"]) — une passerelle en
    # panne dès le lancement doit être détectée au même titre qu'une coupure.
    last_telemetry_ts = state.get("last_telemetry_ts")
    reference_ts = last_telemetry_ts if last_telemetry_ts is not None else state.get("started_at", now)
    silence = now - reference_ts
    if silence > config.TELEMETRY_TIMEOUT_SECONDS:
        since = "depuis le démarrage" if last_telemetry_ts is None else "depuis"
        emit("CRITIQUE", "telemetry_lost",
             f"Aucune mesure reçue de la passerelle {since} {silence / 60:.1f} min "
             f"(seuil {config.TELEMETRY_TIMEOUT_SECONDS / 60:.0f} min)")

    # --- ATTENTION : PT02 (grandeur surveillée) absente ou figée ------------------
    # Sans PT02 valide, AUCUNE consigne n'est publiée : l'automate retombe sur sa propre
    # régulation. Une PT02 qui ne bouge plus pendant une heure alors que le reste vit est presque
    # toujours un mauvais point de passerelle ou une sonde bloquée. Mais PT02 est lue au degré
    # près : constante pendant une heure sur un procédé stable n'est PAS une panne. Elle n'est
    # jugée figée que si l'autre sonde du circuit (PT01) a bougé d'au moins
    # SENSOR_FROZEN_REFERENCE_SPAN_CELSIUS pendant ce temps (sans PT01 : la durée seule fait foi).
    # PT01 est jugée sur sa MOYENNE GLISSANTE (constante de temps FROZEN_REFERENCE_SMOOTHING_SECONDS,
    # 10 min), pas sur ses valeurs brutes : les cycles du ventilateur la font osciller de 0,4 à 1 °C
    # toutes les 5 min, et un pic isolé ajoute jusqu'à 2 °C. Sur les relevés réels du 12 au 22/10/2026,
    # la règle sur PT01 brute s'est déclenchée 19 fois à tort ; sur la moyenne, aucune. Un vrai
    # changement de l'armoire déplace la moyenne : une sonde réellement bloquée reste détectée.
    reference = next((c for c in config.DIELECTRIC_COLUMNS if c != config.TARGET_COLUMN), None)
    if telemetry is not None and _is_new_sample(state, "oil_sensor", telemetry):
        oil = _number(telemetry, config.TARGET_COLUMN)
        if oil is None:
            state.setdefault("oil_missing_since", now)
        else:
            state.pop("oil_missing_since", None)
            if oil != state.get("oil_last_value"):
                state["oil_last_value"] = oil
                state["oil_unchanged_since"] = now
                state["frozen_reference_range"] = None
            raw_reference = _number(telemetry, reference) if reference else None
            ref_value = None
            if raw_reference is not None:
                sample_ts = _number(telemetry, "ts") or now
                ema, ema_ts = state.get("frozen_reference_ema"), state.get("frozen_reference_ema_ts")
                weight = 1.0 if ema is None or ema_ts is None else \
                    1.0 - math.exp(-max(0.0, sample_ts - ema_ts) / FROZEN_REFERENCE_SMOOTHING_SECONDS)
                ref_value = raw_reference if ema is None else ema + weight * (raw_reference - ema)
                state["frozen_reference_ema"], state["frozen_reference_ema_ts"] = ref_value, sample_ts
            if ref_value is not None:
                low, high = state.get("frozen_reference_range") or (ref_value, ref_value)
                state["frozen_reference_range"] = (min(low, ref_value), max(high, ref_value))
    if telemetry is not None:
        missing_since = state.get("oil_missing_since")
        if missing_since is not None and now - missing_since >= config.OIL_SENSOR_MISSING_ALERT_SECONDS:
            emit("ATTENTION", "oil_sensor_missing",
                 f"{regulated} (grandeur surveillée) sans valeur valide depuis {(now - missing_since) / 60:.0f} min alors que "
                 "la passerelle émet : aucune consigne n'est publiée, l'automate régule seul. "
                 "Vérifier la sonde et le point sur la passerelle.")
        unchanged_since = state.get("oil_unchanged_since")
        frozen_after = config.SENSOR_FROZEN_ALERT_MINUTES * 60
        ref_range = state.get("frozen_reference_range")
        ref_span = None if ref_range is None else ref_range[1] - ref_range[0]
        neighbour_moved = ref_span is None or ref_span >= config.SENSOR_FROZEN_REFERENCE_SPAN_CELSIUS
        if (frozen_after and missing_since is None and unchanged_since is not None
                and now - unchanged_since >= frozen_after and neighbour_moved):
            moved = ("" if ref_span is None else
                     f" alors que {str(reference).upper()} (moyenne sur 10 min) a varié de {ref_span:.1f} °C")
            emit("ATTENTION", "oil_sensor_frozen",
                 f"{regulated} figée à {state['oil_last_value']:.1f} °C depuis {(now - unchanged_since) / 60:.0f} min{moved} : "
                 "valeur suspecte (mauvais point de passerelle, consigne lue à la place de la mesure, sonde "
                 "bloquée ?). À vérifier avec l'automaticien.")

    # --- ATTENTION : dérive d'efficacité de l'échangeur (désactivée par défaut) -
    # Formule et bornes NON validées sur l'installation : la règle n'est active que si
    # EFFICIENCY_ALERT_ENABLED=true, pour ne pas envoyer d'alertes sans fondement.
    if config.EFFICIENCY_ALERT_ENABLED and telemetry is not None and _is_new_sample(state, "telemetry", telemetry):
        efficiency = _compute_efficiency(telemetry)
        out_of_range = efficiency is not None and not (
            config.EFFICIENCY_MIN_EXPECTED <= efficiency <= config.EFFICIENCY_MAX_EXPECTED
        )
        _update_counter(state, "efficiency_drift", out_of_range)
    if (config.EFFICIENCY_ALERT_ENABLED
            and state["consecutive_counts"]["efficiency_drift"] >= config.ALERT_CONFIRMATION_CYCLES):
        emit("ATTENTION", "efficiency_drift",
             "Écart d'efficacité échangeur hors plage attendue sur "
             f"{state['consecutive_counts']['efficiency_drift']} mesures consécutives")

    # --- ATTENTION : désaccord entre les deux modèles ML -------------------
    if prediction is not None and _is_new_sample(state, "prediction", prediction):
        _update_counter(state, "model_disagreement", prediction.get("disagreement_flag") is True)
    if state["consecutive_counts"]["model_disagreement"] >= config.ALERT_CONFIRMATION_CYCLES:
        disagreement = _number(prediction, "disagreement")
        detail = (f"Désaccord entre modèles = {disagreement:.2f}°C" if disagreement is not None
                  else "Désaccord entre modèles ML détecté")
        emit("ATTENTION", "model_disagreement",
             f"{detail} sur {state['consecutive_counts']['model_disagreement']} prédictions consécutives")

    # --- ATTENTION : écart capteur extérieur / météo -----------------------
    # Vérification uniquement : la valeur du capteur n'est JAMAIS modifiée (voir
    # collectors/outdoor_consistency_checker.py). Seuil large : un capteur local
    # et une moyenne régionale peuvent légitimement diverger sans qu'aucun ait tort.
    gap = _number(outdoor_consistency, "gap")
    sensor_value = _number(outdoor_consistency, "sensor_value")
    weather_value = _number(outdoor_consistency, "weather_value")
    if (gap is not None and sensor_value is not None and weather_value is not None
            and _is_new_sample(state, "outdoor_consistency", outdoor_consistency)):
        _update_counter(state, "outdoor_sensor_weather_mismatch",
                        gap > config.OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS)
    if (state["consecutive_counts"]["outdoor_sensor_weather_mismatch"] >= config.ALERT_CONFIRMATION_CYCLES
            and gap is not None and sensor_value is not None and weather_value is not None):
        emit("ATTENTION", "outdoor_sensor_weather_mismatch",
             f"Écart capteur/météo = {gap:.1f}°C (capteur={sensor_value:.1f}°C, météo={weather_value:.1f}°C) "
             f"sur {state['consecutive_counts']['outdoor_sensor_weather_mismatch']} vérifications consécutives — "
             "à vérifier, la valeur du capteur reste utilisée sans changement.")

    return alerts


# ---------------------------------------------------------------------------
# Moteur (dans le processus principal)
# ---------------------------------------------------------------------------
class AlertEngine:
    """Évalue les règles sur les données du processus principal et distribue les alertes.

    Alimenté directement par le collecteur (`on_telemetry`), la boucle de contrôle
    (`on_prediction`) et le vérificateur capteur / météo (`on_outdoor_consistency`).

    Les alertes sont ENVOYÉES par le thread du moteur (`run_forever`), jamais par l'appelant :
    un serveur SMTP lent (15 s par tentative) ne retarde ni la collecte ni la régulation.
    `synchronous=True` envoie immédiatement (tests, scripts).
    """

    def __init__(self, deliver: Callable[[str, str, str], bool] | None = None,
                 watchdog_interval_seconds: float = 30.0, synchronous: bool = False) -> None:
        self._deliver = deliver or deliver_alert
        self._interval = watchdog_interval_seconds
        self._synchronous = synchronous
        self._outbox: queue.Queue[dict | None] = queue.Queue()
        # `started_at` : référence du chronomètre « aucune mesure depuis le démarrage ».
        self._state: dict = {"started_at": time.time()}
        self._latest_prediction: dict | None = None
        self._latest_outdoor_consistency: dict | None = None
        self._lock = threading.Lock()
        self._stop_event = threading.Event()

    def restore_cooldowns(self, db_path: str) -> int:
        """Reprend, depuis `alerts_log`, l'heure du dernier envoi de chaque règle, pour que le délai
        entre deux e-mails survive à un redémarrage. Sans cela, chaque `./voltaneum restart` renvoyait
        aussitôt toutes les alertes durables (shunts, consigne : rappel « une fois par jour » envoyé
        deux fois en 17 min le 22/10/2026). Seuls comptent les e-mails réellement partis (ou, sans
        SMTP configuré, les alertes journalisées). Retourne le nombre de règles reprises ; ne lève
        jamais d'exception (une base illisible ne fait que perdre cette reprise)."""
        try:
            with sqlite_client.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT rule_name, MAX(CAST(strftime('%s', timestamp) AS REAL)) FROM alerts_log "
                    "WHERE email_sent = 1 OR ? GROUP BY rule_name",
                    (0 if smtp_configured() else 1,),
                ).fetchall()
        except sqlite3.Error:
            logger.exception("Reprise des délais entre alertes impossible")
            return 0
        with self._lock:
            last_sent = self._state.setdefault("last_sent_at", {})
            for rule, ts in rows:
                if rule and ts is not None:
                    last_sent[rule] = max(float(ts), last_sent.get(rule, 0.0))
        return len(rows)

    def on_telemetry(self, snapshot: dict) -> None:
        payload = {**snapshot, "ts": snapshot.get("ts", time.time())}
        with self._lock:
            self._state["last_telemetry_ts"] = time.time()
            alerts = evaluate_rules(payload, self._latest_prediction, self._state,
                                    self._latest_outdoor_consistency)
        self._dispatch(alerts)

    def on_prediction(self, prediction: dict) -> None:
        with self._lock:
            self._latest_prediction = {**prediction, "ts": prediction.get("ts", time.time())}

    def on_outdoor_consistency(self, result: dict) -> None:
        with self._lock:
            self._latest_outdoor_consistency = result

    def report(self, level: str, rule_name: str, message: str) -> None:
        """Alerte décidée hors des règles (ex. régulation inefficace), avec le même délai entre
        deux e-mails d'une même règle."""
        now = time.time()
        with self._lock:
            self._state.setdefault("last_sent_at", {})
            if not _cooldown_elapsed(rule_name, self._state, now):
                return
            self._state["last_sent_at"][rule_name] = now
        self._dispatch([{"level": level, "rule_name": rule_name, "message": message}])

    def check_now(self) -> None:
        """Évaluation sans nouvelle mesure : règles liées au temps (passerelle muette)."""
        with self._lock:
            alerts = evaluate_rules(None, self._latest_prediction, self._state, self._latest_outdoor_consistency)
        self._dispatch(alerts)

    def run_forever(self) -> None:
        """Envoie les alertes en attente et lance le chien de garde toutes les `_interval` s."""
        next_check = time.monotonic() + self._interval
        while not self._stop_event.is_set():
            try:
                alert = self._outbox.get(timeout=max(0.0, min(1.0, next_check - time.monotonic())))
            except queue.Empty:
                alert = None
            if alert is not None:
                self._send([alert])
            if time.monotonic() >= next_check:
                next_check = time.monotonic() + self._interval
                try:
                    self.check_now()
                except Exception:
                    logger.exception("Erreur du chien de garde des alertes")
        # Arrêt : les alertes déjà décidées ne sont pas perdues (journal + e-mail).
        pending = []
        while not self._outbox.empty():
            item = self._outbox.get_nowait()
            if item is not None:
                pending.append(item)
        self._send(pending)

    def stop(self) -> None:
        self._stop_event.set()
        self._outbox.put(None)  # réveille immédiatement le thread d'envoi

    def _dispatch(self, alerts: list[dict]) -> None:
        if self._synchronous:
            self._send(alerts)
        else:
            for alert in alerts:
                self._outbox.put(alert)

    def _send(self, alerts: list[dict]) -> None:
        for alert in alerts:
            try:
                sent = self._deliver(alert["level"], alert["rule_name"], alert["message"])
            except Exception:
                logger.exception("Distribution impossible de l'alerte %s", alert["rule_name"])
                sent = False
            if not sent and smtp_configured():
                # La règle a déjà démarré son délai entre e-mails : on le raccourcit pour que
                # l'alerte soit retentée sous SEND_RETRY_SECONDS au lieu d'être perdue.
                with self._lock:
                    self._state["last_sent_at"][alert["rule_name"]] = (
                        time.time() - cooldown_seconds(alert["rule_name"]) + SEND_RETRY_SECONDS
                    )
