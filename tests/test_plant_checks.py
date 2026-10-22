"""Tests des contrôles de l'installation (notifications/plant_checks.py) et de la tendance de PT02
estimée utilisée par la régulation et l'affichage (ml/features.watched_state)."""

import time

import pytest

import config
from database import sqlite_client
from ml.features import watched_state
from notifications.plant_checks import catalogue_findings, fan_findings, fan_starts, run_plant_checks


class TestCatalogue:
    """Valeurs réelles du catalogue du 22/10/2026."""

    REAL = {"Emergency stop bypass": "true", "Phase sequence bypass": "true", "ON/OFF": "false",
            "Temperature setpoint": "36", "DIAG Oil temperature setting VD500": "36"}

    def test_real_values_raise_both_alerts(self):
        rules = {rule: (level, message) for level, rule, message in catalogue_findings(self.REAL)}
        assert rules["safety_bypass_active"][0] == "CRITIQUE"
        assert "Emergency stop bypass, Phase sequence bypass" in rules["safety_bypass_active"][1]
        assert rules["automate_setpoint_mismatch"][0] == "ATTENTION"
        assert "Temperature setpoint = 36 °C" in rules["automate_setpoint_mismatch"][1]

    def test_nothing_to_say_when_aligned_and_no_bypass(self, monkeypatch):
        monkeypatch.setattr(config, "TARGET_TEMP", 36.0)
        assert catalogue_findings({"Emergency stop bypass": "false", "Temperature setpoint": "36.3"}) == []

    def test_unreadable_values_are_ignored(self):
        assert catalogue_findings({"Temperature setpoint": "n/a", "Phase sequence bypass": None}) == []


def _fan_cycles(every_seconds, hours=1.0, start=1_000_000.0):
    """PT04 toutes les 15 s : stable à 33,5 °C, chute de 3,3 °C en 30 s à chaque démarrage, remontée."""
    points, t = [], start
    next_start = start + 60
    profile = []
    while t < start + hours * 3600:
        if t >= next_start:
            profile = [32.0, 30.2, 30.6, 31.2, 31.8, 32.4, 33.0]
            next_start += every_seconds
        points.append((t, profile.pop(0) if profile else 33.5))
        t += 15
    return points


def test_fan_starts_are_counted_like_on_the_installation():
    points = _fan_cycles(300)                                   # un démarrage toutes les 5 min
    assert len(fan_starts(points)) == 12


def test_short_cycling_alert_above_the_limit(monkeypatch):
    monkeypatch.setattr(config, "FAN_STARTS_PER_HOUR_MAX", 6)
    points = _fan_cycles(300)
    ((level, rule, message),) = fan_findings(points, now=points[-1][0])
    assert (level, rule) == ("ATTENTION", "fan_short_cycling") and "12 démarrages" in message
    assert "un toutes les 5.0 min" in message


def test_no_alert_for_a_normal_fan_or_when_disabled(monkeypatch):
    monkeypatch.setattr(config, "FAN_STARTS_PER_HOUR_MAX", 6)
    assert fan_findings(_fan_cycles(1200), now=1_000_000.0 + 3600) == []    # 3 par heure
    monkeypatch.setattr(config, "FAN_STARTS_PER_HOUR_MAX", 0)
    assert fan_findings(_fan_cycles(300), now=1_000_000.0 + 3600) == []


def test_slow_drift_is_not_a_fan_start():
    points = [(1_000_000.0 + 15 * i, 34.0 - 0.05 * i) for i in range(200)]    # −0,1 °C / 30 s
    assert fan_starts(points) == []


def test_run_plant_checks_reads_the_catalogue_and_ignores_old_values(tmp_path):
    db = str(tmp_path / "p.db")
    sqlite_client.init_db(db)
    node = config.SENSOR_DRIVER_NODE
    sqlite_client.save_catalogue([{"KeyNode": "K17", "KeyName": "Emergency stop bypass", "DevNode": node, "WrEnable": 0},
                                  {"KeyNode": "K12", "KeyName": "Temperature setpoint", "DevNode": node, "WrEnable": 0}],
                                 {}, node, db_path=db)
    now = time.time()
    sqlite_client.update_catalogue_values(node, {"K17": ("true", now), "K12": (36, now - 7200)}, db_path=db)
    rules = [rule for _, rule, _ in run_plant_checks(db, now=now)]
    assert rules == ["safety_bypass_active"]                    # la consigne, vieille de 2 h, n'est pas jugée


def test_run_plant_checks_without_catalogue_never_fails(tmp_path):
    db = str(tmp_path / "vide.db")
    sqlite_client.init_db(db)
    assert run_plant_checks(db) == []


class TestWatchedState:
    """État de PT02 pour la régulation et l'affichage : estimation au dixième, tendance de la mesure."""

    def test_estimate_and_measured_trend(self):
        from control.trend import slope_per_10min
        t0 = 1_000_000.0
        pt01 = [(t0 + 15 * i, 33.8 + 0.01 * i) for i in range(80)]
        pt02 = [(t, float(round(v + 0.6))) for t, v in pt01]           # lue au degré près
        now = pt01[-1][0]
        state = watched_state(pt02, pt01, now, pt01[-1][1], 600)
        heating = sum(b - a for (_, a), (_, b) in zip(pt01[-40:], pt02[-40:])) / 40
        assert state["estimate"] == pytest.approx(pt01[-1][1] + heating)
        assert state["slope"] == pytest.approx(slope_per_10min([p for p in pt02 if p[0] > now - 600], 600))

    def test_without_history_there_is_no_estimate(self):
        state = watched_state([(1.0, 35.0)], [], 1.0, 34.4, 600)
        assert state["estimate"] is None
