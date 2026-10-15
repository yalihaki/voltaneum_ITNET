"""dashboard/data.py — Lectures de la base pour l'interface de supervision (LECTURE SEULE).

En clair : l'interface ne fait que lire. La base est ouverte en mode `ro` (read-only) au
niveau de SQLite lui-même : même une erreur de programmation ne pourrait pas y écrire.
Rien ici n'envoie quoi que ce soit à la passerelle, au broker MQTT ou à l'automate.

Les horodatages de la base sont en UTC (CURRENT_TIMESTAMP de SQLite) ; ils sont rendus en
millisecondes « epoch » pour que le navigateur les affiche directement à l'heure locale.
"""

from __future__ import annotations

import json
import math
import shutil
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import config
from control.cooling_advice import compute_cooling_conditions

# Les requêtes marquées « nosec B608 » n'interpolent que des noms de colonnes CONSTANTS (définis
# dans ce module) ; toutes les valeurs passent en paramètres SQL.

# Colonnes mesurées de `telemetry`, dans l'ordre d'affichage.
MEASURES: tuple[str, ...] = ("pt01", "pt02", "pt03", "pt04", "ht01", "freq_pompe", "temp_outdoor")
MODELS: tuple[str, ...] = ("random_forest", "gradient_boosting")

# Nombre maximal de points renvoyés pour une courbe : au-delà, les mesures sont moyennées par
# tranche de temps (une courbe de 30 jours à 15 s compterait sinon 170 000 points).
DEFAULT_MAX_POINTS = 720
MAX_POINTS_LIMIT = 4000
# Plus longue période demandable en une fois (jours).
MAX_RANGE_DAYS = 400


class DatabaseUnavailable(RuntimeError):
    """Base absente ou illisible : l'interface l'affiche au lieu de planter."""


@contextmanager
def connect_readonly(db_path: str) -> Iterator[sqlite3.Connection]:
    """Connexion SQLite en LECTURE SEULE (mode=ro), toujours refermée."""
    path = Path(db_path)
    if not path.is_file():
        raise DatabaseUnavailable(f"base introuvable : {path}")
    try:
        conn = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True, timeout=10)
    except sqlite3.Error as exc:
        raise DatabaseUnavailable(f"base illisible : {exc}") from exc
    try:
        yield conn
    finally:
        conn.close()


def _finite(value: object) -> float | None:
    if value is None:
        return None
    try:
        number = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _table_exists(conn: sqlite3.Connection, name: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
    ).fetchone() is not None


def _utc_text(epoch_seconds: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch_seconds))


# ---------------------------------------------------------------------------
# Instantané courant
# ---------------------------------------------------------------------------
def latest_snapshot(db_path: str, now: float | None = None) -> dict:
    """Dernière ligne de `telemetry`, son âge, la tendance de PT02 et les conditions de
    refroidissement qu'en déduit `control/cooling_advice.py` (même calcul que la boucle de contrôle)."""
    now = time.time() if now is None else now
    with connect_readonly(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(telemetry)")}
        source_col = "temp_outdoor_source" if "temp_outdoor_source" in columns else "NULL"
        row = conn.execute(
            f"SELECT id, CAST(strftime('%s', timestamp) AS INTEGER), {', '.join(MEASURES)}, {source_col} "  # nosec B608
            "FROM telemetry ORDER BY id DESC LIMIT 1"
        ).fetchone()
        if row is None:
            return {"empty": True, "server_time": int(now * 1000), "thresholds": thresholds()}
        ts = int(row[1])
        values = {name: _finite(v) for name, v in zip(MEASURES, row[2:2 + len(MEASURES)])}
        # Tendance PT02 : comparaison avec la valeur d'il y a ~10 minutes.
        past = conn.execute(
            "SELECT pt02 FROM telemetry WHERE timestamp <= ? AND pt02 IS NOT NULL "
            "ORDER BY timestamp DESC LIMIT 1",
            (_utc_text(ts - 600),),
        ).fetchone()
        predictions = _latest_predictions(conn, now)
        control = _latest_control(conn, now)

    pt02_trend = None
    if past is not None and values["pt02"] is not None and _finite(past[0]) is not None:
        pt02_trend = round(values["pt02"] - float(past[0]), 2)

    cooling = None
    if values["pt02"] is not None:
        cooling = compute_cooling_conditions(values["pt02"], values["temp_outdoor"])

    return {
        "empty": False,
        "id": row[0],
        "ts": ts * 1000,
        "age_seconds": max(0.0, round(now - ts, 1)),
        "server_time": int(now * 1000),
        "values": values,
        "temp_outdoor_source": row[2 + len(MEASURES)],
        "pt02_trend_10min": pt02_trend,
        "predictions": predictions,
        "control": control,
        "cooling": cooling,
        "thresholds": thresholds(),
    }


def _latest_predictions(conn: sqlite3.Connection, now: float) -> dict | None:
    """Dernière prédiction de chaque modèle (moins de 5 minutes), et le meilleur modèle."""
    if not _table_exists(conn, "predictions_log"):
        return None
    rows = conn.execute(
        """
        SELECT p.model_name, p.predicted_value, CAST(strftime('%s', p.made_at) AS INTEGER),
               CAST(strftime('%s', p.target_ts) AS INTEGER), p.horizon_seconds
        FROM predictions_log p
        JOIN (SELECT model_name, MAX(id) AS id FROM predictions_log
              WHERE made_at >= ? GROUP BY model_name) last ON last.id = p.id
        """,
        (_utc_text(now - 300),),
    ).fetchall()
    if not rows:
        return None
    by_model = {
        name: {"value": _finite(value), "made_at": made * 1000, "target_ts": target * 1000,
               "horizon_seconds": horizon}
        for name, value, made, target, horizon in rows
    }
    best = training_report().get("best_model")
    values = [m["value"] for m in by_model.values() if m["value"] is not None]
    return {
        "models": by_model,
        "best_model": best if best in by_model else next(iter(by_model)),
        "disagreement": round(max(values) - min(values), 3) if len(values) > 1 else None,
        "disagreement_threshold": config.DISAGREEMENT_THRESHOLD_CELSIUS,
    }


_CONTROL_FIELDS = (
    "t_oil", "t_target", "t_predicted", "t_outdoor", "t_outdoor_source", "setpoint", "mode", "error",
    "p_term", "i_term", "d_term", "feedforward_prediction", "feedforward_outdoor", "raw_output",
    "outdoor_band", "fan_on_above", "fan_off_below", "fan_min_freq_hz",
)
_MQTT_FIELDS = ("mqtt_setpoint", "mqtt_conditions", "mqtt_sent")


def _control_columns(conn: sqlite3.Connection) -> tuple[str, ...]:
    """Colonnes lisibles de control_log (une base plus ancienne peut ne pas avoir les colonnes MQTT)."""
    present = {row[1] for row in conn.execute("PRAGMA table_info(control_log)")}
    return tuple(c for c in (*_CONTROL_FIELDS, *_MQTT_FIELDS) if c in present)


def _control_record(columns: tuple[str, ...], row: tuple) -> dict:
    record: dict = {}
    for name, value in zip(columns, row):
        record[name] = value if isinstance(value, str) or value is None else _finite(value)
    record.setdefault("mqtt_sent", None)
    return record


def _latest_control(conn: sqlite3.Connection, now: float) -> dict | None:
    """Dernier cycle de la boucle de contrôle : consigne PID et sa décomposition.

    None si la table n'existe pas encore ou est vide (la régulation n'a jamais tourné sur cette base).
    `age_seconds` permet à l'interface de signaler un calcul ancien (Voltaneum arrêté).
    """
    if not _table_exists(conn, "control_log"):
        return None
    columns = _control_columns(conn)
    row = conn.execute(
        f"SELECT CAST(strftime('%s', timestamp) AS INTEGER), {', '.join(columns)} "  # nosec B608
        "FROM control_log ORDER BY id DESC LIMIT 1"
    ).fetchone()
    if row is None:
        return None
    record = _control_record(columns, row[1:])
    record["ts"] = int(row[0]) * 1000
    record["age_seconds"] = max(0.0, round(now - int(row[0]), 1))
    record["interval_seconds"] = config.CONTROL_LOOP_INTERVAL_SECONDS
    record["gains"] = {"kp": config.PID_KP, "ki": config.PID_KI, "kd": config.PID_KD}
    record["feedforward_thresholds"] = {
        "predicted_overshoot": config.FEEDFORWARD_PREDICTED_OVERSHOOT_THRESHOLD,
        "outdoor_hot": config.FEEDFORWARD_OUTDOOR_HOT_THRESHOLD,
    }
    record["published"] = config.PUBLISH_PID_SETPOINT
    return record


def control_series(db_path: str, start: float, end: float, max_points: int = DEFAULT_MAX_POINTS) -> dict:
    """Consigne PID (%) et température prédite utilisée, moyennées par tranche ; `bypass` /
    `feedforward` = part des cycles de la tranche passés dans ce mode (0 à 1)."""
    max_points = max(10, min(int(max_points), MAX_POINTS_LIMIT))
    bucket = max(_bucket_seconds(start, end, max_points), int(config.CONTROL_LOOP_INTERVAL_SECONDS))
    empty: dict = {"t": [], "setpoint": [], "t_predicted": [], "bypass": [], "feedforward": [],
                   "gap_ms": int(max(bucket * 2, config.CONTROL_LOOP_INTERVAL_SECONDS * 3) * 1000)}
    with connect_readonly(db_path) as conn:
        if not _table_exists(conn, "control_log"):
            return empty
        rows = conn.execute(
            """
            SELECT CAST(MIN(strftime('%s', timestamp)) AS INTEGER), AVG(setpoint), AVG(t_predicted),
                   AVG(mode = 'safety_bypass'), AVG(mode = 'feedforward')
            FROM control_log
            WHERE timestamp >= ? AND timestamp <= ?
            GROUP BY CAST(strftime('%s', timestamp) AS INTEGER) / ?
            ORDER BY 1
            """,
            (_utc_text(start), _utc_text(end), bucket),
        ).fetchall()
    empty["t"] = [int(r[0]) * 1000 for r in rows]
    empty["setpoint"] = [_round(r[1], 2) for r in rows]
    empty["t_predicted"] = [_round(r[2]) for r in rows]
    empty["bypass"] = [_round(r[3], 2) for r in rows]
    empty["feedforward"] = [_round(r[4], 2) for r in rows]
    return empty


def control_history(db_path: str, start: float, end: float, limit: int = 500) -> dict:
    """Cycles de contrôle de la période, du plus récent au plus ancien, avec les messages MQTT
    exacts, et un résumé (nombre de cycles, envois réussis / en échec, temps par mode)."""
    limit = max(1, min(int(limit), 5000))
    with connect_readonly(db_path) as conn:
        if not _table_exists(conn, "control_log"):
            return {"items": [], "summary": None}
        columns = _control_columns(conn)
        params = (_utc_text(start), _utc_text(end))
        rows = conn.execute(
            f"SELECT id, CAST(strftime('%s', timestamp) AS INTEGER), {', '.join(columns)} "  # nosec B608
            "FROM control_log WHERE timestamp >= ? AND timestamp <= ? ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        sent_expr = "SUM(mqtt_sent = 1), SUM(mqtt_sent = 0)" if "mqtt_sent" in columns else "NULL, NULL"
        summary = conn.execute(
            f"SELECT COUNT(*), AVG(setpoint), MIN(setpoint), MAX(setpoint), AVG(setpoint >= 99.95), "  # nosec B608
            f"AVG(setpoint <= 0.05), AVG(mode = 'feedforward'), AVG(mode = 'safety_bypass'), {sent_expr} "
            "FROM control_log WHERE timestamp >= ? AND timestamp <= ?",
            params,
        ).fetchone()
    items = []
    for row in rows:
        record = _control_record(columns, row[2:])
        record["id"] = row[0]
        record["ts"] = int(row[1]) * 1000
        items.append(record)
    keys = ("cycles", "mean", "min", "max", "share_max", "share_zero", "share_feedforward", "share_bypass",
            "sent", "failed")
    counts = ("cycles", "sent", "failed")
    return {"items": items,
            "summary": {k: (None if v is None else int(v)) if k in counts else _round(v, 3) for k, v in zip(keys, summary)},
            "truncated": len(items) == limit}


def control_rows(db_path: str, start: float, end: float) -> Iterator[tuple]:
    """Cycles de contrôle bruts de la période (export CSV), du plus ancien au plus récent."""
    with connect_readonly(db_path) as conn:
        if not _table_exists(conn, "control_log"):
            return
        columns = _control_columns(conn)
        yield ("timestamp_utc", *columns)
        yield from conn.execute(
            f"SELECT timestamp, {', '.join(columns)} FROM control_log "  # nosec B608
            "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
            (_utc_text(start), _utc_text(end)),
        )


def pid_settings() -> dict:
    """Réglages du PID et format des messages MQTT, en lecture seule.

    Chaque réglage indique sa variable : `env` = modifiable dans `.env` (puis ./voltaneum restart),
    `code` = constante de config.py.
    """
    def item(label: str, value: object, variable: str, source: str, unit: str = "", help_text: str = "") -> dict:
        return {"label": label, "value": value, "variable": variable, "source": source, "unit": unit,
                "help": help_text}

    interval = config.CONTROL_LOOP_INTERVAL_SECONDS
    return {
        "gains": [
            item("Proportionnel", config.PID_KP, "PID_KP", "env", "% / °C",
                 "Réagit à l'écart actuel : chaque °C au-dessus de la cible ajoute Kp % de consigne."),
            item("Intégral", config.PID_KI, "PID_KI", "env", "% / (°C·s)",
                 "Corrige un écart qui dure : l'écart cumulé dans le temps, multiplié par Ki."),
            item("Dérivé", config.PID_KD, "PID_KD", "env", "% / (°C/s)",
                 "Amortit : réagit à la vitesse de variation de l'écart."),
        ],
        "loop": [
            item("Cycle de calcul", interval, "CONTROL_LOOP_INTERVAL_SECONDS", "env", "s"),
            item("Température cible (PT02)", config.TARGET_TEMP, "TARGET_TEMP", "env", "°C"),
            item("Seuil critique : 100 % immédiat", config.CRITICAL_TEMP, "CRITICAL_TEMP", "env", "°C",
                 "Au-dessus, le PID est court-circuité (mode safety_bypass)."),
            item("Consigne minimale", config.SETPOINT_MIN, "SETPOINT_MIN", "code", "%"),
            item("Consigne maximale", config.SETPOINT_MAX, "SETPOINT_MAX", "code", "%"),
            item("Mesures trop anciennes : rien n'est calculé", config.CONTROL_MAX_DATA_AGE_SECONDS,
                 "CONTROL_MAX_DATA_AGE_SECONDS", "env", "s"),
            item("Publication de la consigne sur MQTT", "oui" if config.PUBLISH_PID_SETPOINT else "non",
                 "PUBLISH_PID_SETPOINT", "env"),
        ],
        "feedforward": [
            item("Anticipation ML : déclenchée si la prédiction dépasse la cible de",
                 config.FEEDFORWARD_PREDICTED_OVERSHOOT_THRESHOLD, "FEEDFORWARD_PREDICTED_OVERSHOOT_THRESHOLD",
                 "code", "°C"),
            item("Anticipation ML : ajout", config.FEEDFORWARD_PREDICTED_BOOST, "FEEDFORWARD_PREDICTED_BOOST",
                 "code", "%"),
            item("Anticipation météo : déclenchée si l'air extérieur dépasse",
                 config.FEEDFORWARD_OUTDOOR_HOT_THRESHOLD, "FEEDFORWARD_OUTDOOR_HOT_THRESHOLD", "code", "°C"),
            item("Anticipation météo : ajout", config.FEEDFORWARD_OUTDOOR_BOOST, "FEEDFORWARD_OUTDOOR_BOOST",
                 "code", "%"),
        ],
        "anti_windup": (
            f"L'intégrale est gelée quand la consigne est saturée (0 % ou 100 %) et que l'écart pousse "
            f"dans le même sens, et elle reste bornée entre 0 et {config.SETPOINT_MAX:g} % / Ki : "
            f"une longue période froide ne retarde pas la réaction suivante."
        ),
        "mqtt": mqtt_formats(),
        "broker": {"host_is_local": config.MQTT_HOST in ("localhost", "127.0.0.1", "::1"),
                   "tls": config.MQTT_TLS_ENABLED, "port": config.MQTT_PORT},
    }


def mqtt_formats() -> list[dict]:
    """Contrat des messages publiés par la boucle de contrôle (voir service/app.py et le cahier des charges §6.4)."""
    interval = config.CONTROL_LOOP_INTERVAL_SECONDS
    common = {"qos": 0, "retain": False, "encoding": "JSON UTF-8", "every_seconds": interval}
    return [
        {
            **common, "key": "setpoint", "topic": config.MQTT_TOPIC_SETPOINT, "title": "Consigne PID (informative)",
            "enabled": config.PUBLISH_PID_SETPOINT,
            "fields": [
                {"name": "percent", "type": "nombre", "unit": "%", "desc": "consigne de refroidissement suggérée, bornée entre 0 et 100"},
                {"name": "mode", "type": "texte", "unit": "", "desc": "pid, feedforward (anticipation ajoutée) ou safety_bypass (huile au-dessus du seuil critique : 100 %)"},
                {"name": "ts", "type": "nombre", "unit": "s", "desc": "horodatage d'envoi (secondes Unix, UTC)"},
            ],
        },
        {
            **common, "key": "conditions", "topic": config.MQTT_TOPIC_COOLING_CONDITIONS,
            "title": "Conditions de refroidissement (contrat principal)", "enabled": True,
            "fields": [
                {"name": "target_temp", "type": "nombre", "unit": "°C", "desc": "température cible de l'huile"},
                {"name": "critical_temp", "type": "nombre", "unit": "°C", "desc": "seuil critique"},
                {"name": "outdoor_band", "type": "texte", "unit": "", "desc": "froid, tempere, chaud ou inconnue"},
                {"name": "fan_on_above_temp", "type": "nombre", "unit": "°C", "desc": "seuil de marche du ventilateur"},
                {"name": "fan_off_below_temp", "type": "nombre", "unit": "°C", "desc": "seuil d'arrêt du ventilateur"},
                {"name": "fan_min_freq_hz", "type": "nombre ou null", "unit": "Hz", "desc": "fréquence minimale conseillée"},
                {"name": "t_oil", "type": "nombre", "unit": "°C", "desc": "température d'huile utilisée"},
                {"name": "t_outdoor", "type": "nombre ou null", "unit": "°C", "desc": "température extérieure utilisée"},
                {"name": "t_outdoor_source", "type": "texte ou null", "unit": "", "desc": "sensor (capteur) ou weather (API météo)"},
                {"name": "valid_for_seconds", "type": "nombre", "unit": "s", "desc": f"durée de validité ({3 * interval:g} s) : au-delà, l'automate ignore le message"},
                {"name": "ts", "type": "nombre", "unit": "s", "desc": "horodatage d'envoi (secondes Unix, UTC)"},
            ],
        },
        {
            **common, "key": "prediction", "topic": config.MQTT_TOPIC_PREDICTION, "title": "Prédiction ML",
            "enabled": True,
            "fields": [
                {"name": "predictions", "type": "objet", "unit": "°C", "desc": "valeur prévue de PT02 par modèle (random_forest, gradient_boosting)"},
                {"name": "combined_prediction", "type": "nombre", "unit": "°C", "desc": "valeur retenue (meilleur modèle à l'entraînement), utilisée par le PID"},
                {"name": "best_model", "type": "texte", "unit": "", "desc": "nom du modèle retenu"},
                {"name": "disagreement", "type": "nombre", "unit": "°C", "desc": "écart entre les deux modèles"},
                {"name": "disagreement_flag", "type": "booléen", "unit": "", "desc": f"vrai si l'écart dépasse {config.DISAGREEMENT_THRESHOLD_CELSIUS:g} °C"},
                {"name": "horizon_seconds", "type": "nombre", "unit": "s", "desc": "horizon de la prédiction"},
                {"name": "ts", "type": "nombre", "unit": "s", "desc": "horodatage d'envoi (secondes Unix, UTC)"},
            ],
        },
    ]


# ---------------------------------------------------------------------------
# Courbes
# ---------------------------------------------------------------------------
# Tranches de moyennage possibles (secondes) : des durées « rondes », lisibles à l'écran.
_BUCKETS = (15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200, 86400)


def _bucket_seconds(start: float, end: float, max_points: int) -> int:
    """Plus petite tranche ronde qui donne au plus `max_points` points (jamais sous l'intervalle
    des instantanés : en dessous, chaque mesure est déjà affichée telle quelle)."""
    needed = max(1.0, end - start) / max_points
    bucket = next((b for b in _BUCKETS if b >= needed), _BUCKETS[-1])
    return max(config.SNAPSHOT_INTERVAL_SECONDS, bucket)


def telemetry_series(db_path: str, start: float, end: float,
                     max_points: int = DEFAULT_MAX_POINTS) -> dict:
    """Mesures entre `start` et `end` (epoch secondes), moyennées par tranche si nécessaire.

    Chaque point porte aussi le min / max de PT02 de sa tranche (bande d'enveloppe), pour
    qu'un pic bref ne disparaisse pas dans la moyenne sur les longues périodes.
    """
    max_points = max(10, min(int(max_points), MAX_POINTS_LIMIT))
    bucket = _bucket_seconds(start, end, max_points)
    averages = ", ".join(f"AVG({name})" for name in MEASURES)
    with connect_readonly(db_path) as conn:
        rows = conn.execute(
            f"SELECT CAST(MIN(strftime('%s', timestamp)) AS INTEGER), {averages}, "  # nosec B608
            "MIN(pt02), MAX(pt02), COUNT(*) FROM telemetry "
            "WHERE timestamp >= ? AND timestamp <= ? "
            "GROUP BY CAST(strftime('%s', timestamp) AS INTEGER) / ? ORDER BY 1",
            (_utc_text(start), _utc_text(end), bucket),
        ).fetchall()
    n = len(MEASURES)
    return {
        "start": int(start * 1000),
        "end": int(end * 1000),
        "bucket_seconds": bucket,
        # Deux points plus éloignés que ce délai ne sont pas reliés (trou de collecte).
        "gap_ms": max(bucket * 2, config.MAX_GAP_SECONDS) * 1000,
        "t": [int(r[0]) * 1000 for r in rows],
        "series": {name: [_round(r[1 + i]) for r in rows] for i, name in enumerate(MEASURES)},
        "pt02_min": [_round(r[1 + n]) for r in rows],
        "pt02_max": [_round(r[2 + n]) for r in rows],
        "count": sum(int(r[3 + n]) for r in rows),
    }


def prediction_series(db_path: str, start: float, end: float,
                      max_points: int = DEFAULT_MAX_POINTS) -> dict:
    """Prédictions de PT02 positionnées à leur ÉCHÉANCE (target_ts), par modèle : sur la
    courbe, elles se superposent à la mesure réelle qu'elles devaient anticiper."""
    max_points = max(10, min(int(max_points), MAX_POINTS_LIMIT))
    bucket = _bucket_seconds(start, end, max_points)
    result: dict[str, dict[str, list]] = {}
    with connect_readonly(db_path) as conn:
        if not _table_exists(conn, "predictions_log"):
            return {"models": result}
        rows = conn.execute(
            """
            SELECT model_name, CAST(MIN(strftime('%s', target_ts)) AS INTEGER), AVG(predicted_value)
            FROM predictions_log
            WHERE target_ts >= ? AND target_ts <= ?
            GROUP BY model_name, CAST(strftime('%s', target_ts) AS INTEGER) / ?
            ORDER BY 1, 2
            """,
            (_utc_text(start), _utc_text(end), bucket),
        ).fetchall()
    for name, ts, value in rows:
        entry = result.setdefault(name, {"t": [], "v": []})
        entry["t"].append(int(ts) * 1000)
        entry["v"].append(_round(value))
    return {"models": result, "best_model": training_report().get("best_model")}


def _round(value: object, digits: int = 3) -> float | None:
    number = _finite(value)
    return None if number is None else round(number, digits)


def telemetry_rows(db_path: str, start: float, end: float) -> Iterator[tuple]:
    """Lignes brutes de la période, pour l'export CSV (générateur : pas tout en mémoire)."""
    with connect_readonly(db_path) as conn:
        columns = {row[1] for row in conn.execute("PRAGMA table_info(telemetry)")}
        source_col = "temp_outdoor_source" if "temp_outdoor_source" in columns else "NULL"
        cursor = conn.execute(
            f"SELECT timestamp, {', '.join(MEASURES)}, {source_col} FROM telemetry "  # nosec B608
            "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
            (_utc_text(start), _utc_text(end)),
        )
        yield from cursor


# ---------------------------------------------------------------------------
# Alertes, modèles, système
# ---------------------------------------------------------------------------
def alerts(db_path: str, limit: int = 200, level: str | None = None,
           since: float | None = None) -> dict:
    """Journal `alerts_log`, du plus récent au plus ancien, et le décompte par niveau sur 24 h."""
    limit = max(1, min(int(limit), 2000))
    with connect_readonly(db_path) as conn:
        if not _table_exists(conn, "alerts_log"):
            return {"items": [], "counts_24h": {}}
        where, params = [], []
        if level:
            where.append("level = ?")
            params.append(level)
        if since is not None:
            where.append("timestamp >= ?")
            params.append(_utc_text(since))
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        rows = conn.execute(
            f"SELECT id, CAST(strftime('%s', timestamp) AS INTEGER), level, rule_name, message, email_sent "  # nosec B608
            f"FROM alerts_log {clause} ORDER BY id DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        counts = dict(conn.execute(
            "SELECT level, COUNT(*) FROM alerts_log WHERE timestamp >= ? GROUP BY level",
            (_utc_text(time.time() - 86400),),
        ).fetchall())
    return {
        "items": [
            {"id": r[0], "ts": int(r[1]) * 1000 if r[1] is not None else None, "level": r[2],
             "rule": r[3], "message": r[4], "email_sent": bool(r[5])}
            for r in rows
        ],
        "counts_24h": counts,
    }


def training_report() -> dict:
    """Rapport du dernier entraînement (models/training_report.json), ou {} s'il n'existe pas."""
    path = Path(config.MODEL_DIR) / "training_report.json"
    try:
        with open(path, encoding="utf-8") as f:
            report = json.load(f)
    except (OSError, ValueError):
        return {}
    return report if isinstance(report, dict) else {}


def retrain_status() -> dict:
    """État du ré-entraînement automatique (ml/auto_retrain.py) : proposition et historique.

    Lecture seule : accepter ou refuser se fait sur le contrôleur (./voltaneum model).
    """
    from ml import auto_retrain

    root = auto_retrain.retrain_root(config.MODEL_DIR)
    proposal = auto_retrain.load_json(root / "proposal.json")
    history = auto_retrain.read_history(config.MODEL_DIR, limit=30)
    last_run = next((h for h in history if h.get("event") == "run"), None)
    return {
        "proposal": proposal,
        "last_run": last_run,
        "events": [{k: h.get(k) for k in ("event", "run_id", "started_at", "at", "decision", "reason", "operator",
                                           "note", "horizon_seconds", "proposed_horizon_seconds", "rows")}
                   for h in history],
        "criteria": {
            "min_gain": config.AUTO_RETRAIN_MIN_GAIN,
            "max_rmse": config.AUTO_RETRAIN_MAX_RMSE_CELSIUS,
            "max_days": config.AUTO_RETRAIN_MAX_DAYS,
            "min_test_span_horizons": auto_retrain.MIN_TEST_SPAN_HORIZONS,
            "horizons": list(config.AUTO_RETRAIN_HORIZONS_SECONDS),
        },
    }


def model_stats(db_path: str, window: int = 100) -> dict:
    """Erreurs réelles des modèles en production (prédiction comparée à la mesure à
    l'échéance, complétée par ml/monitor.py) et rapport du dernier entraînement."""
    horizons: list[dict] = []
    with connect_readonly(db_path) as conn:
        if _table_exists(conn, "predictions_log"):
            present = [int(r[0]) for r in conn.execute("SELECT DISTINCT horizon_seconds FROM predictions_log")]
            for horizon in sorted(set(config.PREDICTION_HORIZONS_SECONDS) | set(present)):
                for model in MODELS:
                    errors = [float(r[0]) for r in conn.execute(
                        "SELECT error FROM predictions_log WHERE horizon_seconds = ? AND model_name = ? "
                        "AND error IS NOT NULL ORDER BY id DESC LIMIT ?",
                        (horizon, model, window),
                    )]
                    if not errors:
                        continue
                    horizons.append({
                        "horizon_seconds": horizon,
                        "model": model,
                        "n": len(errors),
                        "rmse": round(math.sqrt(sum(e * e for e in errors) / len(errors)), 3),
                        "mae": round(sum(abs(e) for e in errors) / len(errors), 3),
                        "bias": round(sum(errors) / len(errors), 3),
                    })
    report = training_report()
    return {
        "live": horizons,
        "window": window,
        "training": {key: report.get(key) for key in (
            "status", "best_model", "beats_persistence", "trained_at", "n_samples", "horizon_seconds",
            "random_forest", "gradient_boosting", "baseline_persistence", "degenerate_features", "message",
        ) if key in report},
    }


def system_status(db_path: str) -> dict:
    """Taille de la base, espace disque, dernière sauvegarde, étendue des données."""
    path = Path(db_path)
    size = sum(p.stat().st_size for p in (path, Path(f"{db_path}-wal")) if p.exists())
    try:
        disk = shutil.disk_usage(path.parent)
        disk_info = {"total": disk.total, "free": disk.free}
    except OSError:
        disk_info = None

    last_backup = None
    backup_dir = Path(config.STORAGE_BACKUP_DIR)
    if backup_dir.is_dir():
        backups = [p for p in backup_dir.rglob("*.db.gz") if p.is_file()]
        if backups:
            newest = max(backups, key=lambda p: p.stat().st_mtime)
            last_backup = {"name": newest.name, "ts": int(newest.stat().st_mtime * 1000),
                           "size": newest.stat().st_size}

    with connect_readonly(db_path) as conn:
        count, first, last = conn.execute(
            "SELECT COUNT(*), CAST(strftime('%s', MIN(timestamp)) AS INTEGER), "
            "CAST(strftime('%s', MAX(timestamp)) AS INTEGER) FROM telemetry"
        ).fetchone()
        last_hour = conn.execute(
            "SELECT COUNT(*) FROM telemetry WHERE timestamp >= ?", (_utc_text(time.time() - 3600),)
        ).fetchone()[0]

    expected_per_hour = 3600 / config.SNAPSHOT_INTERVAL_SECONDS
    return {
        "db_path": str(path.name),
        "db_bytes": size,
        "storage_limit_bytes": config.STORAGE_LIMIT_BYTES,
        "storage_warning_ratio": config.STORAGE_WARNING_RATIO,
        "disk": disk_info,
        "last_backup": last_backup,
        "rows": count,
        "first_ts": first * 1000 if first else None,
        "last_ts": last * 1000 if last else None,
        "rows_last_hour": last_hour,
        "completeness_last_hour": round(min(1.0, last_hour / expected_per_hour), 3),
        "snapshot_interval_seconds": config.SNAPSHOT_INTERVAL_SECONDS,
    }


def thresholds() -> dict:
    """Seuils utiles à l'affichage (aucun identifiant ni adresse)."""
    return {
        "target_temp": config.TARGET_TEMP,
        "critical_temp": config.CRITICAL_TEMP,
        "fan_hysteresis": config.FAN_HYSTERESIS_CELSIUS,
        "pump_running_min_hz": config.PUMP_RUNNING_MIN_HZ,
        "flow_min_m3h": config.FLOW_MIN_M3H,
        "stale_after_seconds": config.CONTROL_MAX_DATA_AGE_SECONDS,
        "snapshot_interval_seconds": config.SNAPSHOT_INTERVAL_SECONDS,
        "valid_ranges": {k: list(v) for k, v in config.SENSOR_VALID_RANGES.items()},
    }


def settings_summary() -> dict:
    """Réglages en vigueur, en lecture seule, pour l'écran « Réglages ».

    Volontairement limité aux seuils de procédé : aucun identifiant, mot de passe, adresse
    de passerelle ou de serveur SMTP n'est exposé par l'interface.
    """
    return {
        "process": {
            "Température cible huile (°C)": config.TARGET_TEMP,
            "Seuil critique huile (°C)": config.CRITICAL_TEMP,
            "Hystérésis ventilateur (°C)": config.FAN_HYSTERESIS_CELSIUS,
            "Pompe en marche au-delà de (Hz)": config.PUMP_RUNNING_MIN_HZ,
            "Débit minimal pompe en marche (m³/h)": config.FLOW_MIN_M3H,
            "Gains PID (Kp / Ki / Kd)": f"{config.PID_KP} / {config.PID_KI} / {config.PID_KD}",
            "Publication consigne PID": "oui" if config.PUBLISH_PID_SETPOINT else "non",
        },
        "bands": [
            {"name": name, "upper": None if math.isinf(upper) else upper, "offset": offset,
             "fan_on": config.TARGET_TEMP + offset,
             "fan_off": config.TARGET_TEMP + offset - config.FAN_HYSTERESIS_CELSIUS,
             "min_freq_hz": freq}
            for upper, name, offset, freq in config.OUTDOOR_BANDS
        ],
        "collection": {
            "Intervalle des instantanés (s)": config.SNAPSHOT_INTERVAL_SECONDS,
            "Mesure capteur périmée après (s)": config.SENSOR_MAX_AGE_SECONDS,
            "Données périmées pour la régulation après (s)": config.CONTROL_MAX_DATA_AGE_SECONDS,
            "Capteur extérieur": config.OUTDOOR_SENSOR_MODE,
            "Repli météo": "activé" if config.OUTDOOR_WEATHER_FALLBACK_ENABLED else "désactivé",
            "Horizons de prédiction (s)": ", ".join(str(h) for h in config.PREDICTION_HORIZONS_SECONDS),
        },
        "alerts": {
            "Cycles de confirmation": config.ALERT_CONFIRMATION_CYCLES,
            "Délai entre deux e-mails (min)": config.ALERT_COOLDOWN_MINUTES,
            "Perte de télémétrie après (s)": config.TELEMETRY_TIMEOUT_SECONDS,
            "Désaccord entre modèles (°C)": config.DISAGREEMENT_THRESHOLD_CELSIUS,
        },
        "retention": {
            "Mesures conservées au moins (jours)": config.RETENTION_DAYS,
            "Prédictions conservées au moins (jours)": config.PREDICTIONS_RETENTION_DAYS,
            "Alertes conservées au moins (jours)": config.ALERTS_RETENTION_DAYS,
        },
        "valid_ranges": {k: list(v) for k, v in config.SENSOR_VALID_RANGES.items()},
    }
