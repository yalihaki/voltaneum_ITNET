"""Tests des plages de validité des capteurs et des invariants de sécurité de la configuration.

Ces tests verrouillent deux corrections :
  * les plages réelles de l'installation (débit 0-15 m³/h, fréquence 0-50 Hz) : avec l'ancienne
    plage de fréquence (0-15 Hz), une pompe à 30 Hz était rejetée à chaque message ;
  * la borne haute de la température d'huile DOIT dépasser CRITICAL_TEMP, sinon une surchauffe
    réelle serait rejetée comme « hors plage » et l'alerte critique ne partirait jamais.
"""

import pytest

import config


@pytest.fixture
def valid_credentials(monkeypatch):
    monkeypatch.setattr(config, "VOLTANEUM_USER", "u")
    monkeypatch.setattr(config, "VOLTANEUM_PASSWORD", "p")


class TestSensorRanges:
    def test_flow_range_matches_the_installation(self):
        assert config.SENSOR_VALID_RANGES["ht01"] == (0.0, 15.0)

    def test_pump_frequency_range_matches_the_variable_speed_drive(self):
        assert config.SENSOR_VALID_RANGES["freq_pompe"] == (0.0, 50.0)

    def test_outdoor_range(self):
        assert config.SENSOR_VALID_RANGES["temp_outdoor"] == (-20.0, 55.0)

    @pytest.mark.parametrize("column", ["pt01", "pt02", "pt03", "pt04"])
    def test_every_temperature_probe_can_measure_above_the_critical_threshold(self, column):
        assert config.SENSOR_VALID_RANGES[column][1] > config.CRITICAL_TEMP

    def test_observed_real_values_are_accepted(self):
        """Valeurs relevées sur l'installation le 9 octobre 2026."""
        observed = {"pt01": 33.5, "pt02": 34.0, "pt03": 33.1, "pt04": 31.9, "ht01": 10.4, "freq_pompe": 30.0}
        for column, value in observed.items():
            low, high = config.SENSOR_VALID_RANGES[column]
            assert low <= value <= high, column

    def test_every_column_has_a_range(self):
        for column in config.FEATURE_COLUMNS + [config.TARGET_COLUMN]:
            assert column in config.SENSOR_VALID_RANGES


class TestSafetyInvariants:
    def test_default_configuration_is_valid(self, valid_credentials):
        config.validate_config()

    def test_refuses_an_oil_range_that_hides_overheating(self, valid_credentials, monkeypatch):
        ranges = dict(config.SENSOR_VALID_RANGES)
        ranges[config.TARGET_COLUMN] = (-20.0, config.CRITICAL_TEMP)   # l'erreur à ne pas commettre
        monkeypatch.setattr(config, "SENSOR_VALID_RANGES", ranges)
        with pytest.raises(ValueError, match="surchauffe"):
            config.validate_config()


class TestOutdoorBandsValidation:
    def _set_bands(self, monkeypatch, bands):
        monkeypatch.setattr(config, "OUTDOOR_BANDS", bands)

    def test_empty_table_is_refused(self, valid_credentials, monkeypatch):
        self._set_bands(monkeypatch, ())
        with pytest.raises(ValueError, match="OUTDOOR_BANDS"):
            config.validate_config()

    def test_non_increasing_limits_are_refused(self, valid_credentials, monkeypatch):
        self._set_bands(monkeypatch, ((20.0, "a", 0.0, 30.0), (10.0, "b", 0.0, 30.0), (float("inf"), "c", 0.0, 30.0)))
        with pytest.raises(ValueError, match="croissantes"):
            config.validate_config()

    def test_last_band_must_be_open(self, valid_credentials, monkeypatch):
        self._set_bands(monkeypatch, ((10.0, "a", 0.0, 30.0), (25.0, "b", 0.0, 30.0)))
        with pytest.raises(ValueError, match="ouverte"):
            config.validate_config()

    def test_start_threshold_must_stay_below_the_critical_temperature(self, valid_credentials, monkeypatch):
        self._set_bands(monkeypatch, ((float("inf"), "chaud", config.CRITICAL_TEMP, 30.0),))
        with pytest.raises(ValueError, match="CRITICAL_TEMP"):
            config.validate_config()

    def test_frequency_must_fit_the_drive_range(self, valid_credentials, monkeypatch):
        self._set_bands(monkeypatch, ((float("inf"), "chaud", 0.0, 80.0),))
        with pytest.raises(ValueError, match="freq_pompe"):
            config.validate_config()


@pytest.mark.parametrize("name,value,fragment", [
    ("CONTROL_MAX_DATA_AGE_SECONDS", 0, "CONTROL_MAX_DATA_AGE_SECONDS"),
    ("FAN_HYSTERESIS_CELSIUS", 0.0, "FAN_HYSTERESIS_CELSIUS"),
    ("FLOW_MIN_M3H", 0.0, "FLOW_MIN_M3H"),
    ("PUMP_RUNNING_MIN_HZ", -1.0, "PUMP_RUNNING_MIN_HZ"),
    ("STORAGE_BACKUP_MIN_AGE_DAYS", -1, "STORAGE_BACKUP_MIN_AGE_DAYS"),
    ("STORAGE_BACKUP_FREE_SPACE_FACTOR", 0.0, "STORAGE_BACKUP_FREE_SPACE_FACTOR"),
])
def test_new_settings_are_validated(valid_credentials, monkeypatch, name, value, fragment):
    monkeypatch.setattr(config, name, value)
    with pytest.raises(ValueError, match=fragment):
        config.validate_config()


def test_backups_are_kept_at_least_one_year_by_default():
    assert config.STORAGE_BACKUP_MIN_AGE_DAYS >= 365
    assert config.RETENTION_DAYS >= 365
