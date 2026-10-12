"""Point d'entrée de Voltaneum / ITNET : démarre et supervise tous les modules.

Ce fichier ne contient aucune logique métier (ni PID, ni analyse du flux de la
passerelle) : il démarre chaque composant dans son propre thread, les relance s'ils
s'arrêtent de façon inattendue, et les arrête proprement sur SIGINT/SIGTERM.

Threads (tous supervisés : un thread mort est redémarré automatiquement) :
  - "gateway-collector"          : login, flux WebSocket de la passerelle, écriture SQLite
                                    et publication MQTT de la télémétrie ;
  - "control-loop"               : prédiction ML (si des modèles existent) puis calcul PID,
                                    publication d'une SUGGESTION de consigne sur MQTT ;
  - "storage-monitor"            : protection de l'espace disque (voir database/storage_manager.py) ;
  - "outdoor-sensor-listener"    : température extérieure du capteur indépendant (mode mqtt) ;
  - "outdoor-consistency-checker": comparaison capteur extérieur / météo (alerte de vérification).

Le notifier d'e-mails (notifications/email_notifier.py) et le monitoring des modèles
(run_model_monitor.py) tournent comme services séparés (voir deploy/).

Règle de sécurité centrale : aucun module n'écrit jamais sur un actionneur. Les
conditions de refroidissement (température cible, seuils de marche et d'arrêt du ventilateur
selon la température extérieure) et la consigne PID sont seulement PUBLIÉES sur MQTT ;
l'automate garde seul la décision finale.
"""

from __future__ import annotations

import json
import logging
import signal
import threading
import time
from collections.abc import Callable
from datetime import datetime, timedelta, timezone
from pathlib import Path

import paho.mqtt.client as mqtt

import config
from collectors.gateway_collector import GatewayCollector
from collectors.outdoor_consistency_checker import OutdoorConsistencyChecker
from collectors.outdoor_sensor_collector import OutdoorSensorListener, get_outdoor_temperature_http
from control.cooling_advice import compute_cooling_conditions
from control.pid_controller import compute_setpoint
from database import sqlite_client
from database.storage_manager import StorageManager
from messaging.mqtt import connect_with_retry, create_client
from ml.predictor import DualModelPredictor

logger = logging.getLogger("voltaneum.main")

# Format des horodatages stockés dans SQLite (UTC).
_DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# Période de contrôle des threads par le superviseur (secondes).
_SUPERVISOR_INTERVAL_SECONDS = 5.0


class VoltaneumApp:
    """Application : cycle de vie, supervision des threads et boucle de contrôle."""

    def __init__(self) -> None:
        self._stop_event = threading.Event()
        self._workers: dict[str, Callable[[], None]] = {}   # nom -> fonction à (re)lancer
        self._threads: dict[str, threading.Thread] = {}

        self.mqtt_client: mqtt.Client | None = None
        self.collector: GatewayCollector | None = None
        self.storage_manager: StorageManager | None = None
        self.outdoor_sensor: OutdoorSensorListener | None = None
        self.outdoor_consistency_checker: OutdoorConsistencyChecker | None = None

        self._predictor: DualModelPredictor | None = None
        self._predictor_report_mtime: float | None = None
        self._pid_state: dict = {}
        # Vrai tant que la boucle de contrôle est suspendue pour mesures périmées (un seul avertissement par panne).
        self._stale_data_reported = False

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Valide la configuration, initialise la base puis démarre tous les threads."""
        logger.info("Démarrage de Voltaneum / ITNET...")
        config.validate_config()
        sqlite_client.init_db(config.SQLITE_DB_PATH)

        self.mqtt_client = create_client("voltaneum-main")
        self.collector = GatewayCollector(mqtt_client=self.mqtt_client, db_path=config.SQLITE_DB_PATH)
        self.storage_manager = StorageManager(db_path=config.SQLITE_DB_PATH)
        self.outdoor_consistency_checker = OutdoorConsistencyChecker(
            get_sensor_value=self._read_outdoor_temperature
        )
        self._reload_predictor_if_changed()

        # Un broker indisponible ne bloque pas la collecte : les mesures sont écrites en base
        # même si elles ne sont pas publiées, et la connexion est retentée en arrière-plan.
        self._register("mqtt-connection", self._mqtt_connection_worker)
        self._register("gateway-collector", self.collector.run_forever)
        self._register("control-loop", self._control_loop)
        self._register("storage-monitor", self.storage_manager.run_forever)
        self._register("outdoor-consistency-checker", self.outdoor_consistency_checker.run_forever)
        if config.OUTDOOR_SENSOR_MODE == "mqtt":
            self.outdoor_sensor = OutdoorSensorListener()
            self._register("outdoor-sensor-listener", self.outdoor_sensor.run_forever)

        threading.Thread(target=self._supervisor_loop, name="component-supervisor", daemon=True).start()
        logger.info("Voltaneum démarré (%d thread(s) supervisé(s)).", len(self._threads))

    def stop(self) -> None:
        """Arrête proprement tous les threads et ferme les connexions."""
        logger.info("Arrêt de Voltaneum / ITNET...")
        self._stop_event.set()
        for component in (self.collector, self.storage_manager, self.outdoor_sensor,
                          self.outdoor_consistency_checker):
            if component is not None:
                component.stop()

        for name, thread in self._threads.items():
            thread.join(timeout=10)
            if thread.is_alive():
                logger.warning("Le thread '%s' ne s'est pas arrêté dans le délai imparti.", name)

        if self.mqtt_client is not None:
            try:
                self.mqtt_client.loop_stop()
                self.mqtt_client.disconnect()
            except Exception:
                logger.exception("Erreur lors de la déconnexion MQTT")
        logger.info("Voltaneum arrêté.")

    def run_forever(self) -> None:
        """Démarre l'application et bloque jusqu'à SIGINT/SIGTERM (puis arrête tout)."""

        def _request_stop(signum, _frame) -> None:
            logger.info("Signal %s reçu : arrêt en cours.", signal.Signals(signum).name)
            self._stop_event.set()

        # Installés AVANT start() : un signal reçu pendant le démarrage doit aussi être pris en compte.
        signal.signal(signal.SIGINT, _request_stop)
        signal.signal(signal.SIGTERM, _request_stop)
        try:
            self.start()
            while not self._stop_event.wait(1.0):
                pass
        finally:
            self.stop()

    # ------------------------------------------------------------------
    # Supervision des threads
    # ------------------------------------------------------------------
    def _register(self, name: str, target: Callable[[], None]) -> None:
        """Enregistre un worker et le démarre ; le superviseur le relancera s'il s'arrête."""
        self._workers[name] = target
        self._spawn(name)

    def _spawn(self, name: str) -> None:
        thread = threading.Thread(target=self._workers[name], name=name, daemon=True)
        self._threads[name] = thread
        thread.start()

    def _supervisor_loop(self) -> None:
        """Relance tout worker arrêté de façon inattendue (exception, perte de connexion…)."""
        while not self._stop_event.wait(_SUPERVISOR_INTERVAL_SECONDS):
            for name, thread in list(self._threads.items()):
                if not thread.is_alive() and not self._stop_event.is_set():
                    if name == "mqtt-connection":
                        continue  # worker « à usage unique » : se termine normalement une fois connecté
                    logger.error("Le thread '%s' est arrêté : redémarrage.", name)
                    self._spawn(name)

    def _mqtt_connection_worker(self) -> None:
        """Connecte le client MQTT principal (avec reprise tant que le broker est injoignable)."""
        if self.mqtt_client is not None and connect_with_retry(self.mqtt_client, self._stop_event):
            self.mqtt_client.loop_start()
            logger.info("Connecté au broker MQTT %s:%d.", config.MQTT_HOST, config.MQTT_PORT)

    # ------------------------------------------------------------------
    # Boucle de contrôle
    # ------------------------------------------------------------------
    def _control_loop(self) -> None:
        """Appelle `_control_tick` toutes les CONTROL_LOOP_INTERVAL_SECONDS."""
        last_time = time.monotonic()
        while not self._stop_event.wait(config.CONTROL_LOOP_INTERVAL_SECONDS):
            now = time.monotonic()
            dt, last_time = now - last_time, now
            try:
                self._control_tick(dt)
            except Exception:
                logger.exception("Erreur inattendue dans la boucle de contrôle")

    def _control_tick(self, dt: float) -> None:
        """Un cycle : lecture des mesures -> prédiction ML -> PID -> publication de la consigne.

        Dégradation gracieuse : sans température extérieure, ou sans modèle ML, le
        PID et son bypass de sécurité (température critique) continuent de fonctionner.
        Seule l'absence de la température d'huile suspend le cycle.
        """
        collector = self.collector
        if collector is None:
            return

        # Mesures périmées : si la passerelle ne parle plus, la dernière température connue reste
        # « figée » dans le collecteur. Publier des conditions calculées dessus, avec un horodatage
        # récent, tromperait l'automate. On ne publie donc RIEN : au-delà de `valid_for_seconds`,
        # l'automate ignore de toute façon le dernier message et reprend sa propre régulation.
        data_age = collector.seconds_since_last_message()
        if data_age is None or data_age > config.CONTROL_MAX_DATA_AGE_SECONDS:
            if not self._stale_data_reported:
                logger.warning(
                    "Aucun message de la passerelle depuis %s s (seuil %.0f s) : mesures périmées, "
                    "rien n'est publié.",
                    "plus longtemps que le démarrage" if data_age is None else f"{data_age:.0f}",
                    config.CONTROL_MAX_DATA_AGE_SECONDS,
                )
                self._stale_data_reported = True
            return
        if self._stale_data_reported:
            logger.info("Messages de la passerelle de nouveau reçus : publication reprise.")
            self._stale_data_reported = False

        last_known = collector.snapshot()
        t_oil = last_known.get(config.TARGET_COLUMN)
        if t_oil is None:
            logger.debug("Pas encore de mesure %s : cycle de contrôle suspendu.", config.TARGET_COLUMN)
            return

        # None si le capteur est absent ou muet : on n'utilise jamais une valeur périmée.
        t_outdoor = self._read_outdoor_temperature()
        collector.set_value("temp_outdoor", t_outdoor)
        last_known["temp_outdoor"] = t_outdoor

        t_oil_predicted = self._predict_if_possible(last_known, t_oil)
        setpoint, self._pid_state, mode = compute_setpoint(
            t_oil=t_oil,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=t_oil_predicted,
            t_outdoor=t_outdoor,
            dt=dt,
            pid_state=self._pid_state,
        )
        # 1) Conditions à suivre (contrat principal) : température cible + seuils selon la
        #    température extérieure. Rien n'est publié si la température d'huile est invalide.
        conditions = compute_cooling_conditions(t_oil, t_outdoor)
        if conditions is not None:
            self._publish(config.MQTT_TOPIC_COOLING_CONDITIONS, conditions)
        # 2) Consigne PID en % : INFORMATIVE (suivi, comparaison avec la régulation de l'automate).
        if config.PUBLISH_PID_SETPOINT:
            self._publish(config.MQTT_TOPIC_SETPOINT, {"percent": setpoint, "mode": mode})
        logger.info(
            "Consigne suggérée : %.1f %% (mode=%s, huile=%.1f °C, cible=%.1f °C, prédit=%.1f °C, ext=%s).",
            setpoint, mode, t_oil, config.TARGET_TEMP, t_oil_predicted,
            "inconnue" if t_outdoor is None else f"{t_outdoor:.1f} °C",
        )

    def _read_outdoor_temperature(self) -> float | None:
        """Température extérieure ACTUELLE du capteur indépendant, ou None si indisponible.

        Aucune valeur de repli : une mesure périmée ne doit pas passer pour actuelle.
        """
        if config.OUTDOOR_SENSOR_MODE == "mqtt":
            return None if self.outdoor_sensor is None else self.outdoor_sensor.get_last_value()
        try:
            return get_outdoor_temperature_http()
        except Exception as exc:
            logger.warning("Capteur extérieur (mode http) indisponible : %s", exc)
            return None

    # ------------------------------------------------------------------
    # Prédiction ML
    # ------------------------------------------------------------------
    def _reload_predictor_if_changed(self) -> None:
        """(Re)charge les modèles quand `ml.train` en a écrit de nouveaux : pas de redémarrage nécessaire."""
        report = Path(config.MODEL_DIR) / "training_report.json"
        try:
            mtime = report.stat().st_mtime
        except FileNotFoundError:
            return  # aucun modèle entraîné : mode dégradé (PID seul)
        if mtime == self._predictor_report_mtime:
            return
        try:
            self._predictor = DualModelPredictor(model_dir=config.MODEL_DIR)
            self._predictor_report_mtime = mtime
            logger.info("Modèles de prédiction chargés (meilleur : %s).", self._predictor.best_model_name)
        except Exception as exc:  # fichiers incomplets/corrompus : on garde l'ancien prédicteur
            logger.error("Chargement des modèles impossible, ancienne version conservée : %s", exc)

    def _predict_if_possible(self, last_known: dict, t_oil_current: float) -> float:
        """Température d'huile prédite, ou la température actuelle si la prédiction est impossible
        (ce qui neutralise simplement le terme prédictif du PID)."""
        self._reload_predictor_if_changed()
        if self._predictor is None:
            return t_oil_current
        if any(last_known.get(column) is None for column in config.FEATURE_COLUMNS):
            logger.debug("Variables d'entrée incomplètes : prédiction ML ignorée pour ce cycle.")
            return t_oil_current
        try:
            prediction = self._predictor.predict({c: last_known[c] for c in config.FEATURE_COLUMNS})
        except Exception:
            logger.exception("Échec de la prédiction ML")
            return t_oil_current

        self._publish(config.MQTT_TOPIC_PREDICTION, prediction)
        self._log_predictions(prediction)
        return prediction["combined_prediction"]

    def _log_predictions(self, prediction: dict) -> None:
        """Journalise chaque prédiction en base : `ml/monitor.py` la comparera ensuite à la
        mesure réelle à l'échéance (vérification prédiction/réalité en continu)."""
        horizon = int(prediction["horizon_seconds"])
        made_at = datetime.now(timezone.utc)
        target_ts = made_at + timedelta(seconds=horizon)
        try:
            for model_name, value in prediction["predictions"].items():
                sqlite_client.log_prediction(
                    made_at.strftime(_DB_TIME_FORMAT), target_ts.strftime(_DB_TIME_FORMAT),
                    horizon, model_name, float(value), db_path=config.SQLITE_DB_PATH,
                )
        except Exception:
            logger.exception("Échec de la journalisation des prédictions")

    def _publish(self, topic: str, payload: dict) -> None:
        """Publie `payload` (+ horodatage) sur MQTT ; une panne de publication n'interrompt jamais le contrôle."""
        if self.mqtt_client is None:
            return
        try:
            self.mqtt_client.publish(topic, json.dumps({**payload, "ts": time.time()}))
        except Exception:
            logger.exception("Échec de publication MQTT sur %s", topic)


def main() -> None:
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    VoltaneumApp().run_forever()


if __name__ == "__main__":
    main()
