"""
tests/test_sqlite_client.py — Vérifie que sqlite_client.py insère et relit
correctement une ligne d'instantané complet.

En clair : on utilise un fichier temporaire (pas la vraie base de production)
pour que ce test soit reproductible, isolé, et ne laisse aucune trace sur
le disque une fois terminé.
"""

import os
import sqlite3
import tempfile

import pytest

from database import sqlite_client


@pytest.fixture
def temp_db_path():
    """Fournit un chemin de base SQLite temporaire, supprimé après le test."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    # Nettoyage : SQLite en mode WAL peut créer des fichiers -wal et -shm à côté.
    for suffix in ("", "-wal", "-shm"):
        candidate = path + suffix
        if os.path.exists(candidate):
            os.remove(candidate)


def test_init_db_creates_table(temp_db_path):
    """init_db doit créer la table sans erreur, de façon idempotente."""
    sqlite_client.init_db(temp_db_path)
    # Appeler une deuxième fois ne doit pas lever d'erreur (IF NOT EXISTS).
    sqlite_client.init_db(temp_db_path)
    with sqlite_client.connect(temp_db_path) as conn:
        tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        assert {"telemetry", "predictions_log", "alerts_log"} <= tables
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert os.stat(temp_db_path).st_mode & 0o777 == 0o600  # base lisible par son propriétaire seulement


def test_insert_and_read_back(temp_db_path):
    """Insère une ligne factice, la relit, vérifie l'égalité des valeurs."""
    sqlite_client.init_db(temp_db_path)

    expected = {
        "pt01": 24.3,
        "pt02": 26.0,
        "pt03": 23.5,
        "pt04": 23.5,
        "ht01": 39.0,
        "freq_pompe": 0.0,
        "temp_outdoor": 12.9,
    }

    fixed_timestamp = "2026-09-13 12:00:00"
    sqlite_client.insert_reading(
        pt01=expected["pt01"],
        pt02=expected["pt02"],
        pt03=expected["pt03"],
        pt04=expected["pt04"],
        ht01=expected["ht01"],
        freq_pompe=expected["freq_pompe"],
        temp_outdoor=expected["temp_outdoor"],
        timestamp=fixed_timestamp,
        db_path=temp_db_path,
    )

    df = sqlite_client.get_dataframe(
        start_ts="2026-09-13 00:00:00",
        end_ts="2026-09-13 23:59:59",
        db_path=temp_db_path,
    )

    assert len(df) == 1
    row = df.iloc[0]
    for column, value in expected.items():
        assert row[column] == pytest.approx(value)
    assert row["timestamp"] == fixed_timestamp


def test_get_dataframe_empty_period_returns_empty(temp_db_path):
    """Une période sans données doit retourner un DataFrame vide, pas une erreur."""
    sqlite_client.init_db(temp_db_path)
    df = sqlite_client.get_dataframe(
        start_ts="2000-01-01",
        end_ts="2000-01-02",
        db_path=temp_db_path,
    )
    assert len(df) == 0


def test_prediction_monitoring_flow(temp_db_path):
    """Les prédictions sont journalisées puis complétées à partir des mesures réelles."""
    sqlite_client.init_db(temp_db_path)

    sqlite_client.insert_reading(
        pt01=24.0,
        pt02=26.0,
        pt03=23.5,
        pt04=23.5,
        ht01=39.0,
        freq_pompe=30.0,
        temp_outdoor=15.0,
        timestamp="2026-09-13 12:00:00",
        db_path=temp_db_path,
    )

    pred_id = sqlite_client.log_prediction(
        made_at="2026-09-13 11:59:00",
        target_ts="2026-09-13 12:00:00",
        horizon_seconds=60,
        model_name="random_forest",
        predicted_value=27.0,
        db_path=temp_db_path,
    )
    assert pred_id > 0

    completed = sqlite_client.complete_due_predictions(
        now_ts="2026-09-13 12:00:05",
        db_path=temp_db_path,
    )
    assert completed == 1

    with sqlite_client.connect(temp_db_path) as conn:
        row = conn.execute(
            "SELECT actual_value, error FROM predictions_log WHERE id = ?", (pred_id,)
        ).fetchone()
    assert row is not None
    assert row[0] == pytest.approx(26.0)
    assert row[1] == pytest.approx(1.0)

    rmse = sqlite_client.get_rolling_rmse(60, "random_forest", window=10, db_path=temp_db_path)
    assert rmse is not None
    assert rmse == pytest.approx(1.0)

def test_connect_commits_then_closes_the_connection(temp_db_path):
    """Le gestionnaire doit valider ET fermer : `with sqlite3.connect()` ne ferme jamais."""
    sqlite_client.init_db(temp_db_path)
    with sqlite_client.connect(temp_db_path) as conn:
        conn.execute("INSERT INTO telemetry (pt01) VALUES (1.0)")
    with pytest.raises(sqlite3.ProgrammingError):
        conn.execute("SELECT 1")  # connexion fermée
    with sqlite_client.connect(temp_db_path) as conn2:
        assert conn2.execute("SELECT COUNT(*) FROM telemetry").fetchone() == (1,)


def test_connect_rolls_back_on_error(temp_db_path):
    sqlite_client.init_db(temp_db_path)
    with pytest.raises(RuntimeError):
        with sqlite_client.connect(temp_db_path) as conn:
            conn.execute("INSERT INTO telemetry (pt01) VALUES (1.0)")
            raise RuntimeError("échec en cours de transaction")
    with sqlite_client.connect(temp_db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM telemetry").fetchone() == (0,)


def test_incomplete_telemetry_row_does_not_block_prediction_completion(temp_db_path):
    """Régression : une ligne dont pt02 est NULL (capteur absent) faisait échouer
    `complete_due_predictions` en entier, donc plus aucune prédiction n'était complétée."""
    sqlite_client.init_db(temp_db_path)
    sqlite_client.insert_reading(None, None, None, None, None, None, None,
                                 timestamp="2026-09-13 12:00:00", db_path=temp_db_path)
    sqlite_client.insert_reading(24.0, 26.0, 23.5, 23.5, 39.0, 30.0, 15.0,
                                 timestamp="2026-09-13 12:00:10", db_path=temp_db_path)
    sqlite_client.log_prediction("2026-09-13 11:59:00", "2026-09-13 12:00:00", 60,
                                 "random_forest", 27.0, db_path=temp_db_path)
    completed = sqlite_client.complete_due_predictions("2026-09-13 12:00:30", db_path=temp_db_path)
    assert completed == 1  # rapprochée de la ligne complète la plus proche


def test_prediction_without_any_measurement_stays_pending(temp_db_path):
    sqlite_client.init_db(temp_db_path)
    sqlite_client.log_prediction("2026-09-13 11:59:00", "2026-09-13 12:00:00", 60,
                                 "random_forest", 27.0, db_path=temp_db_path)
    assert sqlite_client.complete_due_predictions("2026-09-13 12:00:30", db_path=temp_db_path) == 0


def test_old_unmatched_predictions_are_not_rescanned_forever(temp_db_path):
    sqlite_client.init_db(temp_db_path)
    sqlite_client.log_prediction("2026-09-10 11:59:00", "2026-09-10 12:00:00", 60,
                                 "random_forest", 27.0, db_path=temp_db_path)
    sqlite_client.insert_reading(24.0, 26.0, 23.5, 23.5, 39.0, 30.0, 15.0,
                                 timestamp="2026-09-10 12:00:00", db_path=temp_db_path)
    # Échéance vieille de plus d'un jour : hors fenêtre de recherche (mesure pourtant présente).
    assert sqlite_client.complete_due_predictions("2026-09-13 12:00:30", db_path=temp_db_path) == 0


def test_rolling_rmse_stats_reports_the_real_sample_count(temp_db_path):
    sqlite_client.init_db(temp_db_path)
    assert sqlite_client.get_rolling_rmse_stats(60, "random_forest", db_path=temp_db_path) is None
    for minute in range(3):
        ts = f"2026-09-13 12:0{minute}:00"
        sqlite_client.insert_reading(24.0, 26.0, 23.5, 23.5, 39.0, 30.0, 15.0,
                                     timestamp=ts, db_path=temp_db_path)
        sqlite_client.log_prediction(ts, ts, 60, "random_forest", 28.0, db_path=temp_db_path)
    sqlite_client.complete_due_predictions("2026-09-13 12:10:00", db_path=temp_db_path)
    rmse, n = sqlite_client.get_rolling_rmse_stats(60, "random_forest", db_path=temp_db_path)
    assert n == 3 and rmse == pytest.approx(2.0)


def test_log_alert_never_raises_on_unusable_database(tmp_path):
    sqlite_client.log_alert("INFO", "regle", "message", email_sent=False,
                            db_path=str(tmp_path / "absent" / "x.db"))  # dossier inexistant
    assert not (tmp_path / "absent").exists()  # rien n'a été créé, aucune exception levée


def test_log_alert_persists_the_alert(temp_db_path):
    sqlite_client.init_db(temp_db_path)
    sqlite_client.log_alert("CRITIQUE", "critical_temp", "trop chaud", email_sent=True, db_path=temp_db_path)
    with sqlite_client.connect(temp_db_path) as conn:
        assert conn.execute("SELECT level, rule_name, email_sent FROM alerts_log").fetchone() == (
            "CRITIQUE", "critical_temp", 1)


def test_init_db_migrates_old_schema_without_losing_rows(tmp_path):
    """Une base créée avant la colonne temp_outdoor_source est migrée sans perte."""
    import sqlite3

    from database import sqlite_client

    path = str(tmp_path / "old.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE telemetry (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                     "timestamp DATETIME DEFAULT CURRENT_TIMESTAMP, pt01 REAL, pt02 REAL, pt03 REAL, "
                     "pt04 REAL, ht01 REAL, freq_pompe REAL, temp_outdoor REAL)")
        conn.execute("INSERT INTO telemetry (pt02) VALUES (30.0)")
    sqlite_client.init_db(path)
    sqlite_client.init_db(path)  # idempotent
    with sqlite_client.connect(path) as conn:
        rows = conn.execute("SELECT pt02, temp_outdoor_source FROM telemetry").fetchall()
    assert rows == [(30.0, None)]
