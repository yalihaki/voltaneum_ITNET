"""Sauvegarde et restauration de la base SQLite.

Les sauvegardes sont des copies COHÉRENTES (API de sauvegarde en ligne de SQLite,
sûre même pendant des écritures concurrentes en mode WAL), compressées en gzip,
horodatées (UTC) et limitées en nombre pour ne pas encombrer le disque.

Sécurité : le dossier de sauvegarde est créé en 0700 et chaque archive en 0600
(elles contiennent l'intégralité des mesures de l'installation).

Usage : from database.backup import create_backup, restore_backup, verify_backup
"""

from __future__ import annotations

import gzip
import logging
import os
import shutil
import sqlite3
import zlib
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

BACKUP_PREFIX = "voltaneum-"
_DIR_MODE = 0o700
_FILE_MODE = 0o600
_SQLITE_MAGIC = b"SQLite format 3\x00"  # en-tête de tout fichier SQLite valide


def _remove_sqlite_sidecars(db_file: Path) -> None:
    """Supprime les fichiers annexes `-wal` et `-shm` associés à une base SQLite."""
    for suffix in ("-wal", "-shm"):
        Path(f"{db_file}{suffix}").unlink(missing_ok=True)


def create_backup(db_path: str, backup_dir: str, retention_count: int = 7) -> Path:
    """Crée une sauvegarde compressée et cohérente, puis supprime les plus anciennes.

    Attention à l'espace disque : la copie temporaire non compressée occupe
    autant de place que la base. Le dossier de sauvegarde doit donc se trouver
    sur un volume ayant au moins cet espace libre (idéalement un autre disque).
    """
    destination_dir = Path(backup_dir)
    destination_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(destination_dir, _DIR_MODE)  # `mkdir(mode=)` est filtré par l'umask

    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    temporary = destination_dir / f"{BACKUP_PREFIX}{stamp}.db"
    archive = destination_dir / f"{BACKUP_PREFIX}{stamp}.db.gz"

    try:
        # `closing` : l'API `with sqlite3.connect()` valide mais NE ferme PAS la connexion.
        with closing(sqlite3.connect(db_path, timeout=30)) as source, \
                closing(sqlite3.connect(temporary)) as destination:
            source.backup(destination)

        fd = os.open(archive, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, _FILE_MODE)
        with temporary.open("rb") as plain, os.fdopen(fd, "wb") as raw, \
                gzip.GzipFile(fileobj=raw, mode="wb") as compressed:
            shutil.copyfileobj(plain, compressed)
    except BaseException:
        # Une archive partielle ne doit jamais passer pour une sauvegarde valide.
        archive.unlink(missing_ok=True)
        raise
    finally:
        temporary.unlink(missing_ok=True)
        _remove_sqlite_sidecars(temporary)

    # Rétention : le nom contient l'horodatage UTC, l'ordre alphabétique est chronologique.
    for old_backup in sorted(destination_dir.glob(f"{BACKUP_PREFIX}*.db.gz"), reverse=True)[retention_count:]:
        old_backup.unlink(missing_ok=True)
    logger.info("Sauvegarde SQLite créée : %s", archive)
    return archive


def restore_backup(archive_path: str, destination_path: str) -> None:
    """Restaure une sauvegarde après vérification de son intégrité SQLite.

    L'APPLICATION DOIT ÊTRE ARRÊTÉE avant de restaurer sur sa base active. Les
    fichiers `-wal`/`-shm` de l'ancienne base sont supprimés : sinon SQLite
    rejouerait ce vieux journal sur la base restaurée et la corromprait.
    """
    destination = Path(destination_path)
    temporary = destination.with_name(destination.name + ".restore.tmp")
    try:
        with gzip.open(archive_path, "rb") as compressed, temporary.open("wb") as plain:
            shutil.copyfileobj(compressed, plain)
        with temporary.open("rb") as restored:
            # Une archive vide (sauvegarde interrompue) passerait sinon pour une base « saine ».
            if restored.read(len(_SQLITE_MAGIC)) != _SQLITE_MAGIC:
                raise sqlite3.DatabaseError("le contenu de la sauvegarde n'est pas une base SQLite")
        with closing(sqlite3.connect(temporary)) as connection:
            integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity != ("ok",):
            raise sqlite3.DatabaseError(f"SQLite integrity_check invalide : {integrity}")
        os.chmod(temporary, _FILE_MODE)
        _remove_sqlite_sidecars(destination)
        temporary.replace(destination)
    finally:
        temporary.unlink(missing_ok=True)
        _remove_sqlite_sidecars(temporary)


def verify_backup(archive_path: str) -> bool:
    """Vérifie une archive en la restaurant dans un fichier temporaire (supprimé ensuite)."""
    scratch = Path(archive_path).with_suffix(".verify.db")
    try:
        restore_backup(archive_path, str(scratch))
        return True
    except (OSError, EOFError, zlib.error, sqlite3.Error) as exc:
        logger.error("Sauvegarde invalide %s : %s", archive_path, exc)
        return False
    finally:
        scratch.unlink(missing_ok=True)
        _remove_sqlite_sidecars(scratch)
