"""L'air extérieur seul peut-il refroidir (dry cooler ventilateur arrêté) ?"""

import math

import pytest

import config
from control.cooling_mode import free_cooling_possible


def test_cold_air_with_enough_gap_is_enough():
    assert free_cooling_possible(36.0, 5.0, {})


@pytest.mark.parametrize("t_out", [10.0, 15.0, 30.0, None, math.nan])
def test_mild_warm_or_unknown_air_is_not_enough(t_out):
    assert not free_cooling_possible(36.0, t_out, {})


def test_small_gap_between_bath_and_air_is_not_enough():
    assert not free_cooling_possible(9.0 + config.FREE_COOLING_MIN_GAP_CELSIUS - 1, 9.0, {})


def test_disabled(monkeypatch):
    monkeypatch.setattr(config, "FREE_COOLING_ENABLED", False)
    assert not free_cooling_possible(36.0, 0.0, {})


def test_hysteresis_around_10_degrees():
    state: dict = {}
    assert free_cooling_possible(36.0, 9.5, state)           # sous 10 °C : permis
    assert free_cooling_possible(36.0, 10.5, state)          # 10,5 °C : reste permis
    limit = config.FREE_COOLING_MAX_OUTDOOR_TEMP + config.FREE_COOLING_HYSTERESIS_CELSIUS
    assert not free_cooling_possible(36.0, limit, state)     # 12 °C : interdit
    assert not free_cooling_possible(36.0, 11.0, state)      # 11 °C : pas encore repassé sous 10
    assert free_cooling_possible(36.0, 9.9, state)
