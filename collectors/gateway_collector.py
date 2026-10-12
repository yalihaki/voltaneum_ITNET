"""
collectors/gateway_collector.py — Collecteur de télémétrie depuis la passerelle
HinetGateway L100-WRS485.

En clair : ce module est le "traducteur" entre le monde industriel (la
passerelle qui parle HTTP et WebSocket dans un format propriétaire) et le
reste du projet (qui ne veut voir que des noms de colonnes clairs comme
"pt01" ou "ht01"). Il gère trois choses : se connecter (login), apprendre
le vocabulaire de la passerelle (mapping des points), et écouter le flux
temps réel pour tenir à jour les dernières valeurs connues.

Construction incrémentale (voir spec §3) : login(), refresh_point_mapping()
et désormais la boucle WebSocket complète (_on_ws_message, instantané
périodique, run_forever avec reconnexion automatique) sont implémentés.
"""

from __future__ import annotations

import json
import logging
import math
import threading
import time

import requests
import websocket

import config
from database import sqlite_client

logger = logging.getLogger(__name__)

_MISSING = object()

# Mapping fixe KeyName -> nom de colonne SQLite (contrat 5.3).
# Codé en dur : ces noms ne changent pas sans intervention sur l'automate.
#
# En clair : chaque installation nomme ses points à sa façon. Deux catalogues réels ont été
# observés (relevés du 9 octobre 2026) ; on accepte les deux pour que la même version du code
# fonctionne sur les deux passerelles. Seuls des points en LECTURE SEULE (WrEnable = 0) sont
# listés : les points modifiables du catalogue (ON/OFF, consignes, bypass...) n'apparaissent
# volontairement JAMAIS ici — ce projet ne lit que, il n'écrit jamais.
KEYNAME_TO_COLUMN: dict[str, str] = {
    # Passerelle 1 (10.0.1.33) : noms suffixés « -temperature ».
    "PT01-temperature": "pt01",
    "PT02-temperature": "pt02",
    "PT03-temperature": "pt03",
    "PT04-temperature": "pt04",
    "Pump frequency": "freq_pompe",
    # Passerelle 2 (10.0.1.34) : noms courts.
    "PT01": "pt01",
    "PT02": "pt02",
    "PT03": "pt03",
    "PT04": "pt04",
    # À CONFIRMER avec l'automaticien : sur cette passerelle, « PUMP » (Int16, lecture seule) est
    # supposé être la fréquence pompe / ventilateur ; un point « DIAG Actual frequency VW12 »
    # (diag_vw12) existe aussi et pourrait être la fréquence réellement mesurée.
    "PUMP": "freq_pompe",
    # Commun aux deux passerelles : débit du circuit bain d'huile.
    "HT01": "ht01",
    # Ancien nom supposé dans la spécification initiale (conservé pour compatibilité).
    "Frequence": "freq_pompe",
}


class GatewayCollector:
    """Collecteur de télémétrie pour la passerelle HinetGateway.

    En clair : on regroupe l'état (token, mapping des points, dernières
    valeurs connues) dans une classe plutôt que dans des variables globales,
    pour pouvoir un jour faire tourner plusieurs collecteurs (par exemple
    si une deuxième passerelle est ajoutée) sans qu'ils se marchent dessus.
    """

    def __init__(
        self,
        manage_api_url: str = config.MANAGE_API_URL,
        iot_api_url: str = config.IOT_API_URL,
        ws_url: str = config.WS_URL,
        username: str | None | object = _MISSING,
        password: str | None | object = _MISSING,
        sensor_driver_node: str = config.SENSOR_DRIVER_NODE,
        mqtt_client=None,
        snapshot_interval_seconds: float = config.SNAPSHOT_INTERVAL_SECONDS,
        db_path: str = config.SQLITE_DB_PATH,
    ) -> None:
        self.manage_api_url = manage_api_url
        self.iot_api_url = iot_api_url
        self.ws_url = ws_url
        # Si non fournis explicitement (ex. pour les tests), on retombe sur
        # les variables d'environnement lues dans config.py. Un `None` explicite
        # est, lui, conservé comme « identifiants absents » et doit déclencher
        # une RuntimeError dans `login()`.
        self.username = config.VOLTANEUM_USER if username is _MISSING else username
        self.password = config.VOLTANEUM_PASSWORD if password is _MISSING else password
        self.sensor_driver_node = sensor_driver_node

        # Client MQTT déjà connecté, injecté par l'appelant (main.py, étape 10).
        # En clair : le collecteur ne gère pas lui-même la connexion MQTT — il
        # se contente de publier dessus si on lui en donne un. Cela évite de
        # dupliquer la logique de connexion/reconnexion MQTT à plusieurs
        # endroits du projet ; main.py est le seul responsable du cycle de
        # vie du client MQTT (contrat 6.8).
        self.mqtt_client = mqtt_client
        self.snapshot_interval_seconds = snapshot_interval_seconds
        self.db_path = db_path

        self.token: str | None = None
        self.keynode_to_column: dict[str, str] = {}
        self._lock = threading.Lock()

        # Toutes les colonnes attendues, initialisées à None tant qu'aucune
        # valeur réelle n'a été reçue (contrat 6.3, point 3).
        self.last_known: dict[str, float | None] = {
            column: None for column in config.FEATURE_COLUMNS + [config.TARGET_COLUMN]
        }

        # État interne de la boucle run_forever (thread d'arrêt, app WebSocket
        # active) — permet un arrêt propre depuis stop() (contrat 6.8).
        self._stop_event: threading.Event | None = None
        self._ws_app: websocket.WebSocketApp | None = None
        # Horodatage (monotonic) du dernier message reçu : sert à détecter un flux muet
        # (connexion TCP à moitié morte) et à ne pas écrire de valeurs périmées en base.
        self._last_message_at: float | None = None
        self._stale_reported = False

    def login(self) -> str:
        """Authentifie le collecteur auprès de la passerelle (contrat 5.1).

        Retourne le token obtenu et le stocke dans `self.token`.
        Lève `RuntimeError` si les identifiants sont absents ou si le token
        est introuvable dans la réponse. Ne logue JAMAIS username/password.
        """
        if not self.username or not self.password:
            raise RuntimeError(
                "Identifiants manquants : VOLTANEUM_USER et VOLTANEUM_PASSWORD "
                "doivent être définis dans l'environnement (voir .env.example)."
            )

        try:
            response = requests.post(
                self.manage_api_url,
                data={"username": self.username, "password": self.password},
                timeout=10,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            # En clair : on ne masque jamais une erreur réseau derrière un
            # "except: pass" — on la logue clairement puis on la relaie, pour
            # que l'appelant (run_forever, à une étape suivante) puisse
            # décider de réessayer.
            logger.error("Échec réseau lors de l'authentification : %s", exc)
            raise

        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Réponse non-JSON reçue lors du login (HTTP {response.status_code}, "
                f"{len(response.text)} caractères)."
            ) from exc

        # Le token peut être à la racine ou sous data["data"]["token"]
        # (contrat 5.1 — robustesse face à deux formats de réponse possibles).
        token = data.get("token")
        if not token and isinstance(data.get("data"), dict):
            token = data["data"].get("token")

        if not token:
            # Seules les CLÉS sont journalisées : le corps peut contenir des éléments de session.
            keys = sorted(data) if isinstance(data, dict) else type(data).__name__
            raise RuntimeError(f"Token introuvable dans la réponse de login (clés reçues : {keys}).")

        self.token = token
        logger.info("Login réussi (token obtenu, longueur=%d caractères).", len(token))
        return token

    def refresh_point_mapping(self) -> dict[str, str]:
        """Récupère le catalogue des points et construit le mapping KeyNode -> colonne.

        Combine le mapping KeyNode -> KeyName renvoyé par la passerelle
        (contrat 5.2) avec la table fixe KEYNAME_TO_COLUMN (contrat 5.3),
        et ne retient que les points dont DevNode == self.sensor_driver_node.

        En clair : la passerelle parle en codes internes ("K0", "K1"...) qui
        ne veulent rien dire pour nous. Cette méthode va chercher la "légende"
        de ces codes (KeyNode -> KeyName, ex. "K0" -> "PT03-temperature"),
        puis traduit ces noms en colonnes SQLite compréhensibles ("pt03").
        Le résultat final (self.keynode_to_column) permet, dans la boucle
        WebSocket, de convertir directement un code brut en colonne de base
        de données.

        Doit être appelée après login() (nécessite self.token).
        Lève `RuntimeError` si le format de réponse est inattendu, si aucun
        token n'est disponible, ou si aucun point reconnu n'est trouvé.
        """
        if not self.token:
            raise RuntimeError(
                "Non authentifié : appelez login() avant refresh_point_mapping()."
            )

        try:
            response = requests.post(
                self.iot_api_url,
                data={"token": self.token},
                timeout=10,
            )
            response.raise_for_status()
        except requests.exceptions.RequestException as exc:
            logger.error(
                "Échec réseau lors de la récupération du catalogue des points : %s", exc
            )
            raise

        try:
            payload = response.json()
        except ValueError as exc:
            raise RuntimeError(
                f"Réponse non-JSON reçue pour le catalogue des points. "
                f"Corps brut : {response.text!r}"
            ) from exc

        points = self._extract_points_list(payload)
        if points is None:
            raise RuntimeError(
                f"Format de réponse inattendu pour le catalogue des points : {payload!r}"
            )

        mapping: dict[str, str] = {}
        for point in points:
            if point.get("DevNode") != self.sensor_driver_node:
                continue
            key_node = point.get("KeyNode")
            key_name = point.get("KeyName")
            if not key_node or not key_name:
                continue
            column = KEYNAME_TO_COLUMN.get(key_name)
            if column is None:
                # KeyName non reconnu : hors périmètre, pas une erreur (contrat 6.3 pt.4).
                logger.debug(
                    "KeyName non reconnu, ignoré : %s (KeyNode=%s)", key_name, key_node
                )
                continue
            mapping[key_node] = column

        # Deux points du catalogue ne doivent jamais alimenter la même colonne : la dernière
        # valeur reçue écraserait l'autre sans que personne ne s'en aperçoive.
        seen_columns: dict[str, str] = {}
        for key_node, column in mapping.items():
            if column in seen_columns:
                logger.warning(
                    "Colonne %s alimentée par deux points (%s et %s) : la dernière valeur reçue "
                    "écrasera l'autre — vérifier KEYNAME_TO_COLUMN.",
                    column, seen_columns[column], key_node,
                )
            seen_columns[column] = key_node

        if not mapping:
            raise RuntimeError(
                "Aucun point reconnu dans le catalogue reçu (vérifier "
                f"SENSOR_DRIVER_NODE={self.sensor_driver_node!r} et la table "
                "KEYNAME_TO_COLUMN)."
            )

        self.keynode_to_column = mapping
        logger.info("Mapping des points rafraîchi : %d point(s) reconnu(s).", len(mapping))
        return mapping

    def _on_ws_message(self, raw: str) -> None:
        """Traite un message WebSocket brut reçu de la passerelle (contrat 5.3/6.3.4).

        Ne traite que les messages où Code == "IotData" ET Data.Node ==
        sensor_driver_node ; tout le reste est ignoré silencieusement (pas
        une erreur, juste hors périmètre — ex. "GwDIOData", "NetworkStatus").

        Pour chaque point reçu, traduit son KeyNode en colonne via
        self.keynode_to_column et met à jour self.last_known. Un KeyNode non
        reconnu (mapping pas encore rafraîchi, ou point hors périmètre) est
        ignoré en log DEBUG, jamais en erreur.

        En clair : ce flux WebSocket est très bavard — il envoie plein de
        messages qui ne nous concernent pas (statut réseau, autres appareils
        de la passerelle...). Cette méthode agit comme un videur à l'entrée
        d'une salle : elle ne laisse passer que les informations utiles à
        Voltaneum, et laisse repartir le reste sans faire d'histoires.
        """
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError) as exc:
            logger.warning("Message WebSocket non-JSON ignoré : %s", exc)
            return

        if payload.get("Code") != "IotData":
            return  # Hors périmètre (ex. GwDIOData, NetworkStatus) : pas une erreur.

        data = payload.get("Data", {})
        if data.get("Node") != self.sensor_driver_node:
            return  # Message d'un autre nœud de la passerelle : hors périmètre.

        for point in data.get("Point", []):
            key_node = point.get("K")
            value = point.get("V")
            if key_node is None or value is None:
                continue

            column = self.keynode_to_column.get(key_node)
            if column is None:
                logger.debug(
                    "KeyNode non reconnu dans le flux temps réel, ignoré : %s", key_node
                )
                continue

            if isinstance(value, bool) or not isinstance(value, (int, float)):
                logger.warning(
                    "Valeur hors format pour %s ignorée : %r (attendu nombre fini).",
                    column,
                    value,
                )
                continue
            if not math.isfinite(float(value)):
                logger.warning(
                    "Valeur non finie pour %s ignorée : %r.",
                    column,
                    value,
                )
                continue

            lower, upper = config.SENSOR_VALID_RANGES.get(column, (-float("inf"), float("inf")))
            if not (lower <= float(value) <= upper):
                logger.warning(
                    "Valeur hors plage pour %s ignorée : %r (plage attendue [%s, %s]).",
                    column,
                    value,
                    lower,
                    upper,
                )
                continue

            with self._lock:
                self.last_known[column] = float(value)

    def snapshot(self) -> dict[str, float | None]:
        """Copie cohérente (sous verrou) des dernières valeurs connues."""
        with self._lock:
            return dict(self.last_known)

    def set_value(self, column: str, value: float | None) -> None:
        """Renseigne une valeur qui ne vient pas de la passerelle (ex. température
        extérieure du capteur indépendant). `None` efface la valeur."""
        with self._lock:
            self.last_known[column] = value

    def _ws_on_message(self, ws, message) -> None:
        """Callback compatible websocket-client : délègue à _on_ws_message(raw)."""
        self._last_message_at = time.monotonic()
        self._on_ws_message(message)

    def _ws_on_error(self, ws, error) -> None:
        logger.error("Erreur WebSocket : %s", error)

    def _ws_on_close(self, ws, close_status_code=None, close_msg=None) -> None:
        logger.warning(
            "Connexion WebSocket fermée (code=%s, message=%s).",
            close_status_code,
            close_msg,
        )

    def _snapshot_once(self) -> bool:
        """Insère et publie un instantané si au moins une valeur est connue.

        Retourne True si une insertion a eu lieu, False si toutes les valeurs
        de self.last_known sont encore None (rien reçu jusqu'ici) — contrat
        6.3 point 5 : ne jamais insérer une ligne entièrement vide.
        """
        snapshot = self.snapshot()

        if all(value is None for value in snapshot.values()):
            return False

        if self._stream_is_silent():
            if not self._stale_reported:
                logger.warning(
                    "Aucun message de la passerelle depuis plus de %d s : instantanés suspendus "
                    "(les dernières valeurs ne sont pas réécrites comme si elles étaient actuelles).",
                    config.WS_SILENCE_TIMEOUT_SECONDS,
                )
                self._stale_reported = True
            return False
        self._stale_reported = False

        sqlite_client.insert_reading(
            pt01=snapshot.get("pt01"),
            pt02=snapshot.get("pt02"),
            pt03=snapshot.get("pt03"),
            pt04=snapshot.get("pt04"),
            ht01=snapshot.get("ht01"),
            freq_pompe=snapshot.get("freq_pompe"),
            temp_outdoor=snapshot.get("temp_outdoor"),
            db_path=self.db_path,
        )

        if self.mqtt_client is not None:
            try:
                payload = json.dumps({**snapshot, "ts": time.time()})
                self.mqtt_client.publish(config.MQTT_TOPIC_TELEMETRY, payload)
            except Exception as exc:
                # Une panne MQTT ne doit jamais faire perdre la donnée déjà
                # écrite en base : on logue et on continue.
                logger.error("Échec de publication MQTT (télémétrie) : %s", exc)
        else:
            logger.debug(
                "Aucun client MQTT injecté : instantané écrit en base mais non publié."
            )

        return True

    def seconds_since_last_message(self) -> float | None:
        """Secondes écoulées depuis le dernier message reçu de la passerelle (horloge monotone),
        ou None si aucun message n'a encore été attendu.

        Sert à la boucle de contrôle : une consigne ne doit jamais être calculée à partir de
        mesures périmées (flux coupé, connexion à moitié morte).
        """
        last = self._last_message_at
        return None if last is None else time.monotonic() - last

    def stream_is_silent(self) -> bool:
        """Version publique de `_stream_is_silent` (flux muet plus longtemps que
        `WS_SILENCE_TIMEOUT_SECONDS`)."""
        return self._stream_is_silent()

    def _stream_is_silent(self) -> bool:
        """Vrai si le flux est resté muet plus longtemps que `WS_SILENCE_TIMEOUT_SECONDS`
        (0 = surveillance désactivée). Faux tant qu'aucun message n'a jamais été attendu."""
        timeout = config.WS_SILENCE_TIMEOUT_SECONDS
        if not timeout or self._last_message_at is None:
            return False
        return time.monotonic() - self._last_message_at > timeout

    def _snapshot_loop(self, stop_event: threading.Event) -> None:
        """Boucle d'arrière-plan : un instantané toutes les snapshot_interval_seconds.

        En clair : `stop_event.wait(n)` attend n secondes, MAIS se réveille
        immédiatement si quelqu'un appelle `stop_event.set()` ailleurs — c'est
        ce qui permet un arrêt propre et rapide sur Ctrl+C plutôt que
        d'attendre bêtement la fin du prochain intervalle.
        """
        while not stop_event.wait(self.snapshot_interval_seconds):
            try:
                self._snapshot_once()
                if self._stream_is_silent() and self._ws_app is not None:
                    # Connexion probablement à moitié morte : on la ferme pour forcer la reconnexion.
                    self._ws_app.close()
            except Exception as exc:
                logger.error("Erreur lors de l'instantané périodique : %s", exc)

    def _mapping_refresh_loop(self, stop_event: threading.Event) -> None:
        """Boucle d'arrière-plan : rafraîchit le mapping des points toutes les heures."""
        while not stop_event.wait(config.POINT_MAPPING_REFRESH_SECONDS):
            try:
                self.refresh_point_mapping()
            except Exception as exc:
                logger.error(
                    "Échec du rafraîchissement périodique du mapping des points : %s", exc
                )

    def run_forever(self) -> None:
        """Orchestre login -> refresh_point_mapping -> WebSocket, avec reconnexion.

        En cas de coupure (erreur réseau, fermeture du socket...), retente la
        connexion avec un backoff exponentiel plafonné à
        WS_RECONNECT_BACKOFF_MAX_SECONDS (contrat 6.3 point 6). S'arrête
        proprement quand stop() est appelé.
        """
        stop_event = threading.Event()
        self._stop_event = stop_event

        snapshot_thread = threading.Thread(
            target=self._snapshot_loop, args=(stop_event,), daemon=True
        )
        snapshot_thread.start()

        mapping_thread = threading.Thread(
            target=self._mapping_refresh_loop, args=(stop_event,), daemon=True
        )
        mapping_thread.start()

        backoff = config.WS_RECONNECT_BACKOFF_INITIAL_SECONDS

        while not stop_event.is_set():
            try:
                self.login()
                self.refresh_point_mapping()
                backoff = config.WS_RECONNECT_BACKOFF_INITIAL_SECONDS  # reset après succès

                ws_app = websocket.WebSocketApp(
                    self.ws_url,
                    on_message=self._ws_on_message,
                    on_error=self._ws_on_error,
                    on_close=self._ws_on_close,
                )
                self._ws_app = ws_app
                self._last_message_at = time.monotonic()
                ws_app.run_forever()  # bloque jusqu'à déconnexion ou erreur
            except Exception as exc:
                logger.error("Connexion à la passerelle interrompue : %s", exc)

            if stop_event.is_set():
                break

            logger.info("Reconnexion dans %.1f s...", backoff)
            stop_event.wait(backoff)
            backoff = min(backoff * 2, config.WS_RECONNECT_BACKOFF_MAX_SECONDS)

        snapshot_thread.join(timeout=5)
        mapping_thread.join(timeout=5)
        logger.info("GatewayCollector arrêté proprement.")

    def stop(self) -> None:
        """Demande l'arrêt propre de run_forever() (contrat 6.8)."""
        if self._stop_event is not None:
            self._stop_event.set()
        if self._ws_app is not None:
            self._ws_app.close()

    @staticmethod
    def _extract_points_list(payload) -> list[dict] | None:
        """Extrait la liste de points bruts, quel que soit l'enrobage JSON.

        En clair : selon les API, une liste peut être renvoyée directement,
        ou "enveloppée" dans un objet sous une clé comme "data" ou "points".
        Cette fonction essaie les cas les plus probables plutôt que de
        supposer un seul format figé — voir contrat 5.1 : "si la réponse
        réelle diffère, logger le JSON brut et adapter le parsing, ne pas
        supposer silencieusement".
        """
        if isinstance(payload, list):
            return payload
        if isinstance(payload, dict):
            for key in ("data", "points", "Points", "Data"):
                value = payload.get(key)
                if isinstance(value, list):
                    return value
        return None