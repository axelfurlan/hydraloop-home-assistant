#!/usr/bin/env python3
"""Poll the Hydraloop External APIs and publish Home Assistant MQTT Discovery sensors."""
import json
import logging
import os
import signal
import sys
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import paho.mqtt.client as mqtt
import requests

log = logging.getLogger("hydraloop")

AVAIL_TOPIC = "hydraloop/status"
DEFAULT_LOCAL_URL = "https://hdm.hydraloop.com/api-local/external-api"
HTTP_TIMEOUT = 30
BYPASS_CONFIRM_TRIES = 20
BYPASS_CONFIRM_DELAY = 3


def env(name, default=None, required=False):
    value = os.environ.get(name, default)
    if required and not value:
        sys.exit(f"Missing required environment variable: {name}")
    return value


class Config:
    def __init__(self):
        self.api_key = env("HYDRALOOP_API_KEY", required=True)
        self.root_url = env("HYDRALOOP_ROOT_URL", "https://hdm.hydraloop.com/api-root/external-api").rstrip("/")
        # Optional override. By default each device uses the local server the root API reports for it.
        self.local_url = (env("HYDRALOOP_LOCAL_URL") or "").rstrip("/")
        self.device_id = env("HYDRALOOP_DEVICE_ID")
        self.allow_bypass = env("HYDRALOOP_ALLOW_BYPASS_CONTROL", "true").lower() in ("1", "true", "yes", "on")
        self.mqtt_host = env("MQTT_HOST", required=True)
        self.mqtt_port = int(env("MQTT_PORT", "1883"))
        self.mqtt_user = env("MQTT_USER")
        self.mqtt_password = env("MQTT_PASSWORD")
        self.poll_interval = int(env("POLL_INTERVAL", "600"))
        self.prefix = env("DISCOVERY_PREFIX", "homeassistant")
        self.tz = ZoneInfo(env("TZ", "UTC"))
        self.log_level = env("LOG_LEVEL", "INFO").upper()


class Api:
    def __init__(self, cfg):
        self.cfg = cfg
        self.session = requests.Session()
        self.session.headers["X-API-KEY"] = cfg.api_key
        self.local_bases = {}  # device id -> local API base, from localApiUrl

    def _request(self, method, url, **kwargs):
        resp = self.session.request(method, url, timeout=HTTP_TIMEOUT, **kwargs)
        resp.raise_for_status()
        return resp.json() if resp.content else None

    def list_devices(self):
        devices = self._request("GET", f"{self.cfg.root_url}/list-coupled-devices")
        for dev in devices:
            if dev.get("localApiUrl"):
                self.local_bases[dev["id"]] = dev["localApiUrl"].rstrip("/") + "/external-api"
        return devices

    def local_base(self, device_id):
        """Local servers differ per device (e.g. by region), so use the one the root API reports."""
        return self.cfg.local_url or self.local_bases.get(device_id) or DEFAULT_LOCAL_URL

    def local(self, path, **params):
        return self._request("GET", f"{self.local_base(params.get('deviceId'))}{path}", params=params)

    def set_bypass(self, device_id, activate):
        self._request("POST", f"{self.local_base(device_id)}/bypass-mode",
                      json={"deviceId": device_id, "activate": activate})


# ---------------------------------------------------------------- data fetching

def daily_totals(api, device_id, day):
    data = api.local("/recycled-water-by-month", deviceId=device_id, year=day.year, month=day.month)
    return {rec["x"][:10]: rec["y"] for rec in data.get("waterRecycled", [])}


_past_year_totals = {}  # (device_id, year) -> litres; completed years never change


def year_total(api, device_id, year):
    data = api.local("/recycled-water-by-year", deviceId=device_id, year=year)
    return sum(rec["y"] for rec in data.get("waterRecycled", []))


def lifetime_total(api, device_id, device, today, current_year_total):
    commissioned = (device.get("commissionDate") or "")[:4]
    first = int(commissioned) if commissioned.isdigit() else today.year - 10
    total = current_year_total
    for year in range(first, today.year):
        if (device_id, year) not in _past_year_totals:
            _past_year_totals[(device_id, year)] = year_total(api, device_id, year)
        total += _past_year_totals[(device_id, year)]
    return total


def fetch_water(api, device_id, device, today):
    yesterday = today - timedelta(days=1)
    month = daily_totals(api, device_id, today)
    prev = month if yesterday.month == today.month else daily_totals(api, device_id, yesterday)
    year = year_total(api, device_id, today.year)
    return {
        "today": month.get(today.isoformat(), 0),
        "yesterday": prev.get(yesterday.isoformat(), 0),
        "month": sum(month.values()),
        "year": year,
        "total": lifetime_total(api, device_id, device, today, year),
    }


def fetch_tanks(api, device_id):
    return api.local("/tank-levels", deviceId=device_id)


def fetch_bypass(api, device_id):
    return api.local("/bypass-mode", deviceId=device_id)


def first_firmware(dev):
    for key, value in dev.items():
        if "firmware" in key.lower() and isinstance(value, (str, int, float)) and value != "":
            return value
    return None


# ---------------------------------------------------------------- entity table
# Each row: key, component, group (data bucket that must be fetched), name, value(data) and HA extras.

def on_off(flag):
    return "ON" if flag else "OFF"


def status(data):
    return data["device"].get("deviceStatus") or {}


def issues(data):
    """Active faults, most severe first. The API documents strings but returns objects, so accept both."""
    found = []
    for severity, field in (("blocking", "blockingInfo"), ("major", "majorIssueInfo"), ("minor", "minorIssueInfo")):
        for item in status(data).get(field) or []:
            if isinstance(item, dict):
                found.append({"severity": severity, "code": item.get("publicIdentification"),
                              "issue": item.get("issue"), "created": item.get("created")})
            else:
                found.append({"severity": severity, "code": None, "issue": str(item), "created": None})
    return found


def tank(data, name, field):
    """Return a field of a tank reading, or None when the API has no data for that tank."""
    return (data["tanks"].get(name) or {}).get(field)


def tank_attrs(data, name):
    return {"last_reading": tank(data, name, "time")}


WATER = dict(unit_of_measurement="L", device_class="water", icon="mdi:water-sync")
TANK_LEVEL = dict(unit_of_measurement="%", state_class="measurement", icon="mdi:gauge")
TANK_VOLUME = dict(unit_of_measurement="L", device_class="volume_storage", state_class="measurement")

ENTITIES = [
    dict(key="recycled_today", component="sensor", group="water", name="Water recycled today",
         value=lambda d: d["water"]["today"], extra=dict(WATER, state_class="total_increasing")),
    dict(key="recycled_yesterday", component="sensor", group="water", name="Water recycled yesterday",
         value=lambda d: d["water"]["yesterday"], extra=dict(WATER)),
    dict(key="recycled_month", component="sensor", group="water", name="Water recycled this month",
         value=lambda d: d["water"]["month"], extra=dict(WATER, state_class="total_increasing")),
    dict(key="recycled_year", component="sensor", group="water", name="Water recycled this year",
         value=lambda d: d["water"]["year"], extra=dict(WATER, state_class="total_increasing")),
    dict(key="recycled_total", component="sensor", group="water", name="Water recycled total",
         value=lambda d: d["water"]["total"], extra=dict(WATER, state_class="total_increasing")),
    # default_entity_id: the Water Management dashboard tanks already point at these two ids.
    dict(key="tank2_level", component="sensor", group="tanks", name="Collected water level",
         value=lambda d: tank(d, "tank2", "percentage"),
         extra=dict(TANK_LEVEL, default_entity_id="sensor.hydraloop_collected_water")),
    dict(key="tank3_level", component="sensor", group="tanks", name="Reusable water level",
         value=lambda d: tank(d, "tank3", "percentage"),
         extra=dict(TANK_LEVEL, default_entity_id="sensor.hydraloop_reusable_water")),
    dict(key="tank2_liters", component="sensor", group="tanks", name="Collected water volume",
         value=lambda d: tank(d, "tank2", "liters"), attrs=lambda d: tank_attrs(d, "tank2"),
         extra=TANK_VOLUME),
    dict(key="tank3_liters", component="sensor", group="tanks", name="Reusable water volume",
         value=lambda d: tank(d, "tank3", "liters"), attrs=lambda d: tank_attrs(d, "tank3"),
         extra=TANK_VOLUME),
    dict(key="bypass", component="switch", group="bypass", name="Bypass",
         value=lambda d: on_off(d["bypass"].get("bypassActive")), extra=dict(icon="mdi:pipe-leak")),
    dict(key="bypass_minutes", component="sensor", group="bypass", name="Bypass minutes remaining",
         value=lambda d: d["bypass"].get("minutesRemaining", 0),
         extra=dict(unit_of_measurement="min", device_class="duration")),
    dict(key="online", component="binary_sensor", group="device", name="Online",
         value=lambda d: on_off(status(d).get("online")),
         extra=dict(device_class="connectivity", entity_category="diagnostic")),
    dict(key="state", component="sensor", group="device", name="State",
         value=lambda d: status(d).get("state"), extra=dict(entity_category="diagnostic")),
    dict(key="problem", component="binary_sensor", group="device", name="Problem",
         value=lambda d: on_off(any(status(d).get(f) for f in ("hasMinorIssue", "hasMajorIssue", "hasBlockingIssue"))),
         attrs=lambda d: {"issues": issues(d)},
         extra=dict(device_class="problem", entity_category="diagnostic")),
    dict(key="firmware", component="sensor", group="device", name="Firmware",
         value=lambda d: first_firmware(d["device"]), extra=dict(entity_category="diagnostic", icon="mdi:chip")),
    dict(key="serial", component="sensor", group="device", name="Serial",
         value=lambda d: d["device"].get("serial"), extra=dict(entity_category="diagnostic")),
    dict(key="commissioned", component="sensor", group="device", name="Commissioned",
         value=lambda d: d["device"].get("commissionDate"),
         extra=dict(device_class="timestamp", entity_category="diagnostic", icon="mdi:calendar-check")),
]


class Bridge:
    def __init__(self, cfg):
        self.cfg = cfg
        self.api = Api(cfg)
        self.devices = {}
        self.stop = threading.Event()
        self.client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hydraloop_mqtt")
        if cfg.mqtt_user:
            self.client.username_pw_set(cfg.mqtt_user, cfg.mqtt_password)
        self.client.will_set(AVAIL_TOPIC, "offline", retain=True)
        self.client.on_connect = self.on_connect
        self.client.on_connect_fail = lambda c, u: log.error(
            "MQTT connection to %s:%s failed, retrying", cfg.mqtt_host, cfg.mqtt_port)
        self.client.on_disconnect = lambda c, u, f, rc, p=None: log.warning("MQTT disconnected: %s", rc)
        self.client.on_message = self.on_message

    # ----- MQTT
    def state_topic(self, dev_id, key):
        return f"hydraloop/{dev_id}/{key}"

    def attrs_topic(self, dev_id, key):
        return f"{self.state_topic(dev_id, key)}/attributes"

    def config_topic(self, component, dev_id, key):
        return f"{self.cfg.prefix}/{component}/hydraloop_{dev_id}/{key}/config"

    def publish_discovery(self, dev_id, dev):
        serial = dev.get("serial") or dev_id
        device = {"identifiers": [f"hydraloop_{dev_id}"], "name": f"Hydraloop {serial}",
                  "manufacturer": "Hydraloop", "serial_number": str(serial)}
        for key in ("mac",):
            if dev.get(key):
                device["connections"] = [["mac", dev[key]]]
        fw = first_firmware(dev)
        if fw is not None:
            device["sw_version"] = str(fw)
        if dev.get("deviceName"):
            device["model"] = str(dev["deviceName"])
        if dev.get("hardwareVersion"):
            device["hw_version"] = str(dev["hardwareVersion"])

        for ent in ENTITIES:
            component = ent["component"]
            if ent["key"] == "bypass" and not self.cfg.allow_bypass:
                component = "binary_sensor"
            cfg = {
                "name": ent["name"], "has_entity_name": True,
                "unique_id": f"hydraloop_{dev_id}_{ent['key']}",
                "state_topic": self.state_topic(dev_id, ent["key"]),
                "availability_topic": AVAIL_TOPIC,
                "device": device,
                **ent["extra"],
            }
            if "attrs" in ent:
                cfg["json_attributes_topic"] = self.attrs_topic(dev_id, ent["key"])
            if component == "switch":
                cfg.update(command_topic=self.state_topic(dev_id, "bypass") + "/set",
                           payload_on="ON", payload_off="OFF", state_on="ON", state_off="OFF")
            self.client.publish(self.config_topic(component, dev_id, ent["key"]), json.dumps(cfg), retain=True)
            if ent["key"] == "bypass":  # clear the other component type if the setting changed
                other = "switch" if component == "binary_sensor" else "binary_sensor"
                self.client.publish(self.config_topic(other, dev_id, "bypass"), "", retain=True)

    def on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code.is_failure:
            log.error("MQTT connection refused: %s", reason_code)
            return
        log.info("Connected to MQTT broker %s:%s", self.cfg.mqtt_host, self.cfg.mqtt_port)
        for dev_id, dev in self.devices.items():
            self.publish_discovery(dev_id, dev)
        client.publish(AVAIL_TOPIC, "online", retain=True)
        if self.cfg.allow_bypass:
            client.subscribe("hydraloop/+/bypass/set")

    def on_message(self, client, userdata, msg):
        parts = msg.topic.split("/")
        payload = msg.payload.decode(errors="replace")
        if len(parts) != 4 or parts[1] not in self.devices or payload not in ("ON", "OFF"):
            log.warning("Ignoring unexpected command on %s: %r", msg.topic, payload[:20])
            return
        threading.Thread(target=self.set_bypass, args=(parts[1], payload == "ON"), daemon=True).start()

    def set_bypass(self, dev_id, activate):
        log.info("Setting bypass for %s to %s", dev_id, "ON" if activate else "OFF")
        try:
            self.api.set_bypass(dev_id, activate)
            # The backend lags behind the POST, so poll until it reports the new state.
            for _ in range(BYPASS_CONFIRM_TRIES):
                state = fetch_bypass(self.api, dev_id)
                if bool(state.get("bypassActive")) == activate:
                    break
                self.stop.wait(BYPASS_CONFIRM_DELAY)
            else:
                log.warning("Bypass for %s did not report %s within %ss",
                            dev_id, "ON" if activate else "OFF", BYPASS_CONFIRM_TRIES * BYPASS_CONFIRM_DELAY)
            self.publish_groups(dev_id, {"bypass": state})
        except Exception as exc:
            log.error("Bypass command failed for %s: %s", dev_id, exc)

    def publish_groups(self, dev_id, data):
        for ent in ENTITIES:
            if ent["group"] not in data:
                continue
            try:
                value = ent["value"](data)
                attrs = ent["attrs"](data) if "attrs" in ent else None
            except Exception as exc:
                log.warning("Could not compute %s: %s", ent["key"], exc)
                continue
            if value is None:
                continue
            self.client.publish(self.state_topic(dev_id, ent["key"]), str(value), retain=True)
            if attrs is not None:
                self.client.publish(self.attrs_topic(dev_id, ent["key"]), json.dumps(attrs), retain=True)

    # ----- polling
    def load_devices(self):
        delay = 5
        while not self.stop.is_set():
            try:
                devices = self.api.list_devices()
                break
            except Exception as exc:
                log.error("Could not list devices (retry in %ss): %s", delay, exc)
                self.stop.wait(delay)
                delay = min(delay * 2, 300)
        else:
            return False
        self.devices = {d["id"]: d for d in devices if not self.cfg.device_id or d["id"] == self.cfg.device_id}
        if not self.devices:
            log.error("No matching devices found")
            return False
        log.info("Found %d device(s)", len(self.devices))
        return True

    def poll(self):
        try:
            fresh = {d["id"]: d for d in self.api.list_devices()}
        except Exception as exc:
            log.warning("Device status poll failed: %s", exc)
            fresh = {}
        now = datetime.now(self.cfg.tz)
        for dev_id in self.devices:
            data = {}
            if dev_id in fresh:
                self.devices[dev_id] = data["device"] = fresh[dev_id]
            else:
                # No answer: the retained value would keep claiming the last known state. "None" means unknown in HA.
                self.client.publish(self.state_topic(dev_id, "online"), "None", retain=True)
            for group, fetch in (("water", lambda: fetch_water(self.api, dev_id, self.devices[dev_id], now.date())),
                                 ("tanks", lambda: fetch_tanks(self.api, dev_id)),
                                 ("bypass", lambda: fetch_bypass(self.api, dev_id))):
                try:
                    data[group] = fetch()
                except Exception as exc:
                    log.warning("Fetching %s failed for %s: %s", group, dev_id, exc)
            self.publish_groups(dev_id, data)

    def run(self):
        if not self.load_devices():
            return
        self.client.connect_async(self.cfg.mqtt_host, self.cfg.mqtt_port)
        self.client.loop_start()
        try:
            while not self.stop.is_set():
                self.poll()
                self.stop.wait(self.cfg.poll_interval)
        finally:
            if self.client.is_connected():
                self.client.publish(AVAIL_TOPIC, "offline", retain=True).wait_for_publish(timeout=5)
            self.client.loop_stop()
            self.client.disconnect()


def main():
    cfg = Config()
    logging.basicConfig(level=cfg.log_level, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    bridge = Bridge(cfg)
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: bridge.stop.set())
    bridge.run()


if __name__ == "__main__":
    main()
