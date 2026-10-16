"""Conseil « air extérieur seul » ou « ventilateur » quand le PID demande du refroidissement."""

import math

import pytest

import config
from control.cooling_mode import FAN, NONE, OUTDOOR_AIR, advise_cooling_mode


def _advise(t_out, setpoint=10.0, t_oil=36.0, mode="pid", now=0.0, state=None):
    return advise_cooling_mode(t_oil, t_out, setpoint, mode, now, state or {})


def test_cold_air_and_small_demand_uses_outdoor_air_alone():
    mode, reason, _ = _advise(5.0, setpoint=10.0, t_oil=36.0)
    assert mode == OUTDOOR_AIR and "ventilateur arrêté" in reason


def test_no_cooling_requested_means_nothing_to_do():
    assert _advise(5.0, setpoint=0.0)[0] == NONE


@pytest.mark.parametrize("t_out", [10.0, 15.0, 30.0])
def test_mild_or_warm_air_needs_the_fan(t_out):
    mode, reason, _ = _advise(t_out)
    assert mode == FAN and "trop doux" in reason


def test_strong_demand_needs_the_fan_even_in_cold_air():
    mode, reason, _ = _advise(2.0, setpoint=config.FREE_COOLING_MAX_DEMAND_PERCENT + 1)
    assert mode == FAN and "demande" in reason


def test_small_gap_between_bath_and_air_needs_the_fan():
    mode, reason, _ = _advise(9.0, t_oil=9.0 + config.FREE_COOLING_MIN_GAP_CELSIUS - 1)
    assert mode == FAN and "écart" in reason


@pytest.mark.parametrize("t_out", [None, math.nan])
def test_unknown_air_is_handled_safely_with_the_fan(t_out):
    assert _advise(t_out)[0] == FAN


def test_critical_temperature_always_means_fan():
    assert _advise(-5.0, setpoint=100.0, t_oil=60.0, mode="safety_bypass")[0] == FAN


def test_disabled_never_advises_outdoor_air(monkeypatch):
    monkeypatch.setattr(config, "FREE_COOLING_ENABLED", False)
    assert _advise(0.0)[0] == FAN


def test_outdoor_hysteresis_avoids_flip_flop_around_10_degrees():
    _, _, state = _advise(9.5)                                    # sous 10 °C : air seul possible
    assert _advise(10.5, state=state)[0] == OUTDOOR_AIR             # 10,5 °C : on reste à l'air
    limit = config.FREE_COOLING_MAX_OUTDOOR_TEMP + config.FREE_COOLING_HYSTERESIS_CELSIUS
    mode, _, state = _advise(limit, state=state)                    # 12 °C : ventilateur
    assert mode == FAN
    assert _advise(11.0, state=state, now=10_000)[0] == FAN         # 11 °C : pas encore sous 10


def test_going_to_the_fan_is_immediate_but_back_to_air_waits():
    _, _, state = _advise(5.0, setpoint=10.0, now=0)
    mode, _, state = _advise(5.0, setpoint=80.0, now=60, state=state)       # forte demande : tout de suite
    assert mode == FAN
    mode, reason, state = _advise(5.0, setpoint=10.0, now=120, state=state)  # demande retombée
    assert mode == FAN and "retour à l'air seul" in reason
    later = 60 + config.FREE_COOLING_MIN_DWELL_SECONDS
    assert _advise(5.0, setpoint=10.0, now=later, state=state)[0] == OUTDOOR_AIR


def test_state_is_not_mutated_in_place():
    state = {}
    advise_cooling_mode(36.0, 5.0, 10.0, "pid", 0.0, state)
    assert state == {}
