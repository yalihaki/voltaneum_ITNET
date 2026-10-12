"""Tests de collect_data.py : journalisation en arrière-plan et lecture du capteur extérieur."""

import logging
import logging.handlers

import config
import collect_data


def test_log_file_uses_rotation(tmp_path):
    log_file = tmp_path / "logs" / "collect.log"
    collect_data.configure_logging(str(log_file))
    try:
        handlers = logging.getLogger().handlers
        assert len(handlers) == 1
        assert isinstance(handlers[0], logging.handlers.RotatingFileHandler)
        logging.getLogger("voltaneum.test").warning("message de test")
        handlers[0].flush()
        assert "message de test" in log_file.read_text(encoding="utf-8")
    finally:
        collect_data.configure_logging(None)


class FakeListener:
    def __init__(self, value):
        self.value = value

    def get_last_value(self):
        return self.value


def test_sensor_reader_mqtt_mode(monkeypatch):
    monkeypatch.setattr(config, "OUTDOOR_SENSOR_MODE", "mqtt")
    assert collect_data.make_sensor_reader(FakeListener(17.5))() == 17.5
    assert collect_data.make_sensor_reader(None)() is None


def test_sensor_reader_http_failure_returns_none(monkeypatch):
    monkeypatch.setattr(config, "OUTDOOR_SENSOR_MODE", "http")

    def broken():
        raise ConnectionError("capteur HTTP injoignable")

    monkeypatch.setattr(collect_data, "get_outdoor_temperature_http", broken)
    assert collect_data.make_sensor_reader(None)() is None
