"""control/cooling_advice.py — Conditions de refroidissement à publier sur MQTT (calcul pur).

RÈGLE DE SÉCURITÉ n°2 : aucun appel réseau, aucun import d'actionneur. Ce module reçoit des
nombres et rend un dictionnaire ; c'est main.py qui le publie sur MQTT, et c'est l'AUTOMATE
(ou son pont) qui décide seul de démarrer ou d'arrêter le ventilateur.

Il ne donne donc JAMAIS d'ordre « allume / éteins » : il publie la température cible et les
conditions à suivre (seuil de marche, seuil d'arrêt, fréquence minimale conseillée, seuil
critique), choisies selon la température extérieure.
"""

from __future__ import annotations

import math

import config

_UNKNOWN_BAND = "inconnue"


def select_outdoor_band(t_outdoor: float | None) -> tuple[str, float, float | None]:
    """Retourne (nom de bande, décalage du seuil de marche, fréquence minimale conseillée).

    Température extérieure absente ou non finie : bande « inconnue », décalage nul et AUCUNE
    fréquence conseillée (None) — l'automate applique alors ses propres réglages.
    """
    if t_outdoor is None or not math.isfinite(t_outdoor):
        return _UNKNOWN_BAND, 0.0, None
    for upper_limit, name, offset, min_freq_hz in config.OUTDOOR_BANDS:
        if t_outdoor < upper_limit:
            return name, offset, min_freq_hz
    # Ne devrait pas arriver (la dernière bande est ouverte) : repli neutre.
    return _UNKNOWN_BAND, 0.0, None


def compute_cooling_conditions(t_oil: float, t_outdoor: float | None) -> dict | None:
    """Conditions de refroidissement à publier, ou None si la température d'huile est invalide
    (on ne publie rien plutôt que de publier des conditions sur une mesure fausse)."""
    if t_oil is None or not math.isfinite(t_oil):
        return None

    band, offset, min_freq_hz = select_outdoor_band(t_outdoor)
    fan_on_above = config.TARGET_TEMP + offset
    fan_off_below = fan_on_above - config.FAN_HYSTERESIS_CELSIUS
    outdoor_ok = t_outdoor is not None and math.isfinite(t_outdoor)

    return {
        "target_temp": config.TARGET_TEMP,
        "critical_temp": config.CRITICAL_TEMP,
        "outdoor_band": band,
        "fan_on_above_temp": fan_on_above,
        "fan_off_below_temp": fan_off_below,
        "fan_min_freq_hz": min_freq_hz,
        # Valeurs mesurées, pour la traçabilité côté automate.
        "t_oil": t_oil,
        "t_outdoor": t_outdoor if outdoor_ok else None,
        # Au-delà de cette durée sans nouveau message, l'automate doit ignorer ces conditions.
        "valid_for_seconds": 3 * config.CONTROL_LOOP_INTERVAL_SECONDS,
    }
