"""control/cooling_advice.py — Conditions de refroidissement à publier sur MQTT (calcul pur).

RÈGLE DE SÉCURITÉ n°2 : aucun appel réseau, aucun import d'actionneur. Ce module reçoit des
nombres et rend un dictionnaire ; c'est la boucle de contrôle (service/app.py) qui le publie sur MQTT, et c'est l'AUTOMATE
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


def fan_thresholds(offset: float) -> tuple[float, float]:
    """(seuil de marche, seuil d'arrêt) du ventilateur pour un décalage de bande.

    Bande centrée sur la cible décalée : en tout-ou-rien, la température oscille entre les deux
    seuils et sa moyenne tombe au milieu, donc sur la cible visée."""
    centre = config.TARGET_TEMP + offset
    return centre + config.FAN_HYSTERESIS_CELSIUS / 2, centre - config.FAN_HYSTERESIS_CELSIUS / 2


def mean_load_delta(hot: list[tuple[float, float]], cold: list[tuple[float, float]],
                    min_points: int = 4) -> float | None:
    """Moyenne de la sonde chaude moins moyenne de la sonde froide sur une fenêtre ([(instant,
    valeur)]). La moyenne lisse la lecture au degré près de PT02. None si trop peu de mesures."""
    if len(hot) < min_points or len(cold) < min_points:
        return None
    return sum(v for _, v in hot) / len(hot) - sum(v for _, v in cold) / len(cold)


def inlet_setpoint_advice(load_delta_t: float | None) -> float | None:
    """Consigne de PT01 qui tiendrait PT02 à la cible en régime établi (régulation en cascade) :
    TARGET_TEMP − ΔT, ΔT = moyenne récente de PT02 − PT01 (négatif ramené à 0), bornée à
    [INLET_SETPOINT_MIN, TARGET_TEMP]. None si ΔT est inconnu."""
    if load_delta_t is None or not math.isfinite(load_delta_t):
        return None
    advice = config.TARGET_TEMP - max(0.0, load_delta_t)
    return round(min(config.TARGET_TEMP, max(config.INLET_SETPOINT_MIN, advice)), 1)


def compute_cooling_conditions(t_oil: float, t_outdoor: float | None,
                               load_delta_t: float | None = None) -> dict | None:
    """Conditions de refroidissement à publier, ou None si la grandeur surveillée (PT02) est invalide
    (on ne publie rien plutôt que de publier des conditions sur une mesure fausse).
    `load_delta_t` : moyenne récente de PT02 − PT01 (°C), pour le conseil de consigne de PT01."""
    if t_oil is None or not math.isfinite(t_oil):
        return None

    band, offset, min_freq_hz = select_outdoor_band(t_outdoor)
    fan_on_above, fan_off_below = fan_thresholds(offset)
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
        # Régulation en cascade : échauffement mesuré dans l'armoire et consigne de PT01 conseillée.
        "load_delta_t": None if load_delta_t is None or not math.isfinite(load_delta_t) else round(load_delta_t, 2),
        "inlet_setpoint_advice": inlet_setpoint_advice(load_delta_t),
        # Au-delà de cette durée sans nouveau message, l'automate doit ignorer ces conditions et
        # reprendre sa propre régulation. Les messages ne partent qu'aux étapes (control/supervisor.py) :
        # pendant une demande, un message au plus tard à chaque vérification de l'effet.
        "valid_for_seconds": config.REGULATION_EFFECT_WAIT_SECONDS + 3 * config.CONTROL_LOOP_INTERVAL_SECONDS,
    }
