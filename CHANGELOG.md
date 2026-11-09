# Changelog

## 1.2.0 (2026-11-08)

- The bridge runs on **macOS** as well as Linux: `sudo ./macos/install.sh` installs it as a launchd
  service in `~/T10Link`, with the same `t10link` command and an uninstaller. It can run on the
  Savant host itself. Needs Python 3.10+ (python.org); Intel and Apple silicon.
- **Raspberry Pi**: the Linux installer works on 32- and 64-bit Raspberry Pi OS (Bookworm and
  newer) and other Debian / Ubuntu systems. It checks for Python 3.10+, installs `python3-venv` if
  it's missing, refuses to run the service as root, checks the port is free, waits for the service
  to start, and records the installed package versions in `requirements.lock`. No compiler needed.
- New `linux/uninstall.sh` (also installed as `/opt/t10link/uninstall.sh`), with `--keep-config`.
- Profile 1.2.0: only the notes changed (where the bridge can run).

## 1.1.2 (2026-11-08)

- Auto can now be ticked in Blueprint's HVAC data table. Blueprint enables each column only when
  the profile has a matching state (Heat: `ThermostatCurrentHeatPoint`, Cool:
  `ThermostatCurrentCoolPoint`, Auto: `ThermostatCurrentSetPoint`, Humidity:
  `ThermostatCurrentHumidity`, External Temperature Sensor: `ThermostatCurrentRemoteTemperature`).
  The bridge now sends `ThermostatCurrentSetPoint_1`: the active setpoint in Heat / Cool, blank in
  Auto / Off. Profile 1.1.2 needs bridge 1.1.2.

## 1.1.1 (2026-11-08)

- HVAC section now matches Savant's own Honeywell T-Series profile for Auto (dual setpoint), to
  enable the Auto column in Blueprint's HVAC data table: added `IncreaseTemperatureAuto` /
  `DecreaseTemperatureAuto` (bridge commands `INCTEMP` / `DECTEMP`: the active setpoint, or both
  in Auto), and the `CurrentAutoChangeOverStatus` state variable bound to the new
  `CurrentChangeOverStatus_1` (true in Auto).

## 1.1.0 (2026-11-08)

- Works with Savant's climate scheduler. A setpoint for the mode the thermostat isn't in (the heat
  setpoint while cooling, say) is no longer refused: the bridge saves it (`setpoints.json`), shows it
  in Savant and writes it when the thermostat switches to that mode, from any source. SETRANGE
  outside Auto writes the active side and saves the other.
- New state `T_SAVED_SETPOINTS`, command `CLEARSAVED`, profile action `ClearSavedSetpoints`.
- Manual: schedules section (turn the thermostat's own schedule off; run it in Savant).

## 1.0.0 (2026-11-08)

First release.

- Local HomeKit (HAP over IP) control of a Honeywell T9/T10 through aiohomekit 4.0.1: mode,
  heat/cool setpoints, temperature, humidity, heating/cooling state, RedLINK room sensors
  (temperature, humidity, motion, low battery, fault), occupancy, filter reminders and reset.
- Pairing by IP with a unicast mDNS query, so no multicast discovery is needed (the T10's
  announcements don't reliably reach the LAN).
- Setpoint writes match the T10's behaviour (measured on firmware 1.5.3): the target temperature
  is used in Heat/Cool mode and the thresholds in Auto, and whole °F values round-trip exactly.
- Changes are confirmed only once the thermostat still reports them 2 s later.
- Savant profile `Honeywell T10Link` with the native HVAC service, on port 2400.
- Linux installer, systemd service, `t10link` command.
