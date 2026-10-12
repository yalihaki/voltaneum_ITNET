"""Tests de collectors/outdoor_temperature.py : capteur prioritaire, API météo en repli."""

import pytest

import config
from collectors import outdoor_temperature
from collectors.outdoor_temperature import OutdoorTemperatureSource


class Weather:
    def __init__(self, value=12.0):
        self.value, self.calls = value, 0

    def __call__(self):
        self.calls += 1
        if self.value is None:
            raise ConnectionError("API météo injoignable")
        return self.value


@pytest.fixture
def clock(monkeypatch):
    now = {"t": 1000.0}
    monkeypatch.setattr(outdoor_temperature.time, "monotonic", lambda: now["t"])
    monkeypatch.setattr(config, "OUTDOOR_WEATHER_FALLBACK_ENABLED", True)
    monkeypatch.setattr(config, "OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS", 15)
    monkeypatch.setattr(config, "OUTDOOR_SENSOR_MAX_AGE_SECONDS", 1800)
    return now


def test_sensor_value_has_priority_and_api_is_not_called(clock):
    weather = Weather()
    source = OutdoorTemperatureSource(read_sensor=lambda: 18.0, fetch_weather=weather)
    assert source.read() == (18.0, "sensor")
    assert weather.calls == 0


def test_weather_used_when_sensor_has_no_value(clock):
    source = OutdoorTemperatureSource(read_sensor=lambda: None, fetch_weather=Weather(9.8))
    assert source.read() == (9.8, "weather")


def test_failing_sensor_reader_falls_back_to_weather(clock):
    def broken():
        raise RuntimeError("capteur HTTP hors service")

    source = OutdoorTemperatureSource(read_sensor=broken, fetch_weather=Weather(9.8))
    assert source.read() == (9.8, "weather")


def test_api_refreshed_every_15_seconds_not_more(clock):
    weather = Weather(10.0)
    source = OutdoorTemperatureSource(read_sensor=lambda: None, fetch_weather=weather)
    source.read()
    clock["t"] += 5
    source.read()
    assert weather.calls == 1                      # valeur en cache entre deux instantanés
    weather.value = 10.5
    clock["t"] += 10
    assert source.read() == (10.5, "weather")      # 15 s écoulées : nouvelle valeur
    assert weather.calls == 2


def test_recent_weather_value_kept_when_api_fails_then_expires(clock):
    weather = Weather(10.0)
    source = OutdoorTemperatureSource(read_sensor=lambda: None, fetch_weather=weather)
    source.read()
    weather.value = None
    clock["t"] += 60
    assert source.read() == (10.0, "weather")
    clock["t"] += 1800
    assert source.read() == (None, None)           # jamais de valeur inventée


def test_fallback_disabled(clock, monkeypatch):
    monkeypatch.setattr(config, "OUTDOOR_WEATHER_FALLBACK_ENABLED", False)
    weather = Weather()
    source = OutdoorTemperatureSource(read_sensor=lambda: None, fetch_weather=weather)
    assert source.read() == (None, None) and weather.calls == 0


def test_back_to_sensor_as_soon_as_it_returns(clock):
    sensor = {"value": None}
    source = OutdoorTemperatureSource(read_sensor=lambda: sensor["value"], fetch_weather=Weather(9.8))
    assert source.read() == (9.8, "weather")
    sensor["value"] = 17.2
    assert source.read() == (17.2, "sensor")
