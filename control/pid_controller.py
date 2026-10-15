"""
control/pid_controller.py — Calcul de la consigne de refroidissement (contrat 6.7).

RÈGLE DE SÉCURITÉ FONDAMENTALE DU PROJET (règle non négociable n°2) : ce
fichier ne contient et ne doit JAMAIS contenir le moindre appel réseau, ni
le moindre import de bibliothèque d'accès à un automate ou variateur. Il ne
fait QUE du calcul pur : on lui donne des nombres, il rend un nombre. C'est
l'appelant (service/app.py) qui publiera ce nombre sur MQTT ; c'est l'automate,
et lui seul, qui décidera de l'appliquer réellement au variateur, en
respectant son propre mode manuel/auto. Ce fichier n'a physiquement AUCUN
moyen d'agir directement sur l'installation — c'est une garantie de
sécurité par l'architecture, pas seulement une bonne pratique.

En clair : imagine un copilote qui calcule "il faudrait freiner à 40%" mais
qui n'a pas les mains sur les pédales — c'est le pilote (l'automate) qui
regarde la suggestion et décide. Ce module est ce copilote : il conseille,
il n'actionne jamais.

Régulation à deux étages :
1. PID classique : proportionnel à l'écart actuel (e = t_oil - t_target ;
   positif quand l'huile est plus CHAUDE que la cible, donc appelle PLUS de
   refroidissement), intégral (erreur cumulée dans le temps, pour éliminer
   une erreur résiduelle persistante, avec anti-windup : gel conditionnel quand la
   sortie est saturée ET intégrale bornée dans [0, 100 % / Ki] — voir plus bas), dérivé (vitesse de variation de l'erreur,
   pour anticiper et amortir les oscillations).
2. Feedforward : un "coup de pouce" préventif quand on sait déjà, par la
   prédiction ML ou par la météo, qu'une hausse de température est probable
   — plutôt que d'attendre que le PID réagisse après coup.

Une sécurité absolue (t_oil > CRITICAL_TEMP) court-circuite tout le reste :
consigne maximale immédiate, sans même calculer le PID.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import config


@dataclass(frozen=True)
class SetpointDetail:
    """Résultat complet d'un calcul de consigne, terme par terme (pour l'affichage et le journal).

    Les termes P / I / D sont None en mode « safety_bypass » : ils ne sont alors pas calculés.
    `raw_output` est la somme avant bornage dans [SETPOINT_MIN, SETPOINT_MAX].
    """

    setpoint: float
    state: dict
    mode: str
    error: float | None = None
    p_term: float | None = None
    i_term: float | None = None
    d_term: float | None = None
    feedforward_prediction: float = 0.0
    feedforward_outdoor: float = 0.0
    raw_output: float | None = None

    @property
    def feedforward(self) -> float:
        return self.feedforward_prediction + self.feedforward_outdoor


def compute_setpoint(
    t_oil: float,
    t_target: float,
    t_oil_predicted: float,
    t_outdoor: float | None,
    dt: float,
    pid_state: dict,
) -> tuple[float, dict, str]:
    """Consigne, nouvel état et mode (voir `compute_setpoint_detailed` pour le détail des termes)."""
    detail = compute_setpoint_detailed(t_oil, t_target, t_oil_predicted, t_outdoor, dt, pid_state)
    return detail.setpoint, detail.state, detail.mode


def compute_setpoint_detailed(
    t_oil: float,
    t_target: float,
    t_oil_predicted: float,
    t_outdoor: float | None,
    dt: float,
    pid_state: dict,
) -> SetpointDetail:
    """Calcule la consigne de refroidissement (0-100%) à publier sur MQTT.

    Args:
        t_oil: température actuelle du bain d'huile (°C, ex. PT02).
        t_target: température cible (°C, config.TARGET_TEMP).
        t_oil_predicted: température prédite dans l'horizon configuré
            (°C), fournie par ml/predictor.py.
        t_outdoor: température extérieure actuelle (°C), ou None si inconnue : le
            terme feedforward météo est alors simplement ignoré (le PID et le bypass
            de sécurité continuent de fonctionner).
        dt: intervalle de temps depuis le dernier appel (secondes).
        pid_state: état PID à faire persister entre appels successifs.
            Doit contenir (ou se voir attribuer par défaut) 'integral' et
            'last_error'. Passer {} au tout premier appel.

    Retourne un `SetpointDetail` :
        - setpoint : float toujours dans [SETPOINT_MIN, SETPOINT_MAX] (0-100) ;
        - state : dict à repasser au prochain appel ;
        - mode : "safety_bypass", "feedforward" ou "pid" ;
        - les termes P, I, D et feedforward qui composent la consigne.

    Ne réalise AUCUN appel réseau et n'importe AUCUN driver d'actionneur —
    voir l'en-tête du module.
    """
    # Une mesure non finie ne doit jamais contaminer l'état PID. En cas de
    # température d'huile invalide, on demande le refroidissement maximal.
    if not math.isfinite(t_oil) or not math.isfinite(t_target):
        return SetpointDetail(100.0, dict(pid_state), "safety_bypass")

    if not math.isfinite(dt) or dt < 0.0:
        dt = 0.0

    prediction_is_finite = math.isfinite(t_oil_predicted)
    outdoor_is_finite = t_outdoor is not None and math.isfinite(t_outdoor)
    if not prediction_is_finite:
        t_oil_predicted = t_oil

    # --- Sécurité, priorité absolue : court-circuite tout calcul ci-dessous. ---
    if t_oil > config.CRITICAL_TEMP:
        # En clair : peu importe ce que dirait le PID en temps normal, si la
        # température dépasse le seuil critique, on demande le refroidissement
        # maximal sans discuter. L'état PID est conservé tel quel (pas
        # d'accumulation supplémentaire pendant le bypass) pour repartir
        # proprement une fois la situation redevenue normale.
        return SetpointDetail(100.0, dict(pid_state), "safety_bypass", error=t_oil - t_target)

    # --- Terme PID classique. ---
    # error > 0 signifie huile PLUS CHAUDE que la cible -> il faut REFROIDIR
    # DAVANTAGE, donc une consigne plus élevée. C'est l'inverse d'un
    # convention "chauffage" (où l'erreur positive appelle plus de
    # puissance quand on est trop FROID) : ici on pilote un refroidissement,
    # error = t_oil - t_target, pas t_target - t_oil.
    error = t_oil - t_target
    previous_integral = pid_state.get("integral", 0.0)
    previous_error = pid_state.get("last_error", error)
    if not math.isfinite(previous_integral):
        previous_integral = 0.0
    if not math.isfinite(previous_error):
        previous_error = error

    # --- Anti-windup (clamping conditionnel). ---
    # En clair : si la consigne est déjà saturée à 100% et que l'erreur
    # pousse encore vers PLUS de refroidissement, continuer à accumuler
    # l'intégrale ne changerait rien à la consigne actuelle (déjà à son
    # maximum physique) mais créerait une dette cachée qui mettra
    # anormalement longtemps à se résorber une fois l'erreur redevenue
    # normale ou négative — c'est le phénomène classique de "windup". On
    # gèle donc l'intégrale (on n'accepte pas le nouvel incrément) dans ce
    # cas précis, tout en laissant le terme proportionnel et dérivé réagir
    # normalement. Dès que l'erreur repasse dans l'autre sens (ou que la
    # sortie n'est plus saturée), l'accumulation reprend normalement.
    #
    # On estime la saturation à partir du dernier état connu (mode le plus
    # simple et le plus prévisible : une saturation avérée à l'appel
    # précédent suffit à geler l'accumulation de ce cycle-ci).
    was_saturated_high = pid_state.get("saturated_high", False)
    was_saturated_low = pid_state.get("saturated_low", False)

    integral_candidate = previous_integral + error * dt
    if was_saturated_high and error > 0.0:
        integral = previous_integral  # gelée : la sortie était déjà à 100%
    elif was_saturated_low and error < 0.0:
        integral = previous_integral  # gelée : la sortie était déjà à 0%
    else:
        integral = integral_candidate

    # --- Bornes de l'intégrale (correctif « huile froide »). ---
    # En clair : ce régulateur ne sait que REFROIDIR, sa sortie ne peut jamais être négative.
    # Une « dette » négative de l'intégrale (accumulée quand l'huile est froide) n'aurait donc
    # aucun effet utile, mais retarderait longtemps la réaction dès que l'huile se réchauffe
    # (cas mesuré : huile à 10 °C puis 40 °C -> 9 minutes à 0 % avant de réagir). On la
    # maintient donc dans [0, SETPOINT_MAX / Ki] : au plus l'équivalent de 100 % de sortie.
    if config.PID_KI > 0.0:
        integral = max(0.0, min(integral, config.SETPOINT_MAX / config.PID_KI))

    derivative = (error - previous_error) / dt if dt > 0 else 0.0

    p_term = config.PID_KP * error
    i_term = config.PID_KI * integral
    d_term = config.PID_KD * derivative
    u_pid = p_term + i_term + d_term

    # --- Terme feedforward : anticipation à partir de la prédiction ML et de
    # la météo extérieure. Les seuils et montants sont des valeurs par défaut
    # raisonnables (voir config.py), à calibrer expérimentalement en Phase 3a
    # — ne pas les considérer comme définitives (spec §6.7).
    ff_prediction = ff_outdoor = 0.0
    mode = "pid"

    if t_oil_predicted > t_target + config.FEEDFORWARD_PREDICTED_OVERSHOOT_THRESHOLD:
        ff_prediction = config.FEEDFORWARD_PREDICTED_BOOST
        mode = "feedforward"

    if t_outdoor is not None and outdoor_is_finite and t_outdoor > config.FEEDFORWARD_OUTDOOR_HOT_THRESHOLD:
        ff_outdoor = config.FEEDFORWARD_OUTDOOR_BOOST
        mode = "feedforward"

    raw_setpoint = u_pid + ff_prediction + ff_outdoor
    setpoint = max(config.SETPOINT_MIN, min(config.SETPOINT_MAX, raw_setpoint))

    new_state = {
        "integral": integral,
        "last_error": error,
        "saturated_high": setpoint >= config.SETPOINT_MAX,
        "saturated_low": setpoint <= config.SETPOINT_MIN,
    }
    return SetpointDetail(
        setpoint, new_state, mode, error=error, p_term=p_term, i_term=i_term, d_term=d_term,
        feedforward_prediction=ff_prediction, feedforward_outdoor=ff_outdoor, raw_output=raw_setpoint,
    )