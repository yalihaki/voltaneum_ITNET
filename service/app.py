"""service/app.py — Le processus Voltaneum : démarre, supervise et arrête tous les composants.

Un seul processus (lancé par `./voltaneum`), sans dépendance à un broker MQTT pour fonctionner :

  - gateway-collector     : login, flux WebSocket de la passerelle, instantané toutes les 15 s en base ;
  - control-loop          : prédiction ML (si un modèle validé est installé), PID, conditions de
                            refroidissement ; publication MQTT si un broker est joignable ;
  - alerts                : règles d'alerte évaluées à chaque instantané + chien de garde (30 s) ;
  - storage-monitor       : protection de l'espace disque (sauvegarde puis purge) ;
  - model-monitor         : erreur réelle des prédictions, comparées à la mesure à l'échéance ;
  - dashboard             : interface web en lecture seule ;
  - nightly-retrain       : ré-entraînement quotidien (processus séparé, basse priorité), qui
                            PROPOSE un modèle sans jamais l'installer ;
  - outdoor-sensor-listener / outdoor-consistency-checker : capteur extérieur et contrôle météo.

Chaque composant tourne dans un thread supervisé : s'il s'arrête, il est relancé.

Règle de sécurité centrale : aucun module n'écrit jamais sur un actionneur. Les conditions de
refroidissement et la consigne PID sont seulement PUBLIÉES ; l'automate garde seul la décision.
"""

from __future__ import annotations

import json
import logging
import os
import signal
import subprocess  # nosec B404
import sys
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
from collectors.outdoor_temperature import OutdoorTemperatureSource
from collectors.weather_collector import get_outdoor_conditions
from control.cooling_advice import compute_cooling_conditions
from control.supervisor import decide
from control.trend import slope_per_10min
from control.pid_controller import compute_setpoint_detailed
from database import sqlite_client
from database.storage_manager import StorageManager
from messaging.mqtt import connect_with_retry, create_client
from dashboard.server import DashboardServer, is_loopback, make_server
from ml.monitor import run_monitor_loop
from ml.predictor import DualModelPredictor
from notifications.alerts import AlertEngine

logger = logging.getLogger("voltaneum.app")

# Format des horodatages stockés dans SQLite (UTC).
_DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
# Période de contrôle des threads par le superviseur (secondes).
_SUPERVISOR_INTERVAL_SECONDS = 5.0
# Premier calcul de régulation peu après le démarrage (le temps de recevoir des mesures),
# au lieu d'attendre un cycle complet.
_FIRST_CONTROL_DELAY_SECONDS = 20.0
# Sans modèle installé, un ré-entraînement est lancé ce délai après le démarrage (s), au lieu
# d'attendre la nuit — sauf si un ré-entraînement a déjà eu lieu dans les dernières 24 h.
STARTUP_RETRAIN_DELAY_SECONDS = 300.0


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
        self.alerts: AlertEngine | None = None
        self.dashboard: DashboardServer | None = None

        self._predictor: DualModelPredictor | None = None
        self._predictor_report_mtime: float | None = None
        self._pid_state: dict = {}
        self._supervisor_state: dict = {}     # étape de la régulation (control/supervisor.py)
        self._resend_pending = False          # dernier message d'étape non transmis
        self._prediction_used = False
        self._disagreement_reported = False
        self._out_of_domain_reported = False
        self._retrain_process: subprocess.Popen | None = None
        # Vrai tant que la boucle de contrôle est suspendue pour mesures périmées (un seul avertissement par panne).
        self._stale_data_reported = False
        # Idem quand seule la mesure d'huile est périmée (capteur muet alors que les autres émettent).
        self._target_stale_reported = False

        # Température extérieure : capteur prioritaire, API météo en repli. Partagée entre le
        # collecteur (chaque instantané, 15 s) et la boucle de contrôle (cache commun).
        # `get_outdoor_conditions` est résolu à l'appel (remplaçable dans les tests).
        self.outdoor_temperature = OutdoorTemperatureSource(
            read_sensor=self._read_outdoor_temperature,
            fetch_weather=lambda: get_outdoor_conditions(),
        )

    # ------------------------------------------------------------------
    # Cycle de vie
    # ------------------------------------------------------------------
    def start(self) -> None:
        """Valide la configuration, initialise la base puis démarre tous les threads."""
        logger.info("Démarrage de Voltaneum / ITNET...")
        config.validate_config()
        sqlite_client.init_db(config.SQLITE_DB_PATH)

        if config.MQTT_HOST not in ("localhost", "127.0.0.1", "::1") and not config.MQTT_TLS_ENABLED:
            logger.warning("Broker MQTT distant (%s) sans TLS : les messages circulent en clair.", config.MQTT_HOST)

        self.alerts = AlertEngine()
        self.mqtt_client = create_client("voltaneum-main")
        self.collector = GatewayCollector(
            mqtt_client=self.mqtt_client, db_path=config.SQLITE_DB_PATH,
            outdoor_temperature=self.outdoor_temperature.read,
            on_snapshot=self.alerts.on_telemetry,
            outdoor_humidity=self.outdoor_temperature.humidity,
        )
        self.storage_manager = StorageManager(db_path=config.SQLITE_DB_PATH)
        self.outdoor_consistency_checker = OutdoorConsistencyChecker(
            get_sensor_value=self._read_outdoor_temperature,
            on_result=self.alerts.on_outdoor_consistency,
        )
        self._reload_predictor_if_changed()

        # Un broker indisponible ne bloque pas la collecte : les mesures sont écrites en base
        # même si elles ne sont pas publiées, et la connexion est retentée en arrière-plan.
        self._register("mqtt-connection", self._mqtt_connection_worker)
        self._register("gateway-collector", self.collector.run_forever)
        self._register("control-loop", self._control_loop)
        self._register("storage-monitor", self.storage_manager.run_forever)
        self._register("outdoor-consistency-checker", self.outdoor_consistency_checker.run_forever)
        self._register("alerts", self.alerts.run_forever)
        self._register("model-monitor", self._model_monitor)
        if config.RETRAIN_AT:
            self._register("nightly-retrain", self._retrain_scheduler)
        if config.DASHBOARD_ENABLED:
            self._start_dashboard()
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
                          self.outdoor_consistency_checker, self.alerts):
            if component is not None:
                component.stop()
        if self.dashboard is not None:
            self.dashboard.shutdown()
            self.dashboard.server_close()
        retrain = self._retrain_process
        if retrain is not None and retrain.poll() is None:
            logger.info("Ré-entraînement en cours interrompu par l'arrêt (aucun modèle n'est installé sans validation).")
            retrain.terminate()

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
        """Appelle `_control_tick` toutes les CONTROL_LOOP_INTERVAL_SECONDS (le premier peu après
        le démarrage)."""
        last_time = time.monotonic()
        delay = min(_FIRST_CONTROL_DELAY_SECONDS, config.CONTROL_LOOP_INTERVAL_SECONDS)
        while not self._stop_event.wait(delay):
            delay = config.CONTROL_LOOP_INTERVAL_SECONDS
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
        Seule l'absence de la température d'huile suspend le cycle : aucune consigne
        n'est alors publiée (jamais de consigne calculée sur une mesure périmée),
        l'automate reste sur sa propre logique.
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
            # Reprise après une coupure : l'état du PID (intégrale) date d'avant la panne et ne
            # correspond plus au procédé. Redémarrage « à froid » du PID.
            logger.info("Messages de la passerelle de nouveau reçus : publication reprise, PID réinitialisé.")
            self._stale_data_reported = False
            self._pid_state = {}
            self._supervisor_state = {}   # le dernier message a expiré côté automate : on repart du repos

        last_known = collector.snapshot()   # valeurs périmées capteur par capteur déjà remplacées par None
        t_oil = last_known.get(config.TARGET_COLUMN)
        if t_oil is None:
            if config.TARGET_COLUMN in collector.stale_columns():
                if not self._target_stale_reported:
                    logger.error(
                        "Mesure %s périmée (aucune valeur valide depuis plus de %d s) : "
                        "aucune consigne publiée tant qu'elle n'est pas revenue.",
                        config.TARGET_COLUMN, config.SENSOR_MAX_AGE_SECONDS,
                    )
                    self._target_stale_reported = True
            else:
                logger.debug("Pas de mesure %s valide : cycle de contrôle suspendu.", config.TARGET_COLUMN)
            return
        if self._target_stale_reported:
            logger.info("Mesure %s de nouveau disponible : reprise des consignes, PID réinitialisé.",
                        config.TARGET_COLUMN)
            self._target_stale_reported = False
            self._pid_state = {}
            self._supervisor_state = {}

        # Capteur prioritaire, API météo en repli ; None si aucune des deux n'est disponible.
        t_outdoor, outdoor_source = self.outdoor_temperature.read()
        collector.set_value("temp_outdoor", t_outdoor, source=outdoor_source)
        last_known["temp_outdoor"] = t_outdoor

        t_oil_predicted = self._predict_if_possible(last_known, t_oil)
        detail = compute_setpoint_detailed(
            t_oil=t_oil,
            t_target=config.TARGET_TEMP,
            t_oil_predicted=t_oil_predicted,
            t_outdoor=t_outdoor,
            dt=dt,
            pid_state=self._pid_state,
        )
        setpoint, self._pid_state, mode = detail.setpoint, detail.state, detail.mode

        # Régulation par étapes : publier seulement quand il y a quelque chose à dire (PT02 se
        # réchauffe, effet vérifié, renforcement, fin), jamais à l'aveugle chaque minute.
        now = time.time()
        slope = slope_per_10min(
            sqlite_client.recent_values(config.TARGET_COLUMN, now - config.REGULATION_TREND_WINDOW_SECONDS,
                                        db_path=config.SQLITE_DB_PATH),
            config.REGULATION_TREND_WINDOW_SECONDS)
        decision, self._supervisor_state = decide(
            t_oil, slope, t_outdoor, setpoint, t_oil_predicted if self._prediction_used else None,
            now, self._supervisor_state)
        event = decision.event
        if event is None and self._resend_pending:
            event = "renvoi"     # le message précédent n'a pas pu partir : on le renvoie
        if decision.event == "insuffisant" and self.alerts is not None:
            self.alerts.report("ATTENTION", "cooling_ineffective", decision.reason)

        conditions = compute_cooling_conditions(t_oil, t_outdoor)
        conditions_message = setpoint_message = None
        if event is not None and conditions is not None:
            step = {"phase": decision.phase, "event": event, "cooling_mode": decision.cooling_mode,
                    "cooling_mode_reason": decision.reason, "command_percent": decision.command_percent}
            # 1) Conditions et étape en cours (contrat principal).
            conditions_message = self._publish(
                config.MQTT_TOPIC_COOLING_CONDITIONS, {**conditions, "t_outdoor_source": outdoor_source, **step})
            # 2) Consigne conseillée au ventilateur, en % (avec la sortie brute du PID, pour comparaison).
            if config.PUBLISH_PID_SETPOINT:
                setpoint_message = self._publish(config.MQTT_TOPIC_SETPOINT, {
                    "percent": decision.command_percent, "pid_percent": setpoint, "mode": mode,
                    "phase": decision.phase, "event": event})
            self._resend_pending = conditions_message is None
            log = logger.warning if self._resend_pending else logger.info
            log("Étape %s (%s) : %s%s", decision.phase, event, decision.reason,
                " — NON transmis, renvoi au prochain cycle" if self._resend_pending else "")
        else:
            logger.debug("Rien à envoyer (%s) ; PID %.1f %%.", decision.reason, setpoint)

        # 3) Journal en base (interface) : chaque cycle, publié ou non.
        sqlite_client.log_control({
            "t_oil": t_oil, "t_target": config.TARGET_TEMP,
            "t_predicted": t_oil_predicted if self._prediction_used else None,
            "t_outdoor": t_outdoor, "t_outdoor_source": outdoor_source,
            "setpoint": setpoint, "mode": mode, "error": detail.error,
            "p_term": detail.p_term, "i_term": detail.i_term, "d_term": detail.d_term,
            "feedforward_prediction": detail.feedforward_prediction,
            "feedforward_outdoor": detail.feedforward_outdoor, "raw_output": detail.raw_output,
            "outdoor_band": conditions and conditions["outdoor_band"],
            "fan_on_above": conditions and conditions["fan_on_above_temp"],
            "fan_off_below": conditions and conditions["fan_off_below_temp"],
            "fan_min_freq_hz": conditions and conditions["fan_min_freq_hz"],
            "phase": decision.phase, "event": event, "command_percent": decision.command_percent,
            "cooling_mode": decision.cooling_mode, "cooling_mode_reason": decision.reason,
            "mqtt_setpoint": setpoint_message, "mqtt_conditions": conditions_message,
            # 1 transmis, 0 échec, NULL : rien à envoyer ce cycle.
            "mqtt_sent": None if event is None else int(conditions_message is not None),
        }, db_path=config.SQLITE_DB_PATH)

    # ------------------------------------------------------------------
    # Monitoring des modèles, interface, ré-entraînement nocturne
    # ------------------------------------------------------------------
    def _model_monitor(self) -> None:
        """Compare chaque prédiction à la mesure réelle à son échéance (erreur en production)."""
        run_monitor_loop(interval_seconds=30, mqtt_client=self.mqtt_client, stop_event=self._stop_event)

    def _start_dashboard(self) -> None:
        """Interface web en lecture seule, dans ce processus (thread du serveur HTTP)."""
        try:
            self.dashboard = make_server(config.DASHBOARD_HOST, config.DASHBOARD_PORT,
                                         config.SQLITE_DB_PATH, config.DASHBOARD_ALLOWED_HOSTS)
        except OSError as exc:
            logger.error("Interface web non démarrée (port %d indisponible : %s). Le reste tourne normalement.",
                         config.DASHBOARD_PORT, exc)
            return
        if not is_loopback(config.DASHBOARD_HOST):
            logger.warning("Interface ouverte au réseau (%s) SANS authentification : réseau de confiance uniquement.",
                           config.DASHBOARD_HOST)
        server = self.dashboard
        self._register("dashboard", lambda: server.serve_forever(poll_interval=0.5))
        logger.info("Interface de supervision : http://localhost:%d", config.DASHBOARD_PORT)

    def _retrain_scheduler(self) -> None:
        """Lance le ré-entraînement chaque jour à RETRAIN_AT (heure locale), dans un processus
        séparé à basse priorité : un entraînement long ne ralentit jamais la régulation."""
        hour, minute = (int(part) for part in config.RETRAIN_AT.split(":"))
        if self._startup_retrain_needed():
            logger.info("Aucun modèle de prédiction installé : ré-entraînement dans %.0f min (puis chaque "
                        "jour à %s).", STARTUP_RETRAIN_DELAY_SECONDS / 60, config.RETRAIN_AT)
            if self._stop_event.wait(STARTUP_RETRAIN_DELAY_SECONDS):
                return
            self.run_retrain_subprocess()
        while not self._stop_event.is_set():
            now = datetime.now()
            next_run = now.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if next_run <= now:
                next_run += timedelta(days=1)
            if self._stop_event.wait((next_run - now).total_seconds()):
                return
            self.run_retrain_subprocess()

    @staticmethod
    def _startup_retrain_needed() -> bool:
        """Vrai si aucun modèle validé n'est installé et qu'aucun ré-entraînement n'a eu lieu
        depuis 24 h (un redémarrage répété ne relance pas un entraînement à chaque fois)."""
        from ml import auto_retrain

        if auto_retrain.installed_model(config.MODEL_DIR) is not None:
            return False
        for event in auto_retrain.read_history(config.MODEL_DIR, limit=50):
            if event.get("event") == "run" and event.get("started_at"):
                try:
                    started = datetime.fromisoformat(event["started_at"])
                except ValueError:
                    continue
                if datetime.now(timezone.utc) - started < timedelta(hours=24):
                    return False
        return True

    def run_retrain_subprocess(self) -> int | None:
        """Exécute `voltaneum.py retrain` en sous-processus « nice » ; retourne son code de sortie."""
        script = Path(config.PROJECT_ROOT) / "voltaneum.py"
        command = [sys.executable, str(script), "retrain", "--log-file", str(Path(config.PROJECT_ROOT) / "logs" / "retrain.log")]
        logger.info("Ré-entraînement nocturne lancé (processus séparé, basse priorité).")
        try:
            self._retrain_process = subprocess.Popen(  # nosec B603
                command, cwd=config.PROJECT_ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                preexec_fn=(lambda: os.nice(19)) if hasattr(os, "nice") else None,
            )
            returncode = self._retrain_process.wait()
        except OSError:
            logger.exception("Ré-entraînement nocturne impossible à lancer")
            return None
        finally:
            self._retrain_process = None
        if self._stop_event.is_set():
            return returncode
        level = logging.INFO if returncode == 0 else logging.ERROR
        logger.log(level, "Ré-entraînement nocturne terminé (code %d, voir logs/retrain.log).", returncode)
        return returncode

    def _read_outdoor_temperature(self) -> float | None:
        """Température extérieure ACTUELLE du capteur indépendant, ou None si indisponible.

        Capteur seul (sert aussi à la vérification de cohérence capteur/météo, qui ne doit
        jamais comparer la météo à elle-même) ; le repli météo est fait par
        `collectors.outdoor_temperature.OutdoorTemperatureSource`.
        """
        if config.OUTDOOR_SENSOR_MODE == "none":
            return None  # aucun capteur installé
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
        """(Re)charge les modèles quand un modèle validé est installé (./voltaneum model accept) : pas de redémarrage nécessaire."""
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
        (ce qui neutralise simplement le terme prédictif du PID). `_prediction_used` indique
        si la valeur rendue vient réellement du modèle."""
        self._prediction_used = False
        self._reload_predictor_if_changed()
        if self._predictor is None:
            return t_oil_current
        columns = getattr(self._predictor, "required_columns", config.FEATURE_COLUMNS)
        if any(last_known.get(column) is None for column in columns):
            logger.debug("Variables d'entrée incomplètes : prédiction ML ignorée pour ce cycle.")
            return t_oil_current
        try:
            prediction = self._predictor.predict({c: last_known[c] for c in columns})
        except Exception:
            logger.exception("Échec de la prédiction ML")
            return t_oil_current

        self._publish(config.MQTT_TOPIC_PREDICTION, prediction)
        self._log_predictions(prediction)
        if self.alerts is not None:
            self.alerts.on_prediction(prediction)
        # Les deux modèles ne sont pas d'accord : la prédiction n'est pas fiable, l'anticipation
        # du PID est neutralisée pour ce cycle (le PID travaille sur la mesure actuelle).
        if prediction.get("disagreement_flag"):
            if not self._disagreement_reported:
                logger.warning("Modèles en désaccord (%.2f °C > %.1f °C) : prédiction ignorée par le PID "
                               "tant que le désaccord dure.", prediction.get("disagreement", float("nan")),
                               config.DISAGREEMENT_THRESHOLD_CELSIUS)
                self._disagreement_reported = True
            return t_oil_current
        if self._disagreement_reported:
            logger.info("Modèles de nouveau d'accord : prédiction utilisée par le PID.")
            self._disagreement_reported = False
        # Une variable restée constante pendant tout l'apprentissage a changé (ex. fréquence de la
        # pompe) : le modèle n'a jamais vu ce cas, sa prédiction est ignorée.
        out_of_domain = prediction.get("out_of_domain") or []
        if out_of_domain:
            if not self._out_of_domain_reported:
                logger.warning("Situation jamais vue à l'apprentissage (%s a changé) : prédiction ignorée "
                               "par le PID jusqu'au prochain ré-entraînement.", ", ".join(out_of_domain))
                self._out_of_domain_reported = True
            return t_oil_current
        self._out_of_domain_reported = False
        self._prediction_used = True
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

    def _publish(self, topic: str, payload: dict) -> str | None:
        """Publie `payload` (+ horodatage) sur MQTT ; une panne de publication n'interrompt jamais le contrôle.

        Retourne le message JSON exact transmis au client MQTT, ou None s'il n'a pas pu l'être
        (pas de client, broker non connecté, erreur) : il est journalisé dans `control_log`.
        """
        if self.mqtt_client is None:
            return None
        message = json.dumps({**payload, "ts": time.time()})
        try:
            info = self.mqtt_client.publish(topic, message)
        except Exception:
            logger.exception("Échec de publication MQTT sur %s", topic)
            return None
        rc = getattr(info, "rc", mqtt.MQTT_ERR_SUCCESS)
        if rc != mqtt.MQTT_ERR_SUCCESS:
            logger.debug("Message MQTT non transmis sur %s (code %s : broker non connecté ?)", topic, rc)
            return None
        return message

