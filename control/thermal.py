"""control/thermal.py — Puissance thermique dissipée ESTIMÉE à partir des mesures (calcul pur).

P (kW) = débit (m³/h) / 3600 × masse volumique (kg/m³) × capacité thermique (J/kg/K)
         × (T chaude − T froide) / 1000

Sondes et propriétés du fluide sont réglables (config.THERMAL_POWER_*, OIL_*) et restent À
CONFIRMER avec l'automaticien : tant que ce n'est pas fait, la valeur est une estimation.
Un écart de température légèrement négatif (bruit des sondes à faible charge) est conservé tel
quel, sans être déguisé en zéro.
"""

from __future__ import annotations

import math

import config


def estimate_power_kw(values: dict) -> float | None:
    """Puissance estimée (kW), ou None si le débit ou l'une des deux températures manque."""
    flow = values.get("ht01")
    hot = values.get(config.THERMAL_POWER_HOT)
    cold = values.get(config.THERMAL_POWER_COLD)
    if flow is None or hot is None or cold is None:
        return None
    if not all(math.isfinite(v) for v in (flow, hot, cold)):
        return None
    watts = flow / 3600.0 * config.THERMAL_FLUID_DENSITY_KG_M3 * config.THERMAL_FLUID_HEAT_CAPACITY_J_KG_K * (hot - cold)
    return round(watts / 1000.0, 3)
