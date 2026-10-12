"""Protection automatique de l'espace disque occupé par la base SQLite.

Principe (décision d'architecture) : la purge est déclenchée par la TAILLE de la
base, jamais par une échéance calendaire. Séquence quand la base atteint
`STORAGE_WARNING_RATIO` de `STORAGE_LIMIT_BYTES` :

  1. e-mail d'avertissement, puis délai de grâce (`STORAGE_GRACE_PERIOD_SECONDS`) ;
  2. sauvegarde compressée et vérifiée de la base, conservée AU MOINS `STORAGE_BACKUP_MIN_AGE_DAYS`
     jours (365 par défaut) ; si le volume de sauvegarde manque d'espace ou si la sauvegarde
     échoue : AUCUNE donnée supprimée, alerte critique ;
  3. suppression des lignes les plus anciennes, par lots, jusqu'à ce que les données
     vivantes repassent sous `STORAGE_PURGE_TARGET_RATIO` — pas plus ;
  4. compactage (checkpoint WAL + VACUUM) pour rendre réellement l'espace au disque.

Les durées `*_RETENTION_DAYS` sont des PLANCHERS : une ligne plus récente n'est jamais
supprimée, même sous pression d'espace (une alerte distincte signale ce blocage).
La taille mesurée inclut les fichiers annexes `-wal` et `-shm`.
"""

from __future__ import annotations

import logging
import shutil
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import config
from database import sqlite_client
from database.backup import create_backup, verify_backup
from notifications.email_notifier import send_email

logger = logging.getLogger(__name__)

# (sujet, corps, niveau) -> True si l'e-mail est parti
AlertSender = Callable[[str, str, str], bool]


@dataclass(frozen=True)
class _Journal:
    """Une table purgeable. Les requêtes sont des constantes (aucun SQL construit dynamiquement)."""

    floor_setting: str      # nom du réglage de config donnant le plancher de conservation (jours)
    select_oldest_sql: str  # ids éligibles (plus vieux que le plancher), du plus ancien au plus récent
    delete_sql: str
    count_sql: str
    count_eligible_sql: str


# Ordre de purge : la télémétrie d'abord, puis les prédictions (complétées en premier), puis les alertes.
_JOURNALS = (
    _Journal(
        "RETENTION_DAYS",
        "SELECT id FROM telemetry WHERE timestamp < datetime('now', ?) "
        "ORDER BY timestamp ASC, id ASC LIMIT ?",
        "DELETE FROM telemetry WHERE id = ?",
        "SELECT COUNT(*) FROM telemetry",
        "SELECT COUNT(*) FROM telemetry WHERE timestamp < datetime('now', ?)",
    ),
    _Journal(
        "PREDICTIONS_RETENTION_DAYS",
        "SELECT id FROM predictions_log WHERE made_at < datetime('now', ?) "
        "ORDER BY (actual_value IS NULL), made_at ASC, id ASC LIMIT ?",
        "DELETE FROM predictions_log WHERE id = ?",
        "SELECT COUNT(*) FROM predictions_log",
        "SELECT COUNT(*) FROM predictions_log WHERE made_at < datetime('now', ?)",
    ),
    _Journal(
        "ALERTS_RETENTION_DAYS",
        "SELECT id FROM alerts_log WHERE timestamp < datetime('now', ?) "
        "ORDER BY timestamp ASC, id ASC LIMIT ?",
        "DELETE FROM alerts_log WHERE id = ?",
        "SELECT COUNT(*) FROM alerts_log",
        "SELECT COUNT(*) FROM alerts_log WHERE timestamp < datetime('now', ?)",
    ),
)


def database_size_bytes(db_path: str) -> int:
    """Taille cumulée du fichier SQLite et de ses fichiers annexes `-wal` et `-shm`."""
    return sum(
        path.stat().st_size
        for path in (Path(db_path), Path(f"{db_path}-wal"), Path(f"{db_path}-shm"))
        if path.exists()
    )


def _live_bytes(conn) -> int:
    """Octets de DONNÉES VIVANTES (pages utilisées, hors pages libres).

    Supprimer des lignes ne réduit pas la taille du fichier avant un VACUUM : les
    pages deviennent « libres ». C'est donc cette mesure, et non la taille du
    fichier, qui permet de savoir quand arrêter de supprimer.
    """
    page_size = conn.execute("PRAGMA page_size").fetchone()[0]
    page_count = conn.execute("PRAGMA page_count").fetchone()[0]
    free_pages = conn.execute("PRAGMA freelist_count").fetchone()[0]
    return page_size * (page_count - free_pages)


def live_data_bytes(db_path: str) -> int:
    """Version publique de `_live_bytes` (ouvre sa propre connexion)."""
    with sqlite_client.connect(db_path) as conn:
        return _live_bytes(conn)


def _delete_oldest_batch(conn, batch_size: int) -> int:
    """Supprime un lot dans la première table ayant des lignes éligibles ; retourne le nombre supprimé."""
    for journal in _JOURNALS:
        floor_days = getattr(config, journal.floor_setting)
        ids = conn.execute(journal.select_oldest_sql, (f"-{floor_days} days", batch_size)).fetchall()
        if ids:
            conn.executemany(journal.delete_sql, ids)
            return len(ids)
    return 0


def purge_oldest_until_size(
    db_path: str,
    target_bytes: int,
    batch_size: int | None = None,
) -> int:
    """Supprime les lignes les plus anciennes jusqu'à ce que les données vivantes
    passent sous `target_bytes`, sans jamais toucher aux lignes sous plancher.

    Retourne le nombre de lignes supprimées. Compacte ensuite la base pour libérer
    réellement l'espace disque.
    """
    batch = batch_size or config.STORAGE_PURGE_BATCH_SIZE
    deleted = 0
    with sqlite_client.connect(db_path) as conn:
        while _live_bytes(conn) > target_bytes:
            removed = _delete_oldest_batch(conn, batch)
            if removed == 0:  # plus rien d'éligible : tout le reste est protégé par les planchers
                break
            conn.commit()
            deleted += removed

    try:
        with sqlite_client.connect(db_path) as conn:
            conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            conn.isolation_level = None  # VACUUM ne peut pas s'exécuter dans une transaction
            conn.execute("VACUUM")
    except Exception:
        logger.exception("Échec du compactage SQLite après purge")
    return deleted


def _purge_blocked_by_floor(db_path: str) -> bool:
    """Vrai si la base contient des lignes mais qu'aucune n'est éligible (toutes sous plancher)."""
    with sqlite_client.connect(db_path) as conn:
        total = sum(conn.execute(j.count_sql).fetchone()[0] for j in _JOURNALS)
        eligible = sum(
            conn.execute(j.count_eligible_sql, (f"-{getattr(config, j.floor_setting)} days",)).fetchone()[0]
            for j in _JOURNALS
        )
    return total > 0 and eligible == 0


class StorageManager:
    """Surveille la taille de la base et déclenche avertissement puis purge si nécessaire."""

    def __init__(
        self,
        db_path: str | None = None,
        alert_sender: AlertSender = send_email,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self.db_path = db_path or config.SQLITE_DB_PATH
        self.alert_sender = alert_sender
        self.time_fn = time_fn
        self._stop_event = threading.Event()
        self._warning_sent_at: float | None = None
        self._alert_sent_at: dict[str, float] = {}

    @property
    def warning_threshold_bytes(self) -> int:
        return int(config.STORAGE_LIMIT_BYTES * config.STORAGE_WARNING_RATIO)

    @property
    def purge_target_bytes(self) -> int:
        return int(config.STORAGE_LIMIT_BYTES * config.STORAGE_PURGE_TARGET_RATIO)

    def _alert(self, event_type: str, subject: str, message: str, level: str) -> bool:
        """Journalise l'événement puis envoie l'e-mail, au plus une fois par type et par
        `STORAGE_ALERT_COOLDOWN_MINUTES` après un envoi réussi (un problème qui persiste
        ne doit pas spammer, mais un e-mail qui n'est pas parti doit être retenté)."""
        sqlite_client.log_alert(level, event_type, message, email_sent=False, db_path=self.db_path)
        now = self.time_fn()
        last_sent = self._alert_sent_at.get(event_type)
        if last_sent is not None and now - last_sent < config.STORAGE_ALERT_COOLDOWN_MINUTES * 60:
            return False
        sent = self.alert_sender(f"[Voltaneum][{level}] {subject}", message, level)
        if sent:
            # Le cooldown ne démarre qu'après un envoi RÉUSSI : un échec SMTP est retenté
            # au contrôle suivant (STORAGE_CHECK_INTERVAL_SECONDS) au lieu d'être perdu.
            self._alert_sent_at[event_type] = now
        return sent

    def _backup_space_missing(self) -> str | None:
        """Retourne une description si le volume de sauvegarde n'a pas assez d'espace libre
        (taille de la base × STORAGE_BACKUP_FREE_SPACE_FACTOR), sinon None.

        La sauvegarde crée d'abord une copie temporaire NON compressée de la base, puis son
        archive : sans cette marge elle échouerait en cours de route. Vérifier avant évite de
        saturer le volume et garantit qu'aucune suppression n'a lieu sans sauvegarde valide.
        """
        backup_dir = Path(config.STORAGE_BACKUP_DIR)
        try:
            backup_dir.mkdir(parents=True, exist_ok=True)
            free = shutil.disk_usage(backup_dir).free
        except OSError as exc:
            return f"volume de sauvegarde inaccessible ({exc})"
        needed = int(database_size_bytes(self.db_path) * config.STORAGE_BACKUP_FREE_SPACE_FACTOR)
        if free < needed:
            return (f"espace libre insuffisant sur {backup_dir} : {free / 1024**3:.2f} Gio disponibles, "
                    f"{needed / 1024**3:.2f} Gio nécessaires")
        return None

    def _backup_before_purge(self) -> bool:
        """Crée et vérifie la sauvegarde exigée avant toute suppression ; alerte en cas d'échec."""
        missing_space = self._backup_space_missing()
        if missing_space is not None:
            self._alert("storage_backup_failed", "Sauvegarde stockage impossible",
                        f"Purge annulée, aucune donnée supprimée : {missing_space}.", "CRITIQUE")
            return False
        try:
            backup_path = create_backup(
                self.db_path, config.STORAGE_BACKUP_DIR, config.STORAGE_BACKUP_RETENTION_COUNT,
                config.STORAGE_BACKUP_MIN_AGE_DAYS,
            )
            if verify_backup(str(backup_path)):
                return True
            reason = f"sauvegarde invalide ({backup_path})"
        except Exception as exc:  # disque plein, droits, base verrouillée…
            logger.exception("Échec de la sauvegarde avant purge")
            reason = f"échec de la sauvegarde ({exc})"
        self._alert("storage_backup_failed", "Sauvegarde stockage impossible",
                    f"Purge annulée, aucune donnée supprimée : {reason}.", "CRITIQUE")
        return False

    def check_once(self) -> dict[str, int | bool]:
        """Effectue un contrôle complet et retourne un résumé (utilisé par les tests)."""
        size = database_size_bytes(self.db_path)
        now = self.time_fn()
        result: dict[str, int | bool] = {"size_bytes": size, "warning_sent": False, "purged": 0}

        if size < self.warning_threshold_bytes:
            self._warning_sent_at = None
            return result

        if self._warning_sent_at is None:
            self._warning_sent_at = now
            grace_hours = config.STORAGE_GRACE_PERIOD_SECONDS / 3600
            result["warning_sent"] = self._alert(
                "storage_warning", "Stockage presque plein",
                f"La base {self.db_path} occupe {size / 1024**3:.2f} Gio "
                f"(seuil d'avertissement {self.warning_threshold_bytes / 1024**3:.2f} Gio). "
                f"Une purge automatique aura lieu dans {grace_hours:.1f} heure(s) sans intervention.",
                "ATTENTION",
            )
            return result

        if now - self._warning_sent_at < config.STORAGE_GRACE_PERIOD_SECONDS:
            return result
        if not self._backup_before_purge():
            return result

        deleted = purge_oldest_until_size(self.db_path, self.purge_target_bytes)
        remaining = database_size_bytes(self.db_path)
        if remaining > self.purge_target_bytes:
            blocked = _purge_blocked_by_floor(self.db_path)
            event_type = "storage_purge_blocked_by_floor" if blocked else "storage_purge_ineffective"
            message = (
                f"Purge {'bloquée par le plancher de conservation' if blocked else 'inefficace'} : "
                f"{deleted} ligne(s) supprimée(s), taille restante {remaining / 1024**3:.2f} Gio, "
                "toujours au-dessus de la cible."
            )
        else:
            event_type = "storage_auto_purge"
            message = (f"Purge automatique exécutée : {deleted} ligne(s) supprimée(s), "
                       f"taille restante {remaining / 1024**3:.2f} Gio.")
        self._alert(event_type, event_type, message, "CRITIQUE")

        self._warning_sent_at = None if remaining < self.warning_threshold_bytes else now
        result["purged"] = deleted
        result["size_bytes"] = remaining
        return result

    def run_forever(self) -> None:
        """Boucle de surveillance (à lancer dans un thread dédié)."""
        while not self._stop_event.wait(config.STORAGE_CHECK_INTERVAL_SECONDS):
            try:
                self.check_once()
            except Exception:
                logger.exception("Erreur inattendue du superviseur de stockage")

    def stop(self) -> None:
        self._stop_event.set()
