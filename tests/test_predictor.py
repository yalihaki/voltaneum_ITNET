"""
tests/test_predictor.py — Vérifie DualModelPredictor (contrat 6.6).

En clair : on entraîne d'abord de vrais petits modèles sur des données
synthétiques (comme à l'étape 7) pour avoir des fichiers .joblib réels à
charger, plutôt que de fabriquer de faux modèles factices qui ne
testeraient pas le vrai chemin de chargement.
"""

import json
import shutil
import tempfile
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ml.predictor import DualModelPredictor
from ml.train import train_and_compare
import config


@pytest.fixture
def trained_model_dir():
    """Entraîne de vrais modèles sur des données synthétiques et fournit leur dossier."""
    model_dir = tempfile.mkdtemp(prefix="voltaneum_predictor_test_")

    rng = np.random.default_rng(7)
    n = 2000
    t = pd.date_range("2026-01-01", periods=n, freq="30s")
    outdoor = 15 + 10 * np.sin(np.linspace(0, 8 * np.pi, n)) + rng.normal(0, 0.5, n)
    freq = np.clip(30 + 20 * np.sin(np.linspace(0, 8 * np.pi, n) + 1) + rng.normal(0, 2, n), 0, 100)
    pt02 = 40 + 0.3 * outdoor - 0.05 * freq + rng.normal(0, 0.3, n)
    df = pd.DataFrame(
        {
            "timestamp": t,
            "pt01": pt02 - 0.3, "pt02": pt02,
            "pt03": pt02 - 1.5, "pt04": pt02 - 2.0,
            "ht01": 39.0 + rng.normal(0, 0.2, n),
            "freq_pompe": freq,
            "temp_outdoor": outdoor,
        }
    )
    train_and_compare(df, model_dir=model_dir)

    yield model_dir
    shutil.rmtree(model_dir, ignore_errors=True)


@pytest.fixture
def empty_model_dir():
    path = tempfile.mkdtemp(prefix="voltaneum_predictor_empty_test_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


SAMPLE_FEATURES = {
    "pt02": 24.5,
    "pt01": 24.0,
    "pt03": 23.5,
    "pt04": 23.5,
    "ht01": 39.0,
    "freq_pompe": 30.0,
    "temp_outdoor": 15.0,
}


class TestDualModelPredictorInit:
    def test_raises_file_not_found_when_models_missing(self, empty_model_dir):
        with pytest.raises(FileNotFoundError, match="non entraînés"):
            DualModelPredictor(model_dir=empty_model_dir)

    def test_loads_successfully_when_models_present(self, trained_model_dir):
        predictor = DualModelPredictor(model_dir=trained_model_dir)
        assert predictor.best_model_name in ("random_forest", "gradient_boosting")
        assert predictor.horizon_seconds == max(config.PREDICTION_HORIZONS_SECONDS)

    def test_raises_if_training_report_status_not_ok(self, empty_model_dir):
        """Un dossier avec des fichiers présents mais un rapport 'insufficient_data'
        (ex. copié manuellement) doit aussi être rejeté."""
        model_dir = Path(empty_model_dir)
        # Fabrique des fichiers factices minimaux pour ce cas limite précis.
        (model_dir / "random_forest.joblib").write_bytes(b"fake")
        (model_dir / "gradient_boosting.joblib").write_bytes(b"fake")
        with open(model_dir / "training_report.json", "w") as f:
            json.dump({"status": "insufficient_data"}, f)

        with pytest.raises(FileNotFoundError, match="insufficient_data"):
            DualModelPredictor(model_dir=empty_model_dir)


class TestDualModelPredictorPredict:
    def test_predict_returns_dict_with_expected_keys(self, trained_model_dir):
        """Test de sortie officiel de l'étape 8 (spec §3, ligne 8)."""
        predictor = DualModelPredictor(model_dir=trained_model_dir)
        result = predictor.predict(SAMPLE_FEATURES)

        expected_keys = {
            "predictions", "combined_prediction", "best_model",
            "disagreement", "disagreement_flag", "out_of_domain", "horizon_seconds",
        }
        assert set(result.keys()) == expected_keys

        assert set(result["predictions"].keys()) == {"random_forest", "gradient_boosting"}
        assert isinstance(result["predictions"]["random_forest"], float)
        assert isinstance(result["predictions"]["gradient_boosting"], float)
        assert isinstance(result["combined_prediction"], float)
        assert result["best_model"] in ("random_forest", "gradient_boosting")
        assert isinstance(result["disagreement"], float)
        assert result["disagreement"] >= 0.0
        assert isinstance(result["disagreement_flag"], bool)
        assert result["horizon_seconds"] == max(config.PREDICTION_HORIZONS_SECONDS)

    def test_combined_prediction_matches_best_model(self, trained_model_dir):
        predictor = DualModelPredictor(model_dir=trained_model_dir)
        result = predictor.predict(SAMPLE_FEATURES)
        assert result["combined_prediction"] == result["predictions"][result["best_model"]]

    def test_disagreement_is_absolute_difference(self, trained_model_dir):
        predictor = DualModelPredictor(model_dir=trained_model_dir)
        result = predictor.predict(SAMPLE_FEATURES)
        expected = abs(
            result["predictions"]["random_forest"] - result["predictions"]["gradient_boosting"]
        )
        assert result["disagreement"] == pytest.approx(expected)

    def test_raises_on_incomplete_features(self, trained_model_dir):
        predictor = DualModelPredictor(model_dir=trained_model_dir)
        incomplete = {k: v for k, v in SAMPLE_FEATURES.items() if k != "ht01"}
        with pytest.raises(ValueError, match="incomplet"):
            predictor.predict(incomplete)

    def test_disagreement_flag_true_when_models_disagree(self, trained_model_dir, monkeypatch):
        """Force artificiellement un grand désaccord pour vérifier le flag."""
        predictor = DualModelPredictor(model_dir=trained_model_dir)

        class FakeModel:
            def __init__(self, value):
                self.value = value

            def predict(self, X):
                return np.array([self.value])

        predictor.random_forest = FakeModel(20.0)
        predictor.gradient_boosting = FakeModel(25.0)  # désaccord de 5°C > seuil 1.0°C

        result = predictor.predict(SAMPLE_FEATURES)
        assert result["disagreement"] == pytest.approx(5.0)
        assert result["disagreement_flag"] is True

    def test_disagreement_flag_false_when_models_agree(self, trained_model_dir):
        predictor = DualModelPredictor(model_dir=trained_model_dir)

        class FakeModel:
            def __init__(self, value):
                self.value = value

            def predict(self, X):
                return np.array([self.value])

        predictor.random_forest = FakeModel(20.0)
        predictor.gradient_boosting = FakeModel(20.3)  # désaccord de 0.3°C < seuil

        result = predictor.predict(SAMPLE_FEATURES)
        assert result["disagreement_flag"] is False

def test_model_trained_with_other_inputs_is_refused(trained_model_dir):
    """Un modèle entraîné avec une autre liste de variables donnerait des prédictions absurdes."""
    import json
    report_path = Path(trained_model_dir) / "training_report.json"
    report = json.loads(report_path.read_text())
    report["features"] = ["pt01", "pt03"]
    report_path.write_text(json.dumps(report))
    with pytest.raises(FileNotFoundError, match="autres variables"):
        DualModelPredictor(model_dir=str(trained_model_dir))


def test_model_on_a_subset_of_inputs_is_accepted_and_checks_its_domain(trained_model_dir):
    """Un modèle entraîné sans une variable restée constante (ex. pompe à 30 Hz) est utilisable ;
    si cette variable change, la prédiction est signalée « hors domaine »."""
    import json

    from ml.predictor import compatible_features

    report = {"features": ["pt02", "pt01", "temp_outdoor"], "constant_features": {"freq_pompe": 30.0}}
    assert compatible_features(report)
    assert not compatible_features({"features": ["pt01", "pt02"]})            # ordre différent
    assert not compatible_features({"features": ["pt02", "inconnue"]})
    assert not compatible_features({"features": []})

    predictor = DualModelPredictor(model_dir=trained_model_dir)
    predictor.constant_features = {"freq_pompe": 30.0}
    assert predictor.out_of_domain({**SAMPLE_FEATURES, "freq_pompe": 30.4}) == []
    assert predictor.out_of_domain({**SAMPLE_FEATURES, "freq_pompe": 45.0}) == ["freq_pompe"]
    json.dumps(predictor.predict(SAMPLE_FEATURES))   # résultat sérialisable (publié sur MQTT)
