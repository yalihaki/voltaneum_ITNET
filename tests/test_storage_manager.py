"""Tests de la protection automatique de l'espace disque (database/storage_manager.py)."""

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import config
from database import sqlite_client, storage_manager
from database.storage_manager import (
    StorageManager,
    database_size_bytes,
    live_data_bytes,
    purge_oldest_until_size,
)

ROWS = 20_000


def _fill(db_path: str, rows: int = ROWS, age_days: int = 500) -> None:
    """Remplit `telemetry` avec `rows` lignes espacées d'une minute, la plus récente ayant `age_days` jours."""
    newest = datetime.now(timezone.utc) - timedelta(days=age_days)
    stamps = [(newest - timedelta(minutes=i)).strftime("%Y-%m-%d %H:%M:%S") for i in reversed(range(rows))]
    sqlite_client.init_db(db_path)
    with sqlite_client.connect(db_path) as conn:
        conn.executemany(
            "INSERT INTO telemetry (timestamp, pt01, pt02, pt03, pt04, ht01, freq_pompe, temp_outdoor) "
            "VALUES (?, 25.0, 30.0, 24.0, 23.0, 39.0, 5.0, 15.0)",
            [(s,) for s in stamps],
        )
    with sqlite_client.connect(db_path) as conn:
        conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")  # taille mesurée = fichier principal seul


def _count(db_path: str) -> int:
    with sqlite_client.connect(db_path) as conn:
        return conn.execute("SELECT COUNT(*) FROM telemetry").fetchone()[0]


@pytest.fixture
def db(tmp_path, monkeypatch) -> str:
    path = str(tmp_path / "t.db")
    monkeypatch.setattr(config, "RETENTION_DAYS", 30)
    monkeypatch.setattr(config, "PREDICTIONS_RETENTION_DAYS", 30)
    monkeypatch.setattr(config, "ALERTS_RETENTION_DAYS", 30)
    monkeypatch.setattr(config, "STORAGE_BACKUP_DIR", str(tmp_path / "backups"))
    monkeypatch.setattr(config, "STORAGE_PURGE_BATCH_SIZE", 500)
    _fill(path)
    return path


class FakeAlerts:
    def __init__(self):
        self.sent: list[tuple[str, str, str]] = []

    def __call__(self, subject, body, level):
        self.sent.append((subject, body, level))
        return True


def _manager(db_path, monkeypatch, fill_ratio=0.95):
    """Manager dont la limite place la base à `fill_ratio` de sa capacité, avec horloge contrôlable."""
    limit = int(database_size_bytes(db_path) / fill_ratio)
    monkeypatch.setattr(config, "STORAGE_LIMIT_BYTES", limit)
    clock = {"t": 1_000_000.0}
    alerts = FakeAlerts()
    manager = StorageManager(db_path, alert_sender=alerts, time_fn=lambda: clock["t"])
    return manager, clock, alerts


class TestPurge:
    def test_removes_only_what_is_needed(self, db):
        """Régression majeure : la boucle mesurait la taille du FICHIER (qui ne rétrécit
        pas avant VACUUM) et supprimait donc TOUT ce qui était éligible."""
        target = int(live_data_bytes(db) * 0.7)
        deleted = purge_oldest_until_size(db, target)
        remaining = _count(db)
        assert 0 < deleted < ROWS
        assert remaining == ROWS - deleted
        assert 0.5 * ROWS < remaining < 0.9 * ROWS     # ~70 % conservés, pas zéro
        assert live_data_bytes(db) <= target

    def test_deletes_the_oldest_rows_first(self, db):
        purge_oldest_until_size(db, int(live_data_bytes(db) * 0.5))
        with sqlite_client.connect(db) as conn:
            oldest_kept = conn.execute("SELECT MIN(timestamp) FROM telemetry").fetchone()[0]
            newest = conn.execute("SELECT MAX(timestamp) FROM telemetry").fetchone()[0]
        span_days = (datetime.fromisoformat(newest) - datetime.fromisoformat(oldest_kept)).total_seconds() / 86400
        assert span_days < ROWS / 1440 * 0.6           # on garde la partie RÉCENTE (~50 %)

    def test_file_is_actually_shrunk_by_vacuum(self, db):
        before = database_size_bytes(db)
        purge_oldest_until_size(db, int(live_data_bytes(db) * 0.5))
        assert database_size_bytes(db) < before * 0.7

    def test_nothing_is_deleted_when_already_under_target(self, db):
        assert purge_oldest_until_size(db, live_data_bytes(db) * 2) == 0
        assert _count(db) == ROWS

    def test_rows_younger_than_the_floor_are_never_deleted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "RETENTION_DAYS", 400)
        path = str(tmp_path / "recent.db")
        _fill(path, age_days=10)                        # plus récent que le plancher de 400 jours
        assert purge_oldest_until_size(path, 1) == 0
        assert _count(path) == ROWS

    def test_purges_predictions_and_alerts_after_telemetry(self, db):
        with sqlite_client.connect(db) as conn:
            conn.execute("DELETE FROM telemetry")
            conn.executemany(
                "INSERT INTO predictions_log (made_at, target_ts, horizon_seconds, model_name, predicted_value) "
                "VALUES (datetime('now', '-200 days'), datetime('now', '-200 days'), 600, 'random_forest', 30.0)",
                [()] * 3000,
            )
            conn.executemany(
                "INSERT INTO alerts_log (timestamp, level, rule_name, message) "
                "VALUES (datetime('now', '-200 days'), 'INFO', 'r', 'm')", [()] * 3000)
        assert purge_oldest_until_size(db, 1) == 6000    # tout est éligible et la cible est inatteignable


class TestManager:
    def test_warning_then_grace_then_purge_with_backup(self, db, monkeypatch):
        manager, clock, alerts = _manager(db, monkeypatch)

        first = manager.check_once()
        assert first["warning_sent"] is True and first["purged"] == 0
        assert [level for _, _, level in alerts.sent] == ["ATTENTION"]

        clock["t"] += config.STORAGE_GRACE_PERIOD_SECONDS - 1
        assert manager.check_once()["purged"] == 0       # délai de grâce non écoulé

        clock["t"] += 2
        result = manager.check_once()
        assert 0 < result["purged"] < ROWS
        assert 0 < _count(db) < ROWS
        assert database_size_bytes(db) <= manager.purge_target_bytes
        assert alerts.sent[-1][2] == "CRITIQUE" and "storage_auto_purge" in alerts.sent[-1][0]
        assert list(Path(config.STORAGE_BACKUP_DIR).glob("*.db.gz"))  # sauvegarde faite AVANT la purge

    def test_nothing_happens_below_the_warning_threshold(self, db, monkeypatch):
        manager, _, alerts = _manager(db, monkeypatch, fill_ratio=0.3)
        assert manager.check_once() == {"size_bytes": database_size_bytes(db), "warning_sent": False, "purged": 0}
        assert alerts.sent == []

    def test_backup_failure_cancels_the_purge_and_alerts(self, db, monkeypatch):
        manager, clock, alerts = _manager(db, monkeypatch)
        monkeypatch.setattr(storage_manager, "create_backup", lambda *a: (_ for _ in ()).throw(OSError("disque plein")))
        manager.check_once()
        clock["t"] += config.STORAGE_GRACE_PERIOD_SECONDS + 1
        assert manager.check_once()["purged"] == 0
        assert _count(db) == ROWS                         # AUCUNE donnée supprimée
        assert alerts.sent[-1][2] == "CRITIQUE" and "Sauvegarde" in alerts.sent[-1][0]

    def test_backup_receives_the_one_year_minimum_age(self, db, monkeypatch):
        """La purge doit créer sa sauvegarde avec le plancher d'âge de STORAGE_BACKUP_MIN_AGE_DAYS."""
        manager, clock, _ = _manager(db, monkeypatch)
        received = {}
        real_create_backup = storage_manager.create_backup

        def spy(db_path, backup_dir, retention_count, min_age_days):
            received["min_age_days"] = min_age_days
            return real_create_backup(db_path, backup_dir, retention_count, min_age_days)

        monkeypatch.setattr(storage_manager, "create_backup", spy)
        manager.check_once()
        clock["t"] += config.STORAGE_GRACE_PERIOD_SECONDS + 1
        manager.check_once()
        assert received["min_age_days"] == config.STORAGE_BACKUP_MIN_AGE_DAYS == 365

    def test_insufficient_backup_space_cancels_the_purge_and_alerts(self, db, monkeypatch):
        """Volume de sauvegarde trop petit : aucune donnée supprimée, alerte CRITIQUE explicite."""
        from collections import namedtuple
        usage = namedtuple("usage", "total used free")
        manager, clock, alerts = _manager(db, monkeypatch)
        monkeypatch.setattr(storage_manager.shutil, "disk_usage", lambda path: usage(10, 10, 0))
        manager.check_once()
        clock["t"] += config.STORAGE_GRACE_PERIOD_SECONDS + 1
        assert manager.check_once()["purged"] == 0
        assert _count(db) == ROWS
        assert alerts.sent[-1][2] == "CRITIQUE" and "Sauvegarde" in alerts.sent[-1][0]
        assert "espace libre insuffisant" in alerts.sent[-1][1]
        assert not list(Path(config.STORAGE_BACKUP_DIR).glob("*.db.gz"))

    def test_inaccessible_backup_volume_cancels_the_purge(self, db, monkeypatch):
        manager, clock, alerts = _manager(db, monkeypatch)

        def broken(path):
            raise OSError("volume démonté")

        monkeypatch.setattr(storage_manager.shutil, "disk_usage", broken)
        manager.check_once()
        clock["t"] += config.STORAGE_GRACE_PERIOD_SECONDS + 1
        assert manager.check_once()["purged"] == 0
        assert _count(db) == ROWS
        assert "inaccessible" in alerts.sent[-1][1]

    def test_invalid_backup_cancels_the_purge(self, db, monkeypatch):
        manager, clock, alerts = _manager(db, monkeypatch)
        monkeypatch.setattr(storage_manager, "verify_backup", lambda path: False)
        manager.check_once()
        clock["t"] += config.STORAGE_GRACE_PERIOD_SECONDS + 1
        assert manager.check_once()["purged"] == 0
        assert _count(db) == ROWS
        assert alerts.sent[-1][2] == "CRITIQUE"

    def test_floor_blocking_is_reported_distinctly(self, tmp_path, monkeypatch):
        monkeypatch.setattr(config, "RETENTION_DAYS", 400)
        monkeypatch.setattr(config, "STORAGE_BACKUP_DIR", str(tmp_path / "backups"))
        path = str(tmp_path / "recent.db")
        _fill(path, age_days=10)
        manager, clock, alerts = _manager(path, monkeypatch)
        manager.check_once()
        clock["t"] += config.STORAGE_GRACE_PERIOD_SECONDS + 1
        assert manager.check_once()["purged"] == 0
        assert "storage_purge_blocked_by_floor" in alerts.sent[-1][0]
        assert _count(path) == ROWS

    def test_repeated_failure_does_not_spam_emails(self, db, monkeypatch):
        manager, clock, alerts = _manager(db, monkeypatch)
        monkeypatch.setattr(storage_manager, "verify_backup", lambda path: False)
        manager.check_once()
        clock["t"] += config.STORAGE_GRACE_PERIOD_SECONDS + 1
        manager.check_once()
        sent_after_first_failure = len(alerts.sent)
        for _ in range(5):
            clock["t"] += 60                              # bien en deçà du cooldown
            manager.check_once()
        assert len(alerts.sent) == sent_after_first_failure
        with sqlite_client.connect(db) as conn:           # mais chaque événement reste journalisé
            logged = conn.execute("SELECT COUNT(*) FROM alerts_log WHERE rule_name='storage_backup_failed'").fetchone()[0]
        assert logged >= 6

    def test_failed_email_is_retried_at_next_check(self, db, monkeypatch):
        """Régression : un e-mail qui n'est pas parti ne doit pas déclencher le cooldown."""
        results = [False, True]
        sent: list[str] = []

        def flaky_sender(subject, body, level):
            sent.append(subject)
            return results.pop(0)

        manager = StorageManager(db, alert_sender=flaky_sender)
        assert manager._alert("storage_backup_failed", "s", "m", "CRITIQUE") is False
        assert manager._alert("storage_backup_failed", "s", "m", "CRITIQUE") is True
        assert manager._alert("storage_backup_failed", "s", "m", "CRITIQUE") is False  # cooldown actif
        assert len(sent) == 2

    def test_stop_ends_the_loop(self, db):
        manager = StorageManager(db, alert_sender=FakeAlerts())
        manager.stop()
        manager.run_forever()                             # retourne immédiatement
        assert manager._stop_event.is_set()
