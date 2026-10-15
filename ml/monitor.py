"""Surveillance des modèles : compare chaque prédiction à la mesure réelle à son échéance.

Pour chaque prédiction journalisée (voir service/app.py), `run_monitor_loop` :
  1. la rapproche de la mesure réelle de `telemetry` dès que son échéance est passée ;
  2. calcule le RMSE glissant par modèle et par horizon ;
  3. le publie sur MQTT (`voltaneum/model_health`).

Tourne dans le processus principal (thread « model-monitor »).
"""

from __future__ import annotations

import json
import logging
import threading
import time
from datetime import datetime, timezone

import config
from database import sqlite_client

logger = logging.getLogger(__name__)

_DB_TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
_MODELS = ("random_forest", "gradient_boosting")


def run_monitor_cycle(mqtt_client=None, db_path: str | None = None) -> int:
    """Un cycle de surveillance. Retourne le nombre de prédictions complétées."""
    db_path = db_path or config.SQLITE_DB_PATH
    # SQLite horodate en UTC : comparer avec l'heure LOCALE décalerait toutes les échéances.
    now = datetime.now(timezone.utc).strftime(_DB_TIME_FORMAT)
    completed = sqlite_client.complete_due_predictions(now_ts=now, db_path=db_path)
    horizons = sorted(set(config.PREDICTION_HORIZONS_SECONDS) | set(sqlite_client.prediction_horizons(db_path)))
    for horizon in horizons:
        for model_name in _MODELS:
            stats = sqlite_client.get_rolling_rmse_stats(horizon, model_name, db_path=db_path)
            if stats is None or mqtt_client is None:
                continue
            rmse, n_samples = stats
            payload = {"horizon_seconds": horizon, "model_name": model_name,
                       "rmse_rolling": rmse, "n_samples": n_samples, "ts": time.time()}
            try:
                mqtt_client.publish(config.MQTT_TOPIC_MODEL_HEALTH, json.dumps(payload))
            except Exception:
                logger.exception("Échec de publication de l'état des modèles")
    return completed


def run_monitor_loop(interval_seconds: int = 30, mqtt_client=None,
                     stop_event: threading.Event | None = None, db_path: str | None = None) -> None:
    """Exécute `run_monitor_cycle` toutes les `interval_seconds` jusqu'à l'arrêt demandé."""
    stop_event = stop_event or threading.Event()
    while not stop_event.is_set():
        try:
            run_monitor_cycle(mqtt_client, db_path)
        except Exception:
            logger.exception("Erreur dans la boucle de surveillance des modèles")
        stop_event.wait(interval_seconds)
