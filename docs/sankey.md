# The Sankey chart: layout, measured flows, card mechanics and the 5-minute patch

*Split out of `CLAUDE.md` on 2026-09-13 to keep the always-loaded file small. This is the same text, unchanged. The rules and traps that apply even when you are NOT reading this file stayed in `CLAUDE.md`.*

### Sankey node colours (2026-08-30)

Colours carry meaning here and were chosen deliberately:

| Node | Colour | Why |
|---|---|---|
| Grid import | `var(--error-color)` red | The most expensive flow on the chart. It previously shared `--info-color` with House, so the thing costing the most money read as house-coloured. |
| Grid export | `#a78bfa` violet | It earns money; red was actively misleading. The violet matches the Power card's grid trace. |
| Tesla | `#ffffff` white | The owner's car is white. It is not an accent from the palette, so do not "correct" it to one. |
| House | `var(--primary-color)` | Since 2026-09-11 it excludes the air con as well as the car, but keeps the name "House" -- the owner's words were "house stays named house, but is essentially rest of house". |
| Air con | `#ff69b4` hot pink | Owner's choice, and still the colour now the node is back (2026-09-11). Yellow `#ffd60a` was tried first and rejected on sight: Solar's `--warning-color` resolves `rgb(255,166,0)` on this theme and the two blurred together. Do not retry a yellow or amber here. |
| Solar / battery | warning (orange) / success | unchanged |

**Promoting Tesla to a peer of House was built, reviewed and reverted on 2026-08-30 at the owner's
request. Do not rebuild it unasked.** It worked: Tesla became a section-1 sink fed directly by the
three sources, House carried `subtract_entities: [sensor.tesla_home_charging_energy]` to avoid
double-counting, and the allocation resolved correctly -- solar 5.4 export / 4.5 battery in / 2.9
house / 0 tesla, battery out 6.5 house / 0 tesla, grid import 5.32 **tesla** / 5.28 house, with House
reading 14.7 instead of 20. The owner preferred the car back inside the house total. Two reusable
findings survive it:

- **`subtract_entities` works on any plain `entity` node, including one with outgoing links.**
  Verified in the bundle at offset 62236: `r.state -= Math.min(i, r.state)`, clamped so it cannot go
  negative, and the card's own `autoconfig` uses it on nodes that have children. A subtract-then-split
  node is a supported shape if it is ever wanted again.
- **Read the allocation from `base.__connections`, not from the picture.** The `SANKEY-CHART-BASE`
  element inside the card's shadow root exposes every resolved `{parent, child, state}`. That is the
  only reliable way to check a reordering, because a zero-valued connection is invisible on screen
  and looks identical to a link that was never declared.

### Sankey v2, deployed 2026-08-30: the split IS measured now

v1's allocation was greedy and editorially ordered; v2 measures each source->sink flow and hands the
card the number. **"Card mechanics" below still describes this card and must be read before touching
the layout**, even though v1's allocation narrative no longer describes the live chart.

The graph is **two columns, 9 nodes, 15 links**: Solar / Battery out / Grid import feed House, Tesla,
Air con, Battery in, Grid export and Inverter, and every sink is a terminus. Each link carries
`value: sensor.flow_<source>_to_<sink>_daily`, which the card applies as
`min(parent_remainder, child_remainder, value)`, so declaration order no longer decides the picture.
House, Tesla and Inverter have no counter of their own: each is `entity_id` plus `add_entities` over
its three inbound meters. Inverter is DC/AC conversion loss and parasitic draw, ~2.5 kWh/day, grey
`#6b7280`.

**Air con became its own terminus on 2026-09-11, at the owner's request** ("so that air con comes out
as a node endpoint separate from 'house'"). The carve-out order is Tesla first, then air con:

    T  = min(max(0, tesla), H)
    A  = min(max(0, aircon), H - T)
    Hr = H - T - A

That ordering is deliberate and load-bearing. The car is the larger and far better-measured load, so
putting it first leaves every Tesla ribbon numerically untouched by the addition, and leaves the
Inverter node unchanged too because `Hr + T + A == H` exactly as `Hr + T == H` did. **The blindness
runs one way only**: `A` reads `T`, so the three air-con flows depend on the Tesla channel even
though no Tesla flow depends on air con. Flows that a dead TeslaMate unmoors therefore went from six
of twelve to **nine of fifteen** -- do not "simplify" that count back to six, it would publish three
ribbons computed from a dead input.

**The Inverter node was validated against physics on 2026-08-31 and is correct.** On a pure
grid-charging night it read 0.580 kWh two hours in; integrating the raw 5-minute power statistics
over the same window gives a residual of 5.443 - 4.348 - 0.503 = 0.592 kWh, 2% away. The check that
matters is that the power path reproduces the independently measured 0.887 charge efficiency
(4.348 / (5.443 - 0.503) = 88.0%). **Do not "validate" this node against `house_consumption_today`.**
That counter is a derived residual that already contains the conversion loss, so the comparison makes
the node look like ~0.5 kWh of double-counted house load when it is not -- see "What the daily
counters do and do not prove". Two independent auditors both stalled on exactly that trap.

Source of truth is `sankey_tests/layout_v2.py`, with the design and reasons in
`sankey_tests/BRIEF_V2.md`. **The deploy scripts are in git at the repository root, because the
originals lived in a session scratchpad and were lost with it -- which left the chart
un-redeployable.** Both are dry-run unless `--apply`:

    sankey_helpers.py   creates/updates the HA helper chains from sankey_tests/flow_sensors_v2.yaml
    sankey_apply.py     writes layout_v2.build() onto the energy-live dashboard, backing the card up
                        to sankey_card_backup.json first

Run them in that order: `sankey_apply.py`'s guard 6 refuses to deploy a link whose `value:` entity has
no statistics rows, which is exactly the state a new flow meter is in until `sankey_helpers.py` has
created it. `sankey_helpers.py` likewise rewrites existing templates BEFORE creating new chains --
briefly under-counting a sink is a missing number, briefly double-counting one is a wrong number.

Two API facts that cost time on 2026-09-11 and are not guessable:

- **Config and options flows are REST-only.** There is no `config_entries/options/flow` websocket
  command; HA answers it with a bare `unknown_command`. Drive both over
  `POST /api/config/config_entries/flow` and `.../options/flow`, where a flow step takes the user
  input as the WHOLE body, not wrapped in a `user_input` key. `config_entries/get` over websocket is
  fine.
- The template options-flow schema is `state`, `unit_of_measurement`, `device_class`, `state_class`,
  `device_id`, `additional_options` -- **`name` is deliberately absent**, so an update must omit it
  (HA preserves the stored name) and sending it is rejected. Probe a schema by starting a flow and
  DELETEing it; that is read-only in effect.

**Three nodes were removed on 2026-08-30 at the owner's request. "Stored" and "Rest of house" stay
removed; do not re-add either unasked. "Air con" was re-added on 2026-09-11, by request.** "Stored"
(a `remaining_parent_state` child of Battery in) restated Battery in's own number one column right.
"Air con" and "Rest of house" originally went with the whole third column, because dropping House's
device split is what makes every sink a terminus.

The 2026-09-11 request did **not** reinstate that third column. Air con is a peer of Tesla in
section 1, fed directly by all three sources and feeding nothing, so every sink is still a terminus
and the per-source colouring is kept -- which is precisely why feeding a section-2 Tesla from a
section-1 House was rejected back then, and remains rejected now.

**The link `value:` entities are read as `sum(change)` from statistics, NOT live.** They go into the
same `entityIds` array as the nodes (char 100603) and their state is overwritten by the statistics
result (96959). A `value:` target with no statistics rows returns **null**, so `Number("null")` is
NaN, that NaN lands in the source's `parent_spent`, and **every later ribbon from that source silently
goes to zero**. The load-bearing property is `state_class`, not availability -- an `unavailable`
entity is still present in `hass.states` and still gets substituted. `sankey_apply.py`'s guard 6
checks this and must stay.

**A chart applied mid-day looks broken and is not.** The flow meters are daily `utility_meter` helpers
created at 17:00 on 2026-08-30, while Solar / Battery / Grid / Export boxes read the inverter's own
full-day counters. Until both sides have covered the same window the source bars are full height and
the ribbons are hairlines. Both reset at midnight, after which they measure the same day and the
proportions are honest. Do not "fix" this by rescaling anything.

### The air-con channel is measured differently from everything else

`sensor.aircon_power` is **a `derivative` helper, not a sensor**, and everything awkward about the Air
con node follows from that. `lg_thinq` publishes twelve entities for the unit and **not one of them is
power** -- checked in the entity registry 2026-09-11, there is not even a disabled-by-default one to
enable. What it does publish is `sensor.living_room_air_conditioner_energy_today`, in Wh.

So the chain is `energy_today` -> `derivative` (`unit_time: h`, `round: 2`, `time_window: 0`,
`max_sub_interval: 2h`) -> `sensor.aircon_power` -> the three `flow_*_to_aircon_*` chains. Measured
cadence over four days:

    energy_today       95 changes / 4 days, median gap 3600 s
    energy_this_month   8 changes / 4 days, median gap 22594 s

**Use `energy_today`. The monthly counter updates once a day** and was rejected for that. Three
consequences, all accepted and none of them faults to fix:

- **The air-con split is hourly and lags by up to an hour**, while every other ribbon is sampled at
  10 s. The derivative holds one value for an hour and that value describes the hour just *past*, so
  around sunrise and sunset the air con is attributed to the following hour's source mix. Daily
  totals are unaffected.
- **One hour a day is not attributed at all.** `energy_today` resets at about 00:36 (the cloud poll,
  not midnight), so the 23:36-00:36 delta is negative and clamps to zero; that hour's air con stays
  inside House. Overnight standby is about 10 Wh/h, so the loss is small and in the safe direction.
- **The first value after the helper is created is inflated** and self-corrects within the hour.
  Observed 2026-09-11: created 12:18, first step 12:36 reading 40.55 W for a 12 Wh hourly step,
  because the derivative divided by the 17.9 minutes since creation rather than by an hour.

**`sensor.aircon_power` is the one reading taken with `| float(0)`, and it is deliberately ABSENT from
the availability guard, which still counts exactly five entities.** Both are the opposite of the rule
the other five follow and both are load-bearing: the derivative reads `unknown` for up to two hours
after every restart, and guarding on it would blank all fifteen flows for that whole window, every
restart. A missing reading gives A = 0, which puts the air con back inside House -- the house total
stays right, nothing is fabricated, and only the split is lost. Note the default covers only text
Jinja cannot parse: `'inf'` and `'1e9'` parse, and it is the `[ AR, H - T ] | min` clamp that contains
them.

Verified live 2026-09-11, against the config entries HA actually stores rather than the YAML: all
fifteen deployed templates reproduce `sankey_tests/flows.py` to **4.8e-05 W** with the air con
non-zero, splitting 39.625 solar / 0.925 battery / 0 grid -- the same source mix as every other sink.

### Card mechanics (established on v1, still true of the card)

**Greedy allocation.** ha-sankey-chart 6.3.0 has no flow solver. It walks source nodes in node order
and links in declaration order, allocating `min(source unspent remainder, sink unfilled remainder)`;
`_calcConnections()` iterates `this.connections` in plain push order, and `sort_by` only repaints
afterwards (`boxes:Ue(c,a.boxes,l,d)`). So **node order is allocation priority** and a starved sink
becomes a floating box -- its sensor state draws the node, but a zero allocation draws no ribbon. v2's
explicit `value:` is what removes this as a live concern; it returns the moment a `value:` is dropped.

The v1 worked example is worth keeping because it shows how badly this misleads.
`battery_discharge -> house` is that source's **only** possible link, because all three discharge
windows are unset. It was declared after `grid_import`, so grid import filled House first and the
battery-out connection resolved to **zero**: Battery out rendered as a stranded box with no ribbon at
all, while House falsely showed grid covering the whole load. Live numbers at the time -- solar 2.7,
grid import 5.4, battery out 1.2, house 4.2, battery in 4.8, export 0.5 -- gave battery out 1.20 of
1.20 stranded and battery in 1.40 of 4.80 unfed. **Node order became solar, battery out, grid
import**, i.e. the most physically constrained source first. **Do not** also move `solar -> house`
ahead of `solar -> battery in`; codex proposed it to force a prettier House split, but it directly
contradicts the chart's purpose of seeing solar reach the battery.

Link order under greedy allocation is likewise constrained by physics: **solar -> export first, then
battery in, then house as the elastic sink.** Solar previously fed battery then house before export,
so house consumed the remainder and export floated despite its 5.1 kWh state. The battery-to-grid
link was removed because all discharge windows are unset -- **merely declaring it fabricated battery
export.** Do not reorder for appearance without checking that no constrained sink starves.

**`remaining_parent_state` is not an arithmetic balance**; it sums greedy upstream leftovers. It once
showed 3.4 kWh "Untracked / losses" when the real residual was 0.1.

**A link spanning more than one section makes the card invent an unlabelled ghost box, coloured like
its target.** This is what made the battery appear to charge itself:

    if(h-d<=1)continue; ... const s=t(l,["id","section","type"]);
    e.push({...s, id:`${u}__passthrough_${i}__auto`, section:i, type:"passthrough"})

It clones the **target's** props including `color`, draws at `fill-opacity:.4`, and suppresses name
and state (`if("passthrough"===t.config.type||!r&&!a)return null`). An unlabelled green box beside a
green Battery-out box, or a matching unlabelled red one when export goes above zero, is the giveaway.
Passthroughs do not change the arithmetic (`r={parent:e,child:o,...,passthroughs:n}`). Fixed
2026-08-28 by collapsing four sections to three, so every link is exactly one hop and the machinery
never fires. **The deploy script aborts if any link spans other than one section; keep that guard.**
It was `sankey_fix.py` when written; that script was lost with its scratchpad and the guard now lives
in `sankey_apply.py`.

**`add_entities`/`subtract_entities` use Recorder `change` values under `energy_date_selection`.**
That was proved by the "Unaccounted" node, **removed 2026-08-27 at the owner's request -- do not
re-add it unasked**; it verified against 16.3 + 14.0 + 4.4 = 34.7 kWh in and 22.3 + 5.6 + 6.7 + 0.1 =
34.7 out. Removing it leaves the three source nodes with a small unspent remainder, which the card
renders as a slightly shorter bar -- not an error, and not a link to invent a target for.

**The old House-split validation figures do not hold up.** They were "19.0 kWh derived versus 18.8
kWh from the inverter on 2026-08-26"; checked against recorded history 2026-08-27,
`house_consumption_today` peaked at **17.7 kWh** that day and never reached 18.8, so the two sides are
1.3 kWh apart rather than 0.2 and the provenance of the 19.0 is unknown. Treat the remainder as
**unvalidated** until re-checked; `house_yesterday` (added 2026-08-27) makes that a one-line
comparison. The two built-in device-detail graph variants were tried and removed -- the owner wants
this breakdown only in the Sankey.

**Tesla energy** is a left-Riemann integral with `max_sub_interval` 60 s over charger voltage x current
gated to 180-280 V; TeslaMate publishes current on change, and one 32 A plateau went 80 minutes
without an update. It reads about 6% above the Tesla app because it measures AC-side house load while
the app measures the pack; roughly 94% onboard-charger efficiency explains the real loss. The idle
voltage reads about 2 V -- a TeslaMate artifact -- so the gate needs both `i > 0` **and**
`180 <= v <= 280`; voltage alone is not enough. The band excludes Superchargers but cannot distinguish
home from another AC charger, which is acceptable because the owner charges only at home; without a
device tracker, "home" rests entirely on that voltage test, which is the known hole. Verified end to
end 2026-08-27: 7.88 kWh reached the pack against 8.384 kWh measured on the AC side -- a 6.0% charger
loss -- over three sessions (00:30-01:00, 02:00-02:30, 05:00-05:16) taking SOC 63 -> 67 -> 72 -> 75%.
Neither Tesla nor AC had statistics before 2026-08-27, so no backfill exists. The LG sensor updates in
hourly lumps; a flat hour is cloud polling cadence, not proof the AC is off.

### What the Sankey (and every statistics card) actually reads

Established 2026-08-27 from ha-sankey-chart 6.3.0's own source. This governs any card driven by the
Energy date selector, not just the Sankey.

With `energy_date_selection: true` the card fetches Recorder long-term statistics and **replaces every
node's live state** with `sum(change)` over the selected range. It requests only the `change` column
and never a row's `state`. The period comes from the range length alone -- `hour` up to two days,
`day` up to 35, `month` beyond. **There is no five-minute period and no `statistics_period` option in
6.3.0**; the `throttle` option is unreachable in this branch.

Three consequences that all look like faults and are not:

- **A node that shows a higher number for a moment on refresh, then drops, is correct twice.** First
  paint falls back to `this.hass.states` because the statistics map is still empty; the lower figure
  is the settled statistics sum.
- **A sensor's first hourly statistics row carries `change = 0`.** Whatever was already on the meter
  when Recorder began tracking it is permanently missing from that day's `sum(change)`. A sensor
  created mid-day is short by its reading at creation -- for that day only.
- **Hour N compiles after N+1:00** (typically about :12), so the card runs up to an hour behind.

Worked example, air conditioning on 2026-08-27: live 389 Wh rendered 0.4 kWh, settled statistics
0 + 68 + 62 = 130 Wh rendered 0.1 kWh. The 259 Wh gap is 132 Wh of baseline lost to the first
`change = 0` row plus 127 Wh sitting in the uncompiled hour. That 127 Wh did exist in the five-minute
table at 16:05 -- the card simply never asks for it. Rounding is `unit_prefix: k` with `round: 1`.

**Do not "fix" the lag by turning off `energy_date_selection`.** It is card-wide: every node would
revert to raw live state and any historical date would show today's numbers.

### The 5-minute patch to ha-sankey-chart, and re-applying it

**The recorder is not the bottleneck and never was.** Verified 2026-08-28: HA already writes 5-minute
short-term statistics and they are current -- queried together at 09:56, grid export gave 23 rows at
`period: 5minute` ending 09:50 against a single `period: hour` row ending 08:00. There is nothing to
change in `recorder:`, and **1-minute is not available at all**: HA core hard-codes short-term
statistics at 5 minutes. The inverter's own 10 s poll is likewise already in the states table.

The loss is entirely in the card, which picks its period from range length alone. Patch the one arrow
function (`bi`, called as `bi(start, end)` from both fetch paths) to:

    const bi=(t,e)=>{const i=Se(e||new Date,t);return i>35?"month":i>2?"day":Se(new Date,e||new Date)<=7?"5minute":"hour"}

`Se(t,e)` is date-fns signed day difference `t - e`, so inside `bi` the value `i` is the range length
and `Se(new Date, e||new Date)` is the **age of the selection**.

**The `<=7` guard is mandatory, not caution.** 5-minute statistics are purged along with
`purge_keep_days` (default 10), so an unconditional swap to `5minute` would make any older date render
**empty** -- a far worse failure than the lag it fixes. Seven days leaves margin.

File: `/config/www/community/ha-sankey-chart/ha-sankey-chart.js`. Backup at `ha-sankey-chart.js.pre5min`.

**HACS overwrites this file on every card update and the patch is silently lost.** Nothing errors; the
chart just quietly goes back to being up to an hour behind. So:

- After any HACS update of ha-sankey-chart, **re-apply the patch**. Treat a card update and a repatch
  as one operation.
- Detect the state with `grep -c 5minute ha-sankey-chart.js`: **1 means patched, 0 means reverted**.
- **Apply by pattern match, never by line number or identifier.** `bi`, `Se` and `xi` are minified
  names that change on every upstream rebuild. Re-locate the arrow function by its
  `i>35?"month":i>2?"day":` body, and re-confirm `Se`'s argument order before trusting the guard -- an
  inverted guard silently breaks historical dates rather than erroring.
- Verify by selecting today (should track within ~5 min) and a date older than a week (must still
  render, via the `hour` fallback).

Status 2026-09-07: **applied**, minified names still `bi`/`Se`. Verified live 2026-09-08 by driving the
card's own data: 2026-09-05 and 2026-09-03 (inside the guard, so `5minute`) render fully at 50.1 and
37.9 kWh, and **2026-08-31, eight days back and therefore on the `hour` fallback, still renders at
48.2 kWh** -- which is the failure the guard exists to prevent.

Three traps that will fool the next person:

- **The patched file is served, but browsers keep the old one.** The script URL carries
  `?hacstag=<id>` and that tag does not change when the file is edited by hand. Check the server, not
  the page:
  `curl -s http://homeassistant.local:8123/hacsfiles/ha-sankey-chart/ha-sankey-chart.js | grep -c 5minute`.
  A `fetch()` from inside the HA page returned the *unpatched* text even with `cache: "no-store"`;
  only a hard reload (cmd+shift+R) picked up the new bundle.
- **Do not read the chart within a few seconds of a reload.** The documented first paint from
  `hass.states` is very convincing: at 00:05, seconds after a hard reload, the chart showed "Grid
  import 0.2 kWh -> House 0.1" and looked exactly like proof the 5-minute period had taken effect. It
  was the live daily counters freshly reset at midnight; the settled statistics arrived moments later
  showing that day's real 23.3 kWh of solar.
- **A date before 2026-08-30 17:00 renders empty and always will**, because the `sensor.flow_*_daily`
  meters did not exist yet, so every link value is absent. That is not a patch regression --
  2026-08-25 was misread that way once.

**How to drive the date selection without clicking:** the energy collection is on the connection under
a key named for the dashboard -- `hass.connection["_energy_energy-live"]` -- exposing
`setPeriod(start, end)` and `refresh()`. Combined with reading `SANKEY-CHART-BASE.__connections` this
checks any date in a couple of seconds.

