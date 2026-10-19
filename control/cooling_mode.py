"""control/cooling_mode.py — L'air extérieur seul peut-il refroidir ? (calcul pur).

Principe (« free cooling ») : par air froid, le dry cooler évacue assez de chaleur ventilateur
ARRÊTÉ, par simple circulation naturelle de l'air à travers ses ailettes. C'est la première étape
proposée par control/supervisor.py quand PT01 se réchauffe ; si elle ne suffit pas (PT01 ne baisse
pas après le délai d'effet), le superviseur passe au ventilateur.

L'air seul est possible si :
1. l'air extérieur est sous FREE_COOLING_MAX_OUTDOOR_TEMP (10 °C), avec une hystérésis : une fois
   passé dessous, il faut remonter au-dessus de 10 + FREE_COOLING_HYSTERESIS_CELSIUS pour
   l'interdire (une température qui hésite autour de 10 °C ne fait pas alterner les conseils) ;
2. l'écart PT01 − air atteint FREE_COOLING_MIN_GAP_CELSIUS : sans écart, l'échange naturel est trop
   faible pour évacuer la chaleur.
"""

from __future__ import annotations

import math

import config


def free_cooling_possible(t_oil: float, t_outdoor: float | None, state: dict) -> bool:
    """Vrai si l'air extérieur seul peut suffire. Met à jour `state["air_allowed"]` (hystérésis)."""
    if not config.FREE_COOLING_ENABLED or t_outdoor is None or not math.isfinite(t_outdoor):
        return False
    limit = config.FREE_COOLING_MAX_OUTDOOR_TEMP
    allowed = state.get("air_allowed")
    if allowed is None or (not allowed and t_outdoor < limit):
        allowed = t_outdoor < limit
    elif allowed and t_outdoor >= limit + config.FREE_COOLING_HYSTERESIS_CELSIUS:
        allowed = False
    state["air_allowed"] = allowed
    return allowed and t_oil - t_outdoor >= config.FREE_COOLING_MIN_GAP_CELSIUS
