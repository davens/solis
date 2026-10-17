# The Sankey chart: layout, measured flows, card mechanics and the 5-minute patch

*Split out of `CLAUDE.md` on 2026-09-13 to keep the always-loaded file small. This is the same text, unchanged. The rules and traps that apply even when you are NOT reading this file stayed in `CLAUDE.md`.*

### Sankey node colours (2026-08-30)

Colours carry meaning here and were chosen deliberately:

| Node | Colour | Why |
|---|---|---|
| Grid import | `var(--error-color)` red | The most expensive flow on the chart. It previously shared `--info-color` with House, so the thing costing the most money read as house-coloured. |
| Grid export | `#a78bfa` violet | It earns money; red was actively misleading. The violet matches the Power card's grid trace. |
| Tesla | `#ffffff` white | The owner's car is white. It is not an accent from the palette, so do not "correct" it to one. |
| House | `var(--primary-color)` | It excludes the car and fridge from register 33147; EPS was never inside that register and is shown separately, but keeps the name "House" -- the owner's words were "house stays named house, but is essentially rest of house". |
| Fridge | `#14b8a6` teal | Measured directly by a ZHA smart plug; distinct from Solar, Air con and Tesla. |
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

The graph is **two columns, 10 nodes, 18 links**: Solar / Battery out / Grid import feed House, Tesla,
Air con, Fridge, Battery in, Grid export and Inverter, and every sink is a terminus. Each link carries
`value: sensor.flow_<source>_to_<sink>_daily`, which the card applies as
`min(parent_remainder, child_remainder, value)`, so declaration order no longer decides the picture.
House, Tesla, Air con, Fridge and Inverter have no counter of their own: each is `entity_id` plus `add_entities` over
its three inbound meters. Inverter is the non-negative residual after House and EPS, including conversion loss, parasitic draw and sensor skew, grey
`#6b7280`.

**Allocation updated 2026-10-17.** Tesla and Fridge are carved out of House;
EPS is a separate AC output feeding the Air con node. House keeps its name.

    T  = min(max(0, tesla), H)
    F  = min(max(0, fridge), H - T)
    Hr = H - T - F
    A  = max(0, eps)
    L  = max(0, (S + B + G) - (Hr + T + F + A + C + E))

Total load is `H + A`. Source attribution remains proportional after solar-only
export. Air con is independent of the House split; its existing `*_to_aircon`
keys and all nine helper entity ids remain stable for statistics continuity.
Tesla affects nine flows; fridge affects six; EPS affects the three Air con and
three Inverter flows. The common availability guard requires six entities:
solar, battery, grid, House, Tesla and EPS. Only fridge uses `float(0)`; a missing
fridge reading leaves that load inside House.

Historical captures contain neither EPS nor fridge. Replay holds both at zero
for arithmetic and marks them blind. House + Fridge is the knowable total
excluding the car (`Hr + F = H - T`), and House + Tesla + Fridge equals H.
Air con, Inverter residual and source-spent totals are refused without EPS;
House and Fridge individually are refused without fridge. Tesla blindness also
refuses House + Fridge. Export and battery-charge totals remain knowable.

**Historical power-path validation, 2026-08-31 (before EPS separation).** On a pure
grid-charging night it read 0.580 kWh two hours in; integrating the raw 5-minute power statistics
over the same window gives a residual of 5.443 - 4.348 - 0.503 = 0.592 kWh, 2% away. The check that
matters is that the power path reproduces the independently measured 0.887 charge efficiency
(4.348 / (5.443 - 0.503) = 88.0%). **Do not "validate" this node against `house_consumption_today`.**
That counter is a derived residual; it includes conversion loss and, by inference
from that definition, EPS draw too. The comparison makes
the node look like ~0.5 kWh of double-counted house load when it is not -- see "What the daily
counters do and do not prove". Two independent auditors both stalled on exactly that trap. Historical captures lack
EPS, so their H-only residual cannot independently establish conversion loss alone.

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

### Air con uses the measured EPS register (2026-10-17)

**Owner-stated:** the air con and a few-watt hand-vacuum charger are on the
inverter's EPS (backup) port; other loads there are exceptionally rare. Treating
EPS as Air con is the owner's practical identification of that circuit.

**History is deliberately not corrected (owner's decision, 2026-10-17).** Deployed live at about
09:35 on 2026-10-17. Earlier days keep the old split: Air con carved out of House (so House reads
low by the air-con draw) and EPS hidden inside the Inverter residual. Totals are unaffected. No EPS
history exists to rebuild from, and the old LG-derived `sensor.aircon_power` -- now deleted -- read
76 W against a measured 383 W EPS, so it cannot be used as a stand-in.

**Verified facts supplied by the owner, 2026-10-17:** EPS is outside House load
(register 33147). Register 33148 is exposed as
`sensor.solis_inverter_backup_load`: non-negative AC watts, `state_class:
measurement`, polled about every 10 s in the same Modbus block as House, with
the same availability. EPS is a guarded input read without a float default.

The supplied live check was grid 11265 W, battery charging 2733 W, House 7728 W,
EPS 383 W and solar 0 W: subtracting EPS reduces the residual from 804 to 421 W.
The implied charge efficiency is about 88%, consistent with the 2026-08-31
validated 0.887. The supplied hourly regression since 2026-10-11 found each
LG-derived air-con watt raised the old residual by 1.00 W (95% CI 0.90–1.11),
without raising House. This supports the separate-output interpretation; the
regression alone does not identify every EPS appliance.

These live observations were not repeated during the offline code change.
The LG-derived channel is no longer an input to the Sankey.

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

    if(h-d<=1){continue;} ... const s=t(l,["id","section","type"]);
    e.push({...s, id:`${u}__passthrough_${i}__auto`, section:i, type:"passthrough"})

It clones the **target's** props including `color`, draws at `fill-opacity:.4`, and suppresses name
and state (`if("passthrough"===t.config.type||!r&&!a){return null;}`). An unlabelled green box beside a
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
Tesla statistics begin on 2026-08-27; earlier Tesla use cannot be backfilled here.

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

Status 2026-09-07: **applied**, minified names still `bi`/`Se`. **Found reverted 2026-10-11** (a HACS update on 2026-09-26 had overwritten it, so the chart lagged for two weeks); re-applied with the names now `Ei`/`Ae`, `Ae` re-confirmed as signed `differenceInDays(t, e)`. **Regenerate `ha-sankey-chart.js.gz` after patching** (`gzip -9 -k -f ha-sankey-chart.js`): the server sends the `.gz` to any browser that accepts gzip, so patching only the `.js` changes nothing a browser sees. Verified live 2026-09-08 by driving the
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

