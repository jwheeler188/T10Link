# T10Link

Local Savant control of **Honeywell Home T9 / T10 / T10 Pro** thermostats. T10Link talks to the
thermostat with Apple's HomeKit protocol on your own network, so it needs no Honeywell/Resideo cloud
or account and keeps working when the internet is down.

```
Savant host  --TCP 2400-->  T10Link bridge (Mac, Raspberry Pi or Linux)  --HomeKit, LAN-->  T10 thermostat
```

- Savant's own climate screen: mode (Off / Heat / Cool / Auto), heat and cool setpoints, temperature,
  humidity, heating / cooling state, the dual-setpoint view in Auto.
- Works with Savant's climate scheduler.
- RedLINK room sensors (temperature, humidity, motion), occupancy and filter reminders as states for
  triggers and custom screens.
- Every change is confirmed by the thermostat before the Savant app shows it.
- Not available over HomeKit: fan mode and hold.

Tested on a T10 Pro, firmware 1.5.3, with a RedLINK room sensor, Savant Pro 11.

## Install

1. Download `T10Link-<version>.zip` from [Releases](../../releases) and unzip it on the machine that
   will run the bridge (it needs Python 3.10+):
   - **Mac** (the Savant host is fine): install Python 3.12+ from
     [python.org](https://www.python.org/downloads/macos/), then `sudo ./macos/install.sh`
   - **Raspberry Pi OS** (Bookworm+, 32- or 64-bit) or other Linux with systemd: `sh linux/install.sh`
2. Set `thermostat_ip` in `config.json`, and if the bridge isn't on the Savant host, add the Savant
   host's IP to `allowed_clients`. Then `t10link restart`.
3. On the thermostat open **Menu > Connect HomeKit**, then run `t10link pair` and type the code.
   (The thermostat must not already be in Apple Home.)
4. In Blueprint, import `profile/blueprint-import/honeywell_t10link.xml` (with the `.htm` and
   `.icns` beside it), add the component, point it at the bridge's IP on port 2400 and fill in the
   HVAC data table (Zone, address 1, Heat / Cool / Auto).

The full manual is [MANUAL.md](MANUAL.md), also in the profile's Documentation tab in Blueprint.
Every state is listed in [STATES.md](STATES.md).

## What's in here

- `bridge/`: `t10link_bridge.py` (the service) and `hap_local.py` (HomeKit helpers on
  [aiohomekit](https://github.com/Jc2k/aiohomekit))
- `linux/`, `macos/`: installers, uninstallers and the `t10link` command
- `profile/`: the Blueprint profile and `generate_profile.py`, which builds it
- `tests/`: `mock_test.py` (a simulated T10 built from a real accessory dump; no thermostat or
  aiohomekit needed: `python3 -I tests/mock_test.py`) and `hap_probe.py` (diagnostics)

## License

MIT, see [LICENSE](LICENSE). Not affiliated with or endorsed by Honeywell, Resideo, Apple or Savant
Systems, Inc. HomeKit is a trademark of Apple Inc.
