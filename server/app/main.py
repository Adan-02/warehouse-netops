"""One process owns MQTT ingestion and serves the read-only warehouse dashboard."""

import logging
import os

from flask import Flask, jsonify, render_template, request
import paho.mqtt.client as mqtt
from waitress import serve

from .state import DEVICE_ID, TOPIC, TelemetryStore

LOG = logging.getLogger("warehouse")


def create_app(store):
    app = Flask(__name__)

    @app.after_request
    def response_headers(response):
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.get("/")
    def index():
        return render_template("index.html", device_id=DEVICE_ID,
                               demo_mode=os.environ.get("DEMO_MODE", "false").lower() in ("true", "1"))

    @app.get("/api/status")
    def status():
        result = store.status()
        result["demo_mode"] = os.environ.get("DEMO_MODE", "false").lower() in ("true", "1")
        return jsonify(result)

    @app.get("/api/history")
    def history():
        try:
            limit = int(request.args.get("limit", "120"))
        except ValueError:
            return jsonify(error="limit must be an integer from 1 to 240"), 400
        if not 1 <= limit <= 240:
            return jsonify(error="limit must be an integer from 1 to 240"), 400
        return jsonify(device_id=DEVICE_ID, readings=store.history(limit))

    @app.get("/healthz")
    def healthz():
        # HTTP liveness stays healthy during a broker fault so operators can diagnose it.
        return jsonify(ok=True, broker_connected=store.status()["broker_connected"])

    return app


def make_client(store):
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="warehouse-dashboard", clean_session=True)
    client.username_pw_set(os.environ.get("MQTT_USERNAME", "warehouse-dashboard"), os.environ["MQTT_PASSWORD"])

    def on_connect(connection, userdata, flags, reason_code, properties):
        if reason_code.is_failure:
            store.set_broker_connected(False)
            LOG.warning("MQTT connection rejected: %s", reason_code)
            return
        connection.subscribe(TOPIC, qos=1)

    def on_subscribe(connection, userdata, mid, reason_codes, properties):
        accepted = bool(reason_codes) and all(not result.is_failure for result in reason_codes)
        store.set_broker_connected(accepted)
        LOG.info("MQTT subscription active=%s", accepted)

    def on_disconnect(connection, userdata, disconnect_flags, reason_code, properties):
        store.set_broker_connected(False)
        LOG.warning("MQTT disconnected: %s", reason_code)

    def on_message(connection, userdata, message):
        if message.topic != TOPIC:
            return
        try:
            store.ingest(message.payload, retained=message.retain)
        except ValueError as error:
            LOG.warning("Ignored telemetry: %s", error)
        except Exception:
            LOG.exception("Telemetry persistence failed")

    client.on_connect = on_connect
    client.on_subscribe = on_subscribe
    client.on_disconnect = on_disconnect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=8)
    return client


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    store = TelemetryStore(os.environ.get("SQLITE_PATH", "/data/warehouse.sqlite3"),
                           int(os.environ.get("OFFLINE_AFTER_SECONDS", "20")))
    client = make_client(store)
    client.connect_async(os.environ.get("MQTT_HOST", "broker"), int(os.environ.get("MQTT_PORT", "1883")), keepalive=15)
    client.loop_start()
    try:
        serve(create_app(store), host="0.0.0.0", port=8080, threads=4)
    finally:
        client.disconnect()
        client.loop_stop()
        store.close()


if __name__ == "__main__":
    main()
