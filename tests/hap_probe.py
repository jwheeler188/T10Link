"""Diagnostic: read (and optionally write) raw thermostat characteristics on the bridge machine.

    python -I hap_probe.py /opt/t10link                 read mode/temps/setpoints
    python -I hap_probe.py /opt/t10link 43=23.0 ...     write aid 2 iid=value pairs, then watch for 15 s
"""
import asyncio
import json
import sys
import time
from pathlib import Path

home = Path(sys.argv[1])
sys.path.insert(0, str(home))
import hap_local  # noqa: E402

NAMES = {35: "cur_state", 36: "tgt_mode", 37: "cur_temp", 38: "target", 43: "cool_th", 44: "heat_th"}
KEYS = [(2, i) for i in NAMES]


def c2f(c):
    return round(c * 9 / 5 + 32, 1) if isinstance(c, (int, float)) else c


async def main():
    cfg = json.loads((home / "config.json").read_text())
    data = hap_local.load_pairing(home / "pairing.json", cfg["thermostat_ip"], cfg.get("thermostat_port"))
    controller, azc = await hap_local.make_controller()
    p = controller.load_pairing("probe", data)
    events = []
    p.dispatcher_connect(lambda ev: events.append((time.strftime("%H:%M:%S"), ev)))
    try:
        await p.list_accessories_and_characteristics()

        async def show(tag):
            r = await p.get_characteristics(KEYS)
            print(tag, {NAMES[k[1]]: (v.get("value"), c2f(v.get("value")) if k[1] in (37, 38, 43, 44) else "")
                        for k, v in sorted(r.items())})

        await show("before")
        writes = [(2, int(a.split("=")[0]), float(a.split("=")[1])) for a in sys.argv[2:]]
        if writes:
            await p.subscribe(KEYS)
            print("write", writes, await p.put_characteristics(writes))
            for _ in range(5):
                await asyncio.sleep(3)
                await show(" t+")
            for t, ev in events:
                print("event", t, {NAMES.get(k[1], k): v.get("value") for k, v in ev.items()})
    finally:
        await p.close()
        await azc.async_close()


asyncio.run(main())
