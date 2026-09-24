# The Docker JSON API and the legacy browser dashboard

*Split out of `CLAUDE.md` on 2026-09-13 to keep the always-loaded file small. This is the same text, except that site-specific values are placeholders whose real values are in the gitignored `CLAUDE.local.md`. The rules and traps that apply even when you are NOT reading this file stayed in `CLAUDE.md`.*

## Docker JSON API and browser dashboard

`solis_api.py` is a read-only backend for a box near HA. A daemon thread holds one Solarman V5 session,
sweeps telemetry/settings every 10 s, and serves the cache: `GET /api/state` (full cached telemetry,
settings, forecast, optional Octopus car/cost data) and `GET /api/health` (200 while sweeps are fresh,
503 otherwise). On any exception `_drop_session()` nulls the session so the next sweep reconnects. The
sweep timestamp is taken immediately after register reads, before forecast network work can make fresh
telemetry look old.

`Dockerfile` runs `solis_api.py` alone and deliberately does not copy `dash.py`, so the served container
has no dashboard or network write route. `docker-compose.yml` exposes 5051, mounts `/data`, and supplies
`SOLIS_HOST` (from `.env`) because UDP discovery cannot cross the bridge, and may supply Octopus credentials;
`SOLIS_PORT` overrides the port. Mutable
`solar_actuals.json`, `.solar_cache.json` and `energy_cost.json` follow `SOLIS_DATA_DIR` in both
`solis_api.py` and `solar_forecast.py`; unset means beside the code. The image sets `/data`, which must
be seeded with the repository JSON files on first run. `requirements.txt` exists for this packaged
path.

`homeassistant.md` retains a REST configuration for one HTTP request feeding multiple HA sensors. Treat
it as legacy: the native integration now feeds the live Energy dashboard directly. Both paths contend
for the logger if run simultaneously. `settings_dash.py` is the thin local entry point that mounts
`dash.py`'s Flask blueprint onto the API app and serves both at :5051; `dash.py` holds `PAGE` and uses
`render_template_string`, so there is no templates directory.

### Legacy browser UI facts that still prevent regressions

Top tiles: solar (watts plus string split), battery (SOC, direction/watts, voltage/current/health),
house, solar today versus forecast plus yesterday, grid (import/export headline, voltage secondary,
daily in/out), and -- only when meaningful -- Car. Below are tomorrow weather/verdict, charge current,
charge windows, work mode, and diagnostics. Everything is display-only.

The Car tile is Octopus-gated like the cost line. It headlines `vehicleChargingPreferences` target and
time ("75% by 05:30", verified live), puts plug state at top right, and shows one dim line for live
charging or plan -- never wattage, because Octopus exposes neither car SOC nor charger power. Unplugged
hides it and returns to five-across; no-link/unknown/unavailable remain visible as faults. `renderCar`
toggles hidden and the grid's `six` class; the template must not hard-code six. **SmartFlexDeviceState:
CAPABLE = plugged in, IN_PROGRESS = plugged with a plan (not charging), BOOSTING = charging,
NOT_AVAILABLE = unplugged.** This mapping is the community/HA reading and was observed live, but is not
Octopus-documented; IN_PROGRESS was caught at 16:14 with its first dispatch ten hours away. Otherwise
"Charging" means a dispatch brackets now. Six tiles need about 1276 px; `.wrap` is 1300 px and
`.grid.six` switches to three columns at 1068-1275 px.

The browser polls `/api/state`, so reloads do not open logger sessions. `read_at_epoch` lets it detect a
frozen poller; tiles dim after 30 s. `tick()` may overlap 5-second fetches, so `render()` rejects an
older sweep. **A timestamp more than `STALE_SECONDS` in the future is stale and must not update
`lastEpoch`**: the old order let one NTP step forward reject every later valid sweep until the clock
caught up.

PV-string bars fill against each plane's own nameplate, 12 x 400 W SW and 8 x 400 W SE. **Key rows by
`pv_strings[].string`, never array position**, because dead strings are dropped and the roofs wake at
different times. `PV_PLANES` maps string number to SW/SE only on the **inferred**, unconfirmed basis
that 358.4 V:236.0 V matches 12:8 panels; keep the one-line mapping easy to correct.
`[hidden]{display:none!important}` is necessary because `.pvrows` `display:grid` otherwise defeats the
hidden attribute at night.

Solar-today's bar keeps yesterday as a notch. The battery bipolar bar was tried and reverted on sight
because it competed with the SOC gauge; keep the text direction line.

The charge-current row translates the live register using live voltage, SOC and slot-1 times. If the
rate can fill, it states facts such as "50 A at 52.9 V is 2.6 kW -- fills from 87% in about 15 min"; it
does not recommend a rate. Below the fill rate it answers the useful question -- "the window adds +2%,
reaching 82% -- 2.8 kWh short" -- not the true but useless "about 58 h to full". Keep the fixes:

- Clamp displayed reached SOC to 99; rounding otherwise produced "reaching 100% -- 0.0 kWh short".
- Below 0.05 kWh say "just short of full", not contradictory 0.0.
- Distinguish "no sweep yet" from an unset slot; an unset window means the rate does nothing.
- Read length/start/end from slot 1. It has been observed at 23:30-04:30 and later 23:30-03:30; never
  hard-code the description.

The timed-charging tag is 43110 bit 1, display only. Off dims window/current sections as inert.

The dashboard write path was removed in two stages on 2026-08-26: first the SOC<20% rules engine, then
`/api/write`, `_write()`, `_local_request()`, controls, and all lock/arm/busy JS. The retained
stale/out-of-order/future checks protect display truth. Before any explicitly requested reintroduction,
read pre-removal history: every write needs readback, destructive controls need arm-to-confirm, and the
lock must have the last word after asynchronous work. Do not reintroduce writes speculatively.

`settings_dash.py` needs `OCTOPUS_API_KEY` and `OCTOPUS_ACCOUNT` for cost/car fields. Since 2026-09-24
they come from the gitignored `.env` (see `.env.example`), which every tool and compose read; before that they
were exported from `~/.zshrc`, which non-interactive shells do not source. Without them those fields hide
silently by design; this previously looked like a UI regression.

