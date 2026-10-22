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
    assert "humidité" not in mesures[0]                         # enregistrée, mais pas exportée pour l'instant
    assert len(mesures[0].split(";")) == len(mesures[1].split(";"))   # colonnes alignées


def test_export_only_some_elements(env, tmp_path):
    db, _ = env
    out = tmp_path / "exports"
    assert voltaneum.main(["export", "--db", db, "--only", "mesures,alertes", "--dir", str(out)]) == 0
    assert len(list(out.glob("*.csv"))) == 2
    assert voltaneum.main(["export", "--db", db, "--only", "bidon", "--dir", str(out)]) == 2


def test_copie_makes_a_readable_copy_and_prints_the_scp_command(env, capsys):
    """La copie doit contenir les écritures récentes (mode WAL) et se relire telle quelle."""
    import gzip
    import sqlite3
    import types
    db, tmp_path = env
    folder = tmp_path / "copies"
    for _ in range(voltaneum.COPIE_RETENTION + 1):
        assert voltaneum.cmd_copie(types.SimpleNamespace(db=db, dir=str(folder), mail=False)) == 0
    archives = sorted(folder.glob("voltaneum-*.db.gz"))
    assert 1 <= len(archives) <= voltaneum.COPIE_RETENTION              # pas d'accumulation
    plain = tmp_path / "relue.db"
    plain.write_bytes(gzip.decompress(archives[-1].read_bytes()))
    with sqlite3.connect(plain) as conn:
        assert conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0] == _rows(db)
    out = capsys.readouterr().out
    assert "scp " in out and "gunzip " in out and "./voltaneum data --db" in out and "--mail" in out


def test_copie_refuses_a_missing_database(tmp_path, capsys):
    import types
    assert voltaneum.cmd_copie(types.SimpleNamespace(db=str(tmp_path / "absente.db"), dir=str(tmp_path), mail=True)) == 1


def test_copie_by_mail_attaches_the_archive(env, monkeypatch, capsys):
    """VPS injoignable depuis le poste : la copie part en pièce jointe par le SMTP des alertes."""
    import types
    from unittest.mock import MagicMock

    from notifications import alerts
    db, tmp_path = env
    for name, value in {"SMTP_HOST": "smtp.example.org", "SMTP_PORT": 587, "SMTP_USER": "u", "SMTP_PASSWORD": "p",
                        "ALERT_EMAIL_FROM": "voltaneum@example.org", "ALERT_EMAIL_TO": "moi@example.org"}.items():
        monkeypatch.setattr(config, name, value)
    smtp_cls = MagicMock()
    monkeypatch.setattr(alerts.smtplib, "SMTP", smtp_cls)
    assert voltaneum.cmd_copie(types.SimpleNamespace(db=db, dir=str(tmp_path / "c"), mail=True)) == 0
    message = smtp_cls.return_value.__enter__.return_value.send_message.call_args.args[0]
    (attachment,) = list(message.iter_attachments())
    assert attachment.get_filename().endswith(".db.gz") and message["To"] == "moi@example.org"
    assert "Envoyée par e-mail à moi@example.org" in capsys.readouterr().out


def test_copie_by_mail_without_smtp_says_why(env, monkeypatch, capsys):
    import types
    db, tmp_path = env
    monkeypatch.setattr(config, "SMTP_HOST", None)
    assert voltaneum.cmd_copie(types.SimpleNamespace(db=db, dir=str(tmp_path / "c"), mail=True)) == 1
    assert "E-mail NON envoyé : configuration e-mail incomplète" in capsys.readouterr().out


def test_copie_too_big_for_a_mail_is_refused(env, monkeypatch, capsys):
    import types
    db, tmp_path = env
    monkeypatch.setattr(voltaneum, "COPIE_MAIL_MAX_MB", 0)
    assert voltaneum.cmd_copie(types.SimpleNamespace(db=db, dir=str(tmp_path / "c"), mail=True)) == 1
    assert "Trop volumineuse" in capsys.readouterr().out
