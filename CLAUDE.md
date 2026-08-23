# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

Monitoring and control for a **Solis 6 kW hybrid inverter** (model `0x3105`) reached over Modbus through a **Solarman/IGEN WiFi data logger**. Standalone scripts, no package structure, no dependency manifest. The grid-voltage logger and its Flask chart (`voltage.py`, `dashboard.py`, `solis_voltage_log.db`) were retired and deleted on 2026-08-22 - see Context.

Hardware: **three Fox LV5200 modules in parallel** - ~300 Ah at 51.2 V, **~15.4 kWh** - 5 kW delivery, 90% SOH. (This file said 5.1 kWh until 2026-08-21; that is *one* module, and it made every fill-time estimate three times too short.) Logger serial `0000000000`, MAC `000000000000`, Modbus on port 8899. **Do not hard-code its IP** — see `solis_net.py`.

## Commands

No `pyproject.toml` or `requirements.txt`, and the system Python has none of the deps, so pass them per-run:

```bash
uv run --no-project python scan.py                                   # find logger (stdlib only)
uv run --no-project --with pysolarmanv5 python control.py show
uv run --no-project --with pysolarmanv5 python control.py charge-current 50 --apply
uv run --no-project --with flask --with pysolarmanv5 python settings_dash.py   # control UI on :5051
uv run --no-project python solar_forecast.py                          # tomorrow's kWh + verdict
uv run --no-project python solar_forecast.py record 2026-08-20 32     # log an actual
uv run --no-project python solar_forecast.py calibrate                # refit against actuals
```

No tests, no linter, no build step.

## Architecture

**Read path.** `settings_dash.py` owns it: a daemon thread holds one Solarman V5 session, sweeps telemetry and settings every 10 s, and serves the cache through `/api/state`. On any exception `_drop_session()` sets the session to `None` so the next sweep reconnects — that null-and-retry is the entire error strategy, inherited from the retired logger.

**Control path.** `control.py` writes holding registers over the same session: dry-run unless `--apply`, validates ranges, reads back every write, hard-refuses `PROTECTED`. It replaced an MQTT/Node-RED indirection (`mqtt_pub.py`, `mqtt_2.py`, deleted 2026-08-21) that published `struct.pack('<H', v)` to `nodered/solis/*` via a broker now requiring credentials nobody has. Direct Modbus needs no broker — **do not reintroduce that hop.**

**Address resolution.** `solis_net.py` is the single place that knows how to reach the logger: `$SOLIS_HOST` override → UDP broadcast discovery by serial → `LAST_KNOWN` fallback. `control.py` and `settings_dash.py` both go through it, so a DHCP move needs no code change. `scan.py` is the standalone version of the same broadcast (`WIFIKIT-214028-READ` to port 48899, reply `ipaddress,mac,serial`).

Discovery is layer-2 only — it works when the client shares a segment with the logger, and silently falls back otherwise. Across a routed boundary, set `SOLIS_HOST`.

**Solar forecast.** `solar_forecast.py` answers one question: will tomorrow's sun refill the battery,
or should the off-peak window? `tomorrow_kwh()`, `today_kwh()`, `verdict()` and `tomorrow_weather()` are
the whole interface; the dashboard's Solar today tile and Tomorrow panel use them. Open-Meteo drives it - free, no key, and it
serves *past* days, so `calibrate` refits against actuals in `solar_actuals.json`.

**Roof geometry is surveyed, not guessed (2026-08-21).** The owner supplied a roof survey giving
compass aspects and pitches; `PLANES` now carries per-plane tilt because the two pitches differ.
Roof 1 is 12 x 400 W at **207 deg SW / 21 deg** (survey: 25.1 m2, 990 kWh/kWp), roof 2 is 8 x 400 W at
**117 deg SE / 24 deg** (18 m2, 920 kWh/kWp) - 20 panels, 8.0 kWp. Open-Meteo azimuth = compass - 180,
so +27 and -63. The earlier guess (-15 and -100, one 25 deg pitch) had both planes too far east; it
survived because a wrong azimuth is largely absorbed by the `EFFECTIVE_KWP` fit. Refitting moved RMSE
only 1.66 -> 1.59 over six late-summer actuals, so **do not treat that as the justification** - the
reason to keep the surveyed numbers is that they are measured, and azimuth error grows as the sun
swings away from midsummer. The survey's own annual figure (4752 + 2944 = 7696 kWh) lands within 1%
of the 7743 at 33039/33040, which is *suspected* to be last year's total but is not confirmed.

**Irradiance is the mean of five weather models** (`MODELS`), and this is the single most important
thing in the file. Measured against seven recorded actuals: ensemble RMSE **2.19 kWh**, best single
model 3.26, worst 5.63. Open-Meteo's default `best_match` resolves to **UKMO** at this latitude, and
UKMO alone caused a 7 kWh miss on 2026-08-20 (3.65 kWh/m² against ECMWF's 5.90). But note which model
is worst keeps changing: over the full seven actuals it is **ECMWF** that scores worst (5.63) and
UKMO sits mid-pack (4.28), so there is no single model to promote - which is the whole argument for
averaging. Do not "simplify" back to one model. A model reporting all-zero for a day is missing
data, not darkness - `day_kwh` drops it rather than averaging in a zero.

`EFFECTIVE_KWP` (6.33) is fitted, but against the 8.0 kWp nameplate it implies a performance ratio of
0.79, which is what a real system does - the fit is measuring the roof, not absorbing weather error.
Earlier single-source fits pushed it above nameplate (PR > 1.0), which was the clue the source was
wrong. `verdict()` returns low/borderline/high and declines to call anything within `UNCERTAINTY_KWH`
of the line. Two dead ends, both re-checked and rejected: forecast.solar low-balls this array badly,
and a pvlib Perez transposition scored no better than Open-Meteo's own tilted irradiance.

Adding 2026-08-21 (predicted 29.4, actual 26.0) was the first material *over*-prediction - every
earlier miss ran low - and took RMSE over seven actuals to **~2.1-2.2**. Treat that as a soft number:
it moved 2.07 -> 2.19 on nothing but a cache refetch, because Open-Meteo keeps revising the current
day. Refitting would move `EFFECTIVE_KWP` 6.33 -> 6.24 for 0.06 kWh of RMSE, which is noise; left
alone deliberately. Bias is +0.36 at 6.33 and -0.09
at 6.24, so the model is close to unbiased either way and the spread, not the scale factor, is what
is left to improve.

**Settings dashboard.** Tiles across the top: solar now (PV watts plus per-string split), battery
(SOC, charge/discharge direction and watts, V/A/health), house load, solar today (kWh so far against
the day's forecast, plus yesterday), and grid (import/export watts as the headline, voltage
secondary, today's kWh in/out). A sixth Car tile (2026-08-22, Octopus-gated like the £ line) headlines the
target SOC from vehicleChargingPreferences ("75 % by 05:30" - verified live), with the plug
state as a dot+word in the tile's top-right corner (the grid tile's voltage slot) and one
dim dot-separated line for the live charge or the plan - never a wattage, since Octopus
exposes no car SOC or charger power. **Unplugged hides the tile entirely** and the row
falls back to five-across (renderCar toggles `hidden` + the grid's `six` class; the
template no longer hardcodes `six`); anomalies (no link / unknown / unavailable) stay
visible because they mean something is wrong. The plug word folds SmartFlexDeviceState:
CAPABLE = plugged in, IN_PROGRESS = plugged in with a plan (**not** charging - it held
at 16:14 with the first dispatch ten hours off, fixed 2026-08-23), BOOSTING = charging,
NOT_AVAILABLE = unplugged - the community/HA reading, observed live but not
Octopus-documented. "Charging" otherwise means a dispatch brackets now.
Six tiles fit one row only past ~1276px viewport (hence .wrap at 1300px and the .grid.six
3-column rule between 1068-1275px). Sections below: tomorrow's weather and verdict, charge current,
charge windows (with a timed-charging on/off switch in the section
head - 43110 bit1, read-modify-write so the other mode bits are untouched; off dims the
windows *and* the charge-current section as inert, and arms like Clear because a plugged-in
Tesla drains the unheld battery at up to 5 kW), work mode (read-only), diagnostics. `settings_dash.py` is the browser front end for
the control path. A daemon
thread holds one Solarman session and refreshes a cache every 10 s; the browser polls `/api/state`,
so page refreshes never hit the logger. `/api/state` carries `read_at_epoch` as well as the display
string, because a *frozen* poller is otherwise indistinguishable from a healthy one - the browser
ages it and dims the tiles past 30 s rather than only reddening the status dot.

**The tile sub-details are bars, not sentences (2026-08-21).** Each tile's second line used to be
prose; three now carry a graphic that the text alone could not convey. Solar now draws one bar per
live PV string, filled against *that plane's* nameplate rather than a shared scale - 12 x 400 W SW
and 8 x 400 W SE, so a common axis would make the smaller roof look permanently weak. Solar today
puts a notch on its track where *yesterday* finished, so
today's progress reads against something. The Battery tile got the same treatment - a bipolar
charge/discharge bar - and the owner **reverted it on sight**: SOC already has a bar directly above,
and a second bar in the same tile read as a competing gauge rather than as direction. Its text line
("up arrow charging N W") is the version that stays. Three things here are load-bearing. The PV rows are keyed
on the register index carried in `pv_strings[].string`, never on array position, because dead strings
are dropped server-side and the two planes wake at different times - a dawn where only SE is live
would otherwise be labelled SW. `PV_PLANES` maps that index to SW/SE on the **inferred** basis that
358.4 V : 236.0 V is 12 : 8; it is a one-line table so a correction is one edit, and it has not been
confirmed against a traced cable. And `[hidden]{display:none!important}` is required, because
`.pvrows` is `display:grid`, which otherwise beats the `hidden` attribute and leaves the bars
showing all night.
**Rules engine (2026-08-22): the poller can now write, not just read.** `RULES` in
`settings_dash.py` is evaluated after every fresh sweep, under `_lock`. The first rule:
battery SOC below 20% with timed charging off -> set 43110 bit1 (read-modify-write). Rules
fire **at most once per episode** - armed while the condition is false, fire on the first
true sweep, disarmed until it goes false again - so an owner who flips the switch back off
after a firing is respected, not fought. Never fires on a failed sweep or an SOC of 0 (a
bogus read, not an empty battery). Firings surface in the diagnostics "last rule" row (kept
in `_rule_event`, outside `_state`, which `_refresh()` rebuilds). More rules are planned;
add them to `RULES`, keep the once-per-episode latch.

**Write safety lives in the front end as well as the guards.** Apply disables every button until the
readback lands (a Modbus write is slow enough to double-click through, and each click was a second
register write); Clear arms to "Confirm?" for 5 s before it will fire, because clearing slot 1 wipes
the off-peak window this system depends on; slot Apply stays disabled until both times are filled.
Edited-but-unapplied inputs mark themselves "not applied" and are no longer overwritten by the poll -
the slot rows rebuild only when the *inverter's* values change, so a half-typed time survives.
Slots 2 and 3 sit behind a disclosure: they are unused forever and do not deserve equal weight.

**Overlapping polls must not roll the UI back.** `tick()` fires every 5 s without awaiting the
previous fetch, and a write renders its own fresh state, so responses can land out of order. `render()`
drops any sweep whose `read_at_epoch` is older than the last one rendered - without that, a request
begun before an Apply could return after it and rebuild the slot rows from pre-write values, which
then look live and invite a second Apply that undoes the first. A sweep timestamped in the *future*
counts as stale too: it means the timestamp cannot be trusted either way.

**The write lock is time-based, and time-based means it needs its own clock.** Every control that can
start a write is disabled whenever the sweep is stale, errored, or the dashboard is unreachable
(`applyLock()`), and `post()` refuses regardless of what the buttons look like. Three traps, all hit:
`locked` starts **true** and `applyLock()` runs before the first `tick()`, because until a sweep lands
nothing about the inverter is known and the page was previously fully writable in that gap; staleness
used to be judged only inside `render()`, which runs only when a poll *returns*, so a hung fetch or a
background tab whose timers the browser throttled left the last verdict standing on an hour-old
reading - hence `watchdog()` on its own 2 s interval plus a re-check inside `post()` at click time;
and `post()`'s `finally` restored the button states it snapshotted before the write, which by then
could be stale, so `applyLock()` now has the last word. A failed write sets `locked` and records
`UNKNOWN` in the last-write row, because a rejected fetch does not mean the registers were untouched.
`busy` returns flash a banner rather than returning silently - an armed Clear swallowed that way looks
exactly like a Clear that fired.

**A future timestamp used to freeze the page permanently.** `lastEpoch` was updated before the
freshness test, so one NTP step forward parked it an hour ahead; every later legitimate sweep was then
dropped by the out-of-order guard and the page sat locked until the clock caught up. A sweep stamped
beyond `STALE_SECONDS` in the future is now distrusted *and* not recorded.

**A window write is four registers, and failing partway is a real state.** `/api/write` re-reads and
returns the state on the error path as well as the success path, and the browser renders it either
way - otherwise a half-written window sits in the inverter while the page still shows the old
complete one. The sweep is timestamped immediately after the register reads, before `_forecast()`,
which can block on the network and would otherwise backdate itself onto fresh telemetry.

**The charge-current row carries a slider and a live translation**, because the register value alone
says nothing: 50 A reads as `50 A at 52.9 V is 2.6 kW - fills from 87% in about 15 min, inside the
6 h window`. It uses the live battery voltage and SOC, states facts and stops - it does
not recommend a rate, which is the owner's call (see the charge-rate rule below).

**Below the fill rate the row answers in percent, not in hours.** A rate too low to fill used to
report the time it *would* take - "about 58 h, longer than the 6 h window" - which is true and
useless: the window is fixed, so the question is never how long a full charge takes, it is what SOC
the morning starts at. It now reads `1 A at 53.1 V is 0.1 kW - the 6 h window adds +2%, reaching
82% - 2.8 kWh short of full`. Three things in that sentence are traps that were hit and fixed:
the reached figure is clamped to 99, because rounding printed "reaching 100% - 0.0 kWh short of
full" in one sentence for 24 (SOC, amps) combinations clustered exactly on the ~48 A fill rate and
the low overnight SOC the line exists to be read at; a shortfall under 0.05 kWh says "just short of
full" rather than printing a contradictory `0.0`; and `windowHours === null` means two different
things - no sweep yet, and *slot 1 is unset* - so the unset case says "no charge window is set, so
this rate does nothing" instead of quoting a fill time for a rate that is not being applied, which
contradicted the line directly above it. Both the window
length and the times in the row's description are **read from slot 1**, not written as literals: the
window has already been 04:30 and 03:30, and a hardcoded "23:30-05:30" would have been quietly wrong. An earlier attempt
capped the row at 620px to close the label-to-input gap; that just left half the panel empty. The
slider is what legitimately fills that width. `/api/write` re-imports `control.py`'s constants and guards
rather than restating them, adds its own refusal for *any* discharge-window write, and re-reads after
every write. Bound to `127.0.0.1` deliberately — it writes holding registers. HTML lives in the
`PAGE` constant and is served with `render_template_string`, so there is no template directory to edit.

## Register map (verified live 2026-08-21)

Input registers (FC 0x04) are telemetry; `43xxx` holding registers (FC 0x03/0x06) are settings.

| Register | Meaning |
|---|---|
| 33035 / 33036 | **Solar generated today / yesterday (×0.1 kWh)**. Today resets at midnight. |
| 33049 / 33050 | PV string 1 voltage / current (×0.1) |
| 33051 / 33052 | PV string 2 voltage / current (×0.1). Strings 3–4 (33053–33056) unused. |
| **33057 + 33058** | **Total PV power (W), u32 pair — DC side** |
| 33071 | DC bus voltage (×0.1). Read 397.7 V; inferred from magnitude, not confirmed. |
| 33073 | Grid phase A voltage (×0.1). Also mirrored at 33137. |
| **33079 + 33080** | **Inverter AC output power (W), u32 pair.** The AC-side counterpart to 33057/33058. |
| 33094 | Grid frequency (×0.01). Read 50.10 Hz; inferred from magnitude, not confirmed. |
| 33133 | Battery voltage (×0.1) |
| 33134 | Battery current (×0.1) |
| **33135** | **Battery direction: 0 = charging, 1 = discharging.** Not current. |
| 33139 / 33140 | Battery SOC / SOH (%) |
| 33147 | House load (W) |
| **33149 + 33150** | **Battery power (W), u32 big-endian pair, unsigned — sign comes from 33135** |
| 33161–33164 | Battery **charge** energy: total (u32), today, yesterday (×0.1 kWh) |
| 33165–33168 | Battery **discharge** energy, same layout |
| 33169–33172 | **Grid import** energy, same layout |
| 33173–33176 | **Grid export** energy, same layout |
| 33177–33180 | **House consumption** energy, same layout |
| 33251 / 33252 | Meter voltage (×0.1) / current (×0.01) |
| **33257 + 33258** | **Grid power (W), s32 pair: positive exporting, negative importing.** Mirrored at 33263/33264. |
| 33283–33286 | Meter import / export energy totals (×0.001 kWh) — same values as 33169–33176, finer |
| 43010 / 43011 | Battery capacity (Ah) / type code |
| 43012 / 43013 | Battery profile **capability**: 100.0 A each way (1C) |
| 43110 | Work mode bitfield — bit0 self-use, bit1 time-of-use, bit5 grid charging, bit9 battery healing |
| 43141 / 43142 | **Time-of-use** charge / discharge current limit (×0.1 A) |
| 43143 + (n−1)×8 | Slot n (n=1..3): +0 charge start (h,m), +2 charge end, +4 discharge start, +6 discharge end |

The PV and battery power registers were confirmed on 2026-08-21 by closing the energy balance:
PV 2033 W + battery 1063 W = house load 3008 W, with 33057/33058 agreeing with the string products
(358.4 V × 3.5 A + 236.0 V × 3.3 A) and 33149/33150 agreeing with 52.9 V × 20.1 A.

Slot stride 8 and the hour/minute pairing were confirmed by decoding the live off-peak charge window
(23:30–04:30 when first read on 2026-08-21, later 23:30–03:30 — read it, don't assume it). Slots 2–3 and all discharge windows are unset, deliberately (see below).

**Grid power is 33257/33258, signed: positive exporting, negative importing.** Verified twice on
2026-08-21. First against a clear-sky export - PV 5834 W DC, AC output 5800 W, house 875 W, so
4925 W was leaving for the grid, and the register read +4984. Then the sign resolved itself: read as
*unsigned*, an import surfaced on the dashboard as **4294967253 W**, which is 2^32 − 43, i.e. −43 W.
Read it as s32 or imports appear as ~4.29 billion watts. An earlier attempt took the magnitude here
and the direction from `house_load > ac_power`; that worked but is no longer needed.

**Never derive grid flow from PV and battery.** `house_load - pv_power ± battery_power` looks
correct and is systematically wrong: 33057/33058 and 33149/33150 are **DC** registers, so the
inverter's conversion loss (~2%, over 100 W near full output) lands in the residual and reads as
phantom export. It was wrong in exactly the way that hides - the error is small, and it vanishes
whenever the battery is idle, so it validates fine against a quiet moment. It was caught by a
screenshot showing "exporting 108 W" while the battery discharged 1263 W to cover a 3840 W house
load. Use 33079/33080 (AC output) if an AC-side balance is ever needed.

The daily counters at 33161-33180 were identified by closing yesterday's energy balance:
PV 31.7 + import 15.2 + battery out 6.2 = 53.1 against house 21.8 + export 22.7 + battery in 8.2 =
52.7, 0.4 kWh apart. 33036 independently agreed with the 32 kWh the owner recorded for 2026-08-20,
which is what pins the x0.1 scaling.

## Rules that are not obvious

- **Never write `43038-43049` or `43090-43097`** — DNO-mandated G98/G99 grid protection (over-voltage 262 V, under-voltage 184 V, freq trips 47.5/51.5 Hz, plus delays). Not permitted, not merely risky. `control.py` guards these; keep the guard.
- **The charge window is fixed; the charge rate is the lever.** 23:30–05:30 is the Octopus cheap-rate
  window, so grid import inside it is cheap and the window should not be trimmed. It has a second job:
  a Tesla on Octopus Intelligent charges at unpredictable times inside that same window, and in
  self-use the house battery would otherwise discharge to feed it. An active *charge* window keeps the
  battery charging rather than draining. So tune `43141` (charge current, currently 50 A) and leave
  `43143` alone. Advice that says "shorten the window" is wrong for this setup.
- **The rate that just fills the pack in the window is ~48 A from 0%, ~45 A from the 10% floor.**
  ~15.4 kWh over the 6 h window is 2.56 kW average, about 48 A at 53 V; from the real 10% floor
  it is ~14.6 kWh after losses, 5.5 h at 50 A. So 50 A is not generous headroom - it is roughly
  the minimum that reaches full by 05:30, and anything well below it leaves the battery short. This corrects an earlier bullet that put the crossover at ~16 A: that
  was computed from one module's 5.1 kWh instead of the three-module pack, and its advice ("drop
  below 16 A to leave room for solar") would have left the pack two-thirds empty. Trimming the rate
  to leave headroom for a high-solar day is still the owner's call, not an optimisation to apply
  unasked - but the number to trim from is 48, not 16.
- **Charge overnight, export the sun - settled 2026-08-23, do not re-open.** `strategy_sim.py`
  (half-hourly day sim, Monte-Carlo loads, SOC chained across days, live Octopus rates) found
  plain timed charging - window on, 23:30-05:30, self-use, no daytime limit - within 2-8p/day of a
  perfect-knowledge oracle at every sun level, including bright days with no car plugged in. The
  reason is the tariff: export pays 12p, an overnight kWh costs 6.9p/0.9 = 7.7p delivered, so
  every kWh of sun stored instead of sold loses ~4.3p. A SOC taper via 43117 (researched; 43012 is
  BMS-overwritten, 43130 reportedly inert) gains <=6p/day and *loses* 20-70p when the Tesla
  charges in daytime - not worth flash writes (endurance unpublished, plan on 10k; count every
  43xxx write). Do not propose it again. The one lever with money in it is a daytime Tesla
  dispatch: in self-use the pack feeds the car at 5 kW, and forcing the charge window on for the
  dispatch is worth 30p-1.50 per occurrence - parked, needs a bulletproof restore (a slot left set
  grid-charges at 30p). The owner set 43141 to 50 A deliberately: 0.17C on the 300 Ah pack, fills
  from the 10% floor in ~5.5 h, may stop at 97-98% on an empty-start night, which is accepted as
  kinder to LFP than holding at 100%. The floor is 10%, not 20%.
- **The owner does not want to discharge to grid.** All three discharge windows are intentionally unset and must stay that way — never propose enabling one, and never frame the unused slots as headroom to exploit. Charging (slot 1, 23:30–04:30 off-peak) is the only timed behaviour wanted.
- **Battery charge/discharge tops out at 5 kW** (owner-stated), under the inverter's 6 kW.
- **`43142` does not cap house supply.** Per the owner it limits timed discharge *to grid* (export rate); self-use house supply is demand-driven and bypasses it. Deliberately set to 50 A — do not "helpfully" raise it toward the 100 A capability in `43013`. With every discharge window unset it has no effect at all.
- **`33135` is a direction flag, not battery current.** Current is `33134`. Reading 33135 as amps
  yields a plausible-looking 0.1 A and a battery power near zero, which is why the error survived a
  whole session unnoticed. Powers come from the u32 pairs (33057/33058, 33149/33150), not from
  multiplying volts by amps.
- **Don't confuse `43012`/`43013` (what the battery can do) with `43141`/`43142` (what is applied).**
- **Network topology is mid-change (2026-08-21).** A FRITZ!Box serves `198.51.100.0/24`. Two Tenda Nova meshes hang off it, split by band — a fast main mesh, and a 2.4 GHz IoT mesh carrying the inverter. The IoT mesh *was* NATing `192.0.2.0/24` (gateway `192.0.2.1`, WAN side `198.51.100.49`), which made the inverter unreachable from the main mesh: `5.x` could reach `178.x` outbound, never the reverse. The owner is switching that mesh to **bridge mode**, after which everything is flat on `178.x` and the logger takes a new Fritz-issued address. Let `solis_net.py` find it; don't assume any address.
- **Reaching the logger from the main mesh goes through a port forward**, added in the Tenda app 2026-08-21: `198.51.100.49:8899 → 192.0.2.45:8899` TCP. Verified carrying Modbus. Bridge mode was tried first and the Nova silently rolled back to Dynamic (twice), despite a valid wired uplink — don't burn time retrying it.
- **The forward is LAN-only, not internet-facing.** Mesh B's WAN is the Fritz LAN. The FRITZ!Box 7530 AX (public IP as of 2026-08-21: `<wan-ip>`) has **zero** port mappings, so two NATs sit between the inverter and the internet.
- **The forward has no DHCP reservation behind it.** The Tenda app wouldn't accept a MAC binding, so if the logger's lease moves off `192.0.2.45` the forward breaks silently — the symptom is `control.py` failing only from the main mesh while working from the IoT mesh. Fix by re-pointing the forward, or set a static IP on the logger itself.
- **UPnP `AddPortMapping` does not work on this Tenda** (SOAP 500), almost certainly because it refuses mappings aimed at a device other than the requester. Reading mappings works; the app's own forwards do not show up in the UPnP list. Use the app.
- **Discovery is intermittent.** The logger often ignores the `WIFIKIT` broadcast, especially with a Modbus session open. `solis_net.resolve_host()` falls through to its `CANDIDATES` list for this reason — don't "fix" discovery by removing the fallback.
- **`settings_dash.py` needs `OCTOPUS_API_KEY` and `OCTOPUS_ACCOUNT` in its environment** — they
  are exported from `~/.zshrc`, which a non-interactive shell does not source. Restarted without
  them, the dashboard hides the £ line and the car row *silently* (that gating is by design: no key
  configured means the rows are noise). Hit 2026-08-22: a restart from a tool shell "lost" the £
  line and it read as a UI regression. Source the exports before relaunching.
- **The logger allows one Modbus session at a time.** With `settings_dash.py` running, `control.py`
  dies with a bare `_queue.Empty` from pysolarmanv5 — that is contention, not a network fault. Read
  from `curl localhost:5051/api/state` instead, or stop the dashboard first.
- **forecast.solar low-balls this array badly** and was dropped for Open-Meteo. Its free tier returns
  damped *hourly* watts: it predicted a 3.5 kW peak for a day the owner says hits 6 kW, and 26 kWh
  against the calibrated model's 39 kWh. Don't reach for it again.
- **Never quote a kW figure from `hourly_kw()`; the Tomorrow panel gives timing only.**
  `EFFECTIVE_KWP` (6.33) is nameplate 8.0 x a fitted PR of 0.79, and that PR is fitted to daily
  *energy*. It bundles losses that barely exist at solar noon - part-load inverter efficiency,
  diffuse-heavy shoulder hours, low incidence angles - so used as an instantaneous power it reads
  roughly 20% low by construction. On 2026-08-22's irradiance the same curve gives 4.41 kW at 6.33
  and 5.58 kW at nameplate. The five-model mean flattens it further: that day the models peak at
  5.55 / 4.86 / 4.66 / 4.51 / 3.50 kW, so averaging costs the peak what it wins on the total. Both
  effects leave the daily kWh right and the peak wrong, which is why the owner read "peaks around
  13:00 at 4.4 kW" as nonsense - the inverter shows near 6 kW. It cannot be fixed by rescaling
  `hourly_kw()`, because the same array feeds the four daylight blocks whose kWh must sum to the
  calibrated daily total: energy wants 6.33, power wants 8.0. `peakLine()` therefore reports the
  contiguous span within 80% of the peak ("strongest 11:00-15:00") and no number. Restoring a kW
  figure needs a separate peak computation *and* recorded peak actuals to fit it - there are none.
- **The 6 kW inverter ceiling costs almost nothing** - but the 0.7 kWh below is computed with
  `EFFECTIVE_KWP` and so **understates clipping**, for the reason in the bullet above. Treat it as
  a floor, not a measurement. On a clear midsummer day (2026-07-20, 49 kWh
  actual) the model loses 0.7 kWh to clipping. 12 south + 8 east never peak together, so the DC curve
  is broad rather than spiked. Don't propose a bigger inverter on clipping grounds.
- **The daily `weather_code` is a worst-of-day aggregate** and will report "overcast" for a mostly
  sunny day, contradicting a high kWh estimate. `_sky()` uses it only for precipitation and describes
  clear/cloudy from mean cloud cover instead. That still left the *overnight* half of the day in
  scope - a 03:00 shower named the whole day "light drizzle" above four dry daylight blocks - so
  `_daylight_sky()` now rebuilds the summary from the sunrise..sunset hours only, falling back to the
  daily fields when hourly data is missing. Verified 2026-08-21: hour 0 carried code 51, daylight
  hours topped out at 3, and the headline went from "light drizzle" to "partly cloudy".
- **`tomorrow_weather()` also returns today's sunrise/sunset** under `"today"`, looked up by date in
  the same two-day response. The dashboard needs it to tell nightfall from a fault: "solar now 16 W"
  at 20:15 is correct, and the tile says "sunset 20:08" instead of glowing like a live reading.
- **`_weather_memo` is keyed on the target date, not just age** (fixed 2026-08-21). With a 3600 s TTL
  and an age-only guard, a memo built at 23:55 was still "fresh" at 00:05 and answered for the wrong
  day - the Tomorrow panel showed *today's* weather for up to an hour after midnight. `_series()` does
  not have this bug: it caches a dict keyed by date and each lookup names its day.
- **Open-Meteo azimuth is 0 = south, -90 = east, 90 = west** - verified by probing, not assumed. A
  same-hour peak across all azimuths means a cloudy day, not a broken convention.

## Context

**Voltage logging is finished business — do not rebuild it or suggest doing so.** `voltage.py`, `dashboard.py` and `solis_voltage_log.db` were built during a past period of grid instability to evidence a DNO complaint. The issue was fixed between 2025-03-30 and 2025-08-13 and the grid is behaving. All three were deleted from the repo on 2026-08-22 (they remain in git history before that commit); the database was moved to `~/solis_voltage_log_archive.db` as evidence. Don't propose launchd jobs, backfills, or gap analysis.

**The archived DB is a few days, not 17 months.** Despite spanning 2025-03-29 → 2026-08-21, it holds only 2025-03-29/30, 2025-08-13/14 and a handful of 2026-08-21 rows. Any hour-of-day aggregate over the whole table pools the broken period with the fixed one and is misleading — always split by date first. Like-for-like, mornings went from mean 252.4 V at 09:00 / 255.8 V at 10:00 (peak 262.4 V, climbing steeply — the PV-driven rise) in March, to a flat ~244 V through the same hours in August. That flatness is the evidence the fix took.

Active interest is now `control.py` — battery and tariff settings — not monitoring.
