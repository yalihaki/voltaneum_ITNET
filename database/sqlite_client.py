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

import json
import logging
import math
import os
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager

import pandas as pd

import config
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
    temp_outdoor REAL,
    temp_outdoor_source TEXT,
    humidity_outdoor REAL,
    thermal_power_kw REAL
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


# Journal de la boucle de contrôle (service/app.py) : à chaque cycle, ce que le PID a calculé, terme
# par terme, et les conditions de refroidissement publiées. Sert à l'interface de supervision :
# la consigne n'existait jusque-là que dans un message MQTT éphémère.
_CREATE_CONTROL_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS control_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    timestamp DATETIME DEFAULT CURRENT_TIMESTAMP,
    t_oil REAL,
    t_target REAL,
    t_predicted REAL,
    t_outdoor REAL,
    t_outdoor_source TEXT,
    setpoint REAL NOT NULL,
    mode TEXT NOT NULL,
    error REAL,
    p_term REAL,
    i_term REAL,
    d_term REAL,
    feedforward_prediction REAL,
    feedforward_outdoor REAL,
    raw_output REAL,
    outdoor_band TEXT,
    fan_on_above REAL,
    fan_off_below REAL,
    fan_min_freq_hz REAL,
    -- Régulation par étapes (control/supervisor.py) : phase, événement publié ce cycle (NULL :
    -- rien à envoyer), consigne conseillée au ventilateur, conseil air seul / ventilateur et raison.
    phase TEXT,
    event TEXT,
    command_percent REAL,
    cooling_mode TEXT,
    cooling_mode_reason TEXT,
    -- Messages JSON EXACTS transmis au client MQTT (NULL : non transmis), et résultat de l'envoi
    -- de la consigne : 1 transmis, 0 échec (broker injoignable...), NULL publication désactivée.
    mqtt_setpoint TEXT,
    mqtt_conditions TEXT,
    mqtt_sent INTEGER
);
"""

_CREATE_CONTROL_INDEX_SQL = """
CREATE INDEX IF NOT EXISTS idx_control_ts ON control_log (timestamp);
"""

# Catalogue des points de la passerelle (tous les points, utilisés ou non), relevé à chaque
# rafraîchissement du mapping, avec la dernière valeur reçue de chaque point du nœud capteur.
# Sert à l'onglet « Catalogue » : ce que l'automate envoie et ce que Voltaneum en fait.
_CREATE_CATALOGUE_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS gateway_catalogue (
    dev_node TEXT NOT NULL,
    key_node TEXT NOT NULL,
    key_name TEXT,
    writable INTEGER,
    data_type TEXT,
    unit TEXT,
    gateway_desc TEXT,
    column_name TEXT,
    last_value TEXT,
    last_seen DATETIME,
    refreshed_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (dev_node, key_node)
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
        # Migration des bases créées avant l'ajout de la colonne (aucune donnée perdue).
        columns = {row[1] for row in conn.execute("PRAGMA table_info(telemetry)")}
        for column, sql_type in (("temp_outdoor_source", "TEXT"), ("humidity_outdoor", "REAL"),
                                 ("thermal_power_kw", "REAL")):
            if column not in columns:  # migration des bases existantes, aucune donnée perdue
                conn.execute(f"ALTER TABLE telemetry ADD COLUMN {column} {sql_type}")  # nosec B608
        conn.execute(_CREATE_INDEX_SQL)
        conn.execute(_CREATE_PREDICTIONS_TABLE_SQL)
        conn.execute(_CREATE_PREDICTIONS_INDEX_SQL)
        conn.execute(_CREATE_ALERTS_TABLE_SQL)
        conn.execute(_CREATE_CONTROL_TABLE_SQL)
        control_columns = {row[1] for row in conn.execute("PRAGMA table_info(control_log)")}
        for column, sql_type in (("phase", "TEXT"), ("event", "TEXT"), ("command_percent", "REAL"),
                                 ("cooling_mode", "TEXT"), ("cooling_mode_reason", "TEXT")):
            if column not in control_columns:  # migration des bases existantes, aucune donnée perdue
                conn.execute(f"ALTER TABLE control_log ADD COLUMN {column} {sql_type}")  # nosec B608
        conn.execute(_CREATE_CONTROL_INDEX_SQL)
        conn.execute(_CREATE_CATALOGUE_TABLE_SQL)

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
    temp_outdoor_source: str | None = None,
    humidity_outdoor: float | None = None,
    thermal_power_kw: float | None = None,
) -> None:
    """Insère un instantané complet dans la table `telemetry`.

    `timestamp` est optionnel : si non fourni, SQLite utilise
    CURRENT_TIMESTAMP (heure UTC du serveur au moment de l'insertion).
    `temp_outdoor_source` indique d'où vient `temp_outdoor` ("sensor" ou "weather").
    """
    values = (pt01, pt02, pt03, pt04, ht01, freq_pompe, temp_outdoor, temp_outdoor_source,
              humidity_outdoor, thermal_power_kw)
    columns = ("pt01, pt02, pt03, pt04, ht01, freq_pompe, temp_outdoor, temp_outdoor_source, "
               "humidity_outdoor, thermal_power_kw")
    with connect(db_path) as conn:
        if timestamp is None:
            conn.execute(f"INSERT INTO telemetry ({columns}) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",  # nosec B608
                         values)
        else:
            conn.execute(f"INSERT INTO telemetry (timestamp, {columns}) "  # nosec B608
                         "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", (timestamp, *values))


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


# Mesure de la grandeur régulée la plus proche d'une échéance (nom de colonne venant de config).
_MEASURE_AT_SQL = (
    f"SELECT {config.TARGET_COLUMN} FROM telemetry "  # nosec B608
    "WHERE timestamp BETWEEN datetime(?, '-15 seconds') AND datetime(?, '+15 seconds') "
    f"AND {config.TARGET_COLUMN} IS NOT NULL "
    "ORDER BY ABS(strftime('%s', timestamp) - strftime('%s', ?)) ASC, timestamp ASC LIMIT 1"
)


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
                _MEASURE_AT_SQL,
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


def prediction_horizons(db_path: str = SQLITE_DB_PATH) -> list[int]:
    """Horizons (secondes) réellement présents dans `predictions_log` : un modèle installé par le
    ré-entraînement automatique peut prédire à un horizon absent de PREDICTION_HORIZONS_SECONDS."""
    with connect(db_path) as conn:
        return [int(row[0]) for row in conn.execute(
            "SELECT DISTINCT horizon_seconds FROM predictions_log ORDER BY horizon_seconds")]


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


_CONTROL_COLUMNS = (
    "t_oil", "t_target", "t_predicted", "t_outdoor", "t_outdoor_source", "setpoint", "mode", "error",
    "p_term", "i_term", "d_term", "feedforward_prediction", "feedforward_outdoor", "raw_output",
    "outdoor_band", "fan_on_above", "fan_off_below", "fan_min_freq_hz",
    "phase", "event", "command_percent", "cooling_mode", "cooling_mode_reason",
    "mqtt_setpoint", "mqtt_conditions", "mqtt_sent",
)
_INSERT_CONTROL_SQL = (
    f"INSERT INTO control_log ({', '.join(_CONTROL_COLUMNS)}) "  # nosec B608
    f"VALUES ({', '.join('?' for _ in _CONTROL_COLUMNS)})"
)


def log_control(record: dict, db_path: str = SQLITE_DB_PATH) -> None:
    """Journalise un cycle de contrôle dans `control_log`. Ne lève jamais d'exception.

    Les clés absentes de `record` sont enregistrées à NULL. Une base indisponible ne doit
    jamais interrompre la boucle de contrôle : l'erreur est journalisée et le cycle continue.
    """
    try:
        with connect(db_path) as conn:
            conn.execute(_INSERT_CONTROL_SQL, tuple(record.get(column) for column in _CONTROL_COLUMNS))
    except sqlite3.Error:
        logger.exception("Échec d'écriture dans control_log")


def recent_values(column: str, since_epoch: float, db_path: str = SQLITE_DB_PATH) -> list[tuple[float, float]]:
    """(instant en s, valeur) de `column` depuis `since_epoch`, pour le calcul de tendance.
    Liste vide si la base est indisponible (la régulation continue sans tendance)."""
    if column not in ("pt01", "pt02", "pt03", "pt04"):
        raise ValueError(f"colonne non autorisée : {column}")
    try:
        with connect(db_path) as conn:
            rows = conn.execute(
                f"SELECT CAST(strftime('%s', timestamp) AS REAL), {column} FROM telemetry "  # nosec B608
                f"WHERE timestamp >= ? AND {column} IS NOT NULL ORDER BY timestamp",
                (time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(since_epoch)),),
            ).fetchall()
    except sqlite3.Error:
        logger.exception("Lecture de la tendance de %s impossible", column)
        return []
    return [(float(t), float(v)) for t, v in rows]


def _first(point: dict, *keys: str) -> str | None:
    for key in keys:
        value = point.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def save_catalogue(points: list[dict], columns: dict[str, str], sensor_node: str,
                   db_path: str = SQLITE_DB_PATH) -> None:
    """Remplace le catalogue enregistré par `points` (réponse brute de la passerelle).

    `columns` : KeyNode -> colonne pour les points retenus du nœud `sensor_node`. Les dernières
    valeurs déjà connues sont conservées. Ne lève jamais d'exception (la collecte continue).
    """
    rows = []
    for point in points:
        key_node, dev_node = point.get("KeyNode"), point.get("DevNode")
        if not key_node or dev_node is None:
            continue
        writable = point.get("WrEnable")
        rows.append((
            str(dev_node), str(key_node), _first(point, "KeyName"),
            None if writable is None else int(bool(writable)),
            _first(point, "DataType", "Type", "ValueType"), _first(point, "Unit", "Units"),
            _first(point, "Desc", "Description", "Remark", "Comment"),
            columns.get(key_node) if dev_node == sensor_node else None,
        ))
    try:
        with connect(db_path) as conn:
            conn.execute(_CREATE_CATALOGUE_TABLE_SQL)
            kept = {(r[0], r[1]): (r[2], r[3]) for r in conn.execute(
                "SELECT dev_node, key_node, last_value, last_seen FROM gateway_catalogue")}
            conn.execute("DELETE FROM gateway_catalogue")
            conn.executemany(
                "INSERT OR REPLACE INTO gateway_catalogue (dev_node, key_node, key_name, writable, data_type, unit, "
                "gateway_desc, column_name, last_value, last_seen) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                [(*row, *kept.get((row[0], row[1]), (None, None))) for row in rows],
            )
    except sqlite3.Error:
        logger.exception("Échec d'enregistrement du catalogue de la passerelle")


def update_catalogue_values(dev_node: str, values: dict[str, tuple[object, float]],
                            db_path: str = SQLITE_DB_PATH) -> None:
    """Dernière valeur brute reçue de chaque point (KeyNode -> (valeur, instant epoch)).
    Ne lève jamais d'exception."""
    if not values:
        return
    try:
        with connect(db_path) as conn:
            conn.executemany(
                "UPDATE gateway_catalogue SET last_value = ?, last_seen = ? WHERE dev_node = ? AND key_node = ?",
                [(json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value,
                  time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)), dev_node, key_node)
                 for key_node, (value, ts) in values.items()],
            )
    except sqlite3.Error:
        logger.exception("Échec de mise à jour des valeurs du catalogue")
