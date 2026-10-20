import math

import pytest

import config
from control.cooling_advice import compute_cooling_conditions, select_outdoor_band


@pytest.mark.parametrize("t_out, band", [(-5.0, "froid"), (9.9, "froid"), (10.0, "tempere"),
                                         (24.9, "tempere"), (25.0, "chaud"), (40.0, "chaud")])
def test_outdoor_band_boundaries(t_out, band):
    assert select_outdoor_band(t_out)[0] == band


def test_cold_air_starts_fan_later_and_hot_air_earlier():
    cold = compute_cooling_conditions(35.0, 0.0)
    hot = compute_cooling_conditions(35.0, 35.0)
    assert cold["fan_on_above_temp"] > hot["fan_on_above_temp"]


def test_stop_threshold_is_below_start_threshold_and_all_below_critical():
    for t_out in (-10.0, 5.0, 15.0, 30.0, 45.0, None):
        c = compute_cooling_conditions(35.0, t_out)
        assert c["fan_off_below_temp"] < c["fan_on_above_temp"] < c["critical_temp"]


def test_unknown_outdoor_gives_neutral_conditions_and_no_frequency():
    c = compute_cooling_conditions(35.0, None)
    assert c["outdoor_band"] == "inconnue"
    assert c["fan_on_above_temp"] == config.TARGET_TEMP + config.FAN_HYSTERESIS_CELSIUS / 2
    assert c["fan_min_freq_hz"] is None and c["t_outdoor"] is None


def test_band_is_centred_on_the_target_so_the_average_temperature_is_the_target():
    """Tout-ou-rien : la température oscille entre les seuils, sa moyenne est le milieu."""
    c = compute_cooling_conditions(35.0, 15.0)          # air tempéré : décalage nul
    assert (c["fan_on_above_temp"] + c["fan_off_below_temp"]) / 2 == config.TARGET_TEMP
    assert c["fan_off_below_temp"] < config.TARGET_TEMP < c["fan_on_above_temp"]


@pytest.mark.parametrize("bad", [None, math.nan, math.inf])
def test_invalid_oil_temperature_publishes_nothing(bad):
    assert compute_cooling_conditions(bad, 20.0) is None


def test_non_finite_outdoor_is_treated_as_unknown():
    c = compute_cooling_conditions(35.0, math.nan)
    assert c["outdoor_band"] == "inconnue" and c["t_outdoor"] is None


def test_message_carries_target_and_validity():
    c = compute_cooling_conditions(36.0, 20.0)
    assert c["target_temp"] == config.TARGET_TEMP
    assert c["valid_for_seconds"] == config.REGULATION_EFFECT_WAIT_SECONDS + 3 * config.CONTROL_LOOP_INTERVAL_SECONDS


def test_inlet_setpoint_advice_is_target_minus_load():
    """Régulation en cascade : PT02 = PT01 + échauffement, donc consigne PT01 = cible − échauffement."""
    from control.cooling_advice import inlet_setpoint_advice
    assert inlet_setpoint_advice(0.6) == round(config.TARGET_TEMP - 0.6, 1)
    assert inlet_setpoint_advice(-0.4) == config.TARGET_TEMP          # échauffement négatif : bruit
    assert inlet_setpoint_advice(80.0) == config.INLET_SETPOINT_MIN   # borne basse
    assert inlet_setpoint_advice(None) is None
    c = compute_cooling_conditions(35.0, 15.0, 0.62)
    assert c["load_delta_t"] == 0.62 and c["inlet_setpoint_advice"] == round(config.TARGET_TEMP - 0.62, 1)
    assert compute_cooling_conditions(35.0, 15.0)["inlet_setpoint_advice"] is None


def test_mean_load_delta_smooths_the_one_degree_reading():
    from control.cooling_advice import mean_load_delta
    hot = [(i, 34.0 if i % 2 else 35.0) for i in range(40)]       # PT02 au degré près : 34 / 35
    cold = [(i, 33.9) for i in range(40)]
    assert mean_load_delta(hot, cold) == pytest.approx(0.6)
    assert mean_load_delta(hot[:2], cold) is None
