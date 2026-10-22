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

La règle (mise au point sur 10 jours de relevés réels, 55 472 mesures)
--------
Pour une mesure x[i], avec ses voisines x[i−2], x[i−1] (avant) et x[i+1] (après), c'est un PIC
ISOLÉ si, à la fois :

  1. elle s'écarte de plus de SPIKE_THRESHOLD_CELSIUS (1,0 °C) de la MOYENNE de ses deux voisines
     directes : (x[i−1] + x[i+1]) / 2 ;
  2. elle est au-dessus des deux voisines, ou en dessous des deux (elle « monte puis redescend ») ;
  3. les deux voisines sont d'accord entre elles : |x[i+1] − x[i−1]| ≤ SPIKE_THRESHOLD_CELSIUS ;
  4. la mesure d'avant était stable : |x[i−1] − x[i−2]| ≤ SPIKE_THRESHOLD_CELSIUS.

Pourquoi la moyenne des voisines (1) et non « la suivante revient à la précédente » : un pic peut
tomber pendant que la température bouge déjà (montée de PT04 après un démarrage du ventilateur :
31,7 → 33,7 → 32,2) ; la moyenne suit cette pente, une comparaison stricte avec la précédente
ratait ce cas. Pourquoi la condition 4 : au démarrage du ventilateur, PT04 descend en V
(33,7 → 31,2 → 29,7 → 30,4) ; le fond du V ressemble à un pic, mais la mesure d'avant chutait
déjà de 2,5 °C : c'est un vrai mouvement, conservé.

Résultat sur les relevés du 12 au 22/10/2026 : 5 pics sur PT01, 0 sur PT02, 17 sur PT03, 12 sur
PT04, tous vérifiés à la main ; aucun creux de cycle du ventilateur (environ 2 300) écarté.

Ce qu'elle ne rejette PAS :

  - les cycles du ventilateur du dry cooler (PT04 −3 °C en 30 s, puis remontée en 3 min) ;
  - les marches de PT02, lue au degré près : 34 → 35 → 34 °C s'écarte de 1,0 °C de la moyenne de
    ses voisines, pas de PLUS de 1,0 °C ;
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
# Écart minimal (°C) d'une mesure à la moyenne de ses deux voisines pour être un pic (strictement
# supérieur) : les marches de 1 °C de PT02 ne sont pas concernées ; les pics réels vont de 1,1 à 2,3 °C.
SPIKE_THRESHOLD_CELSIUS: float = config.SPIKE_THRESHOLD_CELSIUS


def is_spike(before: float, previous: float, value: float, following: float,
             threshold: float = SPIKE_THRESHOLD_CELSIUS) -> bool:
    """Vrai si `value` est un pic isolé (règle du module) ; `before` est la mesure qui précède
    `previous`."""
    deviation = value - (previous + following) / 2
    same_direction = ((value - previous) > 0) == ((value - following) > 0)
    return (abs(deviation) > threshold and same_direction
            and abs(following - previous) <= threshold and abs(previous - before) <= threshold)


def spike_mask(values: pd.Series, threshold: float = SPIKE_THRESHOLD_CELSIUS) -> pd.Series:
    """Masque booléen des pics isolés d'une série ordonnée dans le temps (valeurs absentes ignorées :
    chaque mesure est comparée aux mesures VALIDES qui l'entourent)."""
    numeric = pd.to_numeric(values, errors="coerce")
    valid = numeric.dropna()
    mask = pd.Series(False, index=values.index)
    if len(valid) < 4:
        return mask
    before, previous, following = valid.shift(2), valid.shift(1), valid.shift(-1)
    deviation = valid - (previous + following) / 2
    flagged = ((deviation.abs() > threshold) & (((valid - previous) > 0) == ((valid - following) > 0))
               & ((following - previous).abs() <= threshold) & ((previous - before).abs() <= threshold))
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
    """Même règle sur une liste [(instant, valeur)] (historique récent lu en service). Les deux
    premières mesures et la dernière, sans voisines suffisantes, sont toujours conservées."""
    return [point for i, point in enumerate(points)
            if not (1 < i < len(points) - 1
                    and is_spike(points[i - 2][1], points[i - 1][1], point[1], points[i + 1][1], threshold))]
