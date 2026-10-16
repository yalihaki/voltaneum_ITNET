"""Tests de la puissance dissipée estimée, de l'humidité météo et de la migration de la base."""

import sqlite3
from unittest.mock import MagicMock, patch

import pytest

import config
from collectors import weather_collector
from control.thermal import estimate_power_kw
from database import sqlite_client


def test_power_from_flow_and_temperature_difference(monkeypatch):
    monkeypatch.setattr(config, "THERMAL_FLUID_DENSITY_KG_M3", 850.0)
    monkeypatch.setattr(config, "THERMAL_FLUID_HEAT_CAPACITY_J_KG_K", 1900.0)
    # 10,4 m³/h et 1 °C d'écart PT03 - PT04 : 10,4/3600 × 850 × 1900 × 1 ≈ 4,67 kW
    power = estimate_power_kw({"ht01": 10.4, "pt03": 33.5, "pt04": 32.5})
    assert power == pytest.approx(4.665, abs=0.01)


def test_power_absent_when_a_measure_is_missing():
    assert estimate_power_kw({"ht01": None, "pt03": 33.5, "pt04": 32.5}) is None
    assert estimate_power_kw({"ht01": 10.0, "pt03": float("nan"), "pt04": 32.5}) is None


def test_configured_probes_are_used(monkeypatch):
    monkeypatch.setattr(config, "THERMAL_POWER_HOT", "pt02")
    monkeypatch.setattr(config, "THERMAL_POWER_COLD", "pt01")
    assert estimate_power_kw({"ht01": 10.0, "pt02": 35.0, "pt01": 34.0, "pt03": 99.0, "pt04": 0.0}) > 0


@patch("collectors.weather_collector.requests.get")
def test_weather_returns_humidity_when_available(mock_get):
    response = MagicMock()
    response.json.return_value = {"current_weather": {"temperature": 15.2},
                                  "current": {"relative_humidity_2m": 64}}
    mock_get.return_value = response
    assert weather_collector.get_outdoor_conditions() == (15.2, 64.0)


@patch("collectors.weather_collector.requests.get")
def test_missing_or_absurd_humidity_is_none_never_invented(mock_get):
    response = MagicMock()
    mock_get.return_value = response
    for current in (None, {"relative_humidity_2m": "humide"}, {"relative_humidity_2m": 140}):
        response.json.return_value = {"current_weather": {"temperature": 15.2}, "current": current}
        assert weather_collector.get_outdoor_conditions() == (15.2, None)


def test_existing_database_gains_the_new_columns(tmp_path):
    path = str(tmp_path / "ancienne.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, timestamp DATETIME, pt01 REAL,"
                     " pt02 REAL, pt03 REAL, pt04 REAL, ht01 REAL, freq_pompe REAL, temp_outdoor REAL)")
        conn.execute("INSERT INTO telemetry (pt02) VALUES (34.0)")
    sqlite_client.init_db(path)
    with sqlite3.connect(path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(telemetry)")}
        assert {"humidity_outdoor", "thermal_power_kw", "temp_outdoor_source"} <= columns
        assert conn.execute("SELECT pt02 FROM telemetry").fetchone() == (34.0,)   # rien de perdu
