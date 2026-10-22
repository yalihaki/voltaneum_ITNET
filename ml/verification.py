"""ml/verification.py — Vérification des prédictions à leur échéance (`./voltaneum predictions`).

Question à laquelle ce module répond : « à 14:00, le modèle a prédit PT02 à 34,9 °C pour 14:15 ;
que valait PT02 à 14:15 ? ». Chaque prédiction est enregistrée (predictions_log) avec son échéance ;
quand l'échéance arrive, le moniteur des modèles (ml/monitor.py) la complète avec :

  - la PT02 ENVOYÉE PAR L'AUTOMATE à l'échéance (`measured_value`, au degré près) ;
  - la PT02 de RÉFÉRENCE (`actual_value`) sur laquelle l'erreur est calculée : PT02 estimée au
    dixième si le modèle la prédit (config.ML_TARGET_ESTIMATE), sinon la mesure.

Pour juger honnêtement, le rapport compare aussi le modèle à la PERSISTANCE : « dans 15 min, ce
sera la même valeur qu'au moment de la prédiction ». Un modèle utile se trompe moins qu'elle.
"""

from __future__ import annotations

from datetime import datetime, timezone

import config
from dashboard import data
from database import sqlite_client


def _local(utc_text: str) -> str:
    moment = datetime.strptime(utc_text, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
    return moment.astimezone().strftime("%d/%m %H:%M")


def _mean(values: list[float]) -> float | None:
    return sum(values) / len(values) if values else None


def prediction_report(db_path: str, hours: float = 24, rows: int = 20, model: str | None = None) -> str:
    """Rapport texte des prédictions échues sur les `hours` dernières heures, pour `model` (par
    défaut le meilleur modèle du modèle installé, sinon celui qui a le plus de prédictions)."""
    since = datetime.now(timezone.utc).timestamp() - hours * 3600
    with data.connect_readonly(db_path) as conn:
        if not data._table_exists(conn, "predictions_log"):
            return "Aucune prédiction enregistrée dans cette base."
        columns = {row[1] for row in conn.execute("PRAGMA table_info(predictions_log)")}
        measured_col = "measured_value" if "measured_value" in columns else "NULL"
        if model is None:
            from ml import auto_retrain
            installed = auto_retrain.installed_model(config.MODEL_DIR) or {}
            model = installed.get("best_model")
        if model is None:
            row = conn.execute("SELECT model_name, COUNT(*) FROM predictions_log GROUP BY model_name "
                               "ORDER BY 2 DESC LIMIT 1").fetchone()
            model = row[0] if row else None
        if model is None:
            return "Aucune prédiction enregistrée dans cette base."
        done = conn.execute(
            f"SELECT made_at, target_ts, horizon_seconds, predicted_value, actual_value, {measured_col} "  # nosec B608
            "FROM predictions_log WHERE model_name = ? AND made_at >= ? AND actual_value IS NOT NULL "
            "ORDER BY made_at",
            (model, data._utc_text(since)),
        ).fetchall()
        pending = conn.execute(
            "SELECT COUNT(*) FROM predictions_log WHERE model_name = ? AND actual_value IS NULL AND made_at >= ?",
            (model, data._utc_text(since)),
        ).fetchone()[0]
        # Persistance : la valeur de référence AU MOMENT de la prédiction, comparée à l'échéance.
        at_made = {made: sqlite_client._actual_at(conn, made) for made, *_ in done}

    if not done:
        return (f"Aucune prédiction échue sur les {hours:g} dernières heures pour {model} "
                f"({pending} en attente de leur échéance).")
    reference = "PT02 estimée" if config.ML_TARGET_ESTIMATE else "PT02 mesurée"
    model_err = [abs(p - a) for _, _, _, p, a, _ in done]
    plc_err = [abs(p - m) for _, _, _, p, _, m in done if m is not None]
    persist_err = [abs(then[0] - a) for made, _, _, _, a, _ in done if (then := at_made.get(made)) is not None]
    horizon = done[-1][2]
    lines = [
        f"Prédictions de {model} à {horizon // 60} min, échues sur les {hours:g} dernières heures : "
        f"{len(done)} vérifiée(s), {pending} en attente.",
        f"Erreur jugée sur la {reference} (la grandeur que le modèle prédit) ; « automate » = PT02 envoyée "
        "par l'automate à l'échéance, au degré près.",
        "",
        f"{'prédite à':<12} {'pour':<12} {'prédite':>8} {'référence':>10} {'automate':>9} {'erreur':>8}",
    ]
    for made, target, _, predicted, actual, measured in done[-rows:]:
        plc = "—" if measured is None else f"{measured:.1f}"
        lines.append(f"{_local(made):<12} {_local(target):<12} {predicted:>8.2f} {actual:>10.2f} {plc:>9} "
                     f"{predicted - actual:>+8.2f}")
    lines += ["", "Bilan sur la période :"]
    mean_model, mean_persist = _mean(model_err), _mean(persist_err)
    lines.append(f"  erreur moyenne du modèle (vs {reference}) : {mean_model:.2f} °C")
    if mean_persist and mean_model is not None:
        verdict = "le modèle fait MIEUX" if mean_model < mean_persist else "le modèle ne fait PAS mieux"
        lines.append(f"  erreur moyenne de « rien ne change » (persistance) : {mean_persist:.2f} °C -> {verdict} "
                     f"({100 * (1 - mean_model / mean_persist):+.0f} %)")
    if plc_err:
        within = sum(e <= 0.5 for e in plc_err) / len(plc_err)
        lines.append(f"  écart moyen à la PT02 de l'automate : {_mean(plc_err):.2f} °C ; "
                     f"à ±0,5 °C près (l'automate arrondit au degré) : {within:.0%} des cas")
    return "\n".join(lines)
