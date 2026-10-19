"""Tests de l'interface de supervision (dashboard/) : lectures en base et serveur HTTP.

Base temporaire, serveur lancé sur un port libre de la machine locale : rien ne touche la
vraie base ni le réseau.
"""

import json
import os
import sqlite3
import tempfile
import threading
import time
import urllib.error
import urllib.request

import pytest

from dashboard import data
from dashboard.server import make_server
from database import sqlite_client


def _utc(epoch):
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(epoch))


@pytest.fixture
def db_path():
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    sqlite_client.init_db(path)
    now = time.time()
    # 2 h de mesures toutes les 15 s, avec un trou de 10 min et une PT01 absente.
    for i in range(480):
        ts = now - 7200 + i * 15
        if now - 3600 < ts < now - 3000:
            continue
        sqlite_client.insert_reading(
            pt02=40.0, pt01=None if i == 100 else 34.0 + i * 0.01, pt03=39.0, pt04=30.0,
            ht01=2.5, freq_pompe=30.0, temp_outdoor=18.0, timestamp=_utc(ts), db_path=path,
            temp_outdoor_source="weather",
        )
    with sqlite_client.connect(path) as conn:
        for model, value in (("random_forest", 39.0), ("gradient_boosting", 39.4)):
            conn.execute(
                "INSERT INTO predictions_log (made_at, target_ts, horizon_seconds, model_name, predicted_value,"
                " actual_value, error) VALUES (?, ?, 600, ?, ?, ?, ?)",
                (_utc(now - 20), _utc(now + 580), model, value, None, None),
            )
            conn.execute(
                "INSERT INTO predictions_log (made_at, target_ts, horizon_seconds, model_name, predicted_value,"
                " actual_value, error) VALUES (?, ?, 600, ?, ?, ?, ?)",
                (_utc(now - 1800), _utc(now - 1200), model, 37.0, 37.5, -0.5),
            )
    for k, setpoint in enumerate((20.0, 45.5)):
        sqlite_client.log_control({
            "t_oil": 38.0, "t_target": 35.0, "t_predicted": 38.5, "t_outdoor": 18.0, "setpoint": setpoint,
            "mode": "feedforward" if k else "pid", "error": 3.0, "p_term": 15.0, "i_term": 15.5, "d_term": 0.0,
            "feedforward_prediction": 15.0 if k else 0.0, "feedforward_outdoor": 0.0, "raw_output": setpoint,
            "outdoor_band": "tempere",
            "mqtt_setpoint": json.dumps({"percent": setpoint, "mode": "pid", "ts": 1.0}) if k else None,
            "mqtt_sent": k,
        }, db_path=path)
    sqlite_client.log_alert("CRITIQUE", "critical_temp", "Huile à 56 °C", email_sent=True, db_path=path)
    sqlite_client.log_alert("ATTENTION", "model_disagreement", "Désaccord", email_sent=False, db_path=path)
    yield path
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(path + suffix):
            os.remove(path + suffix)


# ---------------------------------------------------------------------------- lectures
def test_database_is_opened_read_only(db_path):
    with data.connect_readonly(db_path) as conn:
        with pytest.raises(sqlite3.OperationalError):
            conn.execute("DELETE FROM telemetry")


def test_missing_database_is_reported_cleanly(tmp_path):
    with pytest.raises(data.DatabaseUnavailable):
        data.latest_snapshot(str(tmp_path / "absente.db"))


def test_latest_snapshot(db_path):
    snap = data.latest_snapshot(db_path)
    assert not snap["empty"]
    assert snap["values"]["pt01"] == pytest.approx(34.0 + 479 * 0.01)
    assert snap["age_seconds"] < 60
    assert snap["temp_outdoor_source"] == "weather"
    assert snap["target_trend_10min"] == pytest.approx(0.4, abs=0.02)   # 40 mesures × 0,01 °C
    # Conditions : même calcul que control/cooling_advice.py (bande tempérée à 18 °C).
    assert snap["cooling"]["outdoor_band"] == "tempere"
    preds = snap["predictions"]
    assert set(preds["models"]) == {"random_forest", "gradient_boosting"}
    assert preds["disagreement"] == pytest.approx(0.4)


def test_latest_control_cycle(db_path):
    control = data.latest_snapshot(db_path)["control"]
    assert control["setpoint"] == 45.5 and control["mode"] == "feedforward"
    assert control["p_term"] + control["i_term"] + control["d_term"] + control["feedforward_prediction"] \
        == pytest.approx(control["raw_output"])
    assert control["age_seconds"] < 60


def test_control_series(db_path):
    now = time.time()
    series = data.control_series(db_path, now - 3600, now + 60)
    assert series["setpoint"] and series["setpoint"][-1] == pytest.approx(32.75)   # même tranche : moyenne
    assert series["feedforward"][-1] == pytest.approx(0.5)


def test_database_without_control_log(tmp_path):
    """Base d'avant le journal de contrôle (main.py jamais lancé depuis) : pas d'erreur."""
    path = str(tmp_path / "ancienne.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE telemetry (id INTEGER PRIMARY KEY, timestamp DATETIME, pt01 REAL, pt02 REAL,"
                     " pt03 REAL, pt04 REAL, ht01 REAL, freq_pompe REAL, temp_outdoor REAL)")
        conn.execute("INSERT INTO telemetry (timestamp, pt02) VALUES (datetime('now'), 34.0)")
    assert data.latest_snapshot(path)["control"] is None
    assert data.control_series(path, time.time() - 600, time.time())["t"] == []


def test_control_history_and_summary(db_path):
    now = time.time()
    history = data.control_history(db_path, now - 3600, now + 60)
    assert [r["setpoint"] for r in history["items"]] == [45.5, 20.0]          # plus récent d'abord
    assert json.loads(history["items"][0]["mqtt_setpoint"])["percent"] == 45.5
    summary = history["summary"]
    assert (summary["cycles"], summary["sent"], summary["failed"]) == (2, 1, 1)
    assert summary["share_feedforward"] == pytest.approx(0.5)


def test_pid_settings_document_every_published_field():
    settings = data.pid_settings()
    assert [g["variable"] for g in settings["gains"]] == ["PID_KP", "PID_KI", "PID_KD"]
    topics = {t["key"]: t for t in settings["mqtt"]}
    assert topics["setpoint"]["topic"] == "voltaneum/control/setpoint"
    assert {f["name"] for f in topics["setpoint"]["fields"]} == {"percent", "pid_percent", "mode", "phase", "event", "ts"}
    from control.cooling_advice import compute_cooling_conditions
    published = set(compute_cooling_conditions(40.0, 15.0)) | {"t_outdoor_source", "phase", "event", "command_percent", "cooling_mode", "cooling_mode_reason", "ts"}
    assert {f["name"] for f in topics["conditions"]["fields"]} == published


def test_empty_database(tmp_path):
    path = str(tmp_path / "vide.db")
    sqlite_client.init_db(path)
    assert data.latest_snapshot(path)["empty"] is True


def test_series_are_bucketed_and_keep_extremes(db_path):
    now = time.time()
    full = data.telemetry_series(db_path, now - 7200, now, max_points=1000)
    assert full["bucket_seconds"] == 15
    assert full["count"] == len(full["t"])
    assert None in full["series"]["pt01"]                      # mesure absente conservée comme trou
    assert full["t"] == sorted(full["t"])
    coarse = data.telemetry_series(db_path, now - 7200, now, max_points=40)
    assert coarse["bucket_seconds"] in data._BUCKETS
    assert len(coarse["t"]) <= 41
    assert max(v for v in coarse["target_max"] if v is not None) == pytest.approx(34.0 + 479 * 0.01)
    assert coarse["count"] == full["count"]


def test_prediction_series_placed_at_target_time(db_path):
    now = time.time()
    preds = data.prediction_series(db_path, now - 7200, now + 3600)
    rf = preds["models"]["random_forest"]
    assert len(rf["t"]) == 2 and rf["t"][-1] > now * 1000       # la plus récente est dans le futur


def test_alerts_filter_and_counts(db_path):
    everything = data.alerts(db_path)
    assert [a["rule"] for a in everything["items"]] == ["model_disagreement", "critical_temp"]
    assert everything["counts_24h"] == {"CRITIQUE": 1, "ATTENTION": 1}
    critical = data.alerts(db_path, level="CRITIQUE")
    assert len(critical["items"]) == 1 and critical["items"][0]["email_sent"] is True


def test_model_stats(db_path):
    stats = data.model_stats(db_path)
    rows = {r["model"]: r for r in stats["live"]}
    assert rows["random_forest"]["rmse"] == pytest.approx(0.5)
    assert rows["random_forest"]["bias"] == pytest.approx(-0.5)


def test_model_stats_follow_horizons_present_in_database(db_path):
    """Un modèle installé par le ré-entraînement peut prédire à 30 min, hors de la liste de config."""
    with sqlite_client.connect(db_path) as conn:
        conn.execute("INSERT INTO predictions_log (made_at, target_ts, horizon_seconds, model_name, predicted_value,"
                     " actual_value, error) VALUES (datetime('now'), datetime('now'), 1800, 'random_forest', 35, 35.2, -0.2)")
    assert 1800 in {r["horizon_seconds"] for r in data.model_stats(db_path)["live"]}
    assert 1800 in sqlite_client.prediction_horizons(db_path)


def test_settings_never_expose_credentials():
    text = json.dumps(data.settings_summary(), ensure_ascii=False).lower()
    for secret in ("password", "mot de passe", "user", "smtp_host", "token"):
        assert secret not in text


# ---------------------------------------------------------------------------- serveur HTTP
@pytest.fixture
def server(db_path):
    srv = make_server("127.0.0.1", 0, db_path)
    thread = threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def _get(url, headers=None):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status, dict(resp.headers), resp.read()
    except urllib.error.HTTPError as err:
        return err.code, dict(err.headers), err.read()


def test_index_and_security_headers(server):
    status, headers, body = _get(server + "/")
    assert status == 200
    assert b"VOLTANEUM" in body
    assert "default-src 'none'" in headers["Content-Security-Policy"]
    assert headers["X-Content-Type-Options"] == "nosniff"


def test_static_files_are_a_closed_list(server):
    assert _get(server + "/static/app.js")[0] == 200
    for path in ("/static/../config.py", "/static/%2e%2e/config.py", "/.env", "/static/server.py"):
        assert _get(server + path)[0] == 404


def test_api_routes(server):
    status, _, body = _get(server + "/api/snapshot")
    assert status == 200 and json.loads(body)["values"]["pt02"] == 40.0
    status, _, body = _get(server + "/api/series?minutes=60&points=100")
    payload = json.loads(body)
    assert status == 200 and payload["t"] and "predictions" in payload
    status, _, body = _get(server + "/api/control?minutes=60")
    assert status == 200 and len(json.loads(body)["items"]) == 2
    now_ms = int(time.time() * 1000)
    status, _, body = _get(server + f"/api/control.csv?start={now_ms - 3_600_000}&end={now_ms + 60_000}")
    assert status == 200 and body.decode("utf-8-sig").splitlines()[0].startswith("date UTC;heure locale;PT01 (°C);cible (°C)")
    for route in ("/api/alerts", "/api/models", "/api/system", "/api/settings", "/api/pid", "/api/retrain", "/healthz"):
        assert _get(server + route)[0] == 200, route


def test_bad_parameters_are_rejected(server):
    assert _get(server + "/api/series?minutes=abc")[0] == 400
    assert _get(server + "/api/series?start=2000&end=1000")[0] == 400
    assert _get(server + "/api/series?minutes=99999999")[0] == 400


def test_csv_export(server):
    now_ms = int(time.time() * 1000)
    status, headers, body = _get(server + f"/api/export.csv?start={now_ms - 600_000}&end={now_ms}")
    assert status == 200
    assert "attachment" in headers["Content-Disposition"]
    text = body.decode("utf-8")
    assert text.startswith("\ufeff")                                  # BOM : accents corrects dans Excel
    lines = text.lstrip("\ufeff").strip().splitlines()
    assert lines[0].startswith("date UTC;heure locale;PT01")
    assert len(lines) > 30
    assert any("," in cell for cell in lines[-1].split(";")[2:])       # virgule décimale (Excel français)
    assert "." not in lines[-1].split(";")[3]                          # jamais de point décimal


def test_every_element_has_its_own_csv_export(server):
    now_ms = int(time.time() * 1000)
    period = f"start={now_ms - 7_200_000}&end={now_ms + 3_600_000}"
    status, _, body = _get(server + "/api/exports")
    names = [e["name"] for e in json.loads(body)]
    assert names == ["mesures", "consignes", "alertes", "predictions", "entrainements"]
    expected_header = {"mesures": "date UTC", "consignes": "date UTC", "alertes": "date UTC;heure locale;niveau",
                       "predictions": "prédite le (UTC)", "entrainements": "date (heure locale)"}
    for name in names:
        status, headers, body = _get(server + f"/api/export/{name}.csv?{period}")
        assert status == 200, name
        assert f"voltaneum-{name}-" in headers["Content-Disposition"]
        assert body.decode("utf-8-sig").startswith(expected_header[name]), name
    alerts_csv = _get(server + f"/api/export/alertes.csv?{period}")[2].decode("utf-8-sig")
    assert "critical_temp" in alerts_csv and "Huile à 56 °C" in alerts_csv
    assert _get(server + f"/api/export/inconnu.csv?{period}")[0] == 404


def test_foreign_host_header_is_refused(server):
    assert _get(server + "/api/snapshot", {"Host": "evil.example.com"})[0] == 403


def test_write_methods_are_not_supported(server):
    req = urllib.request.Request(server + "/api/snapshot", data=b"{}", method="POST")
    with pytest.raises(urllib.error.HTTPError) as err:
        urllib.request.urlopen(req, timeout=5)
    assert err.value.code == 501


def test_stream_sends_snapshot_immediately(server):
    with urllib.request.urlopen(server + "/api/stream", timeout=5) as resp:
        assert resp.headers["Content-Type"].startswith("text/event-stream")
        received = b""
        while b"event: snapshot" not in received:
            line = resp.readline()
            assert line, "flux fermé avant le premier instantané"
            received += line
        data_line = resp.readline()
    assert data_line.startswith(b"data: ")
    assert json.loads(data_line[6:])["values"]["pt03"] == 39.0
