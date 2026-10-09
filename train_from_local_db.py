"""Entraîne les modèles à partir d'une base SQLite locale (avec tous les garde-fous de `ml/train.py`).

Usage : python train_from_local_db.py [base.db] [horizon_secondes] [dossier_modeles]
        (défauts : base de production, 600 s, dossier `models/`)

Aucune logique d'entraînement n'est dupliquée ici : tout passe par `ml.train`, qui refuse
de valider un modèle si les données sont insuffisantes, trop plates, ou si une variable
(ex. la fréquence de la pompe) n'a jamais varié.
"""

import json
import logging
import sys

import config
from ml.train import train_from_db


def main(argv: list[str]) -> int:
    db_path = argv[1] if len(argv) > 1 else config.SQLITE_DB_PATH
    horizon = int(argv[2]) if len(argv) > 2 else 600
    model_dir = argv[3] if len(argv) > 3 else config.MODEL_DIR
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    report = train_from_db("1970-01-01", "2100-01-01", horizon_seconds=horizon, model_dir=model_dir, db_path=db_path)
    print(json.dumps(report, indent=2, default=str))
    return 0 if report.get("status") == "ok" else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv))
