"""
notifications/email_notifier.py — Moteur d'alertes par email.

Contrat : spec §6.9. Ce module est volontairement DÉCOUPLÉ du reste du
pipeline : il ne connaît rien de collectors/ ni de ml/, il ne fait que
s'abonner à trois topics MQTT (voltaneum/telemetry, voltaneum/prediction,
voltaneum/sensors/outdoor_consistency) et réagir à ce qu'il y voit passer. En clair :
si demain on retire ce module entièrement, rien d'autre dans le projet ne
casse — c'est le but recherché (isolation par bus de messages plutôt que par
import direct).

Règles non négociables respectées ici (voir spec §1) :
  - règle 6 : toute panne réseau (MQTT ou SMTP) est journalisée et n'interrompt
    jamais la boucle principale (aucun try/except: pass silencieux).
  - règle 7 : cooldown + confirmation multi-cycles pour ne jamais spammer.
"""

from __future__ import annotations

import json
import logging
import math
import smtplib
import ssl
import threading
import time
from email.mime.text import MIMEText

import paho.mqtt.client as mqtt

import config
from database import sqlite_client

logger = logging.getLogger("voltaneum.email_notifier")

# Délai avant de retenter une alerte dont l'envoi a échoué (secondes). Sans lui, un
# échec SMTP serait « couvert » par le cooldown et l'alerte perdue pendant 30 min.
SEND_RETRY_SECONDS = 60


def smtp_configured() -> bool:
    """Vrai si la configuration e-mail est complète (sinon les alertes sont désactivées)."""
    return all((config.SMTP_HOST, config.SMTP_USER, config.SMTP_PASSWORD,
                config.ALERT_EMAIL_FROM, config.ALERT_EMAIL_TO))


# ---------------------------------------------------------------------------
# Envoi SMTP — contrat 5.5
# ---------------------------------------------------------------------------

def send_email(subject: str, body: str, level: str) -> bool:
    """Envoie l'email, journalise le résultat dans alerts_log, ne lève jamais
    d'exception vers l'appelant (règle 6)."""
    smtp_host, smtp_user, smtp_password = config.SMTP_HOST, config.SMTP_USER, config.SMTP_PASSWORD
    sender, recipient = config.ALERT_EMAIL_FROM, config.ALERT_EMAIL_TO

    if not smtp_configured():
        logger.error(
            "Configuration SMTP incomplète (SMTP_HOST/SMTP_USER/SMTP_PASSWORD/"
            "ALERT_EMAIL_FROM/ALERT_EMAIL_TO) — email non envoyé."
        )
        sqlite_client.log_alert(level, "smtp_config_missing", body, email_sent=False)
        return False

    msg = MIMEText(body)
    # Les en-têtes ne doivent jamais contenir de saut de ligne (injection d'en-têtes).
    msg["Subject"] = " ".join(subject.split())
    msg["From"] = sender
    msg["To"] = recipient

    try:
        with smtplib.SMTP(smtp_host, config.SMTP_PORT, timeout=15) as server:
            # Contexte par défaut : certificat ET nom d'hôte du serveur sont vérifiés.
            # (sans `context=`, smtplib ne vérifie PAS le certificat.)
            server.starttls(context=ssl.create_default_context())
            server.login(smtp_user, smtp_password)
            server.send_message(msg)
        logger.info("Email envoyé : %s", subject)
        sqlite_client.log_alert(level, subject, body, email_sent=True)
        return True
    except (smtplib.SMTPException, OSError) as exc:
        # Panne SMTP = journalisée, jamais propagée (règle 6). Une alerte non
        # envoyée reste visible dans alerts_log avec email_sent=0.
        logger.error("Échec d'envoi SMTP (%s) : %s", subject, exc)
        sqlite_client.log_alert(level, subject, body, email_sent=False)
        return False


# ---------------------------------------------------------------------------
# Moteur de règles — contrat 6.9, table de référence
# ---------------------------------------------------------------------------

def _cooldown_elapsed(rule_name: str, state: dict, now: float) -> bool:
    last_sent = state["last_sent_at"].get(rule_name)
    if last_sent is None:
        return True
    return (now - last_sent) >= config.ALERT_COOLDOWN_MINUTES * 60


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
    température côté bain d'huile par rapport à l'écart maximal disponible
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

    # --- CRITIQUE : température du bain d'huile au-delà du seuil critique ---
    # Immédiat : aucune confirmation multi-cycles (sécurité du procédé).
    t_oil = _number(telemetry, "pt02")
    if t_oil is not None and t_oil > config.CRITICAL_TEMP:
        emit("CRITIQUE", "critical_temp",
             f"T_bain = {t_oil:.1f}°C > seuil {config.CRITICAL_TEMP:.0f}°C")

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
             f"Aucun message voltaneum/telemetry {since} {silence / 60:.1f} min "
             f"(seuil {config.TELEMETRY_TIMEOUT_SECONDS / 60:.0f} min)")

    # --- ATTENTION : dérive d'efficacité de l'échangeur --------------------
    if telemetry is not None and _is_new_sample(state, "telemetry", telemetry):
        efficiency = _compute_efficiency(telemetry)
        out_of_range = efficiency is not None and not (
            config.EFFICIENCY_MIN_EXPECTED <= efficiency <= config.EFFICIENCY_MAX_EXPECTED
        )
        _update_counter(state, "efficiency_drift", out_of_range)
    if state["consecutive_counts"]["efficiency_drift"] >= config.ALERT_CONFIRMATION_CYCLES:
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
# Client MQTT — orchestration en continu
# ---------------------------------------------------------------------------

class EmailNotifier:
    """Abonné MQTT qui maintient l'état des dernières données vues et
    déclenche `evaluate_rules` à chaque nouveau message de télémétrie, plus
    une vérification périodique en tâche de fond pour la règle "perte de
    communication" (qui doit se déclencher même en l'absence de nouveau
    message — par définition)."""

    def __init__(self, mqtt_host: str = config.MQTT_HOST, mqtt_port: int = config.MQTT_PORT):
        self._mqtt_host = mqtt_host
        self._mqtt_port = mqtt_port
        # `started_at` : référence du chronomètre « aucune télémétrie depuis le démarrage »
        # (sans lui, une passerelle en panne dès le lancement ne déclencherait jamais l'alerte).
        self._state: dict = {"started_at": time.time()}
        self._latest_prediction: dict | None = None
        self._latest_outdoor_consistency: dict | None = None
        self._stop_event = threading.Event()
        self._lock = threading.Lock()

        sqlite_client.init_db(config.SQLITE_DB_PATH)

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id="voltaneum-email-notifier",
        )
        if config.MQTT_USERNAME:
            self._client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
        if config.MQTT_TLS_ENABLED:
            self._client.tls_set(
                ca_certs=config.MQTT_TLS_CA_CERT,
                certfile=config.MQTT_TLS_CERTFILE,
                keyfile=config.MQTT_TLS_KEYFILE,
                cert_reqs=ssl.CERT_REQUIRED,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.on_disconnect = self._on_disconnect

    # -- callbacks MQTT ----------------------------------------------------

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code != 0:
            logger.error("Connexion MQTT échouée (code %s)", reason_code)
            return
        logger.info("Connecté au broker MQTT %s:%s", self._mqtt_host, self._mqtt_port)
        for topic in (config.MQTT_TOPIC_TELEMETRY, config.MQTT_TOPIC_PREDICTION,
                      config.MQTT_TOPIC_OUTDOOR_CONSISTENCY):
            client.subscribe(topic)

    def _on_disconnect(self, client, userdata, disconnect_flags, reason_code, properties=None):
        # paho relance automatiquement la reconnexion (loop_start gère le
        # backoff) — on se contente de journaliser (règle 6 : jamais de
        # crash silencieux, jamais d'interruption non journalisée).
        logger.warning("Déconnecté du broker MQTT (code %s), reconnexion automatique en cours", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            logger.warning("Message MQTT illisible sur %s, ignoré", msg.topic)
            return

        if not isinstance(payload, dict):
            logger.warning("Message MQTT sur %s ignoré : un objet JSON est attendu.", msg.topic)
            return

        alerts: list[dict] = []
        try:
            with self._lock:
                if msg.topic == config.MQTT_TOPIC_TELEMETRY:
                    self._state["last_telemetry_ts"] = time.time()
                    alerts = evaluate_rules(
                        telemetry=payload,
                        prediction=self._latest_prediction,
                        state=self._state,
                        outdoor_consistency=self._latest_outdoor_consistency,
                    )
                elif msg.topic == config.MQTT_TOPIC_PREDICTION:
                    self._latest_prediction = payload
                elif msg.topic == config.MQTT_TOPIC_OUTDOOR_CONSISTENCY:
                    self._latest_outdoor_consistency = payload
        except Exception:
            # Filet de sécurité générique : une règle qui plante sur un
            # message inattendu ne doit jamais arrêter l'abonné MQTT.
            logger.exception("Erreur lors du traitement du message %s", msg.topic)
            return
        self._dispatch(alerts)  # hors verrou : un serveur SMTP lent ne bloque pas l'abonné

    # -- surveillance périodique (perte de communication) -------------------

    def _watchdog_loop(self, interval_seconds: float = 30.0) -> None:
        while not self._stop_event.wait(interval_seconds):
            with self._lock:
                alerts = evaluate_rules(
                    telemetry=None,
                    prediction=self._latest_prediction,
                    state=self._state,
                    outdoor_consistency=self._latest_outdoor_consistency,
                )
            self._dispatch(alerts)

    def _dispatch(self, alerts: list[dict]) -> None:
        for alert in alerts:
            subject = f"[Voltaneum][{alert['level']}] {alert['rule_name']}"
            try:
                sent = send_email(subject, alert["message"], alert["level"])
            except Exception:
                # send_email ne devrait déjà rien lever, mais on double la
                # protection ici : ce thread ne doit jamais mourir (règle 6).
                logger.exception("send_email a levé une exception inattendue pour %s", alert["rule_name"])
                sent = False
            if not sent and smtp_configured():
                # `evaluate_rules` a déjà démarré le cooldown : on le raccourcit pour que
                # l'alerte soit retentée dans SEND_RETRY_SECONDS au lieu d'être perdue.
                # (E-mail non configuré : alertes désactivées, inutile de retenter.)
                with self._lock:
                    self._state["last_sent_at"][alert["rule_name"]] = (
                        time.time() - config.ALERT_COOLDOWN_MINUTES * 60 + SEND_RETRY_SECONDS
                    )
                logger.warning("Alerte %s non envoyée : nouvel essai dans %d s.",
                               alert["rule_name"], SEND_RETRY_SECONDS)

    # -- cycle de vie --------------------------------------------------------

    def run_forever(self) -> None:
        if hasattr(self._client, "reconnect_delay_set"):
            self._client.reconnect_delay_set(min_delay=1, max_delay=30)
        backoff = 1.0
        while not self._stop_event.is_set():
            try:
                self._client.connect(
                    self._mqtt_host,
                    self._mqtt_port,
                    keepalive=config.MQTT_KEEPALIVE_SECONDS,
                )
                break
            except Exception as exc:
                logger.error("Connexion initiale du notifier MQTT échouée : %s", exc)
                if self._stop_event.wait(backoff):
                    return
                backoff = min(backoff * 2, 30.0)
        self._client.loop_start()
        watchdog = threading.Thread(target=self._watchdog_loop, daemon=True)
        watchdog.start()
        try:
            self._stop_event.wait()
        finally:
            self.stop()

    def stop(self) -> None:
        self._stop_event.set()
        self._client.loop_stop()
        self._client.disconnect()
        logger.info("EmailNotifier arrêté proprement")


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    notifier = EmailNotifier()
    try:
        notifier.run_forever()
    except KeyboardInterrupt:
        notifier.stop()