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


def test_print_summary_shows_latest_rows_and_exports_csv(tmp_path, capsys):
    from database import sqlite_client
    from database.summary import print_summary
    db = str(tmp_path / "v.db")
    sqlite_client.init_db(db)
    for i in range(3):
        sqlite_client.insert_reading(40.0, 34.0 + i, 39.0, 30.0, 2.5, 30.0, 15.0, db_path=db)
    csv_path = tmp_path / "export.csv"
    assert print_summary(db, rows=2, csv_path=str(csv_path)) == 0
    out = capsys.readouterr().out
    assert "Dernières mesures (2 sur 3)" in out and "36.00" in out
    assert len(csv_path.read_text().splitlines()) == 4          # en-tête + 3 lignes


def test_missing_database_is_never_created(tmp_path, capsys):
    from database.summary import print_summary
    missing = tmp_path / "absente.db"
    assert print_summary(str(missing)) == 1
    assert not missing.exists()


def test_coherence_flags_a_cold_probe_above_the_hot_one():
    from database.summary import coherence
    n = 200
    df = pd.DataFrame({
        "timestamp": [f"2026-10-18 05:{i // 60:02d}:{i % 60:02d}" for i in range(n)],
        "pt01": [34.0] * (n - 1) + [36.7],      # froid
        "pt02": [35.0] * n,                     # chaud, lue au degré près
        "pt03": [33.0] * n, "pt04": [32.0] * n,
    })
    text = "\n".join(coherence(df))
    assert "PT02 ≥ PT01 attendu : inversé sur 1 mesure(s)" in text and "1.7 °C" in text
    assert "troncature ou étalonnage" in text
    assert "PT03 ≥ PT04 attendu : toujours respecté" in text
    assert "PT02 : 100% de valeurs entières" in text


def test_around_shows_the_window_and_the_inversions(tmp_path):
    from database import sqlite_client
    from database.summary import around, print_summary
    db = str(tmp_path / "a.db")
    sqlite_client.init_db(db)
    for minute in range(30):
        pt01 = 36.7 if minute == 15 else 34.0
        sqlite_client.insert_reading(pt01, 35.0, 33.5, 33.0, 10.4, 30.0, 12.0,
                                     timestamp=f"2026-10-18 04:{50 + minute // 6:02d}:{(minute % 6) * 10:02d}",
                                     db_path=db)
    text = around(db, "2026-10-18 04:52:30", minutes=1)
    assert "04:52:30" in text and "<- instant demandé" in text and "-1.70" in text
    assert "04:50:00" not in text                       # hors de la fenêtre de ±1 min
    assert "Aucune mesure" in around(db, "2026-10-19 12:00", minutes=1)
    assert print_summary(db, at="pas une date") == 1
