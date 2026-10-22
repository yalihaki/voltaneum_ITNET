"""Tests de database/quality.py : les pics isolés sont reconnus, les vraies évolutions jamais."""

import pandas as pd
import pytest

from database import sqlite_client
from database.quality import drop_spike_points, drop_spikes, is_spike, spike_mask


class TestRealCases:
    """Relevés réels du contrôleur (./voltaneum data --around)."""

    def test_pt01_spike_of_18_october(self):
        # 05:07:35 → 05:07:50 → 05:08:05
        assert is_spike(34.6, 36.7, 34.6)

    def test_pt04_spike_of_16_october(self):
        # 17:21:01 → 17:21:16 → 17:21:31
        assert is_spike(32.9, 35.2, 33.0)

    def test_dry_cooler_fan_cycle_is_a_real_change(self):
        """PT04 chute de 2 à 4 °C en 30 s quand le ventilateur démarre : elle ne revient pas, ce
        n'est pas un pic (16/10 17:11 et 18/10 05:02)."""
        series = pd.Series([34.2, 32.0, 29.8, 30.2, 30.9])
        assert not spike_mask(series).any()
        assert not is_spike(33.4, 32.6, 30.5)

    def test_one_degree_steps_of_pt02_are_kept(self):
        """PT02 est lue au degré près : 34 → 35 → 34 est sous le seuil de 1,5 °C."""
        assert not spike_mask(pd.Series([34.0, 35.0, 34.0, 35.0, 35.0])).any()


def test_a_lasting_rise_is_never_a_spike():
    assert not spike_mask(pd.Series([35.0, 37.5, 37.6, 37.4])).any()


def test_downward_spike_is_also_detected():
    assert is_spike(34.6, 32.0, 34.5)


def test_missing_values_are_skipped_not_compared():
    mask = spike_mask(pd.Series([34.6, None, 36.7, 34.6]))
    assert mask.tolist() == [False, False, True, False]


def test_drop_spikes_blanks_only_the_faulty_column():
    df = pd.DataFrame({"timestamp": ["t0", "t1", "t2"], "pt01": [34.6, 36.7, 34.6], "pt02": [35.0, 35.0, 35.0]})
    clean, counts = drop_spikes(df)
    assert counts["pt01"] == 1 and counts["pt02"] == 0
    assert pd.isna(clean.loc[1, "pt01"]) and clean.loc[1, "pt02"] == 35.0
    assert df.loc[1, "pt01"] == 36.7                                  # l'original n'est pas modifié


def test_last_point_without_follower_is_kept():
    points = [(0.0, 34.6), (15.0, 36.7), (30.0, 34.6), (45.0, 38.0)]
    assert drop_spike_points(points) == [(0.0, 34.6), (30.0, 34.6), (45.0, 38.0)]


def test_recent_values_used_by_the_trends_have_no_spikes(tmp_path):
    db = str(tmp_path / "q.db")
    sqlite_client.init_db(db)
    for i, value in enumerate((34.6, 34.6, 36.7, 34.6, 34.7)):
        sqlite_client.insert_reading(value, 35.0, 34.0, 33.0, 10.4, 30.0, 12.0,
                                     timestamp=f"2026-10-18 05:07:{i * 10:02d}", db_path=db)
    since = pd.Timestamp("2026-10-18 05:00:00", tz="UTC").timestamp()
    assert [v for _, v in sqlite_client.recent_values("pt01", since, db_path=db)] == [34.6, 34.6, 34.6, 34.7]
    assert 36.7 in [v for _, v in sqlite_client.recent_values("pt01", since, db_path=db, without_spikes=False)]


def test_summary_reports_and_excludes_spikes():
    from database.summary import summarize
    df = pd.DataFrame({
        "timestamp": [f"2026-10-18 05:07:{i * 10:02d}" for i in range(5)],
        "pt01": [34.6, 34.6, 36.7, 34.6, 34.7], "pt02": [35.0] * 5,
    })
    text = summarize(df)
    pt01 = next(line for line in text.splitlines() if line.startswith("PT01"))
    assert "36.7" not in pt01 and "34.7 °C" in pt01                     # le pic n'est plus le maximum
    assert "PT01 : 1 pic(s), écartés des statistiques — 2026-10-18 05:07:20 (36.7 °C)" in text


def test_training_ignores_spikes(caplog):
    import logging

    from ml.train import prepare_frame
    from tests.test_train import make_synthetic_dataframe
    df = make_synthetic_dataframe(n=400)
    df.loc[200, "pt02"] += 5.0                                         # un pic isolé
    with caplog.at_level(logging.INFO, logger="ml.train"):
        frame = prepare_frame(df, 300)
    assert "Pics isolés écartés de l'apprentissage : PT02 1" in caplog.text
    assert frame["pt02"].max() < df["pt02"].max()


@pytest.mark.parametrize("threshold", [0.5, 3.0])
def test_threshold_is_respected(threshold):
    assert is_spike(34.0, 36.0, 34.0, threshold=threshold) is (threshold < 2.0)
