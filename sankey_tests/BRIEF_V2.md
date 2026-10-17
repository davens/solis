# Sankey v2 — Tesla split, Inverter loss node, measured attribution

Owner asks, verbatim:
  "split the tesla off from the house. and the tesla ribbon should know how much of
   grid/battery it took. i want the battery ribbon to end at the third row, like house
   even tho it would naturally end at 2nd row like things that are non house end points.
   write tests"
  "i think if we have inverter parasitic costs, we can have that as a cost node
   'inverter'. what i dont want to show is unaccounted"

Owner also said: "please go with your thoughts rather than mine on the decisions".
So the decisions below are MINE (main). They are settled. Do not re-litigate them;
DO report evidence that one of them is measurably wrong.

## 1. The graph

Section 0 (sources)   Solar, Battery out, Grid import
Section 1 (sinks)     House, Tesla, Air con, Fridge, Battery in, Grid export, Inverter

  solar   -> grid_export | battery_in | house | tesla | aircon | fridge | inverter
  bat_out -> house | tesla | aircon | fridge | inverter
  grid    -> tesla | house | aircon | fridge | battery_in | inverter

18 links, all source->sink. Every sink is a terminus, so the chart is two
columns and every sink box sits at the same x.

Air con was added on 2026-09-11 at the owner's request, superseding section 3
below: "fix the sankey, so that air con comes out as a node endpoint separate
from 'house'", and "house stays named house, but is essentially rest of house".
It is a peer of Tesla, not a child of House. Colour is hot pink #ff69b4 from
CLAUDE.md's table -- never a yellow or amber, because Solar's --warning-color
resolves to rgb(255,166,0) on this theme and the two blur together.
EVERY link spans exactly one section -- keep sankey_fix.py's abort guard.
No battery -> grid_export link, ever (all three discharge windows are unset).

Node identity:
  Solar / Battery out / Battery in    inverter DC daily counters (unchanged)
  Grid import / Grid export           smart-meter AC daily counters (unchanged)
  House                               SUM OF ITS THREE INBOUND FLOW METERS (not
                                      house_consumption_today, and now excludes Tesla)
  Tesla                               sum of its three inbound flow meters
  Inverter                            sum of its three inbound flow meters

## 2. Attribution rule: PROPORTIONAL, with export solar-only

Replaces v1's sequential greedy decompose entirely. Per 10 s sample:

  S  = solar_power            (>=0, DC)
  B  = max(0, -battery_power) (DC delivered)
  C  = max(0,  battery_power) (DC stored)
  G  = max(0,  grid_power)    (AC import)
  E  = max(0, -grid_power)    (AC export)
  H  = max(0, house_load)
  T  = min(max(0, tesla_home_charging_power), H)
  F  = min(max(0, fridge_power), H - T)
  A  = max(0, eps)
  Hr = H - T - F
  L  = max(0, (S + B + G) - (Hr + T + A + F + C + E))     # the Inverter node

  step 1  export is structurally solar-only:   s2e = min(S, E);  S1 = S - s2e
  step 2  every remaining sink draws the SAME source mix:
              w = S1/(S1+B+G), b = B/(S1+B+G), g = G/(S1+B+G)
          sink_from_source = sink * share, for sink in (Hr, T, A, F, C, L)

Why proportional: electrons do not queue. It is the only order-free, symmetric rule.

NARROWED 2026-08-30 after v2-suite measured it. The original claim here was "unlike v1 it
can never fabricate a flow". That is too strong and it is wrong. What actually survives is
only this: a source reading EXACTLY ZERO contributes zero to every sink. A nonzero source
CAN be over-spent without bound when the L >= 0 clamp binds and measured sinks exceed
measured sources -- decompose(1000, 0, 0, 3000, 0) spends 3000 W of a 1000 W solar reading.
Measured cost on real history: the clamp binds 1.4-5.3% of samples and fabricates at most
0.097 kWh/day, under half a counter word. We do NOT correct this: a correction would
reintroduce exactly the fitted constants section 8 exists to remove. It is pinned by a test
against the measured per-day figures instead, and must fail if it exceeds ~0.15 kWh/day. The structural masks that would break it never bind in practice:
battery cannot charge and discharge in the same sample, so B>0 => C=0.

This dissolves the g2h-clamp question entirely: decompose(0,0,2,0) now sends 2 kW to
Inverter, not into a house drawing nothing. t-regimes measured the v1 clamp at up to
-8.3% on import; it is not shipping and is not needed.

The Inverter node is the residual made EXPLICIT and NAMED. The owner is clear: a named
loss node is wanted, an "Unaccounted" node is not. The historical ~2.5 kWh/day residual included unmeasured EPS; it is not a
post-change loss target. Compare cautiously against flow-law's fitted parasitic (AC = 0.9974*DC - 105.9 W, ~106 W fixed).
Report the number; do not tune anything to hit it.

## 3. Section 2

WITHDRAWN 2026-08-30 at the owner's request, in two steps on the same day.

First "Stored", a remaining_parent_state child of Battery in added so the battery
ribbon reached the same column as House's children: on the rendered chart it only
ever restated Battery in's own number one column to the right.

Then House's own Air con / Rest of house split, which was the whole of what was
left in section 2. Dropping it makes House a terminus, which is what lets Tesla sit
level with every other sink -- the alternative, feeding a section-2 Tesla from a
section-1 House, requires the car back inside the house total and was rejected.

There is no section 2, and there still is not.

SUPERSEDED IN PART, 2026-09-11. Air con is back, but NOT as the thing that was
withdrawn. What was withdrawn was a section-2 child of House, which cost House
its terminus status. What exists now is a section-1 terminus fed directly by the
three sources, exactly like Tesla -- so the chart is still two columns and every
sink still sits at the same x. "Stored" stays withdrawn and must not return.

## 4. Per-link values

Each of the 18 source->sink links carries `value: <utility_meter entity>`.
In the flat `links:` config the key is literally `value` and holds an entity id
(`connection_entity_id` is the internal name only). Card rule becomes
`min(parent_remainder, child_remainder, value)`, so ordering sensitivity collapses.

Plumbing per flow: template power sensor (W) -> integration/Riemann (kWh, left,
max_sub_interval 60s) -> utility_meter (daily cycle). 18 x 3 = 54 helpers.
Owner green-lit the compute: "the ha green has spare compute".

## 5. Existing entities

  sensor.solis_inverter_solar_power          W, DC, >=0
  sensor.solis_inverter_battery_power        W, DC, + charging / - discharging
  sensor.solis_inverter_grid_power           W, AC, + importing / - exporting
  sensor.solis_inverter_house_load           W, AC, includes Tesla and fridge; excludes EPS
  sensor.tesla_home_charging_power           W, AC (template, V*I gated 180-280 V)
  sensor.house_load_excluding_car            W, AC (template, already exists)
  sensor.solis_inverter_backup_load          W, AC, separate EPS output (33148)
  sensor.fridge_power                        W, AC (ZHA smart plug, ~10 s cadence)

Tesla power is a TeslaMate on-change value: it can hold flat for up to ~80 min at a
plateau. Held values are correct, not stale. But clamp T <= house_load always.

## 6. Non-negotiable

- No write path to the inverter. Everything here is read-only HA.
- Never write registers 43038-43049 or 43090-43097.
- Discharge windows stay unset; never draw or propose battery -> grid.
- Do not re-add an "Unaccounted" node.
- Code -> Test -> Review order.

## 7. Test coverage is a hard gate, not a nicety

Owner: "remember everything should be covered by tests".

Nothing ships uncovered. Concretely, there must be at least one test that fails if you
break each of:

- every one of the 18 links: its value, its source, its target, its section span
- every node: its identity/state source, its section, its colour, its type
- every branch in the decomposition (each max(), each clamp, each structural zero,
  each division guard)
- the proportional shares summing to 1, and to 0 when total supply is 0
- Tesla: T > house_load clamp; T held flat across a long plateau; T unavailable/unknown
- the Inverter node: L >= 0 clamp; L when outflow exceeds inflow; L over a full replay day
- Battery in: fills exactly from its two sources; every sink is a terminus
- every Jinja template matching its Python counterpart to < 1e-4 W on replayed samples
- link-order and node-order permutations producing an identical allocation
- ghost/passthrough synthesis never firing on the shipped layout
- bad input: NaN, inf, None, "unavailable", "unknown", negative-where-impossible,
  strings, and **bool**. bool is the one this list originally missed: float(True) == 1.0
  and isinstance(True, int) is True, so a bool sails through any `try: float(x)` guard and
  becomes a silent 1 W. float("nan") parses for the same reason, and NaN then meets
  max(0.0, nan) which returns 0.0, because nan > 0.0 is False. Both defeat parse-based
  guards by the same mechanism, so guard BEFORE float(), not after. (Found by t-edges.)

Run `--cov` on decompose_v2.py, allocator.py and the layout builder and report the
number. Anything under 100% statement coverage on decompose_v2.py needs a written
reason. Report branch coverage too. Do not add tests that only assert what the code
says; each test must be able to fail.

## 8. v2 uses NO fitted efficiency constants — and that is the point

v1 needed ETA_FLOOR, LOW_BAND_ETA=0.22, 0.9974 and -105.9 W to refer DC to AC, and
t-regimes was left with a 3.0% gap it could not close honestly. v2 needs none of them:
the Inverter node IS the measured residual (S+B+G) - (Hr+T+C+E), so every node fills
exactly by construction and nothing is fitted.

The cost is that the shared loss is charged to sources proportionally rather than per
source. A battery discharging at 40 W really does lose ~78% of it, and proportional
undercharges the battery for that. Keep a named seam (`LOSS_RULES`) with the per-source
band-table alternative implemented, MEASURE both over the four replay days, and report.
Default stays aggregate-proportional unless the measurement says otherwise.

v2 SUPERSEDES v1. New module is `flows.py`. `allocator.py` is unchanged and reused.
`decompose.py` and `test_decompose_regimes.py` are retired once flows.py is green —
they are deleted, not left rotting.


## 9. Sign tests must use three distinct non-zero sources

Found by t-edges in v1 and it bites HARDER under v2, so it is a rule, not advice.

Proportional allocation gives a zero-power source zero contribution to every sink. So any
polarity or sign-inversion test with a zero source -- or with two sources of equal value --
is STRUCTURALLY INCAPABLE of catching an inversion: both polarities produce the same
all-zero or symmetric answer and the test passes green while the sign is backwards.
t-edges' v1 detectors detected nothing for exactly this reason (solar == house made both
grid polarities give all-zero grid flows).

CLAUDE.md singles out sign inversion as the failure an agent "helpfully" introduces, and
this repo already deliberately inverts two conventions at the HA boundary. So do not leave
it to each author to remember: provide a shared fixture helper that REFUSES degenerate
inputs, and route every sign test through it.

## 10. Open physics question -- unsourced export. MEASURE IT.

Export is solar-only, so `s2e = min(S, E)`. If solar reads exactly 0 while the meter reports
exporting, the Export node is drawn with no inbound ribbon -- a stranded box, which CLAUDE.md
warns looks identical to a link that was never declared.

Do NOT fix this by inventing a source. battery -> export is forbidden outright (all three
discharge windows are unset) and grid -> export is impossible with a single signed meter, so
an export with no solar behind it is a MEASUREMENT inconsistency, and the honest rendering
says so.

But quantify it before accepting it. v2-physics: report, per replay day, the kWh of metered
export that ends up unsourced, and how many samples produce it. Under ~0.05 kWh/day it is
dawn/dusk rounding and we leave it visible. Materially more than that is a finding about the
measurement chain and I want to see it, not have it papered over.


## 11. Replay days: 2026-08-27 is NOT usable for Tesla. Do not assume zero.

The Tesla and house-excluding-car sensors were created mid-afternoon on 2026-08-27, so
that day has one Tesla sample at +48479s and nothing before it. CLAUDE.md confirms
neither Tesla nor AC had statistics before 2026-08-27 and that no backfill exists.

RULING: exclude 2026-08-27 from every Tesla-dependent measurement and SAY SO in the
output. Do not paper it over with a "tesla = 0 before the first sample" assumption.

That assumption is not merely unsupported, it is REFUTED. CLAUDE.md's Tesla paragraph
records the chain being verified end to end on 2026-08-27: 8.384 kWh AC-side over three
sessions at 00:30-01:00, 02:00-02:30 and 05:00-05:16, taking SOC 63 -> 75%. All of that
sits inside the blind window, and a replay that assumes zero reports 0.00 kWh for the day.

GENERAL RULE, because this will recur on every day that straddles a sensor's creation:
a sensor reading 0 and a sensor not existing are indistinguishable downstream. Carry
unavailable as unavailable; never substitute 0. The replay harness must REFUSE to
integrate across a gap rather than silently treating it as zero. Same bug class as the
Jinja `| float(0)` trap.
2026-08-27 remains fully usable for everything that does not involve Tesla.

Do NOT judge the other days by sample count. TeslaMate publishes on change, so a low
count can be COMPLETE rather than sparse — CLAUDE.md records a 32 A plateau going 80
minutes without an update. Five samples on 2026-08-28 may be exactly right. Settle it by
measurement, not by eye: integrate tesla power over each day with a left/step rule and
compare against that day's sensor.tesla_home_charging_energy total. Agreement means the
samples are sufficient. Report the comparison per day.

## 12. Ownership of shared files

  flows.py, test_flows.py                      v2-physics
  layout_v2.py, test_layout_v2.py              v2-layout  (must export NODES and LINKS
                                               as plain data for others to import)
  flow_sensors_v2.yaml, test_jinja_parity.py   v2-helpers
  test_flows_invariants.py, test_replay_v2.py  v2-invariants
  test_edge_cases.py, test_history_replay.py,
    test_chart_integration.py, fixtures/replay.py   v2-suite
  allocator.py                                 t-allocator (frozen; report, do not edit)

fixtures/replay.py is v2-suite's. It needs a fifth channel and a decompose(..., tesla)
call site. v2-suite: make that change first and announce the API to v2-physics and
v2-invariants. Everyone else consumes it and does not edit it.

No test may re-declare the layout. Import NODES/LINKS from layout_v2.py, so the chart
tests cannot drift from what actually ships.

## 13. Measured EPS outside House (2026-10-17)

Owner-stated: EPS supplies the air con plus a few-watt charger; other loads are
rare. The node remains Air con and all `*_to_aircon` keys/helper ids stay stable.
Owner-supplied verified facts: House register 33147 excludes EPS; backup register
33148 supplies `sensor.solis_inverter_backup_load`, non-negative AC W, measurement
state class, about 10 s polling in the same block and availability as House.
The supplied live balance gives 421 W residual after subtracting 383 W EPS,
versus 804 W before, with about 88% charging efficiency. An hourly regression
since 2026-10-11 found a 1.00 W residual increase per LG-derived air-con watt
(95% CI 0.90–1.11), without a House increase. These observations were not
repeated by the offline tests; treating all EPS as air con is the owner's
practical circuit attribution.

Allocate Tesla and fridge inside H; A = max(0, eps) is additional load, with no
House cap. Hr + T + F = H, total load H + A. Inverter excludes EPS. The six
strictly read guarded inputs are solar, battery, grid, House, Tesla and EPS.
Only fridge uses float(0), leaving it inside House when unavailable.

EPS moves Air con and Inverter flows, not the House split. Tesla moves nine
flows (House, Tesla, Fridge); fridge moves six (House, Fridge). Source-spent totals
also depend on EPS because the residual can bind at zero.

Historical fixtures contain no EPS or fridge. Hold both at zero, count the held
time as `eps_assumed_s` / `fridge_assumed_s`, and refuse affected measurements.
`house_and_fridge` is knowable excluding the car (Hr + F = H - T), while
Hr + T + F = H survives even a blind Tesla. Air con, Inverter and source-spent
totals require EPS and must be refused. Export and battery-charge totals survive.
Synthetic profiles include EPS and balance supply against H + A.

## 14. Running the tests: the cwd decides what you see

The canonical invocation is from the repository root:

    uv run --no-project --with pytest --with jinja2 --with pyyaml --with hypothesis \
        python -m pytest sankey_tests/ -q

All four extras are required: pytest alone fails at COLLECTION with
ModuleNotFoundError on jinja2, yaml and hypothesis, which looks like a broken
tree and is not.

**There are two hypothesis example databases -- `./.hypothesis` and
`./sankey_tests/.hypothesis` -- and which one is in scope depends on the
invoking directory. This makes at least one test's result cwd-dependent.**
Established 2026-09-11 the hard way: `test_invariants.py::test_sinks_filled_
under_every_treatment[residual_on_solar]` passes from the repository root and
fails deterministically from inside `sankey_tests/`, because the stored
falsifying example lives in the second database. It was reported as flaky,
then as seed-dependent, and it is neither -- it is a real, reproducible
counterexample that one of the two databases simply does not know about.

So: **a green run from one directory is not evidence of a green run from the
other.** When a hypothesis failure will not reproduce, check the cwd before
concluding anything about seeds.

The counterexample itself is genuine and PRE-DATES the air-con work --
`test_invariants.py`, `decompose.py`, `contract.py` and `conftest.py` are all
untouched by it, and the file imports nothing else. It is v1: for
`(solar, battery, grid, house) = (1.0, -2.0, -1.0, 2.0)`, v1's DEFAULT
`residual_on_solar` treatment gives the house 0.551 against a deliverable of
2.0, because export consumes the whole of solar (`solar_to_export = 1.0`,
`solar_to_house = 0.111`) and the battery contributes only 0.44. The test's own
xfail message already names these exact arguments as the sample that breaks
`derived_solar_ac`; hypothesis found they break the default too.

**Not fixed, deliberately.** `decompose.py` is the v1 allocator and the live
chart has not used it since v2 shipped on 2026-08-30 -- v2 runs on `flows.py`.
Whether to fix v1, delete it, or leave it as a documented comparison is the
owner's call, not a thing to settle inside an unrelated change.

## 15. Fridge terminus, 2026-10-11

Fridge is a section-1 peer of Tesla and Air con, colour #14b8a6 teal. It reads
`sensor.fridge_power`, real W from a ZHA smart plug with state_class measurement
at roughly 10 s cadence; it is a direct power reading.

Carve Tesla first, then Fridge: F = min(max(0, fridge), H - T), Hr = H - T - F.
EPS is separate as described in section 13. Missing fridge means F = 0 and
leaves it inside House. The graph remains ten nodes, eighteen links and 54 helpers.
Synthetic DAY and BALANCED_PROFILE have nonzero fridge and EPS columns.
Link-order tests exhaust each source's local order (7! + 5! + 6! = 5,880),
exhaust all 30,240 node orders, and sample joint link and node orders; the full
link-order product is 435,456,000 and is not run.
