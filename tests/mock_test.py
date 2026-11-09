#!/usr/bin/env python3
"""
T10Link mock tests: the bridge against a simulated T10 built from the real accessory database
(tests/t10_accessories.json, dumped from the thermostat), with the behaviour measured on
firmware 1.5.3:
  * a written Celsius setpoint is converted to F and rounded UP; reported back on the 0.5 C grid
  * outside Auto, writes to the heating/cooling thresholds are echoed once, then reverted
  * the active Heat/Cool setpoint is TargetTemperature
Runs without aiohomekit:  python3 -I tests/mock_test.py
"""
import asyncio
import copy
import json
import math
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent / "bridge"))
import t10link_bridge as b  # noqa: E402

ACCS = json.loads((HERE / "t10_accessories.json").read_text())
PASS, FAIL = [], []


def check(name, cond, detail=""):
    (PASS if cond else FAIL).append(name)
    print(("ok   " if cond else "FAIL ") + name + ("" if cond else f"   {detail}"))


# ---------------------------------------------------------------------------
class FakeT10:
    """Stands in for aiohomekit's IpPairing."""

    def __init__(self, accs):
        self.accs = copy.deepcopy(accs)
        self.vals = {}
        for a in self.accs:
            for s in a["services"]:
                for c in s["characteristics"]:
                    if "value" in c:
                        self.vals[(a["aid"], c["iid"])] = c["value"]
        self.listeners, self.avail = [], []
        self.writes = []
        self.fail_reads = False
        self.loop = asyncio.get_event_loop()

    def dispatcher_connect(self, cb):
        self.listeners.append(cb)

    def dispatcher_availability_changed(self, cb):
        self.avail.append(cb)

    async def list_accessories_and_characteristics(self):
        return copy.deepcopy(self.accs)

    async def subscribe(self, keys):
        return {}

    async def get_characteristics(self, keys):
        if self.fail_reads:
            raise ConnectionError("simulated: no route to host")
        return {k: {"value": self.vals.get(k)} for k in keys}

    def emit(self, ev):
        for cb in self.listeners:
            cb({k: {"value": v} for k, v in ev.items()})

    async def put_characteristics(self, writes):
        self.writes.append(list(writes))
        mode = self.vals[(2, 36)]
        for aid, iid, v in writes:
            if iid in (38, 43, 44):
                f = math.ceil(v * 9 / 5 + 32 - 1e-9)            # thermostat rounds up to F
                stored = round((f - 32) * 5 / 9 * 2) / 2
                if iid in (43, 44) and mode != 3:
                    self.vals[(aid, iid)] = v
                    self.emit({(aid, iid): v})                   # echo ...
                    old = self.vals.get(("orig", iid), None)
                    self.loop.call_later(0.3, self._revert, aid, iid, old)
                    continue
                self.vals[(aid, iid)] = stored
                self.emit({(aid, iid): stored})
            else:
                self.vals[(aid, iid)] = int(v)
                self.emit({(aid, iid): int(v)})
                if iid == 98:                                    # filter 1 reset
                    self.vals[(2, 97)] = 0
                    self.emit({(2, 97): 0})
        return {}

    def _revert(self, aid, iid, old):
        self.vals[(aid, iid)] = old
        self.emit({(aid, iid): old})

    async def close(self):
        pass


# ---------------------------------------------------------------------------
def unit_tests():
    m = b.Model(ACCS)
    st = b.compute_states(m)
    check("model: thermostat found on aid 2", m.thermo_aid == 2)
    check("model: one room sensor, numbered 1", len(m.sensors) == 1 and list(m.numbers.values()) == [1])
    check("model: two filters", len(m.filters) == 2)
    check("states: Cool, 72 F, cool setpoint 72", (st["T_MODE"], st["T_TEMP"], st["T_COOL_SETPOINT"]) == ("Cool", "72", "72"),
          (st["T_MODE"], st["T_TEMP"], st["T_COOL_SETPOINT"]))
    check("states: units F", st["T_UNITS"] == "F" and st["ThermostatTemperatureFormat_1"] == "F")
    check("states: Savant mode booleans", st["IsCurrentHVACModeCool_1"] == "true" and st["IsCurrentHVACModeHeat_1"] == "false")
    check("states: sensor 1 name", st["SENSOR1_NAME"] == "Master Bedroom Air Sensor 01")
    check("states: remote temperature = sensor 1", st["ThermostatCurrentRemoteTemperature_1"] == st["SENSOR1_TEMP"])
    check("states: filter 1 due", st["T_FILTER1_CHANGE"] == 1 and st["T_FILTER2_CHANGE"] == 0)
    off = b.compute_states(m, online=False)
    check("offline: mode Unknown, temperature kept", off["T_MODE"] == "Unknown" and off["T_TEMP"] == "72" and off["T_ONLINE"] == 0)
    check("state defs cover every published key", set(st) <= {k for k, _, _ in b.state_defs(4)},
          set(st) - {k for k, _, _ in b.state_defs(4)})

    # F <-> C: every whole F 50..90 survives the thermostat's round-up
    ok = all(math.ceil(b.unit_to_c(f, "F") * 9 / 5 + 32 - 1e-9) == f for f in range(50, 91))
    check("units: whole F writes land exactly (50-90 F)", ok)
    check("units: 73 F -> 22.5 C (not 23.0, which shows 74)", b.unit_to_c(73, "F") == 22.5)

    w, e, label = b.plan_write(m, "SETCOOL", "71")
    check("Cool mode SETCOOL writes TargetTemperature only", w == [(2, 38, 21.5)], w)
    check("SETCOOL confirms in F", e == {(2, 38): ("temp", "F", 71)}, e)
    try:
        b.plan_write(m, "SETHEAT", "65")
        check("Cool mode SETHEAT refused", False)
    except ValueError as ex:
        check("Cool mode SETHEAT refused", "Heat or Auto" in str(ex), ex)
    w, _, _ = b.plan_write(m, "INCCOOL", "")
    check("INCCOOL = target + 1 F (73 -> 22.5 C)", w == [(2, 38, 22.5)], w)
    for bad in ("SETCOOL 200", "SETCOOL warm", "SETMODE FAN"):
        v, _, a = bad.partition(" ")
        try:
            b.plan_write(m, v, a)
            check(f"refused: {bad}", False)
        except ValueError:
            check(f"refused: {bad}", True)
    w, e, _ = b.plan_write(m, "SETMODE", "auto")
    check("SETMODE AUTO -> 3", w == [(2, 36, 3)])
    m.values[(2, 36)] = 3
    check("CurrentChangeOverStatus_1 true in Auto", b.compute_states(m)["CurrentChangeOverStatus_1"] == "true")
    check("ThermostatCurrentSetPoint_1 blank in Auto", b.compute_states(m)["ThermostatCurrentSetPoint_1"] == "")
    m.values[(2, 36)] = 2
    m.values[(2, 36)] = 3
    w, _, _ = b.plan_write(m, "SETRANGE", "64,76")
    check("Auto SETRANGE writes both thresholds", sorted(w) == [(2, 43, 24.0), (2, 44, 17.5)], w)
    m.values[(2, 36)] = 1
    st = b.compute_states(m)
    check("Heat mode: heat setpoint = target", st["T_HEAT_SETPOINT"] == st["T_TARGET"] == "72", (st["T_HEAT_SETPOINT"], st["T_TARGET"]))
    w, _, _ = b.plan_write(m, "RESETFILTER", "1")
    check("RESETFILTER 1 writes the reset characteristic", w == [(2, 98, 1)], w)

    saved = {"20000000000001": 3}
    m2 = b.Model(ACCS, saved_numbers=saved)
    check("sensor numbering: saved map kept", m2.numbers["20000000000001"] == 3)
    m3 = b.Model(ACCS, sensor_order=["master bedroom air sensor 01"], saved_numbers={"20000000000001": 3})
    check("sensor numbering: config order wins", m3.numbers["20000000000001"] == 1)
    check("allowed: Savant host", b.parse_allowed(["auto", "192.168.1.20"])[2][0].num_addresses == 1)


# ---------------------------------------------------------------------------
async def protocol_tests():
    tmp = Path(tempfile.mkdtemp())
    cfg = dict(b.DEFAULT_CONFIG, tcp_port=0, poll_seconds=15, confirm_seconds=6, allowed_clients="auto",
               thermostat_ip="192.168.1.50")
    bridge = b.Bridge(cfg, tmp)
    t1 = bridge.thermostats[0]
    check("1.2 config (thermostat_ip): one thermostat at that ip", len(bridge.thermostats) == 1 and t1.ip == cfg["thermostat_ip"])
    fake = FakeT10(ACCS)
    # remember the original thresholds for the revert simulation
    fake.vals[("orig", 43)] = fake.vals[(2, 43)]
    fake.vals[("orig", 44)] = fake.vals[(2, 44)]

    async def fake_connect():
        t1.pairing = fake
        fake.dispatcher_connect(t1.on_event)
        fake.dispatcher_availability_changed(t1.on_available)
        t1.model = b.Model(await fake.list_accessories_and_characteristics())
        await t1.publish({"SYS_PAIRED": 1, "SYS_LIVE": 1})
        await t1.set_online(True)
        await t1.read_all()
        return True

    t1.connect = fake_connect
    await bridge.start()
    srv = await asyncio.start_server(bridge.handle_tcp, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    hap = asyncio.ensure_future(t1.hap_loop())
    await asyncio.sleep(0.3)

    r, w = await asyncio.open_connection("127.0.0.1", port)
    lines = []

    async def reader():
        while True:
            ln = await r.readline()
            if not ln:
                return
            lines.append(ln.decode().strip())

    rt = asyncio.ensure_future(reader())

    async def send(cmd, wait=0.5):
        start = len(lines)
        w.write(cmd.encode() + b"\n")
        await w.drain()
        await asyncio.sleep(wait)
        return lines[start:]

    await asyncio.sleep(0.3)
    init = dict(ln.split("=", 1) for ln in lines if "=" in ln)
    check("connect: full dump", len(init) > 60, len(init))
    check("connect: SYS_ONLINE=1", init.get("SYS_ONLINE") == "1")
    check("connect: ThermostatMode_1=Cool", init.get("ThermostatMode_1") == "Cool")
    check("PING -> PONG", "PONG" in await send("PING"))

    out = await send("SETCOOL 73", 4.5)
    check("SETCOOL 73: pending shown", "T_PENDING=cool setpoint 73 F" in out, out)
    check("SETCOOL 73: confirmed", any("cool setpoint 73 F: confirmed" in x for x in out), out)
    check("SETCOOL 73: thermostat shows 73", fake.vals[(2, 38)] == 23.0 and "ThermostatCurrentCoolPoint_1=73" in out,
          (fake.vals[(2, 38)], out))

    # Savant's scheduler sends both setpoints: in Cool mode the heat one is saved, not written
    out = await send("SETHEAT 66", 0.5)
    check("SETHEAT in Cool mode: saved, nothing written",
          any("heat setpoint 66 F: saved" in x for x in out) and len(fake.writes) == 1 and "T_ERROR=" not in
          [x for x in out if x.startswith("T_ERROR=the")], out)
    check("saved heat setpoint shown to Savant", "ThermostatCurrentHeatPoint_1=66" in out and "T_SAVED_SETPOINTS=heat 66" in out, out)
    out = await send("INCHEAT", 0.5)
    check("INCHEAT in Cool mode adjusts the saved value", "T_HEAT_SETPOINT=67" in out, out)
    await send("SETHEAT 66", 0.3)
    check("saved setpoint persisted", json.loads((tmp / "setpoints.json").read_text()).get("heat") == 66)

    # a threshold write the thermostat reverts must NOT be reported as confirmed
    real_plan = b.plan_write
    b.plan_write = lambda m, v, a, u="auto": ([(2, 43, 23.5)], {(2, 43): ("temp", "F", 74)}, "cool setpoint 74 F")
    out = await send("SETCOOL 74", 7)
    b.plan_write = real_plan
    check("reverted write: not confirmed, error after timeout",
          not any(": confirmed" in x for x in out) and any("not confirmed" in x for x in out), out)

    nwrites = len(fake.writes)
    out = await send("SETMODE HEAT", 9)
    check("SETMODE HEAT confirmed", any("mode Heat: confirmed" in x for x in out) and "ThermostatMode_1=Heat" in out, out)
    check("entering Heat applies the saved heat setpoint 66",
          any("heat setpoint 66 F: confirmed" in x for x in out) and fake.vals[(2, 38)] == 19.0
          and len(fake.writes) == nwrites + 2, (fake.vals[(2, 38)], out))
    check("saved heat cleared once applied", "T_SAVED_SETPOINTS=" in out and "heat" not in t1.saved, t1.saved)
    out = await send("SETHEAT 69", 4.5)
    check("Heat mode SETHEAT 69 confirmed", any("heat setpoint 69 F: confirmed" in x for x in out), out)
    out = await send("SETRANGE 68,77", 5)
    check("SETRANGE in Heat mode: heat written, cool saved",
          any("heat setpoint 68 F: confirmed" in x for x in out) and any("cool setpoint 77 F: saved" in x for x in out)
          and "ThermostatCurrentCoolPoint_1=77" in out, out)
    check("saved cool survives a restart", b.Bridge(cfg, tmp).thermostats[0].saved.get("cool") == 77)
    out = await send("INCTEMP", 4.5)
    check("INCTEMP in Heat mode raises the heat setpoint 68 -> 69", any("heat setpoint 69 F: confirmed" in x for x in out), out)
    check("CurrentChangeOverStatus_1 false outside Auto", bridge.values.get("CurrentChangeOverStatus_1") == "false")
    check("ThermostatCurrentSetPoint_1 = active heat setpoint", bridge.values.get("ThermostatCurrentSetPoint_1") == "69",
          bridge.values.get("ThermostatCurrentSetPoint_1"))
    out = await send("RESETFILTER 1", 4.5)
    check("RESETFILTER 1 clears the reminder", "T_FILTER1_CHANGE=0" in out, out)
    out = await send("BOGUS")
    check("unknown command answered", any(x.startswith("SYS_ERROR=unknown command") for x in out), out)

    # event push from the thermostat (temperature change at the wall)
    start = len(lines)
    fake.vals[(2, 37)] = 21.0
    fake.emit({(2, 37): 21.0})
    await asyncio.sleep(0.3)
    check("push event updates temperature", "ThermostatCurrentTemperature_1=70" in lines[start:], lines[start:])

    # offline: SYS_ONLINE drops at once, mode Unknown only after the grace period
    bridge.cfg["offline_grace_seconds"] = 0.5
    fake.fail_reads = True
    t1.wake.set()
    await asyncio.sleep(0.4)
    t1.wake.set()
    await asyncio.sleep(1.0)
    t1.wake.set()
    await asyncio.sleep(6)
    cur = dict(ln.split("=", 1) for ln in lines if "=" in ln)
    check("offline: SYS_ONLINE=0 with an error", cur.get("SYS_ONLINE") == "0" and "unreachable" in cur.get("SYS_ERROR", ""), (cur.get("SYS_ONLINE"), cur.get("SYS_ERROR")))
    check("offline: after grace T_ONLINE=0, mode Unknown, temperature kept",
          cur.get("T_ONLINE") == "0" and cur.get("T_MODE") == "Unknown" and cur.get("T_TEMP") == "70",
          (cur.get("T_ONLINE"), cur.get("T_MODE"), cur.get("T_TEMP")))
    fake.fail_reads = False
    t1.wake.set()
    await asyncio.sleep(16)
    cur = dict(ln.split("=", 1) for ln in lines if "=" in ln)
    check("recovery: back online", cur.get("SYS_ONLINE") == "1" and cur.get("T_MODE") == "Heat" and cur.get("SYS_ERROR") == "",
          (cur.get("SYS_ONLINE"), cur.get("T_MODE"), cur.get("SYS_ERROR")))

    # access control
    bridge2 = b.Bridge(dict(cfg, allowed_clients=["10.9.9.9"]), tmp)
    check("allowed_clients: localhost always allowed", bridge2.client_allowed("127.0.0.1"))
    check("allowed_clients: other LAN host refused", not bridge2.client_allowed("192.168.1.30"))
    check("allowed_clients: listed host allowed", bridge2.client_allowed("10.9.9.9"))

    w.close()
    rt.cancel()
    bridge.request_stop("test")
    hap.cancel()
    srv.close()


# ---------------------------------------------------------------------------
def multi_unit_tests():
    check("key_for: thermostat 1 keeps 1.2 names",
          [b.key_for(1, k) for k in ("T_MODE", "SENSOR1_TEMP", "ThermostatMode_1")] == ["T_MODE", "SENSOR1_TEMP", "ThermostatMode_1"])
    check("key_for: thermostat 2 names",
          [b.key_for(2, k) for k in ("T_MODE", "SENSOR3_TEMP", "ThermostatMode_1", "IsCurrentHVACModeHeat_1")]
          == ["T2_MODE", "T2_SENSOR3_TEMP", "ThermostatMode_2", "IsCurrentHVACModeHeat_2"])
    d1, d3 = [k for k, _, _ in b.state_defs(4, 1)], [k for k, _, _ in b.state_defs(4, 3)]
    check("state_defs(4, 1) = 1.2's states plus the new ones", "T2_MODE" not in d1 and "T_PAIRED" in d1 and "SYS_THERMOSTAT_COUNT" in d1)
    check("state_defs(4, 3): no duplicates, thermostat 3 present",
          len(d3) == len(set(d3)) and {"ThermostatMode_3", "T3_MODE", "T3_SENSOR4_MOTION", "ThermostatCurrentSetPoint_3"} <= set(d3))
    check("state_defs scale per thermostat", len(d3) - len(d1) == 2 * (len(d1) - len(b.SYS_STATES)))
    tcs = b.thermostat_configs(dict(b.DEFAULT_CONFIG, thermostats=[{"ip": "10.0.0.5"}, "10.0.0.6"]))
    check("thermostats: list position = address; plain ip strings allowed",
          [(t["n"], t["ip"], t["port"]) for t in tcs] == [(1, "10.0.0.5", 80), (2, "10.0.0.6", 80)])
    for name, bad in (("duplicate ip", ["10.0.0.5", {"ip": "10.0.0.5"}]), ("missing ip", [{"port": 80}]),
                      ("17 thermostats", [f"10.0.0.{i}" for i in range(17)]), ("not a list", {"ip": "10.0.0.5"})):
        try:
            b.thermostat_configs(dict(b.DEFAULT_CONFIG, thermostats=bad))
            check(f"thermostats refused: {name}", False)
        except ValueError:
            check(f"thermostats refused: {name}", True)
    tmp = Path(tempfile.mkdtemp())
    cfg = dict(b.DEFAULT_CONFIG, thermostats=["10.0.0.5", "10.0.0.6", "10.0.0.7"])
    (tmp / "pairing.json").write_text("{}")
    check("pair with no number: first unpaired thermostat (2)", b.pick_thermostat(cfg, tmp, None, "pair")["n"] == 2)
    check("pair 3", b.pick_thermostat(cfg, tmp, 3, "pair")["ip"] == "10.0.0.7")
    for name, n, purpose in (("unpair needs a number with 3 thermostats", None, "unpair"), ("no thermostat 4", 4, "dump")):
        try:
            b.pick_thermostat(cfg, tmp, n, purpose)
            check(name, False)
        except SystemExit:
            check(name, True)
    check("pairing files: pairing.json, pairing-2.json",
          (b.file_for(tmp, "pairing", 1).name, b.file_for(tmp, "pairing", 2).name) == ("pairing.json", "pairing-2.json"))


async def multi_protocol_tests():
    tmp = Path(tempfile.mkdtemp())
    cfg = dict(b.DEFAULT_CONFIG, tcp_port=0, poll_seconds=15, confirm_seconds=6, allowed_clients="auto",
               thermostats=[{"ip": "192.168.1.50"}, {"ip": "192.168.1.51"}, {"ip": "192.168.1.52"}])
    bridge = b.Bridge(cfg, tmp)
    t1, t2, t3 = bridge.thermostats
    f1, f2 = FakeT10(ACCS), FakeT10(ACCS)
    f2.vals[(2, 36)] = 1                 # thermostat 2 is heating at 70 F
    f2.vals[(2, 37)] = 21.0
    for f in (f1, f2):
        f.vals[("orig", 43)], f.vals[("orig", 44)] = f.vals[(2, 43)], f.vals[(2, 44)]

    def fake_connect(t, fake):
        async def go():
            t.pairing = fake
            fake.dispatcher_connect(t.on_event)
            fake.dispatcher_availability_changed(t.on_available)
            t.model = b.Model(await fake.list_accessories_and_characteristics())
            await t.publish({"SYS_PAIRED": 1, "SYS_LIVE": 1})
            await t.set_online(True)
            await t.read_all()
            return True
        return go

    t1.connect, t2.connect = fake_connect(t1, f1), fake_connect(t2, f2)
    # thermostat 3 keeps the real connect(): no pairing-3.json, so it reports "not paired"
    await bridge.start()
    srv = await asyncio.start_server(bridge.handle_tcp, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    loops = [asyncio.ensure_future(t.hap_loop()) for t in (t1, t2, t3)]
    await asyncio.sleep(0.5)
    r, w = await asyncio.open_connection("127.0.0.1", port)
    lines = []

    async def reader():
        while True:
            ln = await r.readline()
            if not ln:
                return
            lines.append(ln.decode().strip())

    rt = asyncio.ensure_future(reader())

    async def send(cmd, wait=0.5):
        start = len(lines)
        w.write(cmd.encode() + b"\n")
        await w.drain()
        await asyncio.sleep(wait)
        return lines[start:]

    await asyncio.sleep(0.3)
    cur = lambda: dict(ln.split("=", 1) for ln in lines if "=" in ln)  # noqa: E731
    st = cur()
    check("3 thermostats: each has its own states",
          (st.get("ThermostatMode_1"), st.get("ThermostatMode_2"), st.get("T2_MODE"), st.get("T2_TEMP")) == ("Cool", "Heat", "Heat", "70"),
          (st.get("ThermostatMode_1"), st.get("ThermostatMode_2"), st.get("T2_MODE"), st.get("T2_TEMP")))
    check("3 thermostats: T2_IP, T2_SENSOR1_NAME, counts",
          st.get("T2_IP") == "192.168.1.51" and st.get("T2_SENSOR1_NAME") == "Master Bedroom Air Sensor 01"
          and st.get("SYS_THERMOSTAT_COUNT") == "3" and st.get("SYS_SENSOR_COUNT") == "2", st.get("SYS_SENSOR_COUNT"))
    check("unpaired thermostat 3: T3_PAIRED=0, SYS_PAIRED=0, SYS_ONLINE=0, error names it",
          st.get("T3_PAIRED") == "0" and st.get("T_PAIRED") == "1" and st.get("SYS_PAIRED") == "0" and st.get("SYS_ONLINE") == "0"
          and "thermostat 3: not paired: pair it on the T10Link web page (or t10link pair 3)" in st.get("SYS_ERROR", ""), st.get("SYS_ERROR"))
    check("no states for addresses 4+", not any(k.startswith(("T4_", "ThermostatMode_4")) for k in st))

    n1 = len(f1.writes)
    out = await send("@2 SETHEAT 71", 4.5)
    check("@2 SETHEAT 71 goes to thermostat 2 only", len(f1.writes) == n1 and f2.writes[-1] == [(2, 38, 21.5)], f2.writes[-1:])
    check("@2: T2_PENDING, then confirmed with the address in SYS_LAST_COMMAND",
          "T2_PENDING=heat setpoint 71 F" in out and "SYS_LAST_COMMAND=thermostat 2: heat setpoint 71 F: confirmed" in out, out)
    check("@2: ThermostatCurrentHeatPoint_2=71, thermostat 1 untouched",
          "ThermostatCurrentHeatPoint_2=71" in out and not any(x.startswith(("ThermostatCurrentHeatPoint_1", "T_PENDING")) for x in out), out)
    n2 = len(f2.writes)
    out = await send("SETCOOL 74", 4.5)
    check("no @: thermostat 1", len(f2.writes) == n2 and f1.writes[-1] == [(2, 38, 23.0)] and "ThermostatCurrentCoolPoint_1=74" in out, out)
    out = await send("@ SETMODE HEAT", 4.5)
    check("blank address (@ ): thermostat 1", "ThermostatMode_1=Heat" in out and "ThermostatMode_2=Heat" not in out, out)
    out = await send("@3 SETMODE COOL", 0.5)
    check("@3 (not paired): T3_ERROR, nothing written", "T3_ERROR=thermostat not connected" in out, out)
    out = await send("@4 PING")
    check("@4 (not in config): refused", any(x.startswith("SYS_ERROR=no thermostat 4 in config.json") for x in out), out)
    out = await send("@2:SETMODE:COOL", 4.5)     # the profile's form: no spaces
    check("@2:SETMODE:COOL (profile form) -> thermostat 2", "ThermostatMode_2=Cool" in out and f2.vals[(2, 36)] == 2, out)
    out = await send("@2:SETMODE:HEAT", 4.5)
    out = await send("@:INCHEAT", 4.5)           # blank address from an empty action argument
    check("@:INCHEAT (blank address) -> thermostat 1", any("thermostat 1: heat setpoint" in x for x in out), out)
    out = await send("@x SETMODE OFF")
    check("@x: refused", any(x.startswith("SYS_ERROR=no thermostat x") for x in out), out)
    out = await send("@2 SETCOOL 77", 0.5)
    check("@2 in Heat mode: cool saved for thermostat 2 only",
          "T2_SAVED_SETPOINTS=cool 77" in out and json.loads((tmp / "setpoints-2.json").read_text()).get("cool") == 77
          and "cool" not in t1.saved, out)
    out = await send("@2 CLEARSAVED", 0.5)
    check("@2 CLEARSAVED: thermostat 2 only", "T2_SAVED_SETPOINTS=" in out and not t2.saved, out)

    # thermostat 2 drops off Wi-Fi: thermostat 1 keeps working
    bridge.cfg["offline_grace_seconds"] = 0.5
    f2.fail_reads = True
    t2.wake.set()
    await asyncio.sleep(0.4)
    t2.wake.set()
    await asyncio.sleep(1.0)
    t2.wake.set()
    await asyncio.sleep(1.5)
    st = cur()
    check("thermostat 2 offline: T2_ONLINE=0, T_ONLINE=1, error names thermostat 2",
          st.get("T2_ONLINE") == "0" and st.get("T_ONLINE") == "1" and "thermostat 2: thermostat unreachable" in st.get("SYS_ERROR", ""),
          (st.get("T2_ONLINE"), st.get("T_ONLINE"), st.get("SYS_ERROR")))
    out = await send("SETHEAT 67", 4.5)          # thermostat 1 is in Heat now
    check("thermostat 1 still controllable while 2 is offline", any("heat setpoint 67 F: confirmed" in x for x in out), out)

    w.close()
    rt.cancel()
    bridge.request_stop("test")
    for t in loops:
        t.cancel()
    srv.close()


# ---------------------------------------------------------------------------
def profile_tests():
    """The generated Blueprint profile and the bridge agree: every command an action sends parses
    to the right thermostat and verb, and every state the bridge can send has a parser."""
    import xml.etree.ElementTree as ET
    xml = HERE.parent / "profile" / "blueprint-import" / "honeywell_t10link.xml"
    root = ET.parse(xml).getroot()
    sample = {"ThermostatAddress": "2", "Thermostat": "3", "HeatPointTemperature": "68", "CoolPointTemperature": "76",
              "Temperature": "70", "Mode": "HEAT", "Heat": "66", "Cool": "77"}
    sent = {}
    for act in root.iter("action"):
        cmd = act.find("command_interface/command")
        text = cmd.findtext("command_string") or ""
        for prm in cmd.iter("parameter"):
            text += sample[prm.get("action_argument")] if prm.get("action_argument") else (prm.text or "")
        sent[act.get("name")] = text
    want = {"SetHeatPointTemperature": ("2", "SETHEAT", "68"), "SetCoolPointTemperature": ("2", "SETCOOL", "76"),
            "SetHVACModeAuto": ("2", "SETMODE", "AUTO"), "SetHVACModeOff": ("2", "SETMODE", "OFF"),
            "IncreaseTemperatureAuto": ("2", "INCTEMP", ""), "SetTemperatureAuto": ("2", "SETTEMP", "70"),
            "DecreaseCoolPointTemperature": ("2", "DECCOOL", ""), "UpdateHVACStatus": (None, "DUMP", ""),
            "SetMode": ("3", "SETMODE", "HEAT"), "SetRange": ("3", "SETRANGE", "66,77"),
            "ResetFilter2": ("3", "RESETFILTER", "2"), "ClearSavedSetpoints": ("3", "CLEARSAVED", ""),
            "RefreshThermostat": ("3", "REFRESH", ""), "RefreshAllThermostats": (None, "REFRESH", ""),
            "Ping": (None, "PING", "")}
    bad = {k: (sent.get(k), b.parse_command(sent.get(k, ""))) for k, v in want.items() if b.parse_command(sent.get(k, "")) != v}
    check("profile actions -> bridge commands (address, verb, argument)", not bad, bad)
    check("no command parameter contains a space", not any(" " in (p.text or "") for p in root.iter("parameter")))
    hv = [a for a in root.iter("action") if a.find("action_argument[@name='ThermostatAddress']") is not None]
    check("every HVAC action sends its ThermostatAddress (except UpdateHVACStatus)",
          all(any(p.get("action_argument") == "ThermostatAddress" for p in a.iter("parameter"))
              for a in hv if a.get("name") != "UpdateHVACStatus"), [a.get("name") for a in hv])
    parsers = {m.get("name") for m in root.iter("status_message")}
    ent = root.find(".//entity[@name='Zone']")
    n = int(ent.get("max_entities"))
    check(f"profile: Zone entity takes {n} thermostats, parsers for all of them",
          n >= 2 and {f"ThermostatMode_{n}", f"T{n}_MODE", f"T{n}_SENSOR4_TEMP"} <= parsers and f"T{n + 1}_MODE" not in parsers)
    check("profile: parsers = state_defs exactly", parsers == {k for k, _, _ in b.state_defs(4, n)})


# ---------------------------------------------------------------------------
async def http(port, method, path, body=None, headers=None):
    r, w = await asyncio.open_connection("127.0.0.1", port)
    data = b"" if body is None else json.dumps(body).encode()
    hdr = {"Host": "t", "Content-Length": str(len(data)), **(headers or {})}
    w.write((f"{method} {path} HTTP/1.1\r\n" + "".join(f"{k}: {v}\r\n" for k, v in hdr.items()) + "\r\n").encode() + data)
    await w.drain()
    raw = await r.read()
    w.close()
    head, _, payload = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), head.decode(), payload


async def web_tests():
    import t10link_web as web
    tmp = Path(tempfile.mkdtemp())
    # a 1.2-style config.json: the page converts it to the thermostats list when it adds one
    (tmp / "config.json").write_text(json.dumps({"tcp_port": 2400, "thermostat_ip": "192.168.1.50",
                                                 "allowed_clients": ["auto", "192.168.1.20"]}))
    (tmp / "pairing.json").write_text("{}")
    cfg = b.load_config(tmp / "config.json")
    cfg.update(tcp_port=0, confirm_seconds=6)
    bridge = b.Bridge(cfg, tmp, tmp / "config.json")
    t1 = bridge.thermostats[0]
    f1, f2 = FakeT10(ACCS), FakeT10(ACCS)

    def fake_connect(t, fake):
        async def go():
            if not t.pairing_path.exists():
                await t.publish({"SYS_PAIRED": 0, "SYS_ERROR": f"not paired: run  {t.pair_cmd}  on the bridge machine"})
                return False
            t.pairing = fake
            fake.dispatcher_connect(t.on_event)
            t.model = b.Model(await fake.list_accessories_and_characteristics())
            await t.publish({"SYS_PAIRED": 1, "SYS_LIVE": 1})
            await t.set_online(True)
            await t.read_all()
            return True
        return go

    t1.connect = fake_connect(t1, f1)
    await bridge.start()
    bridge.start_loop(t1)
    page = web.WebPage(bridge)
    srv = await asyncio.start_server(page.handle, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    await asyncio.sleep(0.4)
    J = {"Content-Type": "application/json", "X-T10Link": "1"}

    code, _, body = await http(port, "GET", "/")
    check("web: page served", code == 200 and b"<title>T10Link</title>" in body)
    code, _, body = await http(port, "GET", "/api/status")
    st = json.loads(body)
    check("web: status shows thermostat 1 connected, Cool, 72", code == 200 and len(st["thermostats"]) == 1 and
          (st["thermostats"][0]["paired"], st["thermostats"][0]["connected"], st["thermostats"][0]["mode"],
           st["thermostats"][0]["temp"]) == (True, True, "Cool", "72"), st)
    check("web: status lists the room sensor", st["thermostats"][0]["sensors"][0]["name"] == "Master Bedroom Air Sensor 01")
    code, _, _ = await http(port, "POST", "/api/add", {"ip": "192.168.1.51"}, {"Content-Type": "application/json"})
    check("web: change without X-T10Link header refused (403)", code == 403 and len(bridge.thermostats) == 1)
    code, _, body = await http(port, "POST", "/api/add", {"ip": "not-an-ip"}, J)
    check("web: add refuses a bad IP", code == 400 and b"not an IP" in body, body)
    code, _, body = await http(port, "POST", "/api/add", {"ip": "192.168.1.51"}, J)
    raw = json.loads((tmp / "config.json").read_text())
    check("web: add thermostat 2", code == 200 and len(bridge.thermostats) == 2 and b"ThermostatAddress 2" in body, body)
    check("web: config.json converted to the thermostats list, other settings kept",
          raw.get("thermostats") == [{"ip": "192.168.1.50", "port": 80, "sensor_order": []}, {"ip": "192.168.1.51"}]
          and "thermostat_ip" not in raw and raw.get("allowed_clients") == ["auto", "192.168.1.20"], raw)
    check("web: the bridge reloads the same list", [t["ip"] for t in b.thermostat_configs(b.load_config(tmp / "config.json"))]
          == ["192.168.1.50", "192.168.1.51"])
    await asyncio.sleep(0.3)
    check("web: thermostat 2 states published, not paired",
          bridge.values.get("T2_IP") == "192.168.1.51" and bridge.values.get("T2_PAIRED") == "0"
          and bridge.values.get("SYS_THERMOSTAT_COUNT") == "2" and t1.tag == "thermostat 1: ",
          (bridge.values.get("T2_IP"), bridge.values.get("T2_PAIRED"), t1.tag))
    code, _, _ = await http(port, "POST", "/api/add", {"ip": "192.168.1.51"}, J)
    check("web: duplicate IP refused", code == 400)

    t2 = bridge.thermostats[1]
    t2.connect = fake_connect(t2, f2)
    code, _, body = await http(port, "POST", "/api/pair", {"n": 2, "code": "1234"}, J)
    check("web: pair refuses a short code before contacting the thermostat", code == 400 and b"8 digits" in body, body)
    real_q, real_pair = b.hap_local.query_hap_record, b.hap_local.pair
    seen = {}

    async def fake_pair(ip, pin, record=None):
        seen.update(ip=ip, pin=pin)
        return {"iOSPairingId": "test", "AccessoryPairingID": "AA:BB"}

    b.hap_local.query_hap_record = lambda ip, tries=8, timeout=2.0: {"name": "T10 Thermostat", "id": "AA:BB", "sf": "1", "md": "T10"}
    b.hap_local.pair = fake_pair
    try:
        code, _, body = await http(port, "POST", "/api/pair", {"n": 2, "code": "123-45-678"}, J)
        check("web: pair thermostat 2", code == 200 and seen == {"ip": "192.168.1.51", "pin": "123-45-678"}, (code, body, seen))
        pf = tmp / "pairing-2.json"
        check("web: pairing-2.json saved, mode 600, no setup code in it",
              pf.exists() and (pf.stat().st_mode & 0o777) == 0o600 and "123" not in pf.read_text())
        await asyncio.sleep(0.6)
        st = json.loads((await http(port, "GET", "/api/status"))[2])
        check("web: thermostat 2 connects straight after pairing",
              st["thermostats"][1]["connected"] and bridge.values.get("ThermostatMode_2") == "Cool"
              and bridge.values.get("SYS_PAIRED") == "1", st["thermostats"][1])
        code, _, body = await http(port, "POST", "/api/pair", {"n": 2, "code": "12345678"}, J)
        check("web: pairing an already paired thermostat refused", code == 400 and b"already paired" in body)
        await http(port, "POST", "/api/add", {"ip": "192.168.1.52"}, J)
        b.hap_local.query_hap_record = lambda ip, tries=8, timeout=2.0: {"name": "T10", "id": "CC", "sf": "0"}
        code, _, body = await http(port, "POST", "/api/pair", {"n": 3, "code": "12345678"}, J)
        check("web: thermostat already in Apple Home: explained, not paired",
              code == 400 and b"already paired to another HomeKit controller" in body and not (tmp / "pairing-3.json").exists(), body)
    finally:
        b.hap_local.query_hap_record, b.hap_local.pair = real_q, real_pair
    code, _, body = await http(port, "POST", "/api/remove", {"n": 3}, J)
    check("web: remove the last, unpaired thermostat",
          code == 200 and len(bridge.thermostats) == 2 and len(json.loads((tmp / "config.json").read_text())["thermostats"]) == 2
          and bridge.values.get("T3_IP") == "" and bridge.values.get("SYS_THERMOSTAT_COUNT") == "2", body)
    code, _, _ = await http(port, "GET", "/nope")
    check("web: unknown path 404", code == 404)

    # names: the T10's HomeKit name is read-only, so T10Link keeps its own in config.json
    check("name: none set -> T_NAME is the HomeKit name",
          bridge.values.get("T_NAME") == "T10 Thermostat" and bridge.values.get("T_HOMEKIT_NAME") == "T10 Thermostat")
    code, _, body = await http(port, "POST", "/api/name", {"n": 1, "name": "  Living\tRoom "}, J)
    raw = json.loads((tmp / "config.json").read_text())
    check("name: rename thermostat 1 -> T_NAME, saved in config.json, HomeKit name kept",
          code == 200 and bridge.values.get("T_NAME") == "Living Room" and bridge.values.get("T_HOMEKIT_NAME") == "T10 Thermostat"
          and raw["thermostats"][0].get("name") == "Living Room", (code, body, raw["thermostats"][0]))
    st = json.loads((await http(port, "GET", "/api/status"))[2])["thermostats"][0]
    check("name: status has name, custom_name and homekit_name",
          (st["name"], st["custom_name"], st["homekit_name"]) == ("Living Room", "Living Room", "T10 Thermostat"), st)
    check("name: survives a restart", b.Bridge(b.load_config(tmp / "config.json"), tmp).thermostats[0].name == "Living Room")
    code, _, _ = await http(port, "POST", "/api/name", {"n": 2, "name": "Caf\u00e9 Upstairs \u2603"}, J)
    await asyncio.sleep(0.2)
    check("name: accents folded to ASCII for Savant (T2_NAME)", code == 200 and bridge.values.get("T2_NAME") == "Cafe Upstairs",
          bridge.values.get("T2_NAME"))
    code, _, _ = await http(port, "POST", "/api/name", {"n": 1, "name": ""}, J)
    raw = json.loads((tmp / "config.json").read_text())
    check("name: blank -> back to the HomeKit name, removed from config.json",
          code == 200 and bridge.values.get("T_NAME") == "T10 Thermostat" and "name" not in raw["thermostats"][0])
    code, _, body = await http(port, "POST", "/api/add", {"ip": "192.168.1.53", "name": "Guest Suite"}, J)
    await asyncio.sleep(0.2)
    check("name: add with a name -> T3_NAME before it is even paired",
          code == 200 and bridge.values.get("T3_NAME") == "Guest Suite" and b"(Guest Suite)" in body
          and json.loads((tmp / "config.json").read_text())["thermostats"][2].get("name") == "Guest Suite", body)
    await http(port, "POST", "/api/remove", {"n": 3}, J)
    code, _, _ = await http(port, "POST", "/api/name", {"n": 9, "name": "x"}, J)
    check("name: unknown thermostat refused", code == 400)

    check("web_clients lan: private addresses yes, public no",
          page.client_allowed("192.168.7.9") and page.client_allowed("10.1.2.3") and not page.client_allowed("8.8.8.8"))
    bridge.cfg.update(web_clients=["10.9.9.0/24"], web_password="secret")
    strict = web.WebPage(bridge)
    check("web_clients list: only those (plus allowed_clients and this machine)",
          strict.client_allowed("10.9.9.4") and not strict.client_allowed("192.168.7.9")
          and strict.client_allowed("192.168.1.20") and strict.client_allowed("127.0.0.1"))
    srv2 = await asyncio.start_server(strict.handle, "127.0.0.1", 0)
    p2 = srv2.sockets[0].getsockname()[1]
    import base64
    code1, head, _ = await http(p2, "GET", "/")
    code2, _, _ = await http(p2, "GET", "/", headers={"Authorization": "Basic " + base64.b64encode(b"x:wrong").decode()})
    code3, _, _ = await http(p2, "GET", "/", headers={"Authorization": "Basic " + base64.b64encode(b"admin:secret").decode()})
    check("web_password: 401 without, 401 wrong, 200 right", (code1, code2, code3) == (401, 401, 200) and "WWW-Authenticate" in head)
    bridge.request_stop("test")
    for task in list(bridge.loops.values()):
        task.cancel()
    srv.close()
    srv2.close()


async def service_tests():
    """1.4: start with no thermostats, unpair / remove any / change IP / allow Savant / search."""
    import t10link_web as web
    tmp = Path(tempfile.mkdtemp())
    (tmp / "config.json").write_text(json.dumps({"tcp_port": 2400, "thermostats": [], "allowed_clients": "auto"}))
    cfg = b.load_config(tmp / "config.json")
    cfg.update(tcp_port=0, confirm_seconds=6)
    bridge = b.Bridge(cfg, tmp, tmp / "config.json")
    check("fresh install: no placeholder thermostat", bridge.thermostats == [])
    real = (b.Thermostat.connect, b.remove_remote_pairing, b.hap_local.scan_hap, b.hap_local.local_ipv4s, b.hap_local.mac_address)

    async def fake_connect(self):
        if not self.pairing_path.exists():
            await self.publish({"SYS_PAIRED": 0, "SYS_ERROR": "not paired"})
            return False
        self.pairing = FakeT10(ACCS)
        self.pairing.dispatcher_connect(self.on_event)
        self.model = b.Model(await self.pairing.list_accessories_and_characteristics())
        await self.publish({"SYS_PAIRED": 1, "SYS_LIVE": 1})
        await self.set_online(True)
        await self.read_all()
        return True
    calls, answer = [], {"err": ""}

    async def fake_remove(path, ip, port):
        calls.append((path.name, ip))
        return answer["err"]
    b.Thermostat.connect = fake_connect
    b.remove_remote_pairing = fake_remove
    try:
        await bridge.start()
        page = web.WebPage(bridge)
        srv = await asyncio.start_server(page.handle, "127.0.0.1", 0)
        port = srv.sockets[0].getsockname()[1]
        J = {"Content-Type": "application/json", "X-T10Link": "1"}
        st = json.loads((await http(port, "GET", "/api/status"))[2])
        check("no thermostats: page status empty, SYS_ERROR points to the web page, SYS_ONLINE 0",
              st["thermostats"] == [] and "web page" in bridge.values.get("SYS_ERROR", "")
              and bridge.values.get("SYS_ONLINE") == "0" and bridge.values.get("SYS_THERMOSTAT_COUNT") == "0", bridge.values)

        class W:
            out = b""

            def write(self, d):
                W.out += d

            async def drain(self):
                pass
        await bridge.handle_line("SETHEAT 70", W())
        await bridge.handle_line("PING", W())
        check("no thermostats: a command gets an error, PING still answers", b"no thermostats configured" in W.out and b"PONG" in W.out, W.out)

        for i in range(1, 5):
            await http(port, "POST", "/api/add", {"ip": f"192.168.1.5{i}", "name": f"Zone {i}"}, J)
        for n in (1, 2, 3):
            b.hap_local.write_private_json(b.file_for(tmp, "pairing", n), {"iOSPairingId": "t", "AccessoryPairingID": f"AA:0{n}"})
        b.file_for(tmp, "sensors", 3).write_text('{"s3": 1}')
        b.file_for(tmp, "setpoints", 4).write_text('{"heat": 64}')
        bridge.wake_all()
        await asyncio.sleep(0.5)
        check("4 thermostats added, 1-3 connected", len(bridge.thermostats) == 4 and
              [t.online for t in bridge.thermostats] == [True, True, True, False])

        code, _, body = await http(port, "POST", "/api/unpair", {"n": 2}, J)
        await asyncio.sleep(0.2)
        check("unpair 2: thermostat asked to forget the bridge, keys deleted, still in the list",
              code == 200 and calls[-1] == ("pairing-2.json", "192.168.1.52") and not b.file_for(tmp, "pairing", 2).exists()
              and len(bridge.thermostats) == 4 and bridge.values.get("T2_PAIRED") == "0"
              and not bridge.thermostats[1].online and bridge.values.get("SYS_PAIRED") == "0", body)
        code, _, body = await http(port, "POST", "/api/unpair", {"n": 2}, J)
        check("unpair a thermostat that isn't paired: refused", code == 400 and b"not paired" in body)

        answer["err"] = "TimeoutError"
        code, _, body = await http(port, "POST", "/api/unpair", {"n": 1}, J)
        r = json.loads(body)
        await asyncio.sleep(0.3)
        check("unpair, thermostat not answering: nothing changed, page offered force, it reconnects",
              code == 400 and r.get("unreachable") is True and b.file_for(tmp, "pairing", 1).exists()
              and bridge.thermostats[0].online, r)
        code, _, body = await http(port, "POST", "/api/unpair", {"n": 1, "force": True}, J)
        check("unpair with force: keys deleted, message says HomeKit Reset",
              code == 200 and not b.file_for(tmp, "pairing", 1).exists() and b"HomeKit Reset" in body, body)
        answer["err"] = ""

        code, _, body = await http(port, "POST", "/api/remove", {"n": 2}, J)
        await asyncio.sleep(0.4)
        raw = json.loads((tmp / "config.json").read_text())
        check("remove 2 of 4: later ones move up, config.json updated",
              code == 200 and [t["ip"] for t in raw["thermostats"]] == ["192.168.1.51", "192.168.1.53", "192.168.1.54"]
              and [t.ip for t in bridge.thermostats] == ["192.168.1.51", "192.168.1.53", "192.168.1.54"]
              and [t.n for t in bridge.thermostats] == [1, 2, 3], body)
        check("remove: message tells you to fix ThermostatAddress in Blueprint", b"ThermostatAddress" in body and b"3-4 are now 2-3" in body, body)
        check("remove: files moved with them (pairing-3 -> 2, sensors-3 -> 2, setpoints-4 -> 3)",
              json.loads(b.file_for(tmp, "pairing", 2).read_text()).get("AccessoryPairingID") == "AA:03"
              and b.file_for(tmp, "sensors", 2).read_text() == '{"s3": 1}' and not b.file_for(tmp, "sensors", 3).exists()
              and json.loads(b.file_for(tmp, "setpoints", 3).read_text()) == {"heat": 64} and not b.file_for(tmp, "pairing", 3).exists())
        check("remove: states renumbered, address 4 blanked",
              bridge.values.get("T2_IP") == "192.168.1.53" and bridge.values.get("T2_NAME") == "Zone 3"
              and bridge.values.get("T3_IP") == "192.168.1.54" and bridge.values.get("T4_IP") == ""
              and bridge.values.get("T4_ONLINE") == "0" and bridge.values.get("SYS_THERMOSTAT_COUNT") == "3"
              and bridge.thermostats[1].online and bridge.values.get("ThermostatMode_2") == "Cool", bridge.values.get("T4_IP"))
        check("remove: each thermostat's loop is its own", sorted(bridge.loops) == [1, 2, 3]
              and all(not task.done() for task in bridge.loops.values()))

        answer["err"] = "OSError: no route"
        code, _, body = await http(port, "POST", "/api/remove", {"n": 2}, J)
        check("remove a paired thermostat that doesn't answer: refused, offered force", code == 400
              and json.loads(body).get("unreachable") and len(bridge.thermostats) == 3)
        code, _, body = await http(port, "POST", "/api/remove", {"n": 2, "force": True}, J)
        check("remove with force", code == 200 and len(bridge.thermostats) == 2 and not b.file_for(tmp, "pairing", 2).exists()
              and bridge.thermostats[1].ip == "192.168.1.54", body)
        answer["err"] = ""

        code, _, body = await http(port, "POST", "/api/ip", {"n": 2, "ip": "192.168.1.51"}, J)
        check("change IP to another thermostat's: refused", code == 400 and b"already thermostat 1" in body)
        b.hap_local.write_private_json(b.file_for(tmp, "pairing", 2), {"iOSPairingId": "t", "AccessoryPairingID": "AA:04"})
        code, _, body = await http(port, "POST", "/api/ip", {"n": 2, "ip": "192.168.1.60"}, J)
        await asyncio.sleep(0.3)
        check("change IP: saved, T2_IP, reconnects with the same pairing",
              code == 200 and json.loads((tmp / "config.json").read_text())["thermostats"][1]["ip"] == "192.168.1.60"
              and bridge.values.get("T2_IP") == "192.168.1.60" and bridge.thermostats[1].online, body)

        bridge.rejected = ("192.168.1.20", "now")
        st = json.loads((await http(port, "GET", "/api/status"))[2])
        code, _, body = await http(port, "POST", "/api/allow", {"ip": "192.168.1.20"}, J)
        st2 = json.loads((await http(port, "GET", "/api/status"))[2])
        check("refused Savant host: shown, Allow adds it to allowed_clients",
              st["rejected"] == "192.168.1.20" and code == 200 and st2["rejected"] == ""
              and json.loads((tmp / "config.json").read_text())["allowed_clients"] == ["auto", "192.168.1.20"]
              and bridge.client_allowed("192.168.1.20"), (st["rejected"], body))

        b.hap_local.local_ipv4s = lambda: ["192.168.1.10"]
        b.hap_local.mac_address = lambda ip: "48:a2:e6:00:00:" + ip.split(".")[-1]
        seen_hosts = []

        def fake_scan(hosts, rounds=4, wait=1.5):
            seen_hosts.extend(hosts)
            return [{"address": "192.168.1.51", "name": "TSTAT-1", "md": "T9/T10 (+)", "ci": "2", "sf": "1", "id": "AA:01"},
                    {"address": "192.168.1.70", "name": "TSTAT-4", "md": "T9/T10 (+)", "ci": "2", "sf": "0", "id": "aa:04"},
                    {"address": "192.168.1.71", "name": "TSTAT-5", "md": "T9/T10 (+)", "ci": "2", "sf": "1", "id": "AA:05"},
                    {"address": "192.168.1.72", "name": "TSTAT-6", "md": "T9/T10 (+)", "ci": "2", "sf": "0", "id": "AA:06"},
                    {"address": "192.168.1.9", "name": "Smart Bridge Pro 2", "md": "Smart Bridge Pro 2", "ci": "2", "sf": "1"}]
        b.hap_local.scan_hap = fake_scan
        real_plat, real_after = b.sys.platform, b.MAC_RESCAN_AFTER
        b.sys.platform, b.MAC_RESCAN_AFTER = "linux", 0
        code, _, body = await http(port, "POST", "/api/scan", {}, J)
        r = json.loads(body)
        f = {h["ip"]: h for h in r.get("found", [])}
        check("search: thermostats only, sorted, with MAC", code == 200 and list(f) == ["192.168.1.51", "192.168.1.70", "192.168.1.71", "192.168.1.72"]
              and f["192.168.1.71"]["mac"] == "48:a2:e6:00:00:71" and len(seen_hosts) == 254, r)
        check("search: configured one marked, moved one recognised by its pairing, other controller flagged",
              f["192.168.1.51"]["thermostat"] == 1 and f["192.168.1.70"]["moved"] == 2 and not f["192.168.1.70"]["paired_elsewhere"]
              and f["192.168.1.72"]["paired_elsewhere"] and not f["192.168.1.71"]["paired_elsewhere"] and f["192.168.1.71"]["moved"] is None, f)
        # macOS: a thermostat asleep during the first pass (host marked down) is found by the second
        calls = []

        def sleepy_scan(hosts, rounds=4, wait=1.5):
            calls.append(len(hosts))
            if len(calls) == 1:
                return [{"address": "192.168.1.9", "name": "Smart Bridge Pro 2", "md": "Smart Bridge Pro 2", "ci": "2", "sf": "1"}]
            return [{"address": "192.168.1.9", "name": "Smart Bridge Pro 2", "md": "Smart Bridge Pro 2", "ci": "2", "sf": "1"},
                    {"address": "192.168.1.51", "name": "TSTAT-1", "md": "T9/T10 (+)", "ci": "2", "sf": "1", "id": "AA:01"}]
        b.hap_local.scan_hap = sleepy_scan
        b.sys.platform = "darwin"
        code, _, body = await http(port, "POST", "/api/scan", {}, J)
        r = json.loads(body)
        check("search on macOS: second pass finds a sleeping thermostat, skips hosts already found",
              code == 200 and [h["ip"] for h in r.get("found", [])] == ["192.168.1.51"] and calls == [254, 253], (calls, r))
        calls.clear()
        b.sys.platform = "linux"
        await http(port, "POST", "/api/scan", {}, J)
        check("search on Linux: one pass", calls == [254], calls)
        b.sys.platform, b.MAC_RESCAN_AFTER = real_plat, real_after
        code, _, _ = await http(port, "POST", "/api/scan", {}, {"Content-Type": "application/json"})
        check("search needs the X-T10Link header", code == 403)

        code, head, body = await http(port, "GET", "/profile.zip")
        import zipfile
        import io
        names = zipfile.ZipFile(io.BytesIO(body)).namelist() if code == 200 else []
        check("profile.zip: the Blueprint import folder", code == 200 and "application/zip" in head
              and "T10Link Blueprint profile/honeywell_t10link.xml" in names and "T10Link Blueprint profile/honeywell_t10link.htm" in names, names)

        for _ in range(2):
            await http(port, "POST", "/api/remove", {"n": 1, "force": True}, J)
        raw = json.loads((tmp / "config.json").read_text())
        check("remove them all: empty list saved, reloads as no thermostats",
              bridge.thermostats == [] and raw["thermostats"] == [] and b.Bridge(b.load_config(tmp / "config.json"), tmp).thermostats == []
              and bridge.values.get("T_IP") == "" and bridge.values.get("SYS_THERMOSTAT_COUNT") == "0", raw)
        bridge.request_stop("test")
        for task in list(bridge.loops.values()):
            task.cancel()
        srv.close()
    finally:
        (b.Thermostat.connect, b.remove_remote_pairing, b.hap_local.scan_hap, b.hap_local.local_ipv4s, b.hap_local.mac_address) = real


if __name__ == "__main__":
    unit_tests()
    asyncio.run(protocol_tests())
    print("\n-- several thermostats")
    multi_unit_tests()
    asyncio.run(multi_protocol_tests())
    print("\n-- Blueprint profile")
    profile_tests()
    print("\n-- web page")
    asyncio.run(web_tests())
    print("\n-- servicing (1.4)")
    asyncio.run(service_tests())
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
