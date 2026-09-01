"""Instantaneous flow decomposition for the Solis hybrid inverter.

Four live sensors, two of them on the wrong side of the converter:

    solar    W, DC, >= 0
    battery  W, DC, positive charging / negative discharging
    grid     W, AC, positive importing / negative exporting
    house    W, AC, >= 0

so the four cannot be reconciled: DC in and AC out differ by the conversion
loss. A live sample -- solar 2982, battery -457, grid -98, house 3078 -- has
solar + discharge = 3439 DC against house + export = 3176 AC, a 263 W gap.

Two things follow, and the whole design turns on them.

**The loss has to be parked somewhere, and the only harmless place is an
unspent source remainder.** The consumer is ha-sankey-chart, which draws each
ribbon as ``min(parent_remainder, child_remainder, our_value)``. Overstating a
flow is therefore clamped away; understating one silently shrinks a ribbon and
nothing anywhere says so. A source left with an unspent remainder just renders
a slightly shorter bar, which CLAUDE.md already records as expected and not an
error. So: fill every measured sink exactly, spend the constrained sources
fully, and let the residual sit on solar.

**Allocation order is constrained-source-first, not the self-use narrative.**
Battery discharge has exactly one legal outlet -- all three discharge windows
are unset, so the battery can never export -- and grid import is a hard AC
measurement that must land somewhere. Solar is the elastic one. Spending solar
first is what produced the bug this module exists to fix: DC solar was fully
absorbed by the AC house load, so ``solar_to_export`` came out 0 while the
meter measured 98 W flowing out. CLAUDE.md records the same lesson from the
chart side ("the most physically constrained source must come first").

Consequences worth knowing before reading a number out of here:

* ``solar_to_export`` is always the full metered export. No other source may
  feed it (battery cannot export; import-straight-to-export is not a thing on
  this site), so export is solar's by elimination.
* ``battery_to_house`` is the battery's only outflow, and there is no
  ``battery_to_grid`` key at all -- battery-to-grid is structurally
  unrepresentable, not merely computed as zero. It is credited with what
  ARRIVES, not what the pack emitted (see BATTERY_RULES), so the Battery-out
  bar carries a visible unspent remainder -- the owner's call on 2026-08-30.
* Solar's *spend* is a residual, so it can exceed the solar reading on an
  internally inconsistent sample -- but only up to ``solar / ETA_FLOOR``, the
  most a plausible conversion loss could hide. On energy-consistent samples it
  cannot exceed the reading at all, because every other source is spent
  maximally first. When solar reads 0 the bound is 0, so no solar flow can
  appear at night.
* A reading that means "no reading" -- "unavailable", "unknown", "", None, a
  container, NaN, or a magnitude past MAX_PLAUSIBLE_W -- raises BadReading
  rather than becoming 0.0. See that class for why.
* Every metered watt of grid import lands somewhere: the smart meter is
  authoritative, so grid_to_house takes whatever grid_to_battery does not,
  without being capped at a deficit computed from `house`. Consequence,
  accepted deliberately: the total into House can EXCEED house_load. That is
  safe and the shortfall was not -- house_load reads about 60-85 W low against
  its own energy counter every day (t-replay, three full days), and the card
  clamps House against that counter, not against house_load.
"""
import math

from contract import FLOWS

# Nominal DC<->AC conversion efficiency, used only by the non-default loss
# treatments.
#
# **No single constant is correct here, and this one is only right above about
# 3 kW.** flow-law fitted 87.7 h / 64,084 samples: AC = 0.9974 * DC - 105.9 W,
# rms 145 W. The conversion itself is ~99.7% efficient; what looks like a
# percentage loss is a roughly *fixed ~106 W parasitic* -- the inverter's own
# housekeeping. Measured efficiency by DC band:
#
#     0-50 W    0.22        200-400 W   0.68        5-7 kW   0.975
#     50-100 W  0.24        1.5-3 kW    0.93
#
# The battery trickles at 50-100 W for 39 of those 88 hours, where housekeeping
# eats nearly all of it. This also resolves an apparent contradiction in
# CLAUDE.md: the "~2% loss" and the "8% observed" are the same phenomenon at
# different power levels, not two separate facts. Do not retune this number to
# split the difference -- a scale factor is the wrong shape for a fixed offset.
ETA = 0.96

# Lowest plausible DC->AC efficiency, and so the bound on how far solar may be
# credited beyond its own reading. CLAUDE.md records ~2% conversion loss as
# typical and the four sensors are polled ~10 s apart, so a stale solar reading
# on a step change can legitimately look up to several percent low; 8% has been
# observed. 10% is the outer edge of that. Beyond it we are no longer covering
# for skew, we are inventing energy.
#
# Provisional: flow-law is calibrating a loss treatment against recorded
# history and may supersede this. It is used in exactly one place.
ETA_FLOOR = 0.90

# Nothing on this site can move 100 kW. The inverter is 6 kW, the battery 5 kW,
# and a Tesla slot plus the house peaks near 10 kW; even a 100 A single-phase
# supply is 23 kW. The ceiling exists to reject a u32 sign misread: CLAUDE.md
# records reading register 33257 unsigned as 4294967253 W, i.e. 2^32 - 43, when
# the truth was -43 W. Rejected rather than clamped -- clamping would silently
# turn 4.29 GW of "import" into a plausible-looking chart.
MAX_PLAUSIBLE_W = 100_000.0

_EPS = 1e-9


class BadReading(ValueError, TypeError):
    """A sensor reading that must not be decomposed.

    Deliberately both a ValueError and a TypeError: `None` and a list are the
    wrong type, "unavailable" and NaN are the wrong value, and a caller
    catching either one gets what it expects.

    Raising rather than substituting 0.0 is the whole point. The dominant
    failure mode here is logger session contention -- one Modbus session at a
    time, so a competing process gets nothing -- and a zeroed chart is
    indistinguishable from a still night. A template sensor that throws renders
    `unavailable`; one that returns 0 renders a confident lie.
    """


# --------------------------------------------------------------------------
# DC/AC loss treatment -- the swappable block.
#
# Each treatment takes the raw non-negative readings and returns them possibly
# adjusted, plus ``solar_cap``: the most solar may be credited with supplying.
# math.inf means "do not cap" -- solar's spend is then a pure residual and the
# loss lands on its unspent remainder.
#
# Nothing below this block knows which treatment ran. Swap the treatment, not
# the law.
# --------------------------------------------------------------------------

def _residual_on_solar(r):
    """DEFAULT. Nothing is scaled; the loss surfaces as unspent solar.

    Solar's spend is a residual, bounded above by ``solar / ETA_FLOOR``: it may
    be credited with covering the conversion loss of a skewed sample, but not
    with arbitrary energy. When solar reads exactly 0 that bound is 0, so at
    night no solar flow can appear at all.

    Error introduced: the Solar node's outgoing ribbons under-sum its own
    state by the conversion loss (263 W on the sample above, ~8%). Nothing
    else is distorted -- house, export, battery charge and battery discharge
    are all drawn at their measured values.

    On a consistent sample this yields exactly the same six numbers as
    ``derived_solar_ac`` below, because spending battery and grid maximally
    first leaves solar with precisely ``house - grid + battery``. It differs
    only when that derivation would exceed the solar reading, where this one
    overstates (clamped by the chart) rather than understating.
    """
    r = dict(r)
    r["solar_cap"] = r["solar"] / ETA_FLOOR
    return r


def _fixed_efficiency(r):
    """Present both DC sources at an AC equivalent using a fixed ETA.

    Error introduced: ETA is nominal, so on any sample whose real efficiency
    differs the sinks no longer fill exactly -- and ``battery_to_house`` is
    scaled below the battery-discharge sensor, stranding part of that box.
    Understatement, i.e. the dangerous direction. Here for comparison.
    """
    r = dict(r)
    r["discharge"] *= ETA
    r["solar_cap"] = r["solar"] * ETA
    return r


def _derived_solar_ac(r):
    """Close the AC balance exactly: solar_ac = house - grid + battery.

    Does CLAUDE.md's "never derive grid flow from PV and battery power"
    prohibition apply in reverse? No, and the asymmetry is the point. That
    rule protects a *measured AC* quantity (the smart meter) from being
    replaced by a DC-derived one, because ~2% conversion loss then shows up as
    plausible-looking phantom export. Here the meter and the house load stay
    authoritative and the derived quantity is the DC source's AC equivalent --
    the loss lands on the thing that actually incurred it. The direction that
    was banned invents energy; this one accounts for it.

    Error introduced: solar's AC equivalent inherits the noise of three
    sensors instead of one, and any sensor skew (the four are polled ~10 s
    apart) lands entirely on solar. It also caps solar, so on a skewed sample
    it can understate a solar ribbon.
    """
    r = dict(r)
    r["solar_cap"] = max(
        0.0, r["house"] - r["imp"] + r["exp"] + r["charge"] - r["discharge"]
    )
    return r


LOSS_TREATMENTS = {
    "residual_on_solar": _residual_on_solar,
    "fixed_efficiency": _fixed_efficiency,
    "derived_solar_ac": _derived_solar_ac,
}

LOSS_TREATMENT = "residual_on_solar"


def set_loss_treatment(name):
    """Select the DC/AC loss treatment. Returns the previous name."""
    if name not in LOSS_TREATMENTS:
        raise ValueError("unknown loss treatment: %r" % (name,))
    global LOSS_TREATMENT
    previous = LOSS_TREATMENT
    LOSS_TREATMENT = name
    return previous


# --------------------------------------------------------------------------
# The law
# --------------------------------------------------------------------------

def _reading(name, value):
    """Parse one sensor reading, or raise BadReading.

    Numeric strings are the normal path, not the exotic one: HA states are
    always strings, so "2982", " 3000 " and "3e3" all have to parse. What must
    not parse is anything that means "no reading": "unavailable", "unknown",
    the empty string, None, a container -- and NaN, which is the subtle one.
    float("nan") succeeds, and then max(0.0, nan) returns 0.0 because
    `nan > 0.0` is False, so without an explicit isnan guard a broken sensor
    decomposes byte-identically to an idle one.
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


def _readings(solar, battery, grid, house):
    solar = _reading("solar", solar)
    battery = _reading("battery", battery)
    grid = _reading("grid", grid)
    house = _reading("house", house)
    return {
        "solar": max(0.0, solar),
        "solar_raw": max(0.0, solar),  # never touched by a loss treatment
        "house": max(0.0, house),
        "charge": max(0.0, battery),
        "discharge": max(0.0, -battery),
        "imp": max(0.0, grid),
        "exp": max(0.0, -grid),
    }


# --------------------------------------------------------------------------
# Battery-to-house rule -- the second swappable block.
#
# The battery has exactly one legal outlet, so it is tempting to pay its
# discharge in full. Two measurements say otherwise, and they only look
# contradictory:
#
#   t-replay, against the battery_discharge_today counter (DC side): b2h is
#     13-18% short.
#   flow-law, against what actually arrives at the house in AC terms: the
#     battery emits ~23.0 kWh DC over 4 days, we credit 19.97, and only 17.58
#     lands. Paying in full would credit 23.0 for 17.58 delivered.
#
# Both are true; they measure different sides of the converter. Paying in full
# is the worse error, and -- unlike solar -- it is not caught downstream: the
# card clamps b2h against the Battery-out node's own state (~23.0 kWh DC),
# which sits ABOVE 19.97, so the cap never binds and the chart would draw the
# house receiving energy it never got. Overstating is only safe where the node
# total actually binds it.
#
# The honest alternative is AC referral: credit the battery with what arrives,
# `discharge * eta(discharge)` using the band table above, which flow-law
# measured at b2h 17.58 / s2h 27.77 and a battery remainder of 5.32 instead of
# 2.93. It leaves the Battery-out bar visibly ~23% short, which is an editorial
# call for the owner, not a correctness one. NOT IMPLEMENTED pending that
# decision -- this seam exists so it is a drop-in when the call is made.
# --------------------------------------------------------------------------

# flow-law's fit over 87.7 h / 64,084 samples: AC = 0.9974 * DC - 105.9 W,
# rms 145 W. Slope, not efficiency: the conversion is ~99.7% and the loss is a
# fixed parasitic. See the ETA comment for the measured band table.
REFERRAL_SLOPE = 0.9974
PARASITIC_W = 105.9

# The fit alone says NOTHING arrives below ~106 W, which contradicts flow-law's
# own measured 0.22-0.24 efficiency in the 0-100 W bands -- and the battery sits
# there for 39 of 88 hours. Flooring the referral at the measured 0-50 W figure
# uses their number rather than an extrapolation of a line past its data.
LOW_BAND_ETA = 0.22


def _ac_referral(discharge, house):
    """DEFAULT. Credit the battery with what actually reaches the house.

    The battery emits ~23 kWh DC over four days but only ~17.6 kWh arrives,
    because it trickles at 50-100 W for 39 of 88 hours where measured
    efficiency is 0.22-0.24 and the fixed parasitic eats nearly all of it.

    The Battery-out bar is left visibly ~23% short as a result, and its error
    against the DC battery_discharge_today counter stays large (about -13 to
    -18%). **That is not a leak and must not be "fixed".** It is the DC/AC
    measurement gap made visible, which the owner chose on 2026-08-30 over a
    prettier chart: internal honesty first. The unspent remainder is the
    energy the inverter's own housekeeping consumed.
    """
    ac = max(LOW_BAND_ETA * discharge, REFERRAL_SLOPE * discharge - PARASITIC_W)
    return min(max(0.0, ac), house)


def _load_capped(discharge, house):
    """Credit the battery with its full DC discharge, capped at the load.

    The previous default, kept as the documented alternative. It draws a
    fuller Battery-out bar, but credits the house with ~19.97 kWh over the
    four days against the ~17.58 kWh that actually arrived. Unlike an
    overstated solar flow, that overcredit is NOT caught downstream: the card
    clamps b2h against the Battery-out node's own state, which is the DC
    counter (~23.0 kWh) and therefore never binds.
    """
    return min(discharge, house)


BATTERY_RULES = {"ac_referral": _ac_referral, "load_capped": _load_capped}

BATTERY_RULE = "ac_referral"


def set_battery_rule(name):
    """Select the battery-to-house rule. Returns the previous name."""
    if name not in BATTERY_RULES:
        raise ValueError("unknown battery rule: %r" % (name,))
    global BATTERY_RULE
    previous = BATTERY_RULE
    BATTERY_RULE = name
    return previous


def _clean(v):
    v = max(0.0, v)
    return 0.0 if v < _EPS else v


def decompose(solar, battery, grid, house):
    """Instantaneous W -> {flow: W}. Every value is >= 0."""
    r = LOSS_TREATMENTS[LOSS_TREATMENT](_readings(solar, battery, grid, house))
    house, charge, exp = r["house"], r["charge"], r["exp"]
    discharge, imp, cap = r["discharge"], r["imp"], r["solar_cap"]

    # Treatment-independent: nothing generated means nothing to attribute.
    # Crediting metered export to a source reading 0 W is semantically false
    # even though the chart would clamp it away.
    if r["solar_raw"] == 0.0:
        cap = 0.0

    b2h = BATTERY_RULES[BATTERY_RULE](discharge, house)

    # Grid import is a hard AC smart-meter reading and CLAUDE.md makes that
    # meter authoritative, so every watt of it must land somewhere. Solar fills
    # the battery first -- seeing solar reach the battery is the chart's whole
    # point -- grid covers the rest of the charge, and the remainder goes to
    # the house unconditionally. Sizing this against a `house`-derived deficit
    # instead leaked 6.3-9.8% of daily import (t-replay, 08-27/28/29), because
    # house_load runs about 60-85 W low against its own energy counter.
    # g2b is bounded by the import that actually exists: the meter is
    # authoritative in both directions, so an unmet charge cannot conjure
    # import that was never metered.
    s2b = min(charge, r["solar"])
    g2b = min(imp, max(0.0, charge - s2b))
    g2h = imp - g2b

    # Solar takes the slack. Export is solar's by elimination.
    s2e = exp
    s2h = house - b2h - g2h

    # Trim to the cap. Reverse order of how badly a missing ribbon misleads:
    # battery charge first, export last -- a vanished export ribbon is the
    # exact failure this module fixes. Only fires on a sample whose sinks
    # exceed what solar could plausibly have supplied.
    over = (s2e + s2h + s2b) - cap
    if over > _EPS:
        take = min(over, s2b)
        s2b -= take
        over -= take
        take = min(over, s2h)
        s2h -= take
        over -= take
        s2e -= min(over, s2e)

    out = {
        "solar_to_house": _clean(s2h),
        "solar_to_battery": _clean(s2b),
        "solar_to_export": _clean(s2e),
        "battery_to_house": _clean(b2h),
        "grid_to_house": _clean(g2h),
        "grid_to_battery": _clean(g2b),
    }
    assert set(out) == set(FLOWS)
    return out
