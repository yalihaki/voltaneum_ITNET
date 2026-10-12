"""
collectors/weather_collector.py — Température extérieure via une API météo publique.

NOTE (12 octobre 2026) : deux usages.
  1. REPLI : quand le capteur physique (collectors/outdoor_sensor_collector.py)
     n'a pas de valeur actuelle, main.py utilise cette température à sa place
     (le capteur reste toujours prioritaire ; la source "weather" est
     enregistrée dans la colonne `temp_outdoor_source`).
  2. COMPARAISON : collectors/outdoor_consistency_checker.py la compare au
     capteur, quand celui-ci fournit une valeur, pour détecter un écart anormal.

Rappel du contexte (section 4 du brief) : aucun capteur physique de
température de pièce ou de baie n'existe sur l'installation. La seule
source de "température extérieure" possible est donc une API météo
externe, interrogée avec les coordonnées GPS du site (configurées dans
config.py via VOLTANEUM_LAT / VOLTANEUM_LON).

En clair : on utilise Open-Meteo (https://open-meteo.com), une API météo
publique et gratuite qui ne nécessite pas de clé d'authentification —
contrairement à beaucoup d'autres API météo (OpenWeatherMap, etc.) qui
demandent de créer un compte et de gérer une clé API. C'est plus simple à
mettre en place pour ce projet, et évite d'avoir un identifiant de plus à
protéger (voir règle non négociable n°1).
"""

from __future__ import annotations

import logging
from typing import Any

import requests

from config import LATITUDE, LONGITUDE

logger = logging.getLogger(__name__)

OPEN_METEO_URL = "https://api.open-meteo.com/v1/forecast"

# Bornes de plausibilité physique (contrat 6.4 / spec §3 étape 6) : la
# température la plus froide jamais enregistrée sur Terre est d'environ
# -89°C (Antarctique), et la plus chaude d'environ 57°C (Death Valley).
# On retient une marge un peu plus resserrée mais réaliste pour un site
# industriel en usage normal : -50°C à 55°C. Une valeur hors de cette
# plage indique très probablement un problème de parsing, pas une vraie
# mesure météo.
PLAUSIBLE_TEMP_MIN = -50.0
PLAUSIBLE_TEMP_MAX = 55.0


def get_outdoor_temperature(lat: float = LATITUDE, lon: float = LONGITUDE) -> float:
    """Interroge Open-Meteo et retourne la température actuelle en °C.

    Lève une exception explicite en cas d'échec (réseau, réponse
    inattendue, valeur hors plage physiquement plausible) — ne retourne
    JAMAIS une valeur inventée en cas d'échec (règle non négociable n°6 :
    pas d'échec silencieux).
    """
    params: dict[str, Any] = {
        "latitude": lat,
        "longitude": lon,
        "current_weather": "true",
    }

    try:
        response = requests.get(OPEN_METEO_URL, params=params, timeout=10)
        response.raise_for_status()
    except requests.exceptions.RequestException as exc:
        logger.error("Échec réseau lors de l'appel à l'API météo : %s", exc)
        raise

    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            f"Réponse non-JSON reçue de l'API météo. Corps brut : {response.text!r}"
        ) from exc

    current_weather = payload.get("current_weather")
    if not isinstance(current_weather, dict) or "temperature" not in current_weather:
        raise RuntimeError(
            f"Format de réponse météo inattendu (clé 'current_weather.temperature' "
            f"absente). Corps reçu : {payload!r}"
        )

    temperature = current_weather["temperature"]
    try:
        temperature = float(temperature)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Température météo non numérique reçue : {temperature!r}"
        ) from exc

    if not (PLAUSIBLE_TEMP_MIN <= temperature <= PLAUSIBLE_TEMP_MAX):
        raise RuntimeError(
            f"Température météo hors plage plausible ({PLAUSIBLE_TEMP_MIN}°C à "
            f"{PLAUSIBLE_TEMP_MAX}°C) : {temperature}°C reçu. Probable erreur de "
            f"parsing ou d'unité (l'API renvoie-t-elle bien du Celsius ?)."
        )

    return temperature