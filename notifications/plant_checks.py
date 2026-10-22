"""notifications/plant_checks.py — Contrôles de l'INSTALLATION tirés des données déjà reçues.

Voltaneum reçoit, en lecture seule, le catalogue complet de l'automate (gateway_catalogue : tous les
points, utilisés ou non, avec leur dernière valeur) et les mesures. L'analyse des relevés réels du
12 au 22/10/2026 a fait apparaître trois situations qu'il faut surveiller en permanence plutôt que
de les découvrir à la main :

  1. safety_bypass_active (CRITIQUE) : un shunt de sécurité de l'automate est actif
     (`Emergency stop bypass`, `Phase sequence bypass` à vrai). Constaté le 22/10/2026 sur les deux.
     Un arrêt d'urgence shunté ne protège plus personne : à lever dès que la raison a disparu.
  2. automate_setpoint_mismatch (ATTENTION) : la consigne de température de l'automate
     (`Temperature setpoint`, `DIAG Oil temperature setting VD500`) s'écarte de TARGET_TEMP de plus
     de AUTOMATE_SETPOINT_TOLERANCE_CELSIUS. Constaté : 36 °C côté automate, 35 °C côté Voltaneum ;
     les deux systèmes ne visent alors pas la même température.
  3. fan_short_cycling (ATTENTION) : le ventilateur du dry cooler démarre plus de
     FAN_STARTS_PER_HOUR_MAX fois par heure. Il n'est pas mesuré directement : chaque démarrage se
     voit sur PT04 (retour du dry cooler), qui perd au moins FAN_START_DROP_CELSIUS en 30 s.
     Constaté : 236 démarrages par jour, un toutes les 5 min (12 par heure, jusqu'à 18 par air
     chaud), ce qui use le moteur et les contacteurs.

Les trois sont des situations DURABLES : rappel une fois par jour au plus (notifications/alerts.py,
RULE_COOLDOWN_MINUTES). Voltaneum ne corrige rien lui-même : il n'écrit jamais dans l'automate.
Une valeur de catalogue trop ancienne (point qui ne remonte plus) n'est pas jugée.
"""

from __future__ import annotations

import sqlite3
import statistics
import time

import config
from database import sqlite_client

# Points de l'automate qui signalent un shunt de sécurité actif quand ils valent « vrai ».
SAFETY_BYPASS_POINTS: tuple[str, ...] = ("Emergency stop bypass", "Phase sequence bypass")
# Points qui portent la consigne de température de l'automate (°C).
AUTOMATE_SETPOINT_POINTS: tuple[str, ...] = ("Temperature setpoint", "DIAG Oil temperature setting VD500")
# Une valeur du catalogue plus ancienne que ce délai n'est pas jugée (point muet).
CATALOGUE_VALUE_MAX_AGE_SECONDS = 3600
# Deux baisses de PT04 séparées de moins de ce délai sont un même démarrage du ventilateur.
FAN_START_MIN_SPACING_SECONDS = 120
# La baisse doit se faire sur au plus ce délai (2 instantanés de 15 s, avec une marge).
FAN_START_MAX_DROP_SECONDS = 45


def _is_true(raw: object) -> bool:
    return str(raw).strip().lower() in {"true", "1", "on", "yes", "vrai"}


def _number(raw: object) -> float | None:
    try:
        return float(str(raw).strip())
    except (TypeError, ValueError):
        return None


def catalogue_findings(points: dict[str, object]) -> list[tuple[str, str, str]]:
    """Alertes (niveau, règle, message) à partir des dernières valeurs du catalogue, par KeyName."""
    findings: list[tuple[str, str, str]] = []
    active = [name for name in SAFETY_BYPASS_POINTS if name in points and _is_true(points[name])]
    if active:
        findings.append(("CRITIQUE", "safety_bypass_active",
                         f"Shunt de sécurité actif sur l'automate : {', '.join(active)}. Un shunt d'arrêt "
                         "d'urgence ou de contrôle des phases supprime une protection : le lever dès que sa "
                         "raison a disparu (à voir avec l'automaticien)."))
    setpoints = {name: value for name in AUTOMATE_SETPOINT_POINTS
                 if name in points and (value := _number(points[name])) is not None}
    gaps = {name: value for name, value in setpoints.items()
            if abs(value - config.TARGET_TEMP) > config.AUTOMATE_SETPOINT_TOLERANCE_CELSIUS}
    if gaps:
        detail = ", ".join(f"{name} = {value:g} °C" for name, value in gaps.items())
        findings.append(("ATTENTION", "automate_setpoint_mismatch",
                         f"Consigne de l'automate différente de la cible de Voltaneum ({config.TARGET_TEMP:g} °C) : "
                         f"{detail}. Les deux systèmes ne visent pas la même température : aligner TARGET_TEMP "
                         "(.env) et la consigne de l'automate."))
    return findings


def fan_starts(pt04_points: list[tuple[float, float]],
               drop_celsius: float | None = None) -> list[float]:
    """Instants (epoch s) des démarrages du ventilateur du dry cooler, déduits de PT04 : baisse d'au
    moins `drop_celsius` (FAN_START_DROP_CELSIUS) sur les deux mesures suivantes, en
    FAN_START_MAX_DROP_SECONDS au plus ; les baisses à moins de FAN_START_MIN_SPACING_SECONDS l'une
    de l'autre comptent pour un seul démarrage. Les pics isolés doivent avoir été retirés avant
    (sqlite_client.recent_values le fait)."""
    drop = config.FAN_START_DROP_CELSIUS if drop_celsius is None else drop_celsius
    starts: list[float] = []
    for i in range(len(pt04_points) - 2):
        t0, v0 = pt04_points[i]
        t2, v2 = pt04_points[i + 2]
        if t2 - t0 <= FAN_START_MAX_DROP_SECONDS and v0 - v2 >= drop:
            if not starts or t0 - starts[-1] > FAN_START_MIN_SPACING_SECONDS:
                starts.append(t0)
    return starts


def fan_findings(pt04_points: list[tuple[float, float]], now: float) -> list[tuple[str, str, str]]:
    """Alerte si le ventilateur démarre plus de FAN_STARTS_PER_HOUR_MAX fois sur la dernière heure."""
    limit = config.FAN_STARTS_PER_HOUR_MAX
    if not limit:
        return []
    starts = [t for t in fan_starts(pt04_points) if t > now - 3600]
    if len(starts) <= limit:
        return []
    gaps = [b - a for a, b in zip(starts, starts[1:])]
    every = f", un toutes les {statistics.median(gaps) / 60:.1f} min" if gaps else ""
    return [("ATTENTION", "fan_short_cycling",
             f"Ventilateur du dry cooler : {len(starts)} démarrages sur la dernière heure{every} "
             f"(limite {limit} par heure). Des démarrages aussi rapprochés usent le moteur et les "
             "contacteurs : élargir l'hystérésis ou imposer une durée minimale de marche / d'arrêt dans "
             "l'automate. Démarrages déduits des chutes rapides de PT04.")]


def run_plant_checks(db_path: str = config.SQLITE_DB_PATH, now: float | None = None) -> list[tuple[str, str, str]]:
    """Tous les contrôles de l'installation, à partir de la base. Ne lève jamais d'exception."""
    now = time.time() if now is None else now
    findings: list[tuple[str, str, str]] = []
    try:
        with sqlite_client.connect(db_path) as conn:
            rows = conn.execute(
                "SELECT key_name, last_value FROM gateway_catalogue "
                "WHERE dev_node = ? AND last_value IS NOT NULL AND last_seen >= ?",
                (config.SENSOR_DRIVER_NODE,
                 time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(now - CATALOGUE_VALUE_MAX_AGE_SECONDS))),
            ).fetchall()
        findings.extend(catalogue_findings({name: value for name, value in rows if name}))
    except sqlite3.Error:
        pass   # catalogue pas encore relevé (table absente) : rien à juger
    pt04 = sqlite_client.recent_values(config.COOLANT_COLD_COLUMN, now - 3600 - 60, db_path=db_path)
    findings.extend(fan_findings(pt04, now))
    return findings
