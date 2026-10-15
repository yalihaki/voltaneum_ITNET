"""
collectors/outdoor_consistency_checker.py — Compare le capteur extérieur
physique à l'API météo, à titre de VÉRIFICATION uniquement.

Contexte (30 septembre 2026, suite discussion avec Taher ; repli ajouté le
12 octobre 2026) : le capteur physique est la valeur prioritaire injectée dans
last_known / SQLite / le PID ; l'API météo ne la remplace que lorsque le
capteur n'a aucune valeur actuelle (voir service/app.py). Ce module compare les deux
quand le capteur fournit une valeur, et transmet l'écart au moteur d'alertes
(notifications/alerts.py), qui peut alerter en cas de divergence
importante et persistante. Sans valeur capteur, il ne compare rien (jamais
la météo à elle-même).

Ce module ne décide jamais d'une alerte lui-même (séparation des
responsabilités déjà en place pour ml/monitor.py) : il publie un fait mesuré
(l'écart), c'est notifications/alerts.py qui décide d'alerter, avec son propre
cooldown et sa confirmation multi-cycles.

Important : ce module ne rejette et ne remplace JAMAIS une valeur du capteur.
Un grand écart est un signal à vérifier humainement, pas une preuve que le
capteur a tort — les deux mesurent des choses légitimement différentes
(micro-climat local vs moyenne régionale).
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import config
from collectors.weather_collector import get_outdoor_temperature

logger = logging.getLogger(__name__)


class OutdoorConsistencyChecker:
    """Compare périodiquement la valeur du capteur physique (fournie par
    `get_sensor_value`, injectée pour rester agnostique du mode mqtt/http de
    OutdoorSensorListener) à l'API météo, et transmet l'écart à `on_result` (le moteur
    d'alertes du processus principal) : aucun broker n'est nécessaire."""

    def __init__(
        self,
        get_sensor_value: Callable[[], float | None],
        interval_seconds: int | None = None,
        on_result: Callable[[dict], None] | None = None,
    ) -> None:
        self._get_sensor_value = get_sensor_value
        self._interval_seconds = interval_seconds or config.OUTDOOR_CONSISTENCY_CHECK_INTERVAL_SECONDS
        self._on_result = on_result
        self._stop_event = threading.Event()

    def check_once(self) -> dict | None:
        """Effectue une comparaison et publie le résultat. Retourne le
        résultat (utile pour les tests), ou None si l'une des deux valeurs
        est indisponible (pas d'erreur dans ce cas : c'est un état normal,
        ex. capteur pas encore raccordé ou météo temporairement injoignable)."""
        sensor_value = self._get_sensor_value()
        if sensor_value is None:
            logger.debug("Vérification de cohérence ignorée : pas de valeur capteur pour l'instant.")
            return None

        try:
            weather_value = get_outdoor_temperature()
        except Exception as exc:
            logger.debug("Vérification de cohérence ignorée : météo indisponible (%s).", exc)
            return None

        gap = abs(sensor_value - weather_value)
        result = {
            "sensor_value": sensor_value,
            "weather_value": weather_value,
            "gap": gap,
            "ts": time.time(),
        }
        if self._on_result is not None:
            self._on_result(result)

        if gap > config.OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS:
            logger.info(
                "Écart capteur/météo notable : capteur=%.1f°C météo=%.1f°C écart=%.1f°C "
                "(seuil %.1f°C) — signalé pour vérification, capteur conservé comme valeur active.",
                sensor_value, weather_value, gap, config.OUTDOOR_CONSISTENCY_MAX_GAP_CELSIUS,
            )
        return result

    def run_forever(self) -> None:
        """Boucle bloquante, à lancer dans un thread (arrêt via `stop()`)."""
        while not self._stop_event.wait(self._interval_seconds):
            try:
                self.check_once()
            except Exception:
                logger.exception("Erreur inattendue dans la vérification de cohérence")

    def stop(self) -> None:
        self._stop_event.set()
