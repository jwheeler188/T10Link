# Changelog

## 1.4.0 (2026-11-09)

Easier to install and to service.

- **One-command install**: `curl -fsSL https://raw.githubusercontent.com/jwheeler188/T10Link/main/get.sh | bash`
  downloads the latest release, checks its SHA-256 and installs. From the zip, `./install.sh`
  picks the Mac or Linux installer and asks for sudo itself.
- **Mac: Python is installed for you** if there's no 3.10+ (asks first; `--install-python` to
  skip the question): Python 3.13 from python.org, checked by SHA-256 and signature, framework
  only, so the `python3` command, PATH and shell profiles stay exactly as they were (Savant's own
  scripts are unaffected).
- A new install starts with **no thermostats** and no config editing: everything is on the web
  page. The installer prints the next steps and, on a Mac you're sitting at, opens the page.
- Web page:
  - **Search the network** finds T9 / T10 thermostats (unicast HomeKit queries on the bridge's
    /24 networks; about 6 s, or up to about 25 s on a Mac when a thermostat's Wi-Fi is asleep:
    macOS calls an unanswering host "down" for 20 s, so the Mac looks a second time) and shows each one's **IP and MAC address** for the router's DHCP
    reservation. It marks thermostats already added, ones paired to another HomeKit app, and a
    paired thermostat that **moved to a new IP** (one click to follow it).
  - The **DHCP reservation** step is a prominent first step of Add a thermostat, and Add needs
    "This IP is reserved in the router" ticked.
  - **Unpair** (the thermostat forgets the bridge; it keeps its number) and **Remove** (unpairs,
    then removes **any** thermostat; later ones move up one number, files included, and the page
    says which ThermostatAddress values to change). If the thermostat doesn't answer, nothing
    changes until you confirm deleting the bridge's keys anyway.
  - **Change IP** for a thermostat with a new address; the pairing is kept.
  - **Allow** button when a Savant host is refused (adds it to `allowed_clients`).
  - **Savant setup** box: Device IP Address, port, connection, and a **Blueprint profile
    download** (the installer now copies the profile next to the bridge).
  - Each thermostat's MAC address.
- `SYS_ERROR` says "no thermostats yet" until one is added; commands then get an error instead of
  failing.
- Profile: notes only (setup now points to the web page). Re-importing is optional.


## 1.3.1 (2026-11-08)

- **Thermostat names**: `"name"` per thermostat in `config.json`, or **Rename** on the web page
  (also when adding one). It's shown on the page and sent as `T_NAME` / `T2_NAME` ... A T10 always
  reports "T10 Thermostat" over HomeKit, whatever the Honeywell Home app calls it, and HomeKit
  doesn't allow changing it; that name is now `T_HOMEKIT_NAME`. Profile: one new state per
  thermostat (re-import).

## 1.3.0 (2026-11-08)

- **Several thermostats on one bridge and one Blueprint component** (up to 8 with the shipped
  profile, 16 with a regenerated one). List them under `thermostats` in `config.json`; each is one
  row in the HVAC data table with ThermostatAddress = its position in the list.
- HVAC actions now send the ThermostatAddress (`@2:SETHEAT:70`); custom actions take an optional
  Thermostat argument (blank = 1). Commands without an address still go to thermostat 1.
- Thermostat *n*'s states: `ThermostatMode_n`, `Tn_MODE`, `Tn_SENSOR1_TEMP` ... Thermostat 1 keeps
  every 1.2 state name.
- **Web page** on the bridge (`http://<bridge>:2401`): every thermostat's status, pairing with the
  code the thermostat shows, and adding a thermostat by IP without a restart. Private network only
  by default (`web_clients`), optional `web_password`; unpairing stays on the command line.
- `t10link pair [N]` / `unpair [N]` / `dump [N]`; `t10link pair` pairs the next unpaired thermostat.
  Thermostat 2's pairing is kept in `pairing-2.json`, and so on.
- New states: `T_IP`, `T_PAIRED`, `T_SENSOR_COUNT`, `SYS_THERMOSTAT_COUNT`. `SYS_ONLINE`,
  `SYS_PAIRED` and `SYS_LIVE` are 1 when they hold for every thermostat; `SYS_ERROR` names the
  thermostat; `SYS_SENSOR_COUNT` counts all of them.
- macOS: `t10link pair` / `unpair` / `dump` work on Intel Macs.
- Upgrading: re-run the installer (it keeps `config.json` and the pairing), then re-import the
  profile in Blueprint. A 1.2 `config.json` with `thermostat_ip` keeps working.

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
