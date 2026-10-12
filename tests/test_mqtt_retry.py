"""Journalisation de messaging.mqtt.connect_with_retry quand le broker est absent."""

import logging
import threading

from messaging import mqtt as mqtt_module


class FlakyClient:
    """Échoue `failures` fois, puis se connecte."""

    def __init__(self, failures):
        self.failures = failures
        self.calls = 0

    def connect(self, *args, **kwargs):
        self.calls += 1
        if self.calls <= self.failures:
            raise ConnectionRefusedError(111, "Connection refused")


class InstantEvent(threading.Event):
    """`wait()` ne patiente pas : les essais s'enchaînent instantanément."""

    def wait(self, timeout=None):
        return self.is_set()


def _records(caplog, level):
    return [r for r in caplog.records if r.name == mqtt_module.logger.name and r.levelno == level]


def test_absent_broker_logs_one_error_not_one_per_retry(caplog, monkeypatch):
    clock = {"t": 0.0}

    def fake_monotonic():
        clock["t"] += 30.0  # chaque essai « dure » 30 s
        return clock["t"]

    monkeypatch.setattr(mqtt_module.time, "monotonic", fake_monotonic)
    caplog.set_level(logging.DEBUG)
    client = FlakyClient(failures=25)              # ~12 min d'essais simulés
    assert mqtt_module.connect_with_retry(client, InstantEvent()) is True
    assert len(_records(caplog, logging.ERROR)) == 1
    assert len(_records(caplog, logging.WARNING)) == 1    # un seul rappel après 10 min
    assert len(_records(caplog, logging.INFO)) == 1       # retour du broker signalé


def test_immediate_success_logs_nothing(caplog):
    caplog.set_level(logging.DEBUG)
    assert mqtt_module.connect_with_retry(FlakyClient(failures=0), InstantEvent()) is True
    assert not [r for r in caplog.records if r.name == mqtt_module.logger.name]
