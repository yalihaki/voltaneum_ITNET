"""database/quality.py — Détection des PICS ISOLÉS dans les mesures de température (calcul pur).

Le problème
-----------
Les relevés du contrôleur contiennent de rares valeurs parasites : une seule mesure qui saute de
plusieurs degrés puis revient dès la mesure suivante. Deux cas réels, vérifiés avec
`./voltaneum data --around` :

    18/10/2026 05:07:50  PT01 : 34,6 → 36,7 → 34,6 °C   (PT02, PT03, PT04 et le débit immobiles)
    16/10/2026 17:21:16  PT04 : 32,9 → 35,2 → 33,0 °C

Un liquide ne peut pas gagner 2 °C et les perdre en 15 secondes, surtout avec 200 L de fluide en
circulation : ce sont des erreurs de lecture (sonde, module d'entrée de l'automate ou
transmission), pas des températures. Les garder fausserait les records du résumé, les tendances,
l'apprentissage des modèles, et pourrait déclencher une alerte critique à tort.

La règle
--------
Une mesure x[i] d'une sonde est un PIC ISOLÉ si, à la fois :

  1. elle s'écarte de la mesure précédente de plus de SPIKE_THRESHOLD_CELSIUS ;
  2. elle s'écarte de la mesure suivante de plus de SPIKE_THRESHOLD_CELSIUS, dans le même sens
     (elle « monte puis redescend », ou l'inverse) ;
  3. la mesure suivante revient près de la précédente (écart ≤ SPIKE_THRESHOLD_CELSIUS / 2).

Ce qu'elle ne rejette PAS (vérifié sur les relevés) :

  - les cycles du ventilateur du dry cooler : PT04 chute de 3 à 4 °C en 30 s mais ne revient pas
    à la mesure suivante (34,2 → 32,0 → 29,8) : c'est une vraie évolution ;
  - les marches de PT02, lue au degré près : 34 → 35 °C fait 1 °C, sous le seuil de 1,5 °C ;
  - un vrai échauffement, qui dure plus d'une mesure.

Il faut connaître la mesure SUIVANTE pour juger une mesure : la toute dernière reçue ne peut donc
pas être classée. C'est pourquoi les alertes critiques, qui travaillent sur la dernière mesure,
demandent en plus une confirmation sur deux mesures consécutives (notifications/alerts.py).

Ce que fait le projet des pics
------------------------------
  - ils restent ENREGISTRÉS en base tels quels (traçabilité : rien n'est effacé ni modifié) ;
  - ils sont ÉCARTÉS de l'apprentissage des modèles (ml/train.py), des tendances calculées en
    service (régulation par étapes, entrées de tendance, échauffement moyen : service/app.py) et
    des minimum / maximum / moyenne de `./voltaneum data`, qui les compte à part.
"""

from __future__ import annotations

import pandas as pd

import config

# Sondes de température contrôlées (débit et fréquence ne sont pas concernés).
SPIKE_COLUMNS: tuple[str, ...] = ("pt01", "pt02", "pt03", "pt04")
# Saut minimal (°C) d'une mesure par rapport à ses deux voisines pour être un pic. Au-dessus de la
# marche de 1 °C de PT02 (lue au degré près), en dessous des pics observés (2,1 et 2,3 °C).
SPIKE_THRESHOLD_CELSIUS: float = config.SPIKE_THRESHOLD_CELSIUS


def is_spike(previous: float, value: float, following: float,
             threshold: float = SPIKE_THRESHOLD_CELSIUS) -> bool:
    """Vrai si `value` est un pic isolé entre `previous` et `following` (règle du module)."""
    up, down = value - previous, value - following
    same_direction = (up > 0) == (down > 0)
    returns = abs(following - previous) <= threshold / 2
    return abs(up) > threshold and abs(down) > threshold and same_direction and returns


def spike_mask(values: pd.Series, threshold: float = SPIKE_THRESHOLD_CELSIUS) -> pd.Series:
    """Masque booléen des pics isolés d'une série ordonnée dans le temps (valeurs absentes ignorées :
    chaque mesure est comparée à la mesure VALIDE qui la précède et à celle qui la suit)."""
    numeric = pd.to_numeric(values, errors="coerce")
    valid = numeric.dropna()
    mask = pd.Series(False, index=values.index)
    if len(valid) < 3:
        return mask
    previous, following = valid.shift(1), valid.shift(-1)
    up, down = valid - previous, valid - following
    flagged = ((up.abs() > threshold) & (down.abs() > threshold) & ((up > 0) == (down > 0))
               & ((following - previous).abs() <= threshold / 2))
    mask.loc[flagged[flagged].index] = True
    return mask


def drop_spikes(df: pd.DataFrame, columns: tuple[str, ...] = SPIKE_COLUMNS) -> tuple[pd.DataFrame, dict[str, int]]:
    """Copie de `df` (trié par `timestamp`) où chaque pic isolé est remplacé par une valeur absente
    (NaN) dans SA colonne seulement : les autres mesures de la même ligne restent utilisables.
    Retourne aussi le nombre de pics par colonne."""
    out = df.copy()
    counts: dict[str, int] = {}
    for column in columns:
        if column in out:
            mask = spike_mask(out[column])
            counts[column] = int(mask.sum())
            out.loc[mask, column] = float("nan")
    return out, counts


def drop_spike_points(points: list[tuple[float, float]],
                      threshold: float = SPIKE_THRESHOLD_CELSIUS) -> list[tuple[float, float]]:
    """Même règle sur une liste [(instant, valeur)] (historique récent lu en service). La dernière
    mesure, sans suivante, est toujours conservée."""
    return [point for i, point in enumerate(points)
            if not (0 < i < len(points) - 1 and is_spike(points[i - 1][1], point[1], points[i + 1][1], threshold))]
