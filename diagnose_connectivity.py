"""
    Diagnostic réseau sans authentification ni modification de l'installation.
    Vérifie la connectivité TCP vers le broker MQTT et les API de la passerelle.
    Ne tente pas de se connecter à la passerelle ni d'envoyer des commandes industrielles
    (aucune action sur le matériel, pas de risque de déclencher un relais ou un
    variateur).
    
    Usage : python diagnose_connectivity.py

"""

from __future__ import annotations

import socket
from urllib.parse import urlparse

import config


def check_tcp(label: str, host: str, port: int) -> None:
    try:
        with socket.create_connection((host, port), timeout=3):
            print(f"OK    {label}: {host}:{port}")
    except OSError as exc:
        print(f"FAIL  {label}: {host}:{port} ({exc})")


def check_url(label: str, url: str) -> None:
    parsed = urlparse(url)
    if not parsed.hostname:
        print(f"FAIL  {label}: URL invalide")
        return
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    check_tcp(label, parsed.hostname, port)


def main() -> None:
    print("Voltaneum connectivity diagnostic")
    print("No credentials are sent and no industrial command is issued.\n")
    check_tcp("MQTT", config.MQTT_HOST, config.MQTT_PORT)
    check_url("Gateway API login", config.MANAGE_API_URL)
    check_url("Gateway point catalog", config.IOT_API_URL)
    check_url("Gateway WebSocket", config.WS_URL)
    print("\nInterpretation:")
    print("- MQTT localhost refused: start a local broker or set MQTT_HOST to an authorized broker.")
    print("- 10.x/192.168.x gateway unreachable: connect to the company LAN or approved VPN.")
    print("- Do not expose the gateway or broker publicly and do not disable TLS verification.")


if __name__ == "__main__":
    main()
