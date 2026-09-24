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
Section 1 (sinks)     House, Tesla, Air con, Battery in, Grid export, Inverter

  solar   -> grid_export | battery_in | house | tesla | aircon | inverter
  bat_out -> house | tesla | aircon | inverter
  grid    -> tesla | house | aircon | battery_in | inverter

15 links, all source->sink. Every sink is a terminus, so the chart is two
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
  T  = tesla_home_charging_power
  Hr = max(0, house_load - T)
  L  = max(0, (S + B + G) - (Hr + T + C + E))     # the Inverter node

  step 1  export is structurally solar-only:   s2e = min(S, E);  S1 = S - s2e
  step 2  every remaining sink draws the SAME source mix:
              w = S1/(S1+B+G), b = B/(S1+B+G), g = G/(S1+B+G)
          sink_from_source = sink * share, for sink in (Hr, T, C, L)

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
loss node is wanted, an "Unaccounted" node is not. Expect ~2.5 kWh/day -- cross-check
against flow-law's fitted parasitic (AC = 0.9974*DC - 105.9 W, ~106 W fixed).
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

Each of the 12 source->sink links carries `value: <utility_meter entity>`.
In the flat `links:` config the key is literally `value` and holds an entity id
(`connection_entity_id` is the internal name only). Card rule becomes
`min(parent_remainder, child_remainder, value)`, so ordering sensitivity collapses.

Plumbing per flow: template power sensor (W) -> integration/Riemann (kWh, left,
max_sub_interval 60s) -> utility_meter (daily cycle). 12 x 3 = 36 helpers.
Owner green-lit the compute: "the ha green has spare compute".

## 5. Existing entities

  sensor.solis_inverter_solar_power          W, DC, >=0
  sensor.solis_inverter_battery_power        W, DC, + charging / - discharging
  sensor.solis_inverter_grid_power           W, AC, + importing / - exporting
  sensor.solis_inverter_house_load           W, AC, includes Tesla AND air con
  sensor.tesla_home_charging_power           W, AC (template, V*I gated 180-280 V)
  sensor.house_load_excluding_car            W, AC (template, already exists)
  sensor.aircon_power                        W, AC (derivative -- see section 13)

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

- every one of the 12 links: its value, its source, its target, its section span
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

## 13. Air con is an HOURLY channel, and that is the whole of its cost

Every other channel is a real 10 s power reading. The air con is not, and cannot
be: `lg_thinq` publishes **no power entity at all**. Checked in the entity
registry on 2026-09-11 -- twelve air-con entities, not one of them power, and not
even a disabled-by-default one to enable. What it does publish is
`sensor.living_room_air_conditioner_energy_today`, in Wh.

So `sensor.aircon_power` is a `derivative` helper over that counter with
`unit_time: h`, which is watts. Measured cadence over four days, 2026-09-11:

    energy_today        95 changes / 4 days, median gap 3600 s, min 661 s
    energy_this_month    8 changes / 4 days, median gap 22594 s

`energy_today` is the source **because the monthly counter only updates once a
day**, which would have been far worse. Three consequences, all accepted:

* **The air-con split is hourly and lags by up to an hour.** The derivative holds
  one value for a whole hour, and that value describes the hour just *past*. So
  an hour of air con is attributed to the source mix of the *following* hour.
  Around sunrise and sunset that is the wrong mix. Daily totals are unaffected.
* **One hour a day is not attributed at all.** `energy_today` resets at about
  00:36 (the cloud poll, not midnight), so the 23:36-00:36 delta reads negative
  and is clamped to zero. That hour's air-con energy stays inside House.
  Overnight the unit is on standby -- the measured step is +10 Wh/h -- so the
  loss is small, bounded and in the safe direction.
* **`max_sub_interval` is 2 h.** The counter genuinely steps hourly so this never
  fires in normal operation, but a counter frozen by a cloud outage then decays
  toward zero instead of holding a false wattage for ever.

The channel is read with `| float(0)` and is deliberately ABSENT from the
availability guard, which still counts exactly five entities. Both are the
opposite of the rule the rest of the plumbing follows, and both are
load-bearing: the derivative reads `unknown` for up to two hours after every
restart, and guarding on it would blank all fifteen flows for that whole window,
every restart. A = 0 puts the air con back inside Hr, which is exactly the
behaviour the chart had before this node existed -- the house total stays right,
nothing is fabricated, and only the Air con / House split is lost. Zero here is
the well-defined fallback of "we cannot separate it", not a silent guess.

The Tesla is carved out BEFORE the air con:

    T  = min(max(0, tesla), H)
    A  = min(max(0, aircon), H - T)
    Hr = H - T - A

That ordering is deliberate. The car is the larger and far better-measured load,
and it keeps the Tesla flows genuinely blind to the air-con channel, which is
what lets `DEPENDS_ON` keep `_ALL` for them and use `_ALLA` only for the house
and air-con flows. `L` is numerically UNCHANGED by the whole addition, because
`Hr + T + A == H` just as `Hr + T == H` did before.

**The blindness runs one way only.** `A = min(aircon, H - T)` READS T, so the
three air-con flows depend on the Tesla channel even though the Tesla flows do
not depend on the air-con one. The count of Tesla-dependent flows therefore went
from six of twelve to **nine of fifteen**, and it moved in the unsafe direction:
leaving it at six would publish three flows computed from a dead input. This is
why the air-con flows carry `_ALLA` (which contains `tesla`) and is pinned by
`test_exactly_nine_flows_declare_a_dependency_on_the_tesla_channel`.

**"L is unchanged" is exact in mathematics and ulp-accurate in floating point,
and that is deliberate.** `inverter_loss()` builds its `drawn` term from the
three parts, `(H - T - A) + T + A + C + E`, so moving `aircon` by 250 W shifts
`grid_to_inverter` by about **9.1e-13 W** -- a sum-order artifact, not a real
dependency. It could be made bit-exact by summing `r["house"] + C + E` instead,
and that was considered and **not done**: the deployed Jinja writes the same
three-part sum, and keeping the reference model and the live template expressing
the *identical* expression is worth more than an ulp. The empirical dependency
check uses a 1e-9 threshold, which is the right instrument for exactly this
reason, and the three inverter flows are asserted to floating-point slack rather
than with `==`. Do not "tidy" one side of this without the other.

### Air con is BLIND on the replay fixtures, and House+Air con is the knowable half

The four recorded days carry no air-con series. `fixtures/replay.py` therefore
holds the channel at 0 W and declares it **blind**: `blind_channels(doc)` returns
`aircon` on every fixture day (plus `tesla` on 2026-08-27), and `unknowable(doc)`
turns that into the refused `derived()` keys and refused `FLOWS` keys by
delegating to `flows.unreportable()` -- there is no hand-written list on the
replay side, so the two cannot drift.

A held zero is an assumption, not a reading, and the harness says so: it reports
`aircon_assumed_s` alongside `tesla_assumed_s` (on all four days it equals the
whole window), and `__main__` prints `n/a (blind: ...)` for every refused row
rather than a number. **House and Air con individually are refused.** This is the
same rule, and the same mechanism, as the Tesla on 2026-08-27 (section 11).

**What makes this cost nothing is that the pair is knowable even when neither
half is.** `Hr + A == house_load - T` identically, for any A whatsoever, because
A is carved out of exactly what the Tesla left. So `derived()` gained
`house_and_aircon`, and it is that quantity -- not House -- which now faces
`house_consumption_today` minus the car. Every reconciliation the replay had
before survives at its existing bound; only the claim that it was about *House*
is withdrawn.

An earlier draft of this section said to pin A at 0 and keep reporting House,
on the reasoning that blinding would retire those reconciliations. **That was
wrong and is recorded here so it is not re-proposed:** it misses the pair
identity above, and it reports a House figure derived from an assumed input as
though it were measured. The blind treatment is strictly better.

Two pins keep it honest, and both would fail loudly if someone re-introduced the
zero-fill:

* `test_the_aircon_node_is_a_held_zero_and_is_never_reported_as_a_measurement` --
  the node is exactly 0.000, the held time equals the window, and the quantity is
  refused.
* `test_seven_node_totals_are_provably_blind_to_the_aircon_reading` -- replay
  each day twice, once at 0 W and once at `house_load - T`, and require the six
  source/export/battery-in/inverter totals **plus Tesla plus House+Air con** to
  be bit-stable to 1e-9 across a whole day of real data, while House itself must
  move by more than 0.5 kWh and the energy leaving House must equal the energy
  arriving at Air con.

The air con on those days was real and its hourly statistics still exist --
575 / 549 / 238 / 327 Wh for 2026-08-27 to 30, against house days of roughly
13-46 kWh. **Backfilling them into the fixtures is available and was not done.**
The LG counter is flat within an hour, so it resamples to the capture's 5-minute
grid as a piecewise-constant wattage without inventing anything, and that would
turn the replay into a genuine six-channel measurement. Deferred, not rejected.

The two SYNTHETIC fixtures are a different matter and DID gain an air-con column
-- `layout_v2`'s `DAY` and `chart_integration`'s `BALANCED_PROFILE`, non-zero on
half their samples and never exceeding `house - tesla`. Without it the three new
ribbons would have been exercised only at zero, which is coverage that looks
complete and proves nothing. No recorded fixture was touched.

### Greedy became node-order SENSITIVE, and that is a fact about the card

Before the air con, `test_greedy_is_node_order_insensitive_HERE_and_still_wrong`
recorded that on a balanced day the greedy v1 allocator gave the same picture
whatever order the sources were walked in. **That is no longer true, and the
change is not cosmetic.** With a third AC sink, House and Air con compete for the
same supply: whichever source is walked first fills House, and a later one is
left holding Air con. Measured on the balanced profile, the six source orderings
produce **five distinct pictures** -- battery-out's 4.8 kWh goes entirely to
House under one ordering and entirely to Air con under another, from identical
energy.

The test is inverted and strengthened rather than deleted: greedy is now asserted
wrong in *every* ordering, and the same sweep with per-link `value:` entities is
asserted to collapse to exactly one picture. That contrast is the whole argument
for v2, and adding a sink made it sharper.

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
