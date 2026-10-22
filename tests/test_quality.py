"""Tests de database/quality.py : les pics isolés sont reconnus, les vraies évolutions jamais."""

import pandas as pd
import pytest

from database import sqlite_client
from database.quality import drop_spike_points, drop_spikes, is_spike, spike_mask


def _flags(values):
    return spike_mask(pd.Series(values)).tolist()


class TestRealCases:
    """Relevés réels du contrôleur (12 au 22/10/2026), vérifiés avec ./voltaneum data --around."""

    def test_pt01_spike_of_18_october(self):
        assert _flags([34.6, 34.6, 36.7, 34.6, 34.6]) == [False, False, True, False, False]

    def test_pt04_spike_of_16_october(self):
        assert is_spike(32.7, 32.9, 35.2, 33.0)

    def test_pt03_spike_of_1_4_degree_missed_by_the_former_rule(self):
        """16/10 21:45:22 : 33,4 → 32,0 → 33,5 (1,4 °C, sous l'ancien seuil de 1,5 °C)."""
        assert is_spike(33.4, 33.4, 32.0, 33.5)

    def test_spike_while_pt04_is_already_rising(self):
        """13/10 04:57:50 : la température remonte déjà après un démarrage du ventilateur."""
        assert _flags([31.3, 31.7, 33.7, 32.2, 32.4]) == [False, False, True, False, False]

    def test_bottom_of_a_fan_cycle_is_a_real_change(self):
        """14/10 06:06:25 : descente en V au démarrage du ventilateur, la mesure d'avant chutait déjà."""
        assert not any(_flags([33.7, 31.2, 29.7, 30.4, 30.9]))

    def test_fan_drop_is_a_real_change(self):
        assert not any(_flags([34.2, 34.2, 32.0, 29.8, 30.2, 30.9]))

    def test_one_degree_steps_of_pt02_are_kept(self):
        """PT02 est lue au degré près : 34 → 35 → 34 s'écarte de 1,0 °C, pas de PLUS de 1,0 °C."""
        assert not any(_flags([34.0, 34.0, 35.0, 34.0, 35.0, 35.0]))


def test_a_lasting_rise_is_never_a_spike():
    assert not any(_flags([35.0, 35.0, 37.5, 37.6, 37.4]))


def test_downward_spike_is_also_detected():
    assert is_spike(34.6, 34.6, 32.0, 34.5)


def test_missing_values_are_skipped_not_compared():
    assert _flags([34.6, 34.6, None, 36.7, 34.6]) == [False, False, False, True, False]


def test_drop_spikes_blanks_only_the_faulty_column():
    df = pd.DataFrame({"timestamp": ["t0", "t1", "t2", "t3"], "pt01": [34.6, 34.6, 36.7, 34.6],
                       "pt02": [35.0, 35.0, 35.0, 35.0]})
    clean, counts = drop_spikes(df)
    assert counts["pt01"] == 1 and counts["pt02"] == 0
    assert pd.isna(clean.loc[2, "pt01"]) and clean.loc[2, "pt02"] == 35.0
    assert df.loc[2, "pt01"] == 36.7                                  # l'original n'est pas modifié


def test_last_point_without_follower_is_kept():
    points = [(0.0, 34.6), (15.0, 34.6), (30.0, 36.7), (45.0, 34.6), (60.0, 38.0)]
    assert drop_spike_points(points) == [(0.0, 34.6), (15.0, 34.6), (45.0, 34.6), (60.0, 38.0)]


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
    """Un pic ajouté à des données synthétiques (bruit gaussien : quelques pics « naturels » aussi)
    est écarté de l'apprentissage."""
    import logging

    from ml.train import prepare_frame
    from tests.test_train import make_synthetic_dataframe
    df = make_synthetic_dataframe(n=400)
    natural = drop_spikes(df)[1]["pt02"]
    df.loc[200, "pt02"] += 5.0                                         # un pic isolé de plus
    assert drop_spikes(df)[1]["pt02"] == natural + 1
    with caplog.at_level(logging.INFO, logger="ml.train"):
        frame = prepare_frame(df, 300)
    assert f"PT02 {natural + 1}" in caplog.text
    assert frame["pt02"].max() < df["pt02"].max()


@pytest.mark.parametrize("threshold", [0.5, 3.0])
def test_threshold_is_respected(threshold):
    assert is_spike(34.0, 34.0, 36.0, 34.0, threshold=threshold) is (threshold < 2.0)
