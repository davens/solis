"""Edge cases and bad data for flows.decompose() -- the v2 twelve-flow law.

These sensors will run as Home Assistant template sensors reading five live
entities off the solis_solarman integration plus the Tesla charger template. A
template sensor is handed whatever the source entity's state string happens to
be, so the input domain of decompose() is "anything HA can put in a Jinja
variable", not "five floats".

Chosen contract for bad input, asserted throughout this file:

    A non-numeric state MUST raise (ValueError or TypeError). It must NOT be
    coerced to zero.

Justification. A template sensor that throws renders `unavailable`; a template
sensor that returns 0 renders a confident, wrong number. The dominant failure
mode on this system is logger session contention -- CLAUDE.md records that a
competing Modbus session fails with a bare `_queue.Empty`, at which point the
integration's entities go `unavailable`. If decompose() zeroed those, the
Sankey would draw a house with no supply and a battery doing nothing, which is
indistinguishable from 03:00 on a dead-calm night. `unavailable` propagates the
outage honestly and the card simply drops the node. Silence is safer than a
plausible lie, so raising is correct.

`float("nan")` is the subtle one, because it PARSES. It therefore slips past any
"did it parse" guard and is then silently turned into 0.0 by `max(0.0, nan)`.
flows.py carries an explicit isnan guard for exactly this; several tests below
exist only to keep it there.

WHAT CHANGED FROM v1, and why these tests were rewritten rather than tweaked
---------------------------------------------------------------------------
v1's decompose() was sequential-greedy with a late DC->AC referral, so the
allocation depended on the order sources were paid and solar absorbed a fitted
residual. Its edge-case expectations encoded that order.

v2 is proportional (BRIEF_V2.md section 2): export is solar-only, then every
remaining sink draws the same source mix. Three consequences drive the rewrite:

  1. There is no ETA_FLOOR and no fitted efficiency, so v1's `solar_ceiling()`
     helper -- solar / ETA_FLOOR -- has no v2 meaning. The replacement bound is
     tighter and physical: on a self-consistent sample solar spends EXACTLY its
     reading. See test_a_consistent_sample_spends_each_source_exactly.
  2. There is a fifth input (tesla) and a fifth sink (Inverter), so the flow
     count is twelve, not six.
  3. Energy arriving with nowhere to go now lands on the named Inverter node
     instead of vanishing. decompose(0, 0, 2000, 0, 0) sends 2 kW to Inverter.

Nothing here touches the network, HA, or any file.
"""
import itertools
import math
import random

import pytest

from flows import (
    FLOWS,
    MAX_PLAUSIBLE_W,
    BadReading,
    decompose,
    inverter_loss,
    readings,
)


# ---------------------------------------------------------------------------
# Constants from CLAUDE.md. Named so a failure reads as physics, not magic.
# ---------------------------------------------------------------------------
INVERTER_W = 6000.0          # inverter AC ceiling
BATTERY_MAX_W = 5000.0       # owner-stated battery charge/discharge ceiling
CHARGE_LIMIT_W = 50 * 53.0   # 43141 = 50 A at ~53 V = 2650 W
ARRAY_CEILING_W = 6500.0     # 20 x 400 W nameplate, never seen above ~6 kW
TESLA_SLOT_W = 7000.0        # the car takes discrete ~7 kW dispatch slots
U32_MISREAD = 4294967253.0   # 2**32 - 43: the real observed unsigned misread
U32_TRUE_VALUE = -43.0       # what 4294967253 actually means as s32

# Every non-numeric state HA can realistically hand a template.
HA_BAD_STATES = ["unavailable", "unknown", "", "None", "   ", "\n"]

# Position names, so a parametrised failure says which register broke.
ARGS = ("solar", "battery", "grid", "house", "tesla")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def flows(solar, battery, grid, house, tesla=0.0):
    """decompose() with the contract's own key check applied."""
    out = decompose(solar, battery, grid, house, tesla)
    assert set(out) == set(FLOWS), f"unexpected keys: {sorted(out)}"
    return out


def assert_non_negative(out):
    for k, v in out.items():
        assert v >= 0.0, f"{k} = {v!r} is negative"
        assert not math.isnan(v), f"{k} is NaN"


def spent(out, source):
    """Everything one source is credited with delivering."""
    return sum(v for k, v in out.items() if k.startswith(source + "_to_"))


def filled(out, sink):
    """Everything one sink is credited with receiving."""
    return sum(v for k, v in out.items() if k.endswith("_to_" + sink))


def solar_out(out):
    return spent(out, "solar")


def grid_out(out):
    return spent(out, "grid")


def battery_out(out):
    return spent(out, "battery")


def battery_in(out):
    return filled(out, "battery")


def house_in(out):
    return filled(out, "house")


def tesla_in(out):
    return filled(out, "tesla")


def inverter_in(out):
    return filled(out, "inverter")


def total_spent_law(r):
    """What the twelve flows MUST sum to, from the readings alone.

    Derived from BRIEF_V2.md section 2 and verified against flows.py over
    300 000 random samples (worst absolute error 5.5e-12 W). Three mechanisms,
    one formula:

        s2e = min(S, E)                 export is solar-only and capped at solar
        L   = max(0, supply - drawn)    the Inverter node, clamped
        total = s2e + (Hr + T + C + L)  unless S1 + B + G == 0, when the
                                        division guard zeroes all four sinks and
                                        only the export survives

    This is the honest statement of how much energy v2 credits, and it is what
    the exactness tests below are written against. Reproducing the law here
    independently is the point: a test that called decompose() twice would
    prove nothing.
    """
    supply = r["solar"] + r["discharge"] + r["imp"]
    drawn = r["house_rest"] + r["tesla"] + r["charge"] + r["exp"]
    loss = max(0.0, supply - drawn)
    s2e = min(r["solar"], r["exp"])
    if (r["solar"] - s2e) + r["discharge"] + r["imp"] <= 0.0:
        return s2e
    return s2e + r["house_rest"] + r["tesla"] + r["charge"] + loss


def spends_sources_exactly(solar, battery, grid, house, tesla=0.0):
    """True when every source's outbound flows sum to exactly its own reading.

    TWO conditions, and getting this wrong is easy -- an earlier draft of this
    file asserted exactness on the first alone and produced six false failures
    against a correct flows.py:

      supply >= drawn   the L >= 0 clamp is idle. When it binds, the shares
                        distribute more than exists and sources are OVER-spent.
      E <= S            export is solar-only, so metered export beyond the solar
                        reading has no source to charge it to and the remaining
                        sources are UNDER-spent by the difference. Physically
                        this is the battery exporting, which CLAUDE.md says
                        cannot happen (all three discharge windows unset) -- and
                        on real history it costs only 0.020-0.049 kWh/day. On
                        uniform random inputs it is common, which is why the
                        predicate has to say so.
    """
    r = readings(solar, battery, grid, house, tesla)
    supply = r["solar"] + r["discharge"] + r["imp"]
    drawn = r["house_rest"] + r["tesla"] + r["charge"] + r["exp"]
    return supply >= drawn and r["exp"] <= r["solar"]


def supply_exceeds_draw(solar, battery, grid, house, tesla=0.0):
    """True when the Inverter clamp is idle, i.e. L is the real residual."""
    r = readings(solar, battery, grid, house, tesla)
    supply = r["solar"] + r["discharge"] + r["imp"]
    drawn = r["house_rest"] + r["tesla"] + r["charge"] + r["exp"]
    return supply >= drawn


def bad_in_each_position(value):
    """Yield the five argument tuples with `value` in one slot, sane elsewhere."""
    sane = [1000.0, -500.0, 200.0, 1700.0, 0.0]
    for i in range(5):
        args = list(sane)
        args[i] = value
        yield i, tuple(args)


# ===========================================================================
# 1. Home Assistant non-numeric states
# ===========================================================================
@pytest.mark.parametrize("state", HA_BAD_STATES)
def test_ha_bad_state_raises_in_every_position(state):
    """A template sensor going unavailable must not become a silent zero."""
    for i, args in bad_in_each_position(state):
        with pytest.raises((ValueError, TypeError)):
            decompose(*args)


def test_none_raises_in_every_position():
    """A missing entity yields None through the template pipeline, not a string."""
    for i, args in bad_in_each_position(None):
        with pytest.raises((ValueError, TypeError)):
            decompose(*args)


def test_all_five_unavailable_raises():
    """Logger contention (_queue.Empty) takes every entity down at once."""
    with pytest.raises((ValueError, TypeError)):
        decompose(*(["unavailable"] * 5))


def test_container_input_raises_type_error():
    """Defensive: a mis-wired template could pass a list/dict, never a number."""
    for bad in ({}, [], (), {"state": 1}, object()):
        with pytest.raises((TypeError, ValueError)):
            decompose(bad, 0, 0, 0, 0)


def test_numeric_strings_are_accepted():
    """HA states are ALWAYS strings. This is the normal path, not an edge case.

    Asserts the PARSE, not the allocation. Both the v1 and the v2 versions of
    this test have been bitten by asserting an allocation here: v1 pinned
    solar_to_house == 2982 (solar-first), then 1063/200 (constrained-first), and
    both encoded a law rather than the parse. Under v2's proportional law the
    same four readings give solar 2841.5 / battery 1012.9 / grid 190.6 into the
    house with 200 W of residual on the Inverter node, and that would be a third
    number to rewrite the next time the law moves.

    So this test asserts exactly one thing: the string form and the float form
    must decompose identically. The law is tested elsewhere, on purpose.
    """
    out = flows("2982", "-1063", "200", "4045", "0")
    assert_non_negative(out)
    assert out == flows(2982.0, -1063.0, 200.0, 4045.0, 0.0)


def test_numeric_strings_with_decimal_point():
    """Integration sensors publish '2982.0', not '2982'. Both must parse alike."""
    a = flows("2982.0", "-1063.0", "200.0", "4045.0", "0.0")
    b = flows(2982.0, -1063.0, 200.0, 4045.0, 0.0)
    assert a == b


def test_numeric_string_with_surrounding_whitespace():
    """float() tolerates it; assert we do not accidentally tighten that."""
    assert flows(" 3000 ", "0", "0", " 3000 ", " 0 ") == flows(3000.0, 0.0, 0.0, 3000.0, 0.0)


def test_scientific_notation_string():
    assert flows("3e3", "0", "0", "3e3", "0") == flows(3000.0, 0.0, 0.0, 3000.0, 0.0)


@pytest.mark.parametrize("position", range(5))
def test_bool_is_rejected_in_every_position(position):
    """bool is an int subclass, so an unguarded float(True) is a silent 1 W.

    v1 flip-flopped on whether to reject; flows.py settles it -- a bool is a
    state, not a measurement, and HA never hands a template a bare bool. The
    real bug risk is a guard applied to SOME positions and not others, which
    would let a bool inject 1 W through whichever register was missed. This is
    parametrised over all five for exactly that reason.
    """
    args = [0.0] * 5
    args[position] = True
    with pytest.raises((ValueError, TypeError)):
        decompose(*args)
    args[position] = False
    with pytest.raises((ValueError, TypeError)):
        decompose(*args)


def test_nan_string_must_not_become_silent_zero():
    """'nan' is the one bad state that survives a parse check.

    float("nan") parses and max(0.0, nan) returns 0.0, so without an explicit
    isnan guard a broken sensor decomposes byte-identically to an idle one.
    """
    with pytest.raises((ValueError, TypeError)):
        decompose("nan", 0.0, 0.0, 0.0, 0.0)


def test_float_nan_must_not_become_silent_zero():
    """Same guard, reached through a float rather than a string."""
    with pytest.raises((ValueError, TypeError)):
        decompose(float("nan"), 0.0, 0.0, 0.0, 0.0)


def test_bad_reading_is_both_a_value_error_and_a_type_error():
    """None is the wrong TYPE, "unavailable" is the wrong VALUE.

    A caller catching either alone must still see both, or half the bad-input
    surface escapes a correct-looking except clause.
    """
    assert issubclass(BadReading, ValueError)
    assert issubclass(BadReading, TypeError)
    for bad in ("unavailable", None, float("nan"), [], MAX_PLAUSIBLE_W * 2):
        with pytest.raises(ValueError):
            decompose(bad, 0.0, 0.0, 0.0, 0.0)
        with pytest.raises(TypeError):
            decompose(bad, 0.0, 0.0, 0.0, 0.0)


# ===========================================================================
# 2. Numeric pathologies
# ===========================================================================
@pytest.mark.parametrize("position", range(5))
def test_nan_in_any_position_is_not_silently_zeroed(position):
    """NaN in ANY position must not decompose like 0.0 in that position -- a
    broken sensor must not be indistinguishable from an idle one."""
    args = [3000.0, -1000.0, 500.0, 4500.0, 0.0]
    args[position] = float("nan")
    with pytest.raises((ValueError, TypeError)):
        decompose(*args)


@pytest.mark.parametrize(
    "args",
    [
        (float("inf"), 1000.0, -2000.0, 3000.0, 0.0),
        (0.0, 2650.0, float("inf"), 400.0, 0.0),
        (5000.0, 0.0, float("-inf"), 1000.0, 0.0),
        (3000.0, -1000.0, 500.0, float("inf"), 0.0),
        (0.0, float("-inf"), 0.0, 0.0, 0.0),
        (0.0, 0.0, 5000.0, 5000.0, float("inf")),
    ],
)
def test_infinity_is_rejected_in_every_position(args):
    """An infinite reading is not a measurement, so it raises like any other
    non-reading. Rejecting is stricter than absorbing it into a min/max, and is
    consistent with the raise-on-no-reading contract.
    """
    with pytest.raises((ValueError, TypeError)):
        decompose(*args)


def test_u32_misread_is_rejected_as_implausible():
    """CLAUDE.md: treating 33257 as unsigned gave 4294967253 W = -43 W."""
    with pytest.raises((ValueError, TypeError)):
        decompose(0.0, 0.0, U32_MISREAD, 500.0, 0.0)


@pytest.mark.parametrize("position", range(5))
def test_u32_misread_is_rejected_in_every_position(position):
    """The misread can land on any of the five registers, not just the meter."""
    args = [0.0, 0.0, 0.0, 500.0, 0.0]
    args[position] = U32_MISREAD
    with pytest.raises((ValueError, TypeError)):
        decompose(*args)


def test_plausibility_ceiling_rejects_rather_than_clamps():
    """Clamping 4.29 GW to 100 kW would turn a misread into a plausible chart.

    Just under the ceiling must decompose; just over must raise. If a later
    change swaps the raise for a clamp, the second half of this fires.
    """
    ok = MAX_PLAUSIBLE_W - 1.0
    out = flows(0.0, 0.0, ok, ok)
    assert out["grid_to_house"] == pytest.approx(ok)
    with pytest.raises((ValueError, TypeError)):
        decompose(0.0, 0.0, MAX_PLAUSIBLE_W + 1.0, 500.0, 0.0)


def test_plausibility_ceiling_applies_to_negative_magnitudes_too():
    """A misread export is as impossible as a misread import."""
    with pytest.raises((ValueError, TypeError)):
        decompose(0.0, 0.0, -(MAX_PLAUSIBLE_W + 1.0), 500.0, 0.0)


def test_u32_correctly_decoded_is_a_tiny_export():
    """4294967253 read as s32 is -43 W: 43 W of export, and nothing else."""
    out = flows(600.0, 0.0, U32_TRUE_VALUE, 557.0)
    assert_non_negative(out)
    assert out["solar_to_export"] == pytest.approx(43.0)
    assert out["solar_to_house"] == pytest.approx(557.0)
    assert grid_out(out) == 0.0


def test_u32_misread_and_true_value_must_not_agree():
    """A guard that makes these identical would hide the bug, not fix it.

    The misread raises while the true value decomposes, so they cannot agree by
    construction. Kept because the failure it guards against -- a future clamp
    making 4294967253 and -43 render the same chart -- is exactly the
    plausible-looking wrong answer this suite exists to prevent.
    """
    with pytest.raises((ValueError, TypeError)):
        decompose(600.0, 0.0, U32_MISREAD, 557.0, 0.0)
    true = flows(600.0, 0.0, U32_TRUE_VALUE, 557.0)
    assert true["solar_to_export"] == pytest.approx(43.0)


def test_very_small_floats_do_not_underflow_to_negative():
    out = flows(1e-300, 1e-300, -1e-300, 1e-300)
    assert_non_negative(out)


@pytest.mark.parametrize("supply", [1e-9, 1e-12, 5e-300, 5e-324])
def test_the_division_guard_is_an_exact_zero_test_not_an_epsilon(supply):
    """flows.py's `_normalise` uses `if total <= 0.0`, not `<= some epsilon`,
    and its docstring says why: v1 trimmed with `over > _EPS` and leaked a
    nanowatt through the hole that left.

    Nothing was defending that choice. Found by mutation: changing the guard to
    `total <= 1e-9` left the entire suite green, so the documented intent was
    resting on a comment. An epsilon guard silently discards every sink on any
    sample whose total supply falls under it -- at 1e-9 W that is only ever
    noise, but the same edit at 1e-3 or 1.0 would quietly blank real overnight
    trickle samples, and nothing would say so.

    So: a supply that is positive, however small, must still be distributed.
    """
    out = flows(supply, 0.0, 0.0, supply)
    assert out["solar_to_house"] == pytest.approx(supply, rel=1e-12)
    assert out["solar_to_house"] > 0.0
    assert_non_negative(out)


def test_a_positive_supply_below_one_nanowatt_still_reaches_its_sink():
    """The mutation-killing case stated in the units the guard is written in."""
    out = flows(1e-10, 0.0, 0.0, 1e-10)
    assert out["solar_to_house"] == pytest.approx(1e-10, rel=1e-12)
    out = flows(0.0, 0.0, 1e-10, 1e-10)
    assert out["grid_to_house"] == pytest.approx(1e-10, rel=1e-12)
    out = flows(0.0, -1e-10, 0.0, 1e-10)
    assert out["battery_to_house"] == pytest.approx(1e-10, rel=1e-12)


def test_denormal_scale_conserves_solar():
    out = flows(5e-324, 0.0, 0.0, 0.0)
    assert_non_negative(out)
    assert solar_out(out) <= 5e-324


def test_negative_zero_battery_is_neither_charge_nor_discharge():
    """-0.0 must not read as 'discharging by 0', which could seed a phantom link."""
    out = flows(1000.0, -0.0, 0.0, 1000.0)
    assert battery_out(out) == 0.0
    assert battery_in(out) == 0.0


def test_negative_zero_grid_is_neither_import_nor_export():
    out = flows(1000.0, 0.0, -0.0, 1000.0)
    assert out["solar_to_export"] == 0.0
    assert grid_out(out) == 0.0


def test_negative_zero_string_from_template():
    assert flows("1000", "-0.0", "-0.0", "1000", "-0.0") == flows(1000.0, 0.0, 0.0, 1000.0, 0.0)


def test_large_but_physically_plausible_values():
    """Clear-sky midday: 5834 W DC, 5800 W AC, 875 W house (CLAUDE.md).

    The 34 W that neither leaves as export nor reaches the house is the Inverter
    node doing its job -- under v1 this was the unnamed residual smeared onto
    solar.
    """
    out = flows(5834.0, 0.0, -4925.0, 875.0)
    assert_non_negative(out)
    assert out["solar_to_house"] == pytest.approx(875.0)
    assert out["solar_to_export"] == pytest.approx(4925.0)
    assert out["solar_to_inverter"] == pytest.approx(34.0)
    assert solar_out(out) == pytest.approx(5834.0)


def test_all_arguments_zero_gives_all_zero_flows():
    out = flows(0.0, 0.0, 0.0, 0.0)
    assert out == {k: 0.0 for k in FLOWS}


# ===========================================================================
# 3. Sign-convention inversions
#
# CLAUDE.md flags this as "exactly the shape of thing an agent helpfully flips
# in the wrong direction". Each test below is written so that flipping ONE
# convention makes it fail loudly rather than shift a number quietly.
# ===========================================================================
def test_night_import_never_appears_as_export():
    """23:00, no sun, 5 kW import. Export must be structurally impossible."""
    out = flows(0.0, 0.0, 5000.0, 5000.0)
    assert out["grid_to_house"] == pytest.approx(5000.0)
    assert out["solar_to_export"] == 0.0


def test_daytime_export_never_appears_as_import():
    out = flows(6000.0, 0.0, -5000.0, 1000.0)
    assert out["solar_to_export"] == pytest.approx(5000.0)
    assert grid_out(out) == 0.0


def test_flipping_grid_sign_changes_the_answer():
    """Detector: if grid sign were inverted these two would coincide."""
    importing = flows(0.0, 0.0, 5000.0, 5000.0)
    exporting = flows(6000.0, 0.0, -5000.0, 1000.0)
    assert importing["grid_to_house"] > 0 and exporting["grid_to_house"] == 0
    assert exporting["solar_to_export"] > 0 and importing["solar_to_export"] == 0


def test_grid_sign_flip_would_be_caught_by_symmetry():
    """+5000 and -5000 with identical house/solar must not decompose alike.

    House deliberately exceeds solar so the import has somewhere to go. With
    house == solar BOTH signs decompose to all-zero grid flows -- true, since
    neither an import nor an export has an attributable destination, but it
    makes a useless sign detector. Keep the deficit.
    """
    a = flows(3000.0, 0.0, 5000.0, 8000.0)
    b = flows(3000.0, 0.0, -5000.0, 8000.0)
    assert a != b
    assert grid_out(a) > 0
    assert grid_out(b) == 0


def test_export_out_reading_solar_starves_every_sink_not_just_export():
    """The E > S branch, asserted rather than left as a surprise.

    Continuing the case above: exporting 5 kW while producing 3 kW is not
    physically possible, but sensor skew produces it. `s2e = min(S, E)` caps
    export at the solar reading, which leaves S1 == 0; with no battery and no
    import the shares are all zero and EVERY sink -- including an 8 kW house --
    draws nothing at all.

    That is the honest answer (there is no measured source to attribute), but it
    is a cliff rather than a slope, and it is worth knowing it is here. Measured
    cost on the four replay days: 149-621 s/day, 0.020-0.049 kWh/day of export
    with no source behind it.
    """
    out = flows(3000.0, 0.0, -5000.0, 8000.0)
    assert out["solar_to_export"] == pytest.approx(3000.0)
    assert house_in(out) == 0.0
    assert inverter_in(out) == 0.0
    assert solar_out(out) == pytest.approx(3000.0)


def test_discharging_battery_never_appears_as_charging():
    """Contract: battery < 0 is discharging. -2 kW must feed the house."""
    out = flows(0.0, -2000.0, 0.0, 2000.0)
    assert out["battery_to_house"] == pytest.approx(2000.0)
    assert battery_in(out) == 0.0


def test_charging_battery_never_appears_as_discharging():
    out = flows(4000.0, 2000.0, 0.0, 2000.0)
    assert out["solar_to_battery"] == pytest.approx(2000.0)
    assert battery_out(out) == 0.0


def test_flipping_battery_sign_changes_the_answer():
    charging = flows(4000.0, 2000.0, 0.0, 2000.0)
    discharging = flows(0.0, -2000.0, 0.0, 2000.0)
    assert charging != discharging
    assert battery_in(charging) > 0 and battery_in(discharging) == 0
    assert battery_out(discharging) > 0 and battery_out(charging) == 0


def test_battery_direction_flag_semantics_33135():
    """33135 is a DIRECTION FLAG, not current. Magnitude comes from 33149/33150.

    Simulating the integration's own conversion: unsigned magnitude + flag.
    Flag 0 = charging (positive here), flag 1 = discharging (negative here).
    """
    magnitude = 1063.0
    charging = flows(3000.0, +magnitude, 0.0, 1937.0)
    discharging = flows(0.0, -magnitude, 0.0, 1063.0)
    assert charging["solar_to_battery"] == pytest.approx(magnitude)
    assert discharging["battery_to_house"] == pytest.approx(magnitude)


def test_battery_cannot_charge_and_discharge_in_the_same_sample():
    """One signed sensor forbids it, which is why there is no battery_to_battery
    key. BRIEF_V2.md leans on this: it is what stops the proportional rule from
    crediting a discharging pack with charging itself."""
    assert "battery_to_battery" not in FLOWS
    for battery in (-5000.0, -1.0, 0.0, 1.0, 5000.0):
        out = flows(1000.0, battery, 1000.0, 1500.0)
        assert not (battery_in(out) > 0 and battery_out(out) > 0)


def test_overnight_timed_charge_window_flow_direction():
    """23:30-05:30: grid charges the battery AND carries the house baseline."""
    out = flows(0.0, CHARGE_LIMIT_W, CHARGE_LIMIT_W + 400.0, 400.0)
    assert out["grid_to_battery"] == pytest.approx(CHARGE_LIMIT_W)
    assert out["grid_to_house"] == pytest.approx(400.0)
    assert battery_out(out) == 0.0
    assert out["solar_to_export"] == 0.0


def test_no_battery_to_grid_flow_exists_at_all():
    """All three discharge windows are unset and must stay unset (CLAUDE.md).

    Merely declaring such a link fabricated battery export on the real chart.
    """
    assert "battery_to_export" not in FLOWS
    assert "battery_to_grid" not in FLOWS
    assert not any(f.startswith("battery_to_") and "export" in f for f in FLOWS)
    out = flows(0.0, -5000.0, -4000.0, 1000.0)
    assert set(out) == set(FLOWS)


def test_no_grid_to_export_flow_exists_at_all():
    """Import and export never coexist on one signed meter reading, so a
    grid->export link would be pure fiction -- energy shown passing through the
    meter and back out."""
    assert "grid_to_export" not in FLOWS
    assert [f for f in FLOWS if f.endswith("_to_export")] == ["solar_to_export"]


def test_solar_and_grid_share_the_battery_proportionally():
    """v2 REPLACES v1's constrained-source-first law here, and the change is
    visible rather than subtle, so it gets an explicit test.

    v1 asserted solar_to_battery == 2000 and grid_to_battery == 0 on these
    readings, under the editorial rule that free solar must never be displaced
    by import. v2 is order-free: with 3 kW of solar and 1 kW of import feeding a
    2 kW charge and a 2 kW house, both sinks draw the same 75/25 mix.

    This is a deliberate weakening of CLAUDE.md's "seeing solar reach the
    battery is the whole point of the chart" -- solar still visibly reaches the
    battery, but it no longer monopolises it. Asserted here so the trade is
    recorded in an executable place, not only in prose.
    """
    out = flows(3000.0, 2000.0, 1000.0, 2000.0)
    assert out["solar_to_battery"] == pytest.approx(1500.0)
    assert out["grid_to_battery"] == pytest.approx(500.0)
    assert out["solar_to_house"] == pytest.approx(1500.0)
    assert out["grid_to_house"] == pytest.approx(500.0)
    # The mix is identical for both sinks -- that IS the proportional law.
    assert (out["solar_to_battery"] / battery_in(out)
            == pytest.approx(out["solar_to_house"] / house_in(out)))


# ===========================================================================
# 4. The Tesla input -- new in v2
# ===========================================================================
def test_tesla_is_clamped_to_the_house_load():
    """BRIEF_V2.md section 5: clamp T <= house_load, always.

    tesla_home_charging_power is a TeslaMate on-change value and house_load is
    polled every 10 s, so on the sample where a charge stops they disagree for
    one interval. Without the clamp house_rest would go negative.
    """
    out = flows(0.0, 0.0, 5000.0, 3000.0, 9999.0)
    assert out["grid_to_tesla"] == pytest.approx(3000.0)
    assert house_in(out) == 0.0
    assert tesla_in(out) == pytest.approx(3000.0)


def test_tesla_equal_to_house_leaves_no_rest_of_house():
    out = flows(0.0, 0.0, 3000.0, 3000.0, 3000.0)
    assert tesla_in(out) == pytest.approx(3000.0)
    assert house_in(out) == 0.0


def test_tesla_zero_reproduces_the_four_input_behaviour():
    """The car unplugged must not perturb anything. Every Tesla flow is zero and
    the remaining sinks are unchanged."""
    out = flows(3000.0, -1000.0, 500.0, 2500.0, 0.0)
    assert tesla_in(out) == 0.0
    for key in FLOWS:
        if key.endswith("_to_tesla"):
            assert out[key] == 0.0


def test_tesla_held_flat_across_a_plateau_is_not_stale():
    """CLAUDE.md: one 32 A plateau went 80 minutes without a TeslaMate update.

    A held value is CORRECT, not stale, so decompose() must be a pure function
    of the value it is handed and must not discount a repeated reading. Ten
    identical samples must decompose ten identical ways.
    """
    args = (0.0, 0.0, TESLA_SLOT_W + 600.0, TESLA_SLOT_W + 600.0, TESLA_SLOT_W)
    first = flows(*args)
    for _ in range(10):
        assert flows(*args) == first
    assert first["grid_to_tesla"] == pytest.approx(TESLA_SLOT_W)
    assert first["grid_to_house"] == pytest.approx(600.0)


@pytest.mark.parametrize("bad", HA_BAD_STATES + [None, float("nan"), float("inf")])
def test_tesla_unavailable_raises_rather_than_assuming_zero(bad):
    """The car's sensor going unavailable must not silently reassign its whole
    load to the house. That would be a confident lie of up to 7 kW -- the single
    largest misattribution available on this system."""
    with pytest.raises((ValueError, TypeError)):
        decompose(0.0, 0.0, 7600.0, 7600.0, bad)


def test_negative_tesla_is_clamped_not_rejected():
    """A template can briefly publish a small negative from a V*I product. It is
    a measurement, not a non-reading, so it clamps to zero rather than raising."""
    out = flows(0.0, 0.0, 1000.0, 1000.0, -50.0)
    assert tesla_in(out) == 0.0
    assert out["grid_to_house"] == pytest.approx(1000.0)


def test_a_seven_kilowatt_tesla_slot_is_not_confused_with_the_house_battery():
    """CLAUDE.md: the car takes discrete ~7 kW slots; the house pack charges at
    about 2.5 kW at 50 A. Never attribute a 7 kW draw to the battery."""
    out = flows(0.0, CHARGE_LIMIT_W, TESLA_SLOT_W + CHARGE_LIMIT_W + 300.0,
                TESLA_SLOT_W + 300.0, TESLA_SLOT_W)
    assert out["grid_to_tesla"] == pytest.approx(TESLA_SLOT_W)
    assert out["grid_to_battery"] == pytest.approx(CHARGE_LIMIT_W)
    assert out["grid_to_house"] == pytest.approx(300.0)


# ===========================================================================
# 5. The Inverter node -- new in v2
# ===========================================================================
def test_house_zero_while_grid_imports_heavily():
    """5 kW arriving with nowhere else to go is now NAMED, not discarded.

    Under v1 this decomposed to all zeros: the import simply vanished and the
    grid box drew a shorter bar. BRIEF_V2.md section 2 calls this case out by
    name -- decompose(0, 0, 2, 0) sends 2 kW to Inverter, not into a house
    drawing nothing -- and it is the clearest single demonstration of what the
    Inverter node buys.
    """
    out = flows(0.0, 0.0, 5000.0, 0.0)
    assert_non_negative(out)
    assert out["grid_to_inverter"] == pytest.approx(5000.0)
    assert house_in(out) == 0.0
    assert inverter_in(out) == pytest.approx(5000.0)
    assert grid_out(out) == pytest.approx(5000.0)


def test_the_briefs_own_worked_example():
    """BRIEF_V2.md section 2, verbatim: decompose(0,0,2,0) -> 2 kW to Inverter."""
    out = flows(0.0, 0.0, 2000.0, 0.0)
    assert out["grid_to_inverter"] == pytest.approx(2000.0)
    assert sum(out.values()) == pytest.approx(2000.0)


def test_house_zero_while_solar_produces_and_nothing_exports():
    """Solar with nowhere to go lands on the Inverter node, not on nothing."""
    out = flows(4000.0, 0.0, 0.0, 0.0)
    assert_non_negative(out)
    assert out["solar_to_inverter"] == pytest.approx(4000.0)
    assert solar_out(out) == pytest.approx(4000.0)


def test_house_demand_with_no_supply_at_all():
    """Sensor skew can show load with every source at zero. Invent nothing.

    This is the division guard: total supply is 0, so every share is 0 and the
    4 kW house draws from nowhere rather than from a fabricated source.
    """
    out = flows(0.0, 0.0, 0.0, 4000.0)
    assert out == {k: 0.0 for k in FLOWS}


def test_inverter_loss_is_never_negative():
    """The L >= 0 clamp. A negative residual means the sinks out-read the
    sources on that sample -- sensor skew, not energy from nowhere -- and a
    negative ribbon is not a thing the card can draw honestly."""
    for args in (
        (1000.0, 0.0, 0.0, 3000.0, 0.0),
        (0.0, 0.0, 500.0, 3000.0, 3000.0),
        (500.0, 2000.0, 0.0, 100.0, 0.0),
        (0.0, -100.0, 0.0, 9000.0, 0.0),
    ):
        r = readings(*args)
        assert inverter_loss(r) == 0.0
        assert_non_negative(flows(*args))


def test_inverter_loss_matches_the_briefs_formula_exactly():
    """L = max(0, (S + B + G) - (Hr + T + C + E)), recomputed independently."""
    rng = random.Random(20260830)
    for _ in range(500):
        args = (rng.uniform(0.0, 6000.0), rng.uniform(-5000.0, 5000.0),
                rng.uniform(-5000.0, 5000.0), rng.uniform(0.0, 9000.0),
                rng.uniform(0.0, 7000.0))
        r = readings(*args)
        want = max(0.0, (r["solar"] + r["discharge"] + r["imp"])
                   - (r["house_rest"] + r["tesla"] + r["charge"] + r["exp"]))
        assert inverter_loss(r) == pytest.approx(want, rel=1e-12, abs=1e-9)
        assert inverter_in(flows(*args)) == pytest.approx(want, rel=1e-9, abs=1e-9)


def test_the_loss_clamp_is_the_one_place_a_source_can_be_over_spent():
    """MEASURED DEFECT, pinned so it cannot silently grow.

    BRIEF_V2.md section 2 says the proportional rule "can never fabricate a
    flow". The narrow claim holds -- a source reading zero contributes zero to
    every sink -- but a NONZERO source can be over-spent, because when the
    measured sinks out-read the measured sources L pins to zero and the shares
    then distribute more than exists.

    Constructed here at 3x and 6x. On real recorded history the clamp binds
    1.4-5.3% of the time and costs at most 0.097 kWh/day, i.e. under half of the
    counters' own 0.1 kWh quantisation word, which is why the rule is not being
    changed for it. This test exists so that "small" stays checkable.
    """
    over = flows(1000.0, 0.0, 0.0, 3000.0)
    assert solar_out(over) == pytest.approx(3000.0)          # 3 kW spent, 1 kW read
    assert solar_out(over) > 1000.0
    assert not supply_exceeds_draw(1000.0, 0.0, 0.0, 3000.0)

    over = flows(0.0, 0.0, 500.0, 3000.0, 3000.0)
    assert grid_out(over) == pytest.approx(3000.0)           # 3 kW spent, 0.5 kW read
    assert grid_out(over) > 500.0


def test_a_consistent_sample_spends_each_source_exactly():
    """The replacement for v1's `solar_ceiling()`, and it is far tighter.

    v1 needed solar / ETA_FLOOR because solar absorbed a fitted residual. v2 has
    no fitted constant: whenever the sample is self-consistent, each source's
    outbound flows sum to EXACTLY its own reading. Not a bound -- an equality.
    """
    rng = random.Random(0000000000)
    checked = 0
    for _ in range(2000):
        solar = rng.uniform(0.0, 6000.0)
        battery = rng.uniform(-5000.0, 5000.0)
        grid = rng.uniform(-5000.0, 5000.0)
        house = rng.uniform(0.0, 6000.0)
        tesla = rng.uniform(0.0, house)
        if not spends_sources_exactly(solar, battery, grid, house, tesla):
            continue
        r = readings(solar, battery, grid, house, tesla)
        out = flows(solar, battery, grid, house, tesla)
        assert solar_out(out) == pytest.approx(r["solar"], rel=1e-9, abs=1e-9)
        assert battery_out(out) == pytest.approx(r["discharge"], rel=1e-9, abs=1e-9)
        assert grid_out(out) == pytest.approx(r["imp"], rel=1e-9, abs=1e-9)
        checked += 1
    assert checked > 500, "sample did not exercise the consistent branch enough"


def test_every_sink_fills_exactly_regardless_of_consistency():
    """The other half, and the one that holds unconditionally: sinks always fill.

    That asymmetry IS the v2 design. The sinks are measured, so they are filled
    to the last watt; the sources absorb whatever the measurement implies.
    """
    rng = random.Random(31415926)
    for _ in range(2000):
        solar = rng.uniform(0.0, 6000.0)
        battery = rng.uniform(-5000.0, 5000.0)
        grid = rng.uniform(-5000.0, 5000.0)
        house = rng.uniform(0.0, 9000.0)
        tesla = rng.uniform(0.0, 7000.0)
        r = readings(solar, battery, grid, house, tesla)
        out = flows(solar, battery, grid, house, tesla)
        s2e = min(r["solar"], r["exp"])
        if (r["solar"] - s2e) + r["discharge"] + r["imp"] <= 0.0:
            # The division guard: no source is left to attribute anything to, so
            # every sink draws nothing and only the capped export survives.
            assert out["solar_to_export"] == pytest.approx(s2e, rel=1e-9, abs=1e-12)
            assert sum(out.values()) == pytest.approx(s2e, rel=1e-9, abs=1e-12)
            continue
        assert house_in(out) == pytest.approx(r["house_rest"], rel=1e-9, abs=1e-9)
        assert tesla_in(out) == pytest.approx(r["tesla"], rel=1e-9, abs=1e-9)
        assert battery_in(out) == pytest.approx(r["charge"], rel=1e-9, abs=1e-9)
        assert out["solar_to_export"] == pytest.approx(min(r["solar"], r["exp"]),
                                                       rel=1e-9, abs=1e-9)


# ===========================================================================
# 6. Simultaneous impossibilities
# ===========================================================================
@pytest.mark.parametrize(
    "solar,battery,grid,house",
    [
        (0.0, -2000.0, 2000.0, 4000.0),
        (3000.0, 1500.0, -1000.0, 2500.0),
        (0.0, 3000.0, 3400.0, 400.0),
        (6000.0, -500.0, -3000.0, 3500.0),
    ],
)
def test_battery_never_charges_and_discharges_at_once(solar, battery, grid, house):
    """One signed sensor cannot mean both, and the flows must not imply both."""
    out = flows(solar, battery, grid, house)
    assert not (battery_in(out) > 0 and battery_out(out) > 0), (
        f"battery both filling ({battery_in(out)}) and emptying ({battery_out(out)})"
    )


@pytest.mark.parametrize(
    "solar,battery,grid,house",
    [
        (0.0, 0.0, 5000.0, 5000.0),
        (6000.0, 0.0, -5000.0, 1000.0),
        (3000.0, 2000.0, -500.0, 500.0),
    ],
)
def test_grid_never_imports_and_exports_at_once(solar, battery, grid, house):
    out = flows(solar, battery, grid, house)
    assert not (grid_out(out) > 0 and out["solar_to_export"] > 0)


def test_solar_above_array_ceiling_is_still_well_formed():
    """20 x 400 W = 8 kWp nameplate; the inverter caps near 6 kW. Absurd but
    below MAX_PLAUSIBLE_W, so it decomposes rather than raising."""
    out = flows(ARRAY_CEILING_W + 1.0, 0.0, -1000.0, 1000.0)
    assert_non_negative(out)
    assert solar_out(out) == pytest.approx(ARRAY_CEILING_W + 1.0)


def test_absurd_solar_does_not_leak_into_grid_flows():
    out = flows(20000.0, 0.0, 0.0, 1000.0)
    assert grid_out(out) == 0.0
    assert out["solar_to_house"] == pytest.approx(1000.0)
    assert out["solar_to_inverter"] == pytest.approx(19000.0)


def test_battery_charging_while_grid_exports():
    """Physically odd but observable during sensor skew; must stay consistent."""
    out = flows(6000.0, 2000.0, -3000.0, 1000.0)
    assert_non_negative(out)
    assert out["grid_to_battery"] == 0.0
    assert solar_out(out) == pytest.approx(6000.0)


def test_dc_ac_mismatch_lands_on_the_inverter_node():
    """Solar/battery are DC-side, grid/house AC-side: 2-8% loss, no exact close.

    Under v1 this residual had nowhere to go and the house sink was left short.
    Under v2 the house fills exactly and the 88 W difference is the Inverter
    node -- which is the entire point of naming it.
    """
    # 2033 W PV + 1063 W battery discharge vs 3008 W house: 88 W unaccounted.
    out = flows(2033.0, -1063.0, 0.0, 3008.0)
    assert_non_negative(out)
    assert house_in(out) == pytest.approx(3008.0)
    assert inverter_in(out) == pytest.approx(88.0)
    assert solar_out(out) == pytest.approx(2033.0)
    assert battery_out(out) == pytest.approx(1063.0)


# ===========================================================================
# 7. Boundary values
# ===========================================================================
def test_exactly_zero_everywhere():
    out = flows(0, 0, 0, 0)
    assert out == {k: 0.0 for k in FLOWS}


def test_exactly_at_inverter_ceiling():
    """Export exactly equal to solar: S1 becomes 0, the division guard fires,
    and the only non-zero flow is the export itself."""
    out = flows(INVERTER_W, 0.0, -INVERTER_W, 0.0)
    assert out["solar_to_export"] == pytest.approx(INVERTER_W)
    assert sum(out.values()) == pytest.approx(INVERTER_W)
    assert_non_negative(out)


def test_one_watt_above_inverter_ceiling():
    """No clipping logic lives in decompose(); assert it does not appear."""
    out = flows(INVERTER_W + 1.0, 0.0, -(INVERTER_W + 1.0), 0.0)
    assert out["solar_to_export"] == pytest.approx(INVERTER_W + 1.0)


def test_exactly_at_battery_5kw_limit():
    out = flows(BATTERY_MAX_W, BATTERY_MAX_W, 0.0, 0.0)
    assert out["solar_to_battery"] == pytest.approx(BATTERY_MAX_W)


def test_one_watt_above_battery_limit_is_not_clamped_here():
    out = flows(BATTERY_MAX_W + 1.0, BATTERY_MAX_W + 1.0, 0.0, 0.0)
    assert out["solar_to_battery"] == pytest.approx(BATTERY_MAX_W + 1.0)


def test_exactly_at_battery_discharge_limit():
    out = flows(0.0, -BATTERY_MAX_W, 0.0, BATTERY_MAX_W)
    assert out["battery_to_house"] == pytest.approx(BATTERY_MAX_W)


def test_exactly_at_charge_current_limit_2650w():
    """43141 = 50 A at ~53 V. The overnight window's actual operating point."""
    out = flows(0.0, CHARGE_LIMIT_W, CHARGE_LIMIT_W, 0.0)
    assert out["grid_to_battery"] == pytest.approx(CHARGE_LIMIT_W)


def test_just_below_charge_current_limit():
    v = CHARGE_LIMIT_W - 0.001
    out = flows(0.0, v, v, 0.0)
    assert out["grid_to_battery"] == pytest.approx(v)


def test_solar_exactly_equals_house():
    out = flows(3000.0, 0.0, 0.0, 3000.0)
    assert out["solar_to_house"] == pytest.approx(3000.0)
    assert out["solar_to_export"] == 0.0
    assert grid_out(out) == 0.0


def test_solar_one_watt_below_house():
    out = flows(2999.0, 0.0, 1.0, 3000.0)
    assert out["solar_to_house"] == pytest.approx(2999.0)
    assert out["grid_to_house"] == pytest.approx(1.0)


def test_solar_one_watt_above_house():
    out = flows(3001.0, 0.0, -1.0, 3000.0)
    assert out["solar_to_house"] == pytest.approx(3000.0)
    assert out["solar_to_export"] == pytest.approx(1.0)


def test_grid_exactly_covers_deficit():
    out = flows(1000.0, 0.0, 2000.0, 3000.0)
    assert out["grid_to_house"] == pytest.approx(2000.0)
    assert out["grid_to_battery"] == 0.0


def test_battery_exactly_covers_deficit():
    out = flows(1000.0, -2000.0, 0.0, 3000.0)
    assert out["battery_to_house"] == pytest.approx(2000.0)
    assert grid_out(out) == 0.0


@pytest.mark.parametrize("v", [0.0, 1e-9, 1.0, 999.0, 2650.0, 5000.0, 6000.0])
def test_sink_bounds_hold_across_the_operating_range(v):
    """Sinks are measured and fill exactly; sources absorb the inconsistency."""
    out = flows(v, v / 2.0, v / 4.0, v)
    r = readings(v, v / 2.0, v / 4.0, v, 0.0)
    assert_non_negative(out)
    assert house_in(out) == pytest.approx(r["house_rest"], rel=1e-9, abs=1e-12)
    assert battery_in(out) == pytest.approx(r["charge"], rel=1e-9, abs=1e-12)
    assert tesla_in(out) == 0.0


@pytest.mark.parametrize(
    "combo",
    list(itertools.product([0.0, 2650.0], [-5000.0, 0.0, 5000.0], [-3000.0, 3000.0])),
)
def test_source_and_sink_bounds_hold_across_a_small_grid(combo):
    """Twelve corners of the operating envelope, checked against the full law.

    Sinks always fill exactly. Sources are exact only when neither the loss
    clamp nor the export cap bites, and both cases are asserted rather than
    skipped -- the whole point of the grid is that it reaches the corners the
    random sampler visits rarely.
    """
    solar, battery, grid = combo
    house = 2000.0
    out = flows(solar, battery, grid, house)
    r = readings(solar, battery, grid, house, 0.0)
    s2e = min(r["solar"], r["exp"])
    assert_non_negative(out)
    assert out["solar_to_export"] == pytest.approx(s2e, rel=1e-9, abs=1e-9)
    assert sum(out.values()) == pytest.approx(total_spent_law(r), rel=1e-9, abs=1e-9)

    if (r["solar"] - s2e) + r["discharge"] + r["imp"] > 0.0:
        assert house_in(out) == pytest.approx(r["house_rest"], rel=1e-9, abs=1e-9)
        assert battery_in(out) == pytest.approx(r["charge"], rel=1e-9, abs=1e-9)

    if spends_sources_exactly(solar, battery, grid, house):
        assert solar_out(out) == pytest.approx(r["solar"], rel=1e-9, abs=1e-9)
        assert battery_out(out) == pytest.approx(r["discharge"], rel=1e-9, abs=1e-9)
        assert grid_out(out) == pytest.approx(r["imp"], rel=1e-9, abs=1e-9)
    if not supply_exceeds_draw(solar, battery, grid, house):
        assert inverter_in(out) == 0.0


# ===========================================================================
# 8. Midnight / counter behaviour
#
# SCOPE NOTE, stated explicitly rather than skipped silently: decompose() takes
# instantaneous WATTS and has no concept of a counter, a day, or a clock, so
# none of this can be asserted against it. What follows tests a local reference
# implementation of the total_increasing delta rule. Its value is as an
# executable specification for the downstream utility_meter / Riemann design --
# and v2 has 12 x 3 = 36 such helpers, so it matters more than it did.
# ===========================================================================
def counter_delta(prev, cur):
    """total_increasing semantics: a drop means the counter reset to 0.

    prev is None for the first observation, which is HA's first statistics row
    and by definition carries change = 0 (CLAUDE.md).
    """
    if prev is None:
        return 0.0
    if cur < prev:
        return cur
    return cur - prev


def test_first_statistics_row_carries_change_zero():
    """Whatever was already on the meter is permanently missing from day one."""
    assert counter_delta(None, 12.1) == 0.0


def test_first_row_loss_is_the_documented_air_con_case():
    """Live 389 Wh rendered 0.1 kWh: 132 Wh lost to the change = 0 first row."""
    rows = [0.132, 0.068, 0.062]
    prev = None
    total = 0.0
    for r in rows:
        total += counter_delta(prev, prev + r if prev is not None else r)
        prev = (prev + r) if prev is not None else r
    assert total == pytest.approx(0.130, abs=1e-9)


def test_midnight_reset_is_not_a_negative_delta():
    """Counters reset at ~23:59:52 local; a naive cur-prev would give -30.9."""
    assert counter_delta(30.9, 0.0) == 0.0
    assert counter_delta(30.9, 0.1) == pytest.approx(0.1)


def test_no_delta_is_ever_negative_across_a_reset_sequence():
    series = [28.0, 30.4, 30.9, 0.0, 0.1, 0.4]
    prev = None
    for cur in series:
        d = counter_delta(prev, cur)
        assert d >= 0.0, f"negative delta {d} at {cur}"
        prev = cur


def test_daily_total_survives_a_midnight_rollover():
    day1 = [0.0, 10.0, 20.0, 30.9]
    day2 = [0.0, 5.0, 12.0]
    prev = None
    total = 0.0
    for cur in day1 + day2:
        total += counter_delta(prev, cur)
        prev = cur
    # 30.9 accrued after the change = 0 first row, then 12.0 on day two.
    assert total == pytest.approx(30.9 + 12.0)


def test_bst_to_gmt_transition_shifts_the_inverter_day_by_an_hour():
    """2026-10-25: the inverter clock has no DST, so it runs an hour fast.

    Encoded as arithmetic on minutes-since-midnight so the consequence -- the
    23:30-05:30 charge window landing at 22:30-04:30 GMT -- is executable.
    """
    window_start, window_end = 23 * 60 + 30, 5 * 60 + 30
    drift = 60  # inverter clock one hour fast after BST ends
    effective_start = (window_start - drift) % (24 * 60)
    effective_end = (window_end - drift) % (24 * 60)
    assert effective_start == 22 * 60 + 30
    assert effective_end == 4 * 60 + 30
    # The first hour lands on the day rate and the last cheap hour is missed.
    assert effective_start < window_start
    assert effective_end < window_end


def test_counter_rollover_time_is_just_before_midnight_not_after():
    """Observed once: 23:59:52 local. A day boundary assumed at 00:00:00 would
    mis-assign the last eight seconds of generation."""
    rollover = 23 * 3600 + 59 * 60 + 52
    assert rollover < 24 * 3600
    assert 24 * 3600 - rollover == 8


# ===========================================================================
# 9. Float accumulation
# ===========================================================================
def test_repeated_decomposition_does_not_drift():
    """10 000 identical 10 s samples: naive summation vs exact fsum.

    Tolerance: relative error < 1e-12 per flow. Anything larger would be
    visible in a kWh total after a day of 10 s polling -- and v2 integrates
    twelve of these, not six.
    """
    n = 10_000
    args = (2982.1, -1063.3, 200.7, 4045.9, 0.0)
    per_step = flows(*args)
    naive = {k: 0.0 for k in FLOWS}
    for _ in range(n):
        for k in FLOWS:
            naive[k] += per_step[k]
    for k in FLOWS:
        exact = math.fsum([per_step[k]] * n)
        if exact == 0.0:
            assert naive[k] == 0.0
            continue
        assert abs(naive[k] - exact) / exact < 1e-12, k


def test_the_total_credited_matches_the_law_on_every_random_sample():
    """The strongest single statement this file makes about v2.

    v1 needed a soft ETA_FLOOR headroom bound here, because solar absorbed a
    fitted residual and no exact statement was available. v2 has one: the twelve
    flows sum to `total_spent_law(r)` -- derived from the brief, recomputed here
    from the readings alone -- on every input, with no tolerance band and no
    fitted constant anywhere in it.

    The four inputs are drawn uniformly and independently, which produces far
    more internally inconsistent samples than the real inverter ever does. That
    is deliberate: it drives all three mechanisms (the export cap, the loss
    clamp, the division guard) hard, and the assertion holds through all of
    them. The counters below fail the test if a run stops reaching a mechanism,
    so this cannot quietly decay into a test of the easy path.
    """
    rng = random.Random(20260830)
    clamped = capped = guarded = 0
    for _ in range(5000):
        solar = rng.uniform(0.0, 6000.0)
        house = rng.uniform(0.0, 6000.0)
        battery = rng.uniform(-5000.0, 5000.0)
        grid = rng.uniform(-5000.0, 5000.0)
        r = readings(solar, battery, grid, house, 0.0)
        out = flows(solar, battery, grid, house)
        assert sum(out.values()) == pytest.approx(
            total_spent_law(r), rel=1e-9, abs=1e-9)
        assert_non_negative(out)
        s2e = min(r["solar"], r["exp"])
        if not supply_exceeds_draw(solar, battery, grid, house):
            clamped += 1
        if r["exp"] > r["solar"]:
            capped += 1
        if (r["solar"] - s2e) + r["discharge"] + r["imp"] <= 0.0:
            guarded += 1
    assert clamped > 0, "random sample never exercised the loss clamp"
    assert capped > 0, "random sample never exercised the export cap"
    assert guarded > 0, "random sample never exercised the division guard"


def test_over_spend_happens_only_when_the_clamp_binds_and_equals_its_deficit():
    """Pins the over-spend to an exact quantity, not a tolerance.

    When export is within solar and the loss clamp binds, the three sources are
    collectively over-spent by EXACTLY (drawn - supply): the shares distribute
    the four sinks in full, and the energy the clamp refused to show as a
    negative Inverter value reappears as source over-spend. When the clamp is
    idle the over-spend is exactly zero.

    This is what makes the defect reported in
    test_the_loss_clamp_is_the_one_place_a_source_can_be_over_spent bounded
    rather than open-ended.
    """
    rng = random.Random(1787785200)
    seen_clamped = seen_idle = 0
    for _ in range(5000):
        solar = rng.uniform(0.0, 6000.0)
        house = rng.uniform(0.0, 6000.0)
        battery = rng.uniform(-5000.0, 5000.0)
        grid = rng.uniform(-5000.0, 4000.0)
        r = readings(solar, battery, grid, house, 0.0)
        if r["exp"] > r["solar"]:
            continue
        supply = r["solar"] + r["discharge"] + r["imp"]
        drawn = r["house_rest"] + r["tesla"] + r["charge"] + r["exp"]
        # The division guard cannot fire here and is asserted rather than
        # guarded against: E <= S is already established above, so S1 + B + G
        # can only reach zero if S == E and B == G == 0 exactly, which is a
        # measure-zero event on continuous inputs. An earlier draft had a
        # `continue` here; coverage showed it never executed, and an unreachable
        # guard is indistinguishable from a guard that stopped working.
        assert (r["solar"] - min(r["solar"], r["exp"])) + r["discharge"] + r["imp"] > 0.0
        out = flows(solar, battery, grid, house)
        credited = solar_out(out) + battery_out(out) + grid_out(out)
        if supply >= drawn:
            assert credited == pytest.approx(supply, rel=1e-9, abs=1e-9)
            seen_idle += 1
        else:
            assert credited - supply == pytest.approx(drawn - supply,
                                                      rel=1e-9, abs=1e-9)
            seen_clamped += 1
    assert seen_idle > 100 and seen_clamped > 100, (
        "both branches must be exercised: idle=%d clamped=%d"
        % (seen_idle, seen_clamped))


def test_sum_of_flows_matches_fsum_over_a_random_walk():
    rng = random.Random(0000000000)
    naive = 0.0
    values = []
    for _ in range(20_000):
        out = flows(
            rng.uniform(0.0, 6000.0),
            rng.uniform(-5000.0, 5000.0),
            rng.uniform(-5000.0, 5000.0),
            rng.uniform(0.0, 6000.0),
        )
        s = sum(out.values())
        values.append(s)
        naive += s
    exact = math.fsum(values)
    assert abs(naive - exact) / exact < 1e-12


def test_many_tiny_intervals_equal_one_large_interval():
    """Riemann integration of a constant must be exact to the tolerance stated."""
    args = (3000.0, 1000.0, -500.0, 1500.0, 0.0)
    out = flows(*args)
    steps = 3600  # one hour at 1 s
    for k in FLOWS:
        summed = math.fsum([out[k] / steps] * steps)
        assert summed == pytest.approx(out[k], rel=1e-12, abs=1e-9), k


def test_decompose_is_deterministic():
    """Same inputs, same outputs -- no accumulated internal state."""
    args = (2982.0, -1063.0, 200.0, 4045.0, 500.0)
    first = flows(*args)
    for _ in range(100):
        assert flows(*args) == first


def test_scaling_invariance_of_the_split():
    """Doubling every input doubles every flow: the split is scale-free.

    This is a property of the proportional law specifically -- the shares are
    ratios, so they are invariant under scaling while the sinks are linear.
    """
    base = flows(3000.0, 1000.0, -500.0, 1500.0, 400.0)
    doubled = flows(6000.0, 2000.0, -1000.0, 3000.0, 800.0)
    for k in FLOWS:
        assert doubled[k] == pytest.approx(2.0 * base[k], rel=1e-12, abs=1e-9), k
