"""Tests de la supervision en terminal (service/watch.py)."""

import time

from database import sqlite_client
from service.watch import render, sparkline


def _db(tmp_path, rows=True):
    path = str(tmp_path / "w.db")
    sqlite_client.init_db(path)
    if rows:
        now = time.time()
        for i in range(40):
            ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 600 + i * 15))
            sqlite_client.insert_reading(40.0, 34.0 + i * 0.05, 39.0, 30.0, 2.5, 30.0, 15.0,
                                         timestamp=ts, db_path=path, temp_outdoor_source="weather")
        sqlite_client.log_control({"t_oil": 36.0, "t_target": 35.0, "setpoint": 42.0, "mode": "pid", "error": 1.0,
                                   "p_term": 10.0, "i_term": 32.0, "d_term": 0.0, "feedforward_prediction": 0.0,
                                   "feedforward_outdoor": 0.0, "raw_output": 42.0, "mqtt_sent": 0}, db_path=path)
        sqlite_client.log_alert("CRITIQUE", "critical_temp", "Huile à 56 °C", email_sent=False, db_path=path)
    return path


def test_screen_shows_measures_pid_and_alerts(tmp_path):
    screen = render(_db(tmp_path), minutes=60, width=110)
    assert "Régulée (PT02)" in screen and "cible 35,0 °C" in screen
    assert "Refroidissement" in screen and "PT01 40,0 °C" in screen
    assert "Diélectrique" in screen and "PT03 39,0 °C" in screen and "PT04 30,0 °C" in screen
    assert "dissipée" in screen and "ESTIMATION" in screen
    assert "consigne 42,0 %" in screen and "NON transmise" in screen
    assert "critique" in screen and "Huile à 56 °C" in screen
    assert "\033[" not in screen                              # pas de couleurs hors terminal


def test_empty_database_is_explained(tmp_path):
    assert "Aucune mesure" in render(_db(tmp_path, rows=False))


def test_sparkline_scales_and_keeps_gaps():
    line, lo, hi = sparkline([1.0, 2.0, None, 4.0], 4)
    assert (lo, hi) == (1.0, 4.0)
    assert line[0] == "▁" and line[-1] == "█" and line[2] == " "
    assert len(sparkline([5.0, 6.0], 10)[0]) == 10            # peu de points : étirés, pas de trous


def test_stopped_voltaneum_with_fresh_measures_is_flagged(tmp_path, monkeypatch):
    """Cas réel : une ancienne collecte écrit encore en base alors que Voltaneum n'est pas lancé."""
    from service import watch
    monkeypatch.setattr(watch, "voltaneum_running", lambda: False)
    monkeypatch.setattr(watch.config, "MODEL_DIR", str(tmp_path / "models"))
    screen = render(_db(tmp_path), width=120)
    assert "Voltaneum ARRÊTÉ" in screen and "ANCIENNE collecte" in screen
    assert "aucun modèle installé" in screen


def test_constant_measure_is_labelled(tmp_path, monkeypatch):
    from service import watch
    monkeypatch.setattr(watch, "voltaneum_running", lambda: True)
    path = str(tmp_path / "c.db")
    sqlite_client.init_db(path)
    now = time.time()
    for i in range(20):
        ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - 300 + i * 15))
        sqlite_client.insert_reading(33.2, 34.0, 33.0, 32.6, 10.4, 30.0, 15.0, timestamp=ts, db_path=path)
    screen = render(path, width=120)
    assert "34,0 °C CONSTANTE" in screen and "30 Hz CONSTANTE" in screen
