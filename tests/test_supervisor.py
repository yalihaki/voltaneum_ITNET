"""Régulation par étapes : un message seulement quand il y a quelque chose à dire."""

import pytest

import config
from control.supervisor import CRITICAL, FAN, OUTDOOR_AIR, REST, decide
from control.trend import slope_per_10min

WAIT = config.REGULATION_EFFECT_WAIT_SECONDS
TARGET = config.TARGET_TEMP


def run(t_oil, slope=0.0, t_out=20.0, pid=0.0, predicted=None, now=0.0, state=None):
    return decide(t_oil, slope, t_out, pid, predicted, now, state or {})


# --------------------------------------------------------------------------- repos
def test_stable_bath_sends_nothing():
    decision, state = run(TARGET - 1)
    assert decision.phase == REST and decision.event is None
    for minute in range(1, 30):                               # une demi-heure : toujours rien
        decision, state = run(TARGET - 1, now=minute * 60, state=state)
        assert decision.event is None


# --------------------------------------------------------------------------- activation
def test_bath_above_target_starts_the_fan_once():
    decision, state = run(TARGET + 1, t_out=20.0, pid=12.0)
    assert (decision.phase, decision.event) == (FAN, "activation")
    assert decision.command_percent == config.REGULATION_FAN_START_PERCENT   # au moins le démarrage
    decision, _ = run(TARGET + 1, now=60, state=state)
    assert decision.event is None                              # pas de nouveau message la minute suivante


def test_rising_bath_near_target_starts_before_crossing_it():
    decision, _ = run(TARGET, slope=config.REGULATION_HEATING_SLOPE + 0.1)
    assert decision.event == "activation" and "monte" in decision.reason


def test_rising_bath_far_below_target_does_not_start():
    decision, _ = run(TARGET - 3, slope=1.0)
    assert decision.event is None


def test_prediction_above_target_starts_in_advance():
    decision, _ = run(TARGET, predicted=TARGET + config.FEEDFORWARD_PREDICTED_OVERSHOOT_THRESHOLD + 0.5)
    assert decision.event == "activation" and "prédiction" in decision.reason


def test_cold_air_starts_with_outdoor_air_alone():
    decision, _ = run(TARGET + 1, t_out=5.0)
    assert (decision.phase, decision.event, decision.command_percent) == (OUTDOOR_AIR, "activation", 0.0)
    assert decision.cooling_mode == "air_exterieur"


# --------------------------------------------------------------------------- attente puis vérification
def test_waits_for_the_effect_then_keeps_the_command_if_the_bath_cools():
    _, state = run(TARGET + 1, pid=40.0)
    for t in range(60, int(WAIT), 60):
        decision, state = run(TARGET + 1, slope=0.5, now=t, state=state)
        assert decision.event is None and "vérifié dans" in decision.reason
    decision, state = run(TARGET + 0.5, slope=-config.REGULATION_COOLING_SLOPE - 0.1, now=WAIT, state=state)
    assert (decision.event, decision.command_percent) == ("maintien", 40.0)


def test_not_cooling_after_the_wait_increases_the_fan():
    _, state = run(TARGET + 1, pid=30.0)
    decision, state = run(TARGET + 1.2, slope=0.1, pid=25.0, now=WAIT, state=state)
    assert decision.event == "renforcement"
    assert decision.command_percent == 30.0 + config.REGULATION_STEP_PERCENT   # jamais en baisse


def test_outdoor_air_alone_not_enough_switches_to_fan():
    _, state = run(TARGET + 1, t_out=5.0)
    decision, _ = run(TARGET + 1, slope=0.0, t_out=5.0, now=WAIT, state=state)
    assert (decision.phase, decision.event) == (FAN, "renforcement")


def test_outdoor_air_warming_up_switches_to_fan_without_waiting():
    _, state = run(TARGET + 1, t_out=5.0)
    decision, _ = run(TARGET + 1, t_out=15.0, now=60, state=state)
    assert (decision.phase, decision.event) == (FAN, "renforcement")


def test_maximum_without_effect_is_reported_as_insufficient():
    state = {"phase": FAN, "command": config.SETPOINT_MAX, "since": 0.0}
    decision, _ = run(TARGET + 2, slope=0.2, now=WAIT, state=state)
    assert (decision.event, decision.command_percent) == ("insuffisant", config.SETPOINT_MAX)
    assert "vérifier" in decision.reason


def test_missing_trend_keeps_the_command():
    state = {"phase": FAN, "command": 50.0, "since": 0.0}
    decision, _ = run(TARGET + 1, slope=None, now=WAIT, state=state)
    assert (decision.event, decision.command_percent) == ("maintien", 50.0)


# --------------------------------------------------------------------------- fin
def test_back_below_target_ends_the_request_once():
    state = {"phase": FAN, "command": 50.0, "since": 0.0}
    decision, state = run(TARGET - config.REGULATION_RELEASE_BELOW_TARGET_CELSIUS, slope=-0.3, now=120, state=state)
    assert (decision.phase, decision.event, decision.command_percent) == (REST, "fin", 0.0)
    decision, _ = run(TARGET - 2, now=180, state=state)
    assert decision.event is None


def test_no_end_while_the_prediction_still_says_it_will_heat():
    state = {"phase": FAN, "command": 50.0, "since": 0.0}
    predicted = TARGET + config.FEEDFORWARD_PREDICTED_OVERSHOOT_THRESHOLD + 1
    decision, _ = run(TARGET - 0.5, slope=-0.3, predicted=predicted, now=120, state=state)
    assert decision.event != "fin"


# --------------------------------------------------------------------------- critique
def test_critical_sends_maximum_immediately_even_while_waiting():
    _, state = run(TARGET + 1, pid=30.0)
    decision, state = run(config.CRITICAL_TEMP + 1, now=60, state=state)
    assert (decision.phase, decision.event, decision.command_percent) == (CRITICAL, "critique", config.SETPOINT_MAX)
    decision, state = run(config.CRITICAL_TEMP + 1, now=120, state=state)
    assert decision.event is None                                 # pas de répétition chaque minute
    decision, _ = run(config.CRITICAL_TEMP + 1, now=60 + WAIT, state=state)
    assert decision.event == "maintien"


def test_state_is_not_mutated_in_place():
    state: dict = {}
    decide(TARGET + 1, 0.0, 20.0, 0.0, None, 0.0, state)
    assert state == {}


# --------------------------------------------------------------------------- tendance
def test_slope_of_a_quantized_signal_is_smoothed():
    """PT02 au degré près : 34 puis 35 à mi-fenêtre. La pente ajustée vaut ~+1,5 °C / 10 min,
    pas un saut de +1 °C en un pas."""
    points = [(t, 34.0 if t < 300 else 35.0) for t in range(0, 600, 15)]
    assert slope_per_10min(points, 600) == pytest.approx(1.5, abs=0.2)


def test_slope_needs_enough_points_and_span():
    assert slope_per_10min([(0, 34.0), (15, 34.0)], 600) is None
    assert slope_per_10min([(t, 34.0) for t in range(0, 100, 15)], 600) is None   # 100 s sur 600
    assert slope_per_10min([(t, 30 + t / 600) for t in range(0, 600, 15)], 600) == pytest.approx(1.0)


def test_rest_message_gives_the_trend_instead_of_saying_stable():
    """Cas réel (22/10/2026 16:50) : PT02 baissait de 1 °C / 10 min et l'écran disait « stable »."""
    from control.supervisor import decide
    decision, _ = decide(34.0, -1.0, 15.0, 0.0, None, 1000.0, {})
    assert decision.event is None and "stable" not in decision.reason
    assert "tendance −1,0 °C / 10 min" in decision.reason and "ne se réchauffe pas" in decision.reason
