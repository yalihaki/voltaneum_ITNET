"""
database/sqlite_client.py — Couche d'accès à la base SQLite de Voltaneum.

En clair : SQLite est une base de données qui tient dans un simple fichier
(pas besoin d'installer un serveur séparé comme on le ferait avec Postgres).
On choisit ici un schéma "large" : chaque ligne de la table représente un
instantané complet de toutes les sondes à un instant T (PT01, PT02, PT03,
PT04, HT01, fréquence pompe, température extérieure). C'est un choix
délibéré (voir cahier des charges §8) : c'est moins "propre" qu'un schéma
normalisé (une ligne par mesure), mais infiniment plus lisible pour un
débutant qui ouvrirait la base avec un simple outil graphique (DB Browser
for SQLite, par exemple) : chaque ligne raconte une histoire complète.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

import pandas as pd

from config import SQLITE_DB_PATH

logger = logging.getLogger(__name__)

# Attente maximale d'un verrou SQLite concurrent (secondes).
_BUSY_TIMEOUT_SECONDS = 30

# Une prédiction dont l'échéance remonte à plus longtemps n'est plus réexaminée
# (aucune mesure ne viendra la compléter : trou de collecte) — évite de rescanner
# indéfiniment une liste qui ne ferait que grossir.
_PENDING_PREDICTION_LOOKBACK = "-1 day"

_CREATE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS telemetry (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    pt01 REAL,
    pt02 REAL,
    pt03 REAL,
    pt04 REAL,
    ht01 REAL,
    freq_pompe REAL,
    temp_outdoor REAL
);
"""

_CREATE_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_telemetry_ts ON telemetry (timestamp);
"""

_CREATE_PREDICTIONS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS predictions_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    made_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    target_ts DATETIME NOT NULL,
    horizon_seconds INTEGER NOT NULL,
    model_name TEXT NOT NULL,
    predicted_value REAL NOT NULL,
    actual_value REAL,
    error REAL
);
"""

_CREATE_PREDICTIONS_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_predictions_target ON predictions_log (target_ts, actual_value);
"""

_CREATE_ALERTS_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS alerts_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    level TEXT NOT NULL,
    rule_name TEXT NOT NULL,
    message TEXT,
    email_sent INTEGER NOT NULL DEFAULT 0
);
"""


@contextmanager
def connect(db_path: str = SQLITE_DB_PATH) -> Iterator[sqlite3.Connection]:
    """Ouvre une connexion SQLite, la valide (commit) ou l'annule (rollback), puis la FERME.

    En clair : `with sqlite3.connect(...)` de la bibliothèque standard valide la
    transaction mais ne ferme PAS la connexion, ce qui laisse des descripteurs
    de fichiers ouverts dans un service qui tourne pendant des mois. Ce
    gestionnaire garantit la fermeture dans tous les cas (succès ou erreur).
    Le `timeout` laisse SQLite attendre un verrou concurrent (mode WAL, plusieurs
    processus) au lieu d'échouer immédiatement avec "database is locked".
    """
    conn = sqlite3.connect(db_path, timeout=_BUSY_TIMEOUT_SECONDS)
    try:
        yield conn
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db(db_path: str = SQLITE_DB_PATH) -> None:
    """Crée les tables nécessaires et sécurise les permissions du fichier."""
    with connect(db_path) as conn:
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute(_CREATE_TABLE_SQL)
        conn.execute(_CREATE_INDEX_SQL)
        conn.execute(_CREATE_PREDICTIONS_TABLE_SQL)
        conn.execute(_CREATE_PREDICTIONS_INDEX_SQL)
        conn.execute(_CREATE_ALERTS_TABLE_SQL)

    try:
        os.chmod(db_path, 0o600)
    except OSError:
        logger.warning("Impossible de fixer les permissions 0600 sur %s", db_path)

    logger.info("Base initialisée (WAL activé) : %s", db_path)


def insert_reading(
    pt01: float | None,
    pt02: float | None,
    pt03: float | None,
    pt04: float | None,
    ht01: float | None,
    freq_pompe: float | None,
    temp_outdoor: float | None,
    timestamp: str | None = None,
    db_path: str = SQLITE_DB_PATH,
) -> None:
    """Insère un instantané complet dans la table `telemetry`.

    `timestamp` est optionnel : si non fourni, SQLite utilise
    CURRENT_TIMESTAMP (heure UTC du serveur au moment de l'insertion).
    """
    with connect(db_path) as conn:
        if timestamp is None:
            conn.execute(
                """
                INSERT INTO telemetry
                    (pt01, pt02, pt03, pt04, ht01, freq_pompe, temp_outdoor)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (pt01, pt02, pt03, pt04, ht01, freq_pompe, temp_outdoor),
            )
        else:
            conn.execute(
                """
                INSERT INTO telemetry
                    (timestamp, pt01, pt02, pt03, pt04, ht01, freq_pompe, temp_outdoor)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (timestamp, pt01, pt02, pt03, pt04, ht01, freq_pompe, temp_outdoor),
            )


def get_dataframe(start_ts: str, end_ts: str, db_path: str = SQLITE_DB_PATH) -> pd.DataFrame:
    """Retourne toutes les lignes de la période [start_ts, end_ts], triées par timestamp.

    `start_ts` et `end_ts` doivent être des chaînes comparables lexicographiquement
    à un timestamp SQLite standard, ex. "2026-01-01 00:00:00" ou "2026-01-01".
    """
    with connect(db_path) as conn:
        df = pd.read_sql_query(
            """
            SELECT id, timestamp, pt01, pt02, pt03, pt04, ht01, freq_pompe, temp_outdoor
            FROM telemetry
            WHERE timestamp >= ? AND timestamp <= ?
            ORDER BY timestamp ASC
            """,
            conn,
            params=(start_ts, end_ts),
        )
    return df


def log_prediction(
    made_at: str,
    target_ts: str,
    horizon_seconds: int,
    model_name: str,
    predicted_value: float,
    db_path: str = SQLITE_DB_PATH,
) -> int:
    """Insère une ligne dans predictions_log et retourne l'id de la ligne."""
    with connect(db_path) as conn:
        cursor = conn.execute(
            """
            INSERT INTO predictions_log
                (made_at, target_ts, horizon_seconds, model_name, predicted_value)
            VALUES (?, ?, ?, ?, ?)
            """,
            (made_at, target_ts, horizon_seconds, model_name, predicted_value),
        )
        row_id = cursor.lastrowid
        return int(row_id) if row_id is not None else 0


def complete_due_predictions(now_ts: str, db_path: str = SQLITE_DB_PATH) -> int:
    """Complète les prédictions échues à partir des mesures réelles dans telemetry."""
    with connect(db_path) as conn:
        pending = conn.execute(
            """
            SELECT id, target_ts, horizon_seconds, model_name, predicted_value
            FROM predictions_log
            WHERE target_ts <= ? AND target_ts >= datetime(?, ?) AND actual_value IS NULL
            ORDER BY target_ts ASC
            """,
            (now_ts, now_ts, _PENDING_PREDICTION_LOOKBACK),
        ).fetchall()

        completed = 0
        for pred_id, target_ts, horizon_seconds, model_name, predicted_value in pending:
            row = conn.execute(
                """
                SELECT pt02
                FROM telemetry
                WHERE timestamp BETWEEN datetime(?, '-15 seconds') AND datetime(?, '+15 seconds')
                  AND pt02 IS NOT NULL
                ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) ASC, timestamp ASC
                LIMIT 1
                """,
                (target_ts, target_ts, target_ts),
            ).fetchone()
            if row is None:
                continue

            actual_value = float(row[0])
            error = float(predicted_value - actual_value)
            conn.execute(
                "UPDATE predictions_log SET actual_value = ?, error = ? WHERE id = ?",
                (actual_value, error, pred_id),
            )
            completed += 1

    return completed


def get_rolling_rmse_stats(
    horizon_seconds: int,
    model_name: str,
    window: int = 100,
    db_path: str = SQLITE_DB_PATH,
) -> tuple[float, int] | None:
    """RMSE glissant ET nombre réel d'échantillons utilisés, ou None si aucun.

    Le nombre d'échantillons doit toujours accompagner le RMSE : un RMSE calculé
    sur 2 points n'a pas la même valeur que sur 100.
    """
    with connect(db_path) as conn:
        rows = conn.execute(
            """
            SELECT error
            FROM predictions_log
            WHERE horizon_seconds = ? AND model_name = ?
              AND actual_value IS NOT NULL AND error IS NOT NULL
            ORDER BY id DESC
            LIMIT ?
            """,
            (horizon_seconds, model_name, window),
        ).fetchall()

    if not rows:
        return None
    errors = [float(row[0]) for row in rows]
    return math.sqrt(sum(err * err for err in errors) / len(errors)), len(errors)


def get_rolling_rmse(
    horizon_seconds: int,
    model_name: str,
    window: int = 100,
    db_path: str = SQLITE_DB_PATH,
) -> float | None:
    """RMSE glissant sur les dernières prédictions complétées (None si aucune)."""
    stats = get_rolling_rmse_stats(horizon_seconds, model_name, window, db_path)
    return None if stats is None else stats[0]


def log_alert(
    level: str,
    rule_name: str,
    message: str,
    email_sent: bool,
    db_path: str = SQLITE_DB_PATH,
) -> None:
    """Journalise une alerte dans `alerts_log`. Ne lève jamais d'exception.

    Une base indisponible ne doit jamais faire planter le pipeline d'alertes :
    l'erreur est journalisée et le traitement continue.
    """
    try:
        with connect(db_path) as conn:
            conn.execute(
                "INSERT INTO alerts_log (level, rule_name, message, email_sent) VALUES (?, ?, ?, ?)",
                (level, rule_name, message, int(email_sent)),
            )
    except sqlite3.Error:
        logger.exception("Échec d'écriture dans alerts_log (règle=%s)", rule_name)
