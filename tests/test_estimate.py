"""Tests de la PT02 ESTIMÉE au dixième (ml/features.py) et de l'exclusion d'entrées des modèles.

PT02 estimée = PT01 + moyenne de (PT02 − PT01) sur 10 min. Le même calcul doit servir à
l'entraînement, en service et pour juger les prédictions échues : ces tests le vérifient.
"""

import tempfile

import numpy as np
import pandas as pd
import pytest

import config
from database import sqlite_client
from ml.features import (ESTIMATE_MIN_SAMPLES, estimate_from_history, model_features,
                         with_estimated_target)


def _real_like(n=200, start="2026-10-20 10:00:00"):
    """PT01 au dixième, PT02 au degré près (comme sur le contrôleur), échauffement vrai de 0,6 °C."""
    t = pd.date_range(start, periods=n, freq="15s")
    pt01 = 33.8 + 0.3 * np.sin(np.arange(n) / 20)
    true_pt02 = pt01 + 0.6
    return pd.DataFrame({"timestamp": t, "pt01": np.round(pt01, 1), "pt02": np.round(true_pt02),
                         "pt03": 33.2, "pt04": 32.5})


def test_estimate_recovers_a_tenth_of_degree_signal():
    df = _real_like()
    est = with_estimated_target(df)
    assert est["pt02"].iloc[:ESTIMATE_MIN_SAMPLES - 1].isna().all()     # fenêtre pas encore remplie
    true_pt02 = df["pt01"] + 0.6
    raw_error = (df["pt02"] - true_pt02).abs().iloc[50:].mean()
    est_error = (est["pt02"] - true_pt02).abs().iloc[50:].mean()
    assert est_error < raw_error * 0.7                                  # nettement plus proche que la lecture au degré
    assert df["pt02"].iloc[60] == round(df["pt02"].iloc[60])            # l'original n'est pas modifié


def test_service_estimate_matches_training():
    df = _real_like()
    trained = with_estimated_target(df)
    ts = [t.timestamp() for t in df["timestamp"]]
    i = 120
    estimate, history = estimate_from_history(list(zip(ts[:i + 1], df["pt02"][:i + 1])),
                                              list(zip(ts[:i + 1], df["pt01"][:i + 1])), ts[i], df["pt01"][i])
    assert estimate == pytest.approx(trained["pt02"].iloc[i], abs=1e-9)
    assert history[-1][1] == pytest.approx(trained["pt02"].iloc[i])


def test_service_estimate_needs_ten_minutes_of_history():
    df = _real_like(n=5)
    ts = [t.timestamp() for t in df["timestamp"]]
    estimate, _ = estimate_from_history(list(zip(ts, df["pt02"])), list(zip(ts, df["pt01"])), ts[-1], 33.8)
    assert estimate is None
    assert estimate_from_history([], [], 0.0, 33.8) == (None, [])


def test_excluded_features_are_left_out():
    features = model_features(("temp_outdoor",))
    assert "temp_outdoor" not in features and "pt02" in features and "pt04_d5min" in features
    assert "temp_outdoor" in model_features(())


def test_the_watched_probe_cannot_be_excluded(monkeypatch):
    monkeypatch.setattr(config, "ML_EXCLUDED_FEATURES", ("pt02",))
    with pytest.raises(ValueError, match="ML_EXCLUDED_FEATURES"):
        config.validate_config()


def test_model_trained_on_another_target_kind_is_refused(monkeypatch):
    from ml.predictor import compatible_features
    report = {"features": ["pt02", "pt01"], "constant_features": {}, "target": "delta",
              "target_column": "pt02", "target_estimated": True}
    monkeypatch.setattr(config, "ML_TARGET_ESTIMATE", True)
    assert compatible_features(report)
    monkeypatch.setattr(config, "ML_TARGET_ESTIMATE", False)
    assert not compatible_features(report)                              # PT02 brute attendue


def test_training_on_the_estimate(monkeypatch):
    from ml.train import train_and_compare
    from tests.test_train import make_synthetic_dataframe
    monkeypatch.setattr(config, "ML_TARGET_ESTIMATE", True)
    df = make_synthetic_dataframe()
    df["pt02"] = df["pt02"].round()                                     # lue au degré près
    report = train_and_compare(df, 600, tempfile.mkdtemp())
    assert report["target_estimated"] is True and report["status"] in ("ok", "no_skill")


def test_due_predictions_are_judged_on_the_estimate(tmp_path, monkeypatch):
    """Le modèle prédit PT02 estimée : l'erreur suivie en production doit l'être sur la même grandeur."""
    monkeypatch.setattr(config, "ML_TARGET_ESTIMATE", True)
    db = str(tmp_path / "e.db")
    sqlite_client.init_db(db)
    for i in range(40):                                                  # 10 min : PT01 33,4, PT02 34 → +0,6
        ts = (pd.Timestamp("2026-10-20 10:00:00") + pd.Timedelta(seconds=15 * i)).strftime("%Y-%m-%d %H:%M:%S")
        sqlite_client.insert_reading(33.4, 34.0, 33.0, 32.5, 10.4, 30.0, 12.0, timestamp=ts, db_path=db)
    sqlite_client.log_prediction("2026-10-20 09:59:45", "2026-10-20 10:09:45", 600, "gradient_boosting",
                                 34.1, db_path=db)
    assert sqlite_client.complete_due_predictions("2026-10-20 10:10:00", db_path=db) == 1
    with sqlite_client.connect(db) as conn:
        actual, error = conn.execute("SELECT actual_value, error FROM predictions_log").fetchone()
    assert actual == pytest.approx(34.0)                                 # 33,4 + 0,6
    assert error == pytest.approx(0.1)


def test_due_predictions_keep_the_plc_value_and_the_report_shows_both(tmp_path, monkeypatch, capsys):
    """« À 10:00 on prédit PT02 pour 10:15 : que valait-elle à 10:15 ? » — la PT02 de l'automate est
    gardée à côté de la référence, et le rapport compare le modèle à la persistance."""
    from ml.verification import prediction_report
    monkeypatch.setattr(config, "ML_TARGET_ESTIMATE", True)
    monkeypatch.setattr(config, "MODEL_DIR", str(tmp_path / "models"))
    db = str(tmp_path / "v.db")
    sqlite_client.init_db(db)
    start = pd.Timestamp.now(tz="UTC").floor("min") - pd.Timedelta(minutes=40)
    for i in range(160):                                                 # 40 min, PT01 qui monte doucement
        ts = (start + pd.Timedelta(seconds=15 * i)).strftime("%Y-%m-%d %H:%M:%S")
        sqlite_client.insert_reading(33.0 + 0.005 * i, 34.0 if i < 100 else 35.0, 33.0, 32.5, 10.4, 30.0, 12.0,
                                     timestamp=ts, db_path=db)
    made = (start + pd.Timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
    target = (start + pd.Timedelta(minutes=30)).strftime("%Y-%m-%d %H:%M:%S")
    sqlite_client.log_prediction(made, target, 900, "gradient_boosting", 34.4, db_path=db)
    assert sqlite_client.complete_due_predictions(pd.Timestamp.now(tz="UTC").strftime("%Y-%m-%d %H:%M:%S"),
                                                  db_path=db) == 1
    with sqlite_client.connect(db) as conn:
        actual, measured = conn.execute("SELECT actual_value, measured_value FROM predictions_log").fetchone()
    assert measured == 35.0 and actual == pytest.approx(34.62, abs=0.05)  # PT01 33,6 + échauffement moyen 1,02
    report = prediction_report(db, hours=2)
    assert "1 vérifiée(s)" in report and "35.0" in report and "persistance" in report
