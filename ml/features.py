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
# Entrées complètes des modèles : mesures actuelles puis tendances (ordre fixe, enregistré), moins
# celles écartées par config.ML_EXCLUDED_FEATURES (la température extérieure par défaut).
def model_features(excluded: tuple[str, ...] = config.ML_EXCLUDED_FEATURES) -> list[str]:
    return [f for f in list(config.FEATURE_COLUMNS) + TREND_FEATURES if f not in excluded]


MODEL_FEATURES: list[str] = model_features()

# PT02 ESTIMÉE (config.ML_TARGET_ESTIMATE) : PT01 + moyenne glissante de (PT02 − PT01) sur cette
# fenêtre, calculée seulement si elle contient au moins ESTIMATE_MIN_SAMPLES mesures.
ESTIMATE_WINDOW_SECONDS: int = 600
ESTIMATE_MIN_SAMPLES: int = 20
# Ce que les modèles prédisent, choisi à chaque entraînement (ml/train.py) :
#   - "delta"    : l'ÉCART entre la valeur future et la valeur actuelle de la grandeur surveillée
#                  (prédiction = valeur actuelle + écart prédit) ;
#   - "absolute" : la valeur future elle-même.
TARGET_MODES: tuple[str, ...] = ("delta", "absolute")


def with_estimated_target(df: pd.DataFrame) -> pd.DataFrame:
    """Copie de `df` (trié par `timestamp`) où la grandeur surveillée (PT02) est remplacée par son
    ESTIMATION au dixième de degré :

        PT02 estimée(t) = PT01(t) + moyenne de (PT02 − PT01) sur [t − 10 min, t]

    Pourquoi : PT02 est lue au degré près ; ses variations réelles de quelques dixièmes sont
    invisibles, et un modèle ne peut pas les prévoir. PT01 (sortie de la CDU) est lue au dixième et
    réagit en 30 s au ventilateur ; l'échauffement apporté par les équipements (PT02 − PT01) varie
    lentement avec la charge, et sa moyenne sur 10 min lisse la lecture au degré. La somme donne un
    signal au dixième qui respecte le bilan de l'armoire (PT02 = PT01 + échauffement). Limite : un
    changement brusque de charge n'apparaît dans l'échauffement moyen qu'avec ~10 min de retard.

    Le même calcul sert à l'entraînement, à l'évaluation du modèle installé et en service
    (`estimate_from_history`) : jamais deux définitions différentes. Moins de ESTIMATE_MIN_SAMPLES
    mesures dans la fenêtre (début de série, après un trou) : valeur absente, ligne écartée."""
    target, cold = config.TARGET_COLUMN, config.DIELECTRIC_COLD_COLUMN
    out = df.copy()
    times = pd.to_datetime(out["timestamp"])
    heating = pd.Series((pd.to_numeric(out[target], errors="coerce")
                         - pd.to_numeric(out[cold], errors="coerce")).to_numpy(), index=times)
    mean = heating.rolling(f"{ESTIMATE_WINDOW_SECONDS}s", min_periods=ESTIMATE_MIN_SAMPLES).mean()
    out[target] = pd.to_numeric(out[cold], errors="coerce").to_numpy() + mean.to_numpy()
    return out


def estimate_from_history(target_points: list[tuple[float, float]], cold_points: list[tuple[float, float]],
                          now: float, cold_now: float | None) -> tuple[float | None, list[tuple[float, float]]]:
    """En service : PT02 estimée MAINTENANT et son historique récent, à partir des dernières mesures
    en base ([(instant epoch, valeur)] de PT02 et de PT01) et de la valeur actuelle de PT01. Même
    définition que `with_estimated_target`. (None, []) si l'historique est trop court."""
    if cold_now is None or not target_points or not cold_points:
        return None, []
    rows = pd.DataFrame(target_points, columns=["t", config.TARGET_COLUMN]).merge(
        pd.DataFrame(cold_points, columns=["t", config.DIELECTRIC_COLD_COLUMN]), on="t").sort_values("t")
    if rows.empty:
        return None, []
    rows["timestamp"] = pd.to_datetime(rows["t"], unit="s")
    estimated = with_estimated_target(rows)
    history = [(float(t), float(v)) for t, v in zip(estimated["t"], estimated[config.TARGET_COLUMN])
               if pd.notna(v)]
    recent = rows[rows["t"] > now - ESTIMATE_WINDOW_SECONDS]      # ]now − 10 min, now], comme rolling()
    if len(recent) < ESTIMATE_MIN_SAMPLES:
        return None, history
    heating = (recent[config.TARGET_COLUMN] - recent[config.DIELECTRIC_COLD_COLUMN]).mean()
    return float(cold_now) + float(heating), history


def watched_state(target_points: list[tuple[float, float]], cold_points: list[tuple[float, float]],
                  now: float, cold_now: float | None, window_seconds: float) -> dict:
    """État de la grandeur surveillée pour la RÉGULATION et l'AFFICHAGE (même calcul aux deux
    endroits) : PT02 estimée maintenant (ce que prédit le modèle, pour comparer prédiction et
    présent) et la tendance (°C / 10 min) utilisée par la régulation par étapes.

    La tendance est la pente des moindres carrés (control/trend.py) de la PT02 MESURÉE, pas de PT02
    estimée. Décision prise sur les relevés réels (14 252 minutes, 12 au 22/10/2026) : la pente de
    PT02 estimée suit les cycles du ventilateur (PT01 oscille de 0,4 °C toutes les 5 min) ; elle est
    deux fois plus agitée d'une minute à l'autre et déclenche à tort « ça monte » 7,9 % du temps
    (contre 4,8 %) et « ça refroidit » 14,6 % (contre 6,1 %). Pour une décision qui attend 10 min
    l'effet d'un changement, ce sont des faux signaux. PT02 estimée garde son intérêt pour la
    prédiction à 10 min, où ces variations rapides sont justement l'information utile.
    Les historiques sont attendus SANS pics isolés (sqlite_client.recent_values les retire)."""
    from control.trend import slope_per_10min

    estimate, _ = estimate_from_history(target_points, cold_points, now, cold_now)
    slope = slope_per_10min([p for p in target_points if p[0] > now - window_seconds], window_seconds)
    return {"estimate": estimate, "slope": slope}


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
