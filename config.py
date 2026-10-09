"""Configuration unique de Voltaneum / ITNET.

Tous les réglages (adresses, seuils, identifiants) sont lus ici, depuis les variables
d'environnement ou le fichier `.env` du projet ; aucun autre module ne lit
l'environnement directement. Les valeurs par défaut sont sûres et documentées dans
`.env.example`.

Règle de sécurité : aucun identifiant n'est jamais écrit en dur dans le code.

`validate_config()` doit être appelée au démarrage de chaque programme : elle refuse
de démarrer avec une configuration absente ou incohérente (message explicite).
"""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

# Racine du projet : les chemins relatifs sont résolus à partir d'ici, jamais à partir du
# dossier courant (un service lancé depuis un autre répertoire écrirait sinon sa base ailleurs).
PROJECT_ROOT = Path(__file__).resolve().parent

# Les variables déjà définies dans l'environnement priment sur le fichier `.env`.
load_dotenv(PROJECT_ROOT / ".env")


# ---------------------------------------------------------------------------
# Lecture typée de l'environnement (les erreurs nomment toujours la variable fautive)
# ---------------------------------------------------------------------------
def _env_str(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_optional(name: str) -> str | None:
    """Chaîne, ou None si la variable est absente ou vide."""
    return _env_str(name) or None


def _env_int(name: str, default: int) -> int:
    raw = _env_str(name, str(default))
    try:
        return int(raw)
    except ValueError:
        raise ValueError(f"{name} doit être un entier, reçu {raw!r}.") from None


def _env_float(name: str, default: float) -> float:
    raw = _env_str(name, str(default))
    try:
        return float(raw)
    except ValueError:
        raise ValueError(f"{name} doit être un nombre, reçu {raw!r}.") from None


def _env_bool(name: str, default: bool = False) -> bool:
    raw = _env_str(name, str(default)).lower()
    if raw not in {"true", "false", "1", "0", "yes", "no"}:
        raise ValueError(f"{name} doit être un booléen (true/false), reçu {raw!r}.")
    return raw in {"true", "1", "yes"}


def _env_path(name: str, default: str) -> str:
    """Chemin lu de l'environnement ; un chemin relatif est résolu depuis la racine du projet."""
    path = Path(_env_str(name, default)).expanduser()
    return str(path if path.is_absolute() else PROJECT_ROOT / path)


# ---------------------------------------------------------------------------
# Identifiants de la passerelle (OBLIGATOIRES, jamais en dur)
# ---------------------------------------------------------------------------
VOLTANEUM_USER: str | None = _env_optional("VOLTANEUM_USER")
VOLTANEUM_PASSWORD: str | None = _env_optional("VOLTANEUM_PASSWORD")

# ---------------------------------------------------------------------------
# Passerelle industrielle (HinetGateway) — accessible uniquement depuis le réseau
# industriel ou un VPN autorisé ; ne jamais l'exposer ni contourner un pare-feu.
# ---------------------------------------------------------------------------
GATEWAY_HOST: str = _env_str("GATEWAY_HOST", "10.0.1.33")
MANAGE_API_URL: str = _env_str("MANAGE_API_URL", f"http://{GATEWAY_HOST}/ManageApi/Base.Auth.php")
IOT_API_URL: str = _env_str("IOT_API_URL", f"http://{GATEWAY_HOST}:8001/iot/query_all_points")
WS_URL: str = _env_str("WS_URL", f"ws://{GATEWAY_HOST}:91")
# Nom du driver capteur retenu dans les messages WebSocket (à confirmer si un second
# échangeur est un jour ajouté sur la même passerelle).
SENSOR_DRIVER_NODE: str = _env_str("SENSOR_DRIVER_NODE", "idospV1_1")

WS_RECONNECT_BACKOFF_INITIAL_SECONDS: float = 1.0
WS_RECONNECT_BACKOFF_MAX_SECONDS: float = 30.0
# Flux WebSocket muet au-delà de ce délai : connexion rouverte et instantanés suspendus
# (0 = surveillance désactivée).
WS_SILENCE_TIMEOUT_SECONDS: int = _env_int("WS_SILENCE_TIMEOUT_SECONDS", 300)
POINT_MAPPING_REFRESH_SECONDS: int = _env_int("POINT_MAPPING_REFRESH_SECONDS", 3600)

# ---------------------------------------------------------------------------
# MQTT — bus de messages interne entre les modules
# ---------------------------------------------------------------------------
MQTT_HOST: str = _env_str("MQTT_HOST", "localhost")
MQTT_PORT: int = _env_int("MQTT_PORT", 1883)
MQTT_USERNAME: str | None = _env_optional("MQTT_USERNAME")
MQTT_PASSWORD: str | None = _env_optional("MQTT_PASSWORD")
MQTT_KEEPALIVE_SECONDS: int = _env_int("MQTT_KEEPALIVE_SECONDS", 60)
MQTT_TLS_ENABLED: bool = _env_bool("MQTT_TLS_ENABLED")
MQTT_TLS_CA_CERT: str | None = _env_optional("MQTT_TLS_CA_CERT")
MQTT_TLS_CERTFILE: str | None = _env_optional("MQTT_TLS_CERTFILE")
MQTT_TLS_KEYFILE: str | None = _env_optional("MQTT_TLS_KEYFILE")
# Interdit : la vérification du certificat ne doit jamais être désactivée.
MQTT_TLS_INSECURE: bool = _env_bool("MQTT_TLS_INSECURE")

MQTT_TOPIC_TELEMETRY = "voltaneum/telemetry"
MQTT_TOPIC_PREDICTION = "voltaneum/prediction"
MQTT_TOPIC_SETPOINT = "voltaneum/control/setpoint"
MQTT_TOPIC_MODEL_HEALTH = "voltaneum/model_health"
MQTT_TOPIC_OUTDOOR_CONSISTENCY = "voltaneum/sensors/outdoor_consistency"

# ---------------------------------------------------------------------------
# Base de données SQLite et conservation des données
# ---------------------------------------------------------------------------
SQLITE_DB_PATH: str = _env_path("SQLITE_DB_PATH", "voltaneum.db")
# PLANCHERS de conservation (jours) : la purge, déclenchée par la TAILLE de la base
# (voir STORAGE_*), ne supprime jamais une ligne plus récente que ces durées.
RETENTION_DAYS: int = _env_int("RETENTION_DAYS", 365)
PREDICTIONS_RETENTION_DAYS: int = _env_int("PREDICTIONS_RETENTION_DAYS", 90)
ALERTS_RETENTION_DAYS: int = _env_int("ALERTS_RETENTION_DAYS", 365)

SNAPSHOT_INTERVAL_SECONDS: int = _env_int("SNAPSHOT_INTERVAL_SECONDS", 15)
# Écart maximal toléré entre deux mesures pour qu'elles appartiennent à un même
# segment continu (un entraînement ne relie jamais deux segments séparés par un trou).
MAX_GAP_SECONDS: int = _env_int("MAX_GAP_SECONDS", 60)

# Plages physiques plausibles : toute valeur hors plage est rejetée (capteur défaillant).
SENSOR_VALID_RANGES: dict[str, tuple[float, float]] = {
    "pt01": (-20.0, 150.0),
    "pt02": (-20.0, 150.0),
    "pt03": (-20.0, 150.0),
    "pt04": (-20.0, 150.0),
    "ht01": (0.0, 200.0),
    "freq_pompe": (0.0, 15.0),   # plage réelle du variateur de la pompe (Hz)
    "temp_outdoor": (-50.0, 55.0),
}

# ---------------------------------------------------------------------------
# Machine learning
# ---------------------------------------------------------------------------
MODEL_DIR: str = _env_path("MODEL_DIR", "models")
FEATURE_COLUMNS: list[str] = ["pt01", "pt03", "pt04", "ht01", "freq_pompe", "temp_outdoor"]
TARGET_COLUMN: str = "pt02"
# Horizons analysés par train_all_horizons.py ; la production n'utilise que le dernier (600 s).
PREDICTION_HORIZONS_SECONDS: list[int] = [30, 60, 120, 300, 600]
# Tolérance du rapprochement d'horodatage entre une mesure et sa valeur future (secondes).
TARGET_MERGE_TOLERANCE_SECONDS: int = 15
# Écart entre les deux modèles au-delà duquel le désaccord est signalé (°C).
DISAGREEMENT_THRESHOLD_CELSIUS: float = 1.0

# ---------------------------------------------------------------------------
# Régulation PID (valeurs par défaut NON calibrées sur le système réel)
# ---------------------------------------------------------------------------
CONTROL_LOOP_INTERVAL_SECONDS: float = _env_float("CONTROL_LOOP_INTERVAL_SECONDS", 60.0)
TARGET_TEMP: float = _env_float("TARGET_TEMP", 35.0)
CRITICAL_TEMP: float = _env_float("CRITICAL_TEMP", 55.0)
PID_KP: float = _env_float("PID_KP", 5.0)
PID_KI: float = _env_float("PID_KI", 0.1)
PID_KD: float = _env_float("PID_KD", 1.0)
SETPOINT_MIN: float = 0.0
SETPOINT_MAX: float = 100.0
# Termes « feedforward » : anticipent la dérive à partir de la prédiction et de la météo.
FEEDFORWARD_PREDICTED_OVERSHOOT_THRESHOLD: float = 1.5   # °C au-dessus de la cible
FEEDFORWARD_PREDICTED_BOOST: float = 15.0
FEEDFORWARD_OUTDOOR_HOT_THRESHOLD: float = 30.0          # °C
FEEDFORWARD_OUTDOOR_BOOST: float = 10.0

# ---------------------------------------------------------------------------
# Température extérieure : capteur physique indépendant
#   "mqtt" (recommandé) : le capteur publie sur OUTDOOR_SENSOR_MQTT_TOPIC ;
#   "http" (repli)      : le capteur expose une petite API HTTP (OUTDOOR_SENSOR_HTTP_URL).
# ---------------------------------------------------------------------------
OUTDOOR_SENSOR_MODE: str = _env_str("OUTDOOR_SENSOR_MODE", "mqtt").lower()
OUTDOOR_SENSOR_MQTT_TOPIC: str = _env_str("OUTDOOR_SENSOR_MQTT_TOPIC", "voltaneum/sensors/outdoor_temperature")
OUTDOOR_SENSOR_HTTP_URL: str = _env_str("OUTDOOR_SENSOR_HTTP_URL")
OUTDOOR_SENSOR_HTTP_TIMEOUT: float = _env_float("OUTDOOR_SENSOR_HTTP_TIMEOUT", 5.0)
# Une mesure plus ancienne que ce délai est considérée comme absente (capteur muet).
OUTDOOR_SENSOR_MAX_AGE_SECONDS: int = _env_int("OUTDOOR_SENSOR_MAX_AGE_SECONDS", 1800)

# Vérification de cohérence capteur / météo (surveillance uniquement : la météo ne
# remplace JAMAIS le capteur). Seuil large : un capteur local et une moyenne régionale
# peuvent légitimement diverger de plusieurs degrés.
OUTDOOR_CONSISTENCY_CHECK_INTERVAL_SECONDS: int = _env_int("OUTDOOR_CONSISTENCY_CHECK_INTERVAL_SECONDS", 900)
OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS: float = _env_float("OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS", 8.0)
# Position du site, pour interroger l'API météo de comparaison.
LATITUDE: float = _env_float("VOLTANEUM_LAT", 48.8566)
LONGITUDE: float = _env_float("VOLTANEUM_LON", 2.3522)

# ---------------------------------------------------------------------------
# Alertes par e-mail (SMTP)
# ---------------------------------------------------------------------------
SMTP_HOST: str | None = _env_optional("SMTP_HOST")
SMTP_PORT: int = _env_int("SMTP_PORT", 587)
SMTP_USER: str | None = _env_optional("SMTP_USER")
SMTP_PASSWORD: str | None = _env_optional("SMTP_PASSWORD")
ALERT_EMAIL_FROM: str | None = _env_optional("ALERT_EMAIL_FROM")
ALERT_EMAIL_TO: str | None = _env_optional("ALERT_EMAIL_TO")
ALERT_COOLDOWN_MINUTES: int = _env_int("ALERT_COOLDOWN_MINUTES", 30)
ALERT_CONFIRMATION_CYCLES: int = _env_int("ALERT_CONFIRMATION_CYCLES", 3)
TELEMETRY_TIMEOUT_SECONDS: int = _env_int("TELEMETRY_TIMEOUT_SECONDS", 300)
# Plage d'efficacité attendue de l'échangeur (provisoire, à resserrer avec des données réelles).
EFFICIENCY_MIN_EXPECTED: float = _env_float("EFFICIENCY_MIN_EXPECTED", 0.0)
EFFICIENCY_MAX_EXPECTED: float = _env_float("EFFICIENCY_MAX_EXPECTED", 1.0)

# ---------------------------------------------------------------------------
# Protection automatique du stockage (purge pilotée par la TAILLE de la base)
# ---------------------------------------------------------------------------
STORAGE_LIMIT_BYTES: int = _env_int("STORAGE_LIMIT_BYTES", 1024**4)          # 1 Tio
STORAGE_WARNING_RATIO: float = _env_float("STORAGE_WARNING_RATIO", 0.90)
STORAGE_PURGE_TARGET_RATIO: float = _env_float("STORAGE_PURGE_TARGET_RATIO", 0.75)
STORAGE_GRACE_PERIOD_SECONDS: int = _env_int("STORAGE_GRACE_PERIOD_SECONDS", 3600)
STORAGE_CHECK_INTERVAL_SECONDS: int = _env_int("STORAGE_CHECK_INTERVAL_SECONDS", 300)
STORAGE_PURGE_BATCH_SIZE: int = _env_int("STORAGE_PURGE_BATCH_SIZE", 1000)
# Idéalement un AUTRE volume que la base, avec au moins autant d'espace libre qu'elle.
STORAGE_BACKUP_DIR: str = _env_path("STORAGE_BACKUP_DIR", "backups")
STORAGE_BACKUP_RETENTION_COUNT: int = _env_int("STORAGE_BACKUP_RETENTION_COUNT", 7)
STORAGE_ALERT_COOLDOWN_MINUTES: int = _env_int("STORAGE_ALERT_COOLDOWN_MINUTES", 60)

# ---------------------------------------------------------------------------
# Journalisation
# ---------------------------------------------------------------------------
LOG_LEVEL: str = _env_str("LOG_LEVEL", "INFO").upper()

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Configuration invalide : {message}")


def validate_config() -> None:
    """Refuse une configuration absente ou incohérente, avec un message explicite.

    À appeler au démarrage de chaque programme, avant toute connexion.
    """
    missing = [name for name, value in {
        "VOLTANEUM_USER": VOLTANEUM_USER, "VOLTANEUM_PASSWORD": VOLTANEUM_PASSWORD,
        "MQTT_HOST": MQTT_HOST, "SQLITE_DB_PATH": SQLITE_DB_PATH,
    }.items() if not value]
    _require(not missing, "variables obligatoires absentes ou vides : " + ", ".join(missing))

    # Ports et délais
    for name, port in (("MQTT_PORT", MQTT_PORT), ("SMTP_PORT", SMTP_PORT)):
        _require(1 <= port <= 65535, f"{name} doit être compris entre 1 et 65535, reçu {port}.")
    for name, value in {
        "MQTT_KEEPALIVE_SECONDS": MQTT_KEEPALIVE_SECONDS, "SNAPSHOT_INTERVAL_SECONDS": SNAPSHOT_INTERVAL_SECONDS,
        "CONTROL_LOOP_INTERVAL_SECONDS": CONTROL_LOOP_INTERVAL_SECONDS,
        "POINT_MAPPING_REFRESH_SECONDS": POINT_MAPPING_REFRESH_SECONDS, "MAX_GAP_SECONDS": MAX_GAP_SECONDS,
        "TELEMETRY_TIMEOUT_SECONDS": TELEMETRY_TIMEOUT_SECONDS, "ALERT_CONFIRMATION_CYCLES": ALERT_CONFIRMATION_CYCLES,
        "OUTDOOR_SENSOR_HTTP_TIMEOUT": OUTDOOR_SENSOR_HTTP_TIMEOUT,
        "OUTDOOR_SENSOR_MAX_AGE_SECONDS": OUTDOOR_SENSOR_MAX_AGE_SECONDS,
        "OUTDOOR_CONSISTENCY_CHECK_INTERVAL_SECONDS": OUTDOOR_CONSISTENCY_CHECK_INTERVAL_SECONDS,
        "OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS": OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS,
        "STORAGE_LIMIT_BYTES": STORAGE_LIMIT_BYTES, "STORAGE_CHECK_INTERVAL_SECONDS": STORAGE_CHECK_INTERVAL_SECONDS,
        "STORAGE_PURGE_BATCH_SIZE": STORAGE_PURGE_BATCH_SIZE,
        "STORAGE_BACKUP_RETENTION_COUNT": STORAGE_BACKUP_RETENTION_COUNT,
    }.items():
        _require(value > 0, f"{name} doit être strictement positif, reçu {value}.")
    for name, value in {
        "ALERT_COOLDOWN_MINUTES": ALERT_COOLDOWN_MINUTES, "STORAGE_ALERT_COOLDOWN_MINUTES": STORAGE_ALERT_COOLDOWN_MINUTES,
        "STORAGE_GRACE_PERIOD_SECONDS": STORAGE_GRACE_PERIOD_SECONDS, "WS_SILENCE_TIMEOUT_SECONDS": WS_SILENCE_TIMEOUT_SECONDS,
        "RETENTION_DAYS": RETENTION_DAYS, "PREDICTIONS_RETENTION_DAYS": PREDICTIONS_RETENTION_DAYS,
        "ALERTS_RETENTION_DAYS": ALERTS_RETENTION_DAYS,
        "PID_KP": PID_KP, "PID_KI": PID_KI, "PID_KD": PID_KD,
    }.items():
        _require(value >= 0, f"{name} ne peut pas être négatif, reçu {value}.")

    # Sécurité process
    _require(CRITICAL_TEMP > TARGET_TEMP, f"CRITICAL_TEMP ({CRITICAL_TEMP}) doit dépasser TARGET_TEMP ({TARGET_TEMP}).")
    _require(SETPOINT_MIN < SETPOINT_MAX, "SETPOINT_MIN doit être inférieur à SETPOINT_MAX.")
    _require(EFFICIENCY_MIN_EXPECTED < EFFICIENCY_MAX_EXPECTED,
             "EFFICIENCY_MIN_EXPECTED doit être inférieur à EFFICIENCY_MAX_EXPECTED.")
    _require(0.0 < STORAGE_PURGE_TARGET_RATIO < STORAGE_WARNING_RATIO <= 1.0,
             "il faut 0 < STORAGE_PURGE_TARGET_RATIO < STORAGE_WARNING_RATIO <= 1.")

    # MQTT : TLS toujours vérifié
    _require(not MQTT_TLS_INSECURE,
             "MQTT_TLS_INSECURE est interdit : la vérification TLS ne doit jamais être désactivée.")
    _require(not (MQTT_PASSWORD and not MQTT_USERNAME), "MQTT_PASSWORD ne peut pas être défini sans MQTT_USERNAME.")
    if MQTT_TLS_ENABLED:
        for name, path in (("MQTT_TLS_CA_CERT", MQTT_TLS_CA_CERT), ("MQTT_TLS_CERTFILE", MQTT_TLS_CERTFILE),
                           ("MQTT_TLS_KEYFILE", MQTT_TLS_KEYFILE)):
            _require(not path or os.path.isfile(path), f"{name} introuvable : {path!r}.")

    # Capteur extérieur
    _require(OUTDOOR_SENSOR_MODE in ("mqtt", "http"),
             f"OUTDOOR_SENSOR_MODE doit être 'mqtt' ou 'http', reçu {OUTDOOR_SENSOR_MODE!r}.")
    _require(OUTDOOR_SENSOR_MODE != "http" or bool(OUTDOOR_SENSOR_HTTP_URL),
             "OUTDOOR_SENSOR_MODE='http' exige OUTDOOR_SENSOR_HTTP_URL.")
    _require(-90.0 <= LATITUDE <= 90.0, f"VOLTANEUM_LAT hors de [-90, 90] : {LATITUDE}.")
    _require(-180.0 <= LONGITUDE <= 180.0, f"VOLTANEUM_LON hors de [-180, 180] : {LONGITUDE}.")

    # E-mail : absent (alertes désactivées) ou COMPLET — jamais à moitié configuré
    smtp_settings = {"SMTP_HOST": SMTP_HOST, "SMTP_USER": SMTP_USER, "SMTP_PASSWORD": SMTP_PASSWORD,
                     "ALERT_EMAIL_FROM": ALERT_EMAIL_FROM, "ALERT_EMAIL_TO": ALERT_EMAIL_TO}
    unset = [name for name, value in smtp_settings.items() if not value]
    _require(len(unset) in (0, len(smtp_settings)),
             "configuration e-mail incomplète, il manque : " + ", ".join(unset))

    _require(LOG_LEVEL in _LOG_LEVELS, f"LOG_LEVEL doit être l'un de {sorted(_LOG_LEVELS)}, reçu {LOG_LEVEL!r}.")
