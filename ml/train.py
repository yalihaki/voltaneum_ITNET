"""
ml/train.py — Entraînement et comparaison de modèles de prédiction de température.

En clair : ce module répond à la question "peut-on prédire la température
PT02 dans 10 minutes à partir des valeurs actuelles des capteurs et de leur
tendance récente ?". Deux façons d'apprendre sont essayées : l'ÉCART à venir
(valeur future − valeur actuelle : la prédiction part de la persistance et
n'apprend que la variation) ou la valeur future elle-même. La meilleure est
choisie sur une période de validation prise dans l'apprentissage, jamais sur
la période de test qui sert à juger le modèle (voir ml/features.py).
Pour y répondre honnêtement, on ne se contente pas d'entraîner un modèle et
de croire son score : on le compare à une référence bête et méchante — la
"persistance", qui prédit simplement que la température dans 10 minutes
sera... la même qu'actuellement. Si nos modèles de Machine Learning ne
battent pas cette référence naïve, c'est qu'ils n'apprennent rien d'utile.

Règle non négociable n°5 du brief : ne jamais produire silencieusement un
modèle ou une métrique trompeuse. Concrètement, avant même d'entraîner quoi
que ce soit, on vérifie qu'il existe assez de données collectées SANS
COUPURE pour couvrir l'horizon de prédiction visé. Sinon, on le dit
clairement (message + rapport JSON explicite) plutôt que d'entraîner un
modèle sur 3 points de données qui aurait l'air valide.
"""

from __future__ import annotations

import json
import os
import logging
import time
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor, RandomForestRegressor
from sklearn.metrics import mean_absolute_error, mean_squared_error

import config
from database import sqlite_client
from database.quality import drop_spikes
from ml.features import MODEL_FEATURES, TARGET_MODES, add_trend_features, with_estimated_target

logger = logging.getLogger(__name__)

# Nombre minimum d'échantillons exploitables après construction de la cible,
# en dessous duquel on refuse d'entraîner un modèle même si un segment assez
# long existe (un segment long mais avec très peu de points reste fragile).
MIN_SAMPLES_FOR_TRAINING = 20

# Écart-type minimal de la cible (°C) en dessous duquel le signal à prédire
# est jugé trop plat pour qu'un score de prédiction soit interprétable. Sans
# ce garde-fou, un modèle peut afficher un RMSE proche de 0 simplement parce
# que la température n'a presque pas bougé sur la période — pas parce qu'il
# a appris quoi que ce soit d'utile. Valeur calée sur ~3x la résolution
# typique des sondes PT (0.1°C) : en dessous, on ne distingue plus un vrai
# signal du bruit de mesure.
MIN_TARGET_STD_CELSIUS = 0.3

# Seuil (quasi nul) en dessous duquel une variable d'entrée est considérée
# comme n'ayant JAMAIS varié sur la période de collecte : elle n'apporte
# aucune information et le modèle ne peut pas avoir appris son effet.
#
# Une telle variable est ÉCARTÉE des entrées du modèle (et sa valeur constante
# mémorisée), au lieu de faire refuser tout entraînement. Sur l'installation,
# la pompe tourne à fréquence fixe (30 Hz en permanence) : l'ancienne règle
# « une variable figée => aucun modèle » bloquait donc l'apprentissage pour
# toujours. Le modèle appris décrit le procédé DANS ces conditions ; si un
# jour la variable s'écarte de sa valeur constante, ml/predictor.py déclare la
# situation « hors domaine » et la régulation ignore la prédiction.
#
# Volontairement TRÈS proche de zéro (pas un seuil "métier" par capteur) : une
# variable avec un bruit de mesure normal, même faible, n'est jamais écartée
# (voir tests/test_train.py, bruit réaliste sur ht01).
FEATURE_DEGENERACY_STD_EPSILON = 1e-6


def find_longest_continuous_segment(
    df: pd.DataFrame, max_gap_seconds: int = config.MAX_GAP_SECONDS
) -> tuple[pd.Timestamp | None, pd.Timestamp | None, float]:
    """Trouve le plus long segment de collecte sans coupure > max_gap_seconds.

    Retourne (début, fin, durée_en_secondes) du plus long segment. Si le
    DataFrame contient moins de 2 lignes, retourne (None, None, 0.0).

    En clair : on regarde la suite des timestamps et on repère les endroits
    où l'écart entre deux mesures consécutives est anormalement grand (plus
    de max_gap_seconds) — un peu comme repérer les trous dans un emploi du
    temps. Chaque "morceau" entre deux trous est un segment continu ; on
    garde le plus long.
    """
    if len(df) < 2:
        return None, None, 0.0

    timestamps = pd.to_datetime(df["timestamp"]).sort_values().reset_index(drop=True)
    gaps = timestamps.diff().dt.total_seconds()

    # Un nouveau segment commence à chaque fois que le trou dépasse le seuil.
    segment_id = (gaps > max_gap_seconds).cumsum()

    best_start, best_end, best_duration = None, None, 0.0
    for _, group in timestamps.groupby(segment_id):
        duration = (group.iloc[-1] - group.iloc[0]).total_seconds()
        if duration > best_duration:
            best_duration = duration
            best_start = group.iloc[0]
            best_end = group.iloc[-1]

    return best_start, best_end, best_duration


def build_target(
    df: pd.DataFrame,
    horizon_seconds: int = max(config.PREDICTION_HORIZONS_SECONDS),
    tolerance_seconds: int = config.TARGET_MERGE_TOLERANCE_SECONDS,) -> pd.DataFrame:
    
    """Construit la cible "température dans horizon_seconds" par rapprochement
    d'horodatage réel (merge_asof), PAS par décalage naïf de lignes (shift()).

    En clair : `shift()` suppose que les lignes sont régulièrement espacées
    (ex. toujours 15 secondes d'écart) et se contente de "prendre la ligne
    20 plus loin". Mais si la collecte a des trous (réseau coupé, passerelle
    injoignable...), la ligne "20 plus loin" peut en réalité correspondre à
    un moment bien plus tardif que prévu — ce serait mentir sur ce qu'on
    prédit vraiment. `merge_asof` regarde les VRAIS horodatages et associe
    à chaque instant t la valeur réellement mesurée le plus proche de
    t + horizon_seconds, avec une tolérance (ici ±15s) au-delà de laquelle
    on préfère ne pas associer de valeur du tout plutôt que d'en inventer une.
    """
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"])
    df = df.sort_values("timestamp").reset_index(drop=True)

    # Tag each row before merging.  ``merge_asof`` otherwise has no notion of
    # collection outages and could pair the end of one segment with the start
    # of another.
    gaps = df["timestamp"].diff().dt.total_seconds()
    df["_segment_id"] = (gaps > config.MAX_GAP_SECONDS).cumsum()

    future = df[["_segment_id", "timestamp", config.TARGET_COLUMN]].copy()
    # On recule l'horodatage de la cible de `horizon_seconds` : ainsi, une
    # ligne dont l'horodatage décalé tombe à l'instant t correspond bien à
    # la valeur réelle mesurée à t + horizon_seconds.
    future["timestamp"] = future["timestamp"] - pd.to_timedelta(horizon_seconds, unit="s")
    future = future.rename(columns={config.TARGET_COLUMN: "target_future"})
    future = future.sort_values(["_segment_id", "timestamp"]).reset_index(drop=True)

    merged = pd.merge_asof(
        df.sort_values(["_segment_id", "timestamp"]),
        future,
        on="timestamp",
        by="_segment_id",
        direction="nearest",
        tolerance=pd.to_timedelta(tolerance_seconds, unit="s"),
    )
    return merged.sort_values("timestamp").drop(columns=["_segment_id"]).reset_index(drop=True)


def prepare_frame(df: pd.DataFrame, horizon_seconds: int) -> pd.DataFrame:
    """Lignes exploitables pour l'horizon : cible future, tendances, aucune entrée manquante.
    Même calcul pour l'entraînement et pour l'évaluation du modèle installé (ml/auto_retrain.py).

    Les pics isolés (database/quality.py : une mesure aberrante qui revient aussitôt) sont d'abord
    remplacés par des valeurs absentes : sinon le modèle apprendrait qu'une température peut sauter
    de 2 °C en 15 s, et un pic pris comme cible ou comme point de tendance fausserait l'erreur
    mesurée. Les lignes concernées sont simplement écartées (une sur plusieurs milliers).

    Avec config.ML_TARGET_ESTIMATE, la grandeur surveillée est ensuite remplacée par son estimation
    au dixième (PT01 + échauffement moyen sur 10 min, ml/features.py)."""
    df, spikes = drop_spikes(df.sort_values("timestamp").reset_index(drop=True))
    if any(spikes.values()):
        logger.info("Pics isolés écartés de l'apprentissage : %s",
                    ", ".join(f"{c.upper()} {n}" for c, n in spikes.items() if n))
    if config.ML_TARGET_ESTIMATE:
        # PT02 estimée au dixième (ml/features.py) : la cible ET l'entrée « PT02 » des modèles,
        # comme en service ; la persistance de référence est jugée sur la même grandeur.
        df = with_estimated_target(df)
    merged = add_trend_features(build_target(df, horizon_seconds=horizon_seconds))
    return merged.dropna(subset=MODEL_FEATURES + ["target_future"])


def _write_atomically(path: Path, writer) -> None:
    """Écrit `path` via un fichier temporaire puis `os.replace` : jamais de fichier à moitié écrit.

    Les fichiers sont en 0600 : un modèle (pickle) modifiable par un autre utilisateur
    permettrait d'exécuter du code au chargement.
    """
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "wb") as handle:
            writer(handle)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def train_and_compare(
    df: pd.DataFrame,
    horizon_seconds: int = max(config.PREDICTION_HORIZONS_SECONDS),
    model_dir: str = config.MODEL_DIR,
) -> dict:
    """Pipeline complet : vérification qualité des données, split chronologique,
    entraînement RandomForest + GradientBoosting, comparaison à la persistance.

    Retourne un dict de rapport. Si les données sont insuffisantes pour
    l'horizon demandé, retourne un rapport `{"status": "insufficient_data", ...}`
    explicite SANS entraîner ni sauvegarder de modèle — jamais un résultat
    qui aurait l'air valide sur un jeu de données vide ou quasi vide
    (règle non négociable n°5, leçon vécue section 8).
    """
    if "timestamp" not in df.columns:
        raise ValueError("Le DataFrame fourni doit contenir une colonne 'timestamp'.")

    missing_columns = set(config.FEATURE_COLUMNS + [config.TARGET_COLUMN]) - set(df.columns)
    if missing_columns:
        raise ValueError(f"Colonnes manquantes dans le DataFrame : {missing_columns}")

    _, _, longest_duration = find_longest_continuous_segment(df)

    if longest_duration <= horizon_seconds:
        message = (
            f"Données insuffisantes pour un entraînement fiable à l'horizon de "
            f"{horizon_seconds}s ({horizon_seconds / 60:.0f} min) : le plus long "
            f"segment de collecte continue (sans trou > {config.MAX_GAP_SECONDS}s) "
            f"ne dure que {longest_duration:.0f}s ({longest_duration / 60:.1f} min), "
            f"sur {len(df)} ligne(s) au total dans la période fournie. "
            f"Aucun modèle ne sera entraîné ni sauvegardé."
        )
        logger.warning(message)
        print(f"\n⚠️  {message}\n")
        return {
            "status": "insufficient_data",
            "message": message,
            "longest_continuous_segment_seconds": longest_duration,
            "horizon_seconds": horizon_seconds,
            "n_samples": len(df),
            "trained_at": int(time.time()),
        }

    usable = prepare_frame(df, horizon_seconds)

    if len(usable) < MIN_SAMPLES_FOR_TRAINING:
        message = (
            f"Seulement {len(usable)} échantillon(s) exploitable(s) après "
            f"rapprochement d'horodatage (horizon={horizon_seconds}s, tolérance="
            f"±{config.TARGET_MERGE_TOLERANCE_SECONDS}s) — en dessous du minimum "
            f"requis ({MIN_SAMPLES_FOR_TRAINING}). Un segment continu suffisant a "
            f"été trouvé ({longest_duration:.0f}s), mais trop peu de points s'y "
            f"apparient dans la tolérance. Aucun modèle ne sera entraîné ni sauvegardé."
        )
        logger.warning(message)
        print(f"\n⚠️  {message}\n")
        return {
            "status": "insufficient_data",
            "message": message,
            "n_samples": len(usable),
            "horizon_seconds": horizon_seconds,
            "trained_at": int(time.time()),
        }

    feature_matrix = usable[MODEL_FEATURES].to_numpy()
    target_vector = usable["target_future"].to_numpy()
    current_target_vector = usable[config.TARGET_COLUMN].to_numpy()

    # --- Garde-fou n°1 : la cible elle-même doit avoir suffisamment varié. ---
    # En clair : si la température n'a presque pas bougé sur toute la
    # période, n'importe quel modèle (même un mauvais) obtiendra un score
    # qui a l'air excellent, simplement parce qu'il n'y a rien de difficile
    # à prédire. Ce n'est pas une validation, c'est un jeu de données trop
    # plat pour être un test valide.
    target_std = float(np.std(target_vector))
    if target_std < MIN_TARGET_STD_CELSIUS:
        message = (
            f"Cible ({config.TARGET_COLUMN}) quasi constante sur la période "
            f"exploitable : écart-type de {target_std:.4f}°C, en dessous du "
            f"minimum interprétable ({MIN_TARGET_STD_CELSIUS}°C). Un score de "
            f"prédiction sur des données aussi plates n'est pas fiable, même "
            f"s'il a l'air excellent. Aucun modèle ne sera entraîné ni sauvegardé."
        )
        logger.warning(message)
        print(f"\n⚠️  {message}\n")
        return {
            "status": "insufficient_variance",
            "message": message,
            "target_std_celsius": target_std,
            "min_required_target_std_celsius": MIN_TARGET_STD_CELSIUS,
            "n_samples": len(usable),
            "horizon_seconds": horizon_seconds,
            "trained_at": int(time.time()),
        }

    # --- Garde-fou n°2 : chaque variable d'entrée doit avoir varié. ---
    # Calculé maintenant (avant le split) car il concerne la collecte dans
    # son ensemble, pas seulement le jeu de test.
    feature_std = {
        column: float(np.std(feature_matrix[:, idx]))
        for idx, column in enumerate(MODEL_FEATURES)
    }
    degenerate_features = [
        column
        for column, std in feature_std.items()
        if std < FEATURE_DEGENERACY_STD_EPSILON
    ]

    used_features = [c for c in MODEL_FEATURES if c not in degenerate_features]
    constant_features = {c: float(usable[c].iloc[0]) for c in degenerate_features}
    if not used_features:
        message = ("Toutes les variables d'entrée sont restées figées sur la période : rien à apprendre. "
                   "Aucun modèle ne sera entraîné ni sauvegardé.")
        logger.warning(message)
        return {"status": "no_actionable_signal", "message": message, "degenerate_features": degenerate_features,
                "n_samples": len(usable), "horizon_seconds": horizon_seconds, "trained_at": int(time.time())}
    feature_matrix = usable[used_features].to_numpy()

    split_idx = int(len(usable) * 0.8)
    X_train, X_test = feature_matrix[:split_idx], feature_matrix[split_idx:]
    y_test = target_vector[split_idx:]
    persistence_pred = current_target_vector[split_idx:]  # "prédiction" = valeur actuelle

    if len(X_test) == 0 or len(X_train) == 0:
        message = (
            f"Split chronologique 80/20 impossible avec {len(usable)} échantillon(s) "
            f"exploitable(s) : un des deux sous-ensembles serait vide. "
            f"Aucun modèle ne sera entraîné ni sauvegardé."
        )
        logger.warning(message)
        print(f"\n⚠️  {message}\n")
        return {
            "status": "insufficient_data",
            "message": message,
            "n_samples": len(usable),
            "trained_at": int(time.time()),
        }

    def make_models() -> dict:
        return {
            "random_forest": RandomForestRegressor(n_estimators=200, max_depth=12, random_state=42),
            "gradient_boosting": GradientBoostingRegressor(
                n_estimators=200, max_depth=3, learning_rate=0.05, random_state=42),
        }

    def learn_target(mode: str, current: np.ndarray, future: np.ndarray) -> np.ndarray:
        return future - current if mode == "delta" else future

    def reconstruct(mode: str, current: np.ndarray, output: np.ndarray) -> np.ndarray:
        return current + output if mode == "delta" else output

    # Choix de la cible (écart ou valeur) sur les 20 % les plus récents de l'APPRENTISSAGE :
    # la période de test reste intacte pour juger le modèle retenu.
    current_train, future_train = current_target_vector[:split_idx], target_vector[:split_idx]
    val_idx = int(split_idx * 0.8)
    validation_rmse: dict[str, float] = {}
    if 0 < val_idx < split_idx:
        for mode in TARGET_MODES:
            scores = []
            for model in make_models().values():
                model.fit(X_train[:val_idx], learn_target(mode, current_train[:val_idx], future_train[:val_idx]))
                guess = reconstruct(mode, current_train[val_idx:], model.predict(X_train[val_idx:]))
                scores.append(float(np.sqrt(mean_squared_error(future_train[val_idx:], guess))))
            validation_rmse[mode] = min(scores)
    target_mode = min(validation_rmse, key=lambda m: validation_rmse[m]) if validation_rmse else TARGET_MODES[0]

    fitted_models: dict[str, object] = {}
    metrics: dict[str, dict[str, float]] = {}
    for name, model in make_models().items():
        model.fit(X_train, learn_target(target_mode, current_train, future_train))
        predictions = reconstruct(target_mode, persistence_pred, model.predict(X_test))
        rmse = float(np.sqrt(mean_squared_error(y_test, predictions)))
        mae = float(mean_absolute_error(y_test, predictions))
        metrics[name] = {"rmse": rmse, "mae": mae}
        fitted_models[name] = model

    baseline_rmse = float(np.sqrt(mean_squared_error(y_test, persistence_pred)))
    baseline_mae = float(mean_absolute_error(y_test, persistence_pred))
    metrics["baseline_persistence"] = {"rmse": baseline_rmse, "mae": baseline_mae}

    best_model_name = min(
        ("random_forest", "gradient_boosting"), key=lambda name: metrics[name]["rmse"]
    )
    beats_persistence = metrics[best_model_name]["rmse"] < baseline_rmse

    report = {
        "status": "ok",
        "random_forest": metrics["random_forest"],
        "gradient_boosting": metrics["gradient_boosting"],
        "baseline_persistence": metrics["baseline_persistence"],
        "best_model": best_model_name,
        "beats_persistence": beats_persistence,
        "trained_at": int(time.time()),
        "n_samples": len(usable),
        "horizon_seconds": horizon_seconds,
        # Variables réellement utilisées par les modèles (ordre de config.FEATURE_COLUMNS) et
        # variables écartées car constantes, avec leur valeur (contrôle « hors domaine »).
        "features": used_features,
        # Ce que prédisent les modèles : "delta" (écart à la valeur actuelle) ou "absolute",
        # choisi sur la période de validation (erreurs de validation de chaque façon).
        "target": target_mode,
        # Cible prédite : PT02 estimée au dixième (True) ou PT02 brute ; entrées écartées par réglage.
        "target_estimated": config.ML_TARGET_ESTIMATE,
        "excluded_features": list(config.ML_EXCLUDED_FEATURES),
        "validation_rmse": validation_rmse,
        # Part de chaque entrée dans les décisions du meilleur modèle (somme = 1), de la plus forte
        # à la plus faible : montre ce qui fait réellement bouger la prédiction.
        "feature_importance": dict(sorted(
            ((name, round(float(weight), 4)) for name, weight in
             zip(used_features, getattr(fitted_models[best_model_name], "feature_importances_", []))),
            key=lambda item: -item[1])),
        "target_column": config.TARGET_COLUMN,
        "constant_features": constant_features,
        "target_std_celsius": target_std,
        "feature_std": feature_std,
        "degenerate_features": degenerate_features,
    }
    if degenerate_features:
        report["message"] = (
            f"Variable(s) constante(s) sur toute la période, écartée(s) des entrées : "
            f"{', '.join(f'{c} = {v:g}' for c, v in constant_features.items())}. Le modèle n'a pas appris "
            f"leur effet ; si elles changent, la prédiction sera ignorée (hors domaine).")
        logger.warning(report["message"])

    if not beats_persistence:
        # Un modèle qui fait moins bien que « la température ne bouge pas » n'apporte rien et
        # fausserait l'anticipation du PID : il n'est PAS sauvegardé (statut « no_skill »).
        message = (
            f"Aucun modèle ne bat la persistance (meilleur : {best_model_name}, erreur "
            f"{metrics[best_model_name]['rmse']:.3f} °C contre {baseline_rmse:.3f} °C pour « la "
            f"température ne bouge pas »). Aucun modèle n'est sauvegardé."
        )
        logger.warning(message)
        report["status"] = "no_skill"
        report["message"] = message
        return report

    model_dir_path = Path(model_dir)
    model_dir_path.mkdir(parents=True, exist_ok=True)
    os.chmod(model_dir_path, 0o700)
    for name in ("random_forest", "gradient_boosting"):
        _write_atomically(model_dir_path / f"{name}.joblib", lambda f, n=name: joblib.dump(fitted_models[n], f))
    # Le rapport est écrit EN DERNIER : son apparition signale que les modèles sont complets
    # (le processus principal recharge les modèles quand ce fichier change).
    _write_atomically(model_dir_path / "training_report.json",
                      lambda f: f.write(json.dumps(report, indent=2).encode("utf-8")))

    logger.info(
        "Entraînement terminé sur %d échantillon(s). Meilleur modèle : %s "
        "(bat la persistance : %s).",
        len(usable),
        best_model_name,
        beats_persistence,
    )
    return report


def train_from_db(
    start_ts: str,
    end_ts: str,
    db_path: str = config.SQLITE_DB_PATH,
    horizon_seconds: int = max(config.PREDICTION_HORIZONS_SECONDS),
    model_dir: str = config.MODEL_DIR,
) -> dict:
    """Charge les données depuis SQLite (production ou export local) et entraîne.

    Fonctionne aussi bien sur la base de production que sur un export local
    (même schéma de table `telemetry`) — c'est la généralisation demandée du
    ancien script d'entraînement local.
    """
    df = sqlite_client.get_dataframe(start_ts, end_ts, db_path=db_path)
    return train_and_compare(df, horizon_seconds=horizon_seconds, model_dir=model_dir)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    result = train_from_db(start_ts="1970-01-01", end_ts="2100-01-01")
    print(json.dumps(result, indent=2, default=str))