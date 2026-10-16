"""control/supervisor.py — Régulation PAR ÉTAPES : quand envoyer une consigne, et laquelle (calcul pur).

RÈGLE DE SÉCURITÉ n°2 : aucun appel réseau, aucun import d'actionneur. Ce module décide seulement
s'il y a lieu de PUBLIER un conseil ; l'automate reste seul à agir sur le ventilateur.

Pourquoi des étapes : le bain diélectrique est une grosse masse de liquide, il met une dizaine de
minutes à réagir. Envoyer une nouvelle consigne chaque minute n'a pas de sens : on agirait avant
d'avoir vu l'effet de la précédente. On procède donc comme un exploitant :

  REPOS ──(PT02 au-dessus de la cible, ou qui monte, ou prédite au-dessus)──► ACTIVATION
     ▲                                                                         │ un message
     │                                                                         ▼
    FIN ◄──(PT02 revenue sous la cible − marge)── ATTENTE de l'effet (REGULATION_EFFECT_WAIT_SECONDS)
     ▲     un message                                 │ rien n'est envoyé
     │                                                ▼
     └──────────────────────────────────────── VÉRIFICATION
                                       PT02 baisse      -> MAINTIEN (même consigne, un message)
                                       PT02 ne baisse pas -> RENFORCEMENT (un message) :
                                          air extérieur seul -> ventilateur -> ventilateur + fort
                                          -> 100 % ; à 100 % sans effet -> « insuffisant » (alerte)

À tout moment, PT02 au-delà de CRITICAL_TEMP : envoi IMMÉDIAT à 100 % (étape « critique »).

Phases : « repos », « air_exterieur » (ventilateur arrêté, l'air froid suffit), « ventilateur »,
« critique ». Événements (= un message à publier) : activation, maintien, renforcement,
insuffisant, critique, fin ; None = rien à envoyer ce cycle.

Le PID continue d'être calculé chaque minute (affiché, journalisé) : sa sortie sert de PLANCHER à la
consigne envoyée au ventilateur, qui ne descend jamais pendant une demande (pas de va-et-vient).
"""

from __future__ import annotations

from dataclasses import dataclass

import config
from control.cooling_mode import free_cooling_possible

REST, OUTDOOR_AIR, FAN, CRITICAL = "repos", "air_exterieur", "ventilateur", "critique"
_COOLING_MODE = {REST: "aucun", OUTDOOR_AIR: "air_exterieur", FAN: "ventilateur", CRITICAL: "ventilateur"}


@dataclass(frozen=True)
class Decision:
    phase: str
    event: str | None          # None : rien à publier ce cycle
    command_percent: float     # consigne conseillée au ventilateur (0 au repos ou à l'air seul)
    reason: str

    @property
    def cooling_mode(self) -> str:
        return _COOLING_MODE[self.phase]


def _fr(value: float, digits: int = 1) -> str:
    return f"{value:.{digits}f}".replace(".", ",")


def _signed(value: float) -> str:
    value = round(value, 1) + 0.0          # pas de « −0,0 »
    return ("+" if value >= 0 else "−") + _fr(abs(value))


def _activation_reason(t_oil: float, slope: float | None, predicted: float | None) -> str | None:
    """Pourquoi il faut commencer à refroidir, ou None si rien ne le justifie."""
    target = config.TARGET_TEMP
    if t_oil > target + config.REGULATION_START_ABOVE_TARGET_CELSIUS:
        return f"PT02 à {_fr(t_oil)} °C, au-dessus de la cible {_fr(target)} °C"
    near = t_oil > target - config.REGULATION_RELEASE_BELOW_TARGET_CELSIUS
    if near and slope is not None and slope >= config.REGULATION_HEATING_SLOPE:
        return f"PT02 monte ({_signed(slope)} °C / 10 min) à {_fr(t_oil)} °C"
    if near and predicted is not None and predicted > target + config.FEEDFORWARD_PREDICTED_OVERSHOOT_THRESHOLD:
        return f"prédiction : PT02 à {_fr(predicted)} °C dans 10 min"
    return None


def _fan_percent(pid_percent: float, previous: float = 0.0, step: float = 0.0) -> float:
    """Consigne ventilateur : au moins le démarrage, au moins le PID, au moins l'ancienne + le pas."""
    wanted = max(config.REGULATION_FAN_START_PERCENT, pid_percent, previous + step)
    return min(config.SETPOINT_MAX, wanted)


def decide(t_oil: float, slope: float | None, t_outdoor: float | None, pid_percent: float,
           predicted: float | None, now: float, state: dict) -> tuple[Decision, dict]:
    """Décision de ce cycle et nouvel état. Passer {} au premier appel.

    `slope` : tendance de PT02 (°C / 10 min, control/trend.py) ou None ; `predicted` : prédiction
    ML retenue (None si absente ou écartée) ; `pid_percent` : sortie du PID ce cycle.
    """
    s = dict(state)
    phase = s.get("phase", REST)
    command = float(s.get("command", 0.0))
    air_ok = free_cooling_possible(t_oil, t_outdoor, s)
    wait = config.REGULATION_EFFECT_WAIT_SECONDS

    def send(new_phase: str, event: str, percent: float, reason: str) -> tuple[Decision, dict]:
        s.update(phase=new_phase, command=percent, since=now)
        return Decision(new_phase, event, percent, reason), s

    def quiet(reason: str) -> tuple[Decision, dict]:
        return Decision(phase, None, command, reason), s

    # --- Sécurité : au-delà du seuil critique, maximum immédiat. ---------------------------
    if t_oil > config.CRITICAL_TEMP:
        reason = f"PT02 à {_fr(t_oil)} °C, au-delà du seuil critique {_fr(config.CRITICAL_TEMP)} °C : maximum"
        if phase != CRITICAL:
            return send(CRITICAL, "critique", config.SETPOINT_MAX, reason)
        if now - s.get("since", now) >= wait:
            return send(CRITICAL, "maintien", config.SETPOINT_MAX, reason)
        return quiet(reason)
    if phase == CRITICAL:
        phase = s["phase"] = FAN    # redescendu sous le seuil : suite normale, consigne 100 % gardée

    trigger = _activation_reason(t_oil, slope, predicted)

    # --- Repos : rien à envoyer tant que rien ne chauffe. --------------------------------
    if phase == REST:
        if trigger is None:
            return quiet(f"PT02 à {_fr(t_oil)} °C, stable : rien à envoyer, l'automate régule seul")
        if air_ok:
            return send(OUTDOOR_AIR, "activation", 0.0,
                        f"{trigger} : air extérieur seul (ventilateur arrêté), l'air froid devrait suffire")
        return send(FAN, "activation", _fan_percent(pid_percent),
                    f"{trigger} : démarrage du ventilateur")

    # --- Fin : PT02 revenue sous la cible et plus rien ne pousse à refroidir. ----------
    if trigger is None and t_oil <= config.TARGET_TEMP - config.REGULATION_RELEASE_BELOW_TARGET_CELSIUS:
        s.pop("since", None)
        return send(REST, "fin", 0.0, f"PT02 revenue à {_fr(t_oil)} °C : fin de la demande, l'automate régule seul")

    # --- L'air extérieur s'est réchauffé pendant une demande « air seul ». -------------
    if phase == OUTDOOR_AIR and not air_ok:
        return send(FAN, "renforcement", _fan_percent(pid_percent),
                    "l'air extérieur n'est plus assez froid : démarrage du ventilateur")

    # --- Attente : laisser au bain le temps de réagir. ----------------------------------
    waited = now - s.get("since", now)
    if waited < wait:
        return quiet(f"consigne en cours, effet vérifié dans {_fr((wait - waited) / 60, 0)} min")

    # --- Vérification de l'effet. --------------------------------------------------------
    if slope is None:
        return send(phase, "maintien", command, "tendance de PT02 indisponible : consigne maintenue")
    if slope <= -config.REGULATION_COOLING_SLOPE:
        return send(phase, "maintien", command,
                    f"PT02 à {_fr(t_oil)} °C ({_signed(slope)} °C / 10 min) : ça refroidit, consigne maintenue")
    if t_oil <= config.TARGET_TEMP and slope <= 0:
        return send(phase, "maintien", command,
                    f"PT02 à {_fr(t_oil)} °C, à la cible et stable : consigne maintenue")
    not_cooling = f"PT02 ne baisse pas ({_signed(slope)} °C / 10 min, {_fr(t_oil)} °C)"
    if phase == OUTDOOR_AIR:
        return send(FAN, "renforcement", _fan_percent(pid_percent),
                    f"{not_cooling} avec l'air seul : démarrage du ventilateur")
    if command >= config.SETPOINT_MAX:
        return send(FAN, "insuffisant", config.SETPOINT_MAX,
                    f"{not_cooling} malgré le maximum : vérifier que le ventilateur tourne, le dry cooler "
                    "et que l'automate applique la consigne")
    percent = _fan_percent(pid_percent, command, config.REGULATION_STEP_PERCENT)
    return send(FAN, "renforcement", percent, f"{not_cooling} : ventilateur porté à {_fr(percent, 0)} %")
