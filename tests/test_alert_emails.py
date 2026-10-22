"""Tests des e-mails d'alerte explicites (notifications/alert_texts.py), du journal des changements
du catalogue et de l'alerte « changement de mode de l'automate »."""

import time
import types
from unittest.mock import MagicMock

import pytest

import config
from database import sqlite_client
from notifications import alerts
from notifications.alert_texts import RULES, compose
from notifications.plant_checks import mode_change_findings, run_plant_checks

NODE = config.SENSOR_DRIVER_NODE
SNAPSHOT = {"empty": False, "age_seconds": 7, "values": {"pt01": 33.9, "pt02": 34.0, "pt03": 33.7, "pt04": 33.4,
                                                         "ht01": 10.4, "freq_pompe": 30.0, "temp_outdoor": 15.3},
            "thresholds": {"target_temp": 35.0, "critical_temp": 55.0}, "target_estimate": 34.5,
            "target_trend_10min": -1.0, "thermal_power_kw_10min": 2.8,
            "control": {"phase": "repos", "cooling_mode_reason": "PT02 à 34,0 °C : ne se réchauffe pas"}}


class TestEmail:
    def test_subject_is_readable_and_body_explains_everything(self):
        subject, body = compose("CRITIQUE", "safety_bypass_active", "Shunt de sécurité actif : Emergency stop bypass.",
                                SNAPSHOT, reminder_minutes=24 * 60)
        assert subject == "[Voltaneum] CRITIQUE — Shunt de sécurité actif sur l'automate"
        for section in ("CE QUI SE PASSE", "POURQUOI C'EST IMPORTANT", "QUE FAIRE", "MESURES AU MOMENT DE L'ALERTE",
                        "POUR VÉRIFIER SUR LE SERVEUR", "RAPPEL"):
            assert section in body
        assert "Emergency stop bypass" in body and "arrêt d'urgence" in body
        assert "34,0 °C (cible 35,0 °C" in body and "estimée 34,50 °C" in body and "tendance -1,0 °C / 10 min" in body
        assert "./voltaneum catalogue" in body and "renvoyée au plus tôt dans 24 h" in body
        assert "safety_bypass_active" in body                       # référence technique, en bas

    def test_multi_line_message_and_no_reminder(self):
        _, body = compose("ATTENTION", "automate_mode_changed", "Mode modifié :\n- 14:02 · M4.2 : true → false",
                          SNAPSHOT, reminder_minutes=0)
        assert "  - 14:02 · M4.2 : true → false" in body and "RAPPEL" not in body

    def test_unknown_rule_and_missing_measures_still_give_a_readable_email(self):
        subject, body = compose("INFO", "nouvelle_regle", "texte", None)
        assert subject == "[Voltaneum] INFO — Nouvelle regle" and "aucune mesure en base" in body

    def test_every_alert_of_the_project_is_described(self):
        rules = {"critical_temp", "dielectric_critical_temp", "pump_without_flow", "telemetry_lost",
                 "oil_sensor_missing", "oil_sensor_frozen", "cooling_ineffective", "safety_bypass_active",
                 "automate_setpoint_mismatch", "fan_short_cycling", "automate_mode_changed", "efficiency_drift",
                 "model_disagreement", "outdoor_sensor_weather_mismatch", "storage_warning", "storage_backup_failed",
                 "storage_daily_backup_failed", "storage_auto_purge", "storage_purge_blocked_by_floor",
                 "storage_purge_ineffective"}
        assert rules <= set(RULES)

    def test_deliver_alert_sends_the_explicit_email(self, monkeypatch, tmp_path):
        monkeypatch.setattr(config, "SQLITE_DB_PATH", str(tmp_path / "e.db"))
        sqlite_client.init_db(config.SQLITE_DB_PATH)
        for name, value in {"SMTP_HOST": "smtp.example.org", "SMTP_PORT": 587, "SMTP_USER": "u", "SMTP_PASSWORD": "p",
                            "ALERT_EMAIL_FROM": "v@example.org", "ALERT_EMAIL_TO": "moi@example.org"}.items():
            monkeypatch.setattr(config, name, value)
        smtp_cls = MagicMock()
        monkeypatch.setattr(alerts.smtplib, "SMTP", smtp_cls)
        assert alerts.deliver_alert("ATTENTION", "fan_short_cycling", "12 démarrages sur la dernière heure")
        message = smtp_cls.return_value.__enter__.return_value.send_message.call_args.args[0]
        assert message["Subject"] == "[Voltaneum] ATTENTION — Ventilateur du dry cooler : démarrages trop fréquents"
        assert "QUE FAIRE" in message.get_content() and "hystérésis" in message.get_content()
        with sqlite_client.connect(config.SQLITE_DB_PATH) as conn:   # le journal garde le message court
            assert conn.execute("SELECT message FROM alerts_log").fetchone()[0] == "12 démarrages sur la dernière heure"


class TestModeChanges:
    def _db(self, tmp_path):
        db = str(tmp_path / "c.db")
        sqlite_client.init_db(db)
        sqlite_client.save_catalogue([
            {"KeyNode": "K0", "KeyName": "PT01", "DevNode": NODE, "WrEnable": 0},
            {"KeyNode": "diag_m42", "KeyName": "DIAG Auto mode M4.2", "DevNode": NODE, "WrEnable": 0},
            {"KeyNode": "K12", "KeyName": "Temperature setpoint", "DevNode": NODE, "WrEnable": 0},
        ], {"K0": "pt01"}, NODE, db_path=db)
        return db

    def test_only_real_changes_of_settings_are_journaled(self, tmp_path):
        db = self._db(tmp_path)
        t = time.time()
        sqlite_client.update_catalogue_values(NODE, {"K0": (33.6, t), "diag_m42": (True, t), "K12": (36, t)}, db_path=db)
        sqlite_client.update_catalogue_values(NODE, {"K0": (33.8, t + 15), "diag_m42": (True, t + 15)}, db_path=db)
        sqlite_client.update_catalogue_values(NODE, {"K0": (33.9, t + 30), "diag_m42": (False, t + 30)}, db_path=db)
        with sqlite_client.connect(db) as conn:
            rows = conn.execute("SELECT key_name, old_value, new_value FROM catalogue_changes").fetchall()
        assert rows == [("DIAG Auto mode M4.2", "true", "false")]    # ni PT01 (mesure), ni première valeur

    def test_switch_to_manual_is_explained(self):
        ((level, rule, message),) = mode_change_findings([("2026-10-22 12:00:00", "DIAG Auto mode M4.2", "true", "false"),
                                                          ("2026-10-22 12:00:00", "MANUAL PUMP REQUEST M4.3", "false", "true")])
        assert (level, rule) == ("ATTENTION", "automate_mode_changed")
        assert "true → false (mode MANUEL)" in message and "(pompe en marche MANUELLE demandée)" in message

    def test_a_point_that_changes_all_the_time_is_summarised(self):
        changes = [("2026-10-22 12:00:%02d" % i, "DIAG Motor register VW130", str(i), str(i + 1)) for i in range(8)]
        ((_, _, message),) = mode_change_findings(changes)
        assert "a changé 8 fois (dernière valeur 8)" in message

    def test_plant_checks_report_changes_after_the_given_instant_only(self, tmp_path):
        db = self._db(tmp_path)
        t = time.time()
        sqlite_client.update_catalogue_values(NODE, {"diag_m42": (True, t - 120)}, db_path=db)
        sqlite_client.update_catalogue_values(NODE, {"diag_m42": (False, t - 60)}, db_path=db)
        before = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t - 90))
        after = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(t - 30))
        assert "automate_mode_changed" in [r for _, r, _ in run_plant_checks(db, now=t, changes_since=before)]
        assert "automate_mode_changed" not in [r for _, r, _ in run_plant_checks(db, now=t, changes_since=after)]

    def test_cli_lists_the_changes(self, tmp_path, capsys):
        import voltaneum
        db = self._db(tmp_path)
        t = time.time()
        sqlite_client.update_catalogue_values(NODE, {"K12": (36, t)}, db_path=db)
        sqlite_client.update_catalogue_values(NODE, {"K12": (35, t + 60)}, db_path=db)
        args = types.SimpleNamespace(db=db, changes=50, details=False, used=False)
        assert voltaneum.cmd_catalogue(args) == 0
        out = capsys.readouterr().out
        assert "Temperature setpoint" in out and "36" in out and "35" in out


@pytest.mark.parametrize("rule", ["automate_mode_changed"])
def test_mode_changes_are_never_held_back_by_a_daily_delay(rule):
    assert alerts.cooldown_seconds(rule) == 0
