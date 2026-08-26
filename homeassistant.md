# Home Assistant integration

The container (see `Dockerfile` / `docker-compose.yml`) exposes a read-only JSON
API — nothing in it can write to the inverter.

- `GET /api/state` — full cached sweep (telemetry, settings, forecast, car, cost)
- `GET /api/health` — 200 while sweeps are fresh, 503 otherwise

One HTTP call per scan feeds every sensor below via HA's `rest` integration.
Add to `configuration.yaml` (replace `SOLIS_HOST` with the container's host):

```yaml
rest:
  - resource: http://SOLIS_HOST:5051/api/state
    scan_interval: 30
    sensor:
      - name: Solis battery SOC
        unique_id: solis_battery_soc
        value_template: "{{ value_json.telemetry.soc }}"
        unit_of_measurement: "%"
        device_class: battery
        state_class: measurement
      - name: Solis battery power
        unique_id: solis_battery_power
        # signed: positive charging, negative discharging
        value_template: >-
          {{ value_json.telemetry.battery_power
             * (1 if value_json.telemetry.battery_charging else -1) }}
        unit_of_measurement: "W"
        device_class: power
        state_class: measurement
      - name: Solis PV power
        unique_id: solis_pv_power
        value_template: "{{ value_json.telemetry.pv_power }}"
        unit_of_measurement: "W"
        device_class: power
        state_class: measurement
      - name: Solis house load
        unique_id: solis_house_load
        value_template: "{{ value_json.telemetry.house_load }}"
        unit_of_measurement: "W"
        device_class: power
        state_class: measurement
      - name: Solis grid power
        unique_id: solis_grid_power
        # positive importing, negative exporting (dashboard convention)
        value_template: "{{ value_json.telemetry.grid_power }}"
        unit_of_measurement: "W"
        device_class: power
        state_class: measurement
      - name: Solis solar today
        unique_id: solis_solar_today
        value_template: "{{ value_json.telemetry.solar_today }}"
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: total_increasing
      - name: Solis grid import today
        unique_id: solis_grid_import_today
        value_template: "{{ value_json.telemetry.grid_import_today }}"
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: total_increasing
      - name: Solis grid export today
        unique_id: solis_grid_export_today
        value_template: "{{ value_json.telemetry.grid_export_today }}"
        unit_of_measurement: "kWh"
        device_class: energy
        state_class: total_increasing
      - name: Solis charge current
        unique_id: solis_charge_current
        value_template: "{{ value_json.settings.charge_current }}"
        unit_of_measurement: "A"
        state_class: measurement
      - name: Solis solar forecast tomorrow
        unique_id: solis_forecast_tomorrow
        value_template: "{{ value_json.forecast.kwh }}"
        unit_of_measurement: "kWh"
    binary_sensor:
      - name: Solis timed charging
        unique_id: solis_timed_charging
        # 43110 bit1
        value_template: "{{ (value_json.settings.mode | int) // 2 % 2 == 1 }}"
      - name: Solis data fresh
        unique_id: solis_data_fresh
        device_class: connectivity
        value_template: "{{ value_json.ok }}"
```

Notes:

- The battery/PV powers are DC-side registers and the grid figure comes from
  the meter — do not derive one from the others (see CLAUDE.md).
- The energy sensors reset at midnight on the inverter's clock;
  `total_increasing` handles the reset.
- `value_json.car` / `value_json.cost` are only present when the Octopus env
  vars are set on the container.
- The old browser dashboard still exists (`settings_dash.py`), but HA needs
  only this API.

## Energy dashboard (2026-08-27)

The `solis_solarman` HACS integration (this repo, `custom_components/`) is what
feeds HA's native Energy dashboard now - not the `rest:` sensors above, and
**not** the Octopus `previous_accumulative_*` entities, which lump a whole day's
consumption onto the wrong day and must never be used as an Energy source.

`v0.3.0` added the two pieces that were missing:

- `battery_charge_today` / `battery_discharge_today` sensors (registers 33163 /
  33167), so the Energy dashboard can account for the battery at all.
- `energy.py` exposing `async_get_solar_forecast()`. HA discovers a solar
  forecast provider purely by module name, so the calibrated five-model ensemble
  now draws the forecast line on the solar graph. `forecast.py::_wh_hours()`
  returns tz-aware ISO hour -> Wh for today and tomorrow, built from the same
  per-hour means as `_day_kwh`, so the hours sum to the calibrated daily total.
  The peaks read ~20% low by construction (`EFFECTIVE_KWP` is fitted to daily
  energy, not instantaneous power) - that is accepted for an energy-per-hour
  line, and is the same trap described in CLAUDE.md for `hourly_kw()`.

Energy prefs (set over `energy/save_prefs`): grid from/to are the Solis daily
counters priced by `sensor.octopus_..._current_rate`, export at a flat 0.12;
solar is the Solis counter plus the forecast entry; battery uses the two new
counters with `stat_rate_inverted` on `battery_power` (HA wants positive =
discharge, ours is positive = charge); gas stays on the Octopus *external
statistics*, which are backdated hourly and so land on the right day.
Cost is computed per consumption delta against the price entity at that instant,
so the 10 s poll gives near-exact off-peak/peak attribution. The **standing
charge is not included** - the dashboard shows unit cost only.

`ha_energy_view.yaml` is the exported Lovelace view (Overview -> Energy tab).
It is a record of what is live, not the source: edit in HA and re-export.
