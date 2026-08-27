# CLAUDE.md

This file is operational guidance for agents working on this repository. It records facts that were expensive to establish on live hardware. Preserve the distinction between **verified**, **owner-stated**, **inferred**, and **suspected**; do not turn one into another.

## What this is now

This is primarily a **Home Assistant deployment** for a Solis 6 kW hybrid inverter (model 0x3105), reached over Modbus through a Solarman/IGEN WiFi logger. The main artefact is custom_components/solis_solarman/, a HACS custom integration at v0.3.0: local read-only polling, config flow, battery daily counters, the calibrated solar forecast, and a native Energy-dashboard forecast provider. The Docker JSON API and old browser dashboard remain useful, and the standalone scripts still contain the only write path, but they are no longer the centre of gravity.

Hardware is **three Fox LV5200 modules in parallel**: about 300 Ah at 51.2 V, **about 15.4 kWh**, 5 kW delivery, 90% SOH. An earlier version of this file used 5.1 kWh, the capacity of one module, and made every fill-time estimate three times too short. Logger serial 0000000000, MAC 000000000000, Modbus port 8899. Do not bake a discovered IP into standalone code; use solis_net.py. The HA integration is different: its config entry requires an explicit host and does not call solis_net.py.

## Non-negotiable safety and truth rules

- **The HA integration, Docker API, and browser dashboard are read-only. Do not add a write path unless the owner explicitly asks.** control.py on the CLI is the only write path. A network-reachable control surface was removed deliberately on 2026-08-26.
- **Never write holding registers 43038-43049 or 43090-43097.** They are DNO-mandated G98/G99 protection: voltage/frequency trips and delays, including 262/184 V and 47.5/51.5 Hz. This is not merely risky; changing them is not permitted. Keep control.py's PROTECTED guard.
- **All three discharge windows are deliberately unset and must stay unset.** The owner does not want discharge to grid. Never propose enabling one or describe unused slots as exploitable headroom.
- **The intended cheap-rate charge window is fixed; charge current is the lever.** Its second job is preventing the house battery from feeding an unpredictably scheduled Tesla charge. Do not “optimise” by shortening the window.
- **The logger permits one Modbus session at a time.** A competing process commonly fails with bare _queue.Empty; that means contention, not necessarily a network fault.
- **Never derive grid flow from PV and battery power.** Those registers are DC-side and conversion loss becomes plausible-looking phantom export. Use the signed smart-meter pair at 33257/33258.
- **The HA integration deliberately inverts two sign conventions relative to the raw registers.** HA grid power is positive *importing* while register 33257 is positive *exporting*; HA battery power is positive *charging*, built from an unsigned u32 magnitude plus the 33135 direction flag. Both are correct as written. This is exactly the shape of thing an agent "helpfully" flips in the wrong direction -- check which side of the boundary you are on before touching a sign.
- **33135 is a direction flag, not current.** Current is 33134. Power comes from the u32 register pairs, not V × A.
- **The irradiance forecast is the mean of five models. Do not simplify it to one model or Open-Meteo best_match.** The ensemble materially outperformed every single source, and which individual model is worst changes.

## Home Assistant: native HACS integration

custom_components/solis_solarman/ is a config-flow, local-polling device integration requiring pysolarmanv5>=3.0.0. The flow asks for logger host, serial, port (default 8899), and scan interval (default 10 s, accepted range 5-300 s). Serial is the unique ID. The flow probes one complete blocking sweep in an executor, closes that probe client, and refuses the entry if it cannot connect; remember the one-session limit before diagnosing the address. There is no discovery or reconfigure flow in the supplied source.

One SolisClient persists for the entry, using Modbus slave ID 1 and a 15 s socket timeout. Every inverter sweep runs off the HA event loop through an executor. Any read error closes and nulls the Solarman session; the next coordinator poll reconnects. A fresh first inverter refresh must succeed for setup to finish. Unloading the entry closes the client.

Forecasting is deliberately isolated in a second coordinator at 30-minute intervals. Its initial refresh is non-fatal: Open-Meteo being down must make forecast entities unavailable, not take down inverter telemetry. energy.py's async_get_solar_forecast() is a cheap cache read from this coordinator, because HA calls the provider on initial load, date changes, and hourly refreshes.

### Entities actually exposed

All entities belong to one “Solis inverter” device, manufacturer “Ginlong Solis”, model “S6 hybrid via Solarman logger”. There are 21 inverter sensors, four forecast sensors, and two binary sensors:

| Group | Entities and semantics |
|---|---|
| Live power | Solar power; SW and SE string power diagnostics; house load; **battery power positive charging / negative discharging**; **grid power positive importing / negative exporting** |
| Battery | SOC; SOH diagnostic; voltage/current diagnostics; battery-charging binary sensor. The binary turns on only when direction says charge and battery power is at least 10 W. |
| Daily energy | Solar, grid import, grid export, house consumption, battery charge, battery discharge: kWh with total_increasing, because inverter daily counters reset at midnight |
| Other inverter state | Solar yesterday; grid voltage; charge-current-limit, charge-window, and decoded work-mode diagnostics; timed-charging diagnostic binary sensor |
| Forecast | Today kWh; tomorrow kWh with summary/sunrise/sunset attributes; low/borderline/high verdict with advice; tomorrow weather |

Forecast entities intentionally have no measurement/state class and must stay out of long-term statistics. Solar-yesterday likewise is not a total_increasing entity. The integration reads work mode, charge limit, and slot 1's charge start/end, but exposes no control entities and reads no discharge-window values.

The Modbus sweep reads contiguous blocks where practical and preserves these sign conventions:

- Battery power is an unsigned u32 magnitude at 33149/33150, signed from 33135: positive in HA means charging.
- Meter power at 33257/33258 is signed with inverter convention positive exporting; the integration negates it, so HA grid power is positive importing.
- pv1_power and pv2_power are calculated from string V × A. The SW/SE assignment matches the existing inferred string mapping, not a traced cable.
- charge_window is “unset” only when all four slot-1 words are zero. Work-mode names include self-use, time-of-use charging, off-grid, battery wake-up, backup/reserve, grid charging allowed, feed-in priority, and battery healing; unknown set bits remain visible as bitN.

### Native Energy forecast

energy.py is discovered by module name and async_get_solar_forecast(). forecast.py builds a map of timezone-aware Europe/London ISO hour to **interval Wh**, for today and tomorrow. It averages each hour across models that returned a complete, non-zero 24-hour day, applies the same 6.33 effective kWp and 6 kW clipping as the daily model, and omits zero-Wh hours. The hours therefore sum to the calibrated daily total. The verdict line is 25 kWh with ±2 kWh uncertainty: within that band it must say borderline rather than pretend to know.

The plotted hourly peak reads about 20% low by construction: EFFECTIVE_KWP is fitted to daily energy, not instantaneous noon power. That is accepted for an energy-per-hour forecast. Do not “fix” the line by rescaling it; that would break the daily total.

Recalibration still happens in the repository's standalone solar_forecast.py. A changed fit reaches HA only when the vendored integration copy is updated and released. That means **two copies of the calibration constants exist and can drift silently**. Checked 2026-08-27: `LAT/LON`, `MODELS`, `EFFECTIVE_KWP` 6.33, `INVERTER_W` 6000, `LOW_KWH` 25.0, `UNCERTAINTY_KWH` 2.0 and both plane geometries agree. Note `PLANES` is not copy-pasteable between them -- the repo tuples carry a leading name field (`("roof 1", 27, 21, 12/20)`) that the vendored ones omit. Re-check parity after any `calibrate` run.

## Home Assistant: Energy configuration and live view

The native integration, not the REST sensors and not Octopus previous_accumulative_* entities, feeds the Energy dashboard. Those Octopus entities can place an entire day's consumption on the wrong day and must never be Energy sources.

Current Energy preferences:

- Grid import/export use the Solis daily counters. Import is priced by the Octopus current-rate entity; export is a flat £0.12/kWh.
- Solar uses the Solis daily counter plus the solis_solarman forecast provider.
- Battery uses the integration's charge/discharge daily counters. battery_power needs stat_rate_inverted because HA expects positive discharge while this integration uses positive charge.
- Gas stays on Octopus external statistics, which are backdated hourly onto the correct day.
- Consumption cost is calculated per 10-second energy delta against the rate at that instant, giving near-exact peak/off-peak attribution. It excludes the standing charge.

ha_energy_view.yaml records the live storage dashboard energy-live, promoted to the sidebar label “Energy”. HA's built-in Energy panel is hidden per user, but its configuration remains at /config/energy. Storage-dashboard URL paths require a hyphen. The YAML is **a record, not the source of truth**: edit in HA, then re-export. Required HACS cards are Helios, ha-sankey-chart, modern-circular-gauge, lovelace-plotly-graph-card, and card-mod.

The view contains Helios, a four-column Sankey, HA's date/energy/gauge graphs, a battery SOC plot with charge windows shaded, a 21-day hour-of-day house-load heatmap, and the grid-voltage chart. Card order is free: moving the Sankey above energy-date-selection preserved date-scoped data.

### Sankey: presentation, not measurement

**The source-to-sink split is not measured.** The inverter provides totals—solar, import, battery in/out, export, house—not which source fed which sink. ha-sankey-chart 6.3.0 has no flow solver. It walks source nodes in node order and links in declaration order, allocating:

    connection = min(source unspent remainder, sink unfilled remainder)

sort_by moves boxes only after allocation. Reordering a link changes the story, not the underlying energy.

This produced two deceptive but plausible failures:

- A starved sink becomes a floating box. Its sensor state draws the node, but a zero allocation draws no ribbon. Solar previously fed battery then house before export, so house consumed the remainder and export floated despite its 5.1 kWh state.
- remaining_parent_state is not an arithmetic balance; it sums greedy upstream leftovers. It showed 3.4 kWh “Untracked / losses” when the real residual was 0.1.

Link order is therefore constrained by physics: **solar → export first, then battery in, then house as the elastic sink**. The battery-to-grid link was removed because all discharge windows are unset; merely declaring it fabricated battery export. Do not reorder for appearance without checking that no constrained sink starves.

“Unaccounted” is now a plain entity node computing solar + import + battery_out − house − battery_in − export with add_entities/subtract_entities. Greedy allocation can display no more than this real residual. This was verified against 16.3 + 14.0 + 4.4 = 34.7 kWh in and 22.3 + 5.6 + 6.7 + 0.1 = 34.7 out, and confirmed that add/subtract use Recorder change values under energy_date_selection.

House then splits into Tesla, air conditioning, and a remaining_parent_state “Rest of house”. Device consumption already contains Tesla and AC, so the Energy dashboard remainder is genuinely the rest of the house: 19.0 kWh derived versus 18.8 kWh from the inverter on 2026-08-26. The two built-in device-detail graph variants were tried and removed; the owner wants this breakdown only in the Sankey.

The split remains an editorially constrained presentation even after those fixes. A fully honest “Supply total” hub was considered and rejected because the purpose is to see solar reach the battery.

Tesla energy is a left-Riemann integral with max_sub_interval 60 s over charger voltage × current gated to 180-280 V; TeslaMate publishes current on change, and one 32 A plateau went 80 minutes without an update. It reads about 6% above the Tesla app because it measures AC-side house load while the app measures the pack; roughly 94% onboard-charger efficiency explains the real loss. The voltage band excludes Superchargers but cannot distinguish home from another AC charger, which is acceptable because the owner charges only at home. The idle voltage reads about 2 V -- a TeslaMate artifact -- so the gate needs both `i > 0` **and** `180 <= v <= 280`; voltage alone is not enough. Without a device tracker, "home" rests entirely on that voltage test, which is the known hole. The chain was verified end to end on 2026-08-27: 7.88 kWh reached the pack against 8.384 kWh measured on the AC side -- a 6.0% charger loss -- over three sessions (00:30-01:00, 02:00-02:30, 05:00-05:16) taking SOC 63 -> 67 -> 72 -> 75%. Neither Tesla nor AC had statistics before 2026-08-27, so no backfill exists. The LG sensor updates in hourly lumps; a flat hour is cloud polling cadence, not proof the AC is off.

### What the Sankey (and every statistics card) actually reads

Established 2026-08-27 by reading ha-sankey-chart 6.3.0's own source. This governs any card driven
by the Energy date selector, not just the Sankey.

With `energy_date_selection: true` the card fetches Recorder long-term statistics and **replaces
every node's live state** with `sum(change)` over the selected range. It requests only the `change`
column and never a row's `state`. The period comes from the range length alone -- `hour` up to two
days, `day` up to 35, `month` beyond. **There is no five-minute period and no `statistics_period`
option in 6.3.0**; the `throttle` option is unreachable in this branch.

Three consequences that all look like faults and are not:

- **A node that shows a higher number for a moment on refresh, then drops, is correct twice.** First
  paint falls back to `this.hass.states` because the statistics map is still empty; the lower figure
  is the settled statistics sum.
- **A sensor's first hourly statistics row carries `change = 0`.** Whatever was already on the meter
  when Recorder began tracking it is permanently missing from that day's `sum(change)`. A sensor
  created mid-day is short by its reading at creation -- for that day only, since the next midnight
  reset is observed properly.
- **Hour N compiles after N+1:00** (typically about :12), so the card runs up to an hour behind.

Worked example, air conditioning on 2026-08-27: live 389 Wh rendered 0.4 kWh, settled statistics
0 + 68 + 62 = 130 Wh rendered 0.1 kWh. The 259 Wh gap is 132 Wh of baseline lost to the first
`change = 0` row plus 127 Wh sitting in the uncompiled hour. That 127 Wh did exist in the
five-minute table at 16:05 -- the card simply never asks for it. Rounding is
`unit_prefix: k` with `round: 1`, so 389 Wh is 0.4 and 130 Wh is 0.1.

**Do not "fix" the lag by turning off `energy_date_selection`.** It is card-wide: every node would
revert to raw live state and any historical date would show today's numbers. The lag is accepted.

### Helios and grid-voltage chart

Helios highlights the OSM building nearest the configured home. OSM has no building polygon for the house: 70 buildings lie within 250 m, none contains the address point, and the nearest centre is 35 m away; Overpass also finds no addressed feature on the street. HA's old home point was separately 98 m wrong (96 m north, 20 m east), affecting sun times, weather, and zone.home.

zone.home is deliberately at the owner's actual building centroid, **REDACTED_LAT, REDACTED_LON**, about 35 m from the postal point but inside the right footprint. That keeps ring, chips, and highlighted house together and remains within the 100 m presence radius. Do not “correct” it to the postal coordinate without revisiting this trade.

Helios home-latitude/home-longitude move only the building highlight, not ring, chips, or camera, so they are intentionally unused. A dragged camera stores helios:camera-pose:<lat>:<lon> in browser localStorage and overrides camera-pitch-deg; clear helios* keys to restore configured framing. Weather rendering is off because its grey veil flattens the scene. Buildings need high opacity with the custom palette. camera-pitch-deg wins only while camera-locked is true; unlocked stored pose wins.

The 24-hour grid-voltage plot shows raw sensor.solis_inverter_grid_voltage plus hourly min/max as a translucent band. It fixes the y-axis near 212-257 V and marks UK supply limits, 230 V +10% / -6% = **253.0/216.2 V**, because the question is upper-limit headroom and autoranging hides it. This is a live healthy-grid view, not a revival of the retired logger. Its statistics band begins only when the integration's own long-term statistics begin.

With raw_plotly_config: true the card does not map data automatically. Every trace needs explicit x: $ex xs and y: $ex ys; without them the axes and shapes render but no data does, often with a plausible-looking -1..6 numeric x-axis and no console error.

## Docker JSON API and browser dashboard

solis_api.py is a read-only backend for a box near HA. A daemon thread holds one Solarman V5 session, sweeps telemetry/settings every 10 s, and serves the cache:

- GET /api/state: full cached telemetry, settings, forecast, and optional Octopus car/cost data.
- GET /api/health: 200 while sweeps are fresh, 503 otherwise.

On any exception _drop_session() nulls the session so the next sweep reconnects. The sweep timestamp is taken immediately after register reads, before forecast network work can make fresh telemetry look old.

Dockerfile runs solis_api.py alone and deliberately does not copy dash.py, so the served container has no dashboard or network write route. docker-compose.yml exposes 5051, mounts /data, supplies SOLIS_HOST because UDP discovery cannot cross the bridge, and may supply Octopus credentials. SOLIS_PORT overrides the port.

Mutable solar_actuals.json, .solar_cache.json, and energy_cost.json follow SOLIS_DATA_DIR in both solis_api.py and solar_forecast.py; unset means beside the code. The image sets /data, which must be seeded with the repository JSON files on first run. requirements.txt now exists for this packaged path.

homeassistant.md retains a REST configuration for one HTTP request feeding multiple HA sensors. Treat it as an alternative/legacy path: the native custom integration now feeds the live Energy dashboard directly. Both paths contend for the logger if run against it simultaneously.

settings_dash.py is the thin local entry point that mounts dash.py's Flask blueprint onto the API app and serves both at :5051. dash.py holds PAGE and uses render_template_string; there is no templates directory.

### Legacy browser UI facts that still prevent regressions

The top tiles are solar now (watts plus string split), battery (SOC, direction/watts, voltage/current/health), house, solar today versus forecast plus yesterday, grid (import/export headline, voltage secondary, daily in/out), and—only when meaningful—Car. Below are tomorrow weather/verdict, charge current, charge windows, work mode, and diagnostics. Everything is display-only.

The Car tile is Octopus-gated like the cost line. It headlines vehicleChargingPreferences target and time (“75% by 05:30”, verified live), puts plug state at top right, and shows one dim line for live charging or plan—never wattage, because Octopus exposes neither car SOC nor charger power. Unplugged hides it and returns to five-across; no-link/unknown/unavailable remain visible as faults. renderCar toggles hidden and the grid's six class; the template must not hard-code six. SmartFlexDeviceState means CAPABLE = plugged in, IN_PROGRESS = plugged with a plan (**not charging**), BOOSTING = charging, NOT_AVAILABLE = unplugged. This mapping is the community/HA reading and was observed live, but is not Octopus-documented; IN_PROGRESS was caught at 16:14 with its first dispatch ten hours away. Otherwise “Charging” means a dispatch brackets now. Six tiles need about 1276 px; .wrap is 1300 px and .grid.six switches to three columns at 1068-1275 px.

The browser polls /api/state, so reloads do not open logger sessions. read_at_epoch lets it detect a frozen poller; tiles dim after 30 s. tick() may overlap 5-second fetches, so render() rejects an older sweep. A timestamp more than STALE_SECONDS in the future is stale **and must not update lastEpoch**: the old order let one NTP step forward reject every later valid sweep until the clock caught up.

PV-string bars fill against each plane's own nameplate, 12 × 400 W SW and 8 × 400 W SE. Key rows by pv_strings[].string, never array position, because dead strings are dropped and the roofs wake at different times. PV_PLANES maps string number to SW/SE only on the **inferred**, unconfirmed basis that 358.4 V:236.0 V matches 12:8 panels; keep the one-line mapping easy to correct. [hidden]{display:none!important} is necessary because .pvrows display:grid otherwise defeats the hidden attribute at night.

Solar-today's bar keeps yesterday as a notch. The battery bipolar bar was tried and reverted on sight because it competed with the SOC gauge; keep the text direction line.

The charge-current row translates the live register using live voltage, SOC, and slot-1 times. If the rate can fill, it states facts such as “50 A at 52.9 V is 2.6 kW — fills from 87% in about 15 min”; it does not recommend a rate. Below the fill rate it answers the useful question—“the window adds +2%, reaching 82% — 2.8 kWh short”—not the true but useless hypothetical “about 58 h to full”. Keep the fixes:

- Clamp displayed reached SOC to 99; rounding otherwise produced “reaching 100% — 0.0 kWh short”.
- Below 0.05 kWh say “just short of full”, not contradictory 0.0.
- Distinguish “no sweep yet” from an unset slot; an unset window means the rate does nothing.
- Read length/start/end from slot 1. It has been observed at 23:30-04:30 and later 23:30-03:30; never hard-code the description.

The timed-charging tag is 43110 bit 1, display only. Off dims window/current sections as inert.

The dashboard write path was removed in two stages on 2026-08-26: first the SOC<20% rules engine, then /api/write, _write(), _local_request(), controls, and all lock/arm/busy JS. The retained stale/out-of-order/future checks protect display truth. Before any explicitly requested reintroduction, read pre-removal history: every write needs readback, destructive controls need arm-to-confirm, and the lock must have the last word after asynchronous work. Do not reintroduce writes speculatively.

settings_dash.py needs OCTOPUS_API_KEY and OCTOPUS_ACCOUNT for cost/car fields. They are exported from ~/.zshrc, which non-interactive shells do not source. Without them those fields hide silently by design; this previously looked like a UI regression. Source the exports before a local restart; compose carries them when configured.

## Standalone tools and the only write path

Useful commands:

~~~bash
uv run --no-project python scan.py
uv run --no-project --with pysolarmanv5 python control.py show
uv run --no-project --with pysolarmanv5 python control.py charge-current 50 --apply
uv run --no-project --with flask --with pysolarmanv5 python settings_dash.py
uv run --no-project --with flask --with pysolarmanv5 python solis_api.py
docker compose up -d --build
uv run --no-project python solar_forecast.py
uv run --no-project python solar_forecast.py record 2026-08-20 32
uv run --no-project python solar_forecast.py calibrate
~~~

There is still no pyproject.toml and no test, lint, or non-Docker build workflow is recorded.

control.py writes holding registers over its own Solarman session. It is dry-run unless --apply, validates ranges, reads back each write, and hard-refuses PROTECTED. It replaced deleted mqtt_pub.py/mqtt_2.py and a Node-RED/MQTT route that published struct.pack('<H', v) to nodered/solis/* through a broker now requiring unknown credentials. Direct Modbus needs no broker; do not reintroduce that hop.

solis_net.py resolves $SOLIS_HOST override → UDP discovery by serial → LAST_KNOWN/CANDIDATES fallback. control.py and settings_dash.py go through it. Discovery is layer 2 only and intermittent, particularly with a Modbus session open, so do not remove CANDIDATES. Across a routed boundary set SOLIS_HOST. scan.py is the standalone broadcast: WIFIKIT-214028-READ to UDP 48899, reply ipaddress,mac,serial.

With HA, API, or dashboard holding the logger session, control.py may fail with _queue.Empty. Read curl localhost:5051/api/state where appropriate, or stop the reader before using the CLI.

## Register map (verified live 2026-08-21)

Input registers use FC 0x04 for telemetry; 43xxx holding registers use FC 0x03/0x06 for settings.

| Register | Meaning |
|---|---|
| 33035 / 33036 | Solar generated today / yesterday, ×0.1 kWh; today resets at midnight |
| 33049 / 33050 | PV string 1 voltage/current, ×0.1 |
| 33051 / 33052 | PV string 2 voltage/current, ×0.1; strings 3-4 at 33053-33056 are unused |
| **33057 + 33058** | Total PV power, W, u32 pair, **DC side** |
| 33071 | DC bus voltage, ×0.1; 397.7 V magnitude inference, not confirmed |
| 33073 | Grid phase A voltage, ×0.1; mirrored at 33137 |
| **33079 + 33080** | Inverter AC output power, W, u32 pair; AC counterpart of PV power |
| 33094 | Grid frequency, ×0.01; 50.10 Hz magnitude inference, not confirmed |
| 33133 / 33134 | Battery voltage/current, ×0.1 |
| **33135** | Battery direction: 0 charging, 1 discharging; **not current** |
| 33139 / 33140 | Battery SOC/SOH, % |
| 33147 | House load, W |
| **33149 + 33150** | Battery power, W, big-endian u32 unsigned magnitude; sign from 33135 |
| 33161-33164 | Battery charge energy: u32 total, today, yesterday; ×0.1 kWh |
| 33165-33168 | Battery discharge energy, same layout |
| 33169-33172 | Grid import energy, same layout |
| 33173-33176 | Grid export energy, same layout |
| 33177-33180 | House consumption energy, same layout |
| 33251 / 33252 | Meter voltage ×0.1 / current ×0.01 |
| **33257 + 33258** | Grid power, W, s32: positive exporting, negative importing; mirrored at 33263/33264 |
| 33283-33286 | Meter import/export totals, ×0.001 kWh; same energy as 33169-33176 at finer resolution |
| 43010 / 43011 | Battery capacity, Ah / type code |
| 43012 / 43013 | Battery profile capability: 100.0 A each way, 1C |
| 43110 | Work-mode bits. **Verified:** bit0 self-use, bit1 time-of-use charging, bit5 grid charging allowed, bit9 battery healing. `solis.py`'s `MODE_BITS` also names bit2 off-grid, bit3 battery wake-up, bit4 backup/reserve, bit6 feed-in priority — those four are **unverified** on this inverter. Unknown set bits surface as `bitN`. |
| 43141 / 43142 | Time-of-use charge/discharge current limit, ×0.1 A |
| 43143 + (n−1)×8 | Slot n, n=1..3: charge start +0, charge end +2, discharge start +4, discharge end +6; each is hour,minute |

PV and battery power were confirmed by closing the instantaneous balance: 2033 W PV + 1063 W battery = 3008 W house, with string products 358.4 V × 3.5 A + 236.0 V × 3.3 A and battery 52.9 V × 20.1 A.

The slot stride and hour/minute layout were decoded from the live slot-1 window: 23:30-04:30 when first read, later 23:30-03:30. Slots 2-3 and **every discharge window** were unset deliberately. Read the live charge end; do not assume that those dated observations equal the intended 23:30-05:30 tariff policy.

Grid s32 and sign were confirmed twice. At clear-sky export, 5834 W DC, 5800 W AC, and 875 W house implied 4925 W export; the register read +4984. During import, treating it unsigned produced 4294967253 W = 2^32−43, therefore −43 W—about 4.29 billion watts if mishandled. An older fallback took magnitude here and inferred direction from house_load > ac_power; it worked but is no longer needed. The integration negates the meter sign only to match HA's positive-import convention.

Do not replace the meter with house_load − pv_power ± battery_power. PV and battery are DC-side; about 2% conversion loss, more than 100 W near full output, becomes phantom export. This was caught when the display claimed 108 W export while a 1263 W battery discharge covered a 3840 W house load. If an AC balance is needed, use 33079/33080.

Daily counters were identified by closing yesterday's balance: 31.7 solar + 15.2 import + 6.2 battery out = 53.1 versus 21.8 house + 22.7 export + 8.2 battery in = 52.7 kWh, 0.4 kWh apart. Register 33036 independently agreed with the owner's 32 kWh actual for 2026-08-20, pinning ×0.1 scaling.

### What the daily counters do and do not prove

The daily "today" values -- 33171 import, 33179 house, 33163 battery charge, 33167 discharge,
33175 export -- are **single u16 words**, not u32 pairs, so a word-order bug is structurally
impossible for them. Their *meaning*, however, rests entirely on the closed energy balance above and
not on any vendor document. 33283-33286 are not polled by anything here and are **suspected** to
mirror only the two lifetime u32 totals, not the daily ones.

**Solis Cloud disagrees with the registers on daily import; trust the registers.** On 2026-08-27
cloud showed 8 kWh against 14.0 kWh from 33171. The 00:00-01:00 BST hour alone holds 6.1 kWh and
14.0 - 6.1 = 7.9, which rounds to cloud's 8, so the likely cause is cloud's day boundary sitting an
hour off -- the register resets at about 23:59:53 BST. This is **unproven**: shifting every field by
the same hour does not reproduce cloud's consumption (19.0 against 17.8) or battery charge (4.2
against 5). Do not adjust a register reading to agree with the cloud figure.

**A roughly 0.9 kWh gap between integrated house-load power and the house-consumption counter is
expected.** Overnight 2026-08-27: 11.23 kWh from integrating 33147 against 12.1 kWh of statistics on
the counter, 7.8% apart. This was checked and is **not** a Riemann-method or sampling artifact --
left, right, and trapezoid rules span only 0.012 kWh, and the largest sample gap was 92.8 s at about
250 W. It is **inferred** to be two different inverter measurement paths: an instantaneous wattage at
33147 versus an energy counter at 33177-33180. Left unattributed. Do not adjust either side to make
them agree.

## Battery and tariff operating policy

The intended Octopus cheap period is 23:30-05:30. Keep the charge window active for the whole period: besides cheap import, it stops the house pack discharging into Tesla Intelligent slots. Tune 43141, currently 50 A, rather than trimming 43143. The source snapshot cannot report the live inverter's current slot end, and dated reads above differed; display it dynamically and do not silently rewrite it.

The Tesla takes **discrete roughly 7 kW slots**, not one continuous session. On 2026-08-27, 1779 raw house_load samples showed 00:40-01:00, 02:00-02:30, and 05:00-05:16: 1.15 h above 6 kW, 8459 W peak, about 8 kWh for the car over a roughly 660 W baseline. Hourly statistics average partial slots to about 4 kW and understate the rate by nearly 2×. Gaps are normal dispatch behavior, not faults. The house battery charges around 2.5 kW at 50 A/53 V; never attribute a 7 kW draw to it or a 2.5 kW draw to the car.

A small overnight battery charge usually means the battery began nearly full, not that the car stole capacity. On 2026-08-27 it took only 2.3 kWh because previous-day solar left it high and it filled early. Check SOC at window open before raising 43141 or extending a window.

The fill-rate crossover is about **48 A from 0%, about 45 A from the real 10% floor**: 15.4 kWh over six hours is 2.56 kW or roughly 48 A at 53 V; from 10%, about 14.6 kWh after losses takes 5.5 h at 50 A. The old 16 A advice used one module's 5.1 kWh and would leave the pack roughly two-thirds empty. Trimming from 48 A to leave solar headroom is the owner's decision, never an automatic optimisation.

**“Charge overnight, export the sun” was settled 2026-08-23; do not reopen it.** strategy_sim.py used half-hourly simulation, Monte Carlo loads, SOC chained across days, and live Octopus rates. Plain 23:30-05:30 timed charging in self-use, with no daytime limit, stayed within 2-8p/day of a perfect-knowledge oracle at every sun level, including bright no-car days. Export pays 12p while an overnight kWh delivered at 90% costs 6.9p/0.9 = 7.7p, so storing rather than exporting solar loses about 4.3p/kWh.

A SOC taper via 43117 (43012 is BMS-overwritten; 43130 reportedly inert) gained no more than 6p/day and lost 20-70p with daytime Tesla charging. It is not worth flash writes: endurance is unpublished, assume 10k and count every 43xxx write. Do not propose it again.

The one material opportunity is a daytime Tesla dispatch: self-use makes the 5 kW pack feed the car. Temporarily forcing a charge window could save 30p-£1.50 per occurrence, but it remains parked until there is a bulletproof restore; a slot left active grid-charges at 30p.

The owner chose 43141 = 50 A deliberately: 0.17C on 300 Ah, roughly 5.5 h from the 10% floor. It may stop around 97-98% after an empty start; that is accepted as kinder to LFP than holding 100%. The floor is 10%, not 20%.

Battery charge/discharge tops out at 5 kW, owner-stated, below the inverter's 6 kW. 43142 does **not** cap self-use house supply; the owner says it limits timed discharge-to-grid. Leave its deliberate 50 A setting alone rather than raising it toward the 100 A capability at 43013. With discharge windows unset, it has no effect.

Do not confuse 43012/43013 capability with applied 43141/43142 limits.

## Forecast model reference

solar_forecast.py answers whether tomorrow's sun refills the battery or the off-peak rate should. Its public standalone interface -- verified against source 2026-08-27 -- is today_kwh(), tomorrow_kwh(), verdict(), tomorrow_weather(), plus day_kwh(), hourly_kw(), and the record()/calibrate() pair that use solar_actuals.json. The vendored HA copy is a separate implementation with private names (_day_kwh, _verdict, _tomorrow_weather, fetch_forecast); do not assume a function exists in both. Open-Meteo is free, keyless, and includes past days. The vendored HA copy identifies the site as REDACTED at REDACTED_LAT, REDACTED_LON.

### Surveyed array geometry

The geometry in PLANES is measured, not guessed; retain separate tilt as well as azimuth:

- Roof 1: 12 × 400 W, 207° SW compass / 21° pitch; survey 25.1 m² and 990 kWh/kWp.
- Roof 2: 8 × 400 W, 117° SE compass / 24° pitch; survey 18 m² and 920 kWh/kWp.
- Total: 20 panels, 8.0 kWp. Open-Meteo azimuth = compass − 180, hence +27 and −63.

The old guess, −15/−100 at one 25° pitch, put both planes too far east but a fitted EFFECTIVE_KWP absorbed much of the error. Refitting six late-summer actuals moved RMSE only 1.66→1.59; that is not the reason to keep the survey. Keep it because it is measured and azimuth error grows away from midsummer. Survey annual generation 4752 + 2944 = 7696 kWh is within 1% of 7743 at 33039/33040, suspected to be last year's total but **not confirmed**.

### Five-model ensemble and fit

MODELS is ecmwf_ifs025, gfs_seamless, icon_seamless, ukmo_seamless, and meteofrance_seamless. Against seven actuals the mean scored **2.19 kWh RMSE**; the best individual scored 3.26 and worst 5.63. Open-Meteo best_match resolves to UKMO here, which missed 2026-08-20 by 7 kWh (3.65 kWh/m² versus ECMWF 5.90), yet over all seven ECMWF was worst at 5.63 and UKMO mid-pack at 4.28. There is no stable winner: averaging is the point. A complete-day all-zero model is missing/padded data, not darkness, and must be dropped.

EFFECTIVE_KWP = 6.33 against 8.0 kWp gives a realistic 0.79 performance ratio. Earlier single-source fits produced PR > 1.0, revealing bad weather input. verdict() returns low/borderline/high and refuses a call within UNCERTAINTY_KWH = 2 kWh of LOW_KWH = 25 kWh.

Adding 2026-08-21, prediction 29.4 versus actual 26.0, was the first material over-prediction and moved seven-day RMSE to about 2.1-2.2. This is soft: a cache refetch alone moved 2.07→2.19 because Open-Meteo revises the current day. Refitting 6.33→6.24 saved only 0.06 kWh RMSE; bias moved +0.36→−0.09. That is noise, so 6.33 stayed. Model spread, not scale, is the remaining problem.

forecast.solar was rejected because it badly low-balled the array: 3.5 kW peak and 26 kWh against an observed roughly 6 kW peak and calibrated 39 kWh. A pvlib Perez transposition did no better than Open-Meteo tilted irradiance. Do not retry either without new evidence.

Never quote a kW peak from hourly_kw(). Daily-energy fitting folds in part-load efficiency, diffuse shoulder hours, and low incidence angles that do not apply at noon. On 2026-08-22 the same curve gave 4.41 kW at 6.33 versus 5.58 at nameplate; five models ranged 5.55/4.86/4.66/4.51/3.50 kW and averaging flattened the peak further. The owner correctly rejected “peaks around 13:00 at 4.4 kW” while the inverter was near 6 kW. Daily kWh is right while instantaneous peak is wrong. peakLine() therefore reports only the contiguous >80%-of-peak span, for example “strongest 11:00-15:00”, with no number. A kW claim needs a separate model and recorded peak actuals; none exist.

The 6 kW inverter ceiling costs little. The model estimated 0.7 kWh clipping on clear 2026-07-20, a 49 kWh actual day, but because it uses 6.33 effective kWp this **understates clipping** and is a floor, not a measurement. Twelve southwest plus eight southeast panels broaden the curve. Do not propose a bigger inverter on clipping grounds.

The daily weather_code is a worst-of-day aggregate and can say overcast beside high kWh. _sky() uses codes for precipitation and mean cloud for clear/cloudy. _daylight_sky() restricts both to sunrise-sunset; on 2026-08-21 a 03:00 code 51 but daylight max 3 changed “light drizzle” to “partly cloudy”. tomorrow_weather() also supplies today's sunrise/sunset so the UI can distinguish low post-sunset PV from a fault: 16 W at 20:15 with sunset 20:08 is correct.

_weather_memo must be keyed by target date as well as age. With its 3600 s TTL, an age-only memo created 23:55 still served the wrong “tomorrow” at 00:05 and for up to an hour. _series() is safe because it caches a dict keyed by date.

Open-Meteo azimuth is verified as 0 south, −90 east, +90 west. Same-hour peaks across azimuths indicate cloud, not a broken convention.

## Network topology

The retained topology record is internally historical because a bridge migration was attempted but did not stick:

- FRITZ!Box serves 198.51.100.0/24. Two Tenda Nova meshes split main/fast and 2.4 GHz IoT.
- IoT mesh NATs 192.0.2.0/24, gateway 192.0.2.1, WAN 198.51.100.49. The logger was 192.0.2.45. IoT can initiate toward 178.x; main cannot initiate toward 5.x.
- Main-mesh access uses the Tenda-app TCP forward **198.51.100.49:8899 → 192.0.2.45:8899**, verified carrying Modbus.
- Bridge mode was tried twice with a valid wired uplink and silently reverted to Dynamic. Do not burn time repeating it. If bridge mode ever succeeds, the logger will get a Fritz address and solis_net.py should rediscover it.
- The forward is LAN-only. Mesh B's WAN is the Fritz LAN; FRITZ!Box 7530 AX has zero port mappings and a dynamic public IP, so two NATs separate the inverter from the internet.
- There is no DHCP reservation behind the forward: Tenda rejected the MAC binding. If the logger leaves 192.0.2.45, control fails only from main mesh while working on IoT. Repoint the forward or configure the logger itself static.
- Tenda UPnP AddPortMapping returns SOAP 500, likely because the target is not the requester. Reading mappings works, but app-created forwards do not appear there. Use the app.

The native HA integration stores the configured host directly, so a DHCP/forward change requires updating/recreating its config entry; it has no solis_net fallback. The Docker compose host is deliberately the forwarded 198.51.100.49 in the recorded configuration.

## Historical dead ends

Voltage logging is finished business. voltage.py, dashboard.py, and solis_voltage_log.db existed to evidence a DNO complaint, were deleted 2026-08-22 after the grid issue was fixed between 2025-03-30 and 2025-08-13, and remain only in history. The DB moved to ~/solis_voltage_log_archive.db. Do not propose launchd, backfills, gap analysis, or confuse the HA voltage chart with reviving the logger.

The archive spans 2025-03-29 to 2026-08-21 but contains only 2025-03-29/30, 2025-08-13/14, and a handful of 2026-08-21 rows—not 17 months. Whole-table hour aggregates mix broken and fixed periods. Split by date. Like-for-like mornings changed from means 252.4 V at 09:00 / 255.8 V at 10:00, peak 262.4 V and rising steeply in March, to a flat roughly 244 V in August; that flatness is the evidence the fix worked.

The MQTT/Node-RED write indirection, forecast.solar, pvlib Perez experiment, SOC taper, browser write controls/rules engine, and built-in HA device-detail graphs are settled/rejected work for the reasons above. Do not resurrect them because they look simpler.

