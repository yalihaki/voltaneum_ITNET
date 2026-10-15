"""Température extérieure fournie par un capteur physique indépendant.

Le capteur est installé sur un système séparé de la passerelle industrielle. Deux
modes, choisis par `OUTDOOR_SENSOR_MODE` :

  * "mqtt" (recommandé) : le capteur publie lui-même sa mesure sur le broker MQTT du
    projet (`OUTDOOR_SENSOR_MQTT_TOPIC`). `OutdoorSensorListener` écoute ce topic,
    sans jamais interroger le capteur.
  * "http" (repli) : le capteur expose une petite API HTTP, interrogée à la demande
    par `get_outdoor_temperature_http()`.

Principe commun : jamais de valeur inventée. Un message invalide (non numérique,
hors plage plausible) est rejeté et journalisé ; une mesure trop ancienne
(`OUTDOOR_SENSOR_MAX_AGE_SECONDS`) est considérée comme absente.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from typing import Any

import requests

import config
from messaging.mqtt import connect_with_retry, create_client

logger = logging.getLogger(__name__)

# Bornes de plausibilité physique d'une température extérieure (°C).
PLAUSIBLE_TEMP_MIN = -50.0
PLAUSIBLE_TEMP_MAX = 55.0

# Clés JSON acceptées pour la température dans un payload objet.
_TEMPERATURE_KEYS = ("temperature", "value", "temp", "temp_outdoor")


def _parse_temperature(raw: Any) -> float:
    """Extrait une température d'un payload (nombre, chaîne numérique ou objet JSON).

    Lève `ValueError` si la valeur est absente, non numérique, non finie ou hors plage.
    """
    if isinstance(raw, dict):
        for key in _TEMPERATURE_KEYS:
            if key in raw:
                raw = raw[key]
                break
        else:
            raise ValueError(f"aucune clé de température reconnue dans {sorted(raw)}")
    if isinstance(raw, bool):
        raise ValueError("une valeur booléenne n'est pas une température")
    try:
        temperature = float(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"valeur de température non numérique : {raw!r}") from exc
    if not (PLAUSIBLE_TEMP_MIN <= temperature <= PLAUSIBLE_TEMP_MAX):  # rejette aussi NaN et l'infini
        raise ValueError(
            f"température hors plage plausible ({PLAUSIBLE_TEMP_MIN} à {PLAUSIBLE_TEMP_MAX} °C) : {temperature}"
        )
    return temperature


class OutdoorSensorListener:
    """Abonné MQTT qui retient la dernière température extérieure valide reçue du capteur."""

    def __init__(
        self,
        topic: str | None = None,
        max_age_seconds: float | None = None,
        time_fn: Callable[[], float] = time.time,
    ) -> None:
        self._topic = topic or config.OUTDOOR_SENSOR_MQTT_TOPIC
        self._max_age = config.OUTDOOR_SENSOR_MAX_AGE_SECONDS if max_age_seconds is None else max_age_seconds
        self._time_fn = time_fn
        self._lock = threading.Lock()
        self._last_value: float | None = None
        self._last_received_at: float | None = None
        self._stop_event = threading.Event()

        self._client = create_client("voltaneum-outdoor-sensor-listener")
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message

    def _on_connect(self, client, userdata, flags, reason_code, properties=None) -> None:
        if reason_code != 0:
            logger.error("Connexion MQTT échouée (capteur extérieur), code %s", reason_code)
            return
        logger.info("Abonné au topic du capteur extérieur : %s", self._topic)
        client.subscribe(self._topic, qos=1)  # (ré)abonnement à chaque (re)connexion

    def _on_message(self, client, userdata, msg) -> None:
        raw = msg.payload.decode("utf-8", errors="replace")
        try:
            try:
                parsed: Any = json.loads(raw)
            except json.JSONDecodeError:
                parsed = raw  # valeur brute, ex. "18.4"
            temperature = _parse_temperature(parsed)
        except ValueError as exc:
            logger.warning("Mesure rejetée sur %s : %s (payload=%r)", self._topic, exc, raw[:200])
            return
        with self._lock:
            self._last_value = temperature
            self._last_received_at = self._time_fn()

    def get_last_value(self) -> float | None:
        """Dernière température valide, ou None si aucune n'est arrivée ou si elle est
        trop ancienne (capteur muet : une valeur périmée ne doit pas passer pour actuelle)."""
        with self._lock:
            if self._last_value is None or self._last_received_at is None:
                return None
            if self._time_fn() - self._last_received_at > self._max_age:
                return None
            return self._last_value

    def run_forever(self) -> None:
        """Boucle bloquante, à lancer dans un thread (arrêt via `stop()`)."""
        if not connect_with_retry(self._client, self._stop_event):
            return
        self._client.loop_start()
        try:
            self._stop_event.wait()
        finally:
            self._client.loop_stop()
            self._client.disconnect()

    def stop(self) -> None:
        self._stop_event.set()


def get_outdoor_temperature_http(url: str | None = None, timeout: float | None = None) -> float:
    """Interroge l'API HTTP du capteur (mode de repli).

    Lève une exception explicite en cas d'échec (réseau, JSON, valeur invalide).
    """
    url = url or config.OUTDOOR_SENSOR_HTTP_URL
    if not url:
        raise RuntimeError("OUTDOOR_SENSOR_HTTP_URL n'est pas configuré (mode http).")
    try:
        response = requests.get(url, timeout=timeout or config.OUTDOOR_SENSOR_HTTP_TIMEOUT)  # nosec B113
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.error("Échec de l'appel au capteur extérieur : %s", exc)
        raise
    try:
        payload: Any = response.json()
    except ValueError:
        payload = response.text  # capteur qui répond « 18.4 » en texte brut
    return _parse_temperature(payload)
