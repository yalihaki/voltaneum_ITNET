"""control/cooling_mode.py — Air froid seul ou ventilateur ? Conseil à publier (calcul pur).

RÈGLE DE SÉCURITÉ n°2 : aucun appel réseau, aucun import d'actionneur. Ce module dit seulement
COMMENT il serait judicieux de refroidir ; c'est l'automate qui décide d'arrêter ou de démarrer
le ventilateur du dry cooler.

Principe (« free cooling ») : par air froid, le dry cooler évacue assez de chaleur ventilateur
ARRÊTÉ, par simple circulation naturelle de l'air à travers ses ailettes. Le ventilateur n'est
utile que si cette circulation ne suffit pas. Trois conseils possibles :

- « aucun »        : le PID ne demande pas de refroidissement (consigne 0 %) ;
- « air_exterieur » : refroidissement demandé, mais l'air froid suffit, ventilateur arrêté ;
- « ventilateur »   : refroidissement demandé et l'air seul ne suffit pas (ou air inconnu).

L'air seul est conseillé si, en même temps :
1. l'air extérieur est sous FREE_COOLING_MAX_OUTDOOR_TEMP (10 °C) — avec une hystérésis :
   une fois passé dessous, il faut remonter au-dessus de 10 + FREE_COOLING_HYSTERESIS_CELSIUS
   pour l'interdire, sinon une température qui hésite autour de 10 °C ferait alterner les conseils ;
2. l'écart PT02 − air atteint FREE_COOLING_MIN_GAP_CELSIUS : sans écart suffisant, l'échange
   naturel est trop faible ;
3. la demande du PID ne dépasse pas FREE_COOLING_MAX_DEMAND_PERCENT : une forte demande veut dire
   que le bain s'éloigne de la cible, la circulation naturelle ne suffira pas.

Passer au ventilateur est IMMÉDIAT (côté sûr). Revenir à l'air seul n'est conseillé qu'après
FREE_COOLING_MIN_DWELL_SECONDS en mode ventilateur : sans ce délai, le ventilateur ferait baisser
la demande, l'air seul serait conseillé, la demande remonterait… et ainsi de suite.
"""

from __future__ import annotations

import math

import config

NONE, OUTDOOR_AIR, FAN = "aucun", "air_exterieur", "ventilateur"


def _fr(value: float, digits: int = 1) -> str:
    """Nombre à la française (virgule décimale) pour les raisons affichées."""
    return f"{value:.{digits}f}".replace(".", ",")


def advise_cooling_mode(t_oil: float, t_outdoor: float | None, setpoint: float, pid_mode: str,
                        now: float, state: dict) -> tuple[str, str, dict]:
    """(conseil, raison en français, nouvel état). Passer {} comme état au premier appel."""
    state = dict(state)
    outdoor_known = t_outdoor is not None and math.isfinite(t_outdoor)

    # Hystérésis sur l'air extérieur (mise à jour même sans demande, pour rester cohérente).
    if t_outdoor is not None and outdoor_known:
        allowed = state.get("air_allowed")
        limit = config.FREE_COOLING_MAX_OUTDOOR_TEMP
        if allowed is None:
            allowed = t_outdoor < limit
        elif allowed and t_outdoor >= limit + config.FREE_COOLING_HYSTERESIS_CELSIUS:
            allowed = False
        elif not allowed and t_outdoor < limit:
            allowed = True
        state["air_allowed"] = allowed
    air_allowed = outdoor_known and bool(state.get("air_allowed"))

    def result(mode: str, reason: str) -> tuple[str, str, dict]:
        if mode != state.get("mode"):
            state["mode"], state["since"] = mode, now
        return mode, reason, state

    if pid_mode == "safety_bypass":
        return result(FAN, "seuil critique dépassé : refroidissement maximal")
    if not math.isfinite(setpoint) or setpoint <= config.SETPOINT_MIN:
        return result(NONE, "le PID ne demande pas de refroidissement (consigne 0 %)")
    if not config.FREE_COOLING_ENABLED:
        return result(FAN, "conseil air extérieur désactivé (FREE_COOLING_ENABLED=false)")
    if t_outdoor is None or not outdoor_known:
        return result(FAN, "température extérieure inconnue")
    if not air_allowed:
        return result(FAN, f"air extérieur {_fr(t_outdoor, 1)} °C : trop doux pour refroidir sans ventilateur")
    gap = t_oil - t_outdoor
    if gap < config.FREE_COOLING_MIN_GAP_CELSIUS:
        return result(FAN, f"écart PT02 − air de {_fr(gap, 1)} °C, sous {_fr(config.FREE_COOLING_MIN_GAP_CELSIUS, 0)} °C : "
                           "l'air seul n'évacuera pas assez")
    if setpoint > config.FREE_COOLING_MAX_DEMAND_PERCENT:
        return result(FAN, f"demande de {_fr(setpoint, 0)} %, au-delà de {_fr(config.FREE_COOLING_MAX_DEMAND_PERCENT, 0)} % : "
                           "l'air seul ne suffira pas")
    if state.get("mode") == FAN:
        waited = now - state.get("since", now)
        if waited < config.FREE_COOLING_MIN_DWELL_SECONDS:
            return result(FAN, f"air froid suffisant, retour à l'air seul dans "
                               f"{(config.FREE_COOLING_MIN_DWELL_SECONDS - waited) / 60:.0f} min (évite les va-et-vient)")
    return result(OUTDOOR_AIR, f"air extérieur {_fr(t_outdoor, 1)} °C, écart de {_fr(gap, 0)} °C avec PT02 et demande "
                               f"de {_fr(setpoint, 0)} % : ventilateur arrêté, la circulation naturelle suffit")
