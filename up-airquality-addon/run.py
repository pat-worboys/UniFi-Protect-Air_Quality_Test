#!/usr/bin/env python3
"""UP-AirQuality test bridge for Home Assistant add-on use."""

from __future__ import annotations

import base64
import http.cookiejar
import json
import os
import ssl
import struct
import sys
import time
import urllib.request
import zlib
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
import websocket


OPTIONS_PATH = Path("/data/options.json")
BRIDGE_AVAILABILITY_TOPIC = "up_airquality/bridge/availability"

METRICS: dict[str, dict[str, Any]] = {
    "aqi": {"name": "AQI", "device_class": "aqi"},
    "airQuality": {"name": "Air Quality Index", "device_class": "aqi"},
    "vape": {"name": "Vape Index", "unit": "%"},
    "co2": {"name": "CO2", "device_class": "carbon_dioxide", "unit": "ppm"},
    "tvoc": {"name": "TVOC Index", "unit": "idx"},
    "voc": {"name": "VOC Index", "unit": "idx"},
    "nox": {"name": "NOx Index", "unit": "idx"},
    "pm1p0": {"name": "PM1.0", "device_class": "pm1", "unit": "µg/m³"},
    "pm2p5": {"name": "PM2.5", "device_class": "pm25", "unit": "µg/m³"},
    "pm4p0": {"name": "PM4.0", "unit": "µg/m³"},
    "pm10p0": {"name": "PM10", "device_class": "pm10", "unit": "µg/m³"},
    "temperature": {"name": "Temperature", "device_class": "temperature", "unit": "°C"},
    "humidity": {"name": "Humidity", "device_class": "humidity", "unit": "%"},
}


def load_options() -> dict[str, Any]:
    if OPTIONS_PATH.exists():
        with OPTIONS_PATH.open(encoding="utf-8") as file:
            return json.load(file)

    return {
        "protect_host": os.getenv("PROTECT_HOST", ""),
        "protect_user": os.getenv("PROTECT_USER", ""),
        "protect_pass": os.getenv("PROTECT_PASS", ""),
        "mqtt_host": os.getenv("MQTT_HOST", ""),
        "mqtt_port": int(os.getenv("MQTT_PORT", "1883")),
        "mqtt_user": os.getenv("MQTT_USER", ""),
        "mqtt_pass": os.getenv("MQTT_PASS", ""),
        "discovery_prefix": os.getenv("DISCOVERY_PREFIX", "homeassistant"),
        "publish_mqtt": os.getenv("PUBLISH_MQTT", "true").lower() == "true",
        "log_raw_airquality": os.getenv("LOG_RAW_AIRQUALITY", "true").lower() == "true",
        "verify_tls": os.getenv("VERIFY_TLS", "false").lower() == "true",
    }


def require(options: dict[str, Any], key: str) -> str:
    value = str(options.get(key) or "").strip()
    if not value:
        sys.exit(f"Missing required option: {key}")
    return value


def ssl_context(verify_tls: bool) -> ssl.SSLContext:
    context = ssl.create_default_context()
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


def csrf_from_token(token: str) -> str | None:
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        return json.loads(base64.urlsafe_b64decode(payload)).get("csrfToken")
    except Exception:
        return None


def login_and_bootstrap(
    host: str, user: str, password: str, verify_tls: bool
) -> tuple[str, str | None, dict[str, Any]]:
    cookie_jar = http.cookiejar.CookieJar()
    opener = urllib.request.build_opener(
        urllib.request.HTTPSHandler(context=ssl_context(verify_tls)),
        urllib.request.HTTPCookieProcessor(cookie_jar),
    )

    login_body = json.dumps({"username": user, "password": password}).encode()
    login_request = urllib.request.Request(
        f"https://{host}/api/auth/login",
        data=login_body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    login_response = opener.open(login_request, timeout=20)
    csrf = (
        login_response.headers.get("X-CSRF-Token")
        or login_response.headers.get("X-Csrf-Token")
    )
    login_response.read()

    token = next((cookie.value for cookie in cookie_jar if cookie.name == "TOKEN"), None)
    if not token:
        sys.exit("Login succeeded, but UniFi did not return a TOKEN cookie")
    csrf = csrf or csrf_from_token(token)

    bootstrap_request = urllib.request.Request(
        f"https://{host}/proxy/protect/api/bootstrap",
        headers={"Accept": "application/json"},
    )
    bootstrap = json.loads(opener.open(bootstrap_request, timeout=20).read())
    return token, csrf, bootstrap


def decode_packet(data: bytes) -> list[Any]:
    frames: list[Any] = []
    offset = 0
    while offset + 8 <= len(data):
        packet_format = data[offset + 1]
        deflated = data[offset + 2]
        size = struct.unpack(">I", data[offset + 4 : offset + 8])[0]
        payload = data[offset + 8 : offset + 8 + size]
        offset += 8 + size
        if deflated:
            payload = zlib.decompress(payload)
        frames.append(json.loads(payload) if packet_format == 1 else payload)
    return frames


def deep_merge(base: dict[str, Any], delta: dict[str, Any]) -> None:
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            deep_merge(base[key], value)
        else:
            base[key] = value


def slug(value: Any) -> str:
    return "".join(char for char in str(value).lower() if char.isalnum())


def entity_slug(value: Any) -> str:
    parts = []
    last_was_separator = True
    for char in str(value).lower():
        if char.isalnum():
            parts.append(char)
            last_was_separator = False
        elif not last_was_separator:
            parts.append("_")
            last_was_separator = True
    return "".join(parts).strip("_")


def sensor_slug(device: dict[str, Any]) -> str:
    for key in ("mac", "id", "name"):
        value = device.get(key)
        if value and (candidate := slug(value)):
            return candidate
    raise ValueError("UP-AirQuality sensor has no usable mac, id, or name")


def normalized_mac(device: dict[str, Any]) -> str | None:
    value = device.get("mac")
    if not value:
        return None
    compact = str(value).strip().lower().replace(":", "").replace("-", "").replace(".", "")
    if len(compact) != 12 or any(char not in "0123456789abcdef" for char in compact):
        return None
    return ":".join(compact[index : index + 2] for index in range(0, 12, 2))


def device_block(device: dict[str, Any], name: str) -> dict[str, Any]:
    block: dict[str, Any] = {
        "name": name,
        "manufacturer": "Ubiquiti",
        "model": device.get("type", "UP-AirQuality"),
        "sw_version": device.get("firmwareVersion"),
        "identifiers": [f"up_airquality_{sensor_slug(device)}"],
    }
    if mac := normalized_mac(device):
        block["connections"] = [["mac", mac]]
    return block


def airquality_sensors(bootstrap: dict[str, Any]) -> list[dict[str, Any]]:
    sensors = bootstrap.get("sensors", [])
    if isinstance(sensors, dict):
        sensors = sensors.get("data") or sensors.get("sensors") or []

    found = []
    for sensor in sensors:
        if sensor.get("type") != "UP-AirQuality":
            continue
        if isinstance(sensor.get("airQuality"), dict) and sensor["airQuality"]:
            found.append(sensor)
    return found


def metric_value(raw: Any) -> Any:
    return raw.get("value") if isinstance(raw, dict) else raw


def metric_status(raw: Any) -> Any:
    return raw.get("status") if isinstance(raw, dict) else None


def display_name(device: dict[str, Any], duplicate_names: set[str]) -> str:
    name = device.get("name") or "Protect Air Quality"
    if name not in duplicate_names:
        return name
    return f"{name} ({sensor_slug(device)[-6:]})"


def publish_discovery(
    client: mqtt.Client,
    prefix: str,
    device: dict[str, Any],
    name: str,
) -> tuple[str, str]:
    mac = sensor_slug(device)
    entity_base = entity_slug(name) or f"protect_air_quality_{mac}"
    node = f"protect_air_quality_{mac}"
    state_topic = f"up_airquality/{mac}/state"
    availability_topic = BRIDGE_AVAILABILITY_TOPIC
    dev = device_block(device, name)

    for key in sorted(device["airQuality"]):
        metadata = METRICS.get(key, {"name": key})
        object_id = f"{entity_base}_{key}"
        config: dict[str, Any] = {
            "name": metadata["name"],
            "unique_id": f"up_aq_{mac}_{key}",
            "object_id": object_id,
            "default_entity_id": f"sensor.{object_id}",
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{key} }}}}",
            "availability_topic": availability_topic,
            "state_class": "measurement",
            "device": dev,
        }
        if status := metric_status(device["airQuality"][key]):
            config["json_attributes_topic"] = state_topic
            config["json_attributes_template"] = (
                f"{{{{ {{'status': value_json.{key}_status}} | tojson }}}}"
            )
        if device_class := metadata.get("device_class"):
            config["device_class"] = device_class
        if unit := metadata.get("unit"):
            config["unit_of_measurement"] = unit

        client.publish(
            f"{prefix}/sensor/{node}/{key}/config",
            json.dumps(config),
            qos=1,
            retain=True,
        )

    print(
        f"MQTT Discovery entity IDs for {name}: "
        f"{', '.join(f'sensor.{entity_base}_{key}' for key in sorted(device['airQuality']))}",
        flush=True,
    )
    return state_topic, availability_topic


def publish_state(client: mqtt.Client | None, context: dict[str, Any]) -> None:
    payload = {}
    for key, raw in context["device"]["airQuality"].items():
        payload[key] = metric_value(raw)
        if (status := metric_status(raw)) is not None:
            payload[f"{key}_status"] = status

    print(f"UP-AirQuality {context['id']} readings: {json.dumps(payload, sort_keys=True)}", flush=True)
    if client:
        client.publish(context["state_topic"], json.dumps(payload), qos=0, retain=False)
        client.publish(context["availability_topic"], "online", qos=1, retain=True)


def mqtt_client(options: dict[str, Any]) -> mqtt.Client | None:
    if not options.get("publish_mqtt", True):
        print("MQTT publishing disabled; logging Protect readings only", flush=True)
        return None

    host = str(options.get("mqtt_host") or "").strip()
    if not host:
        print("No MQTT host configured; logging Protect readings only", flush=True)
        return None

    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
    user = str(options.get("mqtt_user") or "")
    password = str(options.get("mqtt_pass") or "")
    if user:
        client.username_pw_set(user, password)
    client.will_set(BRIDGE_AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
    client.connect(host, int(options.get("mqtt_port") or 1883), keepalive=60)
    client.loop_start()
    print(f"Connected to MQTT {host}:{options.get('mqtt_port') or 1883}", flush=True)
    return client


def build_contexts(
    client: mqtt.Client | None,
    prefix: str,
    sensors: list[dict[str, Any]],
    log_raw: bool,
) -> dict[str, dict[str, Any]]:
    name_counts: dict[str, int] = {}
    for device in sensors:
        name = device.get("name") or "Protect Air Quality"
        name_counts[name] = name_counts.get(name, 0) + 1
    duplicate_names = {name for name, count in name_counts.items() if count > 1}

    contexts = {}
    for device in sensors:
        sensor_id = device.get("id")
        if not sensor_id:
            print("Skipping UP-AirQuality sensor without id", flush=True)
            continue

        if log_raw:
            print(
                f"Raw airQuality for {device.get('name') or sensor_id}: "
                f"{json.dumps(device['airQuality'], sort_keys=True)}",
                flush=True,
            )

        name = display_name(device, duplicate_names)
        state_topic = availability_topic = ""
        if client:
            state_topic, availability_topic = publish_discovery(client, prefix, device, name)
        contexts[sensor_id] = {
            "id": sensor_id,
            "device": device,
            "state_topic": state_topic,
            "availability_topic": availability_topic,
        }
        publish_state(client, contexts[sensor_id])

    return contexts


def main() -> None:
    options = load_options()
    host = require(options, "protect_host")
    user = require(options, "protect_user")
    password = require(options, "protect_pass")
    prefix = str(options.get("discovery_prefix") or "homeassistant")
    verify_tls = bool(options.get("verify_tls", False))
    log_raw = bool(options.get("log_raw_airquality", True))
    client = mqtt_client(options)
    failures = 0

    while True:
        connected_at = None
        try:
            token, _csrf, bootstrap = login_and_bootstrap(host, user, password, verify_tls)
            sensors = airquality_sensors(bootstrap)
            if not sensors:
                print("No UP-AirQuality sensors with airQuality data found in bootstrap", flush=True)
                failures += 1
                time.sleep(min(5 * 2**failures, 300))
                continue

            last_update_id = bootstrap.get("lastUpdateId", "")
            print(
                f"Found {len(sensors)} UP-AirQuality sensor(s), lastUpdateId={last_update_id}",
                flush=True,
            )
            contexts = build_contexts(client, prefix, sensors, log_raw)

            def on_open(_ws: websocket.WebSocketApp) -> None:
                nonlocal connected_at
                connected_at = time.time()
                print("Protect WebSocket connected", flush=True)

            def on_message(_ws: websocket.WebSocketApp, message: Any) -> None:
                if not isinstance(message, bytes | bytearray):
                    return
                try:
                    frames = decode_packet(message)
                except Exception as err:
                    print(f"WebSocket decode error: {err}", flush=True)
                    return
                if len(frames) < 2:
                    return

                action, delta = frames[0], frames[1]
                if action.get("modelKey") != "sensor":
                    return
                context = contexts.get(action.get("id"))
                if not context:
                    return
                if "airQuality" not in delta:
                    return

                deep_merge(context["device"], delta)
                if log_raw:
                    print(
                        f"WebSocket airQuality delta for {context['id']}: "
                        f"{json.dumps(delta['airQuality'], sort_keys=True)}",
                        flush=True,
                    )
                publish_state(client, context)

            def on_error(_ws: websocket.WebSocketApp, err: Any) -> None:
                print(f"Protect WebSocket error: {err}", flush=True)

            def on_close(_ws: websocket.WebSocketApp, code: Any, msg: Any) -> None:
                print(f"Protect WebSocket closed: {code} {msg}", flush=True)

            ws_url = (
                f"wss://{host}/proxy/protect/ws/updates"
                f"?lastUpdateId={last_update_id}"
            )
            ws = websocket.WebSocketApp(
                ws_url,
                header=[f"Cookie: TOKEN={token}"],
                on_open=on_open,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
            )
            ws.run_forever(
                sslopt={"cert_reqs": ssl.CERT_REQUIRED if verify_tls else ssl.CERT_NONE},
                ping_interval=30,
            )
        except Exception as err:
            print(f"Bridge loop error: {err}", flush=True)
        finally:
            if client:
                try:
                    client.publish(BRIDGE_AVAILABILITY_TOPIC, "offline", qos=1, retain=True)
                except Exception as err:
                    print(f"Availability publish error: {err}", flush=True)

        if connected_at and time.time() - connected_at > 60:
            failures = 0
        else:
            failures += 1
        delay = min(5 * 2**failures, 300)
        print(f"Reconnecting in {delay}s", flush=True)
        time.sleep(delay)


if __name__ == "__main__":
    main()
