"""
tests/test_train.py — Vérifie ml/train.py (contrat 6.5, section 7, leçon section 8).

Deux cas couverts, comme demandé par la spec :
1. Un DataFrame synthétique avec un vrai cycle de variation (section 9 du
   brief) : train_and_compare doit produire des métriques et le meilleur
   modèle doit battre la persistance.
2. Un cas volontairement insuffisant (5 minutes de données continues pour un
   horizon de 10 minutes, comme le cas réel du 9 septembre 2026) : le code
   doit lever l'avertissement attendu (rapport status="insufficient_data")
   plutôt que de planter ou de mentir avec un résultat qui aurait l'air valide.
"""

import shutil
import tempfile

import numpy as np
import pandas as pd
import pytest

# Bibliothèque de test du module ml/train.py
from ml.train import build_target, find_longest_continuous_segment, train_and_compare


@pytest.fixture
def temp_model_dir():
    path = tempfile.mkdtemp(prefix="voltaneum_models_test_")
    yield path
    shutil.rmtree(path, ignore_errors=True)


def make_synthetic_dataframe(n: int = 2000, seed: int = 42) -> pd.DataFrame:
    """Reproduit exactement le générateur de données synthétiques de la section 9."""
    rng = np.random.default_rng(seed)
    t = pd.date_range("2026-01-01", periods=n, freq="30s")
    outdoor = 15 + 10 * np.sin(np.linspace(0, 8 * np.pi, n)) + rng.normal(0, 0.5, n)
    freq = np.clip(
        30 + 20 * np.sin(np.linspace(0, 8 * np.pi, n) + 1) + rng.normal(0, 2, n), 0, 100
    )
    pt02 = 40 + 0.3 * outdoor - 0.05 * freq + rng.normal(0, 0.3, n)
    return pd.DataFrame(
        {
            "timestamp": t,
            "pt01": pt02 - 0.3,
            "pt02": pt02,
            "pt03": pt02 - 1.5,
            "pt04": pt02 - 2.0,
            "ht01": 39.0 + rng.normal(0, 0.2, n),
            "freq_pompe": freq,
            "temp_outdoor": outdoor,
        }
    )


class TestFindLongestContinuousSegment: 
    def test_empty_dataframe_returns_zero(self):
        """Un DataFrame vide doit retourner une durée de segment continu de 0s."""
        df = pd.DataFrame({"timestamp": []})
        _, _, duration = find_longest_continuous_segment(df)
        assert duration == 0.0

    def test_single_continuous_segment(self):
        """Un DataFrame avec un segment continu doit retourner sa durée correcte."""
        t = pd.date_range("2026-01-01", periods=100, freq="15s")
        df = pd.DataFrame({"timestamp": t})
        _, _, duration = find_longest_continuous_segment(df)
        assert duration == pytest.approx(99 * 15)

    def test_detects_gap_and_keeps_longest_segment(self):
        """Un DataFrame avec un trou doit retourner la durée du segment le plus long."""
        # Segment A : 10 points à 15s (135s), puis trou de 1h, puis segment B : 3 points (30s).
        seg_a = pd.date_range("2026-01-01 00:00:00", periods=10, freq="15s")
        seg_b = pd.date_range("2026-01-01 05:00:00", periods=3, freq="15s")
        t = seg_a.append(seg_b)
        df = pd.DataFrame({"timestamp": t})
        _, _, duration = find_longest_continuous_segment(df)
        assert duration == pytest.approx(9 * 15)  # segment A est le plus long

    def test_empty_or_single_row_returns_zero(self):
        """Un DataFrame vide ou avec une seule ligne doit retourner une durée de segment continu de 0s."""
        assert find_longest_continuous_segment(pd.DataFrame({"timestamp": []}))[2] == 0.0
        assert find_longest_continuous_segment(
            pd.DataFrame({"timestamp": [pd.Timestamp("2026-01-01")]})
        )[2] == 0.0


class TestBuildTargetMergeAsof:
    def test_build_target_matches_future_value_within_tolerance(self):
        """Vérifie que la cible correspond bien à la valeur réelle à t+horizon,
        pas à une ligne arbitrairement décalée."""
        t = pd.date_range("2026-01-01", periods=50, freq="10s")
        pt02 = np.arange(50, dtype=float)  # valeurs faciles à vérifier : 0, 1, 2, ...
        df = pd.DataFrame({"timestamp": t, "pt02": pt02})

        merged = build_target(df, horizon_seconds=100, tolerance_seconds=2)
        # À t=0 (index 0), t+100s correspond à l'index 10 (10 * 10s = 100s) -> valeur 10.
        assert merged.loc[0, "target_future"] == pytest.approx(10.0)

    def test_build_target_handles_irregular_sampling_correctly(self):
        """Avec un trou dans l'échantillonnage, merge_asof ne doit PAS associer
        une valeur trompeuse hors tolérance (contrairement à un shift() naïf)."""
        # Points réguliers puis un trou, puis reprise.
        timestamps = (
            list(pd.date_range("2026-01-01 00:00:00", periods=5, freq="10s"))
            + list(pd.date_range("2026-01-01 01:00:00", periods=5, freq="10s"))
        )
        pt02 = list(range(10))
        df = pd.DataFrame({"timestamp": timestamps, "pt02": pt02})

        merged = build_target(df, horizon_seconds=100, tolerance_seconds=2)
        # Pour les premières lignes (avant le trou), t+100s tombe dans le trou :
        # aucune valeur réelle n'existe à proximité -> doit être NaN, pas une
        # valeur inventée du segment suivant (qui serait des heures plus tard).
        assert merged.loc[0, "target_future"] != merged.loc[0, "target_future"] or pd.isna(
            merged.loc[0, "target_future"]
        )

    def test_build_target_never_pairs_across_continuity_gap(self):
        timestamps = list(pd.date_range("2026-01-01", periods=3, freq="10s"))
        timestamps += list(pd.date_range("2026-01-01 01:00:00", periods=3, freq="10s"))
        df = pd.DataFrame({"timestamp": timestamps, "pt02": range(6)})

        merged = build_target(df, horizon_seconds=3600, tolerance_seconds=2)

        assert merged["target_future"].isna().all()


class TestTrainAndCompareSyntheticData:
    """Cas 1 (section 7) : jeu de données synthétique avec un vrai cycle."""

    def test_produces_metrics_and_beats_persistence(self, temp_model_dir):
        df = make_synthetic_dataframe(n=2000, seed=42)

        report = train_and_compare(df, model_dir=temp_model_dir)

        assert report["status"] == "ok"
        assert "random_forest" in report and "rmse" in report["random_forest"]
        assert "gradient_boosting" in report and "rmse" in report["gradient_boosting"]
        assert "baseline_persistence" in report
        assert report["best_model"] in ("random_forest", "gradient_boosting")
        # Exigence explicite de la section 9 : les modèles doivent nettement
        # battre la persistance sur ce jeu de données synthétique.
        assert report["beats_persistence"] is True
        assert report[report["best_model"]]["rmse"] < report["baseline_persistence"]["rmse"]

    def test_saves_models_and_report_to_disk(self, temp_model_dir):
        df = make_synthetic_dataframe(n=2000, seed=1)
        train_and_compare(df, model_dir=temp_model_dir)

        from pathlib import Path

        model_dir = Path(temp_model_dir)
        assert (model_dir / "random_forest.joblib").exists()
        assert (model_dir / "gradient_boosting.joblib").exists()
        assert (model_dir / "training_report.json").exists()

        import json

        with open(model_dir / "training_report.json") as f:
            saved_report = json.load(f)
        assert saved_report["status"] == "ok"
        assert set(saved_report.keys()) >= {
            "random_forest", "gradient_boosting", "baseline_persistence",
            "best_model", "beats_persistence", "trained_at", "n_samples",
        }


class TestTrainAndCompareInsufficientData:
    """Cas 2 (section 7 + leçon section 8) : 5 minutes de données continues,
    horizon de 10 minutes -> avertissement attendu, pas de plantage ni de mensonge."""

    def test_five_minutes_of_data_with_ten_minute_horizon_raises_warning(self, temp_model_dir):
        # 5 minutes de collecte continue toutes les 15s = 21 points (spec §8 : c'est
        # exactement le type de cas rencontré avec voltaneum.db le 9 septembre 2026).
        t = pd.date_range("2026-09-09 10:00:00", periods=21, freq="15s")  # couvre 5 min
        n = len(t)
        rng = np.random.default_rng(0)
        pt02 = 35.0 + rng.normal(0, 0.2, n)

        df = pd.DataFrame(
            {
                "timestamp": t,
                "pt01": pt02 - 0.3,
                "pt02": pt02,
                "pt03": pt02 - 1.5,
                "pt04": pt02 - 2.0,
                "ht01": 39.0 + rng.normal(0, 0.2, n),
                "freq_pompe": np.zeros(n),
                "temp_outdoor": 12.9 + rng.normal(0, 0.1, n),
            }
        )

        report = train_and_compare(df, horizon_seconds=600, model_dir=temp_model_dir)

        assert report["status"] == "insufficient_data"
        assert "message" in report and len(report["message"]) > 0
        assert report["longest_continuous_segment_seconds"] < 600

        # Aucun modèle ne doit avoir été sauvegardé (pas de résultat trompeur).
        from pathlib import Path

        model_dir = Path(temp_model_dir)
        assert not (model_dir / "random_forest.joblib").exists()
        assert not (model_dir / "training_report.json").exists()

    def test_does_not_raise_exception_on_insufficient_data(self, temp_model_dir):
        """Le comportement attendu est un rapport explicite, PAS une exception non gérée."""
        t = pd.date_range("2026-09-09 10:00:00", periods=5, freq="15s")
        df = pd.DataFrame(
            {
                "timestamp": t,
                "pt01": [24.0] * 5, "pt02": [26.0] * 5,
                "pt03": [23.5] * 5, "pt04": [23.5] * 5,
                "ht01": [39.0] * 5, "freq_pompe": [0.0] * 5,
                "temp_outdoor": [12.9] * 5,
            }
        )
        # Ne doit pas lever d'exception.
        report = train_and_compare(df, horizon_seconds=600, model_dir=temp_model_dir)
        assert report["status"] == "insufficient_data"

    def test_reproduces_exact_september_9_scenario(self, temp_model_dir):
        """Reproduit le cas réel documenté en section 8 : 89 lignes, ~8 minutes
        de segment continu maximum, horizon de 10 minutes -> 0 échantillon exploitable."""
        # 89 lignes réparties en un segment de ~8 min (32 points à 15s) et le
        # reste dispersé avec de grands trous, pour approcher le cas réel.
        seg = pd.date_range("2026-09-09 08:00:00", periods=32, freq="15s")  # ~7.75 min
        scattered = pd.date_range("2026-09-09 12:00:00", periods=57, freq="20min")
        t = seg.append(scattered)
        n = len(t)
        rng = np.random.default_rng(0)
        pt02 = 35.0 + rng.normal(0, 0.2, n)

        df = pd.DataFrame(
            {
                "timestamp": t,
                "pt01": pt02 - 0.3, "pt02": pt02,
                "pt03": pt02 - 1.5, "pt04": pt02 - 2.0,
                "ht01": 39.0 + rng.normal(0, 0.2, n),
                "freq_pompe": np.zeros(n),
                "temp_outdoor": 12.9 + rng.normal(0, 0.1, n),
            }
        )

        report = train_and_compare(df, horizon_seconds=600, model_dir=temp_model_dir)

        assert report["status"] == "insufficient_data"
        assert report["n_samples"] == 89
        assert report["longest_continuous_segment_seconds"] < 600


class TestTrainAndCompareDegenerateFeatures:
    """Garde-fou ajouté suite à la revue du 17-18 septembre 2026 : reproduit le
    cas réel où RandomForest a obtenu un RMSE de 0.0 sur la collecte
    9-10 septembre parce que `freq_pompe` et `ht01` sont restés strictement
    constants sur toute la période. Un score arithmétiquement excellent ne
    doit jamais être présenté comme un modèle validé dans ce cas."""

    def _make_dataframe_with_frozen_pump(self, n: int = 1500) -> pd.DataFrame:
        rng = np.random.default_rng(7)
        t = pd.date_range("2026-09-09 15:29:10", periods=n, freq="47s")
        outdoor = 20 + 6 * np.sin(np.linspace(0, 4 * np.pi, n)) + rng.normal(0, 0.3, n)
        pt02 = 27 + 0.15 * outdoor + rng.normal(0, 0.1, n)
        return pd.DataFrame(
            {
                "timestamp": t,
                "pt01": pt02 - 0.3,
                "pt02": pt02,
                "pt03": pt02 - 1.5,
                "pt04": pt02 - 2.0,
                "ht01": np.full(n, 39.0),       # jamais varié, comme sur la vraie collecte
                "freq_pompe": np.zeros(n),      # pompe jamais activée, comme sur la vraie collecte
                "temp_outdoor": outdoor,
            }
        )

    def test_frozen_variables_are_set_aside_not_blocking(self, temp_model_dir):
        """Cas réel : la pompe tourne à fréquence fixe. Une variable constante n'apporte rien au
        modèle ; elle est ÉCARTÉE des entrées (avec sa valeur) au lieu de bloquer tout
        apprentissage pour toujours."""
        df = self._make_dataframe_with_frozen_pump()
        report = train_and_compare(df, horizon_seconds=600, model_dir=temp_model_dir)

        assert set(report["degenerate_features"]) == {"ht01", "freq_pompe"}
        assert "ht01" not in report["features"] and "freq_pompe" not in report["features"]
        assert report["constant_features"] == {"ht01": 39.0, "freq_pompe": 0.0}
        assert report["status"] in ("ok", "no_skill")

    def test_saved_model_uses_only_the_varying_inputs(self, temp_model_dir):
        from ml.predictor import DualModelPredictor

        df = self._make_dataframe_with_frozen_pump()
        report = train_and_compare(df, horizon_seconds=600, model_dir=temp_model_dir)
        if report["status"] != "ok":
            pytest.skip("données synthétiques sans gain sur la persistance")
        predictor = DualModelPredictor(model_dir=temp_model_dir)
        sample = {"pt01": 30.0, "pt02": 30.3, "pt03": 28.8, "pt04": 28.3, "ht01": 39.0,
                  "freq_pompe": 0.0, "temp_outdoor": 20.0}
        assert predictor.predict(sample)["out_of_domain"] == []
        assert predictor.predict({**sample, "freq_pompe": 30.0})["out_of_domain"] == ["freq_pompe"]

    def test_realistic_sensor_noise_does_not_trigger_false_positive(self, temp_model_dir):
        """Non-régression du faux positif détecté en cours de développement :
        un bruit de mesure réaliste mais faible (ex. std=0.2 sur ht01, comme
        dans le générateur synthétique de la section 9) ne doit JAMAIS être
        confondu avec une variable réellement figée."""
        df = make_synthetic_dataframe(n=2000, seed=42)
        report = train_and_compare(df, horizon_seconds=600, model_dir=temp_model_dir)

        assert report["status"] == "ok"
        assert report["degenerate_features"] == []


class TestTrainAndCompareInsufficientVariance:
    def test_flags_insufficient_variance_when_target_barely_moves(self, temp_model_dir):
        """Cible quasi constante sur toute la période (écart-type très
        inférieur à MIN_TARGET_STD_CELSIUS) : aucun modèle ne doit être
        entraîné, même si un segment continu suffisamment long existe."""
        n = 1500
        rng = np.random.default_rng(3)
        t = pd.date_range("2026-09-09 08:00:00", periods=n, freq="15s")
        pt02 = 27.0 + rng.normal(0, 0.01, n)  # bruit de mesure seul, aucun vrai mouvement

        df = pd.DataFrame(
            {
                "timestamp": t,
                "pt01": pt02 - 0.3, "pt02": pt02,
                "pt03": pt02 - 1.5, "pt04": pt02 - 2.0,
                "ht01": 39.0 + rng.normal(0, 0.2, n),
                "freq_pompe": 30 + rng.normal(0, 2, n),
                "temp_outdoor": 20 + rng.normal(0, 0.5, n),
            }
        )

        report = train_and_compare(df, horizon_seconds=600, model_dir=temp_model_dir)

        assert report["status"] == "insufficient_variance"
        assert report["target_std_celsius"] < 0.3