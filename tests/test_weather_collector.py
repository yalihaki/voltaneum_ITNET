"""
tests/test_weather_collector.py — Vérifie get_outdoor_temperature().

En clair : comme pour la passerelle, on simule la réponse de l'API météo
pour tester la logique de parsing sans dépendre d'un vrai appel réseau
(le bac à sable de développement n'a pas forcément accès à internet).
Un test réel (non mocké) est fourni en complément, marqué comme manuel.
"""

from unittest.mock import MagicMock, patch

import os

import pytest
import requests

from collectors.weather_collector import get_outdoor_temperature

requires_real_network = pytest.mark.skipif(
    os.environ.get("RUN_NETWORK_TESTS") != "1",
    reason="Test d'intégration réel (réseau/passerelle) : lancer avec RUN_NETWORK_TESTS=1 depuis le réseau concerné.",
)


class TestGetOutdoorTemperatureMocked:
    @patch("collectors.weather_collector.requests.get")
    def test_returns_plausible_float(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "current_weather": {"temperature": 12.9, "windspeed": 5.4}
        }
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        temperature = get_outdoor_temperature(lat=48.8566, lon=2.3522)

        assert isinstance(temperature, float)
        assert -50.0 <= temperature <= 55.0
        assert temperature == pytest.approx(12.9)

    @patch("collectors.weather_collector.requests.get")
    def test_raises_on_network_error(self, mock_get):
        mock_get.side_effect = requests.exceptions.ConnectionError("timeout")
        with pytest.raises(requests.exceptions.ConnectionError):
            get_outdoor_temperature()

    @patch("collectors.weather_collector.requests.get")
    def test_raises_on_missing_temperature_key(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.return_value = {"current_weather": {"windspeed": 5.4}}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with pytest.raises(RuntimeError, match="Format de réponse météo inattendu"):
            get_outdoor_temperature()

    @patch("collectors.weather_collector.requests.get")
    def test_raises_on_implausible_temperature(self, mock_get):
        """Une valeur absurde (ex. bug d'unité Kelvin/Celsius) doit être rejetée,
        jamais renvoyée telle quelle."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"current_weather": {"temperature": 286.5}}
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with pytest.raises(RuntimeError, match="hors plage plausible"):
            get_outdoor_temperature()

    @patch("collectors.weather_collector.requests.get")
    def test_never_returns_invented_value_on_bad_json(self, mock_get):
        mock_response = MagicMock()
        mock_response.json.side_effect = ValueError("not json")
        mock_response.text = "<html>Service Unavailable</html>"
        mock_response.raise_for_status.return_value = None
        mock_get.return_value = mock_response

        with pytest.raises(RuntimeError, match="non-JSON"):
            get_outdoor_temperature()


@requires_real_network
def test_get_outdoor_temperature_real_api():
    """Test d'intégration réel : l'API Open-Meteo doit répondre avec une valeur plausible."""
    temperature = get_outdoor_temperature()
    assert isinstance(temperature, float)
    assert -50.0 <= temperature <= 55.0