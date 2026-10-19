"""Tests du ré-entraînement automatique avec validation humaine (ml/auto_retrain.py)."""

import json
from datetime import timezone

import pandas as pd
import pytest

import config
from database import sqlite_client
from ml import auto_retrain
from ml.auto_retrain import ProposalError, decide, judge
from tests.test_train import make_synthetic_dataframe


def _report(rmse, baseline, status="ok"):
    return {"status": status, "best_model": "gradient_boosting", "gradient_boosting": {"rmse": rmse, "mae": rmse},
            "baseline_persistence": {"rmse": baseline, "mae": baseline}, "beats_persistence": rmse < baseline}


def _cand(horizon, eligible=True, rmse=0.3):
    return {"horizon_seconds": horizon, "eligible": eligible, "rmse": rmse, "reason": ""}


def _current(horizon, rmse=0.3, reliable=True):
    return {"horizon_seconds": horizon, "rmse": rmse, "still_reliable": reliable}


class TestJudge:
    def test_reliable_model(self):
        assert judge(_report(0.3, 0.6))[0] is True

    def test_frozen_variables_are_named(self):
        eligible, reason = judge({"status": "no_actionable_signal", "degenerate_features": ["freq_pompe", "ht01"]})
        assert not eligible and "fréquence pompe" in reason and "débit HT01" in reason

    def test_refused_when_training_refused(self):
        eligible, reason = judge({"status": "insufficient_variance"})
        assert not eligible and "PT01" in reason

    def test_refused_when_not_enough_better_than_persistence(self):
        eligible, reason = judge(_report(0.45, 0.5))          # 10 % de mieux < 20 %
        assert not eligible and "persistance" in reason

    def test_refused_when_error_too_large(self):
        eligible, reason = judge(_report(0.9, 3.0))           # 70 % de mieux mais 0,9 °C > 0,5 °C
        assert not eligible and "erreur trop grande" in reason


class TestDecide:
    def test_longest_reliable_horizon_is_proposed(self):
        chosen, _ = decide([_cand(600), _cand(1800), _cand(3600, eligible=False)], None)
        assert chosen["horizon_seconds"] == 1800

    def test_nothing_reliable(self):
        assert decide([_cand(600, eligible=False)], None)[0] is None

    def test_longer_horizon_than_installed(self):
        chosen, reason = decide([_cand(600), _cand(1800)], _current(600))
        assert chosen["horizon_seconds"] == 1800 and "plus long" in reason

    def test_same_horizon_must_be_clearly_better(self):
        assert decide([_cand(600, rmse=0.29)], _current(600, rmse=0.30))[0] is None
        assert decide([_cand(600, rmse=0.25)], _current(600, rmse=0.30))[0] is not None

    def test_shorter_horizon_only_if_installed_model_degraded(self):
        assert decide([_cand(600)], _current(1800, reliable=True))[0] is None
        chosen, reason = decide([_cand(600)], _current(1800, reliable=False))
        assert chosen["horizon_seconds"] == 600 and "ne remplit plus" in reason


@pytest.fixture
def setup(tmp_path, monkeypatch):
    """Base SQLite remplie de données synthétiques (17 h, une mesure / 30 s) et dossier de modèles."""
    monkeypatch.setattr(config, "AUTO_RETRAIN_MAX_DAYS", 3650)
    df = make_synthetic_dataframe()
    db = str(tmp_path / "t.db")
    sqlite_client.init_db(db)
    with sqlite_client.connect(db) as conn:
        conn.executemany(
            "INSERT INTO telemetry (timestamp, pt01, pt02, pt03, pt04, ht01, freq_pompe, temp_outdoor) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            [(r.timestamp.strftime("%Y-%m-%d %H:%M:%S"), r.pt01, r.pt02, r.pt03, r.pt04, r.ht01, r.freq_pompe,
              r.temp_outdoor) for r in df.itertuples()],
        )
    now = (df["timestamp"].max() + pd.Timedelta(minutes=1)).to_pydatetime().replace(tzinfo=timezone.utc)
    return db, str(tmp_path / "models"), now


def test_full_cycle_propose_accept_rollback(setup):
    db, model_dir, now = setup
    summary = auto_retrain.run_auto_retrain(db, model_dir, horizons=(60, 300, 86400), now=now)
    by_h = {c["horizon_seconds"]: c for c in summary["candidates"]}
    assert by_h[60]["eligible"] and by_h[300]["eligible"]
    assert not by_h[86400]["eligible"]                              # 17 h de données < 24 h
    assert summary["decision"] == "proposed" and summary["proposed_horizon_seconds"] == 300

    # Rien n'est installé tant qu'une personne n'a pas validé.
    assert not (auto_retrain.Path(model_dir) / "training_report.json").exists()
    proposal = auto_retrain.pending_proposal(model_dir)
    assert proposal["warnings"]                                     # 5 min ≠ horizon de référence du PID

    auto_retrain.accept_proposal(model_dir, operator="test")
    installed = json.loads((auto_retrain.Path(model_dir) / "training_report.json").read_text())
    assert installed["horizon_seconds"] == 300
    with pytest.raises(ProposalError):
        auto_retrain.pending_proposal(model_dir)

    # Nuit suivante, mêmes données : le modèle installé reste au moins aussi bon -> pas de proposition.
    second = auto_retrain.run_auto_retrain(db, model_dir, horizons=(60, 300), now=now)
    assert second["decision"] == "none" and second["current"]["horizon_seconds"] == 300

    # Le premier --accept n'a rien remplacé (aucun modèle avant) : pas de retour arrière possible.
    with pytest.raises(ProposalError):
        auto_retrain.rollback(model_dir)
    events = [e["event"] for e in auto_retrain.read_history(model_dir)]
    assert events == ["run", "accepted", "run"]


def test_reject_keeps_installed_model(setup):
    db, model_dir, now = setup
    auto_retrain.run_auto_retrain(db, model_dir, horizons=(60,), now=now)
    auto_retrain.reject_proposal(model_dir, operator="test", note="attendre le réglage du PID")
    assert not (auto_retrain.Path(model_dir) / "training_report.json").exists()
    proposal = auto_retrain.load_json(auto_retrain.retrain_root(model_dir) / "proposal.json")
    assert proposal["status"] == "rejected" and proposal["note"] == "attendre le réglage du PID"


def test_accept_then_rollback_restores_previous(setup):
    db, model_dir, now = setup
    auto_retrain.run_auto_retrain(db, model_dir, horizons=(60,), now=now)
    auto_retrain.accept_proposal(model_dir)
    auto_retrain.run_auto_retrain(db, model_dir, horizons=(60, 300), now=now)   # 5 min plus long -> proposé
    auto_retrain.accept_proposal(model_dir)
    assert json.loads((auto_retrain.Path(model_dir) / "training_report.json").read_text())["horizon_seconds"] == 300
    report = auto_retrain.rollback(model_dir)
    assert report["horizon_seconds"] == 60
    assert json.loads((auto_retrain.Path(model_dir) / "training_report.json").read_text())["horizon_seconds"] == 60


def test_short_test_window_is_refused(setup, monkeypatch):
    """Juger un horizon sur une période de test trop courte ne prouve rien."""
    db, model_dir, now = setup
    monkeypatch.setattr(auto_retrain, "MIN_TEST_SPAN_HORIZONS", 1000)
    summary = auto_retrain.run_auto_retrain(db, model_dir, horizons=(300,), now=now)
    assert summary["decision"] == "none"
    assert "période de test trop courte" in summary["candidates"][0]["reason"]


def test_two_retrains_never_run_at_the_same_time(setup):
    db, model_dir, now = setup
    with auto_retrain._exclusive(auto_retrain.retrain_root(model_dir)):
        with pytest.raises(auto_retrain.RetrainBusy):
            auto_retrain.run_auto_retrain(db, model_dir, horizons=(60,), now=now)


def test_model_folders_are_private(setup):
    import stat
    db, model_dir, now = setup
    auto_retrain.run_auto_retrain(db, model_dir, horizons=(60,), now=now)
    for folder in (auto_retrain.Path(model_dir), auto_retrain.retrain_root(model_dir)):
        assert stat.S_IMODE(folder.stat().st_mode) == 0o700
