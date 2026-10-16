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
import ssl
import threading
import time
from collections.abc import Callable
from email.mime.text import MIMEText

import config
from database import sqlite_client

logger = logging.getLogger("voltaneum.alerts")

# Délai avant de retenter une alerte dont l'envoi a échoué (secondes). Sans lui, un
# échec SMTP serait « couvert » par le délai entre e-mails et l'alerte perdue 30 min.
SEND_RETRY_SECONDS = 60
# Délai entre deux e-mails propre à certaines règles (minutes). Un défaut DURABLE (PT02 figée,
# cas actuel sur l'installation) n'a pas à être rappelé toutes les 30 min : une fois par jour.
RULE_COOLDOWN_MINUTES: dict[str, int] = {"oil_sensor_frozen": 24 * 60}


def cooldown_seconds(rule_name: str) -> float:
    return RULE_COOLDOWN_MINUTES.get(rule_name, config.ALERT_COOLDOWN_MINUTES) * 60


def smtp_configured() -> bool:
    """Vrai si la configuration e-mail est complète (sinon les alertes sont désactivées)."""
    return all((config.SMTP_HOST, config.SMTP_USER, config.SMTP_PASSWORD,
                config.ALERT_EMAIL_FROM, config.ALERT_EMAIL_TO))


# ---------------------------------------------------------------------------
# Envoi SMTP — contrat 5.5
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str) -> bool:
    """Envoie un e-mail (STARTTLS vérifié). Ne lève jamais d'exception : retourne False en cas
    d'échec ou de configuration incomplète (une panne SMTP n'interrompt jamais le système)."""
    smtp_host, smtp_user, smtp_password = config.SMTP_HOST, config.SMTP_USER, config.SMTP_PASSWORD
    sender, recipient = config.ALERT_EMAIL_FROM, config.ALERT_EMAIL_TO
    if not (smtp_host and smtp_user and smtp_password and sender and recipient):
        return False

    msg = MIMEText(body)
    # Les en-têtes ne doivent jamais contenir de saut de ligne (injection d'en-têtes).
    msg["Subject"] = " ".join(subject.split())
    msg["From"] = sender
    msg["To"] = recipient
    try:
        with smtplib.SMTP(smtp_host, config.SMTP_PORT, timeout=15) as server:
            # Contexte par défaut : certificat ET nom d'hôte du serveur vérifiés.
            server.starttls(context=ssl.create_default_context())
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logger.info("E-mail envoyé : %s", msg["Subject"])
        return True
    except (smtplib.SMTPException, OSError) as exc:
        logger.error("Échec d'envoi SMTP (%s) : %s", msg["Subject"], exc)
        return False


_smtp_missing_reported = False


def deliver_alert(level: str, rule_name: str, message: str) -> bool:
    """Point de sortie UNIQUE de toutes les alertes (procédé, stockage, modèles) :
    une ligne dans `alerts_log` sous le vrai nom de règle, plus un e-mail si SMTP est configuré.
    Retourne True si l'e-mail est parti."""
    global _smtp_missing_reported
    sent = False
    if smtp_configured():
        sent = send_email(f"[Voltaneum][{level}] {rule_name}", message)
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
    """Efficacité approximative de l'échangeur : proportion de la chute de
    température d'une sonde par rapport à l'écart maximal disponible
    avec le circuit dry cooler. Retourne None si les données nécessaires
    manquent ou si le dénominateur est ~0 (pas d'écart de température, donc
    pas d'échange thermique mesurable, division non pertinente).

    TODO (voir config.EFFICIENCY_MIN_EXPECTED / MAX_EXPECTED) : la formule
    exacte et les bornes "attendues" ne sont pas encore validées avec des
    données réelles de fonctionnement normal — à ajuster avant de compter
    sur cette règle en production.
    """
    pt01, pt02, pt03 = _number(telemetry, "pt01"), _number(telemetry, "pt02"), _number(telemetry, "pt03")
    if pt01 is None or pt02 is None or pt03 is None:
        return None
    denom = pt01 - pt03
    if abs(denom) < 0.5:
        return None
    return (pt01 - pt02) / denom


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

    # --- CRITIQUE : grandeur régulée (PT02, liquide de refroidissement) au-delà du seuil critique ---
    # Immédiat : aucune confirmation multi-cycles (sécurité du procédé).
    t_oil = _number(telemetry, "pt02")
    if t_oil is not None and t_oil > config.CRITICAL_TEMP:
        emit("CRITIQUE", "critical_temp",
             f"PT02 (liquide de refroidissement) = {t_oil:.1f} °C > seuil {config.CRITICAL_TEMP:.0f} °C")

    # --- CRITIQUE : liquide diélectrique (cuve, PT03 / PT04) au-delà de son seuil ---
    # C'est le liquide dans lequel baignent les équipements : sa température les protège.
    # Immédiat, comme le seuil critique de PT02.
    overheated = [(column.upper(), value) for column in ("pt03", "pt04")
                  if (value := _number(telemetry, column)) is not None and value > config.DIELECTRIC_CRITICAL_TEMP]
    if overheated:
        detail = ", ".join(f"{name} = {value:.1f} °C" for name, value in overheated)
        emit("CRITIQUE", "dielectric_critical_temp",
             f"Liquide diélectrique trop chaud : {detail} > seuil {config.DIELECTRIC_CRITICAL_TEMP:.0f} °C "
             "— équipements immergés exposés. Vérifier le refroidissement sur site.")

    # --- CRITIQUE : fréquence pompe / ventilateur non nulle sans débit ----------
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
             f"Pompe/ventilateur en marche sans débit sur {pump_count} mesures consécutives "
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

    # --- ATTENTION : PT02 (grandeur régulée) absente ou figée ------------------
    # Sans PT02 valide, AUCUNE consigne n'est publiée : l'automate retombe sur sa propre
    # régulation. Un PT02 qui ne bouge plus d'un dixième de degré pendant une heure alors que
    # le reste vit est presque toujours un mauvais point de passerelle ou une sonde bloquée.
    if telemetry is not None and _is_new_sample(state, "oil_sensor", telemetry):
        oil = _number(telemetry, config.TARGET_COLUMN)
        if oil is None:
            state.setdefault("oil_missing_since", now)
        else:
            state.pop("oil_missing_since", None)
            if oil != state.get("oil_last_value"):
                state["oil_last_value"] = oil
                state["oil_unchanged_since"] = now
    if telemetry is not None:
        missing_since = state.get("oil_missing_since")
        if missing_since is not None and now - missing_since >= config.OIL_SENSOR_MISSING_ALERT_SECONDS:
            emit("ATTENTION", "oil_sensor_missing",
                 f"PT02 (grandeur régulée) sans valeur valide depuis {(now - missing_since) / 60:.0f} min alors que "
                 "la passerelle émet : aucune consigne n'est publiée, l'automate régule seul. "
                 "Vérifier la sonde et le point sur la passerelle.")
        unchanged_since = state.get("oil_unchanged_since")
        frozen_after = config.SENSOR_FROZEN_ALERT_MINUTES * 60
        if (frozen_after and missing_since is None and unchanged_since is not None
                and now - unchanged_since >= frozen_after):
            emit("ATTENTION", "oil_sensor_frozen",
                 f"PT02 figée à {state['oil_last_value']:.1f} °C depuis {(now - unchanged_since) / 60:.0f} min : "
                 "valeur suspecte (mauvais point de passerelle, consigne lue à la place de la mesure, sonde "
                 "bloquée ?). À vérifier avec l'automaticien ; aucun modèle ne peut apprendre sur une mesure figée.")

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
