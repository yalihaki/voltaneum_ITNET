"""Configuration de test : les adresses n'ont plus de valeur par défaut dans le code (.env
uniquement). Les tests reçoivent ici des valeurs FICTIVES, posées avant l'import de config
(l'environnement prime sur .env, jamais lu pour ces variables pendant les tests)."""

import os

TEST_ENV = {
    "VOLTANEUM_USER": "test",
    "VOLTANEUM_PASSWORD": "test",  # nosec B105 — valeur fictive de test
    "MANAGE_API_URL": "http://passerelle.test/ManageApi/Base.Auth.php",
    "IOT_API_URL": "http://passerelle.test:8001/iot/query_all_points",
    "WS_URL": "ws://passerelle.test:91",
    "SENSOR_DRIVER_NODE": "idospV1_1",
    "MQTT_HOST": "localhost",
    "MQTT_PORT": "1883",
    "DASHBOARD_HOST": "127.0.0.1",
    "DASHBOARD_PORT": "8080",
    "SMTP_HOST": "",
    "SMTP_PORT": "587",
    "VOLTANEUM_LAT": "49.0364",
    "VOLTANEUM_LON": "2.0761",
}
for _name, _value in TEST_ENV.items():
    os.environ[_name] = _value
