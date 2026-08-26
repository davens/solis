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
