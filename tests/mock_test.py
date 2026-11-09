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
    cfg = dict(b.DEFAULT_CONFIG, tcp_port=0, poll_seconds=15, confirm_seconds=6, allowed_clients="auto")
    bridge = b.Bridge(cfg, tmp)
    fake = FakeT10(ACCS)
    # remember the original thresholds for the revert simulation
    fake.vals[("orig", 43)] = fake.vals[(2, 43)]
    fake.vals[("orig", 44)] = fake.vals[(2, 44)]

    async def fake_connect():
        bridge.pairing = fake
        fake.dispatcher_connect(bridge.on_event)
        fake.dispatcher_availability_changed(bridge.on_available)
        bridge.model = b.Model(await fake.list_accessories_and_characteristics())
        await bridge.publish({"SYS_PAIRED": 1, "SYS_LIVE": 1})
        await bridge.set_online(True)
        await bridge.read_all()
        return True

    bridge.connect = fake_connect
    bridge.stopping, bridge.wake = asyncio.Event(), asyncio.Event()
    await bridge.publish({"SYS_VERSION": b.VERSION})
    srv = await asyncio.start_server(bridge.handle_tcp, "127.0.0.1", 0)
    port = srv.sockets[0].getsockname()[1]
    hap = asyncio.ensure_future(bridge.hap_loop())
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
    check("saved heat cleared once applied", "T_SAVED_SETPOINTS=" in out and "heat" not in bridge.saved, bridge.saved)
    out = await send("SETHEAT 69", 4.5)
    check("Heat mode SETHEAT 69 confirmed", any("heat setpoint 69 F: confirmed" in x for x in out), out)
    out = await send("SETRANGE 68,77", 5)
    check("SETRANGE in Heat mode: heat written, cool saved",
          any("heat setpoint 68 F: confirmed" in x for x in out) and any("cool setpoint 77 F: saved" in x for x in out)
          and "ThermostatCurrentCoolPoint_1=77" in out, out)
    check("saved cool survives a restart", b.Bridge(cfg, tmp).saved.get("cool") == 77)
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
    bridge.wake.set()
    await asyncio.sleep(0.4)
    bridge.wake.set()
    await asyncio.sleep(1.0)
    bridge.wake.set()
    await asyncio.sleep(6)
    cur = dict(ln.split("=", 1) for ln in lines if "=" in ln)
    check("offline: SYS_ONLINE=0 with an error", cur.get("SYS_ONLINE") == "0" and "unreachable" in cur.get("SYS_ERROR", ""), (cur.get("SYS_ONLINE"), cur.get("SYS_ERROR")))
    check("offline: after grace T_ONLINE=0, mode Unknown, temperature kept",
          cur.get("T_ONLINE") == "0" and cur.get("T_MODE") == "Unknown" and cur.get("T_TEMP") == "70",
          (cur.get("T_ONLINE"), cur.get("T_MODE"), cur.get("T_TEMP")))
    fake.fail_reads = False
    bridge.wake.set()
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


if __name__ == "__main__":
    unit_tests()
    asyncio.run(protocol_tests())
    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    sys.exit(1 if FAIL else 0)
