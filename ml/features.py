"""ml/features.py — Entrées de tendance des modèles (calcul commun entraînement / prédiction).

En clair : une mesure seule est une photo ; le modèle doit aussi savoir si la température est en
train de monter ou de descendre. Le ventilateur du dry cooler n'est pas mesuré, mais ses marches
et arrêts se voient très bien sur la tendance de l'air entrant (PT04 perd ou reprend 3 °C en
quelques dizaines de secondes) et du diélectrique. Pour chaque sonde de TREND_COLUMNS et chaque
fenêtre de TREND_WINDOWS_SECONDS, l'entrée vaut « valeur maintenant − valeur il y a N minutes ».

Le MÊME calcul sert à l'entraînement (sur l'historique, `add_trend_features`) et en service (sur
les dernières mesures en base, `trend_features_at`) : un modèle ne doit jamais recevoir en
production une entrée calculée autrement qu'à l'apprentissage.
"""

from __future__ import annotations

import pandas as pd

import config

TREND_COLUMNS: tuple[str, ...] = ("pt01", "pt02", "pt04")
TREND_WINDOWS_SECONDS: tuple[int, ...] = (300, 600)
# Écart toléré entre l'instant visé (maintenant − N min) et la mesure retenue (s) : un intervalle
# entre instantanés (la mesure la plus proche en est toujours à moins d'un demi-intervalle).
TREND_TOLERANCE_SECONDS: float = float(config.SNAPSHOT_INTERVAL_SECONDS)


def trend_name(column: str, window_seconds: int) -> str:
    return f"{column}_d{window_seconds // 60}min"


TREND_FEATURES: list[str] = [trend_name(c, w) for c in TREND_COLUMNS for w in TREND_WINDOWS_SECONDS]
# Entrées complètes des modèles : mesures actuelles puis tendances (ordre fixe, enregistré).
MODEL_FEATURES: list[str] = list(config.FEATURE_COLUMNS) + TREND_FEATURES
# Ce que les modèles prédisent, choisi à chaque entraînement (ml/train.py) :
#   - "delta"    : l'ÉCART entre la valeur future et la valeur actuelle de la grandeur surveillée
#                  (prédiction = valeur actuelle + écart prédit) ;
#   - "absolute" : la valeur future elle-même.
TARGET_MODES: tuple[str, ...] = ("delta", "absolute")


def add_trend_features(df: pd.DataFrame) -> pd.DataFrame:
    """Ajoute les colonnes TREND_FEATURES à un historique trié (colonne `timestamp`).

    La valeur passée est la mesure réellement relevée la plus proche de t − N min (à
    ±TREND_TOLERANCE_SECONDS), jamais à travers un trou de collecte : sinon NaN (ligne écartée).
    """
    out = df.copy()
    out["timestamp"] = pd.to_datetime(out["timestamp"])
    out = out.sort_values("timestamp").reset_index(drop=True)
    gaps = out["timestamp"].diff().dt.total_seconds()
    out["_segment_id"] = (gaps > config.MAX_GAP_SECONDS).cumsum()
    for column in TREND_COLUMNS:
        if column not in out.columns:
            for window in TREND_WINDOWS_SECONDS:
                out[trend_name(column, window)] = float("nan")
            continue
        past_source = out[["_segment_id", "timestamp", column]].dropna(subset=[column])
        for window in TREND_WINDOWS_SECONDS:
            past = past_source.rename(columns={column: "_past"}).copy()
            # Une mesure prise à s sert de « valeur passée » à l'instant s + N min.
            past["timestamp"] = past["timestamp"] + pd.to_timedelta(window, unit="s")
            merged = pd.merge_asof(
                out[["_segment_id", "timestamp"]].reset_index(),
                past.sort_values("timestamp"),
                on="timestamp", by="_segment_id", direction="nearest",
                tolerance=pd.to_timedelta(TREND_TOLERANCE_SECONDS, unit="s"),
            ).set_index("index").sort_index()
            out[trend_name(column, window)] = out[column] - merged["_past"]
    return out.drop(columns=["_segment_id"])


def trend_features_at(history: dict[str, list[tuple[float, float]]], now: float,
                      current: dict[str, float | None]) -> dict[str, float | None]:
    """Entrées de tendance à l'instant `now` (epoch s), en service.

    `history` : colonne -> [(instant, valeur)] des dernières mesures en base ; `current` : valeurs
    actuelles. Une tendance sans mesure assez proche de now − N min vaut None (pas de prédiction).
    """
    features: dict[str, float | None] = {}
    for column in TREND_COLUMNS:
        points = history.get(column) or []
        value_now = current.get(column)
        for window in TREND_WINDOWS_SECONDS:
            name = trend_name(column, window)
            target = now - window
            nearest = min(points, key=lambda p: abs(p[0] - target), default=None)
            if value_now is None or nearest is None or abs(nearest[0] - target) > TREND_TOLERANCE_SECONDS:
                features[name] = None
            else:
                features[name] = float(value_now) - float(nearest[1])
    return features
