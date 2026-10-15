"""service/diagnose.py — Diagnostic de l'installation (`./voltaneum diagnose`).

Vérifie, sans rien modifier et sans envoyer la moindre commande industrielle : configuration,
accès réseau à la passerelle (TCP seulement, aucun identifiant envoyé), broker MQTT, API météo,
e-mail, base de données, modèle installé et port de l'interface.
"""

from __future__ import annotations

import socket
import sqlite3
from pathlib import Path
from urllib.parse import urlparse

import config

OK, WARN, FAIL = "OK   ", "ATTN ", "ÉCHEC"


def _line(status: str, label: str, detail: str) -> None:
    print(f"  {status} {label:<26} {detail}")


def _tcp(host: str, port: int) -> str | None:
    try:
        with socket.create_connection((host, port), timeout=3):
            return None
    except OSError as exc:
        return str(exc)


def _url_target(url: str) -> tuple[str, int] | None:
    parsed = urlparse(url)
    if not parsed.hostname:
        return None
    default = 443 if parsed.scheme in ("https", "wss") else 80
    return parsed.hostname, parsed.port or default


def run_diagnostic() -> int:
    """Affiche le diagnostic ; retourne 1 si un point bloquant est en échec, sinon 0."""
    blocking = False
    print("Diagnostic Voltaneum (lecture seule, aucune commande envoyée)\n")

    print("Configuration")
    try:
        config.validate_config()
        _line(OK, "Fichier .env", "valide")
    except ValueError as exc:
        _line(FAIL, "Fichier .env", str(exc))
        blocking = True

    print("\nPasserelle industrielle (TCP)")
    for label, url in (("API de connexion", config.MANAGE_API_URL), ("Catalogue des points", config.IOT_API_URL),
                       ("Flux temps réel", config.WS_URL)):
        target = _url_target(url)
        if target is None:
            _line(FAIL, label, f"URL invalide : {url}")
            blocking = True
            continue
        error = _tcp(*target)
        _line(OK if error is None else FAIL, label, f"{target[0]}:{target[1]}" + (f" ({error})" if error else ""))
        blocking |= error is not None
    print("    -> injoignable : se connecter au réseau de l'usine ou au VPN autorisé.")

    print("\nServices")
    error = _tcp(config.MQTT_HOST, config.MQTT_PORT)
    _line(OK if error is None else WARN, "Broker MQTT", f"{config.MQTT_HOST}:{config.MQTT_PORT}"
          + (" joignable" if error is None else " injoignable : rien n'est publié pour l'automate, le reste fonctionne"))
    if config.OUTDOOR_WEATHER_FALLBACK_ENABLED:
        try:
            from collectors.weather_collector import get_outdoor_temperature

            _line(OK, "API météo", f"{get_outdoor_temperature():.1f} °C à ({config.LATITUDE}, {config.LONGITUDE})")
        except Exception as exc:  # réseau, format
            _line(WARN, "API météo", f"indisponible ({exc})")
    from notifications.alerts import smtp_configured

    _line(OK if smtp_configured() else WARN, "E-mail des alertes",
          f"{config.SMTP_HOST}:{config.SMTP_PORT} -> {config.ALERT_EMAIL_TO}" if smtp_configured()
          else "non configuré : alertes visibles seulement dans l'interface")

    print("\nDonnées")
    db = Path(config.SQLITE_DB_PATH)
    if not db.exists():
        _line(WARN, "Base SQLite", f"{db} absente (créée au premier démarrage)")
    else:
        try:
            with sqlite3.connect(f"{db.resolve().as_uri()}?mode=ro", uri=True) as conn:
                rows, last = conn.execute("SELECT COUNT(*), MAX(timestamp) FROM telemetry").fetchone()
            _line(OK, "Base SQLite", f"{db.name} : {rows} mesures, dernière {last} UTC")
        except sqlite3.Error as exc:
            _line(FAIL, "Base SQLite", str(exc))
            blocking = True
    report = Path(config.MODEL_DIR) / "training_report.json"
    _line(OK if report.exists() else WARN, "Modèle de prédiction",
          "installé" if report.exists() else "aucun (PID sans anticipation ; voir ./voltaneum model)")
    in_use = _tcp("127.0.0.1", config.DASHBOARD_PORT) is None
    _line(OK, "Port de l'interface", f"{config.DASHBOARD_PORT} " + ("utilisé (Voltaneum tourne ?)" if in_use else "libre"))

    print("\nRésultat :", "point(s) bloquant(s) à corriger." if blocking else "rien de bloquant.")
    return 1 if blocking else 0
