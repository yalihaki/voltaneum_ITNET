"""dashboard/exports.py — Exports CSV, un par élément (interface web et `./voltaneum export`).

Format « Excel français » : séparateur « ; », virgule décimale, encodage UTF-8 avec BOM (accents
corrects à l'ouverture dans Excel). Chaque ligne horodatée porte l'heure UTC (telle qu'en base)
ET l'heure locale. Lecture seule ; les lignes sont produites au fil de l'eau (pas tout en mémoire).
"""

from __future__ import annotations

import csv
import io
import time
from collections.abc import Iterable, Iterator
from datetime import datetime, timezone

import config
from dashboard import data

BOM = "\ufeff"  # marque UTF-8 : Excel affiche correctement les accents
# Grandeurs enregistrées en base mais ni affichées ni exportées pour l'instant.
HIDDEN_MEASURES = {"humidity_outdoor"}


def _local(utc_text: str | None) -> str:
    """'2026-10-16 08:04:04' (UTC) -> '16/10/2026 11:04:04' (heure locale de la machine)."""
    if not utc_text:
        return ""
    try:
        moment = datetime.strptime(str(utc_text)[:19], "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    except ValueError:
        return ""
    return moment.astimezone().strftime("%d/%m/%Y %H:%M:%S")


def _cell(value: object) -> object:
    """Nombre avec virgule décimale (Excel français) ; le reste inchangé."""
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, float):
        text = f"{value:.6f}".rstrip("0").rstrip(".")
        return text.replace(".", ",") if text not in ("", "-") else "0"
    return value


def _with_local_time(rows: Iterable[tuple], time_index: int = 0) -> Iterator[tuple]:
    """Insère une colonne « heure locale » après la colonne d'horodatage UTC."""
    for row in rows:
        yield (row[time_index], _local(row[time_index]), *row[time_index + 1:])


# ---------------------------------------------------------------------------
# Un générateur par élément : en-tête, puis lignes
# ---------------------------------------------------------------------------
def measures(db_path: str, start: float, end: float) -> Iterator[tuple]:
    labels = {k: v[0] for k, v in config.SENSOR_LABELS.items()}
    names = {"ht01": "HT01 débit (m³/h)", "freq_pompe": "fréquence pompe (Hz)", "temp_outdoor": "air extérieur (°C)",
             "thermal_power_kw": "puissance dissipée estimée (kW)"}
    # Mesures exportées : l'humidité est enregistrée mais n'est pas exportée pour l'instant.
    keep = [i for i, m in enumerate(data.MEASURES) if m not in HIDDEN_MEASURES]
    header = ["date UTC", "heure locale",
              *[f"{labels[data.MEASURES[i]]} (°C)" if data.MEASURES[i] in labels else names.get(data.MEASURES[i], data.MEASURES[i])
                for i in keep],
              "source air extérieur"]
    yield tuple(header)
    for row in _with_local_time(data.telemetry_rows(db_path, start, end)):
        yield (row[0], row[1], *[row[2 + i] for i in keep], row[2 + len(data.MEASURES)])


_CONTROL_HEADERS = {
    "t_oil": "PT02 (°C)", "t_target": "cible (°C)", "t_predicted": "prédiction utilisée (°C)",
    "t_outdoor": "air extérieur (°C)", "t_outdoor_source": "source air extérieur", "setpoint": "consigne (%)",
    "mode": "mode", "error": "écart PT02 - cible (°C)", "p_term": "terme P (%)", "i_term": "terme I (%)",
    "d_term": "terme D (%)", "feedforward_prediction": "anticipation ML (%)",
    "feedforward_outdoor": "anticipation météo (%)", "raw_output": "somme avant bornage (%)",
    "outdoor_band": "bande air extérieur", "fan_on_above": "ventilateur marche au-dessus de (°C)",
    "fan_off_below": "ventilateur arrêt en dessous de (°C)", "fan_min_freq_hz": "fréquence min. conseillée (Hz)",
    "phase": "étape de la régulation", "event": "message envoyé ce cycle (vide : rien à envoyer)",
    "command_percent": "consigne conseillée au ventilateur (%)",
    "cooling_mode": "conseil air seul / ventilateur", "cooling_mode_reason": "raison du conseil",
    "mqtt_setpoint": "message MQTT consigne (exact)", "mqtt_conditions": "message MQTT conditions (exact)",
    "mqtt_sent": "message transmis (1 oui, 0 échec, vide : rien à envoyer)",
}


def setpoints(db_path: str, start: float, end: float) -> Iterator[tuple]:
    rows = data.control_rows(db_path, start, end)
    header = next(rows, None)
    if header is None:
        yield ("date UTC", "heure locale", "aucun cycle de régulation enregistré")
        return
    yield ("date UTC", "heure locale", *[_CONTROL_HEADERS.get(c, c) for c in header[1:]])
    yield from _with_local_time(rows)


def alerts(db_path: str, start: float, end: float) -> Iterator[tuple]:
    yield ("date UTC", "heure locale", "niveau", "règle", "message", "e-mail envoyé")
    with data.connect_readonly(db_path) as conn:
        if not data._table_exists(conn, "alerts_log"):
            return
        cursor = conn.execute(
            "SELECT timestamp, level, rule_name, message, email_sent FROM alerts_log "
            "WHERE timestamp >= ? AND timestamp <= ? ORDER BY timestamp",
            (data._utc_text(start), data._utc_text(end)),
        )
        for ts, level, rule, message, sent in cursor:
            yield (ts, _local(ts), level, rule, (message or "").replace("\n", " "), "oui" if sent else "non")


def predictions(db_path: str, start: float, end: float) -> Iterator[tuple]:
    yield ("prédite le (UTC)", "prédite le (heure locale)", "échéance (UTC)", "échéance (heure locale)",
           "horizon (s)", "modèle", "PT02 prédite (°C)", "PT02 de référence à l'échéance (°C, estimée si le modèle la prédit)",
           "erreur (°C)", "PT02 envoyée par l'automate à l'échéance (°C)", "écart à la PT02 de l'automate (°C)")
    with data.connect_readonly(db_path) as conn:
        if not data._table_exists(conn, "predictions_log"):
            return
        columns = {row[1] for row in conn.execute("PRAGMA table_info(predictions_log)")}
        measured = "measured_value" if "measured_value" in columns else "NULL"
        cursor = conn.execute(
            "SELECT made_at, target_ts, horizon_seconds, model_name, predicted_value, actual_value, error, "  # nosec B608
            f"{measured} FROM predictions_log WHERE made_at >= ? AND made_at <= ? ORDER BY made_at, model_name",
            (data._utc_text(start), data._utc_text(end)),
        )
        for made, target, horizon, model, predicted, actual, error, plc in cursor:
            gap = None if plc is None or predicted is None else round(predicted - plc, 4)
            yield (made, _local(made), target, _local(target), horizon, model, predicted, actual, error, plc, gap)


def retraining(db_path: str, start: float, end: float) -> Iterator[tuple]:
    """Une ligne par horizon essayé à chaque ré-entraînement, plus les décisions humaines."""
    from ml import auto_retrain

    yield ("date (heure locale)", "événement", "horizon", "verdict", "erreur (°C)", "persistance (°C)",
           "gain sur la persistance", "raison / décision", "opérateur")
    for event in reversed(auto_retrain.read_history(config.MODEL_DIR, limit=100_000)):
        when_text = str(event.get("started_at") or event.get("at") or "")
        try:
            when = datetime.fromisoformat(when_text)
        except (TypeError, ValueError):
            continue
        if not start <= when.timestamp() <= end:
            continue
        local = when.astimezone().strftime("%d/%m/%Y %H:%M:%S")
        if event.get("event") == "run":
            for c in event.get("candidates", []):
                yield (local, "ré-entraînement", auto_retrain.format_horizon(c["horizon_seconds"]),
                       "fiable" if c.get("eligible") else "refusé", c.get("rmse"), c.get("persistence_rmse"),
                       c.get("gain"), c.get("reason"), "")
            yield (local, "décision", "", "", "", "", "", event.get("reason"), "")
        else:
            horizon = event.get("horizon_seconds")
            kind = str(event.get("event"))
            yield (local, {"accepted": "modèle installé", "rejected": "proposition refusée",
                           "rollback": "retour arrière"}.get(kind, kind),
                   auto_retrain.format_horizon(horizon) if horizon else "", "", "", "", "",
                   event.get("note") or "", event.get("operator") or "")


EXPORTS = {
    "mesures": ("Mesures", measures),
    "consignes": ("Consignes PID et messages MQTT", setpoints),
    "alertes": ("Alertes", alerts),
    "predictions": ("Prédictions", predictions),
    "entrainements": ("Ré-entraînements", retraining),
}


def write_csv(rows: Iterable[tuple], out: io.TextIOBase) -> int:
    """Écrit les lignes au format Excel français ; retourne le nombre de lignes de données."""
    out.write(BOM)
    writer = csv.writer(out, delimiter=";", lineterminator="\r\n")
    count = -1
    for row in rows:
        writer.writerow([_cell(v) for v in row])
        count += 1
    return max(count, 0)


def csv_chunks(name: str, db_path: str, start: float, end: float, rows_per_chunk: int = 2000) -> Iterator[bytes]:
    """Le CSV d'un élément, par morceaux d'octets (envoi HTTP au fil de l'eau)."""
    _label, producer = EXPORTS[name]
    buffer = io.StringIO()
    buffer.write(BOM)
    writer = csv.writer(buffer, delimiter=";", lineterminator="\r\n")
    for i, row in enumerate(producer(db_path, start, end), 1):
        writer.writerow([_cell(v) for v in row])
        if i % rows_per_chunk == 0:
            yield buffer.getvalue().encode("utf-8")
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue().encode("utf-8")


def filename(name: str, start: float, end: float) -> str:
    fmt = "%Y%m%d-%H%M"
    return (f"voltaneum-{name}-{time.strftime(fmt, time.localtime(start))}"
            f"-{time.strftime(fmt, time.localtime(end))}.csv")


def describe() -> list[dict]:
    return [{"name": name, "label": label} for name, (label, _f) in EXPORTS.items()]
