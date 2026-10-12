"""
    Tests unitaires pour les fonctions de sauvegarde et de restauration de la base de données SQLite.
    Vérifie que la sauvegarde est créée, que l'intégrité est vérifiée et que la restauration fonctionne correctement.
    Utilise un fichier de base de données temporaire pour éviter toute interférence avec la base de production.
    Les tests sont isolés et reproductibles, et ne laissent aucune trace sur le disque après l'exécution.
    
    Usage : pytest tests/test_backup.py

"""

import sqlite3
import stat
import threading

import pytest

from database import sqlite_client
from database.backup import create_backup, restore_backup, verify_backup


def test_backup_restore_and_integrity(tmp_path):
    source = tmp_path / "source.db"
    restored = tmp_path / "restored.db"
    backup_dir = tmp_path / "backups"
    sqlite_client.init_db(str(source))
    sqlite_client.insert_reading(
        24.0, 26.0, 23.5, 23.5, 39.0, 30.0, 15.0,
        timestamp="2026-09-17 12:00:00", db_path=str(source)
    )

    archive = create_backup(str(source), str(backup_dir), retention_count=2)
    assert archive.exists()
    assert verify_backup(str(archive)) is True
    restore_backup(str(archive), str(restored))
    with sqlite3.connect(restored) as connection:
        assert connection.execute("SELECT COUNT(*) FROM telemetry").fetchone() == (1,)

def _populated_db(path, rows=3):
    sqlite_client.init_db(str(path))
    for i in range(rows):
        sqlite_client.insert_reading(24.0, 26.0, 23.5, 23.5, 39.0, 30.0, 15.0,
                                     timestamp=f"2026-09-17 12:00:0{i}", db_path=str(path))


def test_backup_files_and_directory_are_private(tmp_path):
    """Les sauvegardes contiennent toutes les mesures : jamais lisibles par un autre utilisateur."""
    source = tmp_path / "s.db"
    _populated_db(source)
    archive = create_backup(str(source), str(tmp_path / "bk"), retention_count=3)
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(archive.parent.stat().st_mode) == 0o700


def test_no_temporary_files_are_left_behind(tmp_path):
    """Régression : la vérification laissait des fichiers `.verify.db...-wal/-shm` dans le dossier."""
    source = tmp_path / "s.db"
    _populated_db(source)
    archive = create_backup(str(source), str(tmp_path / "bk"))
    assert verify_backup(str(archive)) is True
    assert [p.name for p in archive.parent.iterdir()] == [archive.name]


def test_retention_keeps_only_the_most_recent_backups(tmp_path):
    source = tmp_path / "s.db"
    _populated_db(source)
    backup_dir = tmp_path / "bk"
    backup_dir.mkdir()
    for stamp in ("20200101T000000Z", "20200102T000000Z", "20200103T000000Z"):
        (backup_dir / f"voltaneum-{stamp}.db.gz").write_bytes(b"ancienne")
    create_backup(str(source), str(backup_dir), retention_count=2)
    names = sorted(p.name for p in backup_dir.glob("*.db.gz"))
    assert len(names) == 2 and "voltaneum-20200101T000000Z.db.gz" not in names


def _fake_archive(directory, days_old: int):
    """Crée une fausse archive datée de `days_old` jours (l'âge est lu dans le nom du fichier)."""
    from datetime import datetime, timedelta, timezone
    created = datetime.now(timezone.utc) - timedelta(days=days_old)
    path = directory / f"voltaneum-{created.strftime('%Y%m%dT%H%M%SZ')}.db.gz"
    path.write_bytes(b"x")
    return path


def test_archives_younger_than_the_minimum_age_are_never_deleted(tmp_path):
    """Exigence : données conservées au moins un an. Même au-delà de `retention_count`,
    une archive de moins de `min_age_days` jours ne doit JAMAIS être supprimée."""
    source = tmp_path / "s.db"
    _populated_db(source)
    backup_dir = tmp_path / "bk"
    backup_dir.mkdir()
    young = [_fake_archive(backup_dir, age) for age in (10, 30, 100, 200, 300, 364)]
    create_backup(str(source), str(backup_dir), retention_count=1, min_age_days=365)
    assert all(path.exists() for path in young)


def test_only_archives_beyond_the_count_and_older_than_the_minimum_age_are_deleted(tmp_path):
    source = tmp_path / "s.db"
    _populated_db(source)
    backup_dir = tmp_path / "bk"
    backup_dir.mkdir()
    old = [_fake_archive(backup_dir, age) for age in (366, 500, 800)]
    kept = [_fake_archive(backup_dir, age) for age in (100, 300)]
    create_backup(str(source), str(backup_dir), retention_count=1, min_age_days=365)
    assert not any(path.exists() for path in old)
    assert all(path.exists() for path in kept)


def test_the_most_recent_archives_are_kept_even_when_older_than_the_minimum_age(tmp_path):
    """`retention_count` reste un plancher en nombre : les N plus récentes sont toujours gardées."""
    source = tmp_path / "s.db"
    _populated_db(source)
    backup_dir = tmp_path / "bk"
    backup_dir.mkdir()
    very_old = [_fake_archive(backup_dir, age) for age in (400, 500, 600)]
    create_backup(str(source), str(backup_dir), retention_count=4, min_age_days=365)
    assert all(path.exists() for path in very_old)   # 4 plus récentes = la nouvelle + ces 3


def test_unreadable_archive_name_is_kept(tmp_path):
    source = tmp_path / "s.db"
    _populated_db(source)
    backup_dir = tmp_path / "bk"
    backup_dir.mkdir()
    odd = backup_dir / "voltaneum-pas-une-date.db.gz"
    odd.write_bytes(b"x")
    for age in (400, 500):
        _fake_archive(backup_dir, age)
    create_backup(str(source), str(backup_dir), retention_count=1, min_age_days=0)
    assert odd.exists()


def test_restore_discards_the_stale_wal_of_the_replaced_database(tmp_path):
    """Un ancien `-wal` rejoué sur la base restaurée la corromprait (ou en changerait le contenu)."""
    source = tmp_path / "s.db"
    _populated_db(source, rows=3)
    archive = create_backup(str(source), str(tmp_path / "bk"))
    live = tmp_path / "live.db"
    _populated_db(live, rows=10)
    live_wal = tmp_path / "live.db-wal"
    live_wal.write_bytes(b"journal de l'ancienne base")
    restore_backup(str(archive), str(live))
    assert not live_wal.exists()
    with sqlite3.connect(live) as connection:
        assert connection.execute("SELECT COUNT(*) FROM telemetry").fetchone() == (3,)


@pytest.mark.parametrize("payload", [b"pas du gzip", b""])
def test_verify_rejects_corrupted_archives(tmp_path, payload):
    bad = tmp_path / "voltaneum-x.db.gz"
    bad.write_bytes(payload)
    assert verify_backup(str(bad)) is False


def test_verify_rejects_a_truncated_archive(tmp_path):
    source = tmp_path / "s.db"
    _populated_db(source)
    archive = create_backup(str(source), str(tmp_path / "bk"))
    archive.write_bytes(archive.read_bytes()[: archive.stat().st_size // 2])
    assert verify_backup(str(archive)) is False


def test_failed_restore_keeps_the_existing_database(tmp_path):
    live = tmp_path / "live.db"
    _populated_db(live, rows=4)
    bad = tmp_path / "bad.db.gz"
    bad.write_bytes(b"corrompu")
    with pytest.raises(Exception):
        restore_backup(str(bad), str(live))
    with sqlite3.connect(live) as connection:
        assert connection.execute("SELECT COUNT(*) FROM telemetry").fetchone() == (4,)
    assert not list(tmp_path.glob("*.restore.tmp*"))


def test_backup_is_consistent_during_concurrent_writes(tmp_path):
    source = tmp_path / "s.db"
    _populated_db(source)
    stop = threading.Event()

    def writer():
        while not stop.is_set():
            sqlite_client.insert_reading(1, 2, 3, 4, 5, 6, 7, db_path=str(source))

    thread = threading.Thread(target=writer)
    thread.start()
    try:
        archive = create_backup(str(source), str(tmp_path / "bk"))
    finally:
        stop.set()
        thread.join()
    assert verify_backup(str(archive)) is True
