"""Property-based invariants for the six-flow energy decomposition.

These test the CONTRACT in ``contract.py``, not the implementation's internals.

Where an invariant here disagrees with ``decompose.py``, the disagreement is
resolved explicitly - by narrowing the invariant to the case it actually holds
in, with the reason written down, or by leaving it failing and arguing. Nothing
is relaxed to an arbitrary fudge. Four rulings shape the suite:

1. **The per-source bound on solar is ``solar / ETA_FLOOR``, not ``solar``.**
   The four sensors straddle the converter, so an instantaneous sample cannot
   balance. The consuming chart resolves every ribbon as
   ``min(parent_remainder, child_remainder, our_value)``, so an OVERSTATED flow
   is clamped away harmlessly while an UNDERSTATED one silently shrinks a
   ribbon. Overshoot is the safe direction; ``ETA_FLOOR`` makes it the physical
   bound rather than a fudge. See ``SOLAR_CAP_FACTOR``.

2. **The House sink may be overfilled, but never beyond total supply.** Every
   metered import watt must land somewhere; capping House against ``house_load``
   leaked 6.3-9.8% of daily import because that sensor reads 60-85 W low. So
   ``to_house <= house`` is withdrawn and ``to_house <= supply`` replaces it.

3. **Sink-exactness is GATED on the sample having a legal source, not
   weakened.** ``solar == 0`` with ``grid < 0`` has no source that could feed
   export - the battery may never export and import cannot become export - so
   it is excluded, and a separate test asserts export is then exactly 0.

4. **``battery_to_house`` is AC-referred**, so it sits well below the DC
   discharge counter by design. Every battery invariant asserts the direction
   (short, never long), never equality with the DC reading.

Two consequences worth knowing before adding a test here:

* **Scale invariance is dead on the battery path**, correctly. The AC referral
  is affine (a fixed ~106 W parasitic, not a percentage), so ``f(kx) != k f(x)``
  whenever the battery discharges. The scaling tests assume ``battery >= 0``;
  the break itself is pinned by its own xfail.
* **A DC-side energy balance is not an AC-side one.** At 2 W of discharge only
  ~0.44 W arrives. Any "is the sink filled" invariant is stated against
  ``_ac_supply``, or it asserts something physically false at low power.

Non-finite, unparseable and implausible readings raise ``BadReading`` rather
than becoming 0.0 - a zeroed chart is indistinguishable from a still night, and
logger session contention makes that the dominant real failure mode.

Remaining xfails are all deliberate trades, each naming its cause: ``STARVED_SINK``
(a sink with no legal source is drawn short rather than filled with invented
energy), ``TREATMENT_OVERSHOOT`` and the non-default loss treatments, and
``EPS_BOUNDARY``. Search for "XFAIL:" to read them.
"""

import math

import pytest
from hypothesis import HealthCheck, assume, example, given, settings
from hypothesis import strategies as st

import decompose as decompose_mod
from contract import FLOWS
from decompose import decompose

# Implementation constants the tests need to scope themselves honestly. Read
# via getattr so this file still runs against an implementation without them.
BadReading = getattr(decompose_mod, "BadReading", (ValueError, TypeError))
#: Above this a reading is rejected as a misread rather than decomposed.
MAX_PLAUSIBLE_W = getattr(decompose_mod, "MAX_PLAUSIBLE_W", float("inf"))
#: Worst-case DC->AC efficiency decompose will credit solar with.
ETA_FLOOR = getattr(decompose_mod, "ETA_FLOOR", 1.0)
#: Battery DC->AC referral, fitted over 87.7 h / 64,084 samples.
REFERRAL_SLOPE = getattr(decompose_mod, "REFERRAL_SLOPE", 1.0)
PARASITIC_W = getattr(decompose_mod, "PARASITIC_W", 0.0)
LOW_BAND_ETA = getattr(decompose_mod, "LOW_BAND_ETA", 1.0)

settings.register_profile(
    "solis",
    deadline=None,
    max_examples=200,
    # The loss-treatment fixture sets a module global once per parametrisation
    # and restores it after; it is deliberately constant across generated
    # inputs, which is exactly what this health check warns about.
    suppress_health_check=[
        HealthCheck.filter_too_much,
        HealthCheck.function_scoped_fixture,
    ],
)
settings.load_profile("solis")


# --------------------------------------------------------------------------
# Tolerances. Named so they can be tightened in one place.
# --------------------------------------------------------------------------

#: Fraction of a quantity that may go missing to DC->AC conversion loss.
REL_TOL = 0.08
#: Absolute watt slack for rounding / sensor quantisation.
ABS_TOL = 1.0
#: Slack for pure floating-point re-association (scaling, monotonicity).
FP_TOL = 1e-9


def _slack(x):
    return ABS_TOL + REL_TOL * abs(x)


def _le(a, b):
    """a <= b, allowing conversion loss and rounding."""
    return a <= b + _slack(b)


def _approx(a, b):
    """a == b, allowing conversion loss and rounding."""
    return abs(a - b) <= ABS_TOL + REL_TOL * max(abs(a), abs(b))


#: Solar may be credited with this much more than it reads.
#:
#: THE PER-SOURCE BOUND ON SOLAR IS solar / ETA_FLOOR, NOT solar. This is the
#: physical bound, not a fudge factor, and the asymmetry is deliberate:
#:
#: solar and battery are measured on the DC side, grid and house on the AC
#: side, so an instantaneous sample cannot balance - a live one (solar 2982,
#: battery -457, grid -98, house 3078) is 263 W apart. Something has to absorb
#: that, and the consuming chart decides which direction is safe. It resolves
#: every ribbon as min(parent_remainder, child_remainder, our_value), so an
#: OVERSTATED flow is clamped away harmlessly while an UNDERSTATED one silently
#: shrinks a ribbon with nothing anywhere reporting it. Overshoot is therefore
#: the safe direction, and decompose.py parks the loss on solar as the elastic
#: source.
#:
#: ETA_FLOOR is the worst DC->AC efficiency a real sample could plausibly show,
#: so solar / ETA_FLOOR is the most a genuine conversion loss could hide. A
#: flow above that ceiling is not a loss being absorbed, it is invented energy,
#: and every test below treats it as a failure.
SOLAR_CAP_FACTOR = 1.0 / ETA_FLOOR


def _solar_ceiling(solar):
    """The most solar may be credited with supplying. See SOLAR_CAP_FACTOR."""
    return max(0.0, solar) * SOLAR_CAP_FACTOR + ABS_TOL

#: Shared xfail reason for sinks that cannot be filled from the legal sources.
STARVED_SINK = (
    "XFAIL: with arbitrary (mutually inconsistent) readings a sink can have "
    "no legal source - e.g. solar=0 at night with house>0, or metered export "
    "with no generation, since the battery may never export. decompose "
    "correctly refuses to invent the energy, so the sink is drawn short. The "
    "equality holds only for self-consistent inputs; see those variants."
)

#: Reason for treatments whose ceiling is wider than SOLAR_CAP_FACTOR.
TREATMENT_OVERSHOOT = (
    "XFAIL: this non-default loss treatment does not bound solar's spend by "
    "solar / ETA_FLOOR. Only 'fixed_efficiency' honours a ceiling at or below "
    "the solar reading; 'derived_solar_ac' derives its cap from the other "
    "three sensors and so is unbounded by solar."
)

#: Reason for the sub-nanowatt quantisation edge.
EPS_BOUNDARY = (
    "XFAIL: decompose.py trims solar to its cap only when the excess is "
    "strictly greater than _EPS (1e-9 W), and _clean() snaps to zero only "
    "below _EPS. So up to exactly one nanowatt of solar flow survives at "
    "night, and 'no solar when solar reads 0' holds to within 1e-9 W rather "
    "than exactly. Physically meaningless on a 6 kW inverter; recorded so the "
    "strict equality is not mistaken for a guarantee. The practical form of "
    "this claim is test_no_phantom_solar_at_night, which passes."
)

# Convenience accessors over a result dict.
def _to_house(f):
    return f["solar_to_house"] + f["battery_to_house"] + f["grid_to_house"]


def _to_battery(f):
    return f["solar_to_battery"] + f["grid_to_battery"]


def _from_solar(f):
    return f["solar_to_house"] + f["solar_to_battery"] + f["solar_to_export"]


def _from_grid(f):
    return f["grid_to_house"] + f["grid_to_battery"]


# --------------------------------------------------------------------------
# Strategies. Realistic ranges for this 6 kW inverter / 5 kW 15.4 kWh battery.
# --------------------------------------------------------------------------

SOLAR = st.floats(0.0, 6500.0, allow_nan=False, allow_infinity=False)
BATTERY = st.floats(-5000.0, 5000.0, allow_nan=False, allow_infinity=False)
GRID = st.floats(-6000.0, 10000.0, allow_nan=False, allow_infinity=False)
HOUSE = st.floats(0.0, 12000.0, allow_nan=False, allow_infinity=False)

# Sensors can and do go negative when they should not; the decomposition must
# survive it.
BAD_SOLAR = st.floats(-6500.0, 6500.0, allow_nan=False, allow_infinity=False)
BAD_HOUSE = st.floats(-12000.0, 12000.0, allow_nan=False, allow_infinity=False)

BASE = dict(solar=SOLAR, battery=BATTERY, grid=GRID, house=HOUSE)


@st.composite
def consistent(draw, allow_discharge=True, allow_charge=True):
    """A physically self-consistent reading.

    grid is *computed* to close the AC balance, with a conversion loss taken
    off the DC solar figure, so these are the inputs the decomposition should
    be able to explain completely.
    """
    solar = draw(st.floats(0.0, 6500.0, allow_nan=False, allow_infinity=False))
    house = draw(st.floats(0.0, 8000.0, allow_nan=False, allow_infinity=False))
    lo = -4000.0 if allow_discharge else 0.0
    hi = 4000.0 if allow_charge else 0.0
    battery = draw(st.floats(lo, hi, allow_nan=False, allow_infinity=False))
    loss = draw(st.floats(0.0, REL_TOL, allow_nan=False, allow_infinity=False))
    solar_ac = solar * (1.0 - loss)
    grid = house + battery - solar_ac
    assume(-6000.0 <= grid <= 10000.0)
    return solar, battery, grid, house


# ==========================================================================
# 1. Non-negativity. Every flow, always, no exceptions.
# ==========================================================================


@given(**BASE)
@example(solar=0.0, battery=0.0, grid=0.0, house=0.0)
@example(solar=6500.0, battery=-5000.0, grid=-6000.0, house=0.0)
@example(solar=0.0, battery=5000.0, grid=10000.0, house=12000.0)
def test_all_flows_non_negative(solar, battery, grid, house):
    for k, v in decompose(solar, battery, grid, house).items():
        assert v >= 0.0, f"{k} = {v}"


@given(**BASE)
def test_solar_to_house_non_negative(solar, battery, grid, house):
    assert decompose(solar, battery, grid, house)["solar_to_house"] >= 0.0


@given(**BASE)
def test_solar_to_battery_non_negative(solar, battery, grid, house):
    assert decompose(solar, battery, grid, house)["solar_to_battery"] >= 0.0


@given(**BASE)
def test_solar_to_export_non_negative(solar, battery, grid, house):
    assert decompose(solar, battery, grid, house)["solar_to_export"] >= 0.0


@given(**BASE)
def test_battery_to_house_non_negative(solar, battery, grid, house):
    assert decompose(solar, battery, grid, house)["battery_to_house"] >= 0.0


@given(**BASE)
def test_grid_to_house_non_negative(solar, battery, grid, house):
    assert decompose(solar, battery, grid, house)["grid_to_house"] >= 0.0


@given(**BASE)
def test_grid_to_battery_non_negative(solar, battery, grid, house):
    assert decompose(solar, battery, grid, house)["grid_to_battery"] >= 0.0


@pytest.mark.parametrize(
    "args",
    [
        (0.0, 0.0, 0.0, 0.0),
        (-1.0, -1.0, -1.0, -1.0),
        # At the plausibility limit, not beyond it: past MAX_PLAUSIBLE_W the
        # contract is a BadReading, covered by its own test.
        (MAX_PLAUSIBLE_W, -MAX_PLAUSIBLE_W, -MAX_PLAUSIBLE_W, MAX_PLAUSIBLE_W),
        (0.0, 0.0, -1e-9, 0.0),
        (1e-9, 1e-9, 1e-9, 1e-9),
        (6000.0, 5000.0, -6000.0, 12000.0),
    ],
)
def test_non_negative_on_pathological_inputs(args):
    for k, v in decompose(*args).items():
        assert v >= 0.0, f"{k} = {v} for {args}"


# ==========================================================================
# 2. The battery can never export. All three discharge windows are unset.
# ==========================================================================


def test_no_battery_to_grid_key_exists():
    assert "battery_to_export" not in FLOWS
    assert "battery_to_grid" not in FLOWS
    assert not any("battery_to_" in f and f != "battery_to_house" for f in FLOWS)


@given(**BASE)
def test_returned_keys_are_exactly_the_contract(solar, battery, grid, house):
    assert set(decompose(solar, battery, grid, house)) == set(FLOWS)


@given(**BASE)
@example(solar=0.0, battery=-5000.0, grid=-5000.0, house=0.0)
@example(solar=10.0, battery=0.0, grid=-12.0, house=0.0)
def test_export_never_exceeds_solar(solar, battery, grid, house):
    """Export is bounded by SOLAR_CEILING, not by solar. See that constant."""
    f = decompose(solar, battery, grid, house)
    assert f["solar_to_export"] <= _solar_ceiling(solar)


@given(**BASE)
def test_export_bounded_by_solar_even_while_discharging(solar, battery, grid, house):
    """Discharge must contribute nothing to export - the battery cannot export,
    so a discharging battery may not raise the export ceiling by one watt."""
    assume(battery < 0.0)
    f = decompose(solar, battery, grid, house)
    assert f["solar_to_export"] <= _solar_ceiling(solar)


@given(**BASE)
def test_battery_discharge_reaches_only_the_house(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    discharge = max(0.0, -battery)
    # The only sink the battery may fill is the house, capped by discharge.
    assert _le(f["battery_to_house"], discharge)


# ==========================================================================
# 3. Per-source bounds. A source cannot emit more than it produced.
# ==========================================================================


@given(**BASE)
@example(solar=6500.0, battery=5000.0, grid=10000.0, house=12000.0)
@example(solar=33.0, battery=0.0, grid=0.0, house=37.0)
def test_solar_outflow_within_solar(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert _from_solar(f) <= _solar_ceiling(solar)


@given(**BASE)
@example(solar=1.0, battery=0.0, grid=0.0, house=2.0)
def test_solar_outflow_within_eta_floor_exactly(solar, battery, grid, house):
    """The ceiling with no tolerance added beyond float slop: a treatment may
    hide a conversion loss inside solar, but nothing may hide outside it."""
    f = decompose(solar, battery, grid, house)
    ceiling = max(0.0, solar) * SOLAR_CAP_FACTOR
    assert _from_solar(f) <= ceiling + FP_TOL * (1.0 + ceiling)


@given(**BASE)
def test_solar_overshoot_is_at_most_the_conversion_loss(solar, battery, grid, house):
    """Quantifies the overshoot: never more than solar * (1/ETA_FLOOR - 1)."""
    f = decompose(solar, battery, grid, house)
    overshoot = max(0.0, _from_solar(f) - max(0.0, solar))
    assert overshoot <= max(0.0, solar) * (SOLAR_CAP_FACTOR - 1.0) + ABS_TOL


@given(**BASE)
def test_grid_outflow_within_import(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert _le(_from_grid(f), max(0.0, grid))


@given(**BASE)
def test_grid_outflow_within_import_exactly(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert _from_grid(f) <= max(0.0, grid) + FP_TOL * (1.0 + abs(grid))


@given(**BASE)
def test_battery_outflow_within_discharge(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert _le(f["battery_to_house"], max(0.0, -battery))


@given(**BASE)
def test_no_grid_inflow_while_exporting(solar, battery, grid, house):
    assume(grid < 0.0)
    f = decompose(solar, battery, grid, house)
    assert _from_grid(f) <= ABS_TOL


@given(**BASE)
def test_no_solar_flows_when_solar_is_zero(battery, grid, house, solar):
    """Strict: decompose zeroes the solar cap when the sensor reads exactly 0,
    so no solar ribbon can appear at night however skewed the other three are."""
    f = decompose(0.0, battery, grid, house)
    assert _from_solar(f) <= ABS_TOL


@given(**BASE)
def test_no_battery_inflow_while_discharging(solar, battery, grid, house):
    assume(battery < 0.0)
    f = decompose(solar, battery, grid, house)
    assert _to_battery(f) <= ABS_TOL


@given(**BASE)
def test_grid_import_and_export_are_mutually_exclusive(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    # A single signed sensor cannot be doing both at once.
    assert not (_from_grid(f) > ABS_TOL and f["solar_to_export"] > ABS_TOL)


@given(**BASE)
def test_battery_charge_and_discharge_are_mutually_exclusive(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert not (_to_battery(f) > ABS_TOL and f["battery_to_house"] > ABS_TOL)


@given(**BASE)
@example(solar=33.0, battery=0.0, grid=-37.0, house=0.0)
def test_total_outflow_within_total_supply(solar, battery, grid, house):
    """Global conservation, with solar at its ceiling. Nothing may appear that
    no source could have produced."""
    f = decompose(solar, battery, grid, house)
    supply = _solar_ceiling(solar) + max(0.0, grid) + max(0.0, -battery)
    assert sum(f.values()) <= supply + ABS_TOL


# --------------------------------------------------------------------------
# THE HOUSE SINK MAY BE OVERFILLED. This is a ruling, and it is evidence-based.
#
# Every metered watt of grid import must land somewhere: the smart meter is
# authoritative (CLAUDE.md forbids deriving grid flow from PV and battery), so
# grid_to_house takes whatever grid_to_battery does not, WITHOUT being capped
# at a deficit computed from house_load. t-replay measured the alternative
# across three full days and found capping against house leaked 6.3-9.8% of
# daily import, because house_load reads about 60-85 W low against its own
# energy counter every day. The card clamps House against that counter, not
# against house_load, so the overshoot is absorbed and the shortfall was not.
#
# So "to_house <= house" is NOT an invariant and asserting it was my error.
# The honest bound is the supply side: the house may be credited with more than
# house_load reads, but never with more than the sources could have delivered.
# That is still falsifiable, and it is what these tests assert.
#
# Note the House node on the chart is being changed to the SUM of its inbound
# flows, which makes "house exactly filled" true by construction there. These
# tests deliberately keep measuring against house_load, which is the thing
# decompose actually consumes.
# --------------------------------------------------------------------------


def _total_supply(solar, battery, grid):
    """Everything the three sources could have delivered, solar at its ceiling.

    Uses the RAW DC discharge, because this is an upper bound and the AC
    referral can only reduce what the battery contributes.
    """
    return _solar_ceiling(solar) + max(0.0, -battery) + max(0.0, grid)


def _ac_referred_discharge(battery):
    """Battery discharge as it arrives on the AC side.

    Mirrors decompose.py's _ac_referral. Needed because a DC-side energy
    balance is NOT an AC-side one: at 2 W DC only ~0.44 W reaches the house,
    the fixed ~106 W parasitic having eaten the rest. Any "is the sink filled"
    invariant has to be stated against what actually arrives, or it asserts
    something physically false at low discharge.
    """
    dc = max(0.0, -battery)
    return max(LOW_BAND_ETA * dc, REFERRAL_SLOPE * dc - PARASITIC_W, 0.0)


def _ac_supply(solar, battery, grid):
    """What the sources can actually deliver to AC sinks."""
    return _solar_ceiling(solar) + _ac_referred_discharge(battery) + max(0.0, grid)


def _export_has_a_legal_source(solar, grid):
    """Metered export can only be fed by solar.

    The battery may never export (all three discharge windows unset) and import
    cannot become export, so a solar==0 / grid<0 sample has NO source that could
    feed the export sink. Sink-exactness is gated on this rather than weakened:
    asserting it on such a sample would require crediting a source that
    generated nothing.
    """
    return not (max(0.0, -grid) > 0.0 and max(0.0, solar) == 0.0)


# ==========================================================================
# 4. Per-sink bounds. A sink cannot be filled beyond its demand.
# ==========================================================================


@given(**BASE)
@example(solar=6500.0, battery=-5000.0, grid=10000.0, house=0.0)
def test_house_inflow_never_exceeds_what_the_sources_delivered(
    solar, battery, grid, house
):
    """Replacement for the withdrawn `to_house <= house`."""
    f = decompose(solar, battery, grid, house)
    assert _to_house(f) <= _total_supply(solar, battery, grid) + ABS_TOL


@given(**BASE)
def test_battery_sink_not_overfilled(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert _le(_to_battery(f), max(0.0, battery))


@given(**BASE)
def test_export_sink_not_overfilled(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert _le(f["solar_to_export"], max(0.0, -grid))


# The three sink-fill tests below ask for the sink to be met exactly under
# ARBITRARY inputs. They are the direct counterpart of the SOLAR_OVERSHOOT
# tests: satisfying both at once is impossible, because a sink with no legal
# source can only be filled by crediting solar with energy it did not produce.
# decompose caps solar at solar / ETA_FLOOR and therefore fails these, which
# is the right call - but it is a choice, so it is pinned here rather than
# assumed. The `_for_consistent_inputs` variants below pass and are strict.


@pytest.mark.xfail(strict=False, reason=STARVED_SINK)
@given(**BASE)
@example(solar=0.0, battery=0.0, grid=0.0, house=1.0)
def test_house_sink_exactly_filled_arbitrary_inputs(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert _approx(_to_house(f), max(0.0, house))


@pytest.mark.xfail(strict=False, reason=STARVED_SINK)
@given(**BASE)
@example(solar=0.0, battery=3000.0, grid=-1000.0, house=0.0)
def test_battery_sink_exactly_filled_arbitrary_inputs(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert _approx(_to_battery(f), max(0.0, battery))


@pytest.mark.xfail(strict=False, reason=STARVED_SINK)
@given(**BASE)
def test_export_sink_exactly_filled_when_a_legal_source_exists(
    solar, battery, grid, house
):
    """Export attribution equals the metered export - GATED, not weakened, on
    the sample having a source that could feed it. See
    _export_has_a_legal_source for why solar==0 / grid<0 is excluded."""
    assume(_export_has_a_legal_source(solar, grid))
    f = decompose(solar, battery, grid, house)
    assert _approx(f["solar_to_export"], max(0.0, -grid))


@given(**BASE)
@example(solar=0.0, battery=0.0, grid=-1000.0, house=0.0)
@example(solar=0.0, battery=-5000.0, grid=-6000.0, house=0.0)
@example(solar=0.0, battery=-2.0, grid=-2.0, house=0.0)
def test_zero_solar_exports_exactly_zero_whatever_the_meter_says(
    solar, battery, grid, house
):
    """Looks wrong at first glance, and is not.

    When solar reads 0 the export attribution is 0 even if the meter reports
    export. A source that generated nothing must never be credited with a flow,
    and no other source may feed export. The alternative - crediting solar with
    watts it did not make - is the one direction the chart cannot clamp away,
    because the Solar node's own state would be 0.
    """
    f = decompose(0.0, battery, grid, house)
    assert f["solar_to_export"] == 0.0


@given(consistent())
@example(args=(0.0, -2.0, 0.0, 2.0))
def test_house_sink_at_least_filled_for_consistent_inputs(args):
    """Underfilling is the dangerous direction - it silently shrinks a ribbon -
    so this asserts the house is at LEAST met, up to what the sources can
    actually deliver on the AC side. The AC ceiling is not a let-out: at 2 W of
    discharge only 0.44 W arrives, so demanding a full 2 W here would be
    demanding invented energy."""
    solar, battery, grid, house = args
    f = decompose(*args)
    deliverable = min(house, _ac_supply(solar, battery, grid))
    assert _to_house(f) >= deliverable - _slack(deliverable)


@given(consistent())
def test_house_overfill_is_bounded_on_consistent_inputs(args):
    """The permitted overshoot is not unlimited: on a reading that balances it
    cannot exceed what the sources actually delivered."""
    f = decompose(*args)
    assert _to_house(f) <= _total_supply(args[0], args[1], args[2]) + ABS_TOL


@given(consistent())
def test_battery_sink_filled_for_consistent_inputs(args):
    f = decompose(*args)
    assert _approx(_to_battery(f), max(0.0, args[1]))


@given(consistent(allow_discharge=False))
def test_export_sink_filled_for_consistent_inputs_no_discharge(args):
    f = decompose(*args)
    assert _approx(f["solar_to_export"], max(0.0, -args[2]))


@pytest.mark.xfail(
    strict=False,
    reason=(
        "XFAIL: a self-consistent reading showing the battery discharging "
        "WHILE the grid exports leaves the export ribbon short by roughly the "
        "discharge, because battery->grid is (correctly) not a legal flow and "
        "solar is capped at solar / ETA_FLOOR. A real gap in what the Sankey "
        "can draw, not an arithmetic bug: such readings occur transiently "
        "from the ~10 s skew between the four sensor polls."
    ),
)
@given(consistent(allow_charge=False))
def test_export_sink_filled_for_consistent_inputs_with_discharge(args):
    """Battery discharging WHILE the grid exports."""
    solar, battery, grid, house = args
    assume(grid < -ABS_TOL)
    assume(battery < -ABS_TOL)
    f = decompose(*args)
    assert _approx(f["solar_to_export"], -grid)


@given(consistent(allow_charge=False))
def test_export_shortfall_equals_the_discharge(args):
    """Quantifies the gap above: the missing export is the discharged watts."""
    solar, battery, grid, house = args
    assume(grid < -ABS_TOL)
    assume(battery < -ABS_TOL)
    f = decompose(*args)
    shortfall = max(0.0, -grid) - f["solar_to_export"]
    assert shortfall <= max(0.0, -battery) + _slack(battery)


@given(**BASE)
@example(solar=33.0, battery=0.0, grid=0.0, house=37.0)
def test_house_inflow_within_total_supply(solar, battery, grid, house):
    """Was `<= min(house, supply)`; the demand half was wrong. See the note
    above - House is deliberately allowed to exceed house_load."""
    f = decompose(solar, battery, grid, house)
    assert _to_house(f) <= _total_supply(solar, battery, grid) + ABS_TOL


@given(**BASE)
def test_no_house_inflow_when_house_is_zero_and_nothing_must_land(
    solar, battery, grid, house
):
    """Narrowed: with grid importing, that import has to land somewhere and
    House is where it lands, by ruling. With no import and no discharge there
    is nothing to dispose of, so a zero house must draw zero."""
    assume(grid <= 0.0 and battery >= 0.0)
    f = decompose(solar, battery, grid, 0.0)
    assert _to_house(f) <= ABS_TOL


@given(**BASE)
def test_no_export_when_grid_importing(solar, battery, grid, house):
    assume(grid > 0.0)
    f = decompose(solar, battery, grid, house)
    assert f["solar_to_export"] <= ABS_TOL


# ==========================================================================
# 5. Determinism and purity.
# ==========================================================================


@given(**BASE)
def test_deterministic(solar, battery, grid, house):
    assert decompose(solar, battery, grid, house) == decompose(solar, battery, grid, house)


@given(**BASE)
def test_deterministic_over_many_calls(solar, battery, grid, house):
    first = decompose(solar, battery, grid, house)
    for _ in range(5):
        assert decompose(solar, battery, grid, house) == first


@given(**BASE)
def test_returns_a_fresh_mapping_each_call(solar, battery, grid, house):
    a = decompose(solar, battery, grid, house)
    b = decompose(solar, battery, grid, house)
    assert a is not b


@given(**BASE)
def test_mutating_the_result_does_not_leak(solar, battery, grid, house):
    a = decompose(solar, battery, grid, house)
    a["solar_to_house"] = -12345.0
    b = decompose(solar, battery, grid, house)
    assert b["solar_to_house"] != -12345.0 or _approx(-12345.0, 0.0)


@given(**BASE)
def test_arguments_are_not_mutated(solar, battery, grid, house):
    snapshot = (solar, battery, grid, house)
    decompose(solar, battery, grid, house)
    assert (solar, battery, grid, house) == snapshot


@given(
    solar=st.integers(0, 6500),
    battery=st.integers(-5000, 5000),
    grid=st.integers(-6000, 10000),
    house=st.integers(0, 12000),
)
def test_integer_inputs_match_float_inputs(solar, battery, grid, house):
    a = decompose(solar, battery, grid, house)
    b = decompose(float(solar), float(battery), float(grid), float(house))
    assert a == b


# ==========================================================================
# 6. Monotonicity.
# ==========================================================================

DELTA = st.floats(0.0, 4000.0, allow_nan=False, allow_infinity=False)


@given(**BASE, delta=DELTA)
def test_raising_house_does_not_reduce_house_inflow(solar, battery, grid, house, delta):
    lo = _to_house(decompose(solar, battery, grid, house))
    hi = _to_house(decompose(solar, battery, grid, house + delta))
    assert hi >= lo - FP_TOL * (1.0 + abs(lo))


@given(**BASE, delta=DELTA)
def test_raising_solar_does_not_reduce_solar_outflow(solar, battery, grid, house, delta):
    lo = _from_solar(decompose(solar, battery, grid, house))
    hi = _from_solar(decompose(solar + delta, battery, grid, house))
    assert hi >= lo - FP_TOL * (1.0 + abs(lo))


@given(**BASE, delta=DELTA)
def test_raising_import_does_not_reduce_grid_outflow(solar, battery, grid, house, delta):
    assume(grid >= 0.0)
    lo = _from_grid(decompose(solar, battery, grid, house))
    hi = _from_grid(decompose(solar, battery, grid + delta, house))
    assert hi >= lo - FP_TOL * (1.0 + abs(lo))


@given(**BASE, delta=DELTA)
def test_raising_charge_does_not_reduce_battery_inflow(solar, battery, grid, house, delta):
    assume(battery >= 0.0)
    lo = _to_battery(decompose(solar, battery, grid, house))
    hi = _to_battery(decompose(solar, battery + delta, grid, house))
    assert hi >= lo - FP_TOL * (1.0 + abs(lo))


@given(**BASE, delta=DELTA)
def test_deeper_discharge_does_not_reduce_battery_to_house(solar, battery, grid, house, delta):
    assume(battery <= 0.0)
    lo = decompose(solar, battery, grid, house)["battery_to_house"]
    hi = decompose(solar, battery - delta, grid, house)["battery_to_house"]
    assert hi >= lo - FP_TOL * (1.0 + abs(lo))


@given(**BASE, delta=DELTA)
def test_more_export_does_not_reduce_solar_to_export(solar, battery, grid, house, delta):
    assume(grid <= 0.0)
    lo = decompose(solar, battery, grid, house)["solar_to_export"]
    hi = decompose(solar, battery, grid - delta, house)["solar_to_export"]
    assert hi >= lo - FP_TOL * (1.0 + abs(lo))


@given(**BASE, delta=DELTA)
def test_raising_house_does_not_reduce_solar_to_house(solar, battery, grid, house, delta):
    lo = decompose(solar, battery, grid, house)["solar_to_house"]
    hi = decompose(solar, battery, grid, house + delta)["solar_to_house"]
    assert hi >= lo - FP_TOL * (1.0 + abs(lo))


@given(**BASE, delta=DELTA)
def test_raising_house_does_not_increase_solar_to_export(solar, battery, grid, house, delta):
    """House has first call on PV under self-use, so export can only shrink."""
    lo = decompose(solar, battery, grid, house)["solar_to_export"]
    hi = decompose(solar, battery, grid, house + delta)["solar_to_export"]
    assert hi <= lo + FP_TOL * (1.0 + abs(lo))


# --------------------------------------------------------------------------
# SCALE INVARIANCE IS DEAD ON THE BATTERY PATH, AND CORRECTLY SO.
#
# decompose.py refers battery discharge to the AC side with
#
#     ac = max(LOW_BAND_ETA * discharge, REFERRAL_SLOPE * discharge - PARASITIC_W)
#
# fitted over 87.7 h / 64,084 samples as AC = 0.9974 * DC - 105.9 W. That is
# AFFINE, not linear: the loss is a fixed ~106 W parasitic (the inverter's own
# housekeeping), not a percentage. A fixed offset by definition does not scale,
# so f(k*x) != k*f(x) whenever the battery is discharging. Doubling the power
# does not double the housekeeping.
#
# This is the loss treatment breaking homogeneity, which the brief asked to be
# reported rather than forced. The tests below therefore assume battery >= 0,
# where the law is still exactly homogeneous, and the affine break is pinned
# by its own xfail immediately after.
# --------------------------------------------------------------------------


# ==========================================================================
# 7. Scale invariance / positive homogeneity of degree 1.
# ==========================================================================

FACTOR = st.floats(0.1, 10.0, allow_nan=False, allow_infinity=False)


#: decompose.py's _clean() snaps any flow below this to exactly 0.0, so the
#: decomposition is quantised and homogeneity cannot hold below it. Physically
#: irrelevant (a nanowatt), but it has to be in the tolerance or the scaling
#: tests assert something false. test_quantisation_breaks_homogeneity_below_eps
#: pins the behaviour on its own so this is documented, not buried.
QUANT_W = 1e-9


def _in_band(k, *vals):
    """Scaling past MAX_PLAUSIBLE_W is rejected by design, so homogeneity is
    only a claim about readings that remain decomposable."""
    return k * max(abs(v) for v in vals) <= MAX_PLAUSIBLE_W


def _scale_tol(k, solar, battery, grid, house):
    span = max(abs(solar), abs(battery), abs(grid), abs(house), 1.0)
    return FP_TOL * k * span + QUANT_W * max(1.0, k)


@given(**BASE)
def test_doubling_inputs_doubles_outputs(solar, battery, grid, house):
    assume(battery >= 0.0)  # see the note above: AC referral is affine
    assume(_in_band(2.0, solar, battery, grid, house))
    base = decompose(solar, battery, grid, house)
    doubled = decompose(2 * solar, 2 * battery, 2 * grid, 2 * house)
    tol = _scale_tol(2.0, solar, battery, grid, house)
    for k in FLOWS:
        assert abs(doubled[k] - 2 * base[k]) <= tol, k


@given(**BASE, k=FACTOR)
def test_homogeneous_for_any_positive_factor(solar, battery, grid, house, k):
    assume(battery >= 0.0)  # see the note above: AC referral is affine
    assume(_in_band(k, solar, battery, grid, house))
    base = decompose(solar, battery, grid, house)
    scaled = decompose(k * solar, k * battery, k * grid, k * house)
    tol = _scale_tol(k, solar, battery, grid, house)
    for key in FLOWS:
        assert abs(scaled[key] - k * base[key]) <= tol, key


@given(
    battery=st.floats(1e-12, 1e-8, allow_nan=False, allow_infinity=False),
    k=st.floats(0.1, 0.9, allow_nan=False, allow_infinity=False),
)
def test_quantisation_breaks_homogeneity_below_eps(battery, k):
    """Sub-nanowatt homogeneity. Held as of decompose.py 16:27:54; it fails
    the moment _clean() snaps small flows to zero, which an earlier revision
    did. Physically irrelevant, kept as a canary on that snap returning."""
    base = decompose(0.0, battery, 0.0, 0.0)["solar_to_battery"]
    scaled = decompose(0.0, k * battery, 0.0, 0.0)["solar_to_battery"]
    assert scaled == pytest.approx(k * base, rel=1e-9, abs=1e-18)


@given(**BASE)
def test_scaling_by_zero_gives_zero(solar, battery, grid, house):
    f = decompose(0.0 * solar, 0.0 * battery, 0.0 * grid, 0.0 * house)
    assert all(v == 0.0 for v in f.values())


@given(**BASE, k=FACTOR)
def test_homogeneity_preserves_the_house_share(solar, battery, grid, house, k):
    assume(battery >= 0.0)  # see the note above: AC referral is affine
    assume(_in_band(k, solar, battery, grid, house))
    base = _to_house(decompose(solar, battery, grid, house))
    scaled = _to_house(decompose(k * solar, k * battery, k * grid, k * house))
    tol = _scale_tol(k, solar, battery, grid, house) * len(FLOWS)
    assert abs(scaled - k * base) <= tol


@given(args=consistent(), k=FACTOR)
def test_homogeneous_on_consistent_inputs(args, k):
    assume(args[1] >= 0.0)  # see the note above: AC referral is affine
    assume(_in_band(k, *args))
    base = decompose(*args)
    scaled = decompose(*[k * a for a in args])
    tol = _scale_tol(k, *args) * 2
    for key in FLOWS:
        assert abs(scaled[key] - k * base[key]) <= tol, key


@given(**BASE)
def test_halving_inputs_halves_outputs(solar, battery, grid, house):
    assume(battery >= 0.0)  # see the note above: AC referral is affine
    base = decompose(solar, battery, grid, house)
    half = decompose(solar / 2, battery / 2, grid / 2, house / 2)
    tol = _scale_tol(1.0, solar, battery, grid, house)
    for k in FLOWS:
        assert abs(half[k] - base[k] / 2) <= tol, k


# ==========================================================================
# 8. Zero cases.
# ==========================================================================


def test_all_zero_gives_all_zero():
    assert decompose(0, 0, 0, 0) == {k: 0.0 for k in FLOWS}


@pytest.mark.xfail(strict=False, reason=EPS_BOUNDARY)
@given(battery=BATTERY, grid=GRID, house=HOUSE)
def test_zero_solar_leaves_only_battery_and_grid_flows(battery, grid, house):
    f = decompose(0.0, battery, grid, house)
    assert f["solar_to_house"] == 0.0
    assert f["solar_to_battery"] == 0.0
    assert f["solar_to_export"] == 0.0


@given(solar=SOLAR, battery=BATTERY, grid=GRID)
def test_zero_house_leaves_no_house_flows(solar, battery, grid):
    """Same narrowing as above."""
    assume(grid <= 0.0 and battery >= 0.0)
    f = decompose(solar, battery, grid, 0.0)
    assert f["solar_to_house"] == 0.0
    assert f["battery_to_house"] == 0.0
    assert f["grid_to_house"] == 0.0


@given(solar=SOLAR, battery=BATTERY, house=HOUSE)
def test_zero_grid_leaves_no_grid_flows_and_no_export(solar, battery, house):
    f = decompose(solar, battery, 0.0, house)
    assert f["grid_to_house"] == 0.0
    assert f["grid_to_battery"] == 0.0
    assert f["solar_to_export"] == 0.0


@given(solar=SOLAR, grid=GRID, house=HOUSE)
def test_zero_battery_leaves_no_battery_flows(solar, grid, house):
    f = decompose(solar, 0.0, grid, house)
    assert f["solar_to_battery"] == 0.0
    assert f["grid_to_battery"] == 0.0
    assert f["battery_to_house"] == 0.0


@pytest.mark.xfail(strict=False, reason=EPS_BOUNDARY)
@given(house=HOUSE)
def test_only_house_demand_yields_nothing(house):
    f = decompose(0.0, 0.0, 0.0, house)
    assert all(v == 0.0 for v in f.values())


@given(solar=SOLAR)
def test_only_solar_yields_nothing_with_no_sink(solar):
    f = decompose(solar, 0.0, 0.0, 0.0)
    assert all(v == 0.0 for v in f.values())


@given(battery=st.floats(-5000.0, 0.0, allow_nan=False, allow_infinity=False))
def test_discharge_with_no_load_yields_nothing(battery):
    f = decompose(0.0, battery, 0.0, 0.0)
    assert all(v == 0.0 for v in f.values())


def test_pure_grid_supply_reaches_the_house():
    f = decompose(0.0, 0.0, 3000.0, 3000.0)
    assert _approx(f["grid_to_house"], 3000.0)
    assert f["solar_to_house"] == 0.0
    assert f["battery_to_house"] == 0.0


def test_pure_solar_export():
    f = decompose(4000.0, 0.0, -4000.0, 0.0)
    assert _approx(f["solar_to_export"], 4000.0)


def test_timed_window_grid_charging():
    """23:30-05:30 window: grid feeds house and battery, no solar."""
    f = decompose(0.0, 2500.0, 3200.0, 700.0)
    assert _approx(f["grid_to_house"], 700.0)
    assert _approx(f["grid_to_battery"], 2500.0)
    assert f["solar_to_export"] == 0.0
    assert f["battery_to_house"] == 0.0


# ==========================================================================
# 9. Sign robustness. Bad sensor data must not produce negative flows.
# ==========================================================================


@given(solar=BAD_SOLAR, battery=BATTERY, grid=GRID, house=BAD_HOUSE)
@example(solar=-1.0, battery=0.0, grid=0.0, house=-1.0)
def test_negative_sensor_readings_never_produce_negative_flows(solar, battery, grid, house):
    for k, v in decompose(solar, battery, grid, house).items():
        assert v >= 0.0, f"{k} = {v}"


@given(solar=st.floats(-6500.0, -1e-6, allow_nan=False, allow_infinity=False), **{
    "battery": BATTERY, "grid": GRID, "house": HOUSE})
def test_negative_solar_behaves_as_zero_solar(solar, battery, grid, house):
    assert decompose(solar, battery, grid, house) == decompose(0.0, battery, grid, house)


@given(house=st.floats(-12000.0, -1e-6, allow_nan=False, allow_infinity=False), **{
    "solar": SOLAR, "battery": BATTERY, "grid": GRID})
def test_negative_house_behaves_as_zero_house(solar, battery, grid, house):
    assert decompose(solar, battery, grid, house) == decompose(solar, battery, grid, 0.0)


@given(solar=BAD_SOLAR, battery=BATTERY, grid=GRID, house=BAD_HOUSE)
def test_negative_inputs_still_respect_source_bounds(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert _from_solar(f) <= _solar_ceiling(solar)
    assert _le(_from_grid(f), max(0.0, grid))


@given(solar=BAD_SOLAR, battery=BATTERY, grid=GRID, house=BAD_HOUSE)
def test_negative_inputs_still_respect_sink_bounds(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    # House is a supply-side bound now; the battery sink is still exact.
    assert _to_house(f) <= _total_supply(solar, battery, grid) + ABS_TOL
    assert _le(_to_battery(f), max(0.0, battery))


@given(
    solar=st.floats(0.0, MAX_PLAUSIBLE_W, allow_nan=False, allow_infinity=False),
    battery=st.floats(
        -MAX_PLAUSIBLE_W, MAX_PLAUSIBLE_W, allow_nan=False, allow_infinity=False
    ),
    grid=st.floats(
        -MAX_PLAUSIBLE_W, MAX_PLAUSIBLE_W, allow_nan=False, allow_infinity=False
    ),
    house=st.floats(0.0, MAX_PLAUSIBLE_W, allow_nan=False, allow_infinity=False),
)
def test_any_plausible_reading_decomposes_without_raising(solar, battery, grid, house):
    """Everything up to MAX_PLAUSIBLE_W must decompose, however implausible the
    *combination*. Only individual magnitudes are grounds for rejection."""
    f = decompose(solar, battery, grid, house)
    assert all(v >= 0.0 and math.isfinite(v) for v in f.values())


@pytest.mark.parametrize("slot", range(4))
def test_readings_beyond_the_plausible_limit_are_rejected(slot):
    """A misread, not a measurement: 1e9 W on a 6 kW inverter. Rejecting beats
    decomposing it, per BadReading's docstring."""
    if not math.isfinite(MAX_PLAUSIBLE_W):
        pytest.skip("implementation has no plausibility limit")
    args = [1000.0, 500.0, 200.0, 1500.0]
    args[slot] = MAX_PLAUSIBLE_W * 10.0
    with pytest.raises(BadReading):
        decompose(*args)


@given(
    solar=st.floats(0.0, 1e-6, allow_nan=False, allow_infinity=False),
    battery=st.floats(-1e-6, 1e-6, allow_nan=False, allow_infinity=False),
    grid=st.floats(-1e-6, 1e-6, allow_nan=False, allow_infinity=False),
    house=st.floats(0.0, 1e-6, allow_nan=False, allow_infinity=False),
)
def test_subwatt_inputs_do_not_crash(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert all(v >= 0.0 and math.isfinite(v) for v in f.values())


# ==========================================================================
# 10. No NaN or Inf ever escapes for finite input.
# ==========================================================================


@given(**BASE)
@example(solar=0.0, battery=0.0, grid=0.0, house=0.0)
def test_no_nan_or_inf_for_finite_input(solar, battery, grid, house):
    for k, v in decompose(solar, battery, grid, house).items():
        assert math.isfinite(v), f"{k} = {v}"


@given(solar=BAD_SOLAR, battery=BATTERY, grid=GRID, house=BAD_HOUSE)
def test_no_nan_or_inf_for_negative_input(solar, battery, grid, house):
    for k, v in decompose(solar, battery, grid, house).items():
        assert math.isfinite(v), f"{k} = {v}"


@given(consistent())
def test_no_nan_or_inf_for_consistent_input(args):
    for k, v in decompose(*args).items():
        assert math.isfinite(v), f"{k} = {v}"


@given(**BASE)
def test_all_outputs_are_real_numbers(solar, battery, grid, house):
    for k, v in decompose(solar, battery, grid, house).items():
        assert isinstance(v, (int, float)) and not isinstance(v, bool), k


@pytest.mark.parametrize(
    "bad", [float("nan"), float("inf"), float("-inf")], ids=["nan", "inf", "-inf"]
)
@pytest.mark.parametrize("slot", range(4))
def test_non_finite_reading_is_rejected_not_zeroed(bad, slot):
    """A broken sensor must not decompose byte-identically to an idle one.

    ``max(0.0, nan)`` returns 0.0 because ``nan > 0.0`` is False, so without an
    explicit guard NaN would silently become a confident zero. decompose raises
    instead - the load-bearing behaviour, given that logger session contention
    is the dominant real failure mode.
    """
    args = [1000.0, 500.0, 200.0, 1500.0]
    args[slot] = bad
    with pytest.raises(BadReading):
        decompose(*args)


@pytest.mark.parametrize(
    "bad", [None, "", "  ", "unavailable", "unknown", [], {}, True, False, object()]
)
def test_unreadable_sensor_states_are_rejected(bad):
    """HA states are strings, and 'unavailable'/'unknown' are the ones that
    matter. True/False are in here because bool is an int subclass and would
    otherwise decompose as 1 W / 0 W."""
    with pytest.raises(BadReading):
        decompose(bad, 0.0, 0.0, 0.0)


@pytest.mark.parametrize(
    "text,expected", [("2982", 2982.0), (" 3000 ", 3000.0), ("3e3", 3000.0)]
)
def test_numeric_strings_parse(text, expected):
    """HA states are always strings, so this is the normal path."""
    assert decompose(text, 0.0, 0.0, 0.0) == decompose(expected, 0.0, 0.0, 0.0)


def test_nan_does_not_decompose_like_an_idle_inverter():
    """The specific confusion BadReading exists to prevent."""
    idle = decompose(0.0, 0.0, 0.0, 0.0)
    with pytest.raises(BadReading):
        broken = decompose(float("nan"), 0.0, 0.0, 0.0)
        assert broken != idle  # unreachable; states the intent if it ever is


# ==========================================================================
# 11. Structural contract.
# ==========================================================================


def test_flows_has_exactly_six_keys():
    assert len(FLOWS) == 6
    assert len(set(FLOWS)) == 6


def test_flow_names_use_source_to_sink_form():
    for f in FLOWS:
        assert f.count("_to_") == 1, f


def test_every_flow_name_has_a_known_source_and_sink():
    sources = {"solar", "battery", "grid"}
    sinks = {"house", "battery", "export"}
    for f in FLOWS:
        src, sink = f.split("_to_")
        assert src in sources, f
        assert sink in sinks, f


@given(**BASE)
def test_key_set_is_stable_across_inputs(solar, battery, grid, house):
    assert tuple(sorted(decompose(solar, battery, grid, house))) == tuple(sorted(FLOWS))


@given(**BASE)
def test_result_is_a_plain_mapping_of_six_entries(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert len(f) == 6


@given(**BASE)
def test_no_self_loop_battery_to_battery(solar, battery, grid, house):
    f = decompose(solar, battery, grid, house)
    assert "battery_to_battery" not in f
    # A charging battery must be fed from outside itself.
    if battery > 0:
        assert _le(_to_battery(f), max(0.0, solar) + max(0.0, grid))


# ==========================================================================
# 11b. Phantom solar at night.
#
# The realistic face of the uncapped-solar trade. The four sensors are polled
# ~10 s apart, so at 03:00 with solar genuinely 0 a lagging grid reading leaves
# a house or battery deficit that decompose attributes to SOLAR. The Sankey
# then draws a solar ribbon in the dark. These are the cases to re-run if the
# cap is ever added.
# ==========================================================================

NIGHT_SKEW = [
    # (solar, battery, grid, house), description
    ((0.0, 0.0, 2900.0, 3000.0), "grid poll lags house by 100 W"),
    ((0.0, 2500.0, 3000.0, 700.0), "timed-window charging, grid lags by 200 W"),
    ((0.0, 0.0, 0.0, 2.0), "house alone, everything else zero"),
    ((0.0, 0.0, -2.0, 0.0), "metered export with no generation at all"),
]


@pytest.mark.parametrize("args,why", NIGHT_SKEW)
def test_no_phantom_solar_at_night(args, why):
    f = decompose(*args)
    assert _from_solar(f) <= ABS_TOL, f"{why}: solar spend {_from_solar(f)} W at night"


@pytest.mark.parametrize("args,why", NIGHT_SKEW)
def test_phantom_solar_is_bounded_by_the_measurement_gap(args, why):
    """Quantifies the above: the invented solar never exceeds the sensor
    disagreement, so it is skew being papered over, not unbounded invention."""
    solar, battery, grid, house = args
    f = decompose(*args)
    gap = abs(
        (max(0.0, solar) + max(0.0, -battery) + max(0.0, grid))
        - (max(0.0, house) + max(0.0, battery) + max(0.0, -grid))
    )
    invented = max(0.0, _from_solar(f) - max(0.0, solar))
    assert invented <= gap + ABS_TOL, why


def test_live_sample_from_the_module_docstring():
    """solar 2982, battery -457, grid -98, house 3078 - the 263 W DC/AC gap.

    battery_to_house is AC-REFERRED, so it is deliberately well below the 457 W
    DC discharge reading: 0.9974*457 - 105.9 = 349.9 W actually arrives, and the
    ~107 W difference is the inverter's own housekeeping. Per the owner's
    2026-08-30 decision that shortfall is the DC/AC gap made visible and must
    not be closed, so this pins the direction and the arithmetic, not equality
    with the DC counter.
    """
    f = decompose(2982, -457, -98, 3078)
    assert _approx(f["solar_to_export"], 98)
    assert f["grid_to_house"] == 0.0
    ac = f["battery_to_house"]
    assert ac == pytest.approx(0.9974 * 457 - 105.9, abs=0.5)
    assert ac < 457  # short, never long
    # The house is met, with solar taking the slack the battery no longer covers.
    assert _to_house(f) >= 3078 - ABS_TOL
    assert _from_solar(f) <= _solar_ceiling(2982)


# ==========================================================================
# 12. Loss treatments.
#
# Implementation-specific rather than contract-level: decompose.py exposes
# three swappable DC/AC loss treatments. This section exists because the
# per-source bound failures above are NOT inherent to the problem - one of the
# three treatments satisfies them - so the team can see the cost of each
# choice rather than only the default's.
#
# Skipped wholesale if the API is not present, so the contract tests above
# stay runnable against any implementation.
# ==========================================================================

decompose_mod = pytest.importorskip("decompose")
TREATMENTS = sorted(getattr(decompose_mod, "LOSS_TREATMENTS", {}))
_has_treatments = bool(TREATMENTS) and hasattr(decompose_mod, "set_loss_treatment")

pytestmark_treatments = pytest.mark.skipif(
    not _has_treatments, reason="decompose.py exposes no loss-treatment API"
)


@pytest.fixture
def treatment(request):
    previous = decompose_mod.set_loss_treatment(request.param)
    try:
        yield request.param
    finally:
        decompose_mod.set_loss_treatment(previous)


def _params():
    return pytest.mark.parametrize(
        "treatment", TREATMENTS or ["none"], indirect=True
    )


@pytestmark_treatments
@_params()
@given(**BASE)
def test_non_negative_under_every_treatment(treatment, solar, battery, grid, house):
    for k, v in decompose(solar, battery, grid, house).items():
        assert v >= 0.0, f"{k} = {v} under {treatment}"


@pytestmark_treatments
@_params()
@given(**BASE)
def test_finite_under_every_treatment(treatment, solar, battery, grid, house):
    for k, v in decompose(solar, battery, grid, house).items():
        assert math.isfinite(v), f"{k} = {v} under {treatment}"


@pytestmark_treatments
@_params()
@given(**BASE)
def test_export_never_exceeds_solar_under_every_treatment(
    treatment, solar, battery, grid, house
):
    """The headline contract bound, per treatment.

    Expected: passes under fixed_efficiency, fails under residual_on_solar and
    derived_solar_ac. Recorded rather than xfailed so the pass/fail split by
    treatment is visible in the run.
    """
    if treatment != "fixed_efficiency":
        pytest.xfail(TREATMENT_OVERSHOOT + f" (treatment={treatment})")
    f = decompose(solar, battery, grid, house)
    assert _le(_from_solar(f), max(0.0, solar))


@pytestmark_treatments
@_params()
@given(**BASE)
def test_deterministic_under_every_treatment(treatment, solar, battery, grid, house):
    assert decompose(solar, battery, grid, house) == decompose(
        solar, battery, grid, house
    )


@pytestmark_treatments
@_params()
@given(args=consistent())
def test_sinks_filled_under_every_treatment(treatment, args):
    """fixed_efficiency scales battery discharge by a nominal ETA, so it
    understates battery_to_house - the dangerous direction, per its own
    docstring. Recorded here."""
    f = decompose(*args)
    if treatment != "residual_on_solar":
        pytest.xfail(
            "XFAIL: only the default 'residual_on_solar' treatment fills the "
            "house. 'fixed_efficiency' scales discharge by a nominal ETA on "
            "top of the AC referral; 'derived_solar_ac' caps solar at a figure "
            "derived from the other three sensors, so on a skewed sample "
            "export consumes the whole cap and solar_to_house is trimmed to 0 "
            "(args=(1.0, -2.0, -1.0, 2.0) leaves the house at 0.44 of 2.0). "
            "Both understate, which is the dangerous direction and exactly why "
            "neither is the default. Kept for comparison, per their docstrings."
        )
    # At-least-filled, per the House ruling: overfill is legal, shortfall is not.
    deliverable = min(args[3], _ac_supply(args[0], args[1], args[2]))
    assert _to_house(f) >= deliverable - _slack(deliverable)


@pytestmark_treatments
def test_unknown_treatment_is_rejected():
    with pytest.raises(ValueError):
        decompose_mod.set_loss_treatment("no_such_treatment")


@pytestmark_treatments
def test_default_treatment_is_restored_after_the_fixture():
    assert decompose_mod.LOSS_TREATMENT in TREATMENTS


# ==========================================================================
# 13. Regressions introduced by the 2026-08-30 16:26 rewrite of decompose.py.
#
# These are NOT marked xfail. They are not a deliberate trade like
# SOLAR_OVERSHOOT or STARVED_SINK - they are the grid allocator handing out
# watts the meter never measured, and filling a sink that has no demand. The
# offending lines:
#
#     s2b = min(charge, r["solar"])
#     g2b = max(0.0, charge - s2b)     # not bounded by imp
#     g2h = max(0.0, imp - g2b)        # not bounded by house
#
# g2b is sized from the battery alone, so the grid supplies charge current
# even when the meter reads zero or is exporting. g2h then takes the whole
# remaining import regardless of what the house is drawing.
#
# The comment above them explains the intent - every watt of a hard AC meter
# reading must land somewhere, because sizing g2h from house_load leaked
# 6.3-9.8% of daily import. That intent is sound. Spending the leftover into
# a sink that is not asking for it is not: leave it as an unspent grid
# remainder, which CLAUDE.md already records as expected and not an error.
# ==========================================================================

GRID_REGRESSIONS = [
    (
        (0.0, 2.0, 0.0, 0.0),
        "grid_to_battery=2 W with the meter reading exactly 0 W import",
    ),
    (
        (0.0, 2.0, -1.0, 0.0),
        "grid_to_battery=2 W while the meter reads 1 W EXPORT",
    ),
]

HOUSE_REGRESSIONS = [
    ((0.0, 0.0, 2.0, 0.0), "grid_to_house=2 W into a house drawing 0 W"),
    (
        (6500.0, -5000.0, 10000.0, 0.0),
        "grid_to_house=10 kW into a house drawing 0 W",
    ),
]


@pytest.mark.parametrize("args,why", GRID_REGRESSIONS)
def test_regression_grid_never_supplies_more_than_it_imports(args, why):
    f = decompose(*args)
    assert _le(_from_grid(f), max(0.0, args[2])), why


@pytest.mark.parametrize("args,why", HOUSE_REGRESSIONS)
def test_regression_house_inflow_stays_within_supply(args, why):
    """WITHDRAWN AND REPLACED. This asserted `to_house <= house`, which the
    team lead has since overruled with three days of measured evidence: every
    metered import watt must land somewhere, and capping House against
    house_load leaked 6.3-9.8% of daily import. The supply-side bound is what
    survives, and it still catches genuine fabrication."""
    solar, battery, grid, house = args
    f = decompose(*args)
    assert _to_house(f) <= _total_supply(solar, battery, grid) + ABS_TOL, why


@pytest.mark.parametrize("args,why", GRID_REGRESSIONS + HOUSE_REGRESSIONS)
def test_regression_flows_never_exceed_measured_supply(args, why):
    solar, battery, grid, house = args
    f = decompose(*args)
    supply = max(0.0, solar) * SOLAR_CAP_FACTOR + max(0.0, grid) + max(0.0, -battery)
    assert _le(sum(f.values()), supply), why


@pytest.mark.xfail(
    strict=False,
    reason=(
        "XFAIL: pins the affine break described above. decompose.py refers "
        "battery discharge to AC as 0.9974*DC - 105.9 W, floored at "
        "LOW_BAND_ETA*DC. A fixed parasitic offset is not positively "
        "homogeneous, so scaling a discharging sample does not scale "
        "battery_to_house. Correct physics, deliberately chosen: the "
        "housekeeping draw does not double when the power doubles."
    ),
)
@pytest.mark.parametrize("k", [0.5, 2.0])
def test_ac_referral_breaks_homogeneity_on_discharge(k):
    base = decompose(0.0, -1000.0, 1000.0, 1000.0)["battery_to_house"]
    scaled = decompose(0.0, -1000.0 * k, 1000.0 * k, 1000.0 * k)["battery_to_house"]
    assert scaled == pytest.approx(k * base, rel=1e-9)


def test_ac_referral_never_credits_more_than_the_dc_discharge():
    """The bound that survives: AC-referred output is always BELOW the DC
    reading, so the Battery-out bar is short, never long. Per the owner's
    2026-08-30 decision that shortfall is the DC/AC gap made visible and must
    not be 'fixed' - so this asserts only the direction, never exactness."""
    for dc in (50.0, 100.0, 457.0, 1000.0, 5000.0):
        f = decompose(0.0, -dc, 0.0, 10000.0)
        assert f["battery_to_house"] <= dc + ABS_TOL
