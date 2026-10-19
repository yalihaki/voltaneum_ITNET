"""dashboard/server.py — Serveur web de l'interface de supervision (bibliothèque standard seule).

En clair : un petit serveur HTTP qui sert la page de supervision et quelques routes JSON en
LECTURE SEULE sur la base SQLite. Aucune route n'écrit, aucune ne parle à la passerelle, au
broker MQTT ou à l'automate : l'interface ne peut rien commander.

Les nouvelles mesures sont poussées au navigateur dès leur arrivée en base par
« Server-Sent Events » (/api/stream) : l'écran se met à jour sans recharger la page.

Sécurité :
  - écoute par défaut sur 127.0.0.1 uniquement (accès distant par tunnel SSH) ;
  - en-tête Host vérifié (protège contre le « DNS rebinding ») ;
  - fichiers statiques servis depuis une liste fermée (aucun chemin construit depuis l'URL) ;
  - Content-Security-Policy stricte : aucun script ni ressource externe.
"""

from __future__ import annotations

import ipaddress
import json
import logging
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from dashboard import data, exports

logger = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).resolve().parent / "static"
STATIC_FILES: dict[str, tuple[str, str]] = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/static/app.css": ("app.css", "text/css; charset=utf-8"),
    "/static/app.js": ("app.js", "text/javascript; charset=utf-8"),
    "/static/chart.js": ("chart.js", "text/javascript; charset=utf-8"),
    "/static/favicon.svg": ("favicon.svg", "image/svg+xml"),
}

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; font-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}

# Flux temps réel : fréquence de vérification de la base et battement de cœur.
STREAM_POLL_SECONDS = 1.0
STREAM_HEARTBEAT_SECONDS = 15.0
MAX_STREAM_CLIENTS = 16


class BadRequest(ValueError):
    """Paramètre de requête invalide (réponse 400 avec un message lisible)."""


def _range_from_query(query: dict[str, list[str]], now: float) -> tuple[float, float]:
    """Période demandée : `minutes=` ou `days=` (jusqu'à maintenant), ou `start=` / `end=` (epoch ms)."""
    try:
        if "start" in query:
            start = float(query["start"][0]) / 1000
            end = float(query.get("end", [str(now * 1000)])[0]) / 1000
        elif "days" in query:
            start, end = now - float(query["days"][0]) * 86400, now
        else:
            minutes = float(query.get("minutes", ["120"])[0])
            start, end = now - minutes * 60, now
    except ValueError:
        raise BadRequest("période invalide (minutes, days, start et end doivent être des nombres)") from None
    if not end > start:
        raise BadRequest("la fin de la période doit être postérieure à son début")
    if end - start > data.MAX_RANGE_DAYS * 86400:
        raise BadRequest(f"période limitée à {data.MAX_RANGE_DAYS} jours")
    return start, end


def _int_param(query: dict[str, list[str]], name: str, default: int) -> int:
    try:
        return int(query.get(name, [str(default)])[0])
    except ValueError:
        raise BadRequest(f"{name} doit être un entier") from None


class DashboardServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], db_path: str,
                 extra_hosts: tuple[str, ...] = (), demo: bool = False) -> None:
        super().__init__(address, DashboardHandler)
        self.db_path = db_path
        # Base FICTIVE (./voltaneum demo) : l'interface l'annonce partout, exports compris.
        self.demo = demo
        self.stop_event = threading.Event()
        self.stream_slots = threading.BoundedSemaphore(MAX_STREAM_CLIENTS)
        self.allowed_hosts = {"localhost", "127.0.0.1", "[::1]", address[0].lower(), *extra_hosts}

    def shutdown(self) -> None:
        self.stop_event.set()  # libère les flux temps réel en cours
        super().shutdown()


class DashboardHandler(BaseHTTPRequestHandler):
    server: DashboardServer
    server_version = "Voltaneum"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    # -- journalisation dans le logging du projet (et non sur stderr) --------------------
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        logger.debug("%s - %s", self.address_string(), format % args)

    # -- réponses ------------------------------------------------------------------------
    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        for key, value in {**SECURITY_HEADERS, **(extra or {})}.items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, payload: object, status: int = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False, allow_nan=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", {"Cache-Control": "no-store"})

    def _error(self, status: int, message: str) -> None:
        self._json({"error": message}, status)

    def _host_allowed(self) -> bool:
        host = (self.headers.get("Host") or "").strip().lower()
        if not host:
            return False
        name = host.rsplit(":", 1)[0] if not host.endswith("]") else host
        if name in self.server.allowed_hosts:
            return True
        try:
            # Accès par adresse IP directe (ex. 10.0.1.50:8080 quand l'écoute est élargie).
            ipaddress.ip_address(name.strip("[]"))
            return self.server.server_address[0] not in ("127.0.0.1", "localhost", "::1")
        except ValueError:
            return False

    # -- routage -------------------------------------------------------------------------
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        if not self._host_allowed():
            self._error(HTTPStatus.FORBIDDEN, "hôte non autorisé")
            return
        url = urlsplit(self.path)
        query = parse_qs(url.query)
        route = url.path
        try:
            if route in STATIC_FILES:
                self._static(route)
            elif route == "/api/snapshot":
                self._json(data.latest_snapshot(self.server.db_path))
            elif route == "/api/series":
                start, end = _range_from_query(query, time.time())
                points = _int_param(query, "points", data.DEFAULT_MAX_POINTS)
                payload = data.telemetry_series(self.server.db_path, start, end, points)
                payload["predictions"] = data.prediction_series(self.server.db_path, start, end, points)
                payload["control"] = data.control_series(self.server.db_path, start, end, points)
                self._json(payload)
            elif route == "/api/alerts":
                level = query.get("level", [None])[0] or None
                since = None
                if "since_hours" in query:
                    since = time.time() - _int_param(query, "since_hours", 24) * 3600
                self._json(data.alerts(self.server.db_path, _int_param(query, "limit", 200), level, since))
            elif route == "/api/models":
                self._json(data.model_stats(self.server.db_path))
            elif route == "/api/retrain":
                self._json(data.retrain_status())
            elif route == "/api/system":
                self._json(data.system_status(self.server.db_path))
            elif route == "/api/pid":
                self._json(data.pid_settings())
            elif route == "/api/control":
                start, end = _range_from_query(query, time.time())
                self._json(data.control_history(self.server.db_path, start, end, _int_param(query, "limit", 500)))
            elif route == "/api/control.csv":  # ancienne adresse, conservée
                self._export("consignes", *_range_from_query(query, time.time()))
            elif route.startswith("/api/export/") and route.endswith(".csv"):
                name = route[len("/api/export/"):-len(".csv")]
                if name not in exports.EXPORTS:
                    self._error(HTTPStatus.NOT_FOUND, "export inconnu")
                else:
                    self._export(name, *_range_from_query(query, time.time()))
            elif route == "/api/exports":
                self._json(exports.describe())
            elif route == "/api/catalogue":
                self._json(data.catalogue(self.server.db_path))
            elif route == "/api/settings":
                self._json(data.settings_summary())
            elif route == "/api/mode":
                self._json({"demo": self.server.demo})
            elif route == "/api/export.csv":  # ancienne adresse, conservée
                self._export("mesures", *_range_from_query(query, time.time()))
            elif route == "/api/stream":
                self._stream()
            elif route == "/healthz":
                self._send(HTTPStatus.OK, b"ok", "text/plain; charset=utf-8")
            else:
                self._error(HTTPStatus.NOT_FOUND, "page introuvable")
        except BadRequest as exc:
            self._error(HTTPStatus.BAD_REQUEST, str(exc))
        except data.DatabaseUnavailable as exc:
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, str(exc))
        except (BrokenPipeError, ConnectionResetError):
            pass  # navigateur fermé pendant la réponse
        except Exception:
            logger.exception("Erreur sur %s", route)
            self._error(HTTPStatus.INTERNAL_SERVER_ERROR, "erreur interne (voir le journal du serveur)")

    def _static(self, route: str) -> None:
        name, content_type = STATIC_FILES[route]
        body = (STATIC_DIR / name).read_bytes()
        self._send(HTTPStatus.OK, body, content_type, {"Cache-Control": "no-cache"})

    def _export(self, name: str, start: float, end: float) -> None:
        """Envoie le CSV d'un élément au fil de l'eau (aucune limite de taille en mémoire)."""
        chunks = exports.csv_chunks(name, self.server.db_path, start, end)
        first = next(chunks)  # erreurs (base absente…) levées AVANT l'envoi des en-têtes
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/csv; charset=utf-8")
        self.send_header("Content-Disposition", f'attachment; filename="{"DEMO-FICTIF-" if self.server.demo else ""}{exports.filename(name, start, end)}"')
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        for key, value in SECURITY_HEADERS.items():
            self.send_header(key, value)
        self.end_headers()
        self.close_connection = True
        if self.command == "HEAD":
            return
        self.wfile.write(first)
        for chunk in chunks:
            self.wfile.write(chunk)

    def _stream(self) -> None:
        """Flux SSE : un événement `snapshot` à chaque nouvelle ligne de mesures, `alert` à
        chaque nouvelle alerte, et un commentaire régulier pour garder la connexion ouverte."""
        if not self.server.stream_slots.acquire(blocking=False):
            self._error(HTTPStatus.SERVICE_UNAVAILABLE, "trop de connexions temps réel ouvertes")
            return
        try:
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "close")
            self.send_header("X-Accel-Buffering", "no")
            for key, value in SECURITY_HEADERS.items():
                self.send_header(key, value)
            self.end_headers()
            self.close_connection = True
            self._write_event("retry", "3000")

            last_row = last_alert = None
            last_beat = time.monotonic()
            while not self.server.stop_event.is_set():
                try:
                    row_id, alert_id = self._last_ids()
                except data.DatabaseUnavailable as exc:
                    self._write_event("unavailable", json.dumps({"error": str(exc)}, ensure_ascii=False))
                    row_id = alert_id = None
                if row_id is not None and row_id != last_row:
                    snapshot = data.latest_snapshot(self.server.db_path)
                    self._write_event("snapshot", json.dumps(snapshot, ensure_ascii=False))
                    last_row = row_id
                    last_beat = time.monotonic()
                if alert_id is not None and alert_id != last_alert:
                    if last_alert is not None:  # pas à l'ouverture : seulement les nouvelles
                        self._write_event("alert", json.dumps({"id": alert_id}))
                    last_alert = alert_id
                if time.monotonic() - last_beat >= STREAM_HEARTBEAT_SECONDS:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    last_beat = time.monotonic()
                self.server.stop_event.wait(STREAM_POLL_SECONDS)
        except (BrokenPipeError, ConnectionResetError, TimeoutError):
            pass
        finally:
            self.server.stream_slots.release()

    def _write_event(self, event: str, payload: str) -> None:
        if event == "retry":
            self.wfile.write(f"retry: {payload}\n\n".encode())
        else:
            self.wfile.write(f"event: {event}\ndata: {payload}\n\n".encode())
        self.wfile.flush()

    def _last_ids(self) -> tuple[tuple | None, int | None]:
        """(dernière mesure, dernier cycle de contrôle) et dernière alerte : un changement de
        l'un des deux premiers déclenche l'envoi d'un nouvel instantané."""
        with data.connect_readonly(self.server.db_path) as conn:
            row = conn.execute("SELECT MAX(id) FROM telemetry").fetchone()[0]
            tables = {name for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
            control = conn.execute("SELECT MAX(id) FROM control_log").fetchone()[0] if "control_log" in tables else None
            alert = None
            if "alerts_log" in tables:
                alert = conn.execute("SELECT COALESCE(MAX(id), 0) FROM alerts_log").fetchone()[0]
        return (None if row is None else (row, control)), alert


def is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def make_server(host: str, port: int, db_path: str, extra_hosts: tuple[str, ...] = (),
                demo: bool = False) -> DashboardServer:
    return DashboardServer((host, port), db_path, extra_hosts, demo)
