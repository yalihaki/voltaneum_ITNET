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
# Âge maximal d'une mesure capteur : au-delà, elle est considérée comme ABSENTE (capteur muet
# ou défaillant) au lieu d'être réutilisée comme si elle était actuelle (0 = jamais périmée).
SENSOR_MAX_AGE_SECONDS: int = _env_int("SENSOR_MAX_AGE_SECONDS", 300)

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
MQTT_TOPIC_COOLING_CONDITIONS = "voltaneum/control/cooling_conditions"
MQTT_TOPIC_MODEL_HEALTH = "voltaneum/model_health"

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
#
# POINT DE SÉCURITÉ — bornes hautes des sondes de température PT01..PT04 : elles restent
# volontairement AU-DESSUS de CRITICAL_TEMP (55 °C). Une borne haute à 55 °C rejetterait
# toute mesure > 55 °C comme « hors plage » : l'alerte critique et le bypass de sécurité du
# PID ne verraient alors JAMAIS une surchauffe réelle. `validate_config()` refuse de démarrer
# si cette condition n'est pas respectée pour la grandeur régulée (TARGET_COLUMN, PT02).
SENSOR_VALID_RANGES: dict[str, tuple[float, float]] = {
    "pt01": (-20.0, 150.0),      # °C — liquide diélectrique
    "pt02": (-20.0, 150.0),      # °C — liquide diélectrique (grandeur régulée et prédite)
    "pt03": (-20.0, 150.0),      # °C — circuit dry cooler
    "pt04": (-20.0, 150.0),      # °C — circuit dry cooler
    "ht01": (0.0, 15.0),         # m³/h — débit (circuit à confirmer : diélectrique ou refroidissement)
    "freq_pompe": (0.0, 50.0),   # Hz — fréquence pompe / ventilateur (variateur)
    "temp_outdoor": (-20.0, 55.0),  # °C — capteur extérieur indépendant
}

# ---------------------------------------------------------------------------
# Noms des mesures, IDENTIQUES sur tous les écrans (interface, terminal, résumé)
# ---------------------------------------------------------------------------
# (nom complet, nom court pour le synoptique). Réponse de l'automaticien (16/10/2026, corrigée) :
# deux circuits séparés par un échangeur — PT01 / PT02 sur le LIQUIDE DIÉLECTRIQUE (cuve à
# immersion), PT03 / PT04 sur le LIQUIDE DE REFROIDISSEMENT (boucle du dry cooler). Cohérent avec
# les mesures : la paire PT01/PT02 est la plus chaude (la chaleur va du diélectrique vers le
# refroidissement) et PT04 chute vite au démarrage du dry cooler. Aller / retour : à confirmer.
# Modifiables dans .env (PT01_LABEL … PT04_SHORT).
SENSOR_LABELS: dict[str, tuple[str, str]] = {
    "pt01": (_env_str("PT01_LABEL", "PT01 · liquide diélectrique"), _env_str("PT01_SHORT", "PT01 · diélectrique")),
    "pt02": (_env_str("PT02_LABEL", "PT02 · liquide diélectrique (régulé)"),
             _env_str("PT02_SHORT", "PT02 · diélec. (régulé)")),
    "pt03": (_env_str("PT03_LABEL", "PT03 · liquide de refroidissement"), _env_str("PT03_SHORT", "PT03 · refroid.")),
    "pt04": (_env_str("PT04_LABEL", "PT04 · liquide de refroidissement"), _env_str("PT04_SHORT", "PT04 · refroid.")),
}
# Sondes de chaque circuit (utilisées par les alertes et l'affichage).
DIELECTRIC_COLUMNS: tuple[str, ...] = ("pt01", "pt02")
COOLANT_COLUMNS: tuple[str, ...] = ("pt03", "pt04")

# Puissance thermique dissipée ESTIMÉE (kW) = débit HT01 × masse volumique × capacité thermique
# × (THERMAL_POWER_HOT − THERMAL_POWER_COLD). Par défaut : PT03 − PT04 (liquide de refroidissement)
# et les propriétés d'une eau glycolée (≈ 1040 kg/m³, 3800 J/kg/K). Valable SEULEMENT si HT01
# mesure le débit de la boucle de refroidissement ; s'il est sur le circuit diélectrique, prendre
# PT02 / PT01 et les propriétés du diélectrique (≈ 850 kg/m³, 1900 J/kg/K). À CONFIRMER. Tant que
# THERMAL_POWER_CONFIRMED=false, l'interface l'affiche comme une estimation à confirmer.
THERMAL_FLUID_DENSITY_KG_M3: float = _env_float("THERMAL_FLUID_DENSITY_KG_M3", 1040.0)
THERMAL_FLUID_HEAT_CAPACITY_J_KG_K: float = _env_float("THERMAL_FLUID_HEAT_CAPACITY_J_KG_K", 3800.0)
THERMAL_POWER_HOT: str = _env_str("THERMAL_POWER_HOT", "pt03").lower()
THERMAL_POWER_COLD: str = _env_str("THERMAL_POWER_COLD", "pt04").lower()
THERMAL_POWER_CONFIRMED: bool = _env_bool("THERMAL_POWER_CONFIRMED", False)

# ---------------------------------------------------------------------------
# Machine learning
# ---------------------------------------------------------------------------
MODEL_DIR: str = _env_path("MODEL_DIR", "models")
# Variables d'entrée des modèles. PT02 ACTUELLE en fait partie : prédire PT02 dans 10 min sans
# connaître sa valeur actuelle obligeait le modèle à la deviner, et la persistance (qui, elle,
# la connaît) le battait d'office aux horizons courts. Les modèles enregistrent cette liste ;
# un modèle entraîné avec une autre liste est refusé au chargement.
FEATURE_COLUMNS: list[str] = ["pt02", "pt01", "pt03", "pt04", "ht01", "freq_pompe", "temp_outdoor"]
TARGET_COLUMN: str = "pt02"
# Horizons dont l'erreur en production est suivie ; 600 s = horizon par défaut d'un entraînement.
PREDICTION_HORIZONS_SECONDS: list[int] = [30, 60, 120, 300, 600]
# Tolérance du rapprochement d'horodatage entre une mesure et sa valeur future (secondes).
TARGET_MERGE_TOLERANCE_SECONDS: int = 15
# Écart entre les deux modèles au-delà duquel le désaccord est signalé (°C).
DISAGREEMENT_THRESHOLD_CELSIUS: float = 1.0

# Ré-entraînement automatique (chaque nuit à RETRAIN_AT) : il entraîne une série
# d'horizons, garde le plus LONG qui reste fiable, et le PROPOSE (validation humaine par
# ./voltaneum model accept) ; il ne remplace jamais seul le modèle utilisé par la régulation.
AUTO_RETRAIN_HORIZONS_SECONDS: tuple[int, ...] = tuple(
    int(h) for h in _env_str(
        "AUTO_RETRAIN_HORIZONS", "600,900,1800,3600,7200,21600,43200,86400"
    ).split(",") if h.strip()
)
# Fiable = bat la persistance d'au moins ce gain relatif sur le RMSE (0.2 = 20 % d'erreur en moins)...
AUTO_RETRAIN_MIN_GAIN: float = _env_float("AUTO_RETRAIN_MIN_GAIN", 0.2)
# ... ET reste sous ce plafond d'erreur absolue (°C), quel que soit l'horizon.
AUTO_RETRAIN_MAX_RMSE_CELSIUS: float = _env_float("AUTO_RETRAIN_MAX_RMSE_CELSIUS", 0.5)
# Données utilisées : les N derniers jours (borne le temps de calcul nocturne).
AUTO_RETRAIN_MAX_DAYS: int = _env_int("AUTO_RETRAIN_MAX_DAYS", 90)
# Horizon pour lequel les seuils d'anticipation du PID ont été pensés (signalé si on s'en écarte).
PID_FEEDFORWARD_REFERENCE_HORIZON_SECONDS: int = 600

# ---------------------------------------------------------------------------
# Régulation PID (valeurs par défaut NON calibrées sur le système réel)
# ---------------------------------------------------------------------------
CONTROL_LOOP_INTERVAL_SECONDS: float = _env_float("CONTROL_LOOP_INTERVAL_SECONDS", 60.0)
TARGET_TEMP: float = _env_float("TARGET_TEMP", 35.0)
CRITICAL_TEMP: float = _env_float("CRITICAL_TEMP", 55.0)
# Seuil critique des AUTRES sondes du liquide diélectrique (PT01 ; PT02 relève de CRITICAL_TEMP) :
# c'est ce liquide qui baigne et protège le matériel. Par défaut le même que CRITICAL_TEMP ; à
# fixer avec l'exploitant.
DIELECTRIC_CRITICAL_TEMP: float = _env_float("DIELECTRIC_CRITICAL_TEMP", CRITICAL_TEMP)
# Point de départ prudent pour un procédé thermique lent échantillonné toutes les 60 s :
# Kp = 10 %/°C, temps d'intégration Ti = Kp/Ki = 500 s (≈ 8 cycles), pas de dérivée (sur une
# mesure à 0,1 °C de résolution échantillonnée à 60 s, elle n'amplifie que le bruit).
# Les anciennes valeurs (5 / 0,1 / 1) donnaient Ti = 50 s, plus court qu'un cycle de calcul :
# intégrale dominante, consigne vite saturée à 100 % pour 1 °C d'écart. À régler sur site.
PID_KP: float = _env_float("PID_KP", 10.0)
PID_KI: float = _env_float("PID_KI", 0.02)
PID_KD: float = _env_float("PID_KD", 0.0)
# Âge maximal (secondes) du dernier message reçu de la passerelle pour que la boucle de
# contrôle publie : au-delà, les mesures sont considérées périmées et RIEN n'est publié
# (l'automate retombe sur sa propre régulation). À augmenter seulement si la passerelle
# n'envoie ses valeurs que sur changement.
CONTROL_MAX_DATA_AGE_SECONDS: float = _env_float("CONTROL_MAX_DATA_AGE_SECONDS", 120.0)
# Publie aussi la consigne PID en pourcentage (voltaneum/control/setpoint) : INFORMATIVE
# uniquement (suivi / comparaison avec l'automate). Les conditions à suivre sont publiées
# séparément sur voltaneum/control/cooling_conditions.
PUBLISH_PID_SETPOINT: bool = _env_bool("PUBLISH_PID_SETPOINT", True)

# ---------------------------------------------------------------------------
# Conditions de refroidissement publiées sur MQTT (control/cooling_advice.py)
# ---------------------------------------------------------------------------
# VALEURS INDICATIVES, À VALIDER AVEC L'AUTOMATICIEN (rien n'est calibré sur le vrai système).
# Chaque bande : (limite haute EXCLUE en °C, nom, décalage du seuil de marche en °C,
#                 fréquence minimale conseillée en Hz).
# Seuil de marche du ventilateur = TARGET_TEMP + décalage. Air extérieur froid : le dry cooler
# refroidit plus facilement, on peut démarrer plus tard (+). Air chaud : on démarre plus tôt (-).
OUTDOOR_BANDS: tuple[tuple[float, str, float, float], ...] = (
    (10.0, "froid", 2.0, 20.0),
    (25.0, "tempere", 0.0, 30.0),
    (float("inf"), "chaud", -2.0, 40.0),
)
# Écart entre le seuil de marche et le seuil d'arrêt (évite les marche/arrêt rapprochés).
FAN_HYSTERESIS_CELSIUS: float = 2.0

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
#   "http" (repli)      : le capteur expose une petite API HTTP (OUTDOOR_SENSOR_HTTP_URL) ;
#   "none"              : aucun capteur installé (rien n'est écouté ni interrogé : la
#                         température vient de l'API météo, si le repli est activé).
# ---------------------------------------------------------------------------
# Défaut "none" : aucun capteur extérieur n'est installé à ce jour (repli météo).
OUTDOOR_SENSOR_MODE: str = _env_str("OUTDOOR_SENSOR_MODE", "none").lower()
OUTDOOR_SENSOR_MQTT_TOPIC: str = _env_str("OUTDOOR_SENSOR_MQTT_TOPIC", "voltaneum/sensors/outdoor_temperature")
OUTDOOR_SENSOR_HTTP_URL: str = _env_str("OUTDOOR_SENSOR_HTTP_URL")
OUTDOOR_SENSOR_HTTP_TIMEOUT: float = _env_float("OUTDOOR_SENSOR_HTTP_TIMEOUT", 5.0)
# Une mesure plus ancienne que ce délai est considérée comme absente (capteur muet).
OUTDOOR_SENSOR_MAX_AGE_SECONDS: int = _env_int("OUTDOOR_SENSOR_MAX_AGE_SECONDS", 1800)

# Repli sur l'API météo quand le capteur ne fournit pas de valeur (absent, muet ou
# défaillant). Le capteur reste TOUJOURS prioritaire dès qu'il a une valeur actuelle ;
# la source utilisée est enregistrée en base (colonne `temp_outdoor_source`).
OUTDOOR_WEATHER_FALLBACK_ENABLED: bool = _env_bool("OUTDOOR_WEATHER_FALLBACK_ENABLED", True)
# Intervalle minimal entre deux appels à l'API météo (secondes). L'API ne met à jour sa valeur
# « actuelle » que toutes les 15 min : l'interroger plus souvent n'apporte rien (5 760 appels
# par jour à 15 s) et inonde le journal d'erreurs dès que le réseau tombe. Chaque instantané
# reste renseigné avec la dernière valeur obtenue (tant qu'elle a moins de
# OUTDOOR_SENSOR_MAX_AGE_SECONDS).
OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS: int = _env_int("OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS", 600)

# Vérification de cohérence capteur / météo (n'a lieu que si le capteur fournit une
# valeur : la météo n'est jamais comparée à elle-même). Seuil large : un capteur local et une moyenne régionale
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
# Règle « pompe en marche sans débit » (risque de marche à sec). VALEURS INDICATIVES, à
# valider avec l'automaticien : fréquence au-dessus de laquelle la pompe est considérée en
# marche (Hz), et débit en dessous duquel il n'y a « pas de débit » (m³/h).
PUMP_RUNNING_MIN_HZ: float = _env_float("PUMP_RUNNING_MIN_HZ", 5.0)
FLOW_MIN_M3H: float = _env_float("FLOW_MIN_M3H", 0.5)
# Règle « dérive d'efficacité » : formule et bornes NON validées sur l'installation, donc
# désactivée par défaut (une alerte sans fondement fait ignorer les vraies).
EFFICIENCY_ALERT_ENABLED: bool = _env_bool("EFFICIENCY_ALERT_ENABLED", False)
EFFICIENCY_MIN_EXPECTED: float = _env_float("EFFICIENCY_MIN_EXPECTED", 0.0)
EFFICIENCY_MAX_EXPECTED: float = _env_float("EFFICIENCY_MAX_EXPECTED", 1.0)
# PT02 (grandeur régulée) sans valeur alors que la passerelle émet : alerte après ce délai (s).
OIL_SENSOR_MISSING_ALERT_SECONDS: int = _env_int("OIL_SENSOR_MISSING_ALERT_SECONDS", 120)
# PT02 strictement constante pendant ce délai (min) : valeur suspecte (0 = règle désactivée).
SENSOR_FROZEN_ALERT_MINUTES: int = _env_int("SENSOR_FROZEN_ALERT_MINUTES", 60)

# ---------------------------------------------------------------------------
# Protection automatique du stockage (purge pilotée par la TAILLE de la base)
# ---------------------------------------------------------------------------
STORAGE_LIMIT_BYTES: int = _env_int("STORAGE_LIMIT_BYTES", 1024**4)          # 1 Tio
# La limite EFFECTIVE ne dépasse jamais cette part du disque réellement disponible pour la base
# (taille actuelle + espace libre) : une limite de 1 Tio sur un disque de 64 Go ne protégeait
# rien, le disque se remplissait avant le premier avertissement.
STORAGE_DISK_MAX_SHARE: float = _env_float("STORAGE_DISK_MAX_SHARE", 0.8)
STORAGE_WARNING_RATIO: float = _env_float("STORAGE_WARNING_RATIO", 0.90)
STORAGE_PURGE_TARGET_RATIO: float = _env_float("STORAGE_PURGE_TARGET_RATIO", 0.75)
STORAGE_GRACE_PERIOD_SECONDS: int = _env_int("STORAGE_GRACE_PERIOD_SECONDS", 3600)
STORAGE_CHECK_INTERVAL_SECONDS: int = _env_int("STORAGE_CHECK_INTERVAL_SECONDS", 300)
STORAGE_PURGE_BATCH_SIZE: int = _env_int("STORAGE_PURGE_BATCH_SIZE", 1000)
# Idéalement un AUTRE volume que la base, avec au moins autant d'espace libre qu'elle.
STORAGE_BACKUP_DIR: str = _env_path("STORAGE_BACKUP_DIR", "backups")
STORAGE_BACKUP_RETENTION_COUNT: int = _env_int("STORAGE_BACKUP_RETENTION_COUNT", 7)
# Âge minimal (jours) d'une sauvegarde compressée avant qu'elle puisse être supprimée :
# PLANCHER de conservation. Une archive plus récente n'est JAMAIS supprimée, même au-delà des
# STORAGE_BACKUP_RETENTION_COUNT plus récentes (exigence : données gardées au moins un an).
STORAGE_BACKUP_MIN_AGE_DAYS: int = _env_int("STORAGE_BACKUP_MIN_AGE_DAYS", 365)
# Espace libre exigé sur le volume de sauvegarde, en multiple de la taille de la base
# (copie temporaire non compressée + archive). En dessous : sauvegarde refusée, RIEN supprimé.
STORAGE_BACKUP_FREE_SPACE_FACTOR: float = _env_float("STORAGE_BACKUP_FREE_SPACE_FACTOR", 1.1)
STORAGE_ALERT_COOLDOWN_MINUTES: int = _env_int("STORAGE_ALERT_COOLDOWN_MINUTES", 60)
# Sauvegarde QUOTIDIENNE de la base (protection contre la corruption ou une fausse manœuvre),
# indépendante des archives faites avant purge : les N plus récentes sont gardées dans
# STORAGE_BACKUP_DIR/daily (0 = désactivée).
STORAGE_DAILY_BACKUP_COUNT: int = _env_int("STORAGE_DAILY_BACKUP_COUNT", 7)

# ---------------------------------------------------------------------------
# Interface web de supervision (dans le processus principal) — LECTURE SEULE
# ---------------------------------------------------------------------------
# Par défaut, écoute sur la machine locale uniquement : depuis un autre poste, passer par un
# tunnel SSH (voir README). L'interface n'a pas d'authentification : ne l'ouvrir au réseau
# (DASHBOARD_HOST=0.0.0.0) que sur un réseau de confiance.
DASHBOARD_HOST: str = _env_str("DASHBOARD_HOST", "127.0.0.1")
DASHBOARD_PORT: int = _env_int("DASHBOARD_PORT", 8080)
# Noms d'hôte supplémentaires acceptés dans l'en-tête Host (ex. "controller,supervision.local").
DASHBOARD_ALLOWED_HOSTS: tuple[str, ...] = tuple(
    h.strip().lower() for h in _env_str("DASHBOARD_ALLOWED_HOSTS").split(",") if h.strip()
)

DASHBOARD_ENABLED: bool = _env_bool("DASHBOARD_ENABLED", True)

# Heure (locale, "HH:MM") du ré-entraînement automatique quotidien ; vide = désactivé.
RETRAIN_AT: str = _env_str("RETRAIN_AT", "03:15")

# ---------------------------------------------------------------------------
# Journalisation
# ---------------------------------------------------------------------------
LOG_LEVEL: str = _env_str("LOG_LEVEL", "INFO").upper()

_LOG_LEVELS = {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}


def _valid_hhmm(value: str) -> bool:
    parts = value.split(":")
    return (len(parts) == 2 and all(p.isdigit() for p in parts)
            and 0 <= int(parts[0]) <= 23 and 0 <= int(parts[1]) <= 59)


def _require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(f"Configuration invalide : {message}")


def _validate_cooling_bands() -> None:
    """Contrôle la table OUTDOOR_BANDS : bandes ordonnées, seuils sous le seuil critique."""
    _require(len(OUTDOOR_BANDS) > 0, "OUTDOOR_BANDS ne peut pas être vide.")
    previous_limit = float("-inf")
    freq_low, freq_high = SENSOR_VALID_RANGES["freq_pompe"]
    for upper_limit, name, offset, min_freq_hz in OUTDOOR_BANDS:
        _require(upper_limit > previous_limit,
                 f"OUTDOOR_BANDS : limites hautes non strictement croissantes (bande {name!r}).")
        previous_limit = upper_limit
        fan_on = TARGET_TEMP + offset
        _require(fan_on < CRITICAL_TEMP,
                 f"OUTDOOR_BANDS : le seuil de marche de la bande {name!r} ({fan_on}) doit rester "
                 f"sous CRITICAL_TEMP ({CRITICAL_TEMP}).")
        _require(fan_on - FAN_HYSTERESIS_CELSIUS < fan_on, "FAN_HYSTERESIS_CELSIUS doit être positif.")
        _require(freq_low <= min_freq_hz <= freq_high,
                 f"OUTDOOR_BANDS : la fréquence de la bande {name!r} ({min_freq_hz} Hz) sort de la plage "
                 f"valide de freq_pompe [{freq_low}, {freq_high}].")
    _require(previous_limit == float("inf"),
             "OUTDOOR_BANDS : la dernière bande doit être ouverte (limite haute = inf).")


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
    for name, port in (("MQTT_PORT", MQTT_PORT), ("SMTP_PORT", SMTP_PORT), ("DASHBOARD_PORT", DASHBOARD_PORT)):
        _require(1 <= port <= 65535, f"{name} doit être compris entre 1 et 65535, reçu {port}.")
    for name, value in {
        "MQTT_KEEPALIVE_SECONDS": MQTT_KEEPALIVE_SECONDS, "SNAPSHOT_INTERVAL_SECONDS": SNAPSHOT_INTERVAL_SECONDS,
        "CONTROL_LOOP_INTERVAL_SECONDS": CONTROL_LOOP_INTERVAL_SECONDS,
        "POINT_MAPPING_REFRESH_SECONDS": POINT_MAPPING_REFRESH_SECONDS, "MAX_GAP_SECONDS": MAX_GAP_SECONDS,
        "TELEMETRY_TIMEOUT_SECONDS": TELEMETRY_TIMEOUT_SECONDS, "ALERT_CONFIRMATION_CYCLES": ALERT_CONFIRMATION_CYCLES,
        "OUTDOOR_SENSOR_HTTP_TIMEOUT": OUTDOOR_SENSOR_HTTP_TIMEOUT,
        "OUTDOOR_SENSOR_MAX_AGE_SECONDS": OUTDOOR_SENSOR_MAX_AGE_SECONDS,
        "OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS": OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS,
        "OUTDOOR_CONSISTENCY_CHECK_INTERVAL_SECONDS": OUTDOOR_CONSISTENCY_CHECK_INTERVAL_SECONDS,
        "OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS": OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS,
        "STORAGE_LIMIT_BYTES": STORAGE_LIMIT_BYTES, "STORAGE_CHECK_INTERVAL_SECONDS": STORAGE_CHECK_INTERVAL_SECONDS,
        "STORAGE_PURGE_BATCH_SIZE": STORAGE_PURGE_BATCH_SIZE,
        "STORAGE_BACKUP_RETENTION_COUNT": STORAGE_BACKUP_RETENTION_COUNT,
        "CONTROL_MAX_DATA_AGE_SECONDS": CONTROL_MAX_DATA_AGE_SECONDS,
        "FAN_HYSTERESIS_CELSIUS": FAN_HYSTERESIS_CELSIUS, "FLOW_MIN_M3H": FLOW_MIN_M3H,
        "STORAGE_BACKUP_FREE_SPACE_FACTOR": STORAGE_BACKUP_FREE_SPACE_FACTOR,
    }.items():
        _require(value > 0, f"{name} doit être strictement positif, reçu {value}.")
    for name, value in {
        "ALERT_COOLDOWN_MINUTES": ALERT_COOLDOWN_MINUTES, "STORAGE_ALERT_COOLDOWN_MINUTES": STORAGE_ALERT_COOLDOWN_MINUTES,
        "STORAGE_GRACE_PERIOD_SECONDS": STORAGE_GRACE_PERIOD_SECONDS, "WS_SILENCE_TIMEOUT_SECONDS": WS_SILENCE_TIMEOUT_SECONDS,
        "SENSOR_MAX_AGE_SECONDS": SENSOR_MAX_AGE_SECONDS,
        "RETENTION_DAYS": RETENTION_DAYS, "PREDICTIONS_RETENTION_DAYS": PREDICTIONS_RETENTION_DAYS,
        "ALERTS_RETENTION_DAYS": ALERTS_RETENTION_DAYS,
        "PID_KP": PID_KP, "PID_KI": PID_KI, "PID_KD": PID_KD,
        "STORAGE_BACKUP_MIN_AGE_DAYS": STORAGE_BACKUP_MIN_AGE_DAYS, "PUMP_RUNNING_MIN_HZ": PUMP_RUNNING_MIN_HZ,
        "STORAGE_DAILY_BACKUP_COUNT": STORAGE_DAILY_BACKUP_COUNT,
    }.items():
        _require(value >= 0, f"{name} ne peut pas être négatif, reçu {value}.")

    # Sécurité process
    _require(len(AUTO_RETRAIN_HORIZONS_SECONDS) > 0 and all(h > 0 for h in AUTO_RETRAIN_HORIZONS_SECONDS),
             "AUTO_RETRAIN_HORIZONS doit lister des durées positives en secondes (ex. 600,1800,3600).")
    _require(0.0 <= AUTO_RETRAIN_MIN_GAIN < 1.0, "AUTO_RETRAIN_MIN_GAIN doit être compris entre 0 et 1.")
    _require(AUTO_RETRAIN_MAX_RMSE_CELSIUS > 0 and AUTO_RETRAIN_MAX_DAYS > 0,
             "AUTO_RETRAIN_MAX_RMSE_CELSIUS et AUTO_RETRAIN_MAX_DAYS doivent être positifs.")
    _require(CRITICAL_TEMP > TARGET_TEMP, f"CRITICAL_TEMP ({CRITICAL_TEMP}) doit dépasser TARGET_TEMP ({TARGET_TEMP}).")
    _require(SETPOINT_MIN < SETPOINT_MAX, "SETPOINT_MIN doit être inférieur à SETPOINT_MAX.")
    _require(EFFICIENCY_MIN_EXPECTED < EFFICIENCY_MAX_EXPECTED,
             "EFFICIENCY_MIN_EXPECTED doit être inférieur à EFFICIENCY_MAX_EXPECTED.")
    _require(0.0 < STORAGE_PURGE_TARGET_RATIO < STORAGE_WARNING_RATIO <= 1.0,
             "il faut 0 < STORAGE_PURGE_TARGET_RATIO < STORAGE_WARNING_RATIO <= 1.")
    # Invariant de sécurité : une surchauffe réelle doit rester MESURABLE (voir SENSOR_VALID_RANGES).
    oil_upper_bound = SENSOR_VALID_RANGES[TARGET_COLUMN][1]
    _require(oil_upper_bound > CRITICAL_TEMP,
             f"la borne haute valide de {TARGET_COLUMN} ({oil_upper_bound}) doit dépasser CRITICAL_TEMP "
             f"({CRITICAL_TEMP}) : sinon une surchauffe serait rejetée comme « hors plage » et jamais détectée.")
    _validate_cooling_bands()

    # MQTT : TLS toujours vérifié
    _require(not MQTT_TLS_INSECURE,
             "MQTT_TLS_INSECURE est interdit : la vérification TLS ne doit jamais être désactivée.")
    _require(not (MQTT_PASSWORD and not MQTT_USERNAME), "MQTT_PASSWORD ne peut pas être défini sans MQTT_USERNAME.")
    if MQTT_TLS_ENABLED:
        for name, path in (("MQTT_TLS_CA_CERT", MQTT_TLS_CA_CERT), ("MQTT_TLS_CERTFILE", MQTT_TLS_CERTFILE),
                           ("MQTT_TLS_KEYFILE", MQTT_TLS_KEYFILE)):
            _require(not path or os.path.isfile(path), f"{name} introuvable : {path!r}.")

    # Capteur extérieur
    _require({THERMAL_POWER_HOT, THERMAL_POWER_COLD} <= {"pt01", "pt02", "pt03", "pt04"}
             and THERMAL_POWER_HOT != THERMAL_POWER_COLD,
             "THERMAL_POWER_HOT et THERMAL_POWER_COLD doivent être deux sondes différentes parmi pt01..pt04.")
    _require(THERMAL_FLUID_DENSITY_KG_M3 > 0 and THERMAL_FLUID_HEAT_CAPACITY_J_KG_K > 0,
             "THERMAL_FLUID_DENSITY_KG_M3 et THERMAL_FLUID_HEAT_CAPACITY_J_KG_K doivent être positifs.")
    dielectric_upper = min(SENSOR_VALID_RANGES[c][1] for c in DIELECTRIC_COLUMNS)
    _require(0 < DIELECTRIC_CRITICAL_TEMP < dielectric_upper,
             f"DIELECTRIC_CRITICAL_TEMP ({DIELECTRIC_CRITICAL_TEMP}) doit être positif et sous la borne haute des "
             f"sondes du diélectrique ({dielectric_upper}) pour qu'une surchauffe reste mesurable.")
    _require(0.0 < STORAGE_DISK_MAX_SHARE <= 1.0, "STORAGE_DISK_MAX_SHARE doit être compris entre 0 et 1.")
    _require(not RETRAIN_AT or _valid_hhmm(RETRAIN_AT), f"RETRAIN_AT doit être au format HH:MM, reçu {RETRAIN_AT!r}.")
    _require(OIL_SENSOR_MISSING_ALERT_SECONDS > 0 and SENSOR_FROZEN_ALERT_MINUTES >= 0,
             "OIL_SENSOR_MISSING_ALERT_SECONDS doit être positif et SENSOR_FROZEN_ALERT_MINUTES >= 0.")
    _require(OUTDOOR_SENSOR_MODE in ("mqtt", "http", "none"),
             f"OUTDOOR_SENSOR_MODE doit être 'mqtt', 'http' ou 'none', reçu {OUTDOOR_SENSOR_MODE!r}.")
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
