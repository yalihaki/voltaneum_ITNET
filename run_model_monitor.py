"""Service de surveillance des modèles prédictifs (voir ml/monitor.py).

Usage : python run_model_monitor.py   (arrêt propre sur Ctrl+C / SIGTERM)
"""

import logging
import signal
import threading

import config
from messaging.mqtt import connect_with_retry, create_client
from ml.monitor import run_monitor_loop


def main() -> None:
    logging.basicConfig(level=config.LOG_LEVEL, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    config.validate_config()
    stop_event = threading.Event()
    signal.signal(signal.SIGINT, lambda *_: stop_event.set())
    signal.signal(signal.SIGTERM, lambda *_: stop_event.set())

    client = create_client("voltaneum-model-monitor")
    if connect_with_retry(client, stop_event):
        client.loop_start()
        try:
            run_monitor_loop(mqtt_client=client, stop_event=stop_event)
        finally:
            client.loop_stop()
            client.disconnect()


if __name__ == "__main__":
    main()
