"""
    Collecte les données de la passerelle vers SQLite, sans démarrer le PID 
    et sans publier de consigne. Peut être utilisé pour un diagnostic ou 
    pour alimenter un modèle prédictif externe.


    Usage : python collect_data.py [--db <chemin>] [--hours <durée>] [--interval <secondes>]
    
"""

from __future__ import annotations

import argparse
import logging
import signal
import threading
import time
from pathlib import Path

import config
from collectors.gateway_collector import GatewayCollector
from database import sqlite_client

logger = logging.getLogger("voltaneum.collect_data")


def telemetry_count(db_path: str) -> int:
    with sqlite_client.connect(db_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM telemetry").fetchone()
    return int(row[0]) if row else 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Collecte les données de la passerelle vers SQLite, sans démarrer "
            "le PID et sans publier de consigne."
        )
    )
    parser.add_argument("--db", default=config.SQLITE_DB_PATH, help="Base SQLite de sortie")
    parser.add_argument(
        "--hours",
        type=float,
        default=0.0,
        help="Durée en heures ; 0 signifie jusqu'à Ctrl+C",
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=config.SNAPSHOT_INTERVAL_SECONDS,
        help="Intervalle des snapshots en secondes",
    )
    args = parser.parse_args()

    if args.hours < 0 or args.interval <= 0:
        parser.error("--hours doit être >= 0 et --interval doit être > 0")

    db_path = str(Path(args.db).expanduser())
    sqlite_client.init_db(db_path)
    before = telemetry_count(db_path)
    stop_event = threading.Event()

    def request_stop(signum, frame) -> None:
        logger.info("Signal reçu (%s), arrêt de la collecte...", signum)
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)

    collector = GatewayCollector(
        mqtt_client=None,
        snapshot_interval_seconds=args.interval,
        db_path=db_path,
    )
    worker = threading.Thread(target=collector.run_forever, name="data-collector")
    worker.start()

    deadline = time.monotonic() + args.hours * 3600 if args.hours else None
    try:
        while not stop_event.is_set():
            if deadline is not None and time.monotonic() >= deadline:
                break
            stop_event.wait(1.0)
    finally:
        collector.stop()
        worker.join(timeout=15)
        if worker.is_alive():
            logger.error("Le thread de collecte ne s'est pas arrêté dans les 15 secondes.")

    after = telemetry_count(db_path)
    logger.info(
        "Collecte terminée : %d nouvelle(s) ligne(s), base=%s, arrêt propre=%s.",
        after - before,
        db_path,
        not worker.is_alive(),
    )


if __name__ == "__main__":
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    main()
