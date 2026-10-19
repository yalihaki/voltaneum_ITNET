"""Tests de ml/monitor.py : rapprochement prédiction/mesure et publication de l'état des modèles."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock

import config
from database import sqlite_client
from ml.monitor import run_monitor_cycle

FMT = "%Y-%m-%d %H:%M:%S"


def _seed(db, minutes_ago=10, predicted=27.0, actual=26.0):
    target = datetime.now(timezone.utc) - timedelta(minutes=minutes_ago)
    sqlite_client.init_db(db)
    sqlite_client.insert_reading(actual, 24.0, 23.5, 23.5, 39.0, 5.0, 15.0, timestamp=target.strftime(FMT), db_path=db)
    sqlite_client.log_prediction((target - timedelta(seconds=600)).strftime(FMT), target.strftime(FMT),
                                 600, "random_forest", predicted, db_path=db)


def test_due_prediction_is_matched_with_the_measurement_in_utc(tmp_path):
    """L'heure locale décalerait les échéances (SQLite horodate en UTC) : une prédiction
    échue depuis 10 min doit être complétée, quel que soit le fuseau du serveur."""
    db = str(tmp_path / "t.db")
    _seed(db)
    assert run_monitor_cycle(db_path=db) == 1
    assert sqlite_client.get_rolling_rmse(600, "random_forest", db_path=db) == 1.0


def test_future_prediction_is_not_completed(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db, minutes_ago=-10)
    assert run_monitor_cycle(db_path=db) == 0


def test_model_health_is_published_with_the_real_sample_count(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)
    client = MagicMock()
    run_monitor_cycle(client, db_path=db)
    (topic, raw), _ = client.publish.call_args
    payload = json.loads(raw)
    assert topic == config.MQTT_TOPIC_MODEL_HEALTH
    assert payload["n_samples"] == 1 and payload["rmse_rolling"] == 1.0 and payload["model_name"] == "random_forest"


def test_publish_failure_does_not_stop_the_cycle(tmp_path):
    db = str(tmp_path / "t.db")
    _seed(db)
    client = MagicMock()
    client.publish.side_effect = RuntimeError("broker perdu")
    assert run_monitor_cycle(client, db_path=db) == 1
