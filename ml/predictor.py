"""
ml/predictor.py — Charge les modèles entraînés et produit des prédictions
comparées (contrat 6.6).

En clair : `ml/train.py` est le "cours du soir" — il entraîne les modèles
une fois (ou périodiquement) et les range dans un dossier. Ce module-ci est
l'"élève diplômé" qu'on interroge en production, en continu : il charge les
modèles déjà entraînés (sans jamais les ré-entraîner lui-même) et répond
très vite à la question "que vaudra PT02 dans 10 minutes ?".

On interroge volontairement DEUX modèles (RandomForest et GradientBoosting)
plutôt qu'un seul, et on calcule leur désaccord (`disagreement`). En clair :
si les deux modèles, entraînés différemment, arrivent à des conclusions très
proches, on peut leur faire un peu plus confiance. S'ils divergent beaucoup
(> 1°C par défaut), c'est un signal d'alerte : la situation actuelle
ressemble peut-être à quelque chose que les modèles n'ont pas bien appris
pendant l'entraînement, et il vaut mieux s'en remettre à la prudence humaine
ou à l'automate plutôt qu'à la prédiction.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

import joblib
import numpy as np

import config

logger = logging.getLogger(__name__)


class DualModelPredictor:
    """Charge les deux modèles entraînés (RandomForest, GradientBoosting) et
    produit des prédictions comparées à partir des features actuelles.

    Lève `FileNotFoundError` explicite si les modèles n'ont pas encore été
    entraînés (contrat 6.6) — ne retourne JAMAIS une prédiction factice en
    silence si les fichiers attendus sont absents.
    """

    def __init__(self, model_dir: str = config.MODEL_DIR) -> None:
        self.model_dir = Path(model_dir)

        rf_path = self.model_dir / "random_forest.joblib"
        gb_path = self.model_dir / "gradient_boosting.joblib"
        report_path = self.model_dir / "training_report.json"

        missing = [p.name for p in (rf_path, gb_path, report_path) if not p.exists()]
        if missing:
            raise FileNotFoundError(
                f"Modèles non entraînés : fichier(s) manquant(s) dans "
                f"'{self.model_dir}' : {missing}. Lancez d'abord "
                f"'./voltaneum retrain' puis './voltaneum model accept' pour installer un modèle validé."
            )

        with open(report_path, "r", encoding="utf-8") as f:
            self.training_report: dict = json.load(f)

        if self.training_report.get("status") != "ok":
            # Un rapport "insufficient_data" ne devrait jamais avoir sauvegardé
            # de modèles (voir ml/train.py), mais on se protège quand même
            # d'un dossier de modèles obtenu par un autre moyen (copie
            # manuelle, ancienne version...).
            raise FileNotFoundError(
                f"Le rapport d'entraînement trouvé dans '{self.model_dir}' indique "
                f"un statut '{self.training_report.get('status')}' plutôt que 'ok' : "
                f"les modèles associés ne sont pas fiables. Ré-entraînez avant "
                f"d'utiliser ce dossier pour la prédiction."
            )

        if self.training_report.get("features") != list(config.FEATURE_COLUMNS):
            raise FileNotFoundError(
                f"Les modèles de '{self.model_dir}' ont été entraînés avec d'autres variables d'entrée "
                f"({self.training_report.get('features')}) que celles attendues ({config.FEATURE_COLUMNS}) : "
                f"ré-entraîner (./voltaneum retrain) puis valider (./voltaneum model accept)."
            )

        self.random_forest = joblib.load(rf_path)
        self.gradient_boosting = joblib.load(gb_path)

        self.best_model_name: str = self.training_report.get("best_model", "random_forest")
        self.horizon_seconds: int = self.training_report.get(
            "horizon_seconds", max(config.PREDICTION_HORIZONS_SECONDS)
        )

        logger.info(
            "Modèles chargés depuis '%s' (meilleur modèle au test : %s).",
            self.model_dir,
            self.best_model_name,
        )

    def predict(self, current_features: dict) -> dict:
        """Prédit la valeur future de TARGET_COLUMN à partir des features actuelles.

        `current_features` doit contenir toutes les clés de config.FEATURE_COLUMNS.

        Retourne :
        {
          "predictions": {"random_forest": float, "gradient_boosting": float},
          "combined_prediction": float,   # celle du modèle avec le meilleur RMSE au test
          "best_model": str,
          "disagreement": float,          # |pred_rf - pred_gb|
          "disagreement_flag": bool,      # True si disagreement > seuil configuré
          "horizon_seconds": int,
        }
        """
        missing_features = [
            column for column in config.FEATURE_COLUMNS if column not in current_features
        ]
        if missing_features:
            raise ValueError(
                f"current_features incomplet : colonnes manquantes {missing_features}. "
                f"Colonnes attendues : {config.FEATURE_COLUMNS}."
            )

        feature_vector = np.array(
            [[current_features[column] for column in config.FEATURE_COLUMNS]]
        )

        pred_rf = float(self.random_forest.predict(feature_vector)[0])
        pred_gb = float(self.gradient_boosting.predict(feature_vector)[0])

        predictions = {"random_forest": pred_rf, "gradient_boosting": pred_gb}
        combined_prediction = predictions[self.best_model_name]
        disagreement = abs(pred_rf - pred_gb)
        disagreement_flag = disagreement > config.DISAGREEMENT_THRESHOLD_CELSIUS

        if disagreement_flag:
            logger.warning(
                "Désaccord important entre les deux modèles : %.2f°C "
                "(RF=%.2f, GB=%.2f) — seuil=%.2f°C.",
                disagreement, pred_rf, pred_gb, config.DISAGREEMENT_THRESHOLD_CELSIUS,
            )

        return {
            "predictions": predictions,
            "combined_prediction": combined_prediction,
            "best_model": self.best_model_name,
            "disagreement": disagreement,
            "disagreement_flag": disagreement_flag,
            "horizon_seconds": self.horizon_seconds,
        }