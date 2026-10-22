"""Tests du moteur d'alertes (notifications/alerts.py).

Couvre toutes les règles de `evaluate_rules`, la sécurité de `send_email` (TLS vérifié,
en-têtes sûrs), la journalisation unique de `deliver_alert` et le moteur `AlertEngine`,
qui fonctionne sans aucun broker MQTT.
"""

import ssl
import threading
import time
from unittest.mock import MagicMock

import pytest

import config
from database import sqlite_client
from notifications import alerts
from notifications.alerts import AlertEngine, _number, evaluate_rules

N = config.ALERT_CONFIRMATION_CYCLES


def _telemetry(pt01=40.0, pt02=38.0, pt03=30.0, ts=1.0, pt04=None) -> dict:
    values = {"pt01": pt01, "pt02": pt02, "pt03": pt03, "ts": ts}
    if pt04 is not None:
        values["pt04"] = pt04
    return values


def _fresh_state() -> dict:
    return {"started_at": time.time()}


def _names(alerts: list[dict]) -> list[str]:
    return [a["rule_name"] for a in alerts]


def _pump(frequency=30.0, flow=0.0, ts=1.0) -> dict:
    return {"pt01": 40.0, "pt02": 38.0, "pt03": 30.0, "freq_pompe": frequency, "ht01": flow, "ts": ts}


class TestPumpWithoutFlow:
    """Règle : fréquence pompe non nulle SANS débit -> alerte CRITIQUE confirmée."""

    def _feed(self, state, count, **kwargs):
        alerts = []
        for index in range(count):
            alerts = evaluate_rules(_pump(ts=float(index + 1), **kwargs), None, state)
        return alerts

    def test_alert_after_n_distinct_samples(self):
        state = _fresh_state()
        assert self._feed(state, N - 1) == []
        alerts = evaluate_rules(_pump(ts=float(N)), None, state)
        assert _names(alerts) == ["pump_without_flow"]
        assert alerts[0]["level"] == "CRITIQUE"
        assert "30.0 Hz" in alerts[0]["message"] and "vérifier sur site" in alerts[0]["message"]

    def test_no_alert_when_flow_is_normal(self):
        assert self._feed(_fresh_state(), N + 2, flow=10.3) == []

    def test_no_alert_when_pump_is_stopped(self):
        assert self._feed(_fresh_state(), N + 2, frequency=0.0, flow=0.0) == []

    def test_no_alert_at_the_thresholds(self):
        """Seuils stricts : exactement PUMP_RUNNING_MIN_HZ ou FLOW_MIN_M3H n'est pas une anomalie."""
        assert self._feed(_fresh_state(), N + 2, frequency=config.PUMP_RUNNING_MIN_HZ, flow=0.0) == []
        assert self._feed(_fresh_state(), N + 2, frequency=30.0, flow=config.FLOW_MIN_M3H) == []

    @pytest.mark.parametrize("missing", ["freq_pompe", "ht01"])
    def test_missing_value_never_alerts(self, missing):
        state = _fresh_state()
        for index in range(N + 2):
            sample = _pump(ts=float(index + 1))
            sample[missing] = None
            assert evaluate_rules(sample, None, state) == []

    def test_same_sample_is_not_counted_twice(self):
        state = _fresh_state()
        for _ in range(N + 3):
            alerts = evaluate_rules(_pump(ts=1.0), None, state)
        assert alerts == []

    def test_watchdog_without_telemetry_does_not_touch_the_counter(self):
        state = _fresh_state()
        self._feed(state, N - 1)
        evaluate_rules(None, None, state)
        assert _names(evaluate_rules(_pump(ts=float(N)), None, state)) == ["pump_without_flow"]

    def test_counter_resets_when_flow_returns(self):
        state = _fresh_state()
        self._feed(state, N - 1)
        evaluate_rules(_pump(flow=10.0, ts=100.0), None, state)       # le débit est revenu
        assert evaluate_rules(_pump(ts=101.0), None, state) == []     # le décompte repart de zéro

    def test_cooldown_blocks_a_second_alert(self):
        state = _fresh_state()
        assert _names(self._feed(state, N)) == ["pump_without_flow"]
        assert evaluate_rules(_pump(ts=float(N + 1)), None, state) == []


class TestNumberHelper:
    @pytest.mark.parametrize("bad", [None, "12.5", True, float("nan"), float("inf"), [1], {"a": 1}])
    def test_rejects_non_finite_or_non_numeric(self, bad):
        assert _number({"x": bad}, "x") is None

    def test_accepts_int_and_float(self):
        assert _number({"x": 3}, "x") == 3.0
        assert _number({"x": 2.5}, "x") == 2.5

    def test_non_dict_payload_is_none(self):
        assert _number(None, "x") is None
        assert _number([1, 2], "x") is None


class TestCriticalTemperature:
    """Confirmée sur CRITICAL_CONFIRMATION_SAMPLES mesures distinctes (2 par défaut)."""

    def test_alert_above_threshold_on_the_second_sample(self):
        state = _fresh_state()
        assert evaluate_rules(_telemetry(pt02=config.CRITICAL_TEMP + 1, ts=1.0), None, state) == []
        alerts = evaluate_rules(_telemetry(pt02=config.CRITICAL_TEMP + 1, ts=2.0), None, state)
        assert _names(alerts) == ["critical_temp"]
        assert alerts[0]["level"] == "CRITIQUE" and "2 mesures consécutives" in alerts[0]["message"]

    def test_isolated_spike_never_alerts(self):
        """Cas réel (18/10/2026) : une seule mesure aberrante, puis retour à la normale."""
        state = _fresh_state()
        for ts, value in ((1.0, 35.0), (2.0, config.CRITICAL_TEMP + 2), (3.0, 35.0), (4.0, 35.0)):
            assert evaluate_rules(_telemetry(pt02=value, ts=ts), None, state) == []

    def test_same_sample_read_twice_counts_once(self):
        state = _fresh_state()
        hot = _telemetry(pt02=config.CRITICAL_TEMP + 1, ts=7.0)
        assert evaluate_rules(hot, None, state) == [] and evaluate_rules(hot, None, state) == []

    def test_immediate_when_confirmation_is_one(self, monkeypatch):
        monkeypatch.setattr(config, "CRITICAL_CONFIRMATION_SAMPLES", 1)
        alerts = evaluate_rules(_telemetry(pt02=config.CRITICAL_TEMP + 1), None, _fresh_state())
        assert _names(alerts) == ["critical_temp"] and "consécutives" not in alerts[0]["message"]

    def test_no_alert_at_or_below_threshold(self):
        assert evaluate_rules(_telemetry(pt02=config.CRITICAL_TEMP), None, _fresh_state()) == []

    def test_cooldown_blocks_second_alert(self):
        state = _fresh_state()
        evaluate_rules(_telemetry(pt02=config.CRITICAL_TEMP + 5, ts=1.0), None, state)
        assert len(evaluate_rules(_telemetry(pt02=config.CRITICAL_TEMP + 5, ts=2.0), None, state)) == 1
        assert evaluate_rules(_telemetry(pt02=config.CRITICAL_TEMP + 5, ts=3.0), None, state) == []

    @pytest.mark.parametrize("bad", ["60", float("nan"), None, True])
    def test_malformed_value_never_crashes_nor_alerts(self, bad):
        assert evaluate_rules({"pt02": bad}, None, _fresh_state()) == []


class TestTelemetryLost:
    def test_alert_when_never_received_since_startup(self):
        """Régression : le chronomètre de démarrage permet de détecter une passerelle
        en panne dès le lancement."""
        state = {"started_at": time.time() - config.TELEMETRY_TIMEOUT_SECONDS - 10}
        assert "telemetry_lost" in _names(evaluate_rules(None, None, state))

    def test_no_alert_within_timeout(self):
        state = {"started_at": time.time() - 5, "last_telemetry_ts": time.time() - 5}
        assert evaluate_rules(None, None, state) == []

    def test_alert_after_silence(self):
        state = {"started_at": 0, "last_telemetry_ts": time.time() - config.TELEMETRY_TIMEOUT_SECONDS - 1}
        assert "telemetry_lost" in _names(evaluate_rules(None, None, state))


class TestEfficiencyDrift:
    # Diélectrique 40 -> 30 °C (10 °C d'écart) alors que seuls 2 °C sont disponibles : ε = 5, impossible.
    BAD = dict(pt02=40.0, pt01=30.0, pt03=35.0, pt04=38.0)

    @pytest.fixture(autouse=True)
    def _enabled(self, monkeypatch):
        monkeypatch.setattr(config, "EFFICIENCY_ALERT_ENABLED", True)

    def test_disabled_by_default(self, monkeypatch):
        """Formule non validée sur l'installation : aucune alerte tant qu'elle n'est pas activée."""
        monkeypatch.setattr(config, "EFFICIENCY_ALERT_ENABLED", False)
        state = _fresh_state()
        for i in range(N * 2):
            assert evaluate_rules(_telemetry(**self.BAD, ts=i), None, state) == []

    def test_needs_n_distinct_samples(self):
        state = _fresh_state()
        for i in range(N - 1):
            assert evaluate_rules(_telemetry(**self.BAD, ts=i), None, state) == []
        assert _names(evaluate_rules(_telemetry(**self.BAD, ts=N), None, state)) == ["efficiency_drift"]

    def test_same_sample_is_not_counted_twice(self):
        state = _fresh_state()
        for _ in range(N * 3):
            assert evaluate_rules(_telemetry(**self.BAD, ts=1.0), None, state) == []

    def test_watchdog_does_not_reset_the_counter(self):
        """Régression : l'évaluation périodique (telemetry=None) remettait le
        compteur à zéro, empêchant toute alerte ATTENTION de se déclencher."""
        state = _fresh_state()
        for i in range(N - 1):
            evaluate_rules(_telemetry(**self.BAD, ts=i), None, state)
            evaluate_rules(None, None, state)  # tic du chien de garde
        assert _names(evaluate_rules(_telemetry(**self.BAD, ts=N), None, state)) == ["efficiency_drift"]

    def test_counter_resets_when_condition_clears(self):
        state = _fresh_state()
        for i in range(N - 1):
            evaluate_rules(_telemetry(**self.BAD, ts=i), None, state)
        evaluate_rules(_telemetry(pt02=40.0, pt01=36.0, pt03=34.0, pt04=30.0, ts=99), None, state)
        assert state["consecutive_counts"]["efficiency_drift"] == 0

    def test_formula_uses_the_hot_and_cold_side_of_each_circuit(self):
        """Mesures réelles du 15/10/2026 : PT02 34,0 > PT01 33,3 ; PT03 33,0 > PT04 32,1."""
        from notifications.alerts import _compute_efficiency

        eff = _compute_efficiency({"pt02": 34.0, "pt01": 33.3, "pt03": 33.0, "pt04": 32.1})
        assert eff == pytest.approx(0.9 / 1.9)


class TestModelDisagreement:
    def _prediction(self, ts):
        return {"disagreement_flag": True, "disagreement": 2.4, "ts": ts}

    def test_needs_n_distinct_predictions(self):
        state = _fresh_state()
        for i in range(N - 1):
            assert evaluate_rules(None, self._prediction(i), state) == []
        assert _names(evaluate_rules(None, self._prediction(N), state)) == ["model_disagreement"]

    def test_same_prediction_seen_by_many_telemetry_messages_counts_once(self):
        state = _fresh_state()
        prediction = self._prediction(1.0)
        for i in range(N * 4):
            assert evaluate_rules(_telemetry(ts=i), prediction, state) == []

    def test_string_flag_is_not_truthy(self):
        state = _fresh_state()
        for i in range(N + 1):
            assert evaluate_rules(None, {"disagreement_flag": "false", "ts": i}, state) == []


class TestOutdoorMismatch:
    def _consistency(self, gap, ts):
        return {"sensor_value": 20.0, "weather_value": 20.0 - gap, "gap": gap, "ts": ts}

    BIG = config.OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS + 5

    def _run(self, state, gap, ts):
        return evaluate_rules(None, None, state, outdoor_consistency=self._consistency(gap, ts))

    def test_below_threshold_never_alerts(self):
        state = _fresh_state()
        for i in range(N + 2):
            assert self._run(state, 1.0, i) == []

    def test_requires_n_distinct_readings(self):
        state = _fresh_state()
        for i in range(N - 1):
            assert self._run(state, self.BIG, i) == []
        alerts = self._run(state, self.BIG, N)
        assert _names(alerts) == ["outdoor_sensor_weather_mismatch"]
        assert alerts[0]["level"] == "ATTENTION"

    def test_same_reading_repeated_is_not_a_confirmation(self):
        state = _fresh_state()
        for _ in range(N * 3):
            assert self._run(state, self.BIG, 123.0) == []

    def test_counter_resets_when_gap_returns_to_normal(self):
        state = _fresh_state()
        self._run(state, self.BIG, 1)
        self._run(state, self.BIG, 2)
        assert state["consecutive_counts"]["outdoor_sensor_weather_mismatch"] == 2
        self._run(state, 0.5, 3)
        assert state["consecutive_counts"]["outdoor_sensor_weather_mismatch"] == 0

    def test_cooldown_blocks_second_alert(self):
        state = _fresh_state()
        for i in range(N):
            alerts = self._run(state, self.BIG, i)
        assert len(alerts) == 1
        assert self._run(state, self.BIG, N + 1) == []

    def test_message_states_sensor_value_is_kept(self):
        state = _fresh_state()
        for i in range(N):
            alerts = self._run(state, self.BIG, i)
        assert "reste utilisée sans changement" in alerts[0]["message"]

    def test_malformed_payload_is_ignored(self):
        state = _fresh_state()
        bad = {"sensor_value": "x", "weather_value": None, "gap": "huge", "ts": 1}
        for i in range(N + 1):
            assert evaluate_rules(None, None, state, outdoor_consistency={**bad, "ts": i}) == []

    def test_no_data_does_not_reset_or_alert(self):
        state = _fresh_state()
        self._run(state, self.BIG, 1)
        assert evaluate_rules(None, None, state, outdoor_consistency=None) == []
        assert state["consecutive_counts"]["outdoor_sensor_weather_mismatch"] == 1


class TestSendEmail:
    @pytest.fixture
    def smtp_configured(self, monkeypatch):
        for name, value in {
            "SMTP_HOST": "smtp.example.org", "SMTP_PORT": 587, "SMTP_USER": "u",
            "SMTP_PASSWORD": "p", "ALERT_EMAIL_FROM": "a@example.org", "ALERT_EMAIL_TO": "b@example.org",
        }.items():
            monkeypatch.setattr(config, name, value)
        monkeypatch.setattr(alerts.sqlite_client, "log_alert", MagicMock())

    def test_starttls_uses_a_verifying_context(self, monkeypatch, smtp_configured):
        """Sans `context=`, smtplib n'authentifie pas le serveur : l'envoi serait
        exposé à une interception (attaque de l'homme du milieu)."""
        smtp_cls = MagicMock()
        monkeypatch.setattr(alerts.smtplib, "SMTP", smtp_cls)
        assert alerts.send_email("sujet", "corps") is True
        server = smtp_cls.return_value.__enter__.return_value
        context = server.starttls.call_args.kwargs["context"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_subject_cannot_inject_headers(self, monkeypatch, smtp_configured):
        smtp_cls = MagicMock()
        monkeypatch.setattr(alerts.smtplib, "SMTP", smtp_cls)
        alerts.send_email("ok\r\nBcc: attaquant@example.org", "corps")
        server = smtp_cls.return_value.__enter__.return_value
        message = server.send_message.call_args.args[0]
        assert "\n" not in message["Subject"] and "\r" not in message["Subject"]
        assert message["Bcc"] is None

    def test_incomplete_config_is_reported_not_raised(self, monkeypatch, smtp_configured):
        monkeypatch.setattr(config, "SMTP_PASSWORD", None)
        assert alerts.send_email("s", "b") is False

    def test_smtp_failure_never_raises(self, monkeypatch, smtp_configured):
        monkeypatch.setattr(alerts.smtplib, "SMTP", MagicMock(side_effect=OSError("réseau")))
        assert alerts.send_email("s", "b") is False


class TestOilSensor:
    """PT02 absente ou figée : sans elle, aucune consigne n'est publiée."""

    def _feed(self, state, pt02, at, monkeypatch, pt01=40.0):
        monkeypatch.setattr(alerts.time, "time", lambda: at)
        state["last_telemetry_ts"] = at              # la passerelle émet (comme AlertEngine.on_telemetry)
        return evaluate_rules({"pt01": pt01, "pt02": pt02, "pt03": 30.0, "ts": at}, None, state)

    def test_missing_oil_value_alerts_after_delay(self, monkeypatch):
        state = {"started_at": 0.0}
        assert self._feed(state, None, 1000.0, monkeypatch) == []
        alerts_ = self._feed(state, None, 1000.0 + config.OIL_SENSOR_MISSING_ALERT_SECONDS, monkeypatch)
        assert _names(alerts_) == ["oil_sensor_missing"]

    def test_value_coming_back_cancels_the_timer(self, monkeypatch):
        state = {"started_at": 0.0}
        self._feed(state, None, 1000.0, monkeypatch)
        self._feed(state, 35.0, 1010.0, monkeypatch)
        assert "oil_sensor_missing" not in _names(
            self._feed(state, 35.1, 1000.0 + config.OIL_SENSOR_MISSING_ALERT_SECONDS + 5, monkeypatch))

    def test_frozen_value_alerts_when_pt01_moves(self, monkeypatch):
        """PT02 bloquée à 34,0 °C alors que PT01, sur le même circuit, bouge de plus d'un degré."""
        state = {"started_at": 0.0}
        start = 10_000.0
        for minute in range(config.SENSOR_FROZEN_ALERT_MINUTES):
            assert self._feed(state, 34.0, start + minute * 60, monkeypatch, pt01=32.0 + minute * 0.05) == []
        frozen = self._feed(state, 34.0, start + config.SENSOR_FROZEN_ALERT_MINUTES * 60, monkeypatch, pt01=35.0)
        assert _names(frozen) == ["oil_sensor_frozen"] and "34.0" in frozen[0]["message"]
        assert "PT01" in frozen[0]["message"]

    def test_stable_process_is_not_called_frozen(self, monkeypatch):
        """PT02 lue au degré près : constante pendant des heures sur un procédé stable (PT01 ±0,2 °C),
        ce n'est pas une panne — cas réel du 15-16/10/2026."""
        state = {"started_at": 0.0}
        for minute in range(config.SENSOR_FROZEN_ALERT_MINUTES * 3):
            assert self._feed(state, 34.0, 10_000.0 + minute * 60, monkeypatch, pt01=33.2 + (minute % 3) * 0.1) == []

    def test_fan_cycles_on_pt01_are_not_a_move(self, monkeypatch):
        """Cas réel (19 fausses alertes du 12 au 22/10/2026) : PT02 au degré reste à 35,0 pendant des
        heures pendant que PT01 oscille de ±0,5 °C toutes les 5 min (cycles du ventilateur), avec un
        pic isolé de +2 °C. La moyenne sur 10 min de PT01 ne bouge pas : pas d'alerte."""
        state = {"started_at": 0.0}
        start = 10_000.0
        for i in range(3 * 3600 // 15):
            pt01 = 34.3 + (0.5 if (i % 20) < 2 else -0.1) + (2.0 if i == 300 else 0.0)
            assert self._feed(state, 35.0, start + 15 * i, monkeypatch, pt01=pt01) == []

    def test_frozen_without_reference_falls_back_to_duration(self, monkeypatch):
        state = {"started_at": 0.0}
        for minute in range(config.SENSOR_FROZEN_ALERT_MINUTES):
            self._feed(state, 34.0, 10_000.0 + minute * 60, monkeypatch, pt01=None)
        frozen = self._feed(state, 34.0, 10_000.0 + config.SENSOR_FROZEN_ALERT_MINUTES * 60, monkeypatch, pt01=None)
        assert _names(frozen) == ["oil_sensor_frozen"]

    def test_moving_value_never_frozen(self, monkeypatch):
        state = {"started_at": 0.0}
        for minute in range(config.SENSOR_FROZEN_ALERT_MINUTES * 2):
            assert self._feed(state, 34.0 + (minute % 2) * 0.1, 10_000.0 + minute * 60, monkeypatch) == []


class TestDeliverAlert:
    def test_logged_once_under_its_rule_name_without_smtp(self, monkeypatch, tmp_path):
        """Régression : l'alerte était journalisée sous « smtp_config_missing » (vrai nom perdu),
        et les alertes de stockage l'étaient deux fois."""
        db = str(tmp_path / "t.db")
        sqlite_client.init_db(db)
        monkeypatch.setattr(config, "SQLITE_DB_PATH", db)
        monkeypatch.setattr(sqlite_client, "SQLITE_DB_PATH", db)
        monkeypatch.setattr(config, "SMTP_HOST", None)
        logged = []
        monkeypatch.setattr(alerts.sqlite_client, "log_alert",
                            lambda level, rule, message, email_sent, db_path: logged.append((level, rule, email_sent)))
        assert alerts.deliver_alert("CRITIQUE", "critical_temp", "T = 56 °C") is False
        assert logged == [("CRITIQUE", "critical_temp", False)]


class TestAlertEngine:
    """Le moteur tourne dans le processus principal : aucun broker n'est nécessaire."""

    def _engine(self, results=None):
        delivered: list[tuple[str, str]] = []
        outcomes = list(results or [])

        def deliver(level, rule, message):
            delivered.append((level, rule))
            return outcomes.pop(0) if outcomes else True

        return AlertEngine(deliver=deliver, watchdog_interval_seconds=0.01, synchronous=True), delivered

    def test_critical_temperature_from_a_snapshot(self):
        engine, delivered = self._engine()
        engine.on_telemetry({"pt01": 40.0, "pt02": config.CRITICAL_TEMP + 3, "pt03": 30.0})
        assert delivered == []                                     # une mesure : pas encore confirmée
        engine.on_telemetry({"pt01": 40.0, "pt02": config.CRITICAL_TEMP + 3, "pt03": 30.0})
        assert ("CRITIQUE", "critical_temp") in delivered

    def test_watchdog_alerts_when_gateway_silent_since_startup(self):
        engine, delivered = self._engine()
        engine._state["started_at"] -= config.TELEMETRY_TIMEOUT_SECONDS + 60
        thread = threading.Thread(target=engine.run_forever, daemon=True)
        thread.start()
        deadline = time.time() + 3
        while not delivered and time.time() < deadline:
            time.sleep(0.01)
        engine.stop()
        thread.join(timeout=2)
        assert ("CRITIQUE", "telemetry_lost") in delivered

    def test_failed_email_is_retried_after_short_delay(self, monkeypatch):
        for name in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "ALERT_EMAIL_FROM", "ALERT_EMAIL_TO"):
            monkeypatch.setattr(config, name, "x")
        engine, delivered = self._engine([False, True])
        hot = {"pt01": 40.0, "pt02": config.CRITICAL_TEMP + 3, "pt03": 30.0}
        engine.on_telemetry(hot)
        engine.on_telemetry(hot)                       # immédiatement : pas de rafale
        assert len(delivered) == 1
        engine._state["last_sent_at"]["critical_temp"] -= alerts.SEND_RETRY_SECONDS + 1
        engine.on_telemetry(hot)
        assert len(delivered) == 2

    def test_prediction_and_consistency_feed_the_rules(self):
        engine, delivered = self._engine()
        for i in range(N):
            engine.on_prediction({"disagreement_flag": True, "disagreement": 2.0, "ts": float(i)})
            engine.on_telemetry({"pt01": 40.0, "pt02": 36.0, "pt03": 30.0})
        assert ("ATTENTION", "model_disagreement") in delivered


class TestAsynchronousSending:
    def test_slow_smtp_never_blocks_the_collector(self):
        """L'instantané (thread de collecte) ne doit jamais attendre le serveur SMTP."""
        sent = threading.Event()

        def slow_deliver(level, rule, message):
            time.sleep(0.5)
            sent.set()
            return True

        engine = AlertEngine(deliver=slow_deliver, watchdog_interval_seconds=60)
        started = time.monotonic()
        for _ in range(2):                                        # alerte confirmée sur 2 mesures
            engine.on_telemetry({"pt01": 40.0, "pt02": config.CRITICAL_TEMP + 3, "pt03": 30.0})
        assert time.monotonic() - started < 0.1                  # retour immédiat
        worker = threading.Thread(target=engine.run_forever, daemon=True)
        worker.start()
        assert sent.wait(3)                                       # envoyée par le thread du moteur
        engine.stop()
        worker.join(timeout=3)


def test_durable_defect_is_recalled_once_a_day_not_every_30_min():
    assert alerts.cooldown_seconds("oil_sensor_frozen") == 24 * 3600
    assert alerts.cooldown_seconds("critical_temp") == config.ALERT_COOLDOWN_MINUTES * 60


class TestDielectricCriticalTemperature:
    """PT01 (sortie CDU) au-delà de son seuil : alerte CRITIQUE, confirmée sur 2 mesures."""

    def test_alert_names_the_overheated_probe(self):
        state = _fresh_state()
        hot = {"pt01": config.DIELECTRIC_CRITICAL_TEMP + 2, "pt02": 34.0, "pt03": 80.0}
        assert evaluate_rules({**hot, "ts": 1.0}, None, state) == []
        alerts_ = evaluate_rules({**hot, "ts": 2.0}, None, state)
        assert _names(alerts_) == ["dielectric_critical_temp"]
        assert alerts_[0]["level"] == "CRITIQUE" and "PT01" in alerts_[0]["message"] and "PT03" not in alerts_[0]["message"]

    def test_no_alert_at_or_below_threshold(self):
        state = _fresh_state()
        at = config.DIELECTRIC_CRITICAL_TEMP
        assert evaluate_rules({"pt01": at, "pt02": 34.0, "pt03": at + 20, "ts": 1.0}, None, state) == []

    def test_missing_values_never_alert(self):
        state = _fresh_state()
        assert evaluate_rules({"pt01": None, "pt02": 34.0, "ts": 1.0}, None, state) == []
