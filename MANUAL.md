# T10Link manual

T10Link brings **Honeywell Home T9 / T10 / T10 Pro** thermostats into Savant **locally**. It uses
the Apple HomeKit protocol on your own network, so it doesn't need the Honeywell/Resideo cloud or a
Resideo account, and it keeps working when the internet is down.

The **T10Link bridge** is a small service that runs on the **Savant host Mac** itself, another Mac,
a **Raspberry Pi** or any Linux box that's always on. It is paired with each thermostat over HomeKit,
and it talks to the Savant host over a plain TCP connection on port **2400**. One bridge and one
Blueprint component handle up to 8 thermostats (16 with a regenerated profile).

> Not affiliated with or endorsed by Honeywell, Resideo or Savant Systems, Inc.

## Contents

- [What works](#what-works)
- [Requirements](#requirements)
- [Install the bridge](#install-the-bridge)
- [Pair the thermostat](#pair-the-thermostat)
- [Blueprint setup](#blueprint-setup)
- [Several thermostats](#several-thermostats)
- [Using it in Savant](#using-it-in-savant)
- [The t10link command](#the-t10link-command)
- [Troubleshooting](#troubleshooting)
- [How it works](#how-it-works)

## What works

Verified on a T10 Pro, firmware 1.5.3, with one RedLINK room sensor:

| Feature | Over HomeKit |
|---|---|
| Mode: Off / Heat / Cool / Auto | read and set |
| Heat and cool setpoints | read and set (see the note below) |
| Current temperature, humidity | read, pushed instantly |
| Heating / cooling / idle | read |
| RedLINK room sensors: temperature, humidity, motion, low battery | read |
| Occupancy (thermostat's own sensor) | read |
| Filter 1 / 2 change reminders | read and reset |
| Fan mode (Auto / On / Circulate) | **not available** |
| Hold / schedule override | **not available** |

> **Setpoints and mode.** Over HomeKit the T10 only takes the setpoint of the mode it's in: the
> heat setpoint in **Heat**, the cool setpoint in **Cool**, both in **Auto**. A setpoint for
> another mode (for example the heat setpoint from Savant's scheduler while it's cooling) is
> **saved by the bridge** and written as soon as the thermostat switches to that mode, whether
> the switch comes from Savant, the wall or the Honeywell app. Until then Savant shows the saved
> value, and `T_SAVED_SETPOINTS` lists what's waiting.

Every change is confirmed: the Savant app only shows a new setpoint or mode once the thermostat
reports it back. Until then `T_PENDING` says what is being changed, and if the thermostat doesn't
confirm within 20 seconds, `T_ERROR` says so.

## Requirements

- A Honeywell Home T9 or T10 (Pro / +) on Wi-Fi, **not** already paired with Apple Home. HomeKit
  allows one controller to do the first pairing. (Apple Home can't be added on top of T10Link
  through the thermostat's screen.)
- A machine for the bridge that's always on, on the same network as the thermostat, with
  **Python 3.10 or newer**:
  - **Mac** (the Savant host, or any other): macOS 11 or newer, Intel or Apple silicon. Apple's
    built-in `python3` is 3.9, so install Python 3.12 or newer from
    [python.org](https://www.python.org/downloads/macos/) first.
  - **Raspberry Pi**: Raspberry Pi OS Bookworm or newer, 32- or 64-bit (Python 3.11+ is built in).
    Any Pi from the 3B up is plenty. Other Linux: Debian 12+, Ubuntu 22.04+, with systemd.
- Internet access once, while installing, to download the HomeKit library (aiohomekit). After that
  everything stays on your network.
- A **DHCP reservation** for the thermostat on your router. The bridge connects by IP address.
- Savant Pro host. The profile works with any Savant version that has the HVAC service.

## Install the bridge

Download `T10Link-<version>.zip` from the GitHub release, copy it to the bridge machine and unzip it.
Re-running the installer upgrades in place and keeps `config.json`, the pairing, sensor numbering
and saved setpoints.

**Raspberry Pi / Linux** (as a normal user with sudo; the service runs as that user):

```
cd T10Link
sh linux/install.sh
```

This puts the bridge in `/opt/t10link`, creates a Python environment there with `aiohomekit` (a few
minutes on a Pi), installs the `t10link-bridge` systemd service (started now and at every boot) and
the `t10link` command. If Python's `venv` module is missing, it installs `python3-venv` with apt.

**Mac** (in Terminal; the service runs as you, or `--user RPM` on a Savant host):

```
cd T10Link
sudo ./macos/install.sh
```

This puts the bridge in `~/T10Link`, creates a Python environment there, installs a launchd service
(`org.t10link.bridge`, started now and at every boot), links the `t10link` command into
`/usr/local/bin`, and allows Python through the macOS firewall if the firewall is on.

The installer prints the IP address and port to use in Blueprint. Then edit `config.json`
(`/opt/t10link/config.json` on Linux, `~/T10Link/config.json` on a Mac):

| Setting | Meaning |
|---|---|
| `thermostats` | One entry per thermostat: `[{"ip": "192.168.1.50"}]`. `ip` is the thermostat's IP address (Menu > Wi-Fi on the thermostat shows it). Optional per entry: `port` (80) and `sensor_order`. The position in the list is the thermostat's number, 1, 2, 3 ... (see [Several thermostats](#several-thermostats)). Configs from 1.2 with `thermostat_ip` still work |
| `allowed_clients` | Who may connect. `["auto"]` (only this machine) is right when the bridge runs on the Savant host. Otherwise `["auto", "<Savant host IP>"]` |
| `tcp_port` | 2400 (the profile expects 2400) |
| `units` | `auto` (the thermostat's display units), `F` or `C` |
| `max_sensors` | Room-sensor slots per thermostat (4; the profile has states for 4) |
| `sensor_order` (in a `thermostats` entry) | Optional: fix sensor numbers by name, e.g. `["Master Bedroom Air Sensor 01"]` |
| `poll_seconds` | Safety poll (60). HomeKit pushes changes instantly anyway |
| `offline_grace_seconds` | Short Wi-Fi drops aren't shown in Savant (120) |

After editing: `t10link restart` (on a Mac: `sudo t10link restart`).

## Pair the thermostat

1. On the thermostat: **Menu > Connect HomeKit**. It shows an 8-digit code (123-45-678).
   **The code changes each time that screen opens**, so use the one showing now, not a photo.
2. On the bridge machine run `t10link pair` and type the code (it's hidden as you type).
   The thermostat's Wi-Fi sleeps a lot; if pairing can't find it, just run `t10link pair` again.
3. It says **Paired**. The bridge picks it up straight away: `t10link status` shows
   `SYS_ONLINE=1`.
4. With several thermostats, repeat for each one: `t10link pair` pairs the next one that isn't
   paired yet and says which; `t10link pair 2` pairs thermostat 2.

Only HomeKit's long-term pairing keys are saved (`pairing.json` in the install folder, and
`pairing-2.json`, `pairing-3.json` ... for the other thermostats; readable by the service user only). The setup code is never stored. The Honeywell Home / Resideo app keeps working
alongside.

To unpair: `t10link unpair` (`t10link unpair 2` for thermostat 2). If the pairing keys are lost, reset HomeKit on the thermostat
(Menu > Advanced Setup > Reset > HomeKit Reset) and pair again.

## Blueprint setup

1. Import `profile/blueprint-import/honeywell_t10link.xml`. The `.htm` and `.icns` beside it give
   the Documentation tab and the icon. Search for **T10Link** (or Honeywell T10).
2. Drag in one **Honeywell T10Link** component. Ethernet: **Device IP Address** = the machine
   running the bridge (the Savant host's own IP if it runs there), port 2400.
3. **HVAC data table**: one row per thermostat. For one thermostat:

| Column | Value |
|---|---|
| Controller | this T10Link component |
| Entity | Zone |
| ThermostatAddress [1] | 1 |
| Heat / Cool / Auto | tick what the system has |
| Fan | leave unticked (not available over HomeKit) |

4. Upload. Within a few seconds System Monitor > State Values shows `SYS_ONLINE` = 1 and
   `ThermostatCurrentTemperature_1` with the room temperature.

## Several thermostats

One bridge handles all the thermostats in the house, and Blueprint needs **one** T10Link component
for it. Each thermostat is one row in the HVAC data table. Don't add a second T10Link component
for the same bridge, and don't import the profile again.

1. List every thermostat in `config.json`, in the order you want to number them:

   ```
   "thermostats": [
     {"ip": "192.168.1.50"},
     {"ip": "192.168.1.51"},
     {"ip": "192.168.1.52"}
   ],
   ```

   The first is thermostat 1, the second thermostat 2, and so on. Keep the order once Savant is set
   up: the number ties each data table row to its thermostat. Add new thermostats at the end.
   Then `t10link restart`.
2. Pair each one: `t10link pair` (repeat it; it pairs the next unpaired thermostat), or
   `t10link pair 2` for a particular one. `t10link status` shows `T_PAIRED`, `T2_PAIRED` ...
3. In Blueprint's HVAC data table add one row per thermostat on the same T10Link component, with
   **ThermostatAddress [1]** = its number: 1, 2, 3 ... Each row gets its own Zone, and its own
   Heat / Cool / Auto ticks.

| Row | Controller | ThermostatAddress [1] |
|---|---|---|
| Living Room | T10Link | 1 |
| Primary Bedroom | T10Link | 2 |
| Upstairs | T10Link | 3 |

**State names.** Thermostat 1 keeps the names in Appendix A. Thermostat *n* has the same states with:

- Savant's address suffix: `ThermostatMode_2`, `ThermostatCurrentTemperature_2`,
  `IsCurrentHVACModeHeat_2`
- `Tn_` instead of `T_`: `T2_MODE`, `T2_TEMP`, `T2_ONLINE`, `T2_ERROR`
- `Tn_` in front of the room sensors: `T2_SENSOR1_TEMP`, `T2_SENSOR1_MOTION`

The `SYS_` states cover the whole bridge: `SYS_ONLINE` = 1 only when every thermostat is
connected, and `SYS_ERROR` names the thermostat, e.g. "thermostat 2: thermostat unreachable".
Gate an automation for one thermostat on its own `T_ONLINE` / `T2_ONLINE`.

**Custom actions** take a **Thermostat** argument (1, 2, 3 ...; blank = 1).

The shipped profile has states for 8 thermostats. For more (up to 16), regenerate it:
`python3 profile/generate_profile.py --thermostats 12`, then re-import it.

> Several thermostats were tested with simulated thermostats (built from a real T10's HomeKit
> data), and a single T10 Pro on real hardware.

## Using it in Savant

- **Climate screen**: mode, setpoints and temperature work from the Savant app's built-in HVAC
  screen.
- **Schedules**: HomeKit can't read or change the thermostat's own schedule, so run the schedule
  in Savant:
  1. **At the thermostat, turn its schedule off** (Menu > Schedule; switch the schedule off).
     Otherwise it changes the setpoints at its own program times, and Savant's changes only last
     until the next period.
  2. In the Savant app, set up the climate schedule for the zone. At each period Savant sends the
     heat and cool setpoints; the bridge writes the one the current mode uses and saves the other
     (see the setpoint note under [What works](#what-works)).
  3. Check once: after a scheduled change, the thermostat's screen should show the new setpoint
     with no "Hold" message.
- **Triggers and workflows**: use the `T_*` and `SENSORn_*` states (thermostat 2: `T2_*`,
  `T2_SENSORn_*`). Gate automations on `SYS_ONLINE` = 1 (or one thermostat's `T_ONLINE`). Examples:
  - `T_FILTER1_CHANGE` = 1 → send a notification.
  - `SENSOR1_MOTION` = 1 → a room is in use.
  - `T_STATE` = Heating / Cooling → show an icon on a custom screen.
- **Custom actions** (Workflows > Custom): `SetMode`, `SetHeatSetpoint`, `SetCoolSetpoint`,
  `SetRange`, `ClearSavedSetpoints`, `ResetFilter1`, `ResetFilter2`, `RefreshThermostat` (each with a
  Thermostat argument: 1, 2, 3 ..., blank = 1), `RefreshAllThermostats`, `RequestAllStates`.

Every state is listed in Appendix A.

## The t10link command

| Command | What it does |
|---|---|
| `t10link status` | Service state and the key states |
| `t10link states` | Every state the bridge is sending to Savant |
| `t10link send SETCOOL 74` | Send one command and show the reply (thermostat 2: `t10link send @2 SETCOOL 74`) |
| `t10link logs [n]` / `t10link logs -f` | Recent log lines / follow the log |
| `t10link restart` / `start` / `stop` | Control the service (on a Mac with `sudo`) |
| `t10link pair [N]` / `unpair [N]` | HomeKit pairing (N = thermostat number) |
| `t10link dump [N]` | Every HomeKit service and value thermostat N exposes (default 1) |
| `t10link version` | Bridge version |

Logs: `journalctl -u t10link-bridge` on Linux; `~/T10Link/logs/bridge.log` on a Mac.

**Uninstall**: `sudo /opt/t10link/uninstall.sh` (Linux) or `sudo t10link uninstall` (Mac). Add
`--keep-config` to save the settings and pairing first; reinstalling on a Mac restores them
automatically. The thermostat keeps the bridge's HomeKit pairing, so keep that backup, or run
`t10link unpair` before uninstalling if you won't be back.

Commands the bridge accepts on port 2400: `SETMODE OFF|HEAT|COOL|AUTO`, `SETHEAT <t>`,
`SETCOOL <t>`, `SETTEMP <t>` (active setpoint), `SETRANGE <heat>,<cool>`, `INCHEAT`, `DECHEAT`,
`INCCOOL`, `DECCOOL`, `RESETFILTER <1|2>`, `CLEARSAVED` (forget saved setpoints), `DUMP`, `REFRESH`, `PING`.
Put `@N ` in front for thermostat N (`@2 SETHEAT 70`); without it a command goes to thermostat 1
(`REFRESH` to all). The profile sends the same with colons, `@2:SETHEAT:70`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `SYS_PAIRED` = 0 | Run `t10link pair`. With several thermostats, `T_PAIRED` / `T2_PAIRED` ... show which one, and `t10link pair` pairs the next unpaired one |
| Pairing: "already PAIRED to another controller" | Remove the thermostat from Apple Home, or Menu > Advanced Setup > Reset > HomeKit Reset |
| Pairing fails with the right code | The code changes every time Connect HomeKit opens; reopen it and use the new one. After several wrong tries the thermostat locks pairing for a while |
| `SYS_ONLINE` = 0, `SYS_ERROR` = thermostat unreachable | `SYS_ERROR` names the thermostat when there are several. Check its `ip` in `config.json` (DHCP reservation), that the thermostat is on Wi-Fi, and `ping` it from the bridge. Its Wi-Fi sleeps: the first ping may fail |
| Mac install: "Python 3.10 or newer not found" | Install Python 3.12+ from python.org (Apple's `python3` is 3.9), then rerun the installer |
| Pi install: venv or pip errors | `sudo apt update && sudo apt install python3-venv`, then rerun. 32-bit Pi OS downloads ready-made packages from piwheels.org, so it needs that site reachable once |
| Component shows disconnected in Savant | Bridge not running (`t10link status`), wrong IP/port in Blueprint, the Savant host isn't in `allowed_clients` (`SYS_REJECTED_CLIENT` shows who was refused), or a Mac's firewall blocks Python |
| Savant shows a setpoint the thermostat doesn't | It's a saved setpoint for another mode (`T_SAVED_SETPOINTS`), applied when the mode switches. `t10link send CLEARSAVED` forgets it |
| `SYS_ERROR` = "no thermostat 3 in config.json" | A data table row (or a custom action) uses a ThermostatAddress that isn't in the bridge's `thermostats` list |
| A second thermostat's row does nothing | Check that it's on the same T10Link component, that its ThermostatAddress matches its position in `config.json`, and that it's paired (`T2_PAIRED` = 1) |
| Scheduled changes are undone later | The thermostat's own schedule is still on: turn it off at the thermostat |
| `T_ERROR` = "not confirmed by the thermostat" | The thermostat didn't accept the change (outside its limits, or locked at the wall). `t10link logs` |
| Documentation tab greyed out / "?" icon | Re-import with the `.xml`, `.htm` and `.icns` side by side in `blueprint-import/` |

## How it works

- HomeKit on the T10 is an encrypted HTTP connection to the thermostat on port 80. The bridge pairs
  once (SRP with the setup code), then opens an encrypted session with the saved keys and
  subscribes to push events. A safety poll runs every minute.
- The thermostat's Wi-Fi sleeps aggressively, and its multicast (Bonjour) announcements don't
  reliably reach the LAN. So the bridge never relies on discovery: it connects by IP and asks the
  thermostat directly (unicast mDNS) for its HomeKit record when pairing.
- HomeKit works in Celsius. When a Celsius setpoint is written, the T10 converts it to °F and
  rounds **up**. So for a whole °F the bridge writes the Celsius value that rounds up to exactly
  that °F (73 °F = 22.5 °C; 23.0 °C would become 74 °F).
- In Heat/Cool mode the thermostat's real setpoint is HomeKit's *target temperature*. Its
  heating/cooling thresholds hold the Auto values, and writes to them are reverted outside Auto.
  The bridge confirms a change only when the value still holds on a read two seconds later,
  because a rejected write is echoed once before the thermostat reverts it.
