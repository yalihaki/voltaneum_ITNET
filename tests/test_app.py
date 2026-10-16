"""Tests de main.py : cycle de vie, supervision des threads et boucle de contrôle.

Aucun réseau réel : collecteur, client MQTT, capteur extérieur et vérificateur de
cohérence sont remplacés par des doublures.
"""

import json
import threading
import time

import pytest

import config
from service import app as main_module
from database import sqlite_client


def wait_until(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class _Stoppable:
    """Doublure de composant à thread bloquant, arrêtable par `stop()`."""

    crash_first_runs = 0

    def __init__(self, *args, **kwargs):
        self._stop = threading.Event()
        self.runs = 0
        self.stop_called = False

    def run_forever(self):
        self.runs += 1
        if self.runs <= type(self).crash_first_runs:
            raise RuntimeError("panne simulée du thread")
        self._stop.wait()

    def stop(self):
        self.stop_called = True
        self._stop.set()


class FakeCollector(_Stoppable):
    def __init__(self, *args, **kwargs):
        super().__init__()
        self.last_known = {column: None for column in config.FEATURE_COLUMNS + [config.TARGET_COLUMN]}
        self.sources = {}
        self.kwargs = kwargs

    # Âge simulé du dernier message de la passerelle (None = aucun message encore attendu).
    message_age: float | None = 1.0

    def snapshot(self):
        return dict(self.last_known)

    def set_value(self, column, value, source=None):
        self.last_known[column] = value
        self.sources[column] = source

    def seconds_since_last_message(self):
        return self.message_age

    def stale_columns(self):
        return []


class FakeOutdoorSensor(_Stoppable):
    value = None

    def get_last_value(self):
        return type(self).value


class FakeChecker(_Stoppable):
    pass


class FakeMqttClient:
    def __init__(self):
        self.published: list[tuple[str, dict]] = []
        self.connect_failures = 0
        self.connect_calls = 0
        self.loop_started = threading.Event()
        self.loop_stopped = False
        self.disconnected = False
        self.publish_error: Exception | None = None

    def connect(self, host, port, keepalive=60):
        self.connect_calls += 1
        if self.connect_calls <= self.connect_failures:
            raise OSError("broker injoignable")

    def loop_start(self):
        self.loop_started.set()

    def loop_stop(self):
        self.loop_stopped = True

    def disconnect(self):
        self.disconnected = True

    def publish(self, topic, payload):
        if self.publish_error:
            raise self.publish_error
        self.published.append((topic, json.loads(payload)))

    def messages(self, topic):
        return [payload for t, payload in self.published if t == topic]


class FakePredictor:
    """Remplace DualModelPredictor : retourne une prédiction fixe."""

    instances: list = []
    fail_on_init = False
    fail_on_predict = False
    best_model_name = "random_forest"

    def __init__(self, model_dir=None):
        if type(self).fail_on_init:
            raise ValueError("fichiers de modèle corrompus")
        self.model_dir = model_dir
        type(self).instances.append(self)

    def predict(self, features):
        if type(self).fail_on_predict:
            raise RuntimeError("modèle défaillant")
        return {
            "predictions": {"random_forest": 33.0, "gradient_boosting": 34.0},
            "combined_prediction": 33.5, "disagreement": 1.0, "disagreement_flag": False,
            "horizon_seconds": 600,
        }


class FakeWeather:
    """Remplace l'API météo : retourne `value`, ou lève si elle vaut None."""

    value = None
    calls = 0

    def __call__(self):
        type(self).calls += 1
        if type(self).value is None:
            raise ConnectionError("API météo injoignable")
        return type(self).value


@pytest.fixture
def app(tmp_path, monkeypatch):
    for name, value in {
        "SQLITE_DB_PATH": str(tmp_path / "t.db"), "MODEL_DIR": str(tmp_path / "models"),
        "VOLTANEUM_USER": "test-user", "VOLTANEUM_PASSWORD": "test-password",
        "OUTDOOR_SENSOR_MODE": "mqtt", "DASHBOARD_ENABLED": False, "RETRAIN_AT": "",
    }.items():
        monkeypatch.setattr(config, name, value)
    FakeOutdoorSensor.value = None
    FakePredictor.instances, FakePredictor.fail_on_init, FakePredictor.fail_on_predict = [], False, False
    for cls in (FakeCollector, FakeOutdoorSensor, FakeChecker):
        cls.crash_first_runs = 0
    client = FakeMqttClient()
    monkeypatch.setattr(main_module, "GatewayCollector", FakeCollector)
    monkeypatch.setattr(main_module, "OutdoorSensorListener", FakeOutdoorSensor)
    monkeypatch.setattr(main_module, "OutdoorConsistencyChecker", FakeChecker)
    monkeypatch.setattr(main_module, "DualModelPredictor", FakePredictor)
    monkeypatch.setattr(main_module, "create_client", lambda client_id: client)
    FakeWeather.value, FakeWeather.calls = None, 0   # météo injoignable par défaut (jamais de vrai appel réseau)
    monkeypatch.setattr(main_module, "get_outdoor_conditions", FakeWeather())
    application = main_module.VoltaneumApp()
    application.fake_client = client
    yield application
    application.stop()


def feed_measurements(app, t_oil=30.0):
    for column in config.FEATURE_COLUMNS:
        app.collector.last_known[column] = 25.0
    app.collector.last_known[config.TARGET_COLUMN] = t_oil


class TestLifecycle:
    def test_start_creates_all_supervised_threads(self, app):
        app.start()
        assert set(app._threads) == {
            "mqtt-connection", "gateway-collector", "control-loop", "storage-monitor",
            "outdoor-consistency-checker", "outdoor-sensor-listener", "alerts", "model-monitor",
        }
        assert app.fake_client.loop_started.wait(2)

    def test_http_mode_does_not_start_the_mqtt_listener(self, app, monkeypatch):
        monkeypatch.setattr(config, "OUTDOOR_SENSOR_MODE", "http")
        monkeypatch.setattr(config, "OUTDOOR_SENSOR_HTTP_URL", "http://capteur.local/temp")
        app.start()
        assert "outdoor-sensor-listener" not in app._threads and app.outdoor_sensor is None

    def test_stop_leaves_no_running_thread(self, app):
        app.start()
        app.stop()
        assert not any(thread.is_alive() for thread in app._threads.values())

    def test_stop_closes_mqtt_and_stops_every_component(self, app):
        app.start()
        app.stop()
        assert app.fake_client.loop_stopped and app.fake_client.disconnected
        for component in (app.collector, app.storage_manager, app.outdoor_sensor,
                          app.outdoor_consistency_checker):
            assert component is not None
        assert app.collector.stop_called and app.outdoor_sensor.stop_called
        assert app.outdoor_consistency_checker.stop_called

    def test_double_stop_is_safe(self, app):
        app.start()
        app.stop()
        app.stop()
        assert not any(thread.is_alive() for thread in app._threads.values())

    def test_missing_models_does_not_prevent_startup(self, app):
        app.start()
        assert app._predictor is None

    def test_unreachable_broker_does_not_block_startup_and_connects_later(self, app):
        app.fake_client.connect_failures = 1
        app.start()                                   # ne bloque pas
        assert app.fake_client.loop_started.wait(5)   # la connexion aboutit au 2e essai
        assert app.fake_client.connect_calls == 2

    def test_invalid_configuration_refuses_to_start(self, app, monkeypatch):
        monkeypatch.setattr(config, "VOLTANEUM_PASSWORD", None)
        with pytest.raises(ValueError):
            app.start()


class TestSupervisor:
    def test_restarts_a_worker_that_exits_unexpectedly(self, app, monkeypatch):
        monkeypatch.setattr(main_module, "_SUPERVISOR_INTERVAL_SECONDS", 0.02)
        runs = []
        app._register("fragile", lambda: runs.append(1))        # se termine aussitôt
        threading.Thread(target=app._supervisor_loop, daemon=True).start()
        assert wait_until(lambda: len(runs) >= 3)

    @pytest.mark.filterwarnings("ignore::pytest.PytestUnhandledThreadExceptionWarning")
    def test_restarts_the_outdoor_sensor_listener_after_a_crash(self, app, monkeypatch):
        """Régression : seuls 3 threads étaient redémarrés ; le capteur extérieur et le
        vérificateur de cohérence restaient morts après une panne du broker au démarrage."""
        monkeypatch.setattr(main_module, "_SUPERVISOR_INTERVAL_SECONDS", 0.02)
        FakeOutdoorSensor.crash_first_runs = 2
        FakeChecker.crash_first_runs = 1
        app.start()
        assert wait_until(lambda: app.outdoor_sensor.runs >= 3)
        assert wait_until(lambda: app.outdoor_consistency_checker.runs >= 2)
        assert app._threads["outdoor-sensor-listener"].is_alive()

    def test_does_not_restart_workers_once_stopping(self, app, monkeypatch):
        monkeypatch.setattr(main_module, "_SUPERVISOR_INTERVAL_SECONDS", 0.02)
        runs = []
        app._register("fragile", lambda: runs.append(1))
        app.stop()
        count = len(runs)
        threading.Thread(target=app._supervisor_loop, daemon=True).start()
        time.sleep(0.15)
        assert len(runs) == count


class TestControlTick:
    def test_nothing_published_without_oil_temperature(self, app):
        app.start()
        app._control_tick(1.0)
        assert app.fake_client.messages(config.MQTT_TOPIC_SETPOINT) == []

    def test_nothing_published_when_oil_temperature_is_stale(self, app, monkeypatch):
        """Régression : une mesure PT02 périmée ne doit jamais produire de consigne."""
        from collectors.gateway_collector import GatewayCollector

        monkeypatch.setattr(config, "SENSOR_MAX_AGE_SECONDS", 300)
        app.start()
        collector = GatewayCollector(username="u", password="p")
        collector._last_message_at = time.monotonic()   # la passerelle émet (seul PT02 va se taire)
        clock = {"t": 0.0}
        collector._clock = lambda: clock["t"]
        for column in config.FEATURE_COLUMNS:
            collector.set_value(column, 25.0)
        collector.set_value(config.TARGET_COLUMN, 40.0)
        app.collector = collector
        FakeOutdoorSensor.value = 15.0

        app._control_tick(1.0)
        assert len(app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)) == 1

        clock["t"] += 301  # la passerelle s'est tue
        app._control_tick(1.0)
        assert len(app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)) == 1
        assert app._target_stale_reported

        collector.set_value(config.TARGET_COLUMN, 41.0)  # mesure revenue
        app._control_tick(1.0)
        assert len(app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)) == 2
        assert not app._target_stale_reported

    def test_publishes_a_setpoint_suggestion(self, app):
        app.start()
        feed_measurements(app, t_oil=40.0)
        FakeOutdoorSensor.value = 15.0
        app._control_tick(1.0)
        (message,) = app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)
        assert 0.0 <= message["percent"] <= 100.0 and "ts" in message

    def test_each_cycle_is_logged_with_its_pid_terms(self, app):
        """Journal pour l'interface : la consigne enregistrée est celle publiée, et la somme des
        termes enregistrés redonne bien la consigne (avant bornage)."""
        app.start()
        feed_measurements(app, t_oil=40.0)
        FakeOutdoorSensor.value = 15.0
        app._control_tick(60.0)
        (message,) = app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)
        with sqlite_client.connect(config.SQLITE_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT setpoint, mode, p_term, i_term, d_term, feedforward_prediction, feedforward_outdoor,"
                " raw_output, outdoor_band, t_oil, t_outdoor FROM control_log").fetchall()
        (setpoint, mode, p, i, d, ff_pred, ff_out, raw, band, t_oil, t_outdoor), = rows
        assert setpoint == message["percent"] and mode == message["mode"]
        assert raw == pytest.approx(p + i + d + ff_pred + ff_out)
        assert setpoint == pytest.approx(max(0.0, min(100.0, raw)))
        assert (band, t_oil, t_outdoor) == ("tempere", 40.0, 15.0)

    def test_exact_mqtt_messages_are_logged(self, app):
        """Le journal garde le message JSON exact transmis au broker, et le résultat de l'envoi."""
        app.start()
        feed_measurements(app, t_oil=40.0)
        FakeOutdoorSensor.value = 15.0
        app._control_tick(60.0)
        app.fake_client.publish_error = OSError("broker injoignable")
        app._control_tick(60.0)
        with sqlite_client.connect(config.SQLITE_DB_PATH) as conn:
            rows = conn.execute("SELECT mqtt_setpoint, mqtt_conditions, mqtt_sent FROM control_log ORDER BY id").fetchall()
        (setpoint_msg, conditions_msg, sent), failed = rows
        assert json.loads(setpoint_msg) == app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)[0]
        assert json.loads(conditions_msg) == app.fake_client.messages(config.MQTT_TOPIC_COOLING_CONDITIONS)[0]
        assert sent == 1
        assert failed == (None, None, 0)

    def test_publication_disabled_is_logged_as_such(self, app, monkeypatch):
        monkeypatch.setattr(config, "PUBLISH_PID_SETPOINT", False)
        app.start()
        feed_measurements(app, t_oil=40.0)
        app._control_tick(60.0)
        with sqlite_client.connect(config.SQLITE_DB_PATH) as conn:
            assert conn.execute("SELECT mqtt_setpoint, mqtt_sent FROM control_log").fetchone() == (None, None)

    def test_nothing_logged_without_oil_temperature(self, app):
        app.start()
        app._control_tick(1.0)
        with sqlite_client.connect(config.SQLITE_DB_PATH) as conn:
            assert conn.execute("SELECT COUNT(*) FROM control_log").fetchone()[0] == 0

    def test_safety_bypass_requests_full_cooling(self, app):
        app.start()
        feed_measurements(app, t_oil=config.CRITICAL_TEMP + 5)
        FakeOutdoorSensor.value = 15.0
        app._control_tick(1.0)
        (message,) = app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)
        assert message["percent"] == 100.0 and message["mode"] == "safety_bypass"

    def test_control_continues_without_outdoor_temperature(self, app):
        """Régression de sécurité : sans température extérieure (capteur pas encore installé),
        le cycle s'arrêtait — bypass de sécurité compris."""
        app.start()
        FakeOutdoorSensor.value = None
        feed_measurements(app, t_oil=config.CRITICAL_TEMP + 5)
        app._control_tick(1.0)
        (message,) = app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)
        assert message["percent"] == 100.0

    # ------------------------------------------------------------------
    # Conditions de refroidissement publiées sur MQTT
    # ------------------------------------------------------------------
    def test_publishes_cooling_conditions_with_target_and_thresholds(self, app):
        app.start()
        feed_measurements(app, t_oil=36.0)
        FakeOutdoorSensor.value = 33.0                        # air chaud : le ventilateur démarre plus tôt
        app._control_tick(1.0)
        (message,) = app.fake_client.messages(config.MQTT_TOPIC_COOLING_CONDITIONS)
        assert message["target_temp"] == config.TARGET_TEMP
        assert message["outdoor_band"] == "chaud"
        assert message["fan_on_above_temp"] < config.TARGET_TEMP
        assert message["fan_off_below_temp"] < message["fan_on_above_temp"] < message["critical_temp"]
        assert message["valid_for_seconds"] > 0 and "ts" in message

    def test_cooling_conditions_published_without_outdoor_temperature(self, app):
        app.start()
        FakeOutdoorSensor.value = None
        feed_measurements(app, t_oil=36.0)
        app._control_tick(1.0)
        (message,) = app.fake_client.messages(config.MQTT_TOPIC_COOLING_CONDITIONS)
        assert message["outdoor_band"] == "inconnue" and message["fan_min_freq_hz"] is None

    def test_cold_air_advice_is_published_and_logged(self, app):
        """Air à 5 °C, PT02 un peu au-dessus de la cible : l'air seul suffit, ventilateur arrêté."""
        app.start()
        FakeOutdoorSensor.value = 5.0
        feed_measurements(app, t_oil=config.TARGET_TEMP + 0.5)
        app._control_tick(1.0)
        (message,) = app.fake_client.messages(config.MQTT_TOPIC_COOLING_CONDITIONS)
        assert message["cooling_mode"] == "air_exterieur" and message["cooling_mode_reason"]
        with sqlite_client.connect(config.SQLITE_DB_PATH) as conn:
            (logged,) = conn.execute("SELECT cooling_mode FROM control_log").fetchone()
        assert logged == "air_exterieur"

    def test_warm_air_advice_is_the_fan(self, app):
        app.start()
        FakeOutdoorSensor.value = 20.0
        feed_measurements(app, t_oil=config.TARGET_TEMP + 0.5)
        app._control_tick(1.0)
        (message,) = app.fake_client.messages(config.MQTT_TOPIC_COOLING_CONDITIONS)
        assert message["cooling_mode"] == "ventilateur"

    def test_pid_percent_can_be_switched_off_without_losing_the_conditions(self, app, monkeypatch):
        monkeypatch.setattr(config, "PUBLISH_PID_SETPOINT", False)
        app.start()
        feed_measurements(app, t_oil=40.0)
        app._control_tick(1.0)
        assert app.fake_client.messages(config.MQTT_TOPIC_SETPOINT) == []
        assert len(app.fake_client.messages(config.MQTT_TOPIC_COOLING_CONDITIONS)) == 1

    # ------------------------------------------------------------------
    # Mesures périmées : rien ne doit être publié
    # ------------------------------------------------------------------
    def test_nothing_published_when_gateway_stream_is_stale(self, app):
        """Régression : avec un flux coupé, la dernière température restait figée et le PID
        continuait de publier avec un horodatage récent, trompant l'automate."""
        app.start()
        feed_measurements(app, t_oil=40.0)
        app.collector.message_age = config.CONTROL_MAX_DATA_AGE_SECONDS + 1
        app._control_tick(1.0)
        assert app.fake_client.messages(config.MQTT_TOPIC_COOLING_CONDITIONS) == []
        assert app.fake_client.messages(config.MQTT_TOPIC_SETPOINT) == []

    def test_nothing_published_before_any_gateway_message(self, app):
        app.start()
        feed_measurements(app, t_oil=40.0)
        app.collector.message_age = None
        app._control_tick(1.0)
        assert app.fake_client.messages(config.MQTT_TOPIC_COOLING_CONDITIONS) == []

    def test_publication_resumes_when_the_stream_comes_back(self, app):
        app.start()
        feed_measurements(app, t_oil=40.0)
        app.collector.message_age = config.CONTROL_MAX_DATA_AGE_SECONDS + 1
        app._control_tick(1.0)
        assert app._stale_data_reported is True
        app.collector.message_age = 1.0
        app._control_tick(1.0)
        assert app._stale_data_reported is False
        assert len(app.fake_client.messages(config.MQTT_TOPIC_COOLING_CONDITIONS)) == 1

    def test_stale_warning_is_logged_once_per_outage(self, app, caplog):
        app.start()
        feed_measurements(app, t_oil=40.0)
        app.collector.message_age = config.CONTROL_MAX_DATA_AGE_SECONDS + 1
        with caplog.at_level("WARNING", logger="voltaneum.main"):
            for _ in range(5):
                app._control_tick(1.0)
        assert sum("périmées" in record.getMessage() for record in caplog.records) == 1

    def test_stale_outdoor_value_is_cleared_not_kept(self, app):
        app.start()
        feed_measurements(app, t_oil=30.0)
        FakeOutdoorSensor.value = 18.0
        app._control_tick(1.0)
        assert app.collector.last_known["temp_outdoor"] == 18.0
        FakeOutdoorSensor.value = None                       # capteur devenu muet
        app._control_tick(1.0)
        assert app.collector.last_known["temp_outdoor"] is None

    def test_http_mode_reads_the_sensor_and_survives_failures(self, app, monkeypatch):
        monkeypatch.setattr(config, "OUTDOOR_SENSOR_MODE", "http")
        monkeypatch.setattr(config, "OUTDOOR_SENSOR_HTTP_URL", "http://capteur.local/temp")
        readings = iter([21.5, RuntimeError("capteur HTTP hors service")])

        def fake_http():
            value = next(readings)
            if isinstance(value, Exception):
                raise value
            return value

        monkeypatch.setattr(main_module, "get_outdoor_temperature_http", fake_http)
        app.start()
        feed_measurements(app)
        app._control_tick(1.0)
        assert app.collector.last_known["temp_outdoor"] == 21.5
        app._control_tick(1.0)                               # panne : pas d'exception, valeur effacée
        assert app.collector.last_known["temp_outdoor"] is None
        assert len(app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)) == 2


    def test_mqtt_publish_failure_does_not_break_the_cycle(self, app):
        app.start()
        feed_measurements(app)
        app.fake_client.publish_error = RuntimeError("broker perdu")
        app._control_tick(1.0)                               # ne doit pas lever
        assert app._pid_state  # le calcul PID a bien eu lieu malgré l'échec de publication

    def test_control_loop_survives_a_failing_cycle(self, app, monkeypatch):
        monkeypatch.setattr(config, "CONTROL_LOOP_INTERVAL_SECONDS", 0.01)
        calls = []

        def failing_tick(dt):
            calls.append(dt)
            raise RuntimeError("cycle défaillant")

        monkeypatch.setattr(app, "_control_tick", failing_tick)
        thread = threading.Thread(target=app._control_loop, daemon=True)
        thread.start()
        assert wait_until(lambda: len(calls) >= 3)
        app.stop()
        thread.join(timeout=2)
        assert not thread.is_alive()


class TestOutdoorWeatherFallback:
    """Capteur prioritaire ; API météo utilisée seulement quand le capteur n'a pas de valeur."""

    def test_weather_used_when_sensor_absent(self, app):
        app.start()
        feed_measurements(app)
        FakeOutdoorSensor.value, FakeWeather.value = None, 12.0
        app._control_tick(1.0)
        assert app.collector.last_known["temp_outdoor"] == 12.0
        assert app.collector.sources["temp_outdoor"] == "weather"

    def test_sensor_has_priority_over_weather(self, app):
        app.start()
        feed_measurements(app)
        FakeOutdoorSensor.value, FakeWeather.value = 18.0, 12.0
        app._control_tick(1.0)
        assert app.collector.last_known["temp_outdoor"] == 18.0
        assert app.collector.sources["temp_outdoor"] == "sensor"
        assert FakeWeather.calls == 0                        # API pas appelée quand le capteur répond

    def test_switches_back_to_sensor_when_it_returns(self, app):
        app.start()
        feed_measurements(app)
        FakeOutdoorSensor.value, FakeWeather.value = None, 12.0
        app._control_tick(1.0)
        FakeOutdoorSensor.value = 18.0                       # capteur installé / revenu
        app._control_tick(1.0)
        assert app.collector.last_known["temp_outdoor"] == 18.0
        assert app.collector.sources["temp_outdoor"] == "sensor"

    def test_api_called_at_most_once_per_refresh_interval(self, app, monkeypatch):
        monkeypatch.setattr(config, "OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS", 3600)
        app.start()
        feed_measurements(app)
        FakeWeather.value = 12.0
        for _ in range(5):
            app._control_tick(1.0)
        assert FakeWeather.calls == 1
        assert app.collector.last_known["temp_outdoor"] == 12.0   # valeur en cache réutilisée

    def test_api_failure_keeps_recent_value_then_expires(self, app, monkeypatch):
        monkeypatch.setattr(config, "OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS", 1)
        monkeypatch.setattr(config, "OUTDOOR_SENSOR_MAX_AGE_SECONDS", 1800)
        clock = {"t": 1000.0}
        monkeypatch.setattr(main_module.time, "monotonic", lambda: clock["t"])
        app.start()
        feed_measurements(app)
        FakeWeather.value = 12.0
        app._control_tick(1.0)
        FakeWeather.value = None                             # API injoignable
        clock["t"] += 60
        app._control_tick(1.0)
        assert app.collector.last_known["temp_outdoor"] == 12.0   # encore récente
        clock["t"] += 1800
        app._control_tick(1.0)
        assert app.collector.last_known["temp_outdoor"] is None   # trop ancienne : jamais inventée

    def test_cooling_conditions_report_the_outdoor_source(self, app):
        app.start()
        feed_measurements(app)
        app.collector.message_age = 1.0
        FakeOutdoorSensor.value, FakeWeather.value = None, 12.0
        app._control_tick(1.0)
        (message,) = app.fake_client.messages(config.MQTT_TOPIC_COOLING_CONDITIONS)
        assert message["t_outdoor"] == 12.0 and message["t_outdoor_source"] == "weather"

    def test_collector_receives_the_same_outdoor_source(self, app):
        """Le collecteur renseigne temp_outdoor à chaque instantané avec le même fournisseur."""
        app.start()
        assert app.collector.kwargs["outdoor_temperature"] == app.outdoor_temperature.read

    def test_fallback_can_be_disabled(self, app, monkeypatch):
        monkeypatch.setattr(config, "OUTDOOR_WEATHER_FALLBACK_ENABLED", False)
        app.start()
        feed_measurements(app)
        FakeWeather.value = 12.0
        app._control_tick(1.0)
        assert app.collector.last_known["temp_outdoor"] is None and FakeWeather.calls == 0

    def test_consistency_check_never_compares_weather_with_itself(self, app):
        app.start()
        FakeOutdoorSensor.value, FakeWeather.value = None, 12.0
        assert app._read_outdoor_temperature() is None


class TestPredictions:
    def _install_models(self, app):
        model_dir = config.MODEL_DIR
        import os
        os.makedirs(model_dir, exist_ok=True)
        report = os.path.join(model_dir, "training_report.json")
        with open(report, "w") as handle:
            handle.write("{}")
        return report

    def test_predictions_are_logged_for_the_monitoring(self, app):
        """Régression : `log_prediction` n'était jamais appelée en production, donc la
        comparaison prédiction/réalité (ml/monitor.py) n'avait aucune donnée à traiter."""
        self._install_models(app)
        app.start()
        feed_measurements(app, t_oil=30.0)
        FakeOutdoorSensor.value = 15.0
        app._control_tick(1.0)

        with sqlite_client.connect(config.SQLITE_DB_PATH) as conn:
            rows = conn.execute(
                "SELECT model_name, horizon_seconds, predicted_value, "
                "CAST(strftime('%s', target_ts) AS INTEGER) - CAST(strftime('%s', made_at) AS INTEGER) "
                "FROM predictions_log ORDER BY model_name").fetchall()
        assert rows == [("gradient_boosting", 600, 34.0, 600), ("random_forest", 600, 33.0, 600)]
        (published,) = app.fake_client.messages(config.MQTT_TOPIC_PREDICTION)
        assert published["combined_prediction"] == 33.5 and "ts" in published

    def test_disagreeing_models_are_not_used_by_the_pid(self, app, monkeypatch):
        """Deux modèles en désaccord : la prédiction n'est pas fiable, l'anticipation du PID est
        neutralisée (le PID travaille sur la mesure) et rien n'est noté comme « prédiction utilisée »."""
        self._install_models(app)
        original = FakePredictor.predict
        monkeypatch.setattr(FakePredictor, "predict", lambda self, f: {
            **original(self, f), "predictions": {"random_forest": 45.0, "gradient_boosting": 33.0},
            "combined_prediction": 45.0, "disagreement": 12.0, "disagreement_flag": True})
        app.start()
        feed_measurements(app, t_oil=35.0)
        FakeOutdoorSensor.value = 15.0
        app._control_tick(60.0)
        with sqlite_client.connect(config.SQLITE_DB_PATH) as conn:
            t_predicted, ff_prediction = conn.execute(
                "SELECT t_predicted, feedforward_prediction FROM control_log").fetchone()
        assert t_predicted is None and ff_prediction == 0.0      # 45 °C prédits n'ont rien déclenché

    def test_prediction_out_of_its_learning_domain_is_not_used_by_the_pid(self, app, monkeypatch):
        """Une variable constante pendant l'apprentissage (pompe à 30 Hz) a changé : le modèle n'a
        jamais vu ce cas, l'anticipation du PID est neutralisée."""
        self._install_models(app)
        original = FakePredictor.predict
        monkeypatch.setattr(FakePredictor, "predict", lambda self, f: {
            **original(self, f), "combined_prediction": 45.0, "out_of_domain": ["freq_pompe"]})
        app.start()
        feed_measurements(app, t_oil=35.0)
        FakeOutdoorSensor.value = 15.0
        app._control_tick(60.0)
        with sqlite_client.connect(config.SQLITE_DB_PATH) as conn:
            t_predicted, ff_prediction = conn.execute(
                "SELECT t_predicted, feedforward_prediction FROM control_log").fetchone()
        assert t_predicted is None and ff_prediction == 0.0

    def test_no_prediction_when_an_input_variable_is_missing(self, app):
        self._install_models(app)
        app.start()
        feed_measurements(app, t_oil=30.0)
        FakeOutdoorSensor.value = None                       # temp_outdoor est une variable d'entrée
        app._control_tick(1.0)
        assert app.fake_client.messages(config.MQTT_TOPIC_PREDICTION) == []
        assert len(app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)) == 1

    def test_failing_predictor_degrades_to_pid_only(self, app):
        self._install_models(app)
        FakePredictor.fail_on_predict = True
        app.start()
        feed_measurements(app)
        FakeOutdoorSensor.value = 15.0
        app._control_tick(1.0)
        assert len(app.fake_client.messages(config.MQTT_TOPIC_SETPOINT)) == 1

    def test_new_models_are_loaded_without_restart(self, app):
        app.start()
        assert app._predictor is None
        report = self._install_models(app)
        feed_measurements(app)
        FakeOutdoorSensor.value = 15.0
        app._control_tick(1.0)
        assert app._predictor is not None and len(FakePredictor.instances) == 1
        app._control_tick(1.0)                               # rien n'a changé : pas de rechargement
        assert len(FakePredictor.instances) == 1
        import os
        os.utime(report, (time.time() + 10, time.time() + 10))   # `ml.train` vient de réécrire le rapport
        app._control_tick(1.0)
        assert len(FakePredictor.instances) == 2

    def test_corrupted_new_models_keep_the_previous_predictor(self, app):
        report = self._install_models(app)
        app.start()
        previous = app._predictor
        assert previous is not None
        FakePredictor.fail_on_init = True
        import os
        os.utime(report, (time.time() + 10, time.time() + 10))
        app._reload_predictor_if_changed()
        assert app._predictor is previous


class TestSingleProcessServices:
    """Tout tourne dans un seul processus : interface, ré-entraînement nocturne, alertes."""

    def test_dashboard_runs_inside_the_process(self, app, monkeypatch):
        import urllib.request
        import socket
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        free_port = probe.getsockname()[1]
        probe.close()
        monkeypatch.setattr(config, "DASHBOARD_ENABLED", True)
        monkeypatch.setattr(config, "DASHBOARD_PORT", free_port)
        app.start()
        port = app.dashboard.server_address[1]
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=5) as response:
            assert response.read() == b"ok"

    def test_busy_dashboard_port_does_not_stop_the_rest(self, app, monkeypatch):
        import socket
        busy = socket.socket()
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        monkeypatch.setattr(config, "DASHBOARD_ENABLED", True)
        monkeypatch.setattr(config, "DASHBOARD_PORT", busy.getsockname()[1])
        try:
            app.start()
            assert app.dashboard is None and "gateway-collector" in app._threads
        finally:
            busy.close()

    def test_nightly_retrain_runs_in_a_low_priority_subprocess(self, app, monkeypatch):
        calls = []

        class FakeProcess:
            def __init__(self, command, **kwargs):
                calls.append((command, kwargs))

            def wait(self):
                return 0

            def poll(self):
                return 0

        monkeypatch.setattr(main_module.subprocess, "Popen", FakeProcess)
        assert app.run_retrain_subprocess() == 0
        command, kwargs = calls[0]
        assert command[1].endswith("voltaneum.py") and command[2] == "retrain"
        assert kwargs["preexec_fn"] is not None            # priorité basse (nice)

    def test_pid_restarts_cold_after_a_gateway_outage(self, app):
        """Après une coupure, l'intégrale d'avant la panne ne doit pas peser sur la reprise."""
        app.start()
        feed_measurements(app, t_oil=40.0)
        FakeOutdoorSensor.value = 15.0
        for _ in range(5):
            app._control_tick(60.0)
        assert app._pid_state["integral"] > 0
        FakeCollector.message_age = config.CONTROL_MAX_DATA_AGE_SECONDS + 1     # passerelle muette
        try:
            app._control_tick(60.0)
        finally:
            FakeCollector.message_age = 1.0
        app._control_tick(60.0)                                                  # reprise
        assert app._pid_state["integral"] == pytest.approx(5.0 * 60.0)          # une seule minute d'écart

    def test_collector_snapshots_reach_the_alert_engine(self, app):
        app.start()
        assert app.collector.kwargs["on_snapshot"] == app.alerts.on_telemetry


class TestStartupRetrain:
    def test_needed_when_no_model_and_no_recent_run(self, app):
        assert app._startup_retrain_needed() is True

    def test_not_needed_after_a_recent_run(self, app):
        from ml import auto_retrain
        from datetime import datetime, timezone
        auto_retrain._append_history(config.MODEL_DIR, {"event": "run", "started_at": datetime.now(timezone.utc).isoformat()})
        assert app._startup_retrain_needed() is False
