"""Tests du résumé min / max de check_data.py."""

import pandas as pd

from database.summary import summarize


def _df():
    return pd.DataFrame({
        "timestamp": ["2026-10-12 09:00:00", "2026-10-12 10:00:00", "2026-10-12 11:00:00"],
        "pt01": [33.6, 31.2, 35.9],
        "pt02": [34.0, 34.0, 34.0],
        "ht01": [None, None, None],
        "temp_outdoor": [12.8, 18.7, None],
        "temp_outdoor_source": ["weather", "sensor", None],
    })


def _line(text, label):
    return next(line for line in text.splitlines() if line.startswith(label))


def test_min_and_max_with_their_timestamps():
    pt01 = _line(summarize(_df()), "PT01")
    assert "31.2 °C" in pt01 and "2026-10-12 10:00:00" in pt01      # minimum
    assert "35.9 °C" in pt01 and "2026-10-12 11:00:00" in pt01      # maximum
    assert "n'a jamais varié" not in pt01


def test_constant_measure_is_flagged():
    assert "n'a jamais varié" in _line(summarize(_df()), "PT02")


def test_missing_values_and_outdoor_sources():
    text = summarize(_df())
    assert "aucune valeur" in _line(text, "HT01")
    assert "1 mesure(s) du capteur, 1 de l'API météo, 1 absente(s)" in text
