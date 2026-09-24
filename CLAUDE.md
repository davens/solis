# CLAUDE.md

This file is operational guidance for agents working on this repository. It records facts that were
expensive to establish on live hardware. Preserve the distinction between **verified**,
**owner-stated**, **inferred**, and **suspected**; do not turn one into another.

**This file is deliberately small, and the detail lives in `docs/`.** Read the relevant file below
BEFORE working on that area -- each one contains numbers, failure modes and settled decisions you
cannot reconstruct from the code. What stayed here is what applies even when you are not reading
any of them: the safety rules, the cross-cutting traps, and the list of questions already settled.
Reference them by path (do not use `@` imports -- those get inlined into every session, which is
exactly what this split undoes).

Site-specific private facts live in the gitignored `CLAUDE.local.md`; runtime values live in `.env`.

| Read this | When you are |
|---|---|
| `docs/registers.md` | Touching Modbus, reading raw registers, or doing any energy-balance arithmetic |
| `docs/ha-integration.md` | Changing `custom_components/solis_solarman`, the Energy dashboard, or the gas statistic |
| `docs/sankey.md` | Touching the Sankey chart, the flow sensors, or any statistics-backed card |
| `docs/dashboard-cards.md` | Touching Helios, the grid-voltage chart, mini-graph-card, or the Car tile |
| `docs/forecast.md` | Changing `solar_forecast.py`, recalibrating, or quoting a forecast figure |
| `docs/network.md` | Anything cannot reach the logger or HA, or you are changing a host/IP |
| `docs/legacy-api-dashboard.md` | Touching `solis_api.py`, `dash.py`, or the Docker path |

## What this is now

Primarily a **Home Assistant deployment** for a Solis 6 kW hybrid inverter (model 0x3105), reached
over Modbus through a Solarman/IGEN WiFi logger. The main artefact is `custom_components/solis_solarman/`,
a HACS custom integration at v0.3.0: local read-only polling, config flow, battery daily counters,
the calibrated solar forecast, and a native Energy-dashboard forecast provider. The Docker JSON API
and old browser dashboard remain useful, and the standalone scripts still hold the only write path,
but they are no longer the centre of gravity.

Hardware is **three Fox LV5200 modules in parallel**: about 300 Ah at 51.2 V, **about 15.4 kWh**,
5 kW delivery, 90% SOH. An earlier version of this file used 5.1 kWh -- one module -- and made every
fill-time estimate three times too short. Logger serial and MAC are `SOLIS_LOGGER_SERIAL` and
`SOLIS_LOGGER_MAC` in `.env` (values in `CLAUDE.local.md`), Modbus port 8899. Do not bake a
discovered IP into standalone code; use `solis_net.py`. The HA integration is different: its config
entry requires an explicit host and does not call `solis_net.py`.

## Non-negotiable safety and truth rules

- **This repository is public (github.com/davens/solis). Never commit a site-specific value**:
  addresses, postcodes, coordinates, IPs, SSIDs, serials, MACs, meter/MPAN/MPRN/account/device ids,
  people's or phones' names, e-mail addresses, home-directory paths. Runtime values go in the
  gitignored `.env` (add the variable name to `.env.example`); facts go in the gitignored
  `CLAUDE.local.md`, with a placeholder or a pointer in the public text. Everything that leaked before
  2026-09-24 was a helpful "currently X (verified ...)" note -- grep the diff before every commit.
- **The HA integration, Docker API, and browser dashboard are read-only. Do not add a write path
  unless the owner explicitly asks.** `control.py` on the CLI is the only write path. A
  network-reachable control surface was removed deliberately on 2026-08-26.
- **Never write holding registers 43038-43049 or 43090-43097.** They are DNO-mandated G98/G99
  protection: voltage/frequency trips and delays, including 262/184 V and 47.5/51.5 Hz. This is not
  merely risky; changing them is not permitted. Keep `control.py`'s PROTECTED guard.
- **All three discharge windows are deliberately unset and must stay unset.** The owner does not
  want discharge to grid. Never propose enabling one or describe unused slots as exploitable
  headroom.
- **The intended cheap-rate charge window is fixed; charge current is the lever.** Its second job is
  preventing the house battery from feeding an unpredictably scheduled Tesla charge. Do not
  "optimise" by shortening the window.
- **The logger permits one Modbus session at a time.** A competing process commonly fails with bare
  `_queue.Empty`; that means contention, not necessarily a network fault. HA normally holds the
  session, so the CLI and local dashboard contend with it.
- **Never derive grid flow from PV and battery power.** Those registers are DC-side and conversion
  loss becomes plausible-looking phantom export. Use the signed smart-meter pair at 33257/33258.
- **The HA integration deliberately inverts two sign conventions relative to the raw registers.** HA
  grid power is positive *importing* while register 33257 is positive *exporting*; HA battery power
  is positive *charging*, built from an unsigned u32 magnitude plus the 33135 direction flag. Both
  are correct as written. This is exactly the shape of thing an agent "helpfully" flips in the wrong
  direction -- check which side of the boundary you are on before touching a sign.
- **33135 is a direction flag, not current.** Current is 33134. Power comes from the u32 register
  pairs, not V x A.
- **The irradiance forecast is the mean of five models. Do not simplify it to one model or
  Open-Meteo `best_match`.** The ensemble materially outperformed every single source, and which
  individual model is worst changes.

## Traps that fire when you are not looking for them

These bite people who are working on something else, so they stay here rather than in `docs/`.

- **`house_consumption_today` (33177-33180) is a derived residual and silently contains the
  inverter's conversion loss.** Never compare it against integrated house-load power, and never
  treat it as an independent measurement in an energy balance. **33147 is the honest house number;
  33179 is house plus loss.** A balance built from the six daily counters closes by construction and
  proves nothing. Two independent auditors stalled on exactly this. -- `docs/registers.md`
- **After any HACS update of ha-sankey-chart, re-apply the 5-minute patch.** HACS silently
  overwrites it and nothing errors -- the chart just goes back to lagging by up to an hour. Detect
  with `grep -c 5minute ha-sankey-chart.js`: **1 = patched, 0 = reverted.** -- `docs/sankey.md`
- **Two copies of the forecast calibration constants exist and can drift silently** -- the repo's
  `solar_forecast.py` and the vendored copy in the integration. A refit reaches HA only when the
  vendored copy is updated. Re-check parity after any `calibrate` run. -- `docs/forecast.md`
- **The Octopus `previous_accumulative_*` entities must never be Energy dashboard sources.** They
  can place an entire day's consumption on the wrong day. -- `docs/ha-integration.md`
- **The Solis config entry has no reconfigure flow and no `solis_net.py` fallback.** A DHCP or host
  change breaks it at the next *reconnect*, not the next read, so it can look healthy for days.
  The current host is recorded in `CLAUDE.local.md` (verified 2026-09-13). -- `docs/network.md`
- **A Sankey link whose `value:` entity has no statistics rows returns null**, which becomes NaN and
  silently zeroes **every later ribbon from that source**. The load-bearing property is
  `state_class`, not availability. -- `docs/sankey.md`
- **Statistics-backed cards read `sum(change)`, not live state, and lag up to an hour.** A number
  that is higher on first paint and then drops is correct twice. -- `docs/sankey.md`
- **A ~6-7 kW load is not necessarily the Tesla: there is a pottery kiln.** Verified 2026-09-23. Its
  elements switch on and off every 1-2 minutes for 10-11 hours (a firing took 32 kWh); the Tesla is a
  steady block. Tell them apart by cycling, never by power. The overnight "Tesla slots" attributed from
  2026-08-27 samples below predate any car sensor and may have been kiln -- unverified. Kiln detection,
  its warning and all-clear live on HA in `/config/packages/kiln.yaml` (+ `custom_templates/kiln.jinja`),
  not in this repo.
- **Never assign `card_mod` across every `custom:mini-graph-card` in a script** -- it destroys the
  Power card's own unrelated mod. Back the dashboard config up before writing it. --
  `docs/dashboard-cards.md`

## Settled -- do not reopen unasked

Each of these was investigated and closed. Re-proposing one costs the owner time re-litigating a
decision they already made. The reasoning is in the linked file where noted.

- **"Charge overnight, export the sun"** -- settled 2026-08-23 by simulation; plain timed charging
  stayed within 2-8p/day of a perfect-knowledge oracle. Storing solar rather than exporting loses
  ~4.3p/kWh.
- **A SOC taper via 43117** -- gained at most 6p/day and lost 20-70p with daytime Tesla charging.
  Not worth the flash writes.
- **A bigger inverter on clipping grounds** -- the 6 kW ceiling costs ~0.7 kWh on a clear 49 kWh
  day, and that figure is a floor, not a measurement. `docs/forecast.md`
- **forecast.solar, and a pvlib Perez transposition** -- both did worse than the Open-Meteo
  ensemble. `docs/forecast.md`
- **The MQTT / Node-RED write indirection** -- direct Modbus needs no broker; do not reintroduce
  that hop.
- **Browser write controls and the SOC<20% rules engine** -- removed deliberately 2026-08-26.
  `docs/legacy-api-dashboard.md`
- **HA's built-in device-detail graphs** -- the owner wants that breakdown only in the Sankey.
- **Promoting Tesla to a peer of House on the Sankey** -- built, reviewed and reverted 2026-08-30 at
  the owner's request. It worked; he preferred the car inside the house total. `docs/sankey.md`
- **The Sankey "Stored", "Rest of house" and "Unaccounted" nodes** -- all removed by request.
  `docs/sankey.md`
- **Bridge mode on the Tenda mesh** -- tried twice with a valid wired uplink, silently reverted both
  times. `docs/network.md`
- **Tailscale subnet routes** -- deliberately empty; "i only need the ha box, for security".
  `docs/network.md`
- **A GPS home-gate for Tesla charging** -- declined as unnecessary, not deferred; the owner charges
  only at home.
- **Integrating the Inkbird ITC-308-WiFi** -- declined 2026-08-26; not worth leaving the Inkbird app.
- **Alexa control of HA lights and the aircon, via `emulated_hue`** -- built, then removed 2026-09-19 at the
  owner's request. A "good night" routine left the lights and the air conditioner in a state he had not
  asked for, which settled the wider question: he does not want voice control of these entities at all.
  The whole `emulated_hue:` block and its temporary debug `logger:` block are gone from
  `configuration.yaml`, the four devices were deleted in the Alexa app, and `/config/.storage/emulated_hue.ids`
  is orphaned but inert. Do not propose an Alexa, Google or other voice bridge for them again.
- **Reviving the voltage logger** -- finished business; the grid fault was fixed and the tooling
  deleted 2026-08-22.

## Standalone tools and the only write path

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

There is still no `pyproject.toml`, and no test, lint or non-Docker build workflow is recorded.

`control.py` writes holding registers over its own Solarman session. It is dry-run unless `--apply`,
validates ranges, reads back each write, and hard-refuses PROTECTED. It replaced deleted
`mqtt_pub.py`/`mqtt_2.py` and a Node-RED/MQTT route that published `struct.pack('<H', v)` to
`nodered/solis/*` through a broker now requiring unknown credentials. Direct Modbus needs no broker; do
not reintroduce that hop.

`solis_net.py` resolves `$SOLIS_HOST` override -> UDP discovery by serial -> `SOLIS_HOST_CANDIDATES`
fallback, reading the serial/MAC and `SOLIS_HOST_CANDIDATES` from `.env`. `control.py` and
`settings_dash.py` go through it. Discovery is layer 2 only and intermittent, particularly with a Modbus
session open, so **do not remove CANDIDATES** (keep `SOLIS_HOST_CANDIDATES` populated). Across a routed
boundary set `SOLIS_HOST`. `scan.py` is the standalone broadcast: `WIFIKIT-214028-READ` to UDP 48899, reply
`ipaddress,mac,serial`.

With HA, API or dashboard holding the logger session, `control.py` may fail with `_queue.Empty`. Read
`curl localhost:5051/api/state` where appropriate, or stop the reader before using the CLI.

### The inverter clock, and the DST trap on 2026-10-25

`43000`-`43005` is a plain real-time clock -- year, month, day, hour, minute, second, one per register.
**It has no timezone and no DST awareness.** It holds whatever wall-clock was last written to it, and
`control.py set-time` writes this machine's naive local time (`datetime.datetime.now()`), so today it
holds BST.

Right now that is correct. Verified 2026-08-27 from recorded history: the daily counters rolled over at
**23:59:52 local (BST)** on 2026-08-26 -- solar_today went 30.9 -> 0.0 there, and grid import behaves
the same. Note this is **one observed rollover**, not a pattern; the integration has only been
recording since 2026-08-26.

**BST ends on Sunday 2026-10-25.** Nothing adjusts the inverter, so from that morning its clock is **one
hour fast** until someone runs `control.py set-time --apply`. Two consequences, and the second costs
money:

- The daily counters start rolling over at 23:00 GMT, so a "day" on the inverter stops matching a day in
  HA, and every daily comparison silently shifts by an hour.
- **The charge window moves with the clock.** 43143 stores 23:30-05:30 in *inverter* time, so an
  hour-fast clock runs it 22:30-04:30 GMT: the first hour lands on the expensive day rate and the last
  cheap hour is missed entirely. `control.py`'s own comment at `CLOCK_TOLERANCE_SECONDS` flags exactly
  this -- drift moves the whole window against the tariff.

So: check `control.py show` on 2026-10-25 and set the clock if it has not been done. The same applies in
reverse on the spring transition.

## Battery and tariff operating policy

The intended Octopus cheap period is 23:30-05:30. Keep the charge window active for the whole period:
besides cheap import, it stops the house pack discharging into Tesla Intelligent slots. Tune 43141,
currently 50 A, rather than trimming 43143. The source snapshot cannot report the live inverter's current
slot end, and dated reads above differed; display it dynamically and do not silently rewrite it.

The Tesla takes **discrete roughly 7 kW slots**, not one continuous session. On 2026-08-27, 1779 raw
`house_load` samples showed 00:40-01:00, 02:00-02:30 and 05:00-05:16: 1.15 h above 6 kW, 8459 W peak,
about 8 kWh for the car over a roughly 660 W baseline. Hourly statistics average partial slots to about
4 kW and understate the rate by nearly 2x. Gaps are normal dispatch behaviour, not faults. The house
battery charges around 2.5 kW at 50 A/53 V; **never attribute a 7 kW draw to it or a 2.5 kW draw to the
car.**

A small overnight battery charge usually means the battery began nearly full, not that the car stole
capacity. On 2026-08-27 it took only 2.3 kWh because previous-day solar left it high and it filled early.
Check SOC at window open before raising 43141 or extending a window.

The fill-rate crossover is about **48 A from 0%, about 45 A from the real 10% floor**: 15.4 kWh over six
hours is 2.56 kW or roughly 48 A at 53 V; from 10%, about 14.6 kWh after losses takes 5.5 h at 50 A. The
old 16 A advice used one module's 5.1 kWh and would leave the pack roughly two-thirds empty. Trimming
from 48 A to leave solar headroom is the owner's decision, never an automatic optimisation.

**"Charge overnight, export the sun" was settled 2026-08-23; do not reopen it.** `strategy_sim.py` used
half-hourly simulation, Monte Carlo loads, SOC chained across days and live Octopus rates. Plain
23:30-05:30 timed charging in self-use, with no daytime limit, stayed within 2-8p/day of a
perfect-knowledge oracle at every sun level, including bright no-car days. Export pays 12p while an
overnight kWh delivered at 90% costs 6.9p/0.9 = 7.7p, so storing rather than exporting solar loses about
4.3p/kWh.

A SOC taper via 43117 (43012 is BMS-overwritten; 43130 reportedly inert) gained no more than 6p/day and
lost 20-70p with daytime Tesla charging. It is not worth flash writes: endurance is unpublished, assume
10k and count every 43xxx write. Do not propose it again.

The one material opportunity is a daytime Tesla dispatch: self-use makes the 5 kW pack feed the car.
Temporarily forcing a charge window could save 30p-GBP 1.50 per occurrence, but it remains parked until
there is a bulletproof restore; a slot left active grid-charges at 30p.

The owner chose 43141 = 50 A deliberately: 0.17C on 300 Ah, roughly 5.5 h from the 10% floor. It may stop
around 97-98% after an empty start; that is accepted as kinder to LFP than holding 100%. **The floor is
10%, not 20%.**

Battery charge/discharge tops out at 5 kW, owner-stated, below the inverter's 6 kW. 43142 does **not** cap
self-use house supply; the owner says it limits timed discharge-to-grid. Leave its deliberate 50 A setting
alone rather than raising it toward the 100 A capability at 43013. With discharge windows unset, it has no
effect. Do not confuse 43012/43013 capability with applied 43141/43142 limits.

## Historical dead ends

Voltage logging is finished business. `voltage.py`, `dashboard.py` and `solis_voltage_log.db` existed to
evidence a DNO complaint, were deleted 2026-08-22 after the grid issue was fixed between 2025-03-30 and
2025-08-13, and remain only in history. The DB moved to `~/solis_voltage_log_archive.db`. Do not propose
launchd, backfills or gap analysis, and do not confuse the HA voltage chart with reviving the logger.

The archive spans 2025-03-29 to 2026-08-21 but contains only 2025-03-29/30, 2025-08-13/14 and a handful of
2026-08-21 rows -- not 17 months. Whole-table hour aggregates mix broken and fixed periods; **split by
date.** Like-for-like mornings changed from means 252.4 V at 09:00 / 255.8 V at 10:00, peak 262.4 V and
rising steeply in March, to a flat roughly 244 V in August; that flatness is the evidence the fix worked.

The other settled and rejected work is listed under "Settled -- do not reopen unasked" above. Do not
resurrect any of it because it looks simpler.

