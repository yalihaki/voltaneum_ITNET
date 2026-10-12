"""Tests du moteur d'alertes (notifications/email_notifier.py).

Couvre toutes les règles de `evaluate_rules`, la sécurité de `send_email`
(TLS vérifié, en-têtes sûrs) et le chien de garde de l'`EmailNotifier`.
"""

import ssl
import threading
import time
from unittest.mock import MagicMock

import pytest

import config
from notifications import email_notifier
from notifications.email_notifier import _number, evaluate_rules

N = config.ALERT_CONFIRMATION_CYCLES


def _telemetry(pt01=40.0, pt02=38.0, pt03=30.0, ts=1.0) -> dict:
    return {"pt01": pt01, "pt02": pt02, "pt03": pt03, "ts": ts}


def _fresh_state() -> dict:
    return {"started_at": time.time()}


def _names(alerts: list[dict]) -> list[str]:
    return [a["rule_name"] for a in alerts]


def _pump(frequency=30.0, flow=0.0, ts=1.0) -> dict:
    return {"pt01": 40.0, "pt02": 38.0, "pt03": 30.0, "freq_pompe": frequency, "ht01": flow, "ts": ts}


class TestPumpWithoutFlow:
    """Règle : fréquence pompe/ventilateur non nulle SANS débit -> alerte CRITIQUE confirmée."""

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
    def test_alert_above_threshold(self):
        alerts = evaluate_rules(_telemetry(pt02=config.CRITICAL_TEMP + 1), None, _fresh_state())
        assert _names(alerts) == ["critical_temp"]
        assert alerts[0]["level"] == "CRITIQUE"

    def test_no_alert_at_or_below_threshold(self):
        assert evaluate_rules(_telemetry(pt02=config.CRITICAL_TEMP), None, _fresh_state()) == []

    def test_cooldown_blocks_second_alert(self):
        state = _fresh_state()
        hot = _telemetry(pt02=config.CRITICAL_TEMP + 5)
        assert len(evaluate_rules(hot, None, state)) == 1
        assert evaluate_rules(hot, None, state) == []

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
    BAD = dict(pt01=40.0, pt02=41.0, pt03=30.0)   # efficacité négative : hors plage

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
        evaluate_rules(_telemetry(pt01=40.0, pt02=36.0, pt03=30.0, ts=99), None, state)
        assert state["consecutive_counts"]["efficiency_drift"] == 0


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
        monkeypatch.setattr(email_notifier.sqlite_client, "log_alert", MagicMock())

    def test_starttls_uses_a_verifying_context(self, monkeypatch, smtp_configured):
        """Sans `context=`, smtplib n'authentifie pas le serveur : l'envoi serait
        exposé à une interception (attaque de l'homme du milieu)."""
        smtp_cls = MagicMock()
        monkeypatch.setattr(email_notifier.smtplib, "SMTP", smtp_cls)
        assert email_notifier.send_email("sujet", "corps", "INFO") is True
        server = smtp_cls.return_value.__enter__.return_value
        context = server.starttls.call_args.kwargs["context"]
        assert isinstance(context, ssl.SSLContext)
        assert context.verify_mode == ssl.CERT_REQUIRED
        assert context.check_hostname is True

    def test_subject_cannot_inject_headers(self, monkeypatch, smtp_configured):
        smtp_cls = MagicMock()
        monkeypatch.setattr(email_notifier.smtplib, "SMTP", smtp_cls)
        email_notifier.send_email("ok\r\nBcc: attaquant@example.org", "corps", "INFO")
        server = smtp_cls.return_value.__enter__.return_value
        message = server.send_message.call_args.args[0]
        assert "\n" not in message["Subject"] and "\r" not in message["Subject"]
        assert message["Bcc"] is None

    def test_incomplete_config_is_reported_not_raised(self, monkeypatch, smtp_configured):
        monkeypatch.setattr(config, "SMTP_PASSWORD", None)
        assert email_notifier.send_email("s", "b", "INFO") is False

    def test_smtp_failure_never_raises(self, monkeypatch, smtp_configured):
        monkeypatch.setattr(email_notifier.smtplib, "SMTP", MagicMock(side_effect=OSError("réseau")))
        assert email_notifier.send_email("s", "b", "INFO") is False


class TestNotifierWatchdog:
    def test_alert_when_gateway_down_since_startup(self, monkeypatch, tmp_path):
        """Régression de bout en bout : le chien de garde doit alerter même si AUCUN
        message de télémétrie n'est jamais arrivé depuis le lancement."""
        monkeypatch.setattr(config, "SQLITE_DB_PATH", str(tmp_path / "t.db"))
        sent: list[tuple] = []
        monkeypatch.setattr(email_notifier, "send_email", lambda *a: sent.append(a) or True)
        notifier = email_notifier.EmailNotifier()
        assert "started_at" in notifier._state
        notifier._state["started_at"] -= config.TELEMETRY_TIMEOUT_SECONDS + 60

        thread = threading.Thread(target=notifier._watchdog_loop, args=(0.01,), daemon=True)
        thread.start()
        deadline = time.time() + 3
        while not sent and time.time() < deadline:
            time.sleep(0.01)
        notifier._stop_event.set()
        thread.join(timeout=2)
        assert sent and "telemetry_lost" in sent[0][0]

    def test_non_object_payload_is_ignored(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "SQLITE_DB_PATH", str(tmp_path / "t.db"))
        notifier = email_notifier.EmailNotifier()
        message = MagicMock(topic=config.MQTT_TOPIC_TELEMETRY, payload=b"[1, 2, 3]")
        notifier._on_message(None, None, message)  # ne doit pas lever
        assert "last_telemetry_ts" not in notifier._state
