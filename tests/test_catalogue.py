"""Tests du catalogue de l'automate : enregistrement, dernières valeurs, description et usage."""

import json
from unittest.mock import MagicMock, patch

import config
from collectors.catalogue import describe
from dashboard import data
from database import sqlite_client

NODE = config.SENSOR_DRIVER_NODE
POINTS = [
    {"KeyNode": "K0", "KeyName": "PT01", "DevNode": NODE, "WrEnable": 0, "DataType": "Float"},
    {"KeyNode": "K1", "KeyName": "PT02", "DevNode": NODE, "WrEnable": 0},
    {"KeyNode": "K12", "KeyName": "Temperature setpoint", "DevNode": NODE, "WrEnable": 1},
    {"KeyNode": "X9", "KeyName": "Inconnu", "DevNode": NODE, "WrEnable": 0, "Desc": "texte de la passerelle"},
    {"KeyNode": "K0", "KeyName": "PT01", "DevNode": "autre_appareil", "WrEnable": 0},
]


def test_catalogue_is_saved_with_last_values(tmp_path):
    db = str(tmp_path / "c.db")
    sqlite_client.init_db(db)
    sqlite_client.save_catalogue(POINTS, {"K0": "pt01", "K1": "pt02"}, NODE, db_path=db)
    sqlite_client.update_catalogue_values(NODE, {"K0": (33.4, 1_700_000_000.0), "K12": (35, 1_700_000_000.0)}, db_path=db)
    cat = data.catalogue(db, now=1_700_000_010.0)
    assert cat["source"] == "gateway" and cat["counts"] == {"total": 5, "used": 2, "writable": 1, "with_value": 2}
    first = cat["items"][0]
    assert (first["key_name"], first["column"], first["used"], first["last_value"]) == ("PT01", "pt01", True, "33.4")
    assert first["value_age_seconds"] == 10.0 and "RÉGULÉE" in first["usage"]
    setpoint = next(i for i in cat["items"] if i["key_node"] == "K12")
    assert setpoint["writable"] and not setpoint["used"] and setpoint["usage"] == "Non utilisé"
    other = next(i for i in cat["items"] if i["dev_node"] == "autre_appareil")
    assert not other["used"] and other["column"] is None
    assert next(i for i in cat["items"] if i["key_node"] == "X9")["description"] == "texte de la passerelle"
    # Nouveau relevé : les dernières valeurs connues sont conservées.
    sqlite_client.save_catalogue(POINTS, {"K0": "pt01", "K1": "pt02"}, NODE, db_path=db)
    assert data.catalogue(db)["items"][0]["last_value"] == "33.4"


def test_without_gateway_the_known_points_are_listed(tmp_path):
    db = str(tmp_path / "c.db")
    sqlite_client.init_db(db)
    cat = data.catalogue(db)
    assert cat["source"] == "reference" and cat["items"][0]["key_name"] == "PT01"
    assert {"ON/OFF", "Temperature setpoint"} <= {i["key_name"] for i in cat["items"] if not i["used"]}


def test_describe_never_marks_a_writable_point_as_used():
    assert describe("ON/OFF", None, True)["used"] is False
    assert describe("PT02", "pt02", False)["used"] is True


@patch("collectors.gateway_collector.requests.post")
def test_collector_saves_catalogue_and_raw_values(mock_post, tmp_path):
    from collectors.gateway_collector import GatewayCollector

    db = str(tmp_path / "g.db")
    sqlite_client.init_db(db)
    mock_post.return_value = MagicMock(**{"json.return_value": {"Data": POINTS}, "raise_for_status.return_value": None})
    collector = GatewayCollector(username="u", password="p", sensor_driver_node=NODE, db_path=db)  # nosec B106
    collector.token = "t"
    collector.refresh_point_mapping()
    collector._on_ws_message(json.dumps({"Code": "IotData", "Data": {"Node": NODE, "Point": [
        {"K": "K0", "V": 33.5}, {"K": "K12", "V": 35}]}}))
    with patch.object(collector, "_stream_is_silent", return_value=False):
        assert collector._snapshot_once()
    values = {i["key_node"]: i["last_value"] for i in data.catalogue(db)["items"] if i["dev_node"] == NODE}
    assert values["K0"] == "33.5" and values["K12"] == "35"     # point non utilisé : affiché, jamais exploité
