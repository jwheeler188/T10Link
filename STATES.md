# T10Link states (v1.2.0)

Every state the bridge sends. Temperatures are in the thermostat's display units (`T_UNITS`).

## System

| State | Type | Meaning |
|---|---|---|
| `SYS_ONLINE` | integer | 1 = bridge has a live HomeKit session with the thermostat |
| `SYS_PAIRED` | integer | 1 = paired (pairing.json present). 0 = run: t10link pair |
| `SYS_LIVE` | integer | 1 = HomeKit push events subscribed (0 = polling only) |
| `SYS_LAST_UPDATE` | string | Time of the last value read from the thermostat |
| `SYS_LAST_COMMAND` | string | Last command from Savant and its result |
| `SYS_ERROR` | string | Last bridge error, blank when healthy |
| `SYS_VERSION` | string | Bridge version |
| `SYS_REJECTED_CLIENT` | string | Last connection refused by allowed_clients: IP and time |
| `SYS_SENSOR_COUNT` | integer | Room sensors found |

## Thermostat (T_...)

| State | Type | Meaning |
|---|---|---|
| `T_NAME` | string | Thermostat name (HomeKit) |
| `T_MODEL` | string | Model |
| `T_FIRMWARE` | string | Firmware version |
| `T_ONLINE` | integer | 1 = thermostat reachable (0 after offline_grace_seconds unreachable) |
| `T_MODE` | string | Off | Heat | Cool | Auto (Unknown while offline) |
| `T_STATE` | string | Idle | Heating | Cooling (Unknown while offline) |
| `T_TEMP` | string | Current temperature in T_UNITS (kept while offline) |
| `T_HEAT_SETPOINT` | string | Heat setpoint in T_UNITS |
| `T_COOL_SETPOINT` | string | Cool setpoint in T_UNITS |
| `T_TARGET` | string | Active target temperature (Heat or Cool mode), in T_UNITS |
| `T_HUMIDITY` | integer | Indoor humidity, percent |
| `T_UNITS` | string | F or C (the units every temperature state uses) |
| `T_SETPOINT_MIN` | string | Lowest allowed setpoint, in T_UNITS |
| `T_SETPOINT_MAX` | string | Highest allowed setpoint, in T_UNITS |
| `T_OCCUPIED` | integer | 1 = thermostat's own occupancy sensor sees someone |
| `T_FILTER1_CHANGE` | integer | 1 = filter 1 reminder is due |
| `T_FILTER2_CHANGE` | integer | 1 = filter 2 reminder is due |
| `T_PENDING` | string | What is being changed while a command is being confirmed, else blank |
| `T_SAVED_SETPOINTS` | string | Setpoints saved for another mode, applied when the thermostat switches to it (e.g. heat 68) |
| `T_ERROR` | string | Last command error, blank when fine |
| `T_LAST_CHANGE` | string | Time the mode or a setpoint last changed |

## Room sensors (SENSORn_..., n = 1 to 4)

| State | Type | Meaning |
|---|---|---|
| `SENSORn_PRESENT` | integer | 1 = a room sensor is assigned to this number |
| `SENSORn_NAME` | string | Sensor name (HomeKit) |
| `SENSORn_TEMP` | string | Temperature in T_UNITS |
| `SENSORn_HUMIDITY` | integer | Humidity, percent |
| `SENSORn_MOTION` | integer | 1 = motion detected |
| `SENSORn_LOW_BATTERY` | integer | 1 = battery low |
| `SENSORn_FAULT` | integer | 1 = sensor reports a fault |

## Savant HVAC service

| State | Type | Meaning |
|---|---|---|
| `ThermostatCurrentTemperature_1` | string | Current temperature |
| `ThermostatCurrentHeatPoint_1` | string | Heat setpoint |
| `ThermostatCurrentCoolPoint_1` | string | Cool setpoint |
| `ThermostatCurrentHumidity_1` | string | Indoor humidity |
| `ThermostatMode_1` | string | Off / Heat / Cool / Auto |
| `ThermostatCurrentSetPoint_1` | string | Active setpoint in Heat / Cool mode (blank in Auto / Off). Blueprint enables the HVAC data table's Auto column only for a profile that has this state |
| `CurrentChangeOverStatus_1` | string | true in Auto (auto changeover), as in Savant's Honeywell T-Series profile |
| `IsCurrentHVACModeOff_1` | boolean |  |
| `IsCurrentHVACModeHeat_1` | boolean |  |
| `IsCurrentHVACModeCool_1` | boolean |  |
| `IsCurrentHVACModeAuto_1` | boolean |  |
| `IsCurrentHVACStateIdle_1` | boolean |  |
| `IsCurrentHVACStateHeating_1` | boolean |  |
| `IsCurrentHVACStateCooling_1` | boolean |  |
| `IsW1RelayEnergized_1` | boolean | true while heating |
| `IsY1RelayEnergized_1` | boolean | true while cooling |
| `ThermostatTemperatureFormat_1` | string | F or C |
| `IsThermostatFormatFahrenheit_1` | boolean |  |
| `IsThermostatFormatCelsius_1` | boolean |  |
| `ThermostatCurrentHeatPointMinimum_1` | string |  |
| `ThermostatCurrentHeatPointMaximum_1` | string |  |
| `ThermostatCurrentCoolPointMinimum_1` | string |  |
| `ThermostatCurrentCoolPointMaximum_1` | string |  |
| `ThermostatCurrentRemoteTemperature_1` | string | Room sensor 1 temperature |
| `ThermostatCurrentRemoteHumidity_1` | string | Room sensor 1 humidity |
