"""tests/test_outdoor_consistency.py — Vérificateur de cohérence capteur/météo.

Le vérificateur calcule l'écart, ne décide jamais d'une alerte (les règles sont
testées dans test_alert_rules.py) et ne remplace jamais la valeur du capteur.
"""

import pytest

from collectors.outdoor_consistency_checker import OutdoorConsistencyChecker


class TestOutdoorConsistencyChecker:
    def test_check_once_returns_none_without_sensor_value(self, monkeypatch):
        """Pas de valeur capteur pour l'instant (cas normal au démarrage) :
        pas d'erreur, pas de comparaison inventée."""
        checker = OutdoorConsistencyChecker(get_sensor_value=lambda: None)
        assert checker.check_once() is None

    def test_check_once_returns_none_when_weather_api_fails(self, monkeypatch):
        def _raise():
            raise RuntimeError("API météo indisponible")

        monkeypatch.setattr(
            "collectors.outdoor_consistency_checker.get_outdoor_temperature", _raise
        )
        checker = OutdoorConsistencyChecker(get_sensor_value=lambda: 18.0)
        assert checker.check_once() is None

    def test_check_once_computes_gap_correctly(self, monkeypatch):
        monkeypatch.setattr(
            "collectors.outdoor_consistency_checker.get_outdoor_temperature", lambda: 12.0
        )
        checker = OutdoorConsistencyChecker(get_sensor_value=lambda: 18.5)
        # Pas de vrai broker MQTT dans les tests : la publication peut
        # échouer, ce qui ne doit PAS empêcher check_once() de retourner
        # le résultat calculé (règle 6, aucun crash silencieux mais pas
        # d'interruption de service pour autant).
        result = checker.check_once()
        assert result is not None
        assert result["sensor_value"] == 18.5
        assert result["weather_value"] == 12.0
        assert result["gap"] == pytest.approx(6.5)

