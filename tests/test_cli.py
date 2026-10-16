"""Tests du point d'entrée unique (voltaneum.py) : verrou d'instance unique et restauration."""

import pytest

import config
import voltaneum
from database import sqlite_client
from database.backup import create_backup


@pytest.fixture
def env(tmp_path, monkeypatch):
    db = str(tmp_path / "v.db")
    monkeypatch.setattr(config, "SQLITE_DB_PATH", db)
    monkeypatch.setattr(config, "STORAGE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(voltaneum, "LOG_DIR", tmp_path / "logs")
    sqlite_client.init_db(db)
    sqlite_client.insert_reading(40, 35, 39, 30, 2.5, 30, 15, db_path=db)
    return db, tmp_path


def _rows(db):
    with sqlite_client.connect(db) as conn:
        return conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]


def test_single_instance_lock(env):
    first = voltaneum._single_instance_lock()
    assert first is not None
    assert voltaneum._single_instance_lock() is None          # deuxième processus refusé
    first.close()
    assert voltaneum._single_instance_lock() is not None


def test_restore_lists_then_restores_and_keeps_a_safety_copy(env, capsys):
    db, tmp = env
    archive = create_backup(db, str(tmp / "backups" / "daily"), 7, 0)
    sqlite_client.insert_reading(41, 36, 39, 30, 2.5, 30, 15, db_path=db)   # modification après sauvegarde
    assert _rows(db) == 2

    assert voltaneum.main(["restore"]) == 0
    assert archive.name in capsys.readouterr().out

    assert voltaneum.main(["restore", archive.name, "--yes"]) == 0
    assert _rows(db) == 1                                                     # état de la sauvegarde
    assert list((tmp / "backups" / "avant-restauration").glob("*.db.gz"))     # base remplacée conservée


def test_restore_refused_while_voltaneum_runs(env):
    db, tmp = env
    archive = create_backup(db, str(tmp / "backups" / "daily"), 7, 0)
    running = voltaneum._single_instance_lock()
    try:
        assert voltaneum.main(["restore", archive.name, "--yes"]) == 1
    finally:
        running.close()


def test_export_writes_one_csv_per_element(env, capsys, tmp_path):
    db, _ = env
    out = tmp_path / "exports"
    assert voltaneum.main(["export", "--db", db, "--days", "1", "--dir", str(out)]) == 0
    files = sorted(p.name.split("-")[1] for p in out.glob("*.csv"))
    assert files == ["alertes", "consignes", "entrainements", "mesures", "predictions"]
    mesures = next(out.glob("voltaneum-mesures-*.csv")).read_text(encoding="utf-8-sig").splitlines()
    assert mesures[0].startswith("date UTC;heure locale;PT01") and len(mesures) == 2


def test_export_only_some_elements(env, tmp_path):
    db, _ = env
    out = tmp_path / "exports"
    assert voltaneum.main(["export", "--db", db, "--only", "mesures,alertes", "--dir", str(out)]) == 0
    assert len(list(out.glob("*.csv"))) == 2
    assert voltaneum.main(["export", "--db", db, "--only", "bidon", "--dir", str(out)]) == 2
