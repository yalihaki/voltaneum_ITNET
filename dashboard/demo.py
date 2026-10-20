"""dashboard/demo.py — Base de DÉMONSTRATION pour essayer l'interface sans le contrôleur.

`./voltaneum demo` crée `demo_dashboard.db` (7 jours de mesures fictives,
prédictions et alertes), puis y ajoute une mesure toutes les 15 s pour que l'écran vive.
Ne touche jamais à la vraie base `voltaneum.db`.
"""

from __future__ import annotations

import json
import logging
import math
import random
import threading
import time
from pathlib import Path

import config
from control.cooling_advice import compute_cooling_conditions
from control.supervisor import decide
from control.trend import slope_per_10min
from control.pid_controller import compute_setpoint_detailed
from control.thermal import estimate_power_kw
from database import sqlite_client
from ml.features import MODEL_FEATURES

logger = logging.getLogger(__name__)

DEMO_DB_PATH = str(config.PROJECT_ROOT / "demo_dashboard.db")
# Modèles de démonstration (./voltaneum retrain --db demo_dashboard.db --model-dir demo_models).
DEMO_MODEL_DIR = str(config.PROJECT_ROOT / "demo_models")
_HISTORY_DAYS = 7


def _utc(epoch: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


class _Process:
    """Modèle thermique grossier, dans l'ordre des mesures réelles (PT02 > PT01 > PT03 > PT04) :
    les équipements chauffent le diélectrique de l'armoire (PT02), la CDU le refroidit (PT01, renvoyé
    dans l'armoire) ; la boucle d'eau glycolée emporte la chaleur (PT03 chaud) vers le dry cooler,
    qui la rend à l'air (PT04 froid). Pour que la démonstration ait de quoi apprendre, la « fréquence » y varie et
    représente l'effort de refroidissement (sur le site, la pompe reste à 30 Hz)."""

    def __init__(self, seed: int = 7) -> None:
        self.rng = random.Random(seed)  # nosec B311
        self.oil = 34.0
        self.freq = 25.0
        self.pid_state: dict = {}
        self.supervisor_state: dict = {}
        self.recent: list[tuple[float, float]] = []    # (instant, PT02) : tendance pour les étapes

    def outdoor(self, t: float) -> float:
        hour = (t / 3600) % 24
        day = t / 86400
        return 13.0 + 6.0 * math.sin((hour - 9) / 24 * 2 * math.pi) + 2.0 * math.sin(day / 3) + self.rng.gauss(0, 0.15)

    def step(self, t: float, dt: float) -> dict:
        ext = self.outdoor(t)
        load = 1.0 + 0.6 * math.sin(t / 5400) + 0.3 * math.sin(t / 1300)
        cooling = 0.0045 * self.freq * max(0.0, self.oil - ext) / 20
        self.oil += (0.11 * load - cooling) * dt / 15 + self.rng.gauss(0, 0.03)
        target_freq = 22 + 9 * (self.oil - 34.0)
        self.freq += (max(0.0, min(50.0, target_freq)) - self.freq) * 0.08 + self.rng.gauss(0, 0.3)
        self.freq = max(0.0, min(50.0, self.freq))
        flow = 0.0 if self.freq < 3 else 0.6 + self.freq * 0.065 + self.rng.gauss(0, 0.03)
        coolant_cold = ext + 0.55 * (self.oil - ext)          # sortie du dry cooler (PT04)
        return {
            "pt01": round(self.oil - 0.6 * load + self.rng.gauss(0, 0.05), 2),   # sortie CDU, vers l'armoire
            "pt02": round(self.oil),                                               # sortie armoire, lue au degré près
            "pt03": round(coolant_cold + 0.9 * load + self.rng.gauss(0, 0.05), 2),
            "pt04": round(coolant_cold + self.rng.gauss(0, 0.1), 2),
            "ht01": round(max(0.0, flow), 3),
            "freq_pompe": round(self.freq, 1),
            "temp_outdoor": round(ext, 1),
        }


def _insert(conn, ts: float, values: dict) -> None:
    hour = (ts / 3600) % 24
    humidity = round(70 - 18 * math.sin((hour - 9) / 24 * 2 * math.pi), 0)   # plus sec l'après-midi
    conn.execute(
        "INSERT INTO telemetry (timestamp, pt01, pt02, pt03, pt04, ht01, freq_pompe, temp_outdoor, "
        "temp_outdoor_source, humidity_outdoor, thermal_power_kw) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'weather', ?, ?)",
        (_utc(ts), values["pt01"], values[config.TARGET_COLUMN], values["pt03"], values["pt04"], values["ht01"],
         values["freq_pompe"], values["temp_outdoor"], humidity, estimate_power_kw(values)),
    )


def _predict(conn, made_at: float, target_now: float, target_future: float, rng: random.Random,
             horizon: int, complete: bool) -> None:
    for model, noise in (("random_forest", 0.35), ("gradient_boosting", 0.28)):
        predicted = target_future + rng.gauss(0, noise) if complete else target_now + rng.gauss(0, noise)
        actual = target_future if complete else None
        conn.execute(
            "INSERT INTO predictions_log (made_at, target_ts, horizon_seconds, model_name, predicted_value, "
            "actual_value, error) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (_utc(made_at), _utc(made_at + horizon), horizon, model, round(predicted, 3),
             actual, None if actual is None else round(predicted - actual, 4)),
        )


def _log_control(conn, process: _Process, ts: float, values: dict, predicted: float | None,
                 broker_up: bool = True) -> None:
    """Un cycle de contrôle calculé avec le VRAI code (PID, étapes), comme la boucle de contrôle :
    un message n'est « envoyé » qu'aux étapes."""
    t_oil, t_ext = values[config.TARGET_COLUMN], values["temp_outdoor"]
    detail = compute_setpoint_detailed(
        t_oil, config.TARGET_TEMP, t_oil if predicted is None else predicted, t_ext,
        config.CONTROL_LOOP_INTERVAL_SECONDS, process.pid_state,
    )
    process.pid_state = detail.state
    window = config.REGULATION_TREND_WINDOW_SECONDS
    process.recent = [(t, v) for t, v in process.recent if t >= ts - window]
    slope = slope_per_10min(process.recent, window)
    decision, process.supervisor_state = decide(
        t_oil, slope, t_ext, detail.setpoint, predicted, ts, process.supervisor_state)
    cond = compute_cooling_conditions(t_oil, t_ext, values["pt02"] - values["pt01"]) or {}
    event = decision.event
    step = {"phase": decision.phase, "event": event, "cooling_mode": decision.cooling_mode,
            "cooling_mode_reason": decision.reason, "command_percent": decision.command_percent}
    sent = event is not None and broker_up
    record = {
        "t_oil": t_oil, "t_target": config.TARGET_TEMP, "t_predicted": predicted, "t_outdoor": t_ext,
        "t_outdoor_source": "weather", "setpoint": detail.setpoint, "mode": detail.mode, "error": detail.error,
        "p_term": detail.p_term, "i_term": detail.i_term, "d_term": detail.d_term,
        "feedforward_prediction": detail.feedforward_prediction,
        "feedforward_outdoor": detail.feedforward_outdoor, "raw_output": detail.raw_output,
        "outdoor_band": cond.get("outdoor_band"), "fan_on_above": cond.get("fan_on_above_temp"),
        "fan_off_below": cond.get("fan_off_below_temp"), "fan_min_freq_hz": cond.get("fan_min_freq_hz"),
        **step,
        # Mêmes messages que la boucle de contrôle (_publish) ; aucun broker réel n'est contacté.
        "mqtt_setpoint": json.dumps({"percent": decision.command_percent, "pid_percent": detail.setpoint,
                                     "mode": detail.mode, "phase": decision.phase, "event": event,
                                     "ts": ts}) if sent else None,
        "mqtt_conditions": json.dumps({**cond, "t_outdoor_source": "weather", **step, "ts": ts}) if sent and cond else None,
        "mqtt_sent": None if event is None else int(broker_up),
    }
    columns = ["timestamp", *record]
    conn.execute(
        f"INSERT INTO control_log ({', '.join(columns)}) VALUES ({', '.join('?' for _ in columns)})",  # nosec B608
        (_utc(ts), *record.values()),
    )


def create_demo_database(path: str = DEMO_DB_PATH) -> _Process:
    """(Re)crée la base de démonstration et renvoie le processus simulé, prêt à continuer."""
    for suffix in ("", "-wal", "-shm"):
        Path(f"{path}{suffix}").unlink(missing_ok=True)
    sqlite_client.init_db(path)
    process = _Process()
    rng = random.Random(3)  # nosec B311
    interval = config.SNAPSHOT_INTERVAL_SECONDS
    horizon = max(config.PREDICTION_HORIZONS_SECONDS)
    now = time.time()
    start = now - _HISTORY_DAYS * 86400
    history: list[tuple[float, float]] = []
    snapshots: list[tuple[float, dict]] = []
    with sqlite_client.connect(path) as conn:
        t = start
        while t <= now:
            # Deux courts trous de collecte, pour montrer leur rendu.
            if not (start + 2.2 * 86400 < t < start + 2.25 * 86400 or now - 5 * 3600 < t < now - 4.9 * 3600):
                values = process.step(t, interval)
                _insert(conn, t, values)
                history.append((t, values[config.TARGET_COLUMN]))
                snapshots.append((t, values))
            else:
                process.step(t, interval)
            t += interval
        # Prédictions toutes les 60 s sur les dernières 48 h, comparées à la mesure à l'échéance.
        by_ts = dict(history)
        times = [h[0] for h in history]
        for i, (ts, value) in enumerate(history):
            if ts < now - 48 * 3600 or i % 4:
                continue
            future_index = i + horizon // interval
            if future_index < len(history):
                _predict(conn, ts, value, by_ts[times[future_index]], rng, horizon, complete=True)
            elif ts > now - horizon:
                _predict(conn, ts, value, value - 0.2, rng, horizon, complete=False)
        # Boucle de contrôle toutes les 60 s, avec la prédiction combinée quand elle existe.
        control_process = _Process()
        step = max(1, int(config.CONTROL_LOOP_INTERVAL_SECONDS // interval))
        for i in range(0, len(snapshots), step):
            ts, values = snapshots[i]
            future_index = i + horizon // interval
            future = snapshots[min(future_index, len(snapshots) - 1)][1][config.TARGET_COLUMN]
            predicted = round(future + rng.gauss(0, 0.3), 2)
            # Broker « indisponible » 25 minutes il y a 30 h : envois en échec, pour le rendu.
            broker_up = not (now - 30 * 3600 < ts < now - 30 * 3600 + 1500)
            lookback = int(config.REGULATION_TREND_WINDOW_SECONDS // interval) + 1
            control_process.recent = [(t, v[config.TARGET_COLUMN]) for t, v in snapshots[max(0, i - lookback):i + 1]]
            _log_control(conn, control_process, ts, values, predicted, broker_up)
        process.pid_state = control_process.pid_state
        process.supervisor_state = control_process.supervisor_state
        process.recent = control_process.recent
        alerts = [
            (now - 6 * 86400, "INFO", "storage_backup", "Sauvegarde quotidienne effectuée", 0),
            (now - 5.2 * 86400, "CRITIQUE", "telemetry_lost", "Perte de télémétrie : aucune mesure depuis 300 s", 1),
            (now - 3 * 86400, "ATTENTION", "model_disagreement", "Désaccord entre modèles : 1,3 °C", 1),
            (now - 26 * 3600, "ATTENTION", "efficiency_drift", "Efficacité de l'échangeur hors de la plage attendue", 1),
            (now - 4.9 * 3600, "CRITIQUE", "telemetry_lost", "Perte de télémétrie : passerelle muette depuis 6 min", 1),
            (now - 40 * 60, "INFO", "smtp_config_missing", "Alerte non envoyée : SMTP non configuré (démonstration)", 0),
        ]
        conn.executemany(
            "INSERT INTO alerts_log (timestamp, level, rule_name, message, email_sent) VALUES (?, ?, ?, ?, ?)",
            [(_utc(ts), lvl, rule, msg, sent) for ts, lvl, rule, msg, sent in alerts],
        )
    _seed_catalogue(path, snapshots[-1][1], now)
    _write_demo_training_report(history, horizon)
    logger.info("Base de démonstration créée : %s (%d mesures).", path, len(history))
    return process


# Catalogue FICTIF, sur le modèle du catalogue réel de la passerelle 10.0.1.34 (relevé du 9/10/2026) :
# les six points lus, plus des points modifiables et de diagnostic que Voltaneum n'utilise pas.
_DEMO_CATALOGUE: list[dict] = [
    {"KeyNode": "K0", "KeyName": "PT01", "WrEnable": 0, "DataType": "Float"},
    {"KeyNode": "K1", "KeyName": "PT02", "WrEnable": 0, "DataType": "Float"},
    {"KeyNode": "K2", "KeyName": "PT03", "WrEnable": 0, "DataType": "Float"},
    {"KeyNode": "K3", "KeyName": "PT04", "WrEnable": 0, "DataType": "Float"},
    {"KeyNode": "K4", "KeyName": "HT01", "WrEnable": 0, "DataType": "Float"},
    {"KeyNode": "K5", "KeyName": "PUMP", "WrEnable": 0, "DataType": "Int16"},
    {"KeyNode": "K6", "KeyName": "ON/OFF", "WrEnable": 1, "DataType": "Bool"},
    {"KeyNode": "K12", "KeyName": "Temperature setpoint", "WrEnable": 1, "DataType": "Float"},
    {"KeyNode": "K17", "KeyName": "Emergency stop bypass", "WrEnable": 1, "DataType": "Bool"},
    {"KeyNode": "auto_mode", "KeyName": "AUTO REQUEST M4.2", "WrEnable": 1, "DataType": "Bool"},
    {"KeyNode": "diag_vw12", "KeyName": "DIAG Actual frequency VW12", "WrEnable": 0, "DataType": "Int16"},
]
_DEMO_COLUMNS = {"K0": "pt01", "K1": "pt02", "K2": "pt03", "K3": "pt04", "K4": "ht01", "K5": "freq_pompe"}


def _catalogue_values(values: dict, now: float) -> dict[str, tuple[object, float]]:
    raw: dict[str, tuple[object, float]] = {k: (values[c], now) for k, c in _DEMO_COLUMNS.items()}
    raw.update({"K6": (1, now), "K12": (config.TARGET_TEMP, now), "K17": (0, now), "auto_mode": (1, now),
                "diag_vw12": (round(values["freq_pompe"]), now)})
    return raw


def _seed_catalogue(path: str, values: dict, now: float) -> None:
    node = config.SENSOR_DRIVER_NODE
    sqlite_client.save_catalogue([{**p, "DevNode": node} for p in _DEMO_CATALOGUE], _DEMO_COLUMNS, node, db_path=path)
    sqlite_client.update_catalogue_values(node, _catalogue_values(values, now), db_path=path)


def _write_demo_training_report(history: list[tuple[float, float]], horizon: int) -> None:
    """Rapport d'entraînement FICTIF, cohérent avec les prédictions de la démo (bruit de 0,35 °C
    et 0,28 °C) et avec la persistance réellement mesurée sur les données de démonstration.
    Écrit seulement dans le dossier de démonstration, jamais dans MODEL_DIR."""
    step = max(1, horizon // config.SNAPSHOT_INTERVAL_SECONDS)
    gaps = [history[i + step][1] - history[i][1] for i in range(0, len(history) - step, 4)]
    persistence = math.sqrt(sum(g * g for g in gaps) / len(gaps)) if gaps else 0.5
    report = {
        "status": "ok", "best_model": "gradient_boosting", "beats_persistence": True,
        "trained_at": int(time.time()), "n_samples": len(history), "horizon_seconds": horizon,
        "features": list(MODEL_FEATURES), "target": "delta", "target_column": config.TARGET_COLUMN,
        "random_forest": {"rmse": 0.35, "mae": 0.28},
        "gradient_boosting": {"rmse": 0.28, "mae": 0.22},
        "baseline_persistence": {"rmse": round(max(persistence, 0.45), 3), "mae": round(max(persistence, 0.45) * 0.8, 3)},
        "message": "Démonstration : rapport fictif.",
    }
    model_dir = Path(DEMO_MODEL_DIR)
    model_dir.mkdir(parents=True, exist_ok=True)
    (model_dir / "training_report.json").write_text(json.dumps(report, indent=2), encoding="utf-8")


def run_live_feed(process: _Process, stop: threading.Event, path: str = DEMO_DB_PATH) -> None:
    """Ajoute une mesure (et des prédictions chaque minute) au rythme réel des instantanés."""
    interval = config.SNAPSHOT_INTERVAL_SECONDS
    horizon = max(config.PREDICTION_HORIZONS_SECONDS)
    rng = random.Random()  # nosec B311
    count = 0
    while not stop.wait(interval):
        now = time.time()
        values = process.step(now, interval)
        process.recent.append((now, values[config.TARGET_COLUMN]))
        with sqlite_client.connect(path) as conn:
            _insert(conn, now, values)
            if count % 4 == 0:
                _predict(conn, now, values[config.TARGET_COLUMN], values[config.TARGET_COLUMN] - 0.15, rng, horizon, complete=False)
                _log_control(conn, process, now, values, round(values[config.TARGET_COLUMN] - 0.15 + rng.gauss(0, 0.2), 2))
            if count and count % 120 == 0:
                conn.execute(
                    "INSERT INTO alerts_log (level, rule_name, message, email_sent) VALUES (?, ?, ?, 0)",
                    ("ATTENTION", "demo", "Alerte de démonstration (une toutes les 30 min)"),
                )
        sqlite_client.update_catalogue_values(config.SENSOR_DRIVER_NODE, _catalogue_values(values, now), db_path=path)
        count += 1
