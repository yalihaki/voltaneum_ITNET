"""
tests/test_gateway_collector.py — Vérifie login() de GatewayCollector.

En clair : on utilise `unittest.mock.patch` pour remplacer temporairement
`requests.post` par une fausse fonction qui renvoie une réponse HTTP
fabriquée de toutes pièces. Ça permet de tester la logique de login() (que
fait-on de la réponse ?) sans dépendre d'une vraie passerelle joignable sur
le réseau — utile en particulier ici puisque la passerelle réelle
(10.0.1.33) est sur un réseau industriel local, pas accessible depuis
n'importe quel environnement de développement.

Un test réel (sans mock) est aussi fourni, marqué comme "à lancer
manuellement" (skip par défaut) pour validation sur le réseau réel.
"""

import json
import os
import tempfile
import threading
import time
from unittest.mock import MagicMock, patch

import pytest
import requests

from collectors.gateway_collector import GatewayCollector

requires_real_network = pytest.mark.skipif(
    os.environ.get("RUN_NETWORK_TESTS") != "1",
    reason="Test d'intégration réel (réseau/passerelle) : lancer avec RUN_NETWORK_TESTS=1 depuis le réseau concerné.",
)


def _make_collector(username="testuser", password="testpass"):
    return GatewayCollector(username=username, password=password)


class TestLoginMocked:
    """Tests unitaires de login() avec la passerelle simulée (mock)."""

    @patch("collectors.gateway_collector.requests.post")
    def test_login_returns_non_empty_token_root_level(self, mock_post):
        """Le token présent à la racine de la réponse doit être retourné."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ret": 200,
            "msg": "",
            "token": "tf08f28dc-b7b9-d220-c625-ca1d33d74d2d",
        }
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        collector = _make_collector()
        token = collector.login()

        assert token == "tf08f28dc-b7b9-d220-c625-ca1d33d74d2d"
        assert collector.token == token
        assert len(token) > 0

    @patch("collectors.gateway_collector.requests.post")
    def test_login_returns_non_empty_token_nested_level(self, mock_post):
        """Le token présent sous data["data"]["token"] doit aussi être trouvé."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "ret": 200,
            "data": {"token": "nested-token-abc123"},
        }
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        collector = _make_collector()
        token = collector.login()

        assert token == "nested-token-abc123"

    @patch("collectors.gateway_collector.requests.post")
    def test_login_raises_when_token_absent(self, mock_post):
        """Si aucun token n'est trouvé (racine ou nested), lever RuntimeError."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"ret": 401, "msg": "invalid credentials"}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        collector = _make_collector()
        with pytest.raises(RuntimeError, match="Token introuvable"):
            collector.login()

    def test_login_raises_when_credentials_missing(self):
        """Sans username/password, lever RuntimeError avant tout appel réseau."""
        collector = _make_collector(username=None, password=None)
        with pytest.raises(RuntimeError, match="Identifiants manquants"):
            collector.login()

    @patch("collectors.gateway_collector.requests.post")
    def test_login_propagates_network_errors(self, mock_post):
        """Une erreur réseau ne doit jamais être avalée silencieusement."""
        mock_post.side_effect = requests.exceptions.ConnectionError("connexion refusée")

        collector = _make_collector()
        with pytest.raises(requests.exceptions.ConnectionError):
            collector.login()


class TestRefreshPointMapping:
    """Tests unitaires de refresh_point_mapping() avec le catalogue simulé (mock)."""

    def _fake_catalog_payload(self):
        """Catalogue factice couvrant K0-K3 (PT01-PT04) + un point à ignorer."""
        return {
            "ret": 200,
            "data": [
                {"KeyNode": "K0", "KeyName": "PT01-temperature", "DevNode": "idospV1_1"},
                {"KeyNode": "K1", "KeyName": "PT02-temperature", "DevNode": "idospV1_1"},
                {"KeyNode": "K2", "KeyName": "PT03-temperature", "DevNode": "idospV1_1"},
                {"KeyNode": "K3", "KeyName": "PT04-temperature", "DevNode": "idospV1_1"},
                {"KeyNode": "K4", "KeyName": "HT01", "DevNode": "idospV1_1"},
                # Point hors périmètre (autre driver) : doit être filtré.
                {"KeyNode": "K5", "KeyName": "SomeOtherPoint", "DevNode": "autre_driver"},
                # KeyName inconnu de KEYNAME_TO_COLUMN : doit être ignoré sans erreur.
                {"KeyNode": "K6", "KeyName": "Unknown-Point", "DevNode": "idospV1_1"},
            ],
        }

    @patch("collectors.gateway_collector.requests.post")
    def test_refresh_point_mapping_returns_non_empty_dict_with_k0_to_k3(self, mock_post):
        """Le dict retourné ne doit pas être vide et doit contenir K0 à K3."""
        mock_response = MagicMock()
        mock_response.json.return_value = self._fake_catalog_payload()
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        collector = _make_collector()
        collector.token = "fake-token-already-logged-in"

        mapping = collector.refresh_point_mapping()

        assert mapping  # non vide
        for keynode in ("K0", "K1", "K2", "K3"):
            assert keynode in mapping
        assert mapping["K0"] == "pt01"
        assert mapping["K1"] == "pt02"
        assert mapping["K2"] == "pt03"
        assert mapping["K3"] == "pt04"
        # Le point d'un autre driver ne doit pas apparaître.
        assert "K5" not in mapping
        # Le KeyName inconnu ne doit pas apparaître (mais pas d'exception non plus).
        assert "K6" not in mapping
        # L'état interne du collecteur doit aussi être mis à jour.
        assert collector.keynode_to_column == mapping

    @patch("collectors.gateway_collector.requests.post")
    def test_refresh_point_mapping_accepts_bare_list_payload(self, mock_post):
        """Le parsing doit aussi fonctionner si la réponse est une liste brute."""
        mock_response = MagicMock()
        mock_response.json.return_value = self._fake_catalog_payload()["data"]
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        collector = _make_collector()
        collector.token = "fake-token"

        mapping = collector.refresh_point_mapping()
        assert mapping
        assert "K0" in mapping

    @patch("collectors.gateway_collector.requests.post")
    def test_second_gateway_catalog_is_fully_recognized(self, mock_post):
        """Catalogue réel de la passerelle 10.0.1.34 : noms courts, PUMP, points modifiables ignorés."""
        catalog = {"Data": [
            {"KeyNode": "K0", "KeyName": "PT01", "DevNode": "idospV1_1", "WrEnable": 0},
            {"KeyNode": "K1", "KeyName": "PT02", "DevNode": "idospV1_1", "WrEnable": 0},
            {"KeyNode": "K2", "KeyName": "PT03", "DevNode": "idospV1_1", "WrEnable": 0},
            {"KeyNode": "K3", "KeyName": "PT04", "DevNode": "idospV1_1", "WrEnable": 0},
            {"KeyNode": "K4", "KeyName": "HT01", "DevNode": "idospV1_1", "WrEnable": 0},
            {"KeyNode": "K5", "KeyName": "PUMP", "DevNode": "idospV1_1", "WrEnable": 0},
            # Points MODIFIABLES du catalogue : ne doivent JAMAIS être mappés (lecture seule).
            {"KeyNode": "K6", "KeyName": "ON/OFF", "DevNode": "idospV1_1", "WrEnable": 1},
            {"KeyNode": "K12", "KeyName": "Temperature setpoint", "DevNode": "idospV1_1", "WrEnable": 1},
            {"KeyNode": "K17", "KeyName": "Emergency stop bypass", "DevNode": "idospV1_1", "WrEnable": 1},
            {"KeyNode": "auto_mode", "KeyName": "AUTO REQUEST M4.2", "DevNode": "idospV1_1", "WrEnable": 1},
            {"KeyNode": "diag_vw12", "KeyName": "DIAG Actual frequency VW12", "DevNode": "idospV1_1", "WrEnable": 0},
        ]}
        mock_post.return_value = MagicMock(**{"json.return_value": catalog, "raise_for_status.return_value": None})
        collector = _make_collector()
        collector.token = "fake"
        assert collector.refresh_point_mapping() == {
            "K0": "pt01", "K1": "pt02", "K2": "pt03", "K3": "pt04", "K4": "ht01", "K5": "freq_pompe",
        }

    @patch("collectors.gateway_collector.requests.post")
    def test_first_gateway_catalog_still_recognizes_pump_frequency(self, mock_post):
        catalog = {"Data": [
            {"KeyNode": "K0", "KeyName": "PT03-temperature", "DevNode": "idospV1_1"},
            {"KeyNode": "K4", "KeyName": "Pump frequency", "DevNode": "idospV1_1"},
            {"KeyNode": "K7", "KeyName": "Manual-Auto", "DevNode": "idospV1_1", "WrEnable": 1},
        ]}
        mock_post.return_value = MagicMock(**{"json.return_value": catalog, "raise_for_status.return_value": None})
        collector = _make_collector()
        collector.token = "fake"
        assert collector.refresh_point_mapping() == {"K0": "pt03", "K4": "freq_pompe"}

    def test_no_writable_point_name_is_ever_in_the_mapping_table(self):
        """Règle de sécurité : la table ne contient aucun nom de point modifiable connu."""
        from collectors.gateway_collector import KEYNAME_TO_COLUMN
        writable_names = {
            "ON/OFF", "Temperature setpoint", "Emergency stop bypass", "Phase sequence bypass",
            "Manual-Auto", "AUTO REQUEST M4.2", "MANUAL PUMP REQUEST M4.3", "Maximum frequency",
            "Minimum frequency", "Pump downtime", "Clear alarm timers", "Alarm delay",
        }
        assert not writable_names & set(KEYNAME_TO_COLUMN)

    @patch("collectors.gateway_collector.requests.post")
    def test_two_points_for_the_same_column_are_reported(self, mock_post, caplog):
        catalog = {"Data": [
            {"KeyNode": "K4", "KeyName": "Pump frequency", "DevNode": "idospV1_1"},
            {"KeyNode": "K5", "KeyName": "PUMP", "DevNode": "idospV1_1"},
        ]}
        mock_post.return_value = MagicMock(**{"json.return_value": catalog, "raise_for_status.return_value": None})
        collector = _make_collector()
        collector.token = "fake"
        with caplog.at_level("WARNING"):
            collector.refresh_point_mapping()
        assert any("alimentée par deux points" in r.getMessage() for r in caplog.records)

    def test_refresh_point_mapping_requires_login_first(self):
        """Sans token, refresh_point_mapping doit lever RuntimeError avant tout appel réseau."""
        collector = _make_collector()
        with pytest.raises(RuntimeError, match="Non authentifié"):
            collector.refresh_point_mapping()

    @patch("collectors.gateway_collector.requests.post")
    def test_refresh_point_mapping_raises_if_no_recognized_points(self, mock_post):
        """Si aucun point reconnu n'est trouvé, lever une RuntimeError explicite."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"data": [{"KeyNode": "K9", "KeyName": "Truc", "DevNode": "idospV1_1"}]}
        mock_response.raise_for_status.return_value = None
        mock_post.return_value = mock_response

        collector = _make_collector()
        collector.token = "fake-token"
        with pytest.raises(RuntimeError, match="Aucun point reconnu"):
            collector.refresh_point_mapping()


@pytest.fixture
def temp_db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    yield path
    for suffix in ("", "-wal", "-shm"):
        candidate = path + suffix
        if os.path.exists(candidate):
            os.remove(candidate)


class TestOnWsMessage:
    """Tests unitaires de _on_ws_message() (contrat 5.3 / 6.3 point 4)."""

    def _collector_with_mapping(self):
        collector = _make_collector()
        collector.keynode_to_column = {
            "K0": "pt01",
            "K1": "pt02",
            "K2": "pt03",
            "K3": "pt04",
            "K4": "ht01",
        }
        return collector

    def test_updates_last_known_for_recognized_points(self):
        collector = self._collector_with_mapping()
        raw = json.dumps(
            {
                "Code": "IotData",
                "Data": {
                    "Node": "idospV1_1",
                    "Ts": 1788951620870,
                    "Point": [
                        {"K": "K0", "V": 27.8, "T": 1788951620855},
                        {"K": "K1", "V": 27.5, "T": 1788951620855},
                    ],
                },
            }
        )
        collector._on_ws_message(raw)
        assert collector.last_known["pt01"] == 27.8
        assert collector.last_known["pt02"] == 27.5

    def _message(self, key, value):
        return json.dumps({"Code": "IotData", "Data": {"Node": "idospV1_1", "Point": [{"K": key, "V": value}]}})

    def test_pump_at_30_hz_is_accepted(self):
        """Régression : l'ancienne plage 0-15 Hz rejetait une pompe à 30 Hz à chaque message."""
        collector = self._collector_with_mapping()
        collector.keynode_to_column["K5"] = "freq_pompe"
        collector._on_ws_message(self._message("K5", 30))
        assert collector.last_known["freq_pompe"] == 30.0

    def test_pump_above_50_hz_is_rejected(self):
        collector = self._collector_with_mapping()
        collector.keynode_to_column["K5"] = "freq_pompe"
        collector._on_ws_message(self._message("K5", 51))
        assert collector.last_known["freq_pompe"] is None

    def test_flow_in_range_is_accepted_and_above_15_is_rejected(self):
        collector = self._collector_with_mapping()
        collector._on_ws_message(self._message("K4", 10.4))
        assert collector.last_known["ht01"] == 10.4
        collector._on_ws_message(self._message("K4", 15.5))
        assert collector.last_known["ht01"] is None          # hors plage : rejetée, et l'ancienne valeur effacée

    def test_oil_overheating_is_not_discarded_as_out_of_range(self):
        """Régression de sécurité : 58 °C (> seuil critique) doit être MESURÉ, pas rejeté."""
        collector = self._collector_with_mapping()
        collector._on_ws_message(self._message("K1", 58.0))
        assert collector.last_known["pt02"] == 58.0

    def test_ignores_message_with_other_node(self):
        collector = self._collector_with_mapping()
        raw = json.dumps(
            {
                "Code": "IotData",
                "Data": {"Node": "Gateway", "Point": [{"K": "K0", "V": 99.9}]},
            }
        )
        collector._on_ws_message(raw)
        assert collector.last_known["pt01"] is None

    def test_ignores_non_iotdata_code(self):
        collector = self._collector_with_mapping()
        raw = json.dumps({"Code": "NetworkStatus", "Data": {"Node": "idospV1_1"}})
        # Ne doit pas lever d'exception, et ne rien mettre à jour.
        collector._on_ws_message(raw)
        assert all(v is None for v in collector.last_known.values())

    def test_ignores_unrecognized_keynode_without_error(self):
        collector = self._collector_with_mapping()
        raw = json.dumps(
            {
                "Code": "IotData",
                "Data": {"Node": "idospV1_1", "Point": [{"K": "K99", "V": 1.0}]},
            }
        )
        # Ne doit pas lever d'exception.
        collector._on_ws_message(raw)
        assert all(v is None for v in collector.last_known.values())

    def test_ignores_malformed_json_without_error(self):
        collector = self._collector_with_mapping()
        # Ne doit pas lever d'exception, juste logger un warning, sans toucher aux valeurs.
        collector._on_ws_message("{not valid json")
        assert all(value is None for value in collector.last_known.values())


class TestSnapshotOnce:
    """Tests unitaires de _snapshot_once() (contrat 6.3 point 5)."""

    def test_returns_false_when_all_values_none(self, temp_db_path):
        from database import sqlite_client

        sqlite_client.init_db(temp_db_path)
        collector = _make_collector()
        collector.db_path = temp_db_path

        inserted = collector._snapshot_once()

        assert inserted is False
        df = sqlite_client.get_dataframe("2000-01-01", "2100-01-01", db_path=temp_db_path)
        assert len(df) == 0

    def test_inserts_and_publishes_when_data_present(self, temp_db_path):
        from database import sqlite_client

        sqlite_client.init_db(temp_db_path)
        mock_mqtt = MagicMock()
        collector = _make_collector()
        collector.db_path = temp_db_path
        collector.mqtt_client = mock_mqtt
        collector.last_known["pt01"] = 24.3
        collector.last_known["pt02"] = 26.0

        inserted = collector._snapshot_once()

        assert inserted is True
        df = sqlite_client.get_dataframe("2000-01-01", "2100-01-01", db_path=temp_db_path)
        assert len(df) == 1
        assert df.iloc[0]["pt01"] == pytest.approx(24.3)
        mock_mqtt.publish.assert_called_once()
        call_args = mock_mqtt.publish.call_args
        assert call_args[0][0] == "voltaneum/telemetry"

    def test_mqtt_publish_failure_does_not_prevent_db_write(self, temp_db_path):
        """Une panne MQTT ne doit jamais faire perdre la donnée déjà écrite en base."""
        from database import sqlite_client

        sqlite_client.init_db(temp_db_path)
        mock_mqtt = MagicMock()
        mock_mqtt.publish.side_effect = RuntimeError("broker injoignable")
        collector = _make_collector()
        collector.db_path = temp_db_path
        collector.mqtt_client = mock_mqtt
        collector.last_known["pt01"] = 24.3

        # Ne doit pas lever d'exception malgré l'échec MQTT.
        inserted = collector._snapshot_once()

        assert inserted is True
        df = sqlite_client.get_dataframe("2000-01-01", "2100-01-01", db_path=temp_db_path)
        assert len(df) == 1


class TestSnapshotLoopWritesAtLeastOneLine:
    """Équivalent accéléré du test de sortie officiel de l'étape 5 :

    'après 60 s, au moins une ligne écrite dans telemetry'. On utilise ici
    un snapshot_interval_seconds très court (au lieu des 15 s réels) pour
    que le test s'exécute en une fraction de seconde plutôt que d'attendre
    60 secondes à chaque run de la suite — la boucle testée est exactement
    la même (_snapshot_loop), seule la durée d'attente change.
    """

    def test_snapshot_loop_writes_at_least_one_row(self, temp_db_path):
        from database import sqlite_client

        sqlite_client.init_db(temp_db_path)
        collector = _make_collector()
        collector.db_path = temp_db_path
        collector.snapshot_interval_seconds = 0.05
        collector.last_known["pt01"] = 24.3  # simule une valeur déjà reçue via WS

        stop_event = threading.Event()
        thread = threading.Thread(
            target=collector._snapshot_loop, args=(stop_event,), daemon=True
        )
        thread.start()
        time.sleep(0.3)
        stop_event.set()
        thread.join(timeout=2)

        df = sqlite_client.get_dataframe("2000-01-01", "2100-01-01", db_path=temp_db_path)
        assert len(df) >= 1


@requires_real_network
def test_login_real_gateway():
    """Test d'intégration réel (non exécuté en CI) : login retourne un token non vide."""
    collector = GatewayCollector()
    token = collector.login()
    assert token
    assert isinstance(token, str)


@requires_real_network
def test_run_forever_writes_row_after_60s(temp_db_path):
    """Après 60 s de run_forever() réel, au moins une ligne doit être écrite."""
    from database import sqlite_client

    sqlite_client.init_db(temp_db_path)
    collector = GatewayCollector(db_path=temp_db_path)

    thread = threading.Thread(target=collector.run_forever, daemon=True)
    thread.start()
    time.sleep(60)
    collector.stop()
    thread.join(timeout=10)

    df = sqlite_client.get_dataframe("2000-01-01", "2100-01-01", db_path=temp_db_path)
    assert len(df) >= 1

class TestMessageAge:
    """`seconds_since_last_message` : sert à ne pas publier de consigne sur des mesures périmées."""

    def test_none_before_any_message_is_expected(self):
        assert _make_collector().seconds_since_last_message() is None

    def test_grows_with_time_and_resets_on_a_message(self):
        collector = _make_collector()
        collector.keynode_to_column = {"K0": "pt01"}
        collector._last_message_at = time.monotonic() - 50
        assert 49 <= collector.seconds_since_last_message() <= 60
        collector._ws_on_message(None, json.dumps({"Code": "NetworkStatus"}))
        assert collector.seconds_since_last_message() < 5

    def test_public_silence_check_follows_the_configured_timeout(self, monkeypatch):
        import config
        monkeypatch.setattr(config, "WS_SILENCE_TIMEOUT_SECONDS", 10)
        collector = _make_collector()
        collector._last_message_at = time.monotonic() - 5
        assert collector.stream_is_silent() is False
        collector._last_message_at = time.monotonic() - 20
        assert collector.stream_is_silent() is True


class TestSensorFreshness:
    """Une mesure trop ancienne ou rejetée ne doit jamais passer pour actuelle."""

    @staticmethod
    def _collector_with_clock(monkeypatch, max_age=300):
        import config

        monkeypatch.setattr(config, "SENSOR_MAX_AGE_SECONDS", max_age)
        collector = _make_collector()
        collector.keynode_to_column = {"K0": "pt01", "K1": "pt02"}
        clock = {"t": 1000.0}
        collector._clock = lambda: clock["t"]
        return collector, clock

    @staticmethod
    def _message(*points):
        return json.dumps({"Code": "IotData", "Data": {
            "Node": "idospV1_1", "Point": [{"K": k, "V": v} for k, v in points]}})

    def test_value_expires_after_max_age(self, monkeypatch):
        collector, clock = self._collector_with_clock(monkeypatch)
        collector._on_ws_message(self._message(("K1", 30.0)))
        clock["t"] += 299
        assert collector.snapshot()["pt02"] == 30.0
        clock["t"] += 2
        assert collector.snapshot()["pt02"] is None
        assert collector.stale_columns() == ["pt02"]

    def test_one_silent_sensor_is_detected_while_others_keep_reporting(self, monkeypatch):
        collector, clock = self._collector_with_clock(monkeypatch)
        collector._on_ws_message(self._message(("K0", 25.0), ("K1", 30.0)))
        for _ in range(5):  # seul pt01 continue d'émettre
            clock["t"] += 100
            collector._on_ws_message(self._message(("K0", 25.0)))
        snapshot = collector.snapshot()
        assert snapshot["pt01"] == 25.0 and snapshot["pt02"] is None

    def test_rejected_value_clears_the_previous_one(self, monkeypatch):
        collector, _ = self._collector_with_clock(monkeypatch)
        collector._on_ws_message(self._message(("K1", 30.0)))
        collector._on_ws_message(self._message(("K1", 999.0)))  # hors plage : capteur défaillant
        assert collector.snapshot()["pt02"] is None
        assert collector.stale_columns() == []

    def test_fresh_value_after_staleness_is_used_again(self, monkeypatch):
        collector, clock = self._collector_with_clock(monkeypatch)
        collector._on_ws_message(self._message(("K1", 30.0)))
        clock["t"] += 1000
        collector._on_ws_message(self._message(("K1", 31.0)))
        assert collector.snapshot()["pt02"] == 31.0

    def test_zero_disables_expiry(self, monkeypatch):
        collector, clock = self._collector_with_clock(monkeypatch, max_age=0)
        collector._on_ws_message(self._message(("K1", 30.0)))
        clock["t"] += 10**6
        assert collector.snapshot()["pt02"] == 30.0

    def test_stale_values_are_not_written_to_database(self, monkeypatch, temp_db_path):
        from database import sqlite_client

        sqlite_client.init_db(temp_db_path)
        collector, clock = self._collector_with_clock(monkeypatch)
        collector.db_path = temp_db_path
        collector._on_ws_message(self._message(("K0", 25.0), ("K1", 30.0)))
        clock["t"] += 200
        collector._on_ws_message(self._message(("K0", 25.0)))
        clock["t"] += 200  # pt02 a 400 s, pt01 200 s
        assert collector._snapshot_once() is True
        df = sqlite_client.get_dataframe("2000-01-01", "2100-01-01", db_path=temp_db_path)
        assert df.iloc[0]["pt01"] == pytest.approx(25.0)
        assert df.iloc[0]["pt02"] is None or df.iloc[0]["pt02"] != df.iloc[0]["pt02"]  # NULL -> None/NaN


class TestOutdoorSourceRecorded:
    """La source de temp_outdoor ("sensor" / "weather") est enregistrée avec chaque instantané."""

    def test_source_written_to_database_and_mqtt(self, temp_db_path):
        from database import sqlite_client

        sqlite_client.init_db(temp_db_path)
        mock_mqtt = MagicMock()
        collector = _make_collector()
        collector.db_path = temp_db_path
        collector.mqtt_client = mock_mqtt
        collector.set_value("pt02", 30.0)
        collector.set_value("temp_outdoor", 12.0, source="weather")

        assert collector._snapshot_once() is True
        with sqlite_client.connect(temp_db_path) as conn:
            row = conn.execute("SELECT temp_outdoor, temp_outdoor_source FROM telemetry").fetchone()
        assert row == (12.0, "weather")
        payload = json.loads(mock_mqtt.publish.call_args[0][1])
        assert payload["temp_outdoor_source"] == "weather"

    def test_no_source_when_outdoor_temperature_is_missing(self, temp_db_path):
        from database import sqlite_client

        sqlite_client.init_db(temp_db_path)
        collector = _make_collector()
        collector.db_path = temp_db_path
        collector.set_value("pt02", 30.0)
        collector.set_value("temp_outdoor", None, source="weather")

        collector._snapshot_once()
        with sqlite_client.connect(temp_db_path) as conn:
            row = conn.execute("SELECT temp_outdoor, temp_outdoor_source FROM telemetry").fetchone()
        assert row == (None, None)


class TestOutdoorTemperatureAtEverySnapshot:
    """Chaque instantané (15 s) renseigne temp_outdoor via le fournisseur capteur / météo."""

    @staticmethod
    def _rows(db):
        from database import sqlite_client

        with sqlite_client.connect(db) as conn:
            return conn.execute("SELECT temp_outdoor, temp_outdoor_source FROM telemetry ORDER BY id").fetchall()

    def test_each_snapshot_gets_the_current_outdoor_temperature(self, temp_db_path):
        from database import sqlite_client

        sqlite_client.init_db(temp_db_path)
        readings = iter([(9.8, "weather"), (10.1, "weather"), (17.0, "sensor")])
        collector = GatewayCollector(username="u", password="p", db_path=temp_db_path,
                                     outdoor_temperature=lambda: next(readings))
        collector.set_value("pt02", 34.0)
        for _ in range(3):
            assert collector._snapshot_once() is True
        assert self._rows(temp_db_path) == [(9.8, "weather"), (10.1, "weather"), (17.0, "sensor")]

    def test_provider_failure_writes_the_row_without_outdoor_temperature(self, temp_db_path):
        from database import sqlite_client

        sqlite_client.init_db(temp_db_path)

        def broken():
            raise RuntimeError("panne inattendue")

        collector = GatewayCollector(username="u", password="p", db_path=temp_db_path,
                                     outdoor_temperature=broken)
        collector.set_value("pt02", 34.0)
        assert collector._snapshot_once() is True
        assert self._rows(temp_db_path) == [(None, None)]

    def test_no_row_with_only_outdoor_temperature(self, temp_db_path):
        """Tant que la passerelle n'a rien envoyé, aucune ligne « météo seule » n'est écrite."""
        from database import sqlite_client

        sqlite_client.init_db(temp_db_path)
        collector = GatewayCollector(username="u", password="p", db_path=temp_db_path,
                                     outdoor_temperature=lambda: (9.8, "weather"))
        collector.set_value("temp_outdoor", 9.8, source="weather")
        assert collector._snapshot_once() is False
        assert self._rows(temp_db_path) == []
