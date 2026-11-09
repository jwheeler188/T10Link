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
- [The web page](#the-web-page)
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
    built-in `python3` is 3.9, so if there's no newer Python the installer offers to install
    Python 3.13 from python.org for you (see [Install the bridge](#install-the-bridge)).
  - **Raspberry Pi**: Raspberry Pi OS Bookworm or newer, 32- or 64-bit (Python 3.11+ is built in).
    Any Pi from the 3B up is plenty. Other Linux: Debian 12+, Ubuntu 22.04+, with systemd.
- Internet access once, while installing, to download the HomeKit library (aiohomekit). After that
  everything stays on your network.
- A **DHCP reservation** for each thermostat on your router (also called a static lease or fixed
  IP). The bridge connects by IP address, so if the router gives the thermostat a new address,
  Savant loses it. The web page's search shows each thermostat's IP and MAC address to reserve.
- Savant Pro host. The profile works with any Savant version that has the HVAC service.

## Install the bridge

**One command**, on the machine that will run the bridge (Terminal on a Mac; a Pi over ssh):

```
curl -fsSL https://raw.githubusercontent.com/jwheeler188/T10Link/main/get.sh | bash
```

It downloads the latest release from GitHub, checks its SHA-256, and runs the installer, which asks
for your password (sudo) when it needs it. On a **Savant host**, run it as the RPM user (it then
runs the service as RPM), or add `-s -- --user RPM`:

```
curl -fsSL https://raw.githubusercontent.com/jwheeler188/T10Link/main/get.sh | bash -s -- --user RPM
```

**Or from the zip**: download `T10Link-<version>.zip` from the GitHub release, unzip it and run
`./install.sh` in the `T10Link` folder (same options).

Re-running either one upgrades in place and keeps `config.json`, the pairing, sensor numbering and
saved setpoints.

What it does:

- **Mac**: puts the bridge in `~/T10Link` with its own Python environment, installs a launchd
  service (`org.t10link.bridge`, started now and at every boot), links the `t10link` command into
  `/usr/local/bin`, allows Python through the macOS firewall if the firewall is on, and opens the
  web page if you're sitting at the Mac.
  If there's no Python 3.10 or newer, it offers to install **Python 3.13 from python.org**
  (`--install-python` says yes in advance). That download is checked against a known SHA-256 and
  the Python Software Foundation's signature, and it's installed **framework only**: no `python3`
  command in `/usr/local/bin`, no PATH or shell profile changes, no apps. So on a Savant host,
  Savant's own scripts (which run whatever `python3` they find) keep using the one they use now.
- **Raspberry Pi / Linux**: puts the bridge in `/opt/t10link` with its own Python environment
  (`aiohomekit` takes a few minutes on a Pi), installs the `t10link-bridge` systemd service and the
  `t10link` command, and installs `python3-venv` with apt if it's missing. The service runs as the
  user who ran the installer.

The installer finishes by printing the **web page** address, `http://<bridge IP>:2401`. Everything
else is done there: see [Pair the thermostat](#pair-the-thermostat).

**If the bridge isn't on the Savant host**, it refuses Savant's first connection (only allowed
machines may control the thermostats). The web page then shows "Savant at ... was refused" with an
**Allow** button, which adds that address to `allowed_clients`.

### config.json

You rarely need to edit it: the web page adds, names, re-addresses and removes thermostats and
allows Savant. It's `/opt/t10link/config.json` on Linux, `~/T10Link/config.json` on a Mac.

| Setting | Meaning |
|---|---|
| `thermostats` | One entry per thermostat: `[{"ip": "192.168.1.50", "name": "Living Room"}]`. The position in the list is the thermostat's number, 1, 2, 3 ... (its ThermostatAddress, see [Several thermostats](#several-thermostats)). Optional per entry: `name` (shown as `T_NAME`; the T10's own HomeKit name can't be changed), `port` (80) and `sensor_order`. Empty on a new install. Configs from 1.2 with `thermostat_ip` still work |
| `allowed_clients` | Who may connect on port 2400. `["auto"]` = only this machine, right when the bridge runs on the Savant host. Otherwise add the Savant host's IP (the web page's **Allow** button does) |
| `tcp_port` | 2400 (the profile expects 2400) |
| `units` | `auto` (the thermostat's display units), `F` or `C` |
| `max_sensors` | Room-sensor slots per thermostat (4; the profile has states for 4) |
| `sensor_order` (in a `thermostats` entry) | Optional: fix sensor numbers by name, e.g. `["Master Bedroom Air Sensor 01"]` |
| `web_port` | The web page's port (2401; 0 turns the page off) |
| `web_clients` | Who may open the web page: `"lan"` (private network addresses: 10.x, 172.16-31.x, 192.168.x), `"any"`, or a list of IPs / networks. Machines in `allowed_clients` always may |
| `web_password` | Optional password for the web page (any user name) |
| `poll_seconds` | Safety poll (60). HomeKit pushes changes instantly anyway |
| `offline_grace_seconds` | Short Wi-Fi drops aren't shown in Savant (120) |

After editing by hand: `t10link restart` (on a Mac: `sudo t10link restart`).

## Pair the thermostat

On the web page, `http://<bridge IP>:2401`, from any computer or phone on your network:

1. **Reserve the thermostat's IP in your router first** (a DHCP reservation for its MAC address).
   **Search the network** lists every T9 / T10 it finds with its IP and MAC, so you can copy them
   into the router.
2. Press **Use** next to the thermostat, give it a name if you like ("Living Room"), tick
   **This IP is reserved in the router** and press **Add thermostat**. It gets the next number,
   which is its ThermostatAddress in Blueprint. (You can also type an IP: Menu > Wi-Fi on the
   thermostat shows it.)
3. On the thermostat open **Menu > Connect HomeKit**. It shows an 8-digit code (123-45-678).
   **The code changes each time that screen opens**, so use the one showing now.
4. Type it in the thermostat's card and press **Pair**. The thermostat's Wi-Fi sleeps a lot, so
   allow up to a minute. The card then shows the temperature and mode.

A thermostat that's in Apple Home (or another HomeKit app) can't be paired until it's removed
there; the search shows it as "Paired to another HomeKit app".

Only HomeKit's long-term pairing keys are saved (`pairing.json` in the install folder, and
`pairing-2.json`, `pairing-3.json` ... for the other thermostats; readable by the service user
only). The setup code is never stored. The Honeywell Home / Resideo app keeps working alongside.

On the command line instead: add the thermostat to `config.json`, then `t10link pair` (it pairs
the next unpaired thermostat; `t10link pair 2` pairs thermostat 2).

## The web page

`http://<bridge IP>:2401` (the installer prints it). It shows:

- **each thermostat**: number, name, IP, connected or not, temperature, mode, setpoints,
  humidity and room sensors, and a **Pair** box while it isn't paired
- **Rename**: the name shown on the page and in Savant (`T_NAME`), saved in `config.json`. A T10
  reports "T10 Thermostat" over HomeKit whatever the Honeywell Home app calls it, and HomeKit
  doesn't let anyone change that, so T10Link keeps its own name. Blank goes back to the HomeKit
  name (still in `T_HOMEKIT_NAME`).
- **Service** (on each thermostat):
  - **Change IP**: the same thermostat has a new address. The pairing is kept and the bridge
    reconnects at once. The search also spots a paired thermostat that moved ("Thermostat 2 moved
    here") and offers this in one click.
  - **Unpair**: the thermostat forgets the bridge and can be paired again, here or with Apple Home.
    It stays in the list with its number.
  - **Remove**: unpairs it (if paired) and takes it out of the list. Thermostats after it move up
    one number (3 becomes 2 ...), and the page reminds you to change their ThermostatAddress in
    Blueprint to match.
  - If the thermostat doesn't answer when unpairing or removing, nothing changes, and the page
    offers to delete the bridge's keys anyway. The thermostat then still lists the bridge, so do
    Menu > Advanced Setup > Reset > HomeKit Reset on it before pairing it again.
  - Its MAC address, for the router's DHCP reservation.
- **Add a thermostat**: the DHCP reservation reminder, **Search the network**, and the add form.
- **Savant setup**: the Device IP Address and port for Blueprint, whether Savant is connected,
  and a **Download the Blueprint profile** link (the import folder, zipped).
- A notice with an **Allow** button when a Savant host was refused.

Replacing a thermostat: **Unpair** the old one (or Remove, then Add), **Change IP** if the new
one has a different address, and pair the new one. Its number, and so the Savant data table row,
stays the same.

Who can open it: machines on your private network (`web_clients`), plus the machines in
`allowed_clients`. Anyone who can open it can also unpair or remove thermostats, so set
`web_password` in `config.json` if others share your network. The page uses plain HTTP on your
network, like the Savant connection; the setup code is only used once and never stored. To turn
the page off, set `web_port` to 0.

## Blueprint setup

1. Import `honeywell_t10link.xml`, from the web page's **Download the Blueprint profile** zip or
   `profile/blueprint-import/` in the release. The `.htm` and `.icns` beside it give the
   Documentation tab and the icon. Search for **T10Link** (or Honeywell T10).
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

1. Add and pair each one on the [web page](#the-web-page), in the order you want to number them.
   The first is thermostat 1, the second thermostat 2, and so on; new ones go at the end. The
   number ties each data table row to its thermostat, so if you remove one from the middle, the
   ones after it move up and their ThermostatAddress in Blueprint must change to match.
   (By hand: list them under `thermostats` in `config.json`, then `t10link restart` and
   `t10link pair` for each.)
2. In Blueprint's HVAC data table add one row per thermostat on the same T10Link component, with
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
automatically. The thermostat keeps the bridge's HomeKit pairing, so keep that backup, or unpair
the thermostats on the web page before uninstalling if you won't be back.

Commands the bridge accepts on port 2400: `SETMODE OFF|HEAT|COOL|AUTO`, `SETHEAT <t>`,
`SETCOOL <t>`, `SETTEMP <t>` (active setpoint), `SETRANGE <heat>,<cool>`, `INCHEAT`, `DECHEAT`,
`INCCOOL`, `DECCOOL`, `RESETFILTER <1|2>`, `CLEARSAVED` (forget saved setpoints), `DUMP`, `REFRESH`, `PING`.
Put `@N ` in front for thermostat N (`@2 SETHEAT 70`); without it a command goes to thermostat 1
(`REFRESH` to all). The profile sends the same with colons, `@2:SETHEAT:70`.

## Troubleshooting

| Symptom | Fix |
|---|---|
| `SYS_PAIRED` = 0 | Pair it on the web page. With several thermostats, `T_PAIRED` / `T2_PAIRED` ... show which one |
| `SYS_ERROR` = "no thermostats yet" | Add one on the web page |
| Pairing: "already PAIRED to another controller" | Remove the thermostat from Apple Home, or Menu > Advanced Setup > Reset > HomeKit Reset |
| Pairing fails with the right code | The code changes every time Connect HomeKit opens; reopen it and use the new one. After several wrong tries the thermostat locks pairing for a while |
| `SYS_ONLINE` = 0, `SYS_ERROR` = thermostat unreachable | `SYS_ERROR` names the thermostat when there are several. Search the network on the web page: if it has a new IP, the search offers to switch to it (then add a DHCP reservation). Otherwise check the thermostat is on Wi-Fi. Its Wi-Fi sleeps: the first try may fail |
| Search finds nothing | The bridge searches its own network(s) (/24) and the thermostats' networks. Check the thermostat is on Wi-Fi and on the same network as the bridge, search again (its Wi-Fi sleeps), or type its IP |
| Mac install: "Python 3.10 or newer is needed" | Rerun with `--install-python`, or install Python 3.12+ from python.org yourself (Apple's `python3` is 3.9) |
| Unpair / Remove: "didn't answer" | The thermostat is asleep or off Wi-Fi: try again in a minute. If it's gone for good, confirm the offer to delete the keys anyway |
| Pi install: venv or pip errors | `sudo apt update && sudo apt install python3-venv`, then rerun. 32-bit Pi OS downloads ready-made packages from piwheels.org, so it needs that site reachable once |
| Web page doesn't open | Use the bridge's IP and port 2401. Check `web_port`, that your computer is on the same network (`web_clients`), and `t10link logs` ("web page off" means the port was taken) |
| Component shows disconnected in Savant | Bridge not running (`t10link status`), wrong IP/port in Blueprint (the web page's Savant setup box shows both), the Savant host was refused (the web page shows **Allow**; `SYS_REJECTED_CLIENT` says who), or a Mac's firewall blocks Python |
| Savant shows a setpoint the thermostat doesn't | It's a saved setpoint for another mode (`T_SAVED_SETPOINTS`), applied when the mode switches. `t10link send CLEARSAVED` forgets it |
| `SYS_ERROR` = "no thermostat 3 in config.json" | A data table row (or a custom action) uses a ThermostatAddress that isn't in the bridge's `thermostats` list |
| A second thermostat's row does nothing | Check that it's on the same T10Link component, that its ThermostatAddress matches its number on the web page (after a Remove, the later ones moved up), and that it's paired (`T2_PAIRED` = 1) |
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
