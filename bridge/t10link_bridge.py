#!/usr/bin/env python3
"""
T10Link - Honeywell T9/T10 thermostat bridge for Savant (local HomeKit)
======================================================================
Talks to Honeywell Home T9/T10 (Pro) thermostats directly over the LAN with HomeKit
(HAP over IP, via aiohomekit) - no Honeywell/Resideo cloud - and serves them to the Savant
host over a plain TCP line protocol: one KEY=value line per state. Commands from Savant
come back the same way (SETMODE HEAT, SETHEAT 70, ...). One bridge serves up to 16
thermostats: "@2 SETHEAT 70" goes to thermostat 2 (no "@" = thermostat 1), and thermostat
n's states end in _n (ThermostatMode_2) or start with Tn_ (T2_MODE, T2_SENSOR1_TEMP).

    t10link_bridge.py --pair [N]    one-time HomeKit pairing (type the code the thermostat shows)
    t10link_bridge.py --dump [N]    print every HomeKit service/characteristic thermostat N exposes
    t10link_bridge.py --unpair [N]  remove this bridge's pairing from thermostat N
    t10link_bridge.py               run the service

Only the HomeKit long-term pairing keys are stored (pairing.json for thermostat 1,
pairing-N.json for thermostat N; service user only).
The setup code is used once and never saved.

HomeKit on the T9/T10 exposes: mode, heating/cooling state, temperature, heat and cool
setpoints, humidity, display units, occupancy, filter reminders and the RedLINK room
sensors (temperature, humidity, motion). It does NOT expose fan mode or hold.

Not affiliated with or endorsed by Honeywell, Resideo or Savant Systems, Inc.
"""
from __future__ import annotations

import argparse
import asyncio
import getpass
import ipaddress
import json
import logging
import logging.handlers
import os
import re
import signal
import socket
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import hap_local  # noqa: E402

VERSION = "1.3.0"
log = logging.getLogger("t10link")
SAVANT_ENCODING = "ascii"

DEFAULT_CONFIG = {
    "tcp_port": 2400,                 # Savant connection (the profile expects 2400)
    # One entry per thermostat; the list position is its ThermostatAddress in Blueprint (1, 2, ...).
    # Each: {"ip": ..., "port": 80, "sensor_order": [...]}. Give every thermostat a DHCP reservation.
    # Older configs with thermostat_ip / thermostat_port / sensor_order still work (one thermostat).
    "thermostats": [],
    "thermostat_ip": "192.168.1.50",
    "thermostat_port": 80,
    "max_sensors": 4,                 # room-sensor slots (per thermostat) the profile has states for
    "sensor_order": [],               # optional: fix sensor numbering by name, e.g. ["Master Bedroom Air Sensor 01"]
    "units": "auto",                  # auto = the thermostat's display units; or "F" / "C"
    "poll_seconds": 60,               # safety poll; HomeKit events cover normal changes
    "confirm_seconds": 20,            # how long to wait for a change to be confirmed
    "offline_grace_seconds": 120,     # Wi-Fi power-save blips shorter than this aren't shown
    "log_level": "INFO",
    # Web page for status, pairing and adding thermostats: http://<bridge>:web_port (0 = off).
    # web_clients: "lan" = private network addresses (10.x, 172.16-31.x, 192.168.x), "any", or a
    # list of IPs / networks; allowed_clients may always open it. web_password: optional (any user name).
    "web_port": 2401,
    "web_clients": "lan",
    "web_password": "",
    # Who may connect on tcp_port. Set here only, never from Blueprint (the Savant connection is
    # unauthenticated). "auto" = this machine; add the Savant host's IP when the bridge runs elsewhere.
    "allowed_clients": "auto",
}

# HomeKit types (short form of 0000XXXX-0000-1000-8000-0026BB765291)
S_INFO, S_THERMO, S_FILTER, S_OCC, S_TEMP, S_HUM, S_MOTION = "3E", "4A", "BA", "86", "8A", "82", "85"
C_NAME, C_MODEL, C_SERIAL, C_FW, C_MFR = "23", "21", "30", "52", "20"
C_CUR_STATE, C_TGT_STATE, C_CUR_TEMP, C_TGT_TEMP, C_UNITS = "F", "33", "11", "35", "36"
C_HUM, C_COOL_TH, C_HEAT_TH = "10", "D", "12"
C_FILTER_CHANGE, C_FILTER_RESET, C_OCCUPIED, C_MOTION = "AC", "AD", "71", "22"
C_LOW_BATT, C_FAULT = "79", "77"

MODES = {0: "Off", 1: "Heat", 2: "Cool", 3: "Auto"}
MODE_CODES = {v.upper(): k for k, v in MODES.items()}
HVAC_STATES = {0: "Idle", 1: "Heating", 2: "Cooling"}


def short_type(t: str) -> str:
    t = str(t).upper()
    if t.endswith("-0000-1000-8000-0026BB765291"):
        return t.split("-")[0].lstrip("0") or "0"
    return t


# ---------------------------------------------------------------------------
# State definitions. generate_profile.py builds the Blueprint status parsers from
# these, so the profile always matches what the bridge sends. (key, savant type, description)
# ---------------------------------------------------------------------------
SYS_STATES = [
    ("SYS_ONLINE", "integer", "1 = bridge has a live HomeKit session with every thermostat (per thermostat: T_ONLINE)"),
    ("SYS_PAIRED", "integer", "1 = every thermostat is paired. 0 = run: t10link pair (per thermostat: T_PAIRED)"),
    ("SYS_LIVE", "integer", "1 = HomeKit push events subscribed on every thermostat (0 = polling only)"),
    ("SYS_LAST_UPDATE", "string", "Time of the last value read from the thermostat"),
    ("SYS_LAST_COMMAND", "string", "Last command from Savant and its result"),
    ("SYS_ERROR", "string", "Bridge errors, blank when healthy (with several thermostats: \"thermostat 2: ...\")"),
    ("SYS_VERSION", "string", "Bridge version"),
    ("SYS_REJECTED_CLIENT", "string", "Last connection refused by allowed_clients: IP and time"),
    ("SYS_SENSOR_COUNT", "integer", "Room sensors found, all thermostats"),
    ("SYS_THERMOSTAT_COUNT", "integer", "Thermostats in config.json"),
]

T_STATES = [
    ("T_NAME", "string", "Thermostat name (HomeKit)"),
    ("T_MODEL", "string", "Model"),
    ("T_FIRMWARE", "string", "Firmware version"),
    ("T_IP", "string", "Thermostat IP address (from config.json)"),
    ("T_PAIRED", "integer", "1 = this thermostat is paired with the bridge"),
    ("T_ONLINE", "integer", "1 = thermostat reachable (0 after offline_grace_seconds unreachable)"),
    ("T_MODE", "string", "Off | Heat | Cool | Auto (Unknown while offline)"),
    ("T_STATE", "string", "Idle | Heating | Cooling (Unknown while offline)"),
    ("T_TEMP", "string", "Current temperature in T_UNITS (kept while offline)"),
    ("T_HEAT_SETPOINT", "string", "Heat setpoint in T_UNITS"),
    ("T_COOL_SETPOINT", "string", "Cool setpoint in T_UNITS"),
    ("T_TARGET", "string", "Active target temperature (Heat or Cool mode), in T_UNITS"),
    ("T_HUMIDITY", "integer", "Indoor humidity, percent"),
    ("T_UNITS", "string", "F or C (the units every temperature state uses)"),
    ("T_SETPOINT_MIN", "string", "Lowest allowed setpoint, in T_UNITS"),
    ("T_SETPOINT_MAX", "string", "Highest allowed setpoint, in T_UNITS"),
    ("T_OCCUPIED", "integer", "1 = thermostat's own occupancy sensor sees someone"),
    ("T_FILTER1_CHANGE", "integer", "1 = filter 1 reminder is due"),
    ("T_FILTER2_CHANGE", "integer", "1 = filter 2 reminder is due"),
    ("T_SENSOR_COUNT", "integer", "Room sensors found on this thermostat"),
    ("T_PENDING", "string", "What is being changed while a command is being confirmed, else blank"),
    ("T_SAVED_SETPOINTS", "string", "Setpoints saved for another mode, applied when the thermostat switches to it (e.g. heat 68)"),
    ("T_ERROR", "string", "Last command error, blank when fine"),
    ("T_LAST_CHANGE", "string", "Time the mode or a setpoint last changed"),
]

SENSOR_FIELDS = [
    ("PRESENT", "integer", "1 = a room sensor is assigned to this number"),
    ("NAME", "string", "Sensor name (HomeKit)"),
    ("TEMP", "string", "Temperature in T_UNITS"),
    ("HUMIDITY", "integer", "Humidity, percent"),
    ("MOTION", "integer", "1 = motion detected"),
    ("LOW_BATTERY", "integer", "1 = battery low"),
    ("FAULT", "integer", "1 = sensor reports a fault"),
]

# Savant's own HVAC state names (as in Savant's Honeywell T-Series and Venstar profiles), for the
# Savant app's climate screen. The suffix is the ThermostatAddress: _1 here, _n for thermostat n.
SAVANT_STATES = [
    ("ThermostatCurrentTemperature_1", "string", "Current temperature"),
    ("ThermostatCurrentHeatPoint_1", "string", "Heat setpoint"),
    ("ThermostatCurrentCoolPoint_1", "string", "Cool setpoint"),
    ("ThermostatCurrentHumidity_1", "string", "Indoor humidity"),
    ("ThermostatMode_1", "string", "Off / Heat / Cool / Auto"),
    ("ThermostatCurrentSetPoint_1", "string", "Active setpoint in Heat / Cool mode (blank in Auto / Off). Blueprint enables the HVAC data table's Auto column only for a profile that has this state"),
    ("CurrentChangeOverStatus_1", "string", "true in Auto (auto changeover), as in Savant's Honeywell T-Series profile"),
    ("IsCurrentHVACModeOff_1", "boolean", ""),
    ("IsCurrentHVACModeHeat_1", "boolean", ""),
    ("IsCurrentHVACModeCool_1", "boolean", ""),
    ("IsCurrentHVACModeAuto_1", "boolean", ""),
    ("IsCurrentHVACStateIdle_1", "boolean", ""),
    ("IsCurrentHVACStateHeating_1", "boolean", ""),
    ("IsCurrentHVACStateCooling_1", "boolean", ""),
    ("IsW1RelayEnergized_1", "boolean", "true while heating"),
    ("IsY1RelayEnergized_1", "boolean", "true while cooling"),
    ("ThermostatTemperatureFormat_1", "string", "F or C"),
    ("IsThermostatFormatFahrenheit_1", "boolean", ""),
    ("IsThermostatFormatCelsius_1", "boolean", ""),
    ("ThermostatCurrentHeatPointMinimum_1", "string", ""),
    ("ThermostatCurrentHeatPointMaximum_1", "string", ""),
    ("ThermostatCurrentCoolPointMinimum_1", "string", ""),
    ("ThermostatCurrentCoolPointMaximum_1", "string", ""),
    ("ThermostatCurrentRemoteTemperature_1", "string", "Room sensor 1 temperature"),
    ("ThermostatCurrentRemoteHumidity_1", "string", "Room sensor 1 humidity"),
]


MAX_THERMOSTATS = 16


def sensor_key(n, field):
    return f"SENSOR{n}_{field}"


def key_for(n, key):
    """Thermostat 1 keeps the plain names; thermostat n: T_MODE -> Tn_MODE, SENSOR1_TEMP ->
    Tn_SENSOR1_TEMP, ThermostatMode_1 -> ThermostatMode_n (Savant's address suffix)."""
    if n == 1:
        return key
    if key.startswith("T_"):
        return f"T{n}_" + key[2:]
    if key.startswith("SENSOR"):
        return f"T{n}_{key}"
    if key.endswith("_1"):
        return f"{key[:-2]}_{n}"
    return key


def state_defs(max_sensors, thermostats=1):
    defs = list(SYS_STATES)
    for t in range(1, thermostats + 1):
        per = list(T_STATES)
        for n in range(1, max_sensors + 1):
            per += [(sensor_key(n, f), ty, d) for f, ty, d in SENSOR_FIELDS]
        per += list(SAVANT_STATES)
        defs += [(key_for(t, k), ty, d if t == 1 else f"Thermostat {t}: {d}" if d else "") for k, ty, d in per]
    return defs


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def now_text():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def ascii_text(v, limit=200):
    s = "" if v is None else str(v)
    s = s.replace("\r", " ").replace("\n", " ")
    return s.encode("ascii", "replace").decode("ascii")[:limit]


def c_to_unit(c, units):
    if c is None:
        return None
    if units == "F":
        return int(round(float(c) * 9 / 5 + 32))
    v = round(float(c) * 2) / 2
    return int(v) if v == int(v) else v


def unit_to_c(v, units):
    """Savant value -> HomeKit Celsius on the 0.5 C grid. Integer F round-trips exactly."""
    """Savant value -> HomeKit Celsius (0.5 C grid) that the thermostat will turn into exactly v.
    Measured on a T10 (firmware 1.5.3): it converts a written Celsius value to F and rounds UP
    (23.0 C = 73.4 F became 74 F; 22.0 C = 71.6 F stayed 72 F). So for a whole F pick the
    largest 0.5 C step whose F value is in (v-1, v]; that window (0.56 C) always holds one."""
    import math
    v = float(v)
    if units != "F":
        return round(v * 2) / 2
    hi = (v - 32) * 5 / 9
    c = math.floor(hi * 2 + 1e-9) / 2
    if math.ceil(c * 9 / 5 + 32 - 1e-9) != round(v):     # defensive: never expected
        c = round(hi * 2) / 2
    return c


def fmt(v):
    return "" if v is None else str(v)


def parse_allowed(value):
    items = value if isinstance(value, list) else [value]
    allow_any, allow_local, nets = False, False, []
    for raw in items:
        item = str(raw).strip().lower()
        if item in ("any", "*", "all"):
            allow_any = True
        elif item in ("auto", "local", "self", ""):
            allow_local = True
        else:
            nets.append(ipaddress.ip_network(item, strict=False))
    return allow_any, allow_local, nets


def is_this_machine(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
        if addr.is_loopback:
            return True
        fam = socket.AF_INET6 if addr.version == 6 else socket.AF_INET
        with socket.socket(fam, socket.SOCK_STREAM) as s:
            s.bind((ip, 0))
        return True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------
# HomeKit accessory model -> roles
# ---------------------------------------------------------------------------
class Model:
    """Where each value lives (aid, iid), built from the /accessories database."""

    def __init__(self, accessories, sensor_order=(), saved_numbers=None):
        self.thermo = {}          # role -> (aid, iid)
        self.meta = {}            # (aid, iid) -> characteristic dict (min/max/perms)
        self.info = {}            # thermostat name/model/firmware
        self.filters = []         # [(change_iid, reset_iid)] in order, on the thermostat aid
        self.sensors = []         # [{"serial", "name", roles...}]
        self.values = {}          # (aid, iid) -> value
        thermo_aid = None
        for acc in accessories:
            aid = acc["aid"]
            for svc in acc["services"]:
                for ch in svc["characteristics"]:
                    self.meta[(aid, ch["iid"])] = ch
                    if "value" in ch:
                        self.values[(aid, ch["iid"])] = ch["value"]
                if short_type(svc["type"]) == S_THERMO:
                    thermo_aid = aid
        if thermo_aid is None:
            raise ValueError("no HomeKit Thermostat service found")
        self.thermo_aid = thermo_aid
        for acc in accessories:
            aid = acc["aid"]
            info = self._chars(acc, S_INFO)
            if aid == thermo_aid:
                for svc in acc["services"]:
                    st = short_type(svc["type"])
                    chars = {short_type(c["type"]): c["iid"] for c in svc["characteristics"]}
                    if st == S_THERMO:
                        for role in (C_CUR_STATE, C_TGT_STATE, C_CUR_TEMP, C_TGT_TEMP, C_UNITS, C_HUM, C_COOL_TH, C_HEAT_TH):
                            if role in chars:
                                self.thermo[role] = (aid, chars[role])
                    elif st == S_FILTER and C_FILTER_CHANGE in chars:
                        self.filters.append(((aid, chars[C_FILTER_CHANGE]),
                                             (aid, chars[C_FILTER_RESET]) if C_FILTER_RESET in chars else None))
                    elif st == S_OCC and C_OCCUPIED in chars:
                        self.thermo["occupied"] = (aid, chars[C_OCCUPIED])
                self.info = {k: self.values.get((aid, info.get(c))) for k, c in
                             (("name", C_NAME), ("model", C_MODEL), ("firmware", C_FW))}
                # the accessory's own Name is "T10 Thermostat"; prefer the thermostat service name
                continue
            sensor = {"aid": aid, "serial": str(self.values.get((aid, info.get(C_SERIAL))) or aid),
                      "name": str(self.values.get((aid, info.get(C_NAME))) or f"Sensor {aid}")}
            for svc in acc["services"]:
                st = short_type(svc["type"])
                chars = {short_type(c["type"]): c["iid"] for c in svc["characteristics"]}
                if st == S_TEMP:
                    sensor["temp"] = (aid, chars.get(C_CUR_TEMP))
                    if C_LOW_BATT in chars:
                        sensor["low_battery"] = (aid, chars[C_LOW_BATT])
                    if C_FAULT in chars:
                        sensor["fault"] = (aid, chars[C_FAULT])
                elif st == S_HUM:
                    sensor["humidity"] = (aid, chars.get(C_HUM))
                elif st == S_MOTION:
                    sensor["motion"] = (aid, chars.get(C_MOTION))
                elif st == S_OCC:
                    sensor["motion"] = (aid, chars.get(C_OCCUPIED))
            if any(k in sensor for k in ("temp", "humidity", "motion")):
                self.sensors.append(sensor)
        self.number_sensors(sensor_order, saved_numbers or {})

    @staticmethod
    def _chars(acc, stype):
        for svc in acc["services"]:
            if short_type(svc["type"]) == stype:
                return {short_type(c["type"]): c["iid"] for c in svc["characteristics"]}
        return {}

    def number_sensors(self, order, saved):
        """Stable numbering: config sensor_order, then the saved map (serial -> n), then next free."""
        self.numbers = {}
        used = set()
        by_name = {s["name"].lower(): s for s in self.sensors}
        for i, name in enumerate(order or [], 1):
            s = by_name.get(str(name).lower())
            if s and s["serial"] not in self.numbers:
                self.numbers[s["serial"]] = i
                used.add(i)
        for s in self.sensors:
            n = saved.get(s["serial"])
            if s["serial"] not in self.numbers and isinstance(n, int) and n not in used:
                self.numbers[s["serial"]] = n
                used.add(n)
        for s in self.sensors:
            if s["serial"] not in self.numbers:
                n = 1
                while n in used:
                    n += 1
                self.numbers[s["serial"]] = n
                used.add(n)

    def watched(self):
        """Every (aid, iid) worth reading or subscribing to."""
        keys = set(self.thermo.values())
        for ch, _ in self.filters:
            keys.add(ch)
        for s in self.sensors:
            keys.update(v for k, v in s.items() if isinstance(v, tuple) and v[1] is not None)
        return {k for k in keys if k and "pr" in self.meta.get(k, {}).get("perms", ["pr"])}

    def evented(self):
        return {k for k in self.watched() if "ev" in self.meta.get(k, {}).get("perms", [])}

    def v(self, key):
        return self.values.get(key) if key else None

    def t(self, role):
        return self.v(self.thermo.get(role))

    def units(self, pref="auto"):
        p = str(pref or "auto").upper()
        if p in ("F", "C"):
            return p
        return "F" if self.t(C_UNITS) == 1 else "C"

    def limits(self, role):
        m = self.meta.get(self.thermo.get(role), {})
        return m.get("minValue", 10), m.get("maxValue", 32)


def compute_states(m: Model, units_pref="auto", online=True, max_sensors=4) -> dict:
    """Pure: the model's current values -> every KEY=value the bridge publishes."""
    u = m.units(units_pref)
    mode_code, state_code = m.t(C_TGT_STATE), m.t(C_CUR_STATE)
    mode = MODES.get(mode_code, "Unknown") if online else "Unknown"
    hstate = HVAC_STATES.get(state_code, "Unknown") if online else "Unknown"
    temp = c_to_unit(m.t(C_CUR_TEMP), u)
    heat = c_to_unit(m.t(C_HEAT_TH), u)
    cool = c_to_unit(m.t(C_COOL_TH), u)
    target = c_to_unit(m.t(C_TGT_TEMP), u)
    # Measured on the T10: in Heat/Cool mode the real setpoint is TargetTemperature; the
    # Heating/Cooling thresholds keep the Auto-mode values (writes to them are reverted).
    raw_mode = MODES.get(mode_code)
    if raw_mode == "Heat" and target is not None:
        heat = target
    if raw_mode == "Cool" and target is not None:
        cool = target
    lo, hi = m.limits(C_HEAT_TH if C_HEAT_TH in m.thermo else C_TGT_TEMP)
    lo, hi = c_to_unit(lo, u), c_to_unit(hi, u)
    hum = m.t(C_HUM)
    out = {
        "T_NAME": m.info.get("name") or "", "T_MODEL": m.info.get("model") or "",
        "T_FIRMWARE": m.info.get("firmware") or "", "T_ONLINE": 1 if online else 0,
        "T_MODE": mode, "T_STATE": hstate, "T_TEMP": fmt(temp), "T_HEAT_SETPOINT": fmt(heat),
        "T_COOL_SETPOINT": fmt(cool), "T_TARGET": fmt(target if mode in ("Heat", "Cool") else ""),
        "T_HUMIDITY": "" if hum is None else int(hum), "T_UNITS": u,
        "T_SETPOINT_MIN": fmt(lo), "T_SETPOINT_MAX": fmt(hi),
        "T_OCCUPIED": 1 if m.t("occupied") else 0,
    }
    for i in (1, 2):
        out[f"T_FILTER{i}_CHANGE"] = 1 if (len(m.filters) >= i and m.v(m.filters[i - 1][0])) else 0
    by_num = {m.numbers[s["serial"]]: s for s in m.sensors}
    for n in range(1, max_sensors + 1):
        s = by_num.get(n)
        if not s:
            out.update({sensor_key(n, "PRESENT"): 0, sensor_key(n, "NAME"): "", sensor_key(n, "TEMP"): "",
                        sensor_key(n, "HUMIDITY"): "", sensor_key(n, "MOTION"): 0,
                        sensor_key(n, "LOW_BATTERY"): 0, sensor_key(n, "FAULT"): 0})
            continue
        h = m.v(s.get("humidity"))
        out.update({
            sensor_key(n, "PRESENT"): 1, sensor_key(n, "NAME"): s["name"],
            sensor_key(n, "TEMP"): fmt(c_to_unit(m.v(s.get("temp")), u)),
            sensor_key(n, "HUMIDITY"): "" if h is None else int(h),
            sensor_key(n, "MOTION"): 1 if m.v(s.get("motion")) else 0,
            sensor_key(n, "LOW_BATTERY"): 1 if m.v(s.get("low_battery")) else 0,
            sensor_key(n, "FAULT"): 1 if m.v(s.get("fault")) else 0,
        })
    out["T_SENSOR_COUNT"] = len(m.sensors)
    b = lambda x: "true" if x else "false"  # noqa: E731
    out.update({
        "ThermostatCurrentTemperature_1": fmt(temp), "ThermostatCurrentHeatPoint_1": fmt(heat),
        "ThermostatCurrentCoolPoint_1": fmt(cool), "ThermostatCurrentHumidity_1": "" if hum is None else int(hum),
        "ThermostatMode_1": mode, "CurrentChangeOverStatus_1": b(mode == "Auto"),
        "ThermostatCurrentSetPoint_1": fmt(target if mode in ("Heat", "Cool") else ""),
        "IsCurrentHVACModeOff_1": b(mode == "Off"), "IsCurrentHVACModeHeat_1": b(mode == "Heat"),
        "IsCurrentHVACModeCool_1": b(mode == "Cool"), "IsCurrentHVACModeAuto_1": b(mode == "Auto"),
        "IsCurrentHVACStateIdle_1": b(hstate == "Idle"), "IsCurrentHVACStateHeating_1": b(hstate == "Heating"),
        "IsCurrentHVACStateCooling_1": b(hstate == "Cooling"),
        "IsW1RelayEnergized_1": b(hstate == "Heating"), "IsY1RelayEnergized_1": b(hstate == "Cooling"),
        "ThermostatTemperatureFormat_1": u, "IsThermostatFormatFahrenheit_1": b(u == "F"),
        "IsThermostatFormatCelsius_1": b(u == "C"),
        "ThermostatCurrentHeatPointMinimum_1": fmt(lo), "ThermostatCurrentHeatPointMaximum_1": fmt(hi),
        "ThermostatCurrentCoolPointMinimum_1": fmt(lo), "ThermostatCurrentCoolPointMaximum_1": fmt(hi),
        "ThermostatCurrentRemoteTemperature_1": out.get(sensor_key(1, "TEMP"), ""),
        "ThermostatCurrentRemoteHumidity_1": out.get(sensor_key(1, "HUMIDITY"), ""),
    })
    return out


def plan_write(m: Model, verb: str, arg: str, units_pref="auto"):
    """Savant command -> (writes [(aid, iid, value)], expected {(aid, iid): value}, label).
    Raises ValueError with a message for Savant on a bad command."""
    u = m.units(units_pref)
    mode = MODES.get(m.t(C_TGT_STATE))
    step = 1 if u == "F" else 0.5

    def setpoint_c(text, role):
        try:
            val = float(str(text).strip())
        except ValueError:
            raise ValueError(f"not a temperature: {text!r}")
        c = unit_to_c(val, u)
        lo, hi = m.limits(role)
        if not lo <= c <= hi:
            raise ValueError(f"{val:g} {u} is outside {c_to_unit(lo, u)}-{c_to_unit(hi, u)} {u}")
        return c

    if verb == "SETMODE":
        code = MODE_CODES.get(arg.strip().upper())
        if code is None:
            raise ValueError("mode must be OFF, HEAT, COOL or AUTO")
        k = m.thermo[C_TGT_STATE]
        return [(k[0], k[1], code)], {k: code}, f"mode {MODES[code]}"
    def setpoint_role(heat: bool):
        """Which characteristic holds the heat (cool) setpoint right now. Measured on the T10:
        in Heat (Cool) mode it is TargetTemperature, in Auto the Heating (Cooling) threshold;
        writes to a threshold outside Auto are reverted by the thermostat, so refuse them."""
        want = "Heat" if heat else "Cool"
        if mode == want or (mode == want and C_HEAT_TH not in m.thermo):
            return C_TGT_TEMP
        if mode == "Auto":
            return C_HEAT_TH if heat else C_COOL_TH
        raise ValueError(f"the {want.lower()} setpoint can only be changed in {want} or Auto mode "
                         f"(the thermostat is in {mode or 'an unknown mode'})")

    if verb in ("INCHEAT", "DECHEAT", "INCCOOL", "DECCOOL"):
        role = setpoint_role("HEAT" in verb)
        cur = c_to_unit(m.t(role), u)
        if cur is None:
            raise ValueError("setpoint not known yet")
        arg = str(cur + (step if verb.startswith("INC") else -step))
        verb = "SETHEAT" if "HEAT" in verb else "SETCOOL"
    if verb in ("SETHEAT", "SETCOOL"):
        role = setpoint_role(verb == "SETHEAT")
        c = setpoint_c(arg, role)
        k = m.thermo[role]
        want = c_to_unit(c, u) if u == "C" else int(round(float(arg)))
        # confirm in display units: the thermostat reports its own rounding of what was written
        return [(k[0], k[1], c)], {k: ("temp", u, want)}, \
            f"{'heat' if verb == 'SETHEAT' else 'cool'} setpoint {want} {u}"
    if verb == "SETTEMP":
        # single setpoint: the active one in Heat/Cool mode
        if mode == "Heat":
            return plan_write(m, "SETHEAT", arg, units_pref)
        if mode == "Cool":
            return plan_write(m, "SETCOOL", arg, units_pref)
        raise ValueError("SETTEMP needs Heat or Cool mode; use SETHEAT / SETCOOL")
    if verb == "SETRANGE":
        heat, _, cool = arg.replace(" ", ",").partition(",")
        if mode != "Auto":
            raise ValueError("SETRANGE needs Auto mode")
        try:
            if float(cool) <= float(heat):
                raise ValueError("cool setpoint must be above the heat setpoint")
        except (TypeError, ValueError) as e:
            raise ValueError(str(e) if "setpoint" in str(e) else "SETRANGE needs two temperatures: heat,cool")
        w1, e1, _ = plan_write(m, "SETHEAT", heat, units_pref)
        w2, e2, _ = plan_write(m, "SETCOOL", cool, units_pref)
        return w1 + w2, {**e1, **e2}, f"range {heat.strip()}-{cool.strip()} {u}"
    if verb == "RESETFILTER":
        try:
            i = int(arg.strip() or "1")
            reset = m.filters[i - 1][1]
        except (ValueError, IndexError):
            raise ValueError(f"no filter {arg!r} (the thermostat has {len(m.filters)})")
        if not reset:
            raise ValueError(f"filter {i} can't be reset over HomeKit")
        return [(reset[0], reset[1], 1)], {m.filters[i - 1][0]: 0}, f"filter {i} reset"
    raise ValueError(f"unknown command {verb}")


# ---------------------------------------------------------------------------
# bridge service
# ---------------------------------------------------------------------------
def thermostat_configs(cfg):
    """config.json -> [{"n", "ip", "port", "sensor_order"}]. "thermostats" is a list (position =
    ThermostatAddress); without it, the 1.2 keys thermostat_ip / thermostat_port / sensor_order."""
    raw = cfg.get("thermostats") or [{"ip": cfg.get("thermostat_ip"), "port": cfg.get("thermostat_port", 80),
                                      "sensor_order": cfg.get("sensor_order") or []}]
    if not isinstance(raw, list):
        raise ValueError("thermostats must be a list, e.g. [{\"ip\": \"192.168.1.50\"}]")
    if len(raw) > MAX_THERMOSTATS:
        raise ValueError(f"at most {MAX_THERMOSTATS} thermostats per bridge")
    out, seen = [], {}
    for n, t in enumerate(raw, 1):
        if isinstance(t, str):
            t = {"ip": t}
        if not isinstance(t, dict) or not str(t.get("ip") or "").strip():
            raise ValueError(f"thermostat {n} has no \"ip\"")
        ip = str(t["ip"]).strip()
        if ip in seen:
            raise ValueError(f"thermostats {seen[ip]} and {n} have the same ip {ip}")
        seen[ip] = n
        out.append({"n": n, "ip": ip, "port": int(t.get("port") or 80),
                    "sensor_order": t.get("sensor_order") or []})
    return out


def file_for(home: Path, name: str, n: int) -> Path:
    """pairing.json for thermostat 1 (as in 1.2), pairing-2.json for thermostat 2, ..."""
    return home / (f"{name}.json" if n == 1 else f"{name}-{n}.json")


class _Prefix(logging.LoggerAdapter):
    def process(self, msg, kwargs):
        return self.extra["p"] + str(msg), kwargs


class Thermostat:
    """One paired thermostat: its HomeKit session, model, pending command and saved setpoints.
    It publishes thermostat-1 key names; publish() renames them for its address."""

    def __init__(self, bridge, tc, multi=False):
        self.bridge, self.cfg, self.home = bridge, bridge.cfg, bridge.home
        self.n, self.ip, self.port, self.sensor_order = tc["n"], tc["ip"], tc["port"], tc["sensor_order"]
        self.tag = f"thermostat {self.n}: " if multi else ""
        self.log = _Prefix(log, {"p": self.tag})
        self.model: Model | None = None
        self.pairing = None
        self.azc = None
        self.online = False            # live HomeKit session
        self.offline_since = None
        self.shown_online = True
        self.pending = None            # (expected {(aid,iid): value}, deadline, label)
        self.cmd_lock = asyncio.Lock()
        self.wake = asyncio.Event()
        self.shown = {}                # last published values, thermostat-1 key names
        self.pairing_path = file_for(self.home, "pairing", self.n)
        self.numbers_path = file_for(self.home, "sensors", self.n)
        self.accessories_path = file_for(self.home, "accessories", self.n)
        self.sys = {"SYS_ONLINE": 0, "SYS_LIVE": 0, "SYS_ERROR": "",
                    "SYS_PAIRED": 1 if self.pairing_path.exists() else 0}
        self.sensor_count = 0
        # Setpoints for the mode the thermostat isn't in (e.g. the heat setpoint from Savant's
        # scheduler while in Cool mode). The T10 ignores those over HomeKit, so they are kept here
        # and written when the thermostat switches to that mode. {"heat": 68, "cool": 76, "units": "F"}
        self.saved_path = file_for(self.home, "setpoints", self.n)
        self.saved = self.load_saved()
        self.last_mode = None

    @property
    def pair_cmd(self):
        return "t10link pair" + ("" if self.n == 1 else f" {self.n}")

    def load_saved(self):
        try:
            d = json.loads(self.saved_path.read_text())
            return d if isinstance(d, dict) else {}
        except (OSError, ValueError):
            return {}

    def store_saved(self):
        try:
            self.saved_path.write_text(json.dumps(self.saved))
        except OSError as e:
            self.log.warning("could not save %s: %s", self.saved_path, e)

    def saved_value(self, kind, units):
        if self.saved.get("units") == units and self.saved.get(kind) is not None:
            return self.saved[kind]
        return None

    # -- publishing ------------------------------------------------------
    async def publish(self, updates):
        out, sys_changed = {}, False
        for k, v in updates.items():
            if k == "SYS_LAST_COMMAND":
                out[k] = f"{self.tag}{v}" if v != "" else ""
            elif k == "SYS_LAST_UPDATE":
                out[k] = v
            elif k.startswith("SYS_"):
                self.sys[k] = v
                sys_changed = True
                if k == "SYS_PAIRED":
                    self.shown["T_PAIRED"] = v
                    out[key_for(self.n, "T_PAIRED")] = v
            else:
                if k == "T_SENSOR_COUNT" and v != self.sensor_count:
                    self.sensor_count = v
                    sys_changed = True
                self.shown[k] = v
                out[key_for(self.n, k)] = v
        if sys_changed:
            out.update(self.bridge.sys_summary())
        await self.bridge.publish(out)

    async def publish_model(self):
        if not self.model:
            return
        st = compute_states(self.model, self.cfg.get("units"), self.shown_online, self.bridge.max_sensors)
        # show a saved (not yet applied) setpoint for the mode the thermostat isn't in
        raw_mode = MODES.get(self.model.t(C_TGT_STATE))
        u = st["T_UNITS"]
        for kind, uses, keys in (("heat", ("Heat", "Auto"), ("T_HEAT_SETPOINT", "ThermostatCurrentHeatPoint_1")),
                                 ("cool", ("Cool", "Auto"), ("T_COOL_SETPOINT", "ThermostatCurrentCoolPoint_1"))):
            v = self.saved_value(kind, u)
            if v is not None and raw_mode not in uses:
                for k in keys:
                    st[k] = fmt(v)
        st["T_SAVED_SETPOINTS"] = ", ".join(
            f"{k} {self.saved_value(k, u)}" for k in ("heat", "cool") if self.saved_value(k, u) is not None)
        before = tuple(ascii_text(self.shown[k]) if k in self.shown else None
                       for k in ("T_MODE", "T_HEAT_SETPOINT", "T_COOL_SETPOINT"))
        after = (st["T_MODE"], st["T_HEAT_SETPOINT"], st["T_COOL_SETPOINT"])
        if before[0] is not None and before != after and self.shown_online:
            st["T_LAST_CHANGE"] = now_text()
        await self.publish(st)

    def spawn(self, coro):
        return self.bridge.spawn(coro)

    # -- HomeKit ---------------------------------------------------------
    def on_event(self, event: dict):
        if not self.model:
            return
        for key, val in event.items():
            if isinstance(val, dict) and "value" in val:
                self.model.values[key] = val["value"]
        self.spawn(self.after_values())

    def on_available(self, available: bool):
        self.log.info("thermostat %s", "connected" if available else "disconnected")
        self.spawn(self.set_online(bool(available)))

    async def set_online(self, ok: bool, why=""):
        self.online = ok
        if ok:
            self.offline_since = None
            if not self.shown_online:
                self.shown_online = True
            await self.publish({"SYS_ONLINE": 1, "SYS_ERROR": ""})
        else:
            self.offline_since = self.offline_since or time.monotonic()
            await self.publish({"SYS_ONLINE": 0, "SYS_ERROR": why or "thermostat unreachable (retrying)"})
        await self.publish_model()

    async def after_values(self):
        await self.publish({"SYS_LAST_UPDATE": now_text()})
        await self.check_pending()
        await self.publish_model()
        await self.apply_saved_on_mode_change()

    async def apply_saved_on_mode_change(self):
        """When the thermostat enters Heat / Cool / Auto (from Savant, the wall or the Honeywell
        app), write any saved setpoint that mode uses."""
        mode = MODES.get(self.model.t(C_TGT_STATE)) if self.model else None
        if mode == self.last_mode:
            return
        self.last_mode = mode
        u = self.model.units(self.cfg.get("units"))
        for kind, verb, uses in (("heat", "SETHEAT", ("Heat", "Auto")), ("cool", "SETCOOL", ("Cool", "Auto"))):
            v = self.saved_value(kind, u)
            if v is not None and mode in uses:
                self.log.info("thermostat now in %s: applying the saved %s setpoint %s %s", mode, kind, v, u)
                self.spawn(self.do_write(verb, str(v), f"saved {kind} setpoint {v} {u} (mode now {mode})"))

    async def save_setpoint(self, kind, verb, arg, line):
        """Keep a setpoint for a mode the thermostat isn't in; applied when it switches."""
        m = self.model
        u = m.units(self.cfg.get("units"))
        role = C_HEAT_TH if kind == "heat" else C_COOL_TH
        if verb.startswith(("INC", "DEC")):
            base = self.saved_value(kind, u)
            if base is None:
                base = c_to_unit(m.t(role), u)
            if base is None:
                raise ValueError(f"{kind} setpoint not known yet")
            val = base + ((1 if u == "F" else 0.5) * (1 if verb.startswith("INC") else -1))
        else:
            try:
                val = float(str(arg).strip())
            except ValueError:
                raise ValueError(f"not a temperature: {arg!r}")
        val = int(round(val)) if u == "F" else round(val * 2) / 2
        lo, hi = (c_to_unit(x, u) for x in m.limits(role))
        if not lo <= val <= hi:
            raise ValueError(f"{val} {u} is outside {lo}-{hi} {u}")
        if self.saved.get("units") != u:
            self.saved = {"units": u}
        self.saved[kind] = val
        self.store_saved()
        mode = MODES.get(m.t(C_TGT_STATE), "an unknown mode")
        msg = f"{kind} setpoint {val} {u}: saved, applied when the thermostat switches to {kind.title()} or Auto (now {mode})"
        self.log.info("Savant: %s -> %s", line, msg)
        await self.publish({"T_ERROR": "", "T_PENDING": "", "SYS_LAST_COMMAND": msg})
        await self.publish_model()

    def clear_saved(self, kind):
        if self.saved.pop(kind, None) is not None:
            self.store_saved()

    async def clear_all_saved(self):
        self.saved = {}
        self.store_saved()
        await self.publish({"SYS_LAST_COMMAND": "saved setpoints cleared"})
        await self.publish_model()

    def defer_kind(self, verb):
        """'heat' / 'cool' when this command targets a setpoint the current mode doesn't use."""
        mode = MODES.get(self.model.t(C_TGT_STATE))
        if verb in ("SETHEAT", "INCHEAT", "DECHEAT") and mode not in ("Heat", "Auto"):
            return "heat"
        if verb in ("SETCOOL", "INCCOOL", "DECCOOL") and mode not in ("Cool", "Auto"):
            return "cool"
        return None

    async def do_range(self, arg, line):
        """SETRANGE outside Auto: the active side is written, the other one saved."""
        heat, _, cool = arg.replace(" ", ",").partition(",")
        if self.model and MODES.get(self.model.t(C_TGT_STATE)) == "Auto":
            await self.do_write("SETRANGE", arg, line)
            return
        await self.do_write("SETHEAT", heat.strip(), line)
        await self.do_write("SETCOOL", cool.strip(), line)

    def pending_matches(self, expected):
        for k, want in expected.items():
            got = self.model.values.get(k)
            if got is None:
                return False
            if isinstance(want, tuple):            # ("temp", units, value in units)
                if c_to_unit(got, want[1]) != want[2]:
                    return False
            elif abs(float(got) - float(want)) > 0.01:
                return False
        return True

    async def check_pending(self):
        """A change counts as confirmed only when the thermostat still reports it on a read at
        least 2 s after the first match: a rejected write is echoed once, then reverted."""
        if not self.pending or not self.model:
            return
        expected, deadline, label = self.pending[:3]
        matched_at = self.pending[3] if len(self.pending) > 3 else None
        if not self.pending_matches(expected):
            if matched_at:
                self.pending = (expected, deadline, label, None)   # reverted: keep waiting
            return
        if matched_at is None:
            self.pending = (expected, deadline, label, time.monotonic())
            return
        if time.monotonic() - matched_at >= 2:
            self.pending = None
            self.log.info("confirmed: %s", label)
            await self.publish({"T_PENDING": "", "T_ERROR": "", "SYS_LAST_COMMAND": f"{label}: confirmed"})

    async def read_all(self):
        keys = self.model.watched()
        res = await asyncio.wait_for(self.pairing.get_characteristics(keys), 30)
        for k, v in res.items():
            if "value" in v:
                self.model.values[k] = v["value"]
        await self.after_values()

    async def connect(self):
        data = hap_local.load_pairing(self.pairing_path, self.ip, self.port)
        if not data:
            await self.publish({"SYS_PAIRED": 0, "SYS_ERROR": f"not paired: run  {self.pair_cmd}  on the bridge machine"})
            return False
        await self.publish({"SYS_PAIRED": 1})
        controller, self.azc = await hap_local.make_controller()
        self.pairing = controller.load_pairing(f"t10link-{self.n}", data)
        self.pairing.dispatcher_connect(self.on_event)
        self.pairing.dispatcher_availability_changed(self.on_available)
        accs = await asyncio.wait_for(self.pairing.list_accessories_and_characteristics(), 60)
        self.accessories_path.write_text(json.dumps(accs, indent=1))
        saved = {}
        if self.numbers_path.exists():
            try:
                saved = json.loads(self.numbers_path.read_text())
            except ValueError:
                saved = {}
        self.model = Model(accs, self.sensor_order, saved)
        nums = {s["serial"]: self.model.numbers[s["serial"]] for s in self.model.sensors}
        if nums != {k: v for k, v in saved.items() if k in nums}:
            self.numbers_path.write_text(json.dumps({**saved, **nums}, indent=1))
        self.log.info("thermostat %s at %s (%s, firmware %s); room sensors: %s", self.model.info.get("name"), self.ip,
                      self.model.info.get("model"), self.model.info.get("firmware"),
                      ", ".join(f"{self.model.numbers[s['serial']]}={s['name']}" for s in self.model.sensors) or "none")
        ev = self.model.evented()
        try:
            status = await asyncio.wait_for(self.pairing.subscribe(ev), 30)
            bad = [k for k, v in (status or {}).items() if v.get("status")]
            await self.publish({"SYS_LIVE": 0 if (bad and len(bad) == len(ev)) else 1})
            if bad:
                self.log.warning("events refused for %s", bad)
        except Exception as e:
            self.log.warning("event subscription failed (%s): polling only", e)
            await self.publish({"SYS_LIVE": 0})
        await self.set_online(True)
        await self.read_all()
        return True

    async def hap_loop(self):
        backoff = 5
        stopping = self.bridge.stopping
        while not stopping.is_set():
            try:
                if self.pairing is None:
                    if not await self.connect():
                        await self.sleep(60)
                        continue
                    backoff = 5
                await self.sleep(int(self.cfg["poll_seconds"]))
                if stopping.is_set():
                    break
                await self.read_all()
                if not self.online:
                    await self.set_online(True)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                msg = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
                self.log.warning("HomeKit: %s", msg)
                if self.online or self.offline_since is None:
                    await self.set_online(False, f"thermostat unreachable: {ascii_text(msg, 120)}")
                if self.model is None and self.pairing is not None:
                    # never got the accessory database: start over
                    await self.close_hap()
                await self.grace_check()
                grace_left = float(self.cfg["offline_grace_seconds"]) - (time.monotonic() - (self.offline_since or time.monotonic()))
                await self.sleep(min(backoff, grace_left) if self.shown_online and grace_left > 0 else backoff)
                backoff = min(backoff * 2, 120)
            await self.grace_check()

    async def grace_check(self):
        if self.offline_since and self.shown_online and \
                time.monotonic() - self.offline_since > float(self.cfg["offline_grace_seconds"]):
            self.shown_online = False
            self.log.warning("thermostat offline for over %ss", self.cfg["offline_grace_seconds"])
            await self.publish_model()

    async def sleep(self, secs):
        self.wake.clear()
        try:
            await asyncio.wait_for(self.wake.wait(), secs)
        except asyncio.TimeoutError:
            pass

    async def close_hap(self):
        try:
            if self.pairing is not None:
                await self.pairing.close()
        except Exception:
            pass
        try:
            if self.azc is not None:
                await self.azc.async_close()
        except Exception:
            pass
        self.pairing, self.azc, self.model = None, None, None

    # -- commands --------------------------------------------------------
    async def do_write(self, verb, arg, line):
        async with self.cmd_lock:
            if not self.model or not self.pairing:
                await self.publish({"T_ERROR": "thermostat not connected", "SYS_LAST_COMMAND": f"{line}: not connected"})
                return
            try:
                if verb in ("INCTEMP", "DECTEMP"):
                    # Savant's Increase/DecreaseTemperatureAuto: the active setpoint, or both in Auto
                    mode = MODES.get(self.model.t(C_TGT_STATE))
                    step = "INC" if verb == "INCTEMP" else "DEC"
                    if mode == "Heat":
                        verb = step + "HEAT"
                    elif mode == "Cool":
                        verb = step + "COOL"
                    elif mode == "Auto":
                        u = self.model.units(self.cfg.get("units"))
                        d = (1 if u == "F" else 0.5) * (1 if step == "INC" else -1)
                        h, c = c_to_unit(self.model.t(C_HEAT_TH), u), c_to_unit(self.model.t(C_COOL_TH), u)
                        verb, arg = "SETRANGE", f"{h + d},{c + d}"
                    else:
                        raise ValueError(f"no setpoint to adjust in {mode or 'an unknown'} mode")
                kind = self.defer_kind(verb)
                if kind:
                    await self.save_setpoint(kind, verb, arg, line)
                    return
                if verb in ("SETHEAT", "INCHEAT", "DECHEAT"):
                    self.clear_saved("heat")
                elif verb in ("SETCOOL", "INCCOOL", "DECCOOL"):
                    self.clear_saved("cool")
                elif verb == "SETRANGE":
                    self.clear_saved("heat")
                    self.clear_saved("cool")
                writes, expected, label = plan_write(self.model, verb, arg, self.cfg.get("units"))
            except ValueError as e:
                self.log.warning("%s: %s", line, e)
                await self.publish({"T_ERROR": ascii_text(e), "SYS_LAST_COMMAND": f"{line}: {ascii_text(e)}"})
                return
            self.log.info("Savant: %s -> %s", line, label)
            await self.publish({"T_PENDING": label, "T_ERROR": "", "SYS_LAST_COMMAND": f"{label}: sent"})
            deadline = time.monotonic() + float(self.cfg["confirm_seconds"])
            self.pending = (expected, deadline, label)
            try:
                res = await asyncio.wait_for(self.pairing.put_characteristics(writes), 20)
                errs = [f"{v.get('description', v.get('status'))}" for v in (res or {}).values() if v.get("status")]
                if errs:
                    raise RuntimeError("thermostat refused: " + "; ".join(errs))
            except Exception as e:
                self.pending = None
                msg = ascii_text(f"{label} failed: {e}")
                self.log.warning(msg)
                await self.publish({"T_PENDING": "", "T_ERROR": msg, "SYS_LAST_COMMAND": msg})
                return
            # Never assume: read back until the thermostat reports the new value.
            for delay in (1, 2, 3, 5, 8):
                await asyncio.sleep(delay)
                if not self.pending or self.pending[2] != label:
                    return
                try:
                    await self.read_all()
                except Exception as e:
                    self.log.debug("confirm read: %s", e)
                if not self.pending or self.pending[2] != label:
                    return
                if time.monotonic() > deadline:
                    break
            if self.pending and self.pending[2] == label:
                self.pending = None
                msg = f"{label}: not confirmed by the thermostat within {self.cfg['confirm_seconds']} s"
                self.log.warning(msg)
                await self.publish({"T_PENDING": "", "T_ERROR": msg, "SYS_LAST_COMMAND": msg})


def parse_command(line):
    """"@2:SETHEAT:70" / "@2 SETHEAT 70" / "SETHEAT 70" -> (address text or None, VERB, arg)."""
    line = line.strip()
    a = None
    if line.startswith("@"):
        a, line = re.match(r"@([^\s:]*)[\s:]*(.*)", line, re.S).groups()
    verb, arg = re.match(r"([^\s:]*)[\s:]?(.*)", line.strip(), re.S).groups()
    return a, verb.upper(), arg.strip()


WRITE_VERBS = ("SETMODE", "SETHEAT", "SETCOOL", "SETTEMP", "INCHEAT", "DECHEAT",
               "INCCOOL", "DECCOOL", "INCTEMP", "DECTEMP", "RESETFILTER")


class Bridge:
    """The Savant side: TCP server, the bridge-wide SYS_* states, and command routing by address."""

    def __init__(self, cfg, home: Path, config_path: Path | None = None):
        self.cfg, self.home = cfg, home
        self.config_path = Path(config_path) if config_path else home / "config.json"
        self.values = {}
        self.clients = set()
        self.loops = {}                # thermostat n -> its hap_loop task
        self.pair_lock = asyncio.Lock()
        self.pairing_now = None        # thermostat n being paired from the web page
        self.rejected_logged = {}
        self.allow_any, self.allow_local, self.allow_nets = parse_allowed(cfg.get("allowed_clients", "auto"))
        self.max_sensors = max(1, min(16, int(cfg.get("max_sensors", 4))))
        self.stopping = None
        self.tasks = set()
        tcs = thermostat_configs(cfg)
        self.thermostats = [Thermostat(self, tc, multi=len(tcs) > 1) for tc in tcs]
        self.by_n = {t.n: t for t in self.thermostats}

    # -- publishing ------------------------------------------------------
    async def publish(self, updates):
        lines = []
        for k, v in updates.items():
            if isinstance(v, bool):
                v = "true" if v else "false"
            v = ascii_text(v)
            if self.values.get(k) != v:
                self.values[k] = v
                lines.append(f"{k}={v}\n")
        if lines and self.clients:
            data = "".join(lines).encode(SAVANT_ENCODING, "replace")
            for w in list(self.clients):
                try:
                    w.write(data)
                except Exception:
                    self.clients.discard(w)
            for w in list(self.clients):
                try:
                    await asyncio.wait_for(w.drain(), 5)
                except Exception:
                    self.clients.discard(w)

    def sys_summary(self):
        """Bridge-wide health from every thermostat's own SYS_* values. With one thermostat this
        is exactly that thermostat's values, as in 1.2."""
        ts = self.thermostats
        errs = [t.tag + str(t.sys["SYS_ERROR"]) for t in ts if t.sys.get("SYS_ERROR")]
        every = lambda k: 1 if all(int(t.sys.get(k) or 0) == 1 for t in ts) else 0  # noqa: E731
        return {"SYS_ONLINE": every("SYS_ONLINE"), "SYS_PAIRED": every("SYS_PAIRED"), "SYS_LIVE": every("SYS_LIVE"),
                "SYS_ERROR": "; ".join(errs), "SYS_SENSOR_COUNT": sum(int(t.sensor_count or 0) for t in ts),
                "SYS_THERMOSTAT_COUNT": len(ts)}

    def spawn(self, coro):
        t = asyncio.ensure_future(coro)
        self.tasks.add(t)
        t.add_done_callback(self.tasks.discard)
        return t

    def wake_all(self):
        for t in self.thermostats:
            t.wake.set()

    # -- commands --------------------------------------------------------
    async def handle_line(self, line, writer):
        """[@N:]VERB[:ARG] or [@N ]VERB [ARG]. @N picks thermostat N (Savant's ThermostatAddress;
        blank = 1); without it, thermostat 1 (bridge-wide verbs: all of them). The profile uses
        ":" so no command parameter needs a space."""
        line = line.strip()
        if not line:
            return
        a, verb, arg = parse_command(line)
        t = None
        if a is not None:
            try:
                t = self.by_n.get(int(a) if a else 1)
            except ValueError:
                t = None
            if t is None:
                writer.write(f"SYS_ERROR=no thermostat {ascii_text(a, 10)} in config.json "
                             f"({len(self.thermostats)} configured)\n".encode(SAVANT_ENCODING, "replace"))
                await writer.drain()
                return
            if not verb:
                return
        target = t or self.thermostats[0]
        if verb == "PING":
            writer.write(b"PONG\n")
        elif verb == "DUMP":
            writer.write("".join(f"{k}={v}\n" for k, v in self.values.items()).encode(SAVANT_ENCODING, "replace"))
        elif verb in ("REFRESH", "STATUS"):
            if t:
                t.wake.set()
            else:
                self.wake_all()
        elif verb == "SETRANGE":
            self.spawn(target.do_range(arg, line))
        elif verb == "CLEARSAVED":
            await target.clear_all_saved()
        elif verb in WRITE_VERBS:
            self.spawn(target.do_write(verb, arg.replace(",", " ").strip(), line))
        else:
            writer.write(f"SYS_ERROR=unknown command {ascii_text(verb, 40)}\n".encode(SAVANT_ENCODING, "replace"))
        await writer.drain()

    def client_allowed(self, ip):
        if self.allow_any:
            return True
        try:
            addr = ipaddress.ip_address(ip)
        except ValueError:
            return False
        if getattr(addr, "ipv4_mapped", None):
            addr = addr.ipv4_mapped
        if addr.is_loopback:
            return True
        if any(addr in net for net in self.allow_nets):
            return True
        return self.allow_local and is_this_machine(str(addr))

    async def handle_tcp(self, reader, writer):
        peer = writer.get_extra_info("peername")
        ip = peer[0] if peer else ""
        if not self.client_allowed(ip):
            now = time.monotonic()
            if now - self.rejected_logged.get(ip, -1e9) > 300:
                self.rejected_logged[ip] = now
                log.warning("refused connection from %s (not in allowed_clients %s)", ip,
                            json.dumps(self.cfg.get("allowed_clients")))
            await self.publish({"SYS_REJECTED_CLIENT": f"{ip} {now_text()}"})
            try:
                writer.close()
            except Exception:
                pass
            return
        log.info("Savant connected from %s", peer)
        self.clients.add(writer)
        try:
            writer.write("".join(f"{k}={v}\n" for k, v in self.values.items()).encode(SAVANT_ENCODING, "replace"))
            await writer.drain()
            while not self.stopping.is_set():
                raw = await reader.readline()
                if not raw:
                    break
                for part in raw.decode("utf-8", "replace").replace("\r", "\n").split("\n"):
                    await self.handle_line(part, writer)
        except (ConnectionError, asyncio.IncompleteReadError):
            pass
        except Exception as e:
            log.warning("client %s: %s", peer, e)
        finally:
            self.clients.discard(writer)
            try:
                writer.close()
            except Exception:
                pass
            log.info("Savant disconnected %s", peer)

    # -- web page: status, pairing, adding thermostats -------------------------
    def retag(self):
        multi = len(self.thermostats) > 1
        for t in self.thermostats:
            t.tag = f"thermostat {t.n}: " if multi else ""
            t.log.extra["p"] = t.tag

    def start_loop(self, t):
        if self.stopping is not None and not self.stopping.is_set():
            self.loops[t.n] = asyncio.ensure_future(t.hap_loop())

    def write_thermostats(self, change):
        """Apply change(list) to config.json's thermostats list, keeping every other setting. A 1.2
        config (thermostat_ip ...) is converted to the list first."""
        path = self.config_path
        raw = json.loads(path.read_text()) if path.exists() else {}
        if not raw.get("thermostats"):
            raw["thermostats"] = [{"ip": t.ip, "port": t.port, "sensor_order": t.sensor_order} for t in self.thermostats]
            for k in ("thermostat_ip", "thermostat_port", "sensor_order"):
                raw.pop(k, None)
        change(raw["thermostats"])
        thermostat_configs(dict(self.cfg, thermostats=raw["thermostats"]))     # validate before writing
        mode = path.stat().st_mode & 0o777 if path.exists() else 0o644
        tmp = path.with_name(path.name + ".tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        self.cfg["thermostats"] = raw["thermostats"]

    async def add_thermostat(self, ip):
        """Add a thermostat by IP while running: saved to config.json, connected straight away."""
        try:
            ip = str(ipaddress.ip_address(str(ip).strip()))
        except ValueError:
            raise ValueError(f"not an IP address: {ascii_text(ip, 60)!r}")
        if any(t.ip == ip for t in self.thermostats):
            raise ValueError(f"{ip} is already thermostat {next(t.n for t in self.thermostats if t.ip == ip)}")
        if len(self.thermostats) >= MAX_THERMOSTATS:
            raise ValueError(f"a bridge takes at most {MAX_THERMOSTATS} thermostats")
        self.write_thermostats(lambda lst: lst.append({"ip": ip}))
        tc = thermostat_configs(self.cfg)[-1]
        t = Thermostat(self, tc)
        self.thermostats.append(t)
        self.by_n[t.n] = t
        self.retag()
        log.info("web page: added thermostat %s at %s", t.n, ip)
        await self.publish(self.sys_summary())
        await t.publish({"T_IP": t.ip, "T_PAIRED": t.sys["SYS_PAIRED"], "T_ONLINE": 0, "T_PENDING": "", "T_ERROR": "",
                         "T_LAST_CHANGE": ""})
        self.start_loop(t)
        return t

    async def remove_thermostat(self, n):
        """Only the last thermostat, and only while it isn't paired: removing it can't renumber the
        others or take a working thermostat out of Savant. (Unpairing stays on the command line.)"""
        t = self.by_n.get(n)
        if t is None:
            raise ValueError(f"no thermostat {n}")
        if n != len(self.thermostats) or n == 1:
            raise ValueError("only the last thermostat can be removed here, and not the only one")
        if t.pairing_path.exists():
            raise ValueError(f"thermostat {n} is paired: unpair it first on the bridge machine (t10link unpair {n})")
        self.write_thermostats(lambda lst: lst.pop())
        task = self.loops.pop(n, None)
        if task:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        await t.close_hap()
        self.thermostats.remove(t)
        del self.by_n[n]
        self.retag()
        log.info("web page: removed thermostat %s (%s)", n, t.ip)
        await self.publish({key_for(n, "T_IP"): "", key_for(n, "T_ONLINE"): 0, key_for(n, "T_PAIRED"): 0, **self.sys_summary()})

    async def pair(self, n, code):
        """Pair thermostat n with the code it shows (from the web page); the bridge connects at once."""
        t = self.by_n.get(n)
        if t is None:
            raise PairError(f"no thermostat {n}")
        if t.pairing_path.exists():
            raise PairError(f"thermostat {n} is already paired")
        if self.pair_lock.locked():
            raise PairError(f"thermostat {self.pairing_now} is being paired: wait for it to finish")
        async with self.pair_lock:
            self.pairing_now = n
            try:
                hap_local.normalize_pin(code)          # a typo fails before waking the thermostat
            except ValueError as e:
                self.pairing_now = None
                raise PairError(str(e))
            try:
                t.log.info("web page: pairing with %s", t.ip)
                rec = await find_record(t.ip)
                await pair_with_code(t.ip, code, t.pairing_path, rec)
            finally:
                self.pairing_now = None
        t.log.info("web page: paired (%s)", rec.get("name"))
        await t.publish({"SYS_PAIRED": 1, "SYS_ERROR": ""})
        t.wake.set()
        return f"Paired thermostat {n} ({rec.get('name', 'T10')}). The bridge is connecting to it now."

    def status(self):
        """Everything the web page shows."""
        out = []
        for t in self.thermostats:
            sh = t.shown
            g = lambda k: "" if sh.get(k) is None else str(sh.get(k))  # noqa: E731
            sensors = [{"n": i, "name": g(sensor_key(i, "NAME")), "temp": g(sensor_key(i, "TEMP")),
                        "humidity": g(sensor_key(i, "HUMIDITY")), "motion": g(sensor_key(i, "MOTION")) == "1"}
                       for i in range(1, self.max_sensors + 1) if g(sensor_key(i, "PRESENT")) == "1"]
            out.append({"n": t.n, "ip": t.ip, "paired": t.pairing_path.exists(), "connected": bool(t.online),
                        "online": g("T_ONLINE") == "1", "name": g("T_NAME"), "model": g("T_MODEL"),
                        "firmware": g("T_FIRMWARE"), "mode": g("T_MODE"), "state": g("T_STATE"), "temp": g("T_TEMP"),
                        "heat": g("T_HEAT_SETPOINT"), "cool": g("T_COOL_SETPOINT"), "humidity": g("T_HUMIDITY"),
                        "units": g("T_UNITS"), "pending": g("T_PENDING"), "error": g("T_ERROR"),
                        "problem": str(t.sys.get("SYS_ERROR") or ""), "sensors": sensors,
                        "removable": t.n == len(self.thermostats) and t.n > 1 and not t.pairing_path.exists()})
        return {"version": VERSION, "thermostats": out, "max": MAX_THERMOSTATS, "pairing": self.pairing_now,
                "savant_connected": len(self.clients), "port": int(self.cfg["tcp_port"])}

    def request_stop(self, why):
        self.stop_reason = why
        self.stopping.set()
        self.wake_all()

    async def start(self):
        """Initial states; the service and the tests both start here."""
        self.stopping = asyncio.Event()
        self.stop_reason = ""
        await self.publish({"SYS_VERSION": VERSION, "SYS_LAST_COMMAND": "", "SYS_LAST_UPDATE": "",
                            "SYS_REJECTED_CLIENT": "", **self.sys_summary()})
        for t in self.thermostats:
            await t.publish({"T_IP": t.ip, "T_PAIRED": t.sys["SYS_PAIRED"], "T_PENDING": "", "T_ERROR": "",
                             "T_LAST_CHANGE": ""})

    async def run(self):
        loop = asyncio.get_running_loop()
        await self.start()
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            try:
                loop.add_signal_handler(sig, self.request_stop, "signal " + sig.name)
            except (NotImplementedError, RuntimeError, ValueError):
                pass
        srv = await asyncio.start_server(self.handle_tcp, "0.0.0.0", int(self.cfg["tcp_port"]))
        log.info("Savant TCP on :%s, pid %s", self.cfg["tcp_port"], os.getpid())
        log.info("thermostats: %s", ", ".join(f"{t.n}={t.ip}" for t in self.thermostats))
        who = (["this machine"] if self.allow_local else []) + [str(n) for n in self.allow_nets]
        log.info("accepting Savant connections from: %s", "anyone" if self.allow_any else ", ".join(who) or "localhost only")
        for t in self.thermostats:
            self.start_loop(t)
        web_srv = None
        if int(self.cfg.get("web_port") or 0):
            import t10link_web
            try:
                web_srv = await asyncio.start_server(t10link_web.WebPage(self).handle, "0.0.0.0", int(self.cfg["web_port"]))
                log.info("web page on :%s (status, pairing, adding thermostats)", self.cfg["web_port"])
            except OSError as e:
                log.warning("web page off: can't use port %s (%s); set web_port in config.json", self.cfg["web_port"], e)
        await self.stopping.wait()
        log.info("shutting down (%s)", self.stop_reason)
        loops = list(self.loops.values())
        for task in loops:
            task.cancel()
        srv.close()
        if web_srv:
            web_srv.close()
        for w in list(self.clients):
            try:
                w.close()
            except Exception:
                pass
        await asyncio.gather(*loops, return_exceptions=True)
        for t in self.thermostats:
            await t.close_hap()
        log.info("stopped")
        return 0


# ---------------------------------------------------------------------------
# CLI: pair / unpair / dump
# ---------------------------------------------------------------------------
def nudge_service(cfg, n=1, verb="REFRESH"):
    try:
        with socket.create_connection(("127.0.0.1", int(cfg["tcp_port"])), 3) as s:
            s.sendall(f"@{n} {verb}\n".encode())
        return True
    except OSError:
        return False


class PairError(Exception):
    pass


async def find_record(ip, tries=25):
    """The thermostat's HomeKit record, checked to be pairable."""
    rec = await asyncio.get_running_loop().run_in_executor(None, lambda: hap_local.query_hap_record(ip, tries=tries))
    if not rec:
        raise PairError(f"No HomeKit answer from {ip}. Check the thermostat's ip in config.json and that it is on Wi-Fi "
                        "(its Wi-Fi sleeps: try again).")
    if str(rec.get("sf")) == "0":
        raise PairError("It is already paired to another HomeKit controller. Remove it from Apple Home (or the other "
                        "controller) first, or use Menu > Advanced Setup > Reset > HomeKit Reset on the thermostat.")
    return rec


async def pair_with_code(ip, code, out: Path, rec):
    """Pair-Setup with the code the thermostat shows; save only the long-term keys (mode 600)."""
    try:
        pin = hap_local.normalize_pin(code)
    except ValueError as e:
        raise PairError(str(e))
    try:
        data = await asyncio.wait_for(hap_local.pair(ip, pin, record=rec), 60)
    except Exception as e:
        raise PairError(f"Pairing failed: {type(e).__name__}: {e}. A wrong code fails harmlessly; after several "
                        "wrong tries the thermostat locks pairing for a while. The code changes each time "
                        "Connect HomeKit opens.")
    hap_local.write_private_json(out, data)


def pick_thermostat(cfg, home: Path, n, purpose):
    """n from the command line (None = not given) -> that thermostat's config entry."""
    tcs = thermostat_configs(cfg)
    if n is None or n == 0:
        if purpose == "pair":
            unpaired = [tc for tc in tcs if not file_for(home, "pairing", tc["n"]).exists()]
            if not unpaired:
                which = "it is" if len(tcs) == 1 else f"all {len(tcs)} are"
                raise SystemExit(f"Nothing to pair: {which} already paired (t10link unpair N to start over).")
            return unpaired[0]
        if len(tcs) > 1 and purpose == "unpair":
            raise SystemExit(f"Which thermostat? {len(tcs)} are configured: t10link unpair 1 ... t10link unpair {len(tcs)}")
        return tcs[0]
    if not 1 <= n <= len(tcs):
        raise SystemExit(f"No thermostat {n}: config.json lists {len(tcs)} (1-{len(tcs)}).")
    return tcs[n - 1]


async def cli_pair(cfg, home: Path, n=None):
    tc = pick_thermostat(cfg, home, n, "pair")
    ip, out = tc["ip"], file_for(home, "pairing", tc["n"])
    cmd_n = "" if tc["n"] == 1 else f" {tc['n']}"
    if out.exists():
        raise SystemExit(f"{out} already exists: thermostat {tc['n']} is already paired. Unpair first (t10link unpair{cmd_n}).")
    many = len(thermostat_configs(cfg)) > 1
    print(f"{'Thermostat ' + str(tc['n']) + ': l' if many else 'L'}ooking for the thermostat's HomeKit record at {ip} "
          "(it may take a few seconds to wake) ...")
    try:
        rec = await find_record(ip)
        print(f"Found {rec['name']} ({rec.get('md', '?')}), HomeKit id {rec['id']}, NOT paired.")
        print("\nOn the thermostat: Menu > Connect HomeKit. It shows an 8-digit code (123-45-678).")
        print("The code changes each time that screen opens: use the one showing now.")
        code = getpass.getpass("HomeKit setup code (hidden): ")
        print("Pairing ... (up to 30 s)")
        await pair_with_code(ip, code, out, rec)
    except PairError as e:
        raise SystemExit(str(e))
    print(f"\nPaired thermostat {tc['n']}. Long-term keys saved to {out} (mode 600). The setup code was not saved.")
    if nudge_service(cfg, tc["n"]):
        print("Told the running bridge to connect.")
    left = [t["n"] for t in thermostat_configs(cfg) if not file_for(home, "pairing", t["n"]).exists()]
    if left:
        print(f"Still to pair: thermostat {', '.join(map(str, left))}  (t10link pair {left[0]})")


async def open_pairing(cfg, home: Path, tc):
    data = hap_local.load_pairing(file_for(home, "pairing", tc["n"]), tc["ip"], tc["port"])
    if not data:
        raise SystemExit(f"thermostat {tc['n']} is not paired: run  t10link pair{'' if tc['n'] == 1 else ' ' + str(tc['n'])}  first")
    controller, azc = await hap_local.make_controller()
    p = controller.load_pairing("t10link-cli", data)
    return p, azc


async def cli_dump(cfg, home: Path, raw=False, n=None):
    tc = pick_thermostat(cfg, home, n, "dump")
    p, azc = await open_pairing(cfg, home, tc)
    out = file_for(home, "accessories", tc["n"])
    try:
        accs = await asyncio.wait_for(p.list_accessories_and_characteristics(), 60)
        out.write_text(json.dumps(accs, indent=1))
        if raw:
            print(json.dumps(accs, indent=1))
            return
        for a in accs:
            print(f"\n=== accessory aid={a['aid']}")
            for s in a["services"]:
                print(f"  service {short_type(s['type'])} iid={s['iid']}")
                for c in s["characteristics"]:
                    extra = {k: c[k] for k in ("minValue", "maxValue", "minStep", "valid-values", "unit") if k in c}
                    print(f"    {c['iid']:>4} {short_type(c['type'])[:12]:12} {','.join(c.get('perms', [])):10} "
                          f"value={c.get('value')!r} {extra if extra else ''}")
        m = Model(accs, tc["sensor_order"])
        print(f"\nAs Savant sees thermostat {tc['n']}:")
        for k, v in compute_states(m, cfg.get("units"), True, int(cfg.get("max_sensors", 4))).items():
            if not k.startswith(("Is", "Thermostat")):
                print(f"  {key_for(tc['n'], k)}={v}")
        print(f"\nRaw JSON saved to {out}")
    finally:
        await p.close()
        await azc.async_close()


async def cli_unpair(cfg, home: Path, n=None):
    tc = pick_thermostat(cfg, home, n, "unpair")
    p, azc = await open_pairing(cfg, home, tc)
    try:
        await asyncio.wait_for(p.remove_pairing(p.pairing_data["iOSPairingId"]), 30)
        print(f"Removed this bridge's pairing from thermostat {tc['n']} ({tc['ip']}).")
    except Exception as e:
        print(f"Could not remove the pairing on the thermostat ({e}); deleting the local keys anyway.")
    finally:
        await p.close()
        await azc.async_close()
    path = file_for(home, "pairing", tc["n"])
    path.unlink(missing_ok=True)
    print(f"Deleted {path.name}. The thermostat can now be paired again (restart the service to drop its session).")


def load_config(path):
    cfg = dict(DEFAULT_CONFIG)
    p = Path(path)
    if p.exists():
        cfg.update({k: v for k, v in json.loads(p.read_text()).items() if not k.startswith("_")})
    cfg["poll_seconds"] = max(15, int(cfg["poll_seconds"]))
    try:
        parse_allowed(cfg.get("allowed_clients", "auto"))
    except ValueError as e:
        raise SystemExit(f"config.json allowed_clients: {e}")
    try:
        thermostat_configs(cfg)
    except (ValueError, TypeError) as e:
        raise SystemExit(f"config.json thermostats: {e}")
    return cfg


def main():
    ap = argparse.ArgumentParser(description="T10Link - Honeywell T9/T10 HomeKit bridge for Savant")
    ap.add_argument("-c", "--config", default=str(Path(__file__).parent / "config.json"))
    ap.add_argument("-v", "--verbose", action="store_true")
    ap.add_argument("-l", "--logfile", help="log to a rotating file (1 MB x 3) instead of the console")
    ap.add_argument("--pair", nargs="?", type=int, const=0, metavar="N",
                    help="one-time HomeKit pairing of thermostat N (default: the first one not paired)")
    ap.add_argument("--unpair", nargs="?", type=int, const=0, metavar="N", help="remove this bridge's pairing from thermostat N")
    ap.add_argument("--dump", nargs="?", type=int, const=0, metavar="N", help="print thermostat N's HomeKit services (default 1)")
    ap.add_argument("--raw", action="store_true", help="with --dump: raw JSON")
    ap.add_argument("--version", action="version", version=VERSION)
    a = ap.parse_args()
    cfg = load_config(a.config)
    home = Path(a.config).resolve().parent
    if a.pair is not None or a.unpair is not None or a.dump is not None:
        logging.basicConfig(level=logging.DEBUG if a.verbose else logging.WARNING, format="%(levelname)s %(message)s")
        asyncio.run(cli_pair(cfg, home, a.pair) if a.pair is not None else
                    cli_unpair(cfg, home, a.unpair) if a.unpair is not None else cli_dump(cfg, home, a.raw, a.dump))
        return
    level = logging.DEBUG if a.verbose else getattr(logging, str(cfg.get("log_level", "INFO")).upper(), logging.INFO)
    handlers = [logging.handlers.RotatingFileHandler(a.logfile, maxBytes=1000000, backupCount=3, encoding="utf-8")] \
        if a.logfile else [logging.StreamHandler()]
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s %(message)s", handlers=handlers)
    for noisy in ("aiohomekit", "zeroconf"):
        logging.getLogger(noisy).setLevel(logging.DEBUG if a.verbose else logging.WARNING)
    log.info("T10Link bridge %s, Python %s", VERSION, sys.version.split()[0])
    try:
        code = asyncio.run(Bridge(cfg, home, Path(a.config).resolve()).run())
    except OSError as e:
        log.error("could not start: %s (is port %s already in use?)", e, cfg["tcp_port"])
        code = 2
    sys.exit(code)


if __name__ == "__main__":
    main()
