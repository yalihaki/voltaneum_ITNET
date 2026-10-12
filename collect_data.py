"""
    Collecte les données de la passerelle vers SQLite, sans démarrer le PID 
    et sans publier de consigne. Peut être utilisé pour un diagnostic ou 
    pour alimenter un modèle prédictif externe.


    Usage : python collect_data.py [--db <chemin>] [--hours <durée>] [--interval <secondes>]
    
"""

from __future__ import annotations

import argparse
import logging
import logging.handlers
import signal
import threading
import time
from pathlib import Path

import config
from collectors.gateway_collector import GatewayCollector
from collectors.outdoor_sensor_collector import OutdoorSensorListener, get_outdoor_temperature_http
from collectors.outdoor_temperature import OutdoorTemperatureSource
from database import sqlite_client

logger = logging.getLogger("voltaneum.collect_data")


def telemetry_count(db_path: str) -> int:
    with sqlite_client.connect(db_path) as connection:
        row = connection.execute("SELECT COUNT(*) FROM telemetry").fetchone()
    return int(row[0]) if row else 0


def configure_logging(log_file: str | None) -> None:
    """Journalisation sur la console, ou dans un fichier à rotation (10 Mo x 5) si `log_file`
    est donné — utilisé quand la collecte tourne en arrière-plan (voir deploy/)."""
    handler: logging.Handler
    if log_file:
        Path(log_file).parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_file, maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8"
        )
    else:
        handler = logging.StreamHandler()
    logging.basicConfig(
        level=getattr(logging, config.LOG_LEVEL.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=[handler],
        force=True,
    )


def make_sensor_reader(listener: OutdoorSensorListener | None):
    """Lecture du capteur extérieur physique seul (None si absent, muet ou en panne)."""

    def read() -> float | None:
        if config.OUTDOOR_SENSOR_MODE == "none":
            return None  # aucun capteur installé
        if config.OUTDOOR_SENSOR_MODE == "mqtt":
            return None if listener is None else listener.get_last_value()
        try:
            return get_outdoor_temperature_http()
        except Exception as exc:
            logger.warning("Capteur extérieur (mode http) indisponible : %s", exc)
            return None

    return read


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
    parser.add_argument(
        "--log-file",
        default=None,
        help="Fichier de log à rotation (défaut : console)",
    )
    args = parser.parse_args()
    configure_logging(args.log_file)

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

    # Température extérieure de chaque instantané : capteur physique en priorité, API météo
    # en repli (voir collectors/outdoor_temperature.py) — la colonne n'est plus laissée vide.
    listener: OutdoorSensorListener | None = None
    if config.OUTDOOR_SENSOR_MODE == "mqtt":
        listener = OutdoorSensorListener()
        threading.Thread(target=listener.run_forever, name="outdoor-sensor-listener", daemon=True).start()
    outdoor_temperature = OutdoorTemperatureSource(read_sensor=make_sensor_reader(listener))

    collector = GatewayCollector(
        mqtt_client=None,
        snapshot_interval_seconds=args.interval,
        db_path=db_path,
        outdoor_temperature=outdoor_temperature.read,
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
        if listener is not None:
            listener.stop()
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
    main()
