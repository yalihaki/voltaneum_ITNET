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


def _probes(n=200, pt01=34.3, pt02=35.0, pt03=33.4, pt04=32.6):
    return pd.DataFrame({
        "timestamp": [f"2026-10-18 05:{i // 60:02d}:{i % 60:02d}" for i in range(n)],
        "pt01": [pt01] * n, "pt02": [pt02] * n, "pt03": [pt03] * n, "pt04": [pt04] * n,
    })


def test_inversion_within_rounding_is_normal():
    """PT02 au degré, PT01 au dixième : une inversion de 0,4 °C s'explique par l'arrondi (cas réel :
    0,6 °C le 18/10 à 12:45, sous les 0,75 °C d'arrondi et de précision)."""
    from database.summary import coherence
    df = _probes()
    df.loc[50, "pt01"] = 35.4
    text = "\n".join(coherence(df))
    assert "PT02 ≥ PT01 attendu : inversé sur 1 mesure(s)" in text and "dans l'arrondi et la précision des sondes (jusqu'à 0.75 °C)" in text
    assert "PT03 ≥ PT04 attendu : toujours respecté" in text
    assert "PT02 : 100% de valeurs entières" in text and "PT01 :" not in text.split("respecté")[-1]


def test_inversion_beyond_rounding_points_to_a_transient_and_the_command():
    """PT03 / PT04 au dixième : 1,1 °C d'inversion n'est pas un arrondi (cas réel du 16/10 21:45)."""
    from database.summary import coherence
    df = _probes()
    df.loc[80, "pt04"] = 33.1
    df.loc[80, "pt03"] = 32.0
    text = "\n".join(coherence(df))
    assert "au pire de 1.1 °C" in text and "transitoire probable" in text
    assert 'vérifier : ./voltaneum data --around "2026-10-18 05:01"' in text
    assert "arrondi au degré" not in text


def test_power_is_summarized_as_a_ten_minute_mean():
    """Puissance instantanée de −2 à +7 kW (PT02 au degré) : le résumé donne la moyenne sur 10 min."""
    from database.summary import power_mean, summarize
    n = 200
    df = _probes(n)
    df["timestamp"] = pd.date_range("2026-10-20 19:00:00", periods=n, freq="15s").strftime("%Y-%m-%d %H:%M:%S")
    df["thermal_power_kw"] = [7.0 if i % 2 else -1.9 for i in range(n)]
    mean = power_mean(df)
    assert mean.iloc[:19].isna().all() and mean.iloc[50:].between(2.4, 2.7).all()
    power = next(line for line in summarize(df).splitlines() if line.startswith("Puissance"))
    assert "moy. 10 min" in power and "-1.9" not in power and "7.0" not in power


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
