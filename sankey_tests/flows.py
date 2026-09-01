"""Sankey v2 physics: instantaneous power -> twelve directed source->sink flows.

Supersedes ``decompose.py``. Five live readings go in:

    solar    W, DC, >= 0
    battery  W, DC, positive charging / negative discharging
    grid     W, AC, positive importing / negative exporting
    house    W, AC, >= 0, INCLUDING the Tesla
    tesla    W, AC, >= 0, the home-charging template sensor

and twelve non-negative watt figures come out, one per link of the v2 graph.

Why this module exists at all
-----------------------------
v1 spent the sources in a fixed order and had to refer DC to AC with fitted
constants (``ETA``, ``ETA_FLOOR``, ``0.9974``, ``-105.9 W``) to make the four
sensors balance. It could not close the last ~3%, and worse, the order it spent
sources in *was* the story the chart told: reorder the links and the narrative
changed while the energy did not.

v2 fixes both with one move. The DC/AC gap is not modelled, it is **measured and
named**: whatever arrives that does not leave is the Inverter node,

    L = max(0, (S + B + G) - (Hr + T + C + E))

so every sink fills exactly by construction and there is nothing left to fit.
See ``NO_FITTED_CONSTANTS`` below -- the absence is load-bearing, not an
oversight.

The attribution rule is proportional, not sequential: after export is taken
(structurally solar-only), every remaining sink draws the same source mix. That
is the only order-free, symmetric rule, and unlike a greedy walk its result does
not depend on where a source sits in any list.

**The guarantee is narrower than "it can never fabricate a flow", and the
difference matters.** What holds absolutely is only this: a source reading
EXACTLY zero contributes exactly zero to every sink. A source reading anything
above zero can be spent well beyond its own reading -- without bound in
principle -- whenever the ``L >= 0`` clamp binds, because the sinks then demand
more than the pool holds and filling each one exactly costs the sources the
difference. ``decompose(0, 0, 100, 900, 0)`` credits the grid with 900 W against
a 100 W meter. Over four replayed days that overstatement is worth +0.02% to
+0.42% of daily source energy, and it is the safe direction (the card clamps a
ribbon to the node's own state), but it is real and it is not going to be
corrected: the only way to close it is to model the DC/AC gap, which is exactly
the fitted machinery this module exists to remove.

What the numbers do and do not mean
-----------------------------------
* ``solar_to_export`` is the whole metered export, capped at the solar reading.
  Nothing else may feed it: the battery cannot export (all three discharge
  windows are unset and must stay unset) and import-straight-to-export is not a
  thing on this site. There is no ``battery_to_export`` or ``grid_to_export``
  key at all -- those flows are unrepresentable, not merely computed as zero.
* The Inverter node is a **residual, not a meter**. It absorbs the true
  conversion and housekeeping loss, and also every sensor-skew artefact: the
  five readings are polled independently, so a step change lands there first.
  Over a day the skew averages out and what is left is real.
* The shared loss is charged to sources *proportionally*. A battery trickling at
  40 W really does lose ~78% of it while solar at 4 kW loses ~2.5%, so
  proportional undercharges the battery for its own inefficiency. That is the
  known cost of the default, and it is why ``LOSS_RULES`` exists with a
  per-source band-table alternative implemented alongside. Measure, do not
  assume.
* A reading that means "no reading" -- ``"unavailable"``, ``"unknown"``, ``""``,
  ``None``, a container, NaN, inf, a bool, or a magnitude past
  ``MAX_PLAUSIBLE_W`` -- raises ``BadReading`` rather than becoming 0.0. See
  that class for why a zeroed chart is the dangerous outcome.

Two exceptions, and callers must NOT treat them alike
-----------------------------------------------------
``BadReading`` means the input was bad. Routine and recoverable: catch it,
render that flow sensor ``unavailable``, hold the integral.

``StructureError`` means **this module is broken** -- a negative flow, a key
that must not exist, a sample reading as charging and discharging at once.
Nothing is wrong with the input. **Let it propagate. Do not catch it, and the
Jinja templates must NOT map it to `unavailable`**, because a code defect
rendering as a missing sensor is invisible: it looks exactly like TeslaMate
being down, and we would ship a broken decomposition and see a sensor blink out
occasionally. The two share no ancestry, and a test requires that no bad input
can reach one nor well-formed input the other.
"""
import math

# The twelve directed flows, in a fixed order. Keys are stable; tests and the
# helper/template plumbing key off these exact strings.
#
# The absent combinations are the design: no battery_to_export (the battery has
# no legal path to the grid), no grid_to_export (import and export never
# coexist on one meter reading, and passing energy straight through would be
# fiction), and no *_to_battery from the battery itself (a pack cannot charge
# and discharge in the same instant -- one signed sensor forbids it).
FLOWS = (
    "solar_to_house",
    "solar_to_tesla",
    "solar_to_battery",
    "solar_to_export",
    "solar_to_inverter",
    "battery_to_house",
    "battery_to_tesla",
    "battery_to_inverter",
    "grid_to_house",
    "grid_to_tesla",
    "grid_to_battery",
    "grid_to_inverter",
)

# The five section-1 sinks each flow lands in, and the source of each flow.
SINKS = ("house", "tesla", "battery", "export", "inverter")
SOURCES = ("solar", "battery", "grid")

# The five input channels, in decompose()'s argument order.
CHANNELS = ("solar", "battery", "grid", "house", "tesla")

# Which input channels each reportable quantity can actually be moved by.
#
# This exists so that a replay over a day where one sensor did not yet exist
# can REFUSE exactly the quantities that sensor could have affected, and keep
# the rest -- instead of discarding the whole day, or (far worse) reporting a
# zero-filled number as though it were measured. 2026-08-27 is the live case:
# the Tesla template was created at 13:27, and CLAUDE.md records 8.384 kWh
# going into the car that same night, so a zero there is a fabrication.
#
# It is a map rather than a hardcoded pair of names because a hardcoded list
# rots silently: add a thirteenth flow that reads a blind channel and the old
# list happily reports a fabricated number for it. The map is verified COMPLETE
# AND MINIMAL by test_dependency_map_is_complete_and_minimal, which perturbs
# each channel and requires "moves" and "declared" to agree exactly. Minimality
# matters as much as completeness: a map that declared every channel for every
# quantity would be safe and useless, refusing all of 2026-08-27.
#
# The non-obvious entries, all consequences of Hr + T == house:
#   *_to_battery   ignores house AND tesla -- C depends on battery alone, and
#                  the shares on solar/battery/grid alone.
#   *_to_inverter  ignores tesla -- L reads Hr + T, never either part.
#   solar_spent    ignores tesla -- to_house + to_tesla is house * share.
#   the six counter-facing quantities ALL ignore tesla, which is precisely what
#   licenses keeping 2026-08-27's inverter reconciliation.
_SBG = frozenset(("solar", "battery", "grid"))
_SBGH = _SBG | {"house"}
_ALL = _SBGH | {"tesla"}

DEPENDS_ON = {
    "solar_to_export": frozenset(("solar", "grid")),
    "solar_to_battery": _SBG, "grid_to_battery": _SBG,
    "solar_to_inverter": _SBGH, "battery_to_inverter": _SBGH,
    "grid_to_inverter": _SBGH,
    "solar_to_house": _ALL, "battery_to_house": _ALL, "grid_to_house": _ALL,
    "solar_to_tesla": _ALL, "battery_to_tesla": _ALL, "grid_to_tesla": _ALL,
    # node_totals() keys
    "house": _ALL, "tesla": _ALL,
    "battery_in": _SBG,
    "export": frozenset(("solar", "grid")),
    "inverter": _SBGH,
    "solar_spent": _SBGH, "battery_spent": _SBGH, "grid_spent": _SBGH,
}


def unreportable(blind_channels):
    """Quantities that must not be reported while these channels are blind.

    A blind channel is one with no reading at all -- a sensor that does not
    exist yet, or a gap. It is NOT a channel reading zero: those two are
    indistinguishable downstream, which is the whole reason this exists.

    Returns a frozenset of quantity names. A caller reports None for each,
    never 0.0.
    """
    blind = frozenset(blind_channels)
    unknown = blind - set(CHANNELS)
    if unknown:
        raise ValueError("not input channels: %r" % (sorted(unknown),))
    return frozenset(q for q, deps in DEPENDS_ON.items() if deps & blind)

# Nothing on this site can move 100 kW. The inverter is 6 kW, the battery 5 kW,
# and a ~7 kW Tesla slot on top of the house peaks near 10 kW; even a 100 A
# single-phase supply is 23 kW. The ceiling exists to reject a u32 sign misread:
# CLAUDE.md records register 33257 read unsigned as 4294967253 W, i.e. 2^32 - 43,
# when the truth was -43 W. Rejected rather than clamped -- clamping would
# silently turn 4.29 GW of "import" into a plausible-looking chart.
MAX_PLAUSIBLE_W = 100_000.0

# Deliberately empty. v2 has no fitted efficiency, no fitted parasitic, no
# nominal eta anywhere on the default path: the Inverter node is the measured
# residual. If a constant of that kind ever appears in this module, the design
# has regressed -- test_flows.py asserts on the module source that it has not.
NO_FITTED_CONSTANTS = ()


class StructureError(Exception):
    """A structural rule of the graph was broken. **flows.py is broken.**

    This is NOT a BadReading and deliberately shares no ancestry with it. The
    two mean opposite things and must not be handled alike:

        BadReading      the INPUT was bad. Routine, recoverable, expected. A
                        sensor went away. The Jinja equivalent is rendering
                        that flow sensor `unavailable` and holding the
                        integral, which is exactly right.
        StructureError  the ARITHMETIC was bad. Nothing is wrong with the
                        input. We produced a negative flow, or a key that must
                        not exist, or a sample that reads as charging and
                        discharging at once.

    Collapsing the second into the first is the worst available outcome: a code
    defect would render as `unavailable`, look identical to TeslaMate being
    down, and be invisible in HA -- we would ship a broken decomposition and
    see a sensor blink out occasionally. This exception is loud and
    wrong-shaped on purpose.

    **Callers must let this propagate. Do not catch it, and the Jinja templates
    must NOT map it to `unavailable`.** It found a real regression in 20
    minutes once (an export cap deleted while its comment stayed behind)
    precisely because it did not look like a routine sensor fault.
    """


class BadReading(ValueError, TypeError):
    """A sensor reading that must not be decomposed.

    Deliberately both a ValueError and a TypeError: ``None`` and a list are the
    wrong type, ``"unavailable"`` and NaN are the wrong value, and a caller
    catching either one gets what it expects.

    Raising rather than substituting 0.0 is the whole point. The dominant
    failure mode here is logger session contention -- the Solarman logger
    permits one Modbus session at a time, so a competing process gets nothing --
    and a zeroed chart is indistinguishable from a still night. A template
    sensor that throws renders ``unavailable``; one that returns 0 renders a
    confident lie.

    **That last sentence reaches the right answer for the wrong reason, and the
    real mechanism changes what the plumbing must guarantee.** The natural
    defence of it -- "unavailability does not propagate, because the link
    ``value:`` is a daily utility_meter which holds its last value" -- is
    FALSE. v2-helpers flipped one availability template to false on a live
    accumulating chain and the template, the integration AND the utility_meter
    all went unavailable within 10 s, then recovered exactly, losing nothing.
    The meter does not hold.

    It is harmless anyway, because the card never sees the unavailable state.
    Under ``energy_date_selection: true`` a link's ``value:`` entity is pushed
    into the SAME ``entityIds`` array as the node ids (6.3.0 bundle char
    100603), that array goes to ``recorder/statistics_during_period`` (96800),
    and the result OVERWRITES ``state`` for any entity that merely EXISTS in
    ``hass.states``, whatever its live state is (96959). An unavailable link
    value is therefore replaced by its ``sum(change)`` and never reaches
    ``Math.min`` as NaN.

    **The real hazard is a different one, and it is worse.** ``ki`` (char
    75288) returns **null**, not 0, for an entity with no statistics rows.
    ``String(null)`` is ``"null"``, ``Number("null")`` is NaN, and that NaN
    poisons ``parent_spent`` and silently zeroes every later ribbon from that
    source -- t-allocator's failure reached by a different door. So the
    load-bearing property is not "never unavailable", it is **has a
    ``state_class``**. A utility_meter carries ``total_increasing`` natively,
    which is why the link values point at the meters directly and the wrapper
    stage was cancelled.

    This class does NOT need reopening: raising is still right, and the reason
    is the statistics substitution rather than how ``unavailable`` renders.
    Evidence: read from the bundle at the offsets above, plus one live
    propagation measurement. ONE CONTINGENCY REMAINS OPEN -- the substitution
    requires the entity to appear in ``hass.states`` at all, and whether an
    unavailable entity does (rather than being absent) is still being measured.
    If it comes back "absent", this reasoning needs revisiting.
    """


def _reading(name, value):
    """Parse one sensor reading, or raise BadReading.

    Numeric strings are the normal path, not the exotic one: HA states are
    always strings, so ``"2982"``, ``" 3000 "`` and ``"3e3"`` all have to parse.
    What must not parse is anything meaning "no reading" -- and NaN is the
    subtle one. ``float("nan")`` succeeds, and then ``max(0.0, nan)`` returns
    0.0 because ``nan > 0.0`` is False, so without an explicit isnan guard a
    broken sensor decomposes byte-identically to an idle one.
    """
    # bool is an int subclass, so an unguarded True would decompose as 1 W.
    # A bool is a state, not a measurement.
    if isinstance(value, bool):
        raise BadReading("%s: %r is not a power reading" % (name, value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise BadReading("%s: empty reading" % name)
        try:
            v = float(text)
        except ValueError:
            raise BadReading("%s: %r is not a number" % (name, value)) from None
    else:
        try:
            v = float(value)
        except (TypeError, ValueError):
            raise BadReading("%s: %r is not a number" % (name, value)) from None

    if math.isnan(v):
        raise BadReading("%s is NaN -- a broken sensor, not a zero" % name)
    if math.isinf(v):
        raise BadReading("%s is infinite" % name)
    if abs(v) > MAX_PLAUSIBLE_W:
        raise BadReading(
            "%s: %g W exceeds %g W, so it is a misread and not a measurement"
            % (name, v, MAX_PLAUSIBLE_W)
        )
    return v


def readings(solar, battery, grid, house, tesla):
    """Five raw inputs -> the eight non-negative quantities the law works in.

    Split out and public because every test of the clamps wants to see these
    directly, and because the Jinja templates that will ship to HA must be
    checkable against exactly this arithmetic and no more.

    ``tesla`` is clamped to ``house`` before ``house_rest`` is taken. That clamp
    is not defensive tidying: ``sensor.tesla_home_charging_power`` is a
    TeslaMate on-change value that can hold a plateau for ~80 minutes (the held
    value is correct, not stale), while ``house_load`` is polled every 10 s, so
    on the sample where a charge stops the two disagree for one interval and
    ``house - tesla`` would go negative. Clamping keeps ``house_rest`` >= 0
    structurally, with no second guard needed.
    """
    solar = _reading("solar", solar)
    battery = _reading("battery", battery)
    grid = _reading("grid", grid)
    house = _reading("house", house)
    tesla = _reading("tesla", tesla)

    house = max(0.0, house)
    t = min(max(0.0, tesla), house)
    return {
        "solar": max(0.0, solar),          # S
        "discharge": max(0.0, -battery),   # B, DC delivered
        "charge": max(0.0, battery),       # C, DC stored
        "imp": max(0.0, grid),             # G, AC imported
        "exp": max(0.0, -grid),            # E, AC exported
        "house": house,
        "tesla": t,                        # T, clamped to house
        "house_rest": house - t,           # Hr, >= 0 by the clamp above
    }


def inverter_loss(r):
    """The Inverter node: what came in and did not leave. Never negative.

        L = max(0, (S + B + G) - (Hr + T + C + E))

    Clamped at zero because the alternative is a negative ribbon, and because a
    negative residual means the *sinks* out-read the sources on that sample --
    sensor skew, not energy from nowhere. The samples it fires on are the ones
    where a step change reached the AC sensors before the DC ones. Measured
    cost over four replayed days: the sources are over-spent by +0.02% to
    +0.42% of their daily energy, because the sinks still fill exactly while
    the pool has shrunk.
    """
    supply = r["solar"] + r["discharge"] + r["imp"]
    drawn = r["house_rest"] + r["tesla"] + r["charge"] + r["exp"]
    return max(0.0, supply - drawn)


def _normalise(weights):
    """Three non-negative weights -> three shares summing to 1, or three zeros.

    The zero return is the division guard, and it is an exact ``<= 0.0`` test
    rather than an epsilon one on purpose. v1 trimmed with ``over > _EPS`` and
    leaked a nanowatt through the hole that left; there is no threshold here for
    anything to hide under.
    """
    total = weights[0] + weights[1] + weights[2]
    if total <= 0.0:
        return (0.0, 0.0, 0.0)
    return (weights[0] / total, weights[1] / total, weights[2] / total)


# --------------------------------------------------------------------------
# Loss rules -- the swappable block.
#
# A rule takes the post-export quantities and returns a share matrix: for each
# of the four remaining sinks, what fraction of it is credited to solar, to
# battery discharge, and to grid import. Every row must sum to 1 (or to 0 when
# there is no supply at all), which is what makes every sink fill exactly
# regardless of which rule ran.
#
# Nothing below this block knows which rule is active. Swap the rule, not the
# law.
# --------------------------------------------------------------------------

# flow-law's measured DC-band efficiencies, used ONLY by the band-table rule.
# These are measurements handed over, not parameters fitted here; the default
# path never touches them. Band -> (representative DC watts, efficiency):
#
#     0-50 W  0.22   50-100 W  0.24   200-400 W  0.68
#     1.5-3 kW  0.93   5-7 kW  0.975
#
# The battery trickles at 50-100 W for 39 of the 88 hours flow-law measured,
# where the inverter's fixed housekeeping eats nearly all of it. That is the
# whole reason this alternative exists: proportional charges that trickle's loss
# to whichever source happens to be large at the time.
BAND_TABLE = ((25.0, 0.22), (75.0, 0.24), (300.0, 0.68), (2250.0, 0.93), (6000.0, 0.975))

# flow-law's measured charging-direction figure: DC stored per AC drawn.
ETA_CHARGE = 0.887


def band_efficiency(dc_watts):
    """Interpolate BAND_TABLE at a DC power. Flat outside the measured range.

    Linear between band mid-points. That is an interpolation of somebody else's
    measurements, not a curve fitted here -- there is no free parameter in it,
    and the endpoints are held flat rather than extrapolated, because a line
    through 0.22 and 0.24 continued downward crosses zero and a line continued
    upward past 0.975 crosses one. Neither is physical.
    """
    if dc_watts <= BAND_TABLE[0][0]:
        return BAND_TABLE[0][1]
    for i in range(1, len(BAND_TABLE)):
        x1, y1 = BAND_TABLE[i]
        if dc_watts <= x1:
            x0, y0 = BAND_TABLE[i - 1]
            return y0 + (y1 - y0) * (dc_watts - x0) / (x1 - x0)
    return BAND_TABLE[-1][1]


def _fallback(share, alternative):
    """A degenerate all-zero share row, replaced by a usable one.

    Distinguishes "no supply, so nothing to attribute" -- where the alternative
    is all-zero too and this changes nothing -- from "supply exists but this
    weighting collapsed", which would otherwise leave a sink unfilled with no
    signal at all.
    """
    if share == (0.0, 0.0, 0.0):
        return alternative
    return share


def _proportional(solar1, discharge, imp):
    """DEFAULT. Every sink draws the identical source mix.

    Order-free and symmetric: there is no first source and no elastic one, so no
    reordering of nodes or links can change a single number. The cost is that a
    trickling battery's own ~78% loss is spread across whatever else is flowing
    instead of charged to the battery.
    """
    share = _normalise((solar1, discharge, imp))
    return {"house_rest": share, "tesla": share, "charge": share, "loss": share}


def _band_table(solar1, discharge, imp):
    """ALTERNATIVE. Charge each source the loss its own DC band predicts.

    Three different weightings, because the three kinds of sink sit in different
    places relative to the converter:

    * AC sinks (house remainder, Tesla) are weighted by what each source can
      actually *deliver* as AC: ``eta(P) * P`` for the DC sources, and grid
      import unchanged because the smart meter already reads AC.
    * The DC sink (battery charge) is weighted by DC availability: solar goes
      into the pack DC-coupled, grid has to cross the converter first and so is
      derated by ``ETA_CHARGE``. A discharging battery cannot feed a charging
      one, and the single signed sensor makes that structural, so its weight is
      zero.
    * The Inverter node is weighted by each source's own *predicted loss*,
      ``P - eta(P) * P``. This is the entire point of the rule: a 40 W battery
      trickle carries ~78% of itself into that column, where proportional would
      have charged it 2.376 W -- 5.9% of its own output, measured on that same
      sample (4 kW solar, 40 W battery, 240 W total loss) -- and handed the
      rest to solar.

    Known approximation, stated rather than hidden: ``eta`` is evaluated at each
    source's own DC power, but flow-law measured the inverter's *aggregate*
    DC->AC behaviour, and most of the loss is a roughly fixed housekeeping
    draw. When solar and battery both flow, each is charged as though it alone
    were carrying that fixed cost, so this rule over-predicts total loss in
    exactly those samples. It is a seam for measurement, not a claim to be
    better.

    When no DC source is running there is no predicted loss to weight by, and
    the Inverter row falls back to the AC-delivery row -- which puts the loss on
    grid import, the only thing that was flowing.

    EVERY row falls back to the plain proportional row if its own weighting
    degenerates to all-zero while supply exists. Without that this rule breaks
    the contract the block comment above states -- and it is reachable, not
    theoretical: at a denormal discharge (5e-324 W) the AC weight
    ``discharge * 0.22`` underflows to exactly 0.0, every row goes to zero, and
    the sinks are silently left unfilled while the pool is non-empty. Found by
    the hypothesis property, not by inspection.

    The charge row's fallback cannot misroute: if charge > 0 then discharge is
    0, so the proportional row's battery weight is 0 too, and a discharging
    battery can never be credited with charging one.
    """
    ac = (solar1 * band_efficiency(solar1), discharge * band_efficiency(discharge), imp)
    dc = (solar1, 0.0, imp * ETA_CHARGE)
    loss = (solar1 - ac[0], discharge - ac[1], 0.0)

    plain = _normalise((solar1, discharge, imp))
    ac_share = _fallback(_normalise(ac), plain)
    loss_share = _fallback(_normalise(loss), ac_share)
    return {
        "house_rest": ac_share,
        "tesla": ac_share,
        "charge": _fallback(_normalise(dc), plain),
        "loss": loss_share,
    }


LOSS_RULES = {"proportional": _proportional, "band_table": _band_table}

LOSS_RULE = "proportional"


def set_loss_rule(name):
    """Select the loss rule. Returns the previous name."""
    if name not in LOSS_RULES:
        raise ValueError("unknown loss rule: %r" % (name,))
    global LOSS_RULE
    previous = LOSS_RULE
    LOSS_RULE = name
    return previous


def check_structure(out, r):
    """Raise StructureError if one of the five structural rules is broken.

    NOT AssertionError -- StructureError shares no ancestry with AssertionError
    OR with BadReading. Catching it as either is wrong, and the docstring said
    AssertionError for a while after the body had stopped raising one, which
    cost v2-invariants a round of tests written against the wrong exception.

    The five: key-set drift, a negative flow, a sample reading as charging and
    discharging at once, a sample reading as importing and exporting at once,
    and a charging battery credited as a source.

    These are not defensive re-checks of arithmetic that obviously holds. They
    are the properties the *graph* depends on, and each one has a way to be
    violated by a future edit: adding a key resurrects a link the dashboard must
    never draw, a negative value inverts a ribbon, and a sample that reads as
    charging and discharging at once means the signed battery sensor has been
    replaced by two unsigned ones somewhere upstream.

    Raises StructureError, never BadReading: by the time we are here the input
    has already been validated, so anything wrong is OUR fault. Written as
    explicit raises rather than ``assert`` so they survive ``python -O`` and so
    each failure path is reachable from a test.
    """
    if set(out) != set(FLOWS):
        raise StructureError(
            "flow keys drifted: %r" % (sorted(set(out) ^ set(FLOWS)),))
    for k, v in out.items():
        if v < 0.0:
            raise StructureError("%s is negative: %r" % (k, v))
    if r["charge"] > 0.0 and r["discharge"] > 0.0:
        raise StructureError("battery reads charging and discharging at once")
    if r["imp"] > 0.0 and r["exp"] > 0.0:
        raise StructureError("grid reads importing and exporting at once")
    if r["charge"] > 0.0 and (
            out["battery_to_house"] or out["battery_to_tesla"]
            or out["battery_to_inverter"]):
        raise StructureError("a charging battery cannot also be a source")


def decompose(solar, battery, grid, house, tesla):
    """Instantaneous W -> {flow: W}. Twelve keys, every value >= 0.

    ``house`` is the full house load *including* the Tesla; ``tesla`` is split
    back out here. Both are AC. ``solar`` and ``battery`` are DC.
    """
    r = readings(solar, battery, grid, house, tesla)

    # Step 1: export is structurally solar-only, and capped by the solar
    # reading. Uncapped, a sample where export momentarily out-reads solar would
    # credit a source with energy it did not produce -- at night that would draw
    # a solar ribbon in the dark. The card would clamp the ribbon away, which is
    # exactly why the cap has to be here instead: a number nobody can see is
    # still wrong.
    s2e = min(r["solar"], r["exp"])
    solar1 = r["solar"] - s2e

    # Step 2: every remaining sink draws the same source mix (or whatever mix
    # the active loss rule dictates). Sinks fill exactly; sources are left with
    # whatever remainder the measurement implies.
    sinks = {
        "house_rest": r["house_rest"],
        "tesla": r["tesla"],
        "charge": r["charge"],
        "loss": inverter_loss(r),
    }
    shares = LOSS_RULES[LOSS_RULE](solar1, r["discharge"], r["imp"])

    key = {"house_rest": "house", "tesla": "tesla", "charge": "battery",
           "loss": "inverter"}
    out = {"solar_to_export": s2e}
    for sink, amount in sinks.items():
        ws, wb, wg = shares[sink]
        out["solar_to_" + key[sink]] = amount * ws
        out["battery_to_" + key[sink]] = amount * wb
        out["grid_to_" + key[sink]] = amount * wg
    # Solar and grid can feed the pack; a discharging battery cannot. The
    # battery_to_battery link does not exist, so drop the row the loop built.
    del out["battery_to_battery"]

    check_structure(out, r)
    return out


def node_totals(flows):
    """Flows -> the state each v2 node should show, in the same units.

    The five section-1 nodes are defined as the sum of their inbound flows --
    that is the v2 node-identity decision, and it is what makes House exclude
    the Tesla without a subtract_entities trick. The three sources are summed
    too, but only for comparison against the inverter's own counters: those
    sums may fall short of the counters, and the shortfall is the loss the
    Inverter node carries, not an error.
    """
    return {
        "house": sum(flows[s + "_to_house"] for s in SOURCES),
        "tesla": sum(flows[s + "_to_tesla"] for s in SOURCES),
        "battery_in": flows["solar_to_battery"] + flows["grid_to_battery"],
        "export": flows["solar_to_export"],
        "inverter": sum(flows[s + "_to_inverter"] for s in SOURCES),
        "solar_spent": sum(v for k, v in flows.items() if k.startswith("solar_to_")),
        "battery_spent": sum(v for k, v in flows.items() if k.startswith("battery_to_")),
        "grid_spent": sum(v for k, v in flows.items() if k.startswith("grid_to_")),
    }
