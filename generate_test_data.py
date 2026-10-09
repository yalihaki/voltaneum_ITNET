"""
    Script de génération de données de test pour l'entraînement du modèle.

    Ce script crée une base de données SQLite avec des lectures simulées 
    pour tester le pipeline d'entraînement du modèle. Il génère des données de capteurs fictives
    et les insère dans la base de données, permettant ainsi de tester le processus
    d'entraînement du modèle sans dépendre de données réelles.

    Les paramètres tels que le nombre de lignes, l'intervalle entre les échantillons
    et la graine aléatoire peuvent être configurés via les arguments de ligne de commande
"""

from __future__ import annotations

import argparse
from pathlib import Path

import numpy as np
import pandas as pd

from database import sqlite_client


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", default="voltaneum_test.db", help="SQLite output path")
    parser.add_argument("--rows", type=int, default=4000)
    parser.add_argument("--interval", type=int, default=30, help="Seconds between samples")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--force", action="store_true", help="Replace the output database")
    args = parser.parse_args()

    path = Path(args.db)
    if path.exists() and not args.force:
        raise SystemExit(f"Refus de remplacer {path}; utilisez --force explicitement.")
    if args.rows < 100 or args.interval <= 0:
        raise SystemExit("--rows doit être >= 100 et --interval doit être > 0.")
    if args.force:
        for suffix in ("", "-wal", "-shm"):
            candidate = Path(f"{path}{suffix}")
            if candidate.exists():
                candidate.unlink()

    rng = np.random.default_rng(args.seed)
    timestamps = pd.date_range(
        "2026-01-01", periods=args.rows, freq=f"{args.interval}s"
    )
    phase = np.linspace(0, 12 * np.pi, args.rows)

    outdoor = 18 + 8 * np.sin(phase * 0.7) + rng.normal(0, 0.8, args.rows)

    pump_cycle = 7.0 + 5.0 * np.sin(phase * 0.9 + 1.2)
    pump_noise = rng.normal(0.0, 1.2, args.rows)
    pump = np.clip(pump_cycle + pump_noise, 0.0, 15.0)

    # Simule un régime plus proche du terrain : pompe faible, PT02 varie peu
    # mais reste suffisamment explicite pour valider le pipeline et le PID.
    pt02 = 39.5 + 0.12 * outdoor - 0.22 * pump + rng.normal(0, 0.18, args.rows)

    sqlite_client.init_db(str(path))
    for index, timestamp in enumerate(timestamps):
        value = float(pt02[index])
        sqlite_client.insert_reading(
            pt01=value - 0.3,
            pt02=value,
            pt03=value - 1.5,
            pt04=value - 1.7,
            ht01=39.0 + float(rng.normal(0, 0.2)),
            freq_pompe=float(pump[index]),
            temp_outdoor=float(outdoor[index]),
            timestamp=timestamp.strftime("%Y-%m-%d %H:%M:%S"),
            db_path=str(path),
        )

    print(f"Created {args.rows} rows in {path}")
    print(f"Train with: python train_from_local_db.py {path} 600")


if __name__ == "__main__":
    main()
