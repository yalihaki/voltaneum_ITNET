"""ml/auto_retrain.py — Ré-entraînement automatique, avec validation humaine.

En clair : chaque nuit, le processus principal lance `voltaneum.py retrain`, qui ré-entraîne les modèles sur les données récentes,
pour une série d'horizons (10 min, 15 min, 30 min, 1 h ... 24 h). Il garde l'horizon le plus
LONG qui reste fiable et le PROPOSE. Il ne remplace JAMAIS seul le modèle utilisé par la
régulation : une personne valide avec `./voltaneum model accept` (ou refuse avec `reject`).

Pourquoi une validation humaine : changer d'horizon change le comportement du PID (son
anticipation se déclenche sur la prédiction). Tant que le PID n'est pas réglé avec
l'automaticien, ce choix reste humain.

Un horizon est jugé FIABLE si :
  1. `ml/train.py` l'a accepté (assez de collecte continue, PT02 varie, aucune variable figée) ;
  2. son erreur (RMSE) est au moins AUTO_RETRAIN_MIN_GAIN plus faible que la persistance
     (« la température ne bouge pas ») ;
  3. son erreur reste sous AUTO_RETRAIN_MAX_RMSE_CELSIUS.
Toutes les erreurs sont mesurées sur les 20 % de données les PLUS RÉCENTES, jamais vues à
l'entraînement (découpage chronologique de `ml/train.py`).

Fichiers (dans MODEL_DIR/auto_retrain/) :
  candidates/<run>/<horizon>/  modèles candidats de chaque nuit (les 3 dernières nuits gardées)
  proposal.json                proposition en attente (ou acceptée / refusée)
  history.jsonl                une ligne par nuit et par décision humaine
  previous/<date>/             modèles remplacés, pour revenir en arrière (--rollback)
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

import joblib
import numpy as np
import pandas as pd

import config
from database import sqlite_client
from ml.train import build_target, train_and_compare

logger = logging.getLogger(__name__)

RETRAIN_DIRNAME = "auto_retrain"
MODEL_FILES = ("random_forest.joblib", "gradient_boosting.joblib")
REPORT_FILE = "training_report.json"
KEEP_RUNS = 3
# Même horizon que le modèle installé : le candidat doit faire au moins 5 % mieux, sur les
# mêmes données récentes, pour justifier un remplacement.
SAME_HORIZON_MIN_IMPROVEMENT = 0.05
# La période de test (20 % les plus récents) doit couvrir au moins N fois l'horizon : juger une
# prédiction à 24 h sur une seule journée de test ne prouve rien (un seul « cycle » observé).
MIN_TEST_SPAN_HORIZONS = 3
# Au-delà, les mesures instantanées ne suffisent plus à anticiper (charge et météo à venir).
LONG_HORIZON_WARNING_SECONDS = 2 * 3600

# Noms lisibles des variables d'entrée (messages destinés à l'exploitant).
_FEATURE_LABELS = {
    "pt01": "PT01", "pt02": "PT02", "pt03": "PT03", "pt04": "PT04", "ht01": "débit HT01",
    "freq_pompe": "fréquence pompe", "temp_outdoor": "température extérieure",
}

_STATUS_REASONS = {
    "insufficient_data": "données insuffisantes (collecte continue trop courte pour cet horizon)",
    "insufficient_variance": "PT02 ne varie pas assez pour juger une prédiction",
    "no_actionable_signal": "une variable d'entrée est restée figée",
    "no_skill": "ne fait pas mieux que la persistance (« la température ne bouge pas »)",
}


def retrain_root(model_dir: str = config.MODEL_DIR) -> Path:
    return Path(model_dir) / RETRAIN_DIRNAME


def format_duration(seconds: float) -> str:
    """Durée lisible, en jours au-delà de 48 h."""
    return f"{seconds / 86400:.1f} j".replace(".", ",") if seconds >= 2 * 86400 else format_horizon(seconds)


def test_span_seconds(df: pd.DataFrame, horizon: int) -> float:
    """Durée couverte par la période de test de `ml/train.py` (20 % des échantillons les plus récents)."""
    usable = build_target(df, horizon_seconds=horizon).dropna(subset=config.FEATURE_COLUMNS + ["target_future"])
    if len(usable) < 2:
        return 0.0
    test = usable["timestamp"].iloc[int(len(usable) * 0.8):]
    return float((test.iloc[-1] - test.iloc[0]).total_seconds()) if len(test) > 1 else 0.0


def format_horizon(seconds: float) -> str:
    """600 -> « 10 min », 5400 -> « 1 h 30 », 86400 -> « 24 h »."""
    seconds = int(seconds)
    if seconds < 3600:
        return f"{seconds // 60} min" if seconds >= 60 else f"{seconds} s"
    hours, minutes = divmod(seconds // 60, 60)
    return f"{hours} h" + (f" {minutes:02d}" if minutes else "")


def _now_utc() -> datetime:
    return datetime.now(timezone.utc)


def _write_json_atomically(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False, default=str), encoding="utf-8")
    os.replace(tmp, path)


def _append_history(model_dir: str, entry: dict) -> None:
    path = retrain_root(model_dir) / "history.jsonl"
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False, default=str) + "\n")


def load_json(path: Path) -> dict | None:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def read_history(model_dir: str = config.MODEL_DIR, limit: int = 30) -> list[dict]:
    path = retrain_root(model_dir) / "history.jsonl"
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    entries = []
    for line in lines[-limit:]:
        try:
            entries.append(json.loads(line))
        except ValueError:
            continue
    return list(reversed(entries))


# ---------------------------------------------------------------------------
# Jugement d'un entraînement
# ---------------------------------------------------------------------------
def judge(report: dict) -> tuple[bool, str]:
    """(fiable ?, raison en français) pour un rapport de `train_and_compare`."""
    status = report.get("status")
    if status == "no_actionable_signal" and report.get("degenerate_features"):
        names = ", ".join(_FEATURE_LABELS.get(f, f) for f in report["degenerate_features"])
        return False, f"variable(s) restée(s) figée(s) sur toute la période : {names}"
    if status != "ok":
        return False, _STATUS_REASONS.get(str(status), f"statut {status}")
    best = report.get("best_model")
    rmse = (report.get(best) or {}).get("rmse")
    baseline = (report.get("baseline_persistence") or {}).get("rmse")
    if rmse is None or baseline is None:
        return False, "rapport incomplet"
    gain = 1.0 - rmse / baseline if baseline > 0 else 0.0
    if gain < config.AUTO_RETRAIN_MIN_GAIN:
        return False, (f"pas assez meilleur que la persistance ({gain:+.0%}, "
                       f"minimum {config.AUTO_RETRAIN_MIN_GAIN:.0%})")
    if rmse > config.AUTO_RETRAIN_MAX_RMSE_CELSIUS:
        return False, f"erreur trop grande ({rmse:.2f} °C > {config.AUTO_RETRAIN_MAX_RMSE_CELSIUS:g} °C)"
    return True, f"fiable : erreur {rmse:.2f} °C, {gain:.0%} de mieux que la persistance"


def _candidate_summary(horizon: int, report: dict, model_dir: Path, root: Path) -> dict:
    eligible, reason = judge(report)
    best = report.get("best_model")
    rmse = (report.get(best) or {}).get("rmse") if best else None
    baseline = (report.get("baseline_persistence") or {}).get("rmse")
    return {
        "horizon_seconds": horizon,
        "status": report.get("status"),
        "eligible": eligible,
        "reason": reason,
        "best_model": best,
        "rmse": rmse,
        "mae": (report.get(best) or {}).get("mae") if best else None,
        "persistence_rmse": baseline,
        "gain": None if rmse is None or not baseline else 1.0 - rmse / baseline,
        "n_samples": report.get("n_samples"),
        "model_dir": str(model_dir.relative_to(root)) if eligible else None,
    }


# ---------------------------------------------------------------------------
# Modèle installé : performance sur les données récentes
# ---------------------------------------------------------------------------
def installed_model(model_dir: str = config.MODEL_DIR) -> dict | None:
    report = load_json(Path(model_dir) / REPORT_FILE)
    if not report or report.get("status") != "ok":
        return None
    return report


def evaluate_installed(df: pd.DataFrame, model_dir: str = config.MODEL_DIR) -> dict | None:
    """Erreur du modèle INSTALLÉ sur les 20 % de données les plus récentes (même fenêtre que
    celle qui sert à tester les candidats de cet horizon). None si impossible."""
    report = installed_model(model_dir)
    if report is None:
        return None
    if report.get("features") != list(config.FEATURE_COLUMNS):
        return None  # entraîné avec d'autres variables : inutilisable, donc « aucun modèle fiable »
    horizon = int(report.get("horizon_seconds", 600))
    best = report.get("best_model", "random_forest")
    try:
        model = joblib.load(Path(model_dir) / f"{best}.joblib")
    except Exception:
        logger.exception("Modèle installé illisible : %s", best)
        return None
    usable = build_target(df, horizon_seconds=horizon).dropna(
        subset=config.FEATURE_COLUMNS + ["target_future"])
    split_idx = int(len(usable) * 0.8)
    test = usable.iloc[split_idx:]
    if len(test) < 5:
        return None
    predictions = model.predict(test[config.FEATURE_COLUMNS].to_numpy())
    truth = test["target_future"].to_numpy()
    rmse = float(np.sqrt(np.mean((predictions - truth) ** 2)))
    baseline = float(np.sqrt(np.mean((test[config.TARGET_COLUMN].to_numpy() - truth) ** 2)))
    gain = 1.0 - rmse / baseline if baseline > 0 else 0.0
    return {
        "horizon_seconds": horizon, "best_model": best, "rmse": rmse, "persistence_rmse": baseline,
        "gain": gain, "n_test": len(test), "trained_at": report.get("trained_at"),
        "still_reliable": gain >= config.AUTO_RETRAIN_MIN_GAIN and rmse <= config.AUTO_RETRAIN_MAX_RMSE_CELSIUS,
    }


# ---------------------------------------------------------------------------
# Décision
# ---------------------------------------------------------------------------
def decide(candidates: list[dict], current: dict | None) -> tuple[dict | None, str]:
    """Choisit le candidat à proposer (ou aucun) et explique pourquoi."""
    eligible = [c for c in candidates if c["eligible"]]
    if not eligible:
        return None, "aucun horizon ne remplit les critères de fiabilité"
    best = max(eligible, key=lambda c: c["horizon_seconds"])
    label = format_horizon(best["horizon_seconds"])
    if current is None:
        return best, f"aucun modèle fiable installé : proposition d'un modèle à {label}"
    current_label = format_horizon(current["horizon_seconds"])
    if best["horizon_seconds"] > current["horizon_seconds"]:
        return best, f"un horizon plus long est devenu fiable : {label} au lieu de {current_label}"
    if best["horizon_seconds"] == current["horizon_seconds"]:
        if best["rmse"] < current["rmse"] * (1 - SAME_HORIZON_MIN_IMPROVEMENT):
            return best, (f"même horizon ({label}), erreur plus faible sur les données récentes : "
                          f"{best['rmse']:.2f} °C au lieu de {current['rmse']:.2f} °C")
        return None, f"le modèle installé ({current_label}) reste au moins aussi bon"
    if not current["still_reliable"]:
        return best, (f"le modèle installé ({current_label}) ne remplit plus les critères sur les "
                      f"données récentes : retour à {label}")
    return None, f"le modèle installé ({current_label}) reste fiable et prédit plus loin"


def _warnings(horizon: int) -> list[str]:
    warnings = []
    ref = config.PID_FEEDFORWARD_REFERENCE_HORIZON_SECONDS
    if horizon != ref:
        warnings.append(
            f"L'anticipation du PID (+{config.FEEDFORWARD_PREDICTED_BOOST:g} % si la prédiction dépasse la "
            f"cible de {config.FEEDFORWARD_PREDICTED_OVERSHOOT_THRESHOLD:g} °C) a été pensée pour une "
            f"prédiction à {format_horizon(ref)}. À {format_horizon(horizon)}, elle se déclenchera "
            f"{'plus tôt' if horizon > ref else 'plus tard'} : à revoir avec l'automaticien."
        )
    if horizon > LONG_HORIZON_WARNING_SECONDS:
        warnings.append(
            "Prédiction lointaine faite uniquement à partir des mesures actuelles : elle ne connaît ni "
            "la charge à venir ni la météo prévue. Surveiller son erreur réelle (onglet Modèles)."
        )
    return warnings


def _cleanup(root: Path, keep: set[str]) -> None:
    runs_dir = root / "candidates"
    if not runs_dir.is_dir():
        return
    runs = sorted((p for p in runs_dir.iterdir() if p.is_dir()), key=lambda p: p.name, reverse=True)
    for run in runs[KEEP_RUNS:]:
        if run.name not in keep:
            shutil.rmtree(run, ignore_errors=True)


class RetrainBusy(RuntimeError):
    """Un ré-entraînement tourne déjà (nocturne ou manuel)."""


@contextmanager
def _exclusive(root: Path) -> Iterator[None]:
    """Un seul ré-entraînement à la fois : deux exécutions concurrentes écriraient la même
    proposition. Sans fcntl (Windows), pas de verrou."""
    root.mkdir(parents=True, exist_ok=True)
    # Les modèles sont des pickles : seul le propriétaire doit pouvoir écrire dans ces dossiers.
    for folder in (root.parent, root):
        os.chmod(folder, 0o700)
    with open(root / ".lock", "w") as handle:
        try:
            import fcntl

            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass
        except OSError:
            raise RetrainBusy("un ré-entraînement est déjà en cours.") from None
        yield


def run_auto_retrain(db_path: str = config.SQLITE_DB_PATH, model_dir: str = config.MODEL_DIR,
                     horizons: tuple[int, ...] | None = None, now: datetime | None = None) -> dict:
    """Une nuit de ré-entraînement : entraîne, juge, compare, propose. Retourne le résumé.
    Lève RetrainBusy si un autre ré-entraînement tourne déjà."""
    with _exclusive(retrain_root(model_dir)):
        return _run_auto_retrain(db_path, model_dir, horizons, now)


def _run_auto_retrain(db_path: str, model_dir: str, horizons: tuple[int, ...] | None,
                      now: datetime | None) -> dict:
    now = now or _now_utc()
    horizons = tuple(sorted(set(horizons or config.AUTO_RETRAIN_HORIZONS_SECONDS)))
    root = retrain_root(model_dir)
    run_id = now.strftime("%Y%m%dT%H%M%SZ")
    run_dir = root / "candidates" / run_id
    started = time.time()

    start_ts = (now - pd.Timedelta(days=config.AUTO_RETRAIN_MAX_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    df = sqlite_client.get_dataframe(start_ts, now.strftime("%Y-%m-%d %H:%M:%S"), db_path=db_path)
    logger.info("Ré-entraînement %s : %d mesures depuis %s, horizons %s.", run_id, len(df), start_ts,
                ", ".join(format_horizon(h) for h in horizons))

    candidates = []
    for horizon in horizons:
        target = run_dir / str(horizon)
        try:
            report = train_and_compare(df, horizon_seconds=horizon, model_dir=str(target))
        except Exception as exc:  # un horizon en échec n'empêche pas les autres
            logger.exception("Entraînement à %s impossible", format_horizon(horizon))
            report = {"status": "error", "message": str(exc)}
        candidate = _candidate_summary(horizon, report, target, root)
        if candidate["eligible"]:
            span = test_span_seconds(df, horizon)
            candidate["test_span_seconds"] = span
            if span < MIN_TEST_SPAN_HORIZONS * horizon:
                candidate.update(eligible=False, model_dir=None, reason=(
                    f"période de test trop courte ({format_duration(span)}) pour juger {format_horizon(horizon)} : "
                    f"il en faut au moins {MIN_TEST_SPAN_HORIZONS} fois l'horizon "
                    f"({format_duration(MIN_TEST_SPAN_HORIZONS * horizon)}), soit environ "
                    f"{format_duration(MIN_TEST_SPAN_HORIZONS * horizon / 0.2)} de données"))
        if not candidate["eligible"]:
            shutil.rmtree(target, ignore_errors=True)  # ne garder que les candidats utilisables
        candidates.append(candidate)
        logger.info("  %s : %s", format_horizon(horizon), candidate["reason"])

    current = evaluate_installed(df, model_dir) if len(df) else None
    chosen, reason = decide(candidates, current)
    previous = load_json(root / "proposal.json")

    proposal = None
    if chosen is not None:
        proposal = {
            "status": "pending",
            "created_at": now.isoformat(),
            "run_id": run_id,
            "proposed": chosen,
            "current": current,
            "reason": reason,
            "warnings": _warnings(chosen["horizon_seconds"]),
        }
        _write_json_atomically(root / "proposal.json", proposal)
    elif previous and previous.get("status") == "pending":
        previous.update(status="withdrawn", withdrawn_at=now.isoformat(),
                        withdrawn_reason=f"retirée par la nuit {run_id} : {reason}")
        _write_json_atomically(root / "proposal.json", previous)

    summary: dict = {
        "event": "run",
        "run_id": run_id,
        "started_at": now.isoformat(),
        "duration_seconds": round(time.time() - started, 1),
        "rows": len(df),
        "data_from": None if df.empty else str(df["timestamp"].min()),
        "data_to": None if df.empty else str(df["timestamp"].max()),
        "candidates": candidates,
        "current": current,
        "decision": "proposed" if proposal else "none",
        "proposed_horizon_seconds": chosen["horizon_seconds"] if chosen else None,
        "reason": reason,
    }
    _append_history(model_dir, summary)
    keep = {run_id}
    pending = proposal or (previous if previous and previous.get("status") == "pending" else None)
    if pending:
        keep.add(str(pending["run_id"]))
    _cleanup(root, keep)
    logger.info("Décision : %s (%s).", summary["decision"], reason)
    return summary


# ---------------------------------------------------------------------------
# Décisions humaines (./voltaneum model)
# ---------------------------------------------------------------------------
class ProposalError(RuntimeError):
    """Action impossible : message lisible pour l'opérateur."""


def pending_proposal(model_dir: str = config.MODEL_DIR) -> dict:
    proposal = load_json(retrain_root(model_dir) / "proposal.json")
    if not proposal or proposal.get("status") != "pending":
        raise ProposalError("aucune proposition en attente.")
    return proposal


def _copy_model_files(source: Path, destination: Path) -> None:
    """Copie les modèles puis le rapport EN DERNIER, chacun de façon atomique : la régulation recharge
    les modèles quand le rapport change, il ne voit donc jamais un mélange ancien / nouveau."""
    destination.mkdir(parents=True, exist_ok=True)
    os.chmod(destination, 0o700)  # modèles = pickles : personne d'autre ne doit pouvoir les remplacer
    for name in (*MODEL_FILES, REPORT_FILE):
        tmp = destination / f".{name}.tmp"
        shutil.copy2(source / name, tmp)
        os.replace(tmp, destination / name)


def accept_proposal(model_dir: str = config.MODEL_DIR, operator: str = "") -> dict:
    """Installe le modèle proposé dans MODEL_DIR (l'ancien est sauvegardé dans previous/)."""
    proposal = pending_proposal(model_dir)
    root = retrain_root(model_dir)
    source = root / proposal["proposed"]["model_dir"]
    missing = [n for n in (*MODEL_FILES, REPORT_FILE) if not (source / n).is_file()]
    if missing:
        raise ProposalError(f"fichiers du modèle proposé introuvables dans {source} : {', '.join(missing)}.")
    report = load_json(source / REPORT_FILE)
    if not report or report.get("status") != "ok":
        raise ProposalError("le rapport du modèle proposé n'a pas le statut « ok » : installation refusée.")

    now = _now_utc()
    backup = None
    model_path = Path(model_dir)
    if all((model_path / n).is_file() for n in (*MODEL_FILES, REPORT_FILE)):
        backup = root / "previous" / now.strftime("%Y%m%dT%H%M%SZ")
        backup.mkdir(parents=True, exist_ok=True)
        for name in (*MODEL_FILES, REPORT_FILE):
            shutil.copy2(model_path / name, backup / name)
    _copy_model_files(source, model_path)

    proposal.update(status="accepted", decided_at=now.isoformat(), operator=operator,
                    backup=None if backup is None else str(backup.relative_to(root)))
    _write_json_atomically(root / "proposal.json", proposal)
    _append_history(model_dir, {"event": "accepted", "at": now.isoformat(), "operator": operator,
                                "run_id": proposal["run_id"],
                                "horizon_seconds": proposal["proposed"]["horizon_seconds"]})
    return proposal


def reject_proposal(model_dir: str = config.MODEL_DIR, operator: str = "", note: str = "") -> dict:
    proposal = pending_proposal(model_dir)
    now = _now_utc()
    proposal.update(status="rejected", decided_at=now.isoformat(), operator=operator, note=note)
    _write_json_atomically(retrain_root(model_dir) / "proposal.json", proposal)
    _append_history(model_dir, {"event": "rejected", "at": now.isoformat(), "operator": operator,
                                "run_id": proposal["run_id"], "note": note,
                                "horizon_seconds": proposal["proposed"]["horizon_seconds"]})
    return proposal


def rollback(model_dir: str = config.MODEL_DIR, operator: str = "") -> dict:
    """Réinstalle le dernier modèle remplacé (sauvegardé lors d'un --accept)."""
    previous_dir = retrain_root(model_dir) / "previous"
    backups = sorted((p for p in previous_dir.iterdir() if p.is_dir()), reverse=True) if previous_dir.is_dir() else []
    if not backups:
        raise ProposalError("aucun modèle précédent sauvegardé.")
    latest = backups[0]
    report = load_json(latest / REPORT_FILE) or {}
    _copy_model_files(latest, Path(model_dir))
    shutil.rmtree(latest)
    now = _now_utc()
    _append_history(model_dir, {"event": "rollback", "at": now.isoformat(), "operator": operator,
                                "horizon_seconds": report.get("horizon_seconds")})
    return report
