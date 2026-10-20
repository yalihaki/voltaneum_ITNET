"""control/trend.py — Tendance d'une température (°C par 10 min), par moindres carrés (calcul pur).

Une simple différence « maintenant − il y a 10 min » ne vaut rien sur PT02, lue au degré près : un
seul pas de quantification ferait croire à +1 °C / 10 min. La pente de la droite ajustée sur
toutes les mesures de la fenêtre (une toutes les 15 s) lisse ces marches.
"""

from __future__ import annotations

import math

# En dessous, la pente n'est pas calculée (fenêtre trop pauvre ou trop courte pour conclure).
MIN_POINTS = 5
MIN_SPAN_RATIO = 0.5   # les points doivent couvrir au moins la moitié de la fenêtre demandée


def slope_per_10min(points: list[tuple[float, float]], window_seconds: float) -> float | None:
    """Pente (°C / 10 min) des points (instant en s, valeur), ou None si elle n'est pas fiable."""
    clean = [(t, v) for t, v in points if math.isfinite(t) and math.isfinite(v)]
    if len(clean) < MIN_POINTS:
        return None
    times = [t for t, _ in clean]
    if max(times) - min(times) < MIN_SPAN_RATIO * window_seconds:
        return None
    t_mean = sum(times) / len(times)
    v_mean = sum(v for _, v in clean) / len(clean)
    var = sum((t - t_mean) ** 2 for t in times)
    if var == 0:
        return None
    cov = sum((t - t_mean) * (v - v_mean) for t, v in clean)
    return cov / var * 600.0
