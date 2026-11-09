# T10Link

Local Savant control of **Honeywell Home T9 / T10 / T10 Pro** thermostats. T10Link talks to the
thermostat with Apple's HomeKit protocol on your own network, so it needs no Honeywell/Resideo cloud
or account and keeps working when the internet is down.

```
Savant host  --TCP 2400-->  T10Link bridge (Mac, Raspberry Pi or Linux)  --HomeKit, LAN-->  T10 thermostat
```

- Savant's own climate screen: mode (Off / Heat / Cool / Auto), heat and cool setpoints, temperature,
  humidity, heating / cooling state, the dual-setpoint view in Auto.
- Several thermostats on one bridge and one Blueprint component: one HVAC data table row each
  (up to 8; 16 with a regenerated profile).
- A web page on the bridge for setup and service: finds thermostats on the network, pairs, names,
  re-addresses, unpairs and removes them, and hands out the Blueprint profile.
- Works with Savant's climate scheduler.
- RedLINK room sensors (temperature, humidity, motion), occupancy and filter reminders as states for
  triggers and custom screens.
- Every change is confirmed by the thermostat before the Savant app shows it.
- Not available over HomeKit: fan mode and hold.

Tested on a T10 Pro, firmware 1.5.3, with a RedLINK room sensor, Savant Pro 11.

## Install

1. On the machine that will run the bridge (a Mac, including the Savant host, or a Raspberry Pi /
   Linux box), run:

   ```
   curl -fsSL https://raw.githubusercontent.com/jwheeler188/T10Link/main/get.sh | bash
   ```

   It downloads the latest release, checks it, and installs the service. On a Mac without Python
   3.10+ it offers to install Python from python.org, framework only, so the `python3` that Savant
   and other apps use doesn't change. (Or download the zip from [Releases](../../releases) and run
   `./install.sh`.)
2. Open the web page it prints, `http://<bridge IP>:2401`, and:
   - give each thermostat a **DHCP reservation** in your router (the page's search shows its IP and
     MAC),
   - **Search the network**, add the thermostat, and **Pair** it with the code from
     **Menu > Connect HomeKit** on the thermostat. (It must not already be in Apple Home.)
3. Download the Blueprint profile from the same page and import it. Add one component pointing at
   the bridge's IP, port 2400, and one HVAC data table row per thermostat (Zone,
   ThermostatAddress 1, 2, 3 ..., Heat / Cool / Auto). If the bridge isn't on the Savant host,
   press **Allow** on the web page when Savant first connects.

The same web page unpairs, removes and re-addresses thermostats later.

The full manual is [MANUAL.md](MANUAL.md), also in the profile's Documentation tab in Blueprint.
Every state is listed in [STATES.md](STATES.md).

## What's in here

- `bridge/`: `t10link_bridge.py` (the service), `t10link_web.py` (its web page) and `hap_local.py` (HomeKit helpers on
  [aiohomekit](https://github.com/Jc2k/aiohomekit))
- `linux/`, `macos/`: installers, uninstallers and the `t10link` command
- `profile/`: the Blueprint profile and `generate_profile.py`, which builds it
- `tests/`: `mock_test.py` (simulated T10s built from a real accessory dump, one and three behind
  one bridge, plus a check of the generated profile; no thermostat or aiohomekit needed:
  `python3 -I tests/mock_test.py`) and `hap_probe.py` (diagnostics)

## License

MIT, see [LICENSE](LICENSE). Not affiliated with or endorsed by Honeywell, Resideo, Apple or Savant
Systems, Inc. HomeKit is a trademark of Apple Inc.
