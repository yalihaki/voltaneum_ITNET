"""
    Entraîne tous les modèles prédictifs pour chaque horizon configuré.
    Les modèles sont entraînés dans des sous-répertoires isolés pour chaque horizon
    (ex. models/horizons/30, models/horizons/60, etc.).
    Un rapport global est généré dans models/horizons/grid_report.json.
    Peut être exécuté en tant que script autonome (train_all_horizons.py) ou intégré dans un autre programme.
    
    Usage : python train_all_horizons.py [--db <chemin>] [--horizons <liste>]
"""

from __future__ import annotations

import json
from pathlib import Path

import pandas as pd

import config
from ml.train import train_and_compare


def train_all_horizons(
    df: pd.DataFrame,
    model_dir: str = config.MODEL_DIR,
    horizons: list[int] | None = None,
) -> dict:
    """Train every configured horizon in an isolated subdirectory."""
    selected = horizons or config.PREDICTION_HORIZONS_SECONDS
    root = Path(model_dir) / "horizons"
    reports: dict[str, dict] = {}
    for horizon in selected:
        report = train_and_compare(df, horizon_seconds=horizon, model_dir=str(root / str(horizon)))
        reports[str(horizon)] = report

    grid_report = {
        "status": "ok",
        "horizons_seconds": selected,
        "reports": reports,
        "all_status_ok": all(report.get("status") == "ok" for report in reports.values()),
        "all_beat_persistence": all(
            report.get("beats_persistence") is True for report in reports.values()
        ),
    }
    root.mkdir(parents=True, exist_ok=True)
    with (root / "grid_report.json").open("w", encoding="utf-8") as output:
        json.dump(grid_report, output, indent=2)
    return grid_report


def train_all_horizons_from_db(
    db_path: str = config.SQLITE_DB_PATH,
    model_dir: str = config.MODEL_DIR,
    horizons: list[int] | None = None,
) -> dict:
    from database import sqlite_client

    data = sqlite_client.get_dataframe("1970-01-01", "2100-01-01", db_path=db_path)
    return train_all_horizons(data, model_dir=model_dir, horizons=horizons)
