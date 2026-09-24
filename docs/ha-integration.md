# The Home Assistant integration, Energy dashboard and gas statistic

*Split out of `CLAUDE.md` on 2026-09-13 to keep the always-loaded file small. This is the same text, except that site-specific values are placeholders whose real values are in the gitignored `CLAUDE.local.md`. The rules and traps that apply even when you are NOT reading this file stayed in `CLAUDE.md`.*

## Home Assistant: native HACS integration

`custom_components/solis_solarman/` is a config-flow, local-polling device integration requiring
`pysolarmanv5>=3.0.0`. The flow asks for logger host, serial, port (default 8899), and scan interval
(default 10 s, range 5-300 s); serial is the unique ID. It probes one complete blocking sweep in an
executor, closes that probe client, and refuses the entry if it cannot connect -- remember the
one-session limit before diagnosing the address. **There is no discovery and no reconfigure flow**
(`config_flow.py` has `async_step_user` and nothing else), so the host cannot be changed from the UI.

One `SolisClient` persists per entry, Modbus slave ID 1, 15 s socket timeout, every sweep off the
event loop through an executor. Any read error closes and nulls the Solarman session; the next
coordinator poll reconnects. A fresh first inverter refresh must succeed for setup to finish.
Unloading closes the client.

Forecasting is deliberately isolated in a second coordinator at 30-minute intervals, and its initial
refresh is non-fatal: Open-Meteo being down must make forecast entities unavailable, not take down
inverter telemetry. `energy.py`'s `async_get_solar_forecast()` is a cheap cache read from that
coordinator, because HA calls the provider on initial load, date changes, and hourly refreshes.

Live config entry host is **`<logger-ip>`** (value in `CLAUDE.local.md`), state `loaded` (verified 2026-09-13).

### Entities actually exposed

All entities belong to one "Solis inverter" device, manufacturer "Ginlong Solis", model "S6 hybrid
via Solarman logger": 21 inverter sensors, four forecast sensors, two binary sensors.

| Group | Entities and semantics |
|---|---|
| Live power | Solar power; SW and SE string power diagnostics; house load; **battery power positive charging / negative discharging**; **grid power positive importing / negative exporting** |
| Battery | SOC; SOH diagnostic; voltage/current diagnostics; battery-charging binary sensor, on only when direction says charge **and** battery power is at least 10 W |
| Daily energy | Solar, grid import, grid export, house consumption, battery charge, battery discharge: kWh with `total_increasing`, because inverter daily counters reset at midnight |
| Other inverter state | Solar yesterday; grid voltage; charge-current-limit, charge-window and decoded work-mode diagnostics; timed-charging diagnostic binary sensor |
| Forecast | Today kWh; tomorrow kWh with summary/sunrise/sunset attributes; low/borderline/high verdict with advice; tomorrow weather |

Forecast entities intentionally have no measurement/state class and must stay out of long-term
statistics; solar-yesterday likewise is not `total_increasing`. The integration reads work mode,
charge limit, and slot 1's charge start/end, but **exposes no control entities and reads no
discharge-window values**.

The sweep reads contiguous blocks where practical and preserves these conventions: battery power is
an unsigned u32 magnitude at 33149/33150 signed from 33135 (positive = charging in HA); meter power
at 33257/33258 is signed inverter-convention positive exporting and is negated (positive = importing
in HA); `pv1_power`/`pv2_power` are string V x A, with the SW/SE assignment matching the **inferred**
string mapping, not a traced cable. `charge_window` is "unset" only when all four slot-1 words are
zero. Work-mode names include self-use, time-of-use charging, off-grid, battery wake-up,
backup/reserve, grid charging allowed, feed-in priority and battery healing; unknown set bits remain
visible as `bitN`.

### Native Energy forecast

`energy.py` is discovered by module name and `async_get_solar_forecast()`. `forecast.py` builds a map
of timezone-aware Europe/London ISO hour to **interval Wh** for today and tomorrow, averaging each
hour across models that returned a complete, non-zero 24-hour day, applying the same 6.33 effective
kWp and 6 kW clipping as the daily model, and omitting zero-Wh hours -- so the hours sum to the
calibrated daily total. The verdict line is 25 kWh with +/-2 kWh uncertainty: within that band it must
say borderline rather than pretend to know.

**The plotted hourly peak reads about 20% low by construction**, because `EFFECTIVE_KWP` is fitted to
daily energy, not instantaneous noon power. That is accepted for an energy-per-hour forecast. Do not
"fix" the line by rescaling it; that would break the daily total.

Recalibration happens in the repository's standalone `solar_forecast.py`, and a changed fit reaches
HA only when the vendored copy is updated and released -- so **two copies of the calibration
constants exist and can drift silently**. Checked 2026-08-27: `LAT/LON`, `MODELS`, `EFFECTIVE_KWP`
6.33, `INVERTER_W` 6000, `LOW_KWH` 25.0, `UNCERTAINTY_KWH` 2.0 and both plane geometries agree. Since the
move to `.env`, `LAT/LON` is no longer a shared constant: the standalone reads `SITE_LATITUDE`/`SITE_LONGITUDE`
and the integration uses HA's home location, so leave it out of the parity check. Note
`PLANES` is **not** copy-pasteable between them -- the repo tuples carry a leading name field
(`("roof 1", 27, 21, 12/20)`) that the vendored ones omit. Re-check parity after any `calibrate` run.

## Home Assistant: Energy configuration and live view

The native integration -- not the REST sensors, and **never** the Octopus `previous_accumulative_*`
entities, which can place an entire day's consumption on the wrong day -- feeds the Energy dashboard.

- Grid import/export use the Solis daily counters. Import is priced by the Octopus current-rate
  entity; export is flat GBP 0.12/kWh.
- Solar uses the Solis daily counter plus the `solis_solarman` forecast provider.
- Battery uses the integration's charge/discharge daily counters. `battery_power` needs
  `stat_rate_inverted` because HA expects positive discharge while this integration uses positive
  charge.
- Gas uses `gas_hybrid:consumption_kwh` and `gas_hybrid:cost_gbp`, **not** the Octopus external
  statistics directly.
- Consumption cost is calculated per 10-second energy delta against the rate at that instant, giving
  near-exact peak/off-peak attribution. It excludes the standing charge.

`ha_energy_view.yaml` records the live storage dashboard `energy-live`, promoted to the sidebar label
"Energy". HA's built-in Energy panel is hidden per user, but its configuration remains at
`/config/energy`. **Storage-dashboard URL paths require a hyphen** (`energy-live`, not
`energy_live`, which fails with `config_not_found`). The YAML is **a record, not the source of
truth**: edit in HA, then re-export. Required HACS cards are Helios, ha-sankey-chart,
modern-circular-gauge, lovelace-plotly-graph-card and card-mod.

The view contains Helios, a three-column Sankey, HA's date/energy/gauge graphs, a battery SOC plot
with charge windows shaded, a 21-day hour-of-day house-load heatmap, and the grid-voltage chart. Card
order is free: moving the Sankey above `energy-date-selection` preserved date-scoped data.

### Gas: the hybrid statistic (2026-09-07)

The Octopus Home Mini was enabled in the account entry on 2026-09-07 (`supports_live_consumption`,
both refresh rates **5 minutes**). The Octopus API caps at 100 calls/hour and the gas meter only
reports half-hourly, so 5 costs nothing in resolution and leaves headroom; do not drop it to 1
without a reason. The account entry has **no options flow** (`supports_options` false,
`supports_reconfigure` true), so this is changed by a **reconfigure** flow
(`POST /api/config/config_entries/flow` with `entry_id`), which prefills every current value.

Two gas sources exist and neither alone is good enough:

- `octopus_energy:gas_..._previous_accumulative_consumption_kwh` (and `..._cost`) -- the **billed**
  half-hourly meter data, published as correctly-dated hourly external statistics. A day lands about
  **18-42 h late**: 2026-09-06's arrived 2026-09-07 at 18:21. The identically-named *entity* is the
  day-shifted one; the external statistic is the correct-by-day one.
- `sensor.octopus_energy_gas_..._current_total_consumption_kwh` -- the Mini's **lifetime** meter
  total, within minutes. Use this one, not `current_accumulative_consumption_kwh`: the accumulative
  sensor resets at midnight, and a `total` sensor resetting without `last_reset` is a statistics
  hazard. The lifetime total is `total_increasing` and needs no special handling.

`custom_components/gas_hybrid` (source of truth `ha_gas_hybrid/gas_hybrid/` in this repo) merges them
into `gas_hybrid:consumption_kwh` and `gas_hybrid:cost_gbp`. Every 30 minutes it rebuilds a rolling
8-day window hour by hour: **any finished local day the legacy series has published owns that day;
every other day comes from the Mini.** Re-importing an hour overwrites it, so a day estimated from
the Mini is silently replaced by the billed figures when they land. Service `gas_hybrid.merge` runs
it on demand; `{"full": true}` rebuilds the entire history from the earliest legacy row. Both inputs
and the target are discovered by pattern, so a meter or MPRN change needs no edit.

Four things about it are load-bearing:

- **Presence, not count, decides a legacy day.** Octopus publishes only the half-hours the meter
  reported: 2026-01-02 has three hours and no more will ever arrive. An earlier "at least 20 rows =
  complete" rule zeroed 26 such hours. Verified after the fix: **13583 of 13583 legacy hours
  reproduced exactly.**
- **The legacy series re-bases its cumulative sum, and negative changes are clamped to zero.** It has
  done so five times -- 2025-05-17, 2025-11-24, 2025-12-22, 2026-04-07, 2026-08-05 -- the worst
  reading **-20220.65 kWh** in a single hour. Summing its raw hourly `change` over the whole history
  gives 169.2 kWh instead of the real 31836. Gas cannot be un-burnt, so the clamp is right, and it
  makes the hybrid strictly better than its source. Those five hours are the *only* places the hybrid
  deliberately differs from legacy.
- **`async_add_external_statistics` queues a recorder job; it does not write synchronously.** Two
  separate "the fix didn't work" false alarms here were just reads racing the queue -- a full rebuild
  is ~24000 rows per series and the cost series is queued behind the consumption one. Give it a
  minute before verifying, and re-read before concluding anything.
- Cost matches the billed convention: it **includes the standing charge**, added once at local
  midnight on live days. Verified both ways -- 7.978 x 0.078367 + 0.284949 = 0.910 against a billed
  0.91, and 0.696 x 0.078367 + 0.284949 = 0.339 on the live side.

The changeover day is short by construction: the Mini's statistics only begin when it is enabled, so
2026-09-07 carries gas only from 21:45 until the billed day lands. Do not rescale anything to "fix"
it.

Backups: `scratchpad/energy_prefs_pre_gas_hybrid.json` holds the previous Energy preferences and
`/config/configuration.yaml.pre_gas_hybrid` the previous core config.

