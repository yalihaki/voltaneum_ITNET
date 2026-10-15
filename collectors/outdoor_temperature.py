"""Température extérieure à enregistrer : capteur physique en priorité, API météo en repli.

Utilisé à chaque instantané du collecteur (toutes les `SNAPSHOT_INTERVAL_SECONDS`) et par
la boucle de contrôle, dans le processus principal : la colonne
`temp_outdoor` est ainsi remplie dès que l'une des deux sources a une valeur actuelle,
et `temp_outdoor_source` dit laquelle ("sensor" ou "weather").

Règles :
  * le capteur est utilisé dès qu'il a une valeur actuelle (l'API n'est alors pas appelée) ;
  * sinon, l'API météo est interrogée au plus toutes les
    `OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS` (cache partagé entre les appelants) ;
  * une valeur météo plus ancienne que `OUTDOOR_SENSOR_MAX_AGE_SECONDS` (API injoignable
    depuis longtemps) n'est jamais utilisée : la valeur est alors absente, jamais inventée.
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable

import config
from collectors.weather_collector import get_outdoor_temperature

logger = logging.getLogger(__name__)

SOURCE_SENSOR = "sensor"
SOURCE_WEATHER = "weather"


class OutdoorTemperatureSource:
    """Fournit `(température, source)` ; sûr entre threads (collecteur et boucle de contrôle)."""

    def __init__(
        self,
        read_sensor: Callable[[], float | None],
        fetch_weather: Callable[[], float] = get_outdoor_temperature,
    ) -> None:
        self._read_sensor = read_sensor
        self._fetch_weather = fetch_weather
        self._lock = threading.Lock()
        self._weather_value: float | None = None
        self._weather_value_at: float | None = None
        self._weather_attempted_at: float | None = None
        self._last_source: str | None = None

    def read(self) -> tuple[float | None, str | None]:
        """Température extérieure actuelle et sa source ("sensor", "weather" ou None)."""
        with self._lock:
            value = self._read_sensor_safely()
            source: str | None = SOURCE_SENSOR if value is not None else None
            if value is None and config.OUTDOOR_WEATHER_FALLBACK_ENABLED:
                value = self._read_weather()
                source = SOURCE_WEATHER if value is not None else None
            self._log_source_change(source)
            return value, source

    def _read_sensor_safely(self) -> float | None:
        try:
            return self._read_sensor()
        except Exception as exc:
            logger.warning("Lecture du capteur extérieur impossible : %s", exc)
            return None

    def _read_weather(self) -> float | None:
        now = time.monotonic()
        if (self._weather_attempted_at is None
                or now - self._weather_attempted_at >= config.OUTDOOR_WEATHER_FALLBACK_REFRESH_SECONDS):
            self._weather_attempted_at = now  # même en cas d'échec : pas de rafale d'appels
            try:
                self._weather_value = self._fetch_weather()
                self._weather_value_at = now
            except Exception as exc:
                logger.warning("API météo indisponible pour le repli de température extérieure : %s", exc)
        if self._weather_value_at is None or now - self._weather_value_at > config.OUTDOOR_SENSOR_MAX_AGE_SECONDS:
            return None
        return self._weather_value

    def _log_source_change(self, source: str | None) -> None:
        if source == self._last_source:
            return
        if source == SOURCE_SENSOR:
            logger.info("Température extérieure : capteur utilisé.")
        elif source == SOURCE_WEATHER:
            logger.warning("Température extérieure : capteur sans valeur, repli sur l'API météo.")
        else:
            logger.warning("Température extérieure indisponible (ni capteur, ni météo).")
        self._last_source = source
