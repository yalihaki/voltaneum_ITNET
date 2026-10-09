"""Création homogène des clients MQTT du projet.

Tous les modules (collecteur, notifier, capteur extérieur, monitoring…) créent leur
client ici : mêmes réglages de sécurité (authentification, TLS vérifié) et même API
paho-mqtt (callbacks version 2) partout, sans code dupliqué.
"""

from __future__ import annotations

import logging
import ssl
import threading

import paho.mqtt.client as mqtt

import config

logger = logging.getLogger(__name__)


def create_client(client_id: str) -> mqtt.Client:
    """Crée un client MQTT configuré (identifiants et TLS selon `config`).

    Le TLS, s'il est activé, exige TOUJOURS la vérification du certificat du
    broker (`CERT_REQUIRED`) : `config.validate_config()` refuse
    `MQTT_TLS_INSECURE=true`.
    """
    client = mqtt.Client(callback_api_version=mqtt.CallbackAPIVersion.VERSION2, client_id=client_id)
    client.reconnect_delay_set(min_delay=1, max_delay=30)
    if config.MQTT_USERNAME:
        client.username_pw_set(config.MQTT_USERNAME, config.MQTT_PASSWORD)
    if config.MQTT_TLS_ENABLED:
        client.tls_set(
            ca_certs=config.MQTT_TLS_CA_CERT,
            certfile=config.MQTT_TLS_CERTFILE,
            keyfile=config.MQTT_TLS_KEYFILE,
            cert_reqs=ssl.CERT_REQUIRED,
            tls_version=ssl.PROTOCOL_TLS_CLIENT,
        )
    return client


def connect_with_retry(client: mqtt.Client, stop_event: threading.Event, max_backoff: float = 30.0) -> bool:
    """Connecte le client au broker en réessayant (délai exponentiel plafonné) tant que
    le broker est injoignable. Retourne False si l'arrêt est demandé avant la connexion.

    Sans cela, un broker indisponible au démarrage ferait mourir le thread définitivement.
    """
    backoff = 1.0
    while not stop_event.is_set():
        try:
            client.connect(config.MQTT_HOST, config.MQTT_PORT, keepalive=config.MQTT_KEEPALIVE_SECONDS)
            return True
        except OSError as exc:
            logger.error("Broker MQTT %s:%s injoignable (%s), nouvel essai dans %.0f s",
                         config.MQTT_HOST, config.MQTT_PORT, exc, backoff)
            if stop_event.wait(backoff):
                return False
            backoff = min(backoff * 2, max_backoff)
    return False
