"""Tests for flows.py -- the Sankey v2 twelve-flow decomposition.

Run:

    uv run --no-project --with pytest --with pytest-cov --with hypothesis \
        python -m pytest sankey_tests/test_flows.py -q

Run the offline reconciliation table (the measurement, not a test):

    uv run --no-project python sankey_tests/test_flows.py

Every test here must be able to fail. Tests that merely restate the
implementation are worse than no test: they lock in whatever the code happens to
do and give false confidence.

**A test that is green because it never RAN is the same failure in a disguise
-- check the skip count, not just the colour.** That bit this project twice in
one day: a property below excluded the `supply == drawn` boundary and so
silently skipped valid samples instead of failing on them, and separately a
`check_structure_disabled` mutant survived five green tests of a guard
`decompose()` had stopped calling. Nothing complained in either case, and
nothing would have. Whenever a property carries a precondition, check that it
still admits the cases it is supposed to cover. So the checks below are written against the
*specification* -- the brief's rule, the sign conventions, the graph's
structural absences -- and several are cross-checked against recorded history
rather than against flows.py itself.
"""
import gzip
import json
import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import flows  # noqa: E402
from flows import (BadReading, StructureError, decompose,  # noqa: E402
                   node_totals, readings)

FIX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
DAYS = ("2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30")
PARTIAL_DAY = "2026-08-30"

# 2026-08-27 is EXCLUDED from every Tesla-dependent measurement. The Tesla
# template was created at 13:28 local that day, so the first 13.5 hours have no
# reading at all -- and CLAUDE.md records 8.384 kWh going into the car between
# 00:30 and 05:16 that same day, entirely inside the blind window. The day is
# fully usable for everything that does not involve the Tesla, because no
# non-Tesla flow can see the Tesla reading (proved by
# test_tesla_reading_cannot_move_any_non_tesla_flow).
TESLA_DAYS = ("2026-08-28", "2026-08-29", "2026-08-30")


@pytest.fixture(autouse=True)
def _default_rule():
    """No test may leak a loss-rule change into the next one."""
    previous = flows.set_loss_rule("proportional")
    yield
    flows.set_loss_rule(previous)


# --------------------------------------------------------------------------
# Offline replay harness -- v2-suite's, NOT a private one.
#
# There were three hand-rolled left-hold integrators in this tree at one point,
# mine among them. A parity test whose two walkers were written independently
# can pass while measuring nothing, so fixtures/replay.py is the single one and
# it is v2-suite's file. Everything below CONSUMES it: the only things added
# here are the reporting-boundary refusal and two measurements v2-suite's
# replay() does not compute, both built on ITS intervals() walker rather than
# another copy.
#
# THE CROSS-CHECK, AND ITS LIMIT. Before the swap this module carried its own
# walker, written independently of v2-suite's and before it existed. Replaying
# the same four days through both produced IDENTICAL figures -- solar, grid
# import/export, battery charge/discharge, house, the Inverter node
# (3.180 / 3.097 / 2.509 / 1.662 kWh) and unsourced export in the third decimal
# of a kWh. Two independent implementations of BRIEF_V2 section 2 agreeing over
# four real days is the strongest evidence available that the spec is
# implemented correctly, and it is worth more than either author testing their
# own work.
#
# The limit, stated because the claim is easy to over-read: that comparison was
# made at the precision that had been RECORDED -- three to four decimals -- not
# at floating-point precision. The private walker was deleted in the swap and
# no copy was kept to diff against, so the comparison cannot now be tightened
# without resurrecting it, which is not judged worth doing. "Identical" here
# means identical to the decimals reported, not bitwise.
# --------------------------------------------------------------------------

sys.path.insert(0, FIX)
import replay as rp  # noqa: E402


def load_day(day):
    """v2-suite's five-channel loader, plus the two sidecar extras I need."""
    doc = rp.load_day(day, tesla=True)
    doc["tesla_counter"] = doc["tesla_counters"]["tesla_home_charging_energy"]
    path = os.path.join(FIX, "tesla_history_%s.json.gz" % day)
    with gzip.open(path, "rt") as fh:
        doc["house_excl_car"] = json.load(fh)["series"]["house_excl_car"]
    return doc


def load_counters():
    return rp.load_counters()["days"]


def integrate_channel(doc, points, end_epoch=None):
    """Left-Riemann integral of one extra series over v2-suite's timeline. kWh."""
    probe = dict(doc)
    probe["series"] = dict(doc["series"])
    probe["series"]["tesla"] = points
    total = 0.0
    for _t, dt, v in rp.intervals(probe, end_epoch):
        if v["tesla"] is not None:
            total += max(0.0, v["tesla"]) * dt / 3_600_000.0
    return total


def unsourced_export(doc, end_epoch=None):
    """Metered export with no solar behind it: kWh and how many samples.

    BRIEF_V2 section 10. Export is solar-only, so E beyond S has no legal source
    and is left unattributed rather than invented. Second pass over v2-suite's
    OWN walker -- not a second walker.
    """
    kwh = 0.0
    samples = 0
    for _t, dt, v in rp.intervals(doc, end_epoch):
        if any(v[c] is None for c in rp.CHANNELS):
            continue
        short = max(0.0, -v["grid"]) - min(max(0.0, v["solar"]), max(0.0, -v["grid"]))
        if short > 0.0:
            kwh += short * dt / 3_600_000.0
            samples += 1
    return kwh, samples


def replay_day(day):
    doc = load_day(day)
    end = rp.last_sample_epoch(doc) if day == PARTIAL_DAY else None
    r = rp.replay(doc, decompose, end_epoch=end)
    # v2-suite's replay zero-fills a blind Tesla channel and returns the seconds
    # it covered rather than absorbing them silently. That is the honest half;
    # the refusal is the other half and it belongs here, at the reporting
    # boundary, so the ten quantities the Tesla channel provably cannot reach
    # survive on 2026-08-27.
    r["blind_channels"] = (frozenset(["tesla"]) if r["tesla_assumed_s"] > 0.0
                           else frozenset())
    r["unsourced_export"], r["unsourced_samples"] = unsourced_export(doc, end)
    r["tesla_counter"] = doc["tesla_counter"]
    r["house_excl_car"] = doc["house_excl_car"]
    r["doc"] = doc
    r["end_epoch"] = end
    return r


COUNTER_KEY = rp.COUNTER_KEY
pct = rp.pct


def counter_facing(f):
    """The six quantities that face the inverter's own daily counters.

    Deliberately NOT rp.derived(): that returns the House NODE, which excludes
    the Tesla, while house_consumption_today measures the whole house. Facing
    the counter with the node would understate it by the car -- 22 kWh on
    2026-08-30. Every other quantity here agrees with rp.derived() exactly, and
    a test asserts that.
    """
    n = node_totals(f)
    return {
        "house": n["house"] + n["tesla"],
        "grid_export": n["export"],
        "grid_import": n["grid_spent"],
        "battery_charge": n["battery_in"],
        "battery_discharge": n["battery_spent"],
        "solar": n["solar_spent"],
    }


def reportable(r):
    """Every quantity for a replayed day, with None wherever it was not measured.

    The refusal is DERIVED from flows.DEPENDS_ON, not hand-maintained. Add a
    thirteenth flow tomorrow that reads a blind channel and this refuses it
    automatically; a hardcoded pair of names would have gone on reporting a
    fabricated number for it. flows.py's map is held complete AND minimal by
    test_dependency_map_is_complete_and_minimal.

    None, never 0.0. On 2026-08-27 a zero Tesla figure would not be a small
    error, it would be a fabrication: CLAUDE.md records 8.384 kWh going into
    the car inside the blind window.
    """
    out = dict(r["flows"])
    out.update(node_totals(r["flows"]))
    for q in flows.unreportable(r["blind_channels"]):
        out[q] = None
    return out


# --------------------------------------------------------------------------
# 1. The contract: which flows exist, and which deliberately do not
# --------------------------------------------------------------------------

EXPECTED_FLOWS = {
    "solar_to_house", "solar_to_tesla", "solar_to_battery", "solar_to_export",
    "solar_to_inverter",
    "battery_to_house", "battery_to_tesla", "battery_to_inverter",
    "grid_to_house", "grid_to_tesla", "grid_to_battery", "grid_to_inverter",
}


def test_flow_keys_are_exactly_the_twelve_links():
    assert set(flows.FLOWS) == EXPECTED_FLOWS
    assert len(flows.FLOWS) == 12


@pytest.mark.parametrize("forbidden", [
    "battery_to_export",   # all three discharge windows are unset, forever
    "battery_to_battery",  # one signed sensor: a pack cannot do both at once
    "grid_to_export",      # import and export never coexist on one meter
    "solar_to_solar",
    "grid_to_grid",
])
def test_structurally_impossible_links_have_no_key(forbidden):
    """Absent, not zero. A zero-valued link is invisible on the chart and looks
    identical to one that was never declared -- CLAUDE.md records exactly that
    trap. Making it unrepresentable is the only version that cannot regress."""
    assert forbidden not in flows.FLOWS
    assert forbidden not in decompose(3000, -500, -100, 2500, 0)


def test_decompose_returns_every_key_on_every_sample():
    for args in [(0, 0, 0, 0, 0), (5000, 2000, -1000, 2000, 0),
                 (0, -900, 400, 1300, 700), (100, 0, 3000, 3100, 3000)]:
        assert set(decompose(*args)) == EXPECTED_FLOWS


def test_no_flow_is_ever_negative():
    for args in [(0, 0, 0, 0, 0), (10, -10, 10, 10, 10), (0, 500, 5000, 0, 0),
                 (6000, -1, -6000, 100, 0)]:
        assert all(v >= 0.0 for v in decompose(*args).values())


# --------------------------------------------------------------------------
# 2. readings(): sign conventions and clamps
# --------------------------------------------------------------------------

def non_degenerate(solar, battery, grid, house, tesla):
    """Refuse a sign-test input that cannot detect a sign error.

    BRIEF_V2 section 9, and it bites harder under proportional allocation than
    it did under v1's greedy walk: a source reading zero contributes zero to
    every sink, so a polarity test with a zero source gives the SAME all-zero
    answer for both polarities and passes green while the sign is backwards.
    Two equal sources are the same trap by symmetry. t-edges' v1 sign detectors
    detected nothing for exactly this reason.

    Every sign test in this file routes through here.
    """
    r = readings(solar, battery, grid, house, tesla)
    supply = (r["solar"], r["discharge"] + r["charge"], r["imp"] + r["exp"])
    if min(supply) <= 0.0:
        raise AssertionError(
            "degenerate sign input: a zero source cannot detect an inversion "
            "(%r)" % (supply,))
    if len(set(supply)) != len(supply):
        raise AssertionError(
            "degenerate sign input: equal sources are symmetric under "
            "inversion (%r)" % (supply,))
    return (solar, battery, grid, house, tesla)


def test_the_non_degenerate_helper_refuses_a_zero_source():
    with pytest.raises(AssertionError, match="zero source"):
        non_degenerate(0, -500, 400, 1000, 0)
    with pytest.raises(AssertionError, match="zero source"):
        non_degenerate(3000, 0, 400, 1000, 0)
    with pytest.raises(AssertionError, match="zero source"):
        non_degenerate(3000, -500, 0, 1000, 0)


def test_the_non_degenerate_helper_refuses_equal_sources():
    with pytest.raises(AssertionError, match="equal sources"):
        non_degenerate(400, -400, 900, 1000, 0)


def test_grid_sign_inversion_is_detectable_on_a_non_degenerate_sample():
    """Three distinct non-zero sources, so flipping the meter's sign MUST move
    the answer. CLAUDE.md singles out sign inversion as the failure an agent
    helpfully introduces, and this repo already inverts two conventions at the
    HA boundary."""
    args = non_degenerate(3000, -500, 900, 2600, 0)
    imported = decompose(*args)
    exported = decompose(3000, -500, -900, 2600, 0)
    assert imported["grid_to_house"] > 0.0
    assert exported["grid_to_house"] == 0.0
    assert exported["solar_to_export"] == pytest.approx(900.0)
    assert imported["solar_to_export"] == 0.0
    assert imported != exported


def test_battery_sign_inversion_is_detectable_on_a_non_degenerate_sample():
    args = non_degenerate(3000, -500, 900, 2600, 0)
    discharging = decompose(*args)
    charging = decompose(3000, 500, 900, 2600, 0)
    assert discharging["battery_to_house"] > 0.0
    assert charging["battery_to_house"] == 0.0
    assert charging["solar_to_battery"] > 0.0
    assert discharging["solar_to_battery"] == 0.0
    assert discharging != charging


def test_battery_sign_convention_splits_charge_and_discharge():
    assert readings(0, 800, 0, 0, 0)["charge"] == 800.0
    assert readings(0, 800, 0, 0, 0)["discharge"] == 0.0
    assert readings(0, -800, 0, 0, 0)["discharge"] == 800.0
    assert readings(0, -800, 0, 0, 0)["charge"] == 0.0


def test_grid_sign_convention_is_positive_importing():
    """HA's convention, NOT register 33257's. CLAUDE.md flags this as exactly
    the sign an agent flips in the wrong direction."""
    assert readings(0, 0, 1200, 0, 0)["imp"] == 1200.0
    assert readings(0, 0, 1200, 0, 0)["exp"] == 0.0
    assert readings(0, 0, -1200, 0, 0)["exp"] == 1200.0
    assert readings(0, 0, -1200, 0, 0)["imp"] == 0.0


def test_negative_where_impossible_is_clamped_not_rejected():
    """Solar and house cannot be negative physically, but a sensor can briefly
    read -3 W. Rejecting the whole sample for that would blank the chart."""
    assert readings(-3, 0, 0, -5, -7)["solar"] == 0.0
    assert readings(-3, 0, 0, -5, -7)["house"] == 0.0
    assert readings(-3, 0, 0, -5, -7)["tesla"] == 0.0


def test_tesla_is_clamped_to_house_load():
    r = readings(0, 0, 3000, 2000, 7000)
    assert r["tesla"] == 2000.0
    assert r["house_rest"] == 0.0


def test_tesla_below_house_leaves_the_remainder():
    r = readings(0, 0, 0, 3000, 1800)
    assert r["tesla"] == 1800.0
    assert r["house_rest"] == 1200.0


def test_house_rest_is_never_negative_for_any_tesla_reading():
    for t in (-1e4, 0, 1, 999, 1000, 1001, 1e4):
        r = readings(0, 0, 0, 1000, t)
        assert r["house_rest"] >= 0.0
        assert r["tesla"] + r["house_rest"] == pytest.approx(1000.0)


def test_tesla_reading_cannot_move_any_non_tesla_flow():
    """The fact that makes 2026-08-27 recoverable, stated as an invariant.

    T only ever splits the house sink. Hr + T == house for every T, the shares
    depend on S1/B/G alone, and L depends on Hr + T rather than on either part.
    So ten of the twelve flows -- and each source's to_house + to_tesla sum --
    are provably blind to the Tesla reading. A day whose Tesla channel does not
    exist yet therefore loses ONLY the House/Tesla split, and keeps solar,
    export, battery, grid and the Inverter node intact.

    This is why the replay integrates 2026-08-27 with T = 0 for those ten and
    returns None for the other two, rather than discarding the day.
    """
    blind = ("solar_to_house", "solar_to_tesla", "battery_to_house",
             "battery_to_tesla", "grid_to_house", "grid_to_tesla")
    for args in [(3000, -500, 400, 2600), (0, 900, 3000, 1800),
                 (6000, 2000, -1500, 2000), (0, 0, 0, 0), (50, -20, 30, 60)]:
        a = decompose(*args, 0)
        for t in (1, 250, 999.9, 7000, 1e5 - 1):
            b = decompose(*args, t)
            for k in flows.FLOWS:
                if k in blind:
                    continue
                assert b[k] == pytest.approx(a[k], abs=1e-9), k
            for src in flows.SOURCES:
                assert (b[src + "_to_house"] + b[src + "_to_tesla"]) == \
                    pytest.approx(a[src + "_to_house"] + a[src + "_to_tesla"],
                                  abs=1e-9), src


def test_tesla_clamp_matters_on_a_stale_plateau():
    """TeslaMate publishes on change, so a 7 kW plateau can outlive the charge
    by minutes. Without the clamp that sample sends house_rest negative."""
    f = decompose(0, 0, 300, 300, 7000)
    assert f["grid_to_tesla"] == pytest.approx(300.0)
    assert f["grid_to_house"] == 0.0


# --------------------------------------------------------------------------
# 3. Bad readings
# --------------------------------------------------------------------------

@pytest.mark.parametrize("bad", [
    None, "", "   ", "unavailable", "unknown", "none", "abc", "nan", "inf",
    [], {}, (1,), object(), True, False,
    float("nan"), float("inf"), float("-inf"),
    1e6, -1e6, 100_000.01,
])
@pytest.mark.parametrize("slot", range(5))
def test_bad_reading_in_any_slot_raises(bad, slot):
    args = [1000.0, -100.0, 50.0, 900.0, 0.0]
    args[slot] = bad
    with pytest.raises(BadReading):
        decompose(*args)


def test_nan_would_otherwise_decompose_as_a_still_night():
    """The specific reason the isnan guard exists: max(0.0, nan) is 0.0, so
    without it a broken sensor is byte-identical to darkness."""
    assert max(0.0, float("nan")) == 0.0
    with pytest.raises(BadReading):
        decompose(float("nan"), 0, 0, 0, 0)


def test_bad_reading_is_both_valueerror_and_typeerror():
    assert issubclass(BadReading, ValueError)
    assert issubclass(BadReading, TypeError)
    with pytest.raises(ValueError):
        decompose(None, 0, 0, 0, 0)
    with pytest.raises(TypeError):
        decompose(None, 0, 0, 0, 0)


@pytest.mark.parametrize("text,want", [
    ("2982", 2982.0), (" 3000 ", 3000.0), ("3e3", 3000.0), ("-43", -43.0),
    ("0.0", 0.0), ("\t-1.5\n", -1.5),
])
def test_numeric_strings_are_the_normal_path(text, want):
    """HA states are always strings; this is not an exotic case."""
    assert flows._reading("x", text) == want


def test_the_exact_u32_misread_claudemd_records_is_rejected():
    """Register 33257 read unsigned gives 4294967253 W for a true -43 W."""
    with pytest.raises(BadReading):
        decompose(0, 0, 4294967253, 0, 0)
    assert decompose(0, 0, -43, 0, 0) is not None


def test_the_plausibility_ceiling_boundary_is_inclusive():
    assert flows._reading("x", flows.MAX_PLAUSIBLE_W) == flows.MAX_PLAUSIBLE_W
    assert flows._reading("x", -flows.MAX_PLAUSIBLE_W) == -flows.MAX_PLAUSIBLE_W
    with pytest.raises(BadReading):
        flows._reading("x", flows.MAX_PLAUSIBLE_W + 1)


def test_bad_reading_names_the_offending_channel():
    for slot, name in enumerate(("solar", "battery", "grid", "house", "tesla")):
        args = [0, 0, 0, 0, 0]
        args[slot] = "unavailable"
        with pytest.raises(BadReading) as e:
            decompose(*args)
        assert name in str(e.value)


# --------------------------------------------------------------------------
# 4. Step 1: export is solar-only, and capped
# --------------------------------------------------------------------------

def test_export_is_credited_entirely_to_solar():
    f = decompose(5000, 0, -2000, 3000, 0)
    assert f["solar_to_export"] == pytest.approx(2000.0)
    assert node_totals(f)["export"] == pytest.approx(2000.0)


def test_export_cannot_exceed_the_solar_reading():
    """At night with a nonsense export reading, no solar ribbon may appear."""
    f = decompose(0, -1000, -500, 500, 0)
    assert f["solar_to_export"] == 0.0
    assert all(v == 0.0 for k, v in f.items() if k.startswith("solar_"))


def test_partial_export_cap_leaves_the_rest_of_solar_available():
    f = decompose(300, 0, -1000, 300, 0)
    assert f["solar_to_export"] == pytest.approx(300.0)
    # Solar is now fully spent on export; the house must be fed by something
    # else, and there is nothing else, so nothing reaches it.
    assert f["solar_to_house"] == 0.0


def test_export_removes_solar_from_the_pool_for_other_sinks():
    """S1 = S - s2e, not S. If export did not consume solar, the same watt
    would be credited twice."""
    f = decompose(4000, 0, -1000, 3000, 0)
    assert f["solar_to_export"] == pytest.approx(1000.0)
    assert f["solar_to_house"] == pytest.approx(3000.0)
    assert node_totals(f)["solar_spent"] <= 4000.0 + 1e-9


def test_no_import_while_exporting_reaches_any_sink():
    f = decompose(4000, 0, -1000, 3000, 0)
    assert all(v == 0.0 for k, v in f.items() if k.startswith("grid_"))


# --------------------------------------------------------------------------
# 5. Step 2: the proportional shares
# --------------------------------------------------------------------------

def test_every_sink_is_filled_exactly_by_its_three_inbound_flows():
    for args in [(3000, -500, 400, 2000, 0), (0, 1000, 3000, 1800, 900),
                 (6000, 2000, -1500, 2000, 500), (12, -3, 7, 15, 4)]:
        r = readings(*args)
        f = decompose(*args)
        n = node_totals(f)
        assert n["house"] == pytest.approx(r["house_rest"])
        assert n["tesla"] == pytest.approx(r["tesla"])
        assert n["battery_in"] == pytest.approx(r["charge"])
        assert n["inverter"] == pytest.approx(flows.inverter_loss(r))


def test_shares_sum_to_one_when_there_is_supply():
    ws, wb, wg = flows._normalise((1000.0, 500.0, 250.0))
    assert ws + wb + wg == pytest.approx(1.0)
    assert (ws, wb, wg) == pytest.approx((4 / 7, 2 / 7, 1 / 7))


def test_shares_are_zero_when_total_supply_is_zero():
    assert flows._normalise((0.0, 0.0, 0.0)) == (0.0, 0.0, 0.0)


def test_the_division_guard_is_exact_not_epsilon():
    """v1 leaked a nanowatt through an `over > _EPS` trim. There is no
    threshold here for a value to hide beneath: a picowatt of supply is still
    supply and still normalises."""
    share = flows._normalise((1e-12, 0.0, 0.0))
    assert share == (1.0, 0.0, 0.0)


def test_zero_supply_sends_every_flow_to_zero():
    f = decompose(0, 0, 0, 0, 0)
    assert all(v == 0.0 for v in f.values())


def test_a_house_load_with_no_supply_at_all_draws_nothing():
    """Not a lie by omission: the chart will show a House node with no ribbon,
    which is the honest rendering of four sensors that disagree."""
    f = decompose(0, 0, 0, 2000, 0)
    assert all(v == 0.0 for v in f.values())


def test_a_source_reading_zero_contributes_zero_to_every_sink():
    f = decompose(0, -1000, 500, 1200, 0)
    assert all(v == 0.0 for k, v in f.items() if k.startswith("solar_"))
    f = decompose(1000, 0, 500, 1200, 0)
    assert all(v == 0.0 for k, v in f.items() if k.startswith("battery_"))
    f = decompose(1000, -500, 0, 1200, 0)
    assert all(v == 0.0 for k, v in f.items() if k.startswith("grid_"))


def test_every_sink_draws_the_same_mix():
    """The defining property of the rule. Two sinks fed in different ratios
    would mean an implicit ordering had crept back in."""
    f = decompose(3000, -1000, 500, 2000, 800)
    r = readings(3000, -1000, 500, 2000, 800)
    for sink, amount in (("house", r["house_rest"]), ("tesla", r["tesla"]),
                         ("battery", r["charge"])):
        if amount <= 0.0:
            continue
        assert f["solar_to_" + sink] / amount == pytest.approx(
            f["solar_to_house"] / r["house_rest"])


def test_attribution_is_order_free():
    """No permutation exists to test -- that is the point. What can be tested is
    that the result depends only on the readings, so scaling every source and
    every sink by the same factor scales every flow by that factor."""
    base = decompose(3000, -1000, 500, 2000, 800)
    doubled = decompose(6000, -2000, 1000, 4000, 1600)
    for k in flows.FLOWS:
        assert doubled[k] == pytest.approx(2 * base[k])


def test_pure_grid_import_into_nothing_goes_to_the_inverter():
    """The brief's worked example: decompose(0, 0, 2 kW, 0) must NOT push 2 kW
    into a house drawing nothing. v1's clamp did, at up to -8.3% on import."""
    f = decompose(0, 0, 2000, 0, 0)
    assert f["grid_to_inverter"] == pytest.approx(2000.0)
    assert f["grid_to_house"] == 0.0


# --------------------------------------------------------------------------
# 6. The Inverter node
# --------------------------------------------------------------------------

def test_inverter_loss_is_the_measured_residual():
    r = readings(2982, -457, -98, 3078, 0)
    assert flows.inverter_loss(r) == pytest.approx(263.0)
    assert node_totals(decompose(2982, -457, -98, 3078, 0))["inverter"] == \
        pytest.approx(263.0)


def test_inverter_loss_clamps_at_zero_when_sinks_out_read_sources():
    """100 W of import against a 900 W house is not a physical sample -- it is
    sensor skew across a step change. The residual would be -800 W; clamping it
    to zero means the house still fills exactly, at the price of crediting the
    grid with 9x what the meter read. The card clamps that ribbon back to the
    Grid node's own counter, so the overstatement never reaches the screen; over
    a whole replayed day it is worth +0.02% to +0.16% of daily import."""
    r = readings(0, 0, 100, 900, 0)
    assert flows.inverter_loss(r) == 0.0
    f = decompose(0, 0, 100, 900, 0)
    assert node_totals(f)["inverter"] == 0.0
    assert f["grid_to_house"] == pytest.approx(900.0)
    assert node_totals(f)["grid_spent"] == pytest.approx(900.0)


def test_inverter_loss_counts_export_as_an_outflow():
    """Export leaves the system, so it cannot also be loss."""
    r = readings(5000, 0, -4000, 800, 0)
    assert flows.inverter_loss(r) == pytest.approx(200.0)


def test_inverter_loss_counts_battery_charge_as_an_outflow():
    r = readings(3000, 2000, 0, 800, 0)
    assert flows.inverter_loss(r) == pytest.approx(200.0)


def test_inverter_loss_excludes_tesla_double_counting():
    """house already contains tesla; counting both would invent 7 kW of loss."""
    r = readings(0, 0, 8000, 7500, 7000)
    assert flows.inverter_loss(r) == pytest.approx(500.0)


def test_inverter_node_is_shared_across_sources_proportionally():
    f = decompose(3000, -1000, 0, 3000, 0)
    n = node_totals(f)
    assert n["inverter"] == pytest.approx(1000.0)
    assert f["solar_to_inverter"] == pytest.approx(750.0)
    assert f["battery_to_inverter"] == pytest.approx(250.0)


# --------------------------------------------------------------------------
# 7. check_structure -- every raise must be reachable
# --------------------------------------------------------------------------

def _ok_flows():
    return {k: 0.0 for k in flows.FLOWS}


def _ok_readings():
    return readings(0, 0, 0, 0, 0)


def test_structure_error_and_bad_reading_are_disjoint():
    """The distinction that makes StructureError worth having.

    BadReading means the INPUT was bad -- routine, and the Jinja equivalent is
    rendering that sensor unavailable. StructureError means FLOWS.PY IS BROKEN.
    If a code defect could surface as BadReading it would render as
    `unavailable`, look exactly like TeslaMate being down, and be invisible in
    HA: we would ship a broken decomposition and see a sensor blink out now and
    then. So the two must share no ancestry and no reachable input.

    DO NOT DELETE THIS AS PEDANTIC. It is a worked example of why a ruling
    belongs in a test rather than in prose in a brief.

    On 2026-08-30 flows.py was edited by another agent to
    `class StructureError(ValueError, TypeError)` -- which makes it a
    BadReading in all but name, since BadReading IS (ValueError, TypeError).
    That is exactly the change team-lead had ruled against in writing, and the
    ruling was recorded in BRIEF_V2 and in messages. It was caught ONLY because
    this test failed. Nobody noticed the file change.

    And the failure mode this prevents is invisible by construction: had the
    line shipped, a genuine defect in flows.py would have rendered as a missing
    sensor in HA, indistinguishable from TeslaMate being offline, and would
    have gone on rendering that way indefinitely. There is no symptom to
    notice. A brief cannot fail; a test can.
    """
    assert not issubclass(StructureError, BadReading)
    assert not issubclass(BadReading, StructureError)
    assert not issubclass(StructureError, (ValueError, TypeError))


def test_no_bad_input_can_raise_a_structure_error():
    """A malformed input reaching check_structure would be a hole in the input
    guards, not a structural failure -- so it must raise BadReading first."""
    for bad in (None, "", "unavailable", "unknown", [], True, False,
                float("nan"), float("inf"), 1e9, -1e9, "abc"):
        for slot in range(5):
            args = [1000.0, -100.0, 50.0, 900.0, 10.0]
            args[slot] = bad
            with pytest.raises(BadReading):
                decompose(*args)


def test_no_well_formed_input_can_raise_a_bad_reading_or_structure_error():
    rng = random.Random(23)
    for _ in range(3000):
        args = (rng.uniform(0, 9000), rng.uniform(-9000, 9000),
                rng.uniform(-9000, 9000), rng.uniform(0, 9000),
                rng.uniform(0, 9000))
        decompose(*args)  # must not raise either


def test_check_structure_accepts_a_good_sample():
    assert flows.check_structure(_ok_flows(), _ok_readings()) is None


def test_check_structure_rejects_an_added_key():
    bad = _ok_flows()
    bad["battery_to_export"] = 1.0
    with pytest.raises(StructureError, match="battery_to_export"):
        flows.check_structure(bad, _ok_readings())


def test_check_structure_rejects_a_missing_key():
    bad = _ok_flows()
    del bad["grid_to_tesla"]
    with pytest.raises(StructureError, match="grid_to_tesla"):
        flows.check_structure(bad, _ok_readings())


def test_check_structure_rejects_a_negative_flow():
    bad = _ok_flows()
    bad["solar_to_house"] = -1e-30
    with pytest.raises(StructureError, match="solar_to_house"):
        flows.check_structure(bad, _ok_readings())


def test_check_structure_rejects_simultaneous_charge_and_discharge():
    r = _ok_readings()
    r["charge"], r["discharge"] = 100.0, 100.0
    with pytest.raises(StructureError, match="charging and discharging"):
        flows.check_structure(_ok_flows(), r)


def test_check_structure_rejects_simultaneous_import_and_export():
    r = _ok_readings()
    r["imp"], r["exp"] = 100.0, 100.0
    with pytest.raises(StructureError, match="importing and exporting"):
        flows.check_structure(_ok_flows(), r)


def test_check_structure_rejects_a_charging_battery_used_as_a_source():
    r = _ok_readings()
    r["charge"] = 100.0
    bad = _ok_flows()
    bad["battery_to_house"] = 1.0
    with pytest.raises(StructureError, match="cannot also be a source"):
        flows.check_structure(bad, r)


def test_a_charging_battery_never_appears_as_a_source_in_practice():
    f = decompose(4000, 2000, -500, 1500, 0)
    assert f["battery_to_house"] == 0.0
    assert f["battery_to_tesla"] == 0.0
    assert f["battery_to_inverter"] == 0.0


# --------------------------------------------------------------------------
# 8. The loss-rule seam
# --------------------------------------------------------------------------

def test_set_loss_rule_round_trips():
    previous = flows.set_loss_rule("band_table")
    assert previous == "proportional"
    assert flows.LOSS_RULE == "band_table"
    assert flows.set_loss_rule("proportional") == "band_table"


def test_set_loss_rule_rejects_an_unknown_name():
    with pytest.raises(ValueError, match="unknown loss rule"):
        flows.set_loss_rule("nope")
    assert flows.LOSS_RULE == "proportional"


def test_default_rule_is_proportional():
    assert flows.LOSS_RULE == "proportional"


@pytest.mark.parametrize("watts,want", [
    (0.0, 0.22), (25.0, 0.22), (10.0, 0.22),
    (75.0, 0.24), (300.0, 0.68), (2250.0, 0.93), (6000.0, 0.975),
    (7000.0, 0.975), (1e5, 0.975),
])
def test_band_efficiency_at_and_beyond_the_measured_knots(watts, want):
    assert flows.band_efficiency(watts) == pytest.approx(want)


def test_band_efficiency_interpolates_between_knots():
    assert flows.band_efficiency(50.0) == pytest.approx(0.23)
    assert flows.band_efficiency(187.5) == pytest.approx(0.46)


def test_band_efficiency_is_monotone_over_the_whole_range():
    xs = [i * 5.0 for i in range(0, 1600)]
    ys = [flows.band_efficiency(x) for x in xs]
    assert all(b >= a for a, b in zip(ys, ys[1:]))


def test_band_table_charges_a_trickling_battery_its_own_loss():
    """The reason the seam exists. A 40 W battery trickle beside 4 kW of solar:
    proportional charges the battery ~1% of the loss, the band table ~78% of
    its own output."""
    args = (4000, -40, 0, 3800, 0)
    prop = decompose(*args)
    flows.set_loss_rule("band_table")
    band = decompose(*args)
    assert band["battery_to_inverter"] > prop["battery_to_inverter"]
    assert band["battery_to_inverter"] == pytest.approx(40 * (1 - 0.22), rel=0.05)
    # Proportional charges the battery its share of the pool, ~1% of 4040 W,
    # so ~6% of its own output instead of the measured ~78%.
    assert prop["battery_to_inverter"] == pytest.approx(240 * 40 / 4040, rel=1e-6)
    assert prop["battery_to_inverter"] < 0.10 * 40


def test_band_table_still_fills_every_sink_exactly():
    flows.set_loss_rule("band_table")
    for args in [(3000, -500, 400, 2000, 0), (0, 1000, 3000, 1800, 900),
                 (6000, 2000, -1500, 2000, 500), (40, -20, 30, 45, 5)]:
        r = readings(*args)
        n = node_totals(decompose(*args))
        assert n["house"] == pytest.approx(r["house_rest"])
        assert n["tesla"] == pytest.approx(r["tesla"])
        assert n["battery_in"] == pytest.approx(r["charge"])
        assert n["inverter"] == pytest.approx(flows.inverter_loss(r))


def test_band_table_derates_grid_charging_by_the_measured_efficiency():
    """Solar reaches the pack DC-coupled; grid has to cross the converter."""
    shares = flows._band_table(1000.0, 0.0, 1000.0)
    ws, wb, wg = shares["charge"]
    assert wb == 0.0
    assert wg / ws == pytest.approx(flows.ETA_CHARGE)


def test_band_table_rows_never_degenerate_to_zero_while_supply_exists():
    """A real defect, found by the hypothesis property rather than by reading.

    At a denormal discharge the AC weighting ``discharge * 0.22`` UNDERFLOWS to
    exactly 0.0, so every band_table row collapsed to zero, every share
    vanished, and the sinks were left silently unfilled while the pool was
    non-empty. That broke the contract the LOSS_RULES block comment states --
    "every row must sum to 1, or to 0 when there is no supply at all" -- which
    is precisely what makes every sink fill exactly regardless of which rule
    ran.

    Fixed in the CODE, not narrowed in the test: a comment asserting an
    invariant the lines below do not enforce is the exact failure this file
    exists to catch.
    """
    assert 5e-324 * 0.22 == 0.0, "the underflow this test is about"
    for row in flows._band_table(0.0, 5e-324, 0.0).values():
        assert row == (0.0, 1.0, 0.0)

    # ...and with genuinely no supply, every row correctly stays at zero.
    for row in flows._band_table(0.0, 0.0, 0.0).values():
        assert row == (0.0, 0.0, 0.0)

    flows.set_loss_rule("band_table")
    r = readings(0, -5e-324, 0, 1.0, 0)
    n = node_totals(decompose(0, -5e-324, 0, 1.0, 0))
    assert n["house"] == pytest.approx(r["house_rest"], abs=1e-9)


def test_fallback_helper_only_replaces_a_degenerate_row():
    assert flows._fallback((0.0, 0.0, 0.0), (0.1, 0.2, 0.7)) == (0.1, 0.2, 0.7)
    assert flows._fallback((0.5, 0.5, 0.0), (0.1, 0.2, 0.7)) == (0.5, 0.5, 0.0)


def test_band_table_falls_back_to_ac_shares_when_no_dc_source_is_running():
    """Grid-only: there is no predicted DC loss to weight the Inverter node by,
    and the loss must still land on the only thing that was flowing."""
    shares = flows._band_table(0.0, 0.0, 1000.0)
    assert shares["loss"] == (0.0, 0.0, 1.0)
    flows.set_loss_rule("band_table")
    f = decompose(0, 0, 2000, 0, 0)
    assert f["grid_to_inverter"] == pytest.approx(2000.0)


def test_band_table_and_proportional_agree_when_only_one_source_runs():
    """With a single source every rule must put everything on it. A difference
    here would mean a rule had invented a source."""
    for args in [(3000, 0, 0, 2500, 0), (0, -1000, 0, 800, 0), (0, 0, 900, 700, 0)]:
        prop = decompose(*args)
        flows.set_loss_rule("band_table")
        band = decompose(*args)
        flows.set_loss_rule("proportional")
        for k in flows.FLOWS:
            assert band[k] == pytest.approx(prop[k]), k


def test_only_proportional_spends_the_metered_import_exactly():
    """The measured reason the default is what it is.

    Because proportional gives every sink the same row, each source's total
    spend collapses to ``share * total supply`` -- its own reading, exactly.
    The band table gives the Inverter node a different row, so that identity
    breaks and the smart meter's watts no longer land to the watt. CLAUDE.md
    makes that meter authoritative, which is a stronger constraint than
    charging the battery its own trickle loss.
    """
    args = (3000, -40, 900, 3500, 0)
    r = readings(*args)
    assert flows.inverter_loss(r) > 0.0
    assert node_totals(decompose(*args))["grid_spent"] == pytest.approx(r["imp"])
    flows.set_loss_rule("band_table")
    assert node_totals(decompose(*args))["grid_spent"] != pytest.approx(r["imp"])


def test_the_default_path_ignores_the_band_table_constants(monkeypatch):
    """v2's claim is that the DEFAULT decomposition contains no fitted
    efficiency. Corrupting every measured constant in the module must leave it
    byte-identical."""
    before = decompose(3000, -500, 400, 2000, 300)
    monkeypatch.setattr(flows, "BAND_TABLE", ((1.0, 0.001), (2.0, 0.002)))
    monkeypatch.setattr(flows, "ETA_CHARGE", 0.5)
    assert decompose(3000, -500, 400, 2000, 300) == before


def test_no_fitted_constants_marker_is_empty():
    assert flows.NO_FITTED_CONSTANTS == ()


@pytest.mark.parametrize("literal", ["0.9974", "105.9", "ETA_FLOOR", "0.96"])
def test_v1s_fitted_constants_are_absent_from_the_source(literal):
    """A grep-level guard. These four are what v2 exists to remove; if one
    reappears as live code the design has regressed. Comments are stripped so a
    docstring may still discuss them."""
    src = open(os.path.join(os.path.dirname(FIX), "flows.py")).read()
    code = "\n".join(ln for ln in src.splitlines()
                     if not ln.lstrip().startswith("#"))
    body = code.split('"""', 2)[-1]
    for chunk in body.split('"""')[::2]:
        assert literal not in chunk


# --------------------------------------------------------------------------
# 9. node_totals
# --------------------------------------------------------------------------

def test_node_totals_sums_only_inbound_flows_for_sinks():
    f = {k: 0.0 for k in flows.FLOWS}
    f["solar_to_house"], f["battery_to_house"], f["grid_to_house"] = 1.0, 2.0, 3.0
    f["solar_to_tesla"], f["grid_to_tesla"] = 4.0, 5.0
    f["solar_to_battery"], f["grid_to_battery"] = 6.0, 7.0
    f["solar_to_export"] = 8.0
    f["solar_to_inverter"] = 9.0
    n = node_totals(f)
    assert n["house"] == 6.0
    assert n["tesla"] == 9.0
    assert n["battery_in"] == 13.0
    assert n["export"] == 8.0
    assert n["inverter"] == 9.0
    assert n["solar_spent"] == 1 + 4 + 6 + 8 + 9
    assert n["battery_spent"] == 2.0
    assert n["grid_spent"] == 3 + 5 + 7


def test_house_node_excludes_tesla():
    """The whole point of v2's House identity. If House were
    house_consumption_today it would still contain the car."""
    f = decompose(0, 0, 8000, 7500, 7000)
    n = node_totals(f)
    assert n["house"] == pytest.approx(500.0)
    assert n["tesla"] == pytest.approx(7000.0)


def test_source_spend_never_exceeds_the_source_reading_on_a_consistent_sample():
    """A consistent sample is one where supply >= demand, so the Inverter node
    absorbs the difference and every source is spent exactly."""
    args = (4000, -600, 300, 3500, 0)
    r = readings(*args)
    n = node_totals(decompose(*args))
    assert n["solar_spent"] == pytest.approx(r["solar"])
    assert n["battery_spent"] == pytest.approx(r["discharge"])
    assert n["grid_spent"] == pytest.approx(r["imp"])


def test_sources_overspend_when_the_residual_clamps():
    """The one case where a source is credited beyond its own reading, and it
    has to be understood rather than "fixed".

    When the sinks out-read the sources the residual clamps at zero, so the
    sinks now demand more than the pool holds; filling every sink exactly then
    costs the sources more than they have. The overstatement is the safe
    direction -- ha-sankey-chart clamps a ribbon to
    min(parent_remainder, child_remainder, value), so it is drawn away -- and
    over the four replay days it is worth +0.11% to +0.42% of daily solar.
    Understating instead would silently shrink a ribbon with nothing to say so.
    """
    args = (0, 0, 1000, 1500, 0)
    r = readings(*args)
    assert flows.inverter_loss(r) == 0.0
    n = node_totals(decompose(*args))
    assert n["house"] == pytest.approx(1500.0)   # the sink still fills exactly
    assert n["grid_spent"] == pytest.approx(1500.0)  # ... at 1.5x the meter


def test_a_consistent_sample_leaves_no_source_overspent():
    args = (0, 0, 1500, 1000, 0)
    r = readings(*args)
    assert flows.inverter_loss(r) == pytest.approx(500.0)
    n = node_totals(decompose(*args))
    assert n["grid_spent"] == pytest.approx(1500.0)


# --------------------------------------------------------------------------
# 10. Properties over random samples
# --------------------------------------------------------------------------

try:
    from hypothesis import HealthCheck, given, settings
    from hypothesis import strategies as st
except ImportError:  # pragma: no cover - hypothesis is a declared dependency
    given = None

if given is not None:
    W = st.floats(min_value=-9000, max_value=9000, allow_nan=False,
                  allow_infinity=False)
    POS = st.floats(min_value=0, max_value=9000, allow_nan=False,
                    allow_infinity=False)

    @settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
    @given(POS, W, W, POS, POS)
    def test_property_flows_are_finite_and_non_negative(s, b, g, h, t):
        for rule in ("proportional", "band_table"):
            flows.set_loss_rule(rule)
            f = decompose(s, b, g, h, t)
            assert set(f) == EXPECTED_FLOWS
            for k, v in f.items():
                assert v >= 0.0, k
                assert math.isfinite(v), k
        flows.set_loss_rule("proportional")

    @settings(max_examples=400, suppress_health_check=[HealthCheck.too_slow])
    @given(POS, W, W, POS, POS)
    def test_property_every_sink_fills_exactly(s, b, g, h, t):
        """...whenever there is any supply left after export. With none, the
        division guard sends every flow to zero instead, which is the honest
        rendering of sinks that read energy no source reported."""
        for rule in ("proportional", "band_table"):
            flows.set_loss_rule(rule)
            r = readings(s, b, g, h, t)
            f = decompose(s, b, g, h, t)
            n = node_totals(f)
            if r["solar"] - min(r["solar"], r["exp"]) + r["discharge"] + r["imp"] <= 0.0:
                assert all(v == 0.0 for k, v in f.items() if k != "solar_to_export")
                continue
            assert n["house"] == pytest.approx(r["house_rest"], abs=1e-6)
            assert n["tesla"] == pytest.approx(r["tesla"], abs=1e-6)
            assert n["battery_in"] == pytest.approx(r["charge"], abs=1e-6)
            assert n["export"] == pytest.approx(min(r["solar"], r["exp"]), abs=1e-6)
            assert n["inverter"] == pytest.approx(flows.inverter_loss(r), abs=1e-6)
        flows.set_loss_rule("proportional")

    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    @given(POS, W, W, POS, POS)
    def test_property_energy_is_conserved_across_the_cut(s, b, g, h, t):
        """Everything the sources are credited with spending equals everything
        the sinks are credited with receiving. This holds on EVERY sample,
        including the inconsistent ones -- unlike per-source exactness, which
        does not (see test_sources_overspend_when_the_residual_clamps)."""
        r = readings(s, b, g, h, t)
        f = decompose(s, b, g, h, t)
        n = node_totals(f)
        spent = n["solar_spent"] + n["battery_spent"] + n["grid_spent"]
        received = (n["house"] + n["tesla"] + n["battery_in"] + n["export"]
                    + n["inverter"])
        assert spent == pytest.approx(received, abs=1e-6)
        assert spent == pytest.approx(sum(f.values()), abs=1e-6)

    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    @given(POS, W, W, POS, POS)
    def test_property_a_consistent_sample_spends_every_source_exactly(s, b, g, h, t):
        """When the residual is strictly positive the sample is internally
        consistent, and then proportional shares spend each source exactly its
        own reading -- the metered import in particular lands to the watt."""
        r = readings(s, b, g, h, t)
        # The exact closed-form condition, and it is TWO clauses, not one.
        # v2-suite derived the same pair independently and produced six false
        # failures against a CORRECT flows.py by asserting only the first.
        #   supply >= drawn : otherwise the L clamp binds, the sinks demand
        #                     more than the pool holds, and sources OVERspend.
        #   E <= S          : otherwise part of the export is unattributable by
        #                     design (no other source may feed export) and
        #                     sources UNDERspend.
        # Note the boundary supply == drawn IS included: exactness holds there.
        supply = r["solar"] + r["discharge"] + r["imp"]
        drawn = r["house_rest"] + r["tesla"] + r["charge"] + r["exp"]
        if supply < drawn or r["exp"] > r["solar"]:
            return
        n = node_totals(decompose(s, b, g, h, t))
        assert n["solar_spent"] == pytest.approx(r["solar"], abs=1e-6)
        assert n["battery_spent"] == pytest.approx(r["discharge"], abs=1e-6)
        assert n["grid_spent"] == pytest.approx(r["imp"], abs=1e-6)

    @settings(max_examples=300, suppress_health_check=[HealthCheck.too_slow])
    @given(POS, W, W, POS)
    def test_property_zero_tesla_matches_a_four_channel_world(s, b, g, h):
        f = decompose(s, b, g, h, 0)
        assert f["solar_to_tesla"] == 0.0
        assert f["battery_to_tesla"] == 0.0
        assert f["grid_to_tesla"] == 0.0


# --------------------------------------------------------------------------
# 11. Replay against four days of recorded history
# --------------------------------------------------------------------------

@pytest.fixture(scope="module")
def replays():
    return {d: replay_day(d) for d in DAYS}


def test_fixtures_are_present_for_every_replay_day():
    for d in DAYS:
        assert os.path.exists(os.path.join(FIX, "history_%s.json.gz" % d))
        assert os.path.exists(os.path.join(FIX, "tesla_history_%s.json.gz" % d))


def test_replay_reconstructs_the_raw_power_integrals(replays):
    """The tight reconciliation, and the one that actually tests this module.

    Every decomposed quantity is compared against the integral of the very
    sensor it came from, so the only thing that can move it is the attribution
    rule. Measured 2026-08-30 over the four days: worst case +0.89%, and the
    house total is exact to floating point. A regression here is a real bug.
    """
    limits = {"solar": 1.0, "grid_import": 0.5, "grid_export": 1.0,
              "battery_charge": 0.5, "battery_discharge": 1.5, "house": 0.01}
    for day in DAYS:
        r = replays[day]
        d = counter_facing(r["flows"])
        for k, limit in limits.items():
            assert abs(pct(d[k], r["raw"][k])) < limit, "%s %s" % (day, k)


def test_replay_reaches_the_inverter_counters(replays):
    """The loose reconciliation, against the inverter's own daily counters.

    This one tests the *sensors* as much as the decomposition, and the two
    outliers are already-documented facts about this site, not bugs here:

    * ``house`` runs 2.7-14.1% BELOW ``house_consumption_today``. CLAUDE.md
      records that gap (7.8% one night) and attributes it to two different
      inverter measurement paths -- an instantaneous wattage at register 33147
      against an energy counter at 33177-33180. It says explicitly: do not
      adjust either side to make them agree. The decomposition reproduces the
      power integral exactly, so it inherits the gap and adds nothing.
    * ``solar`` runs 1.9-3.2% above ``solar_today``, which is stored at 0.1 kWh
      resolution.

    The bounds are therefore set to the measured spread with headroom, and the
    test that means something is the raw-integral one above.
    """
    limits = {"solar": 5.0, "grid_import": 2.0, "grid_export": 5.0,
              "battery_charge": 3.0, "battery_discharge": 3.0, "house": 16.0}
    counters = load_counters()
    for day in DAYS:
        if day == PARTIAL_DAY:
            continue  # a partial day cannot face an end-of-day counter
        d = counter_facing(replays[day]["flows"])
        for k, limit in limits.items():
            want = counters[day][COUNTER_KEY[k]]["max"]
            assert abs(pct(d[k], want)) < limit, "%s %s" % (day, k)


def test_replay_grid_import_is_never_lost(replays):
    """v1 leaked 6.3-9.8% of daily import by sizing grid_to_house against a
    house-derived deficit -- an UNDERSTATEMENT, which the card cannot catch.

    v2 spends every metered watt and then some: measured +0.02% to +0.16% over
    the four days, all of it from samples where the residual clamps and the
    sinks briefly out-read the sources. The direction is what matters. An
    overstatement is clamped to the Grid node's own counter and disappears; the
    v1 shortfall silently shrank a ribbon with nothing anywhere to say so.
    """
    for day in DAYS:
        r = replays[day]
        d = counter_facing(r["flows"])
        err = pct(d["grid_import"], r["raw"]["grid_import"])
        assert 0.0 <= err < 0.5, "%s %.3f%%" % (day, err)


def test_replay_solar_export_matches_the_meter(replays):
    for day in DAYS:
        r = replays[day]
        assert r["flows"]["solar_to_export"] == pytest.approx(
            r["raw"]["grid_export"], rel=0.02)


def test_replay_house_plus_tesla_equals_the_raw_house_integral(replays):
    """House + Tesla is defined as inbound flows, so it must reconstruct the raw
    house-load integral. This one holds on EVERY day including 2026-08-27,
    because the sum does not depend on where the Tesla boundary falls."""
    for day in DAYS:
        r = replays[day]
        n = node_totals(r["flows"])
        assert n["house"] + n["tesla"] <= r["raw"]["house"] + 1e-9
        assert n["house"] + n["tesla"] > 0.97 * r["raw"]["house"]


def test_a_tesla_blind_day_refuses_exactly_the_tesla_dependent_quantities(replays):
    """2026-08-27 must return None -- not 0.0 -- for what the blind channel
    could have moved, and a real number for everything it provably could not.

    Refusing the whole day would throw away a complete inverter reconciliation
    to protect two figures; reporting zeroes would fabricate them. This is the
    third option, and it is only safe because the map is verified."""
    r = replays["2026-08-27"]
    assert r["blind_channels"] == frozenset(["tesla"])
    assert r["tesla_assumed_s"] > 40_000
    rep = reportable(r)

    for q in ("house", "tesla", "solar_to_house", "solar_to_tesla",
              "battery_to_house", "battery_to_tesla", "grid_to_house",
              "grid_to_tesla"):
        assert rep[q] is None, q
    # ...and everything the Tesla channel provably cannot reach survives.
    for q in ("solar_spent", "battery_spent", "grid_spent", "inverter",
              "export", "battery_in", "solar_to_battery", "grid_to_battery",
              "solar_to_inverter", "battery_to_inverter", "grid_to_inverter",
              "solar_to_export"):
        assert rep[q] is not None and rep[q] >= 0.0, q

    for day in TESLA_DAYS:
        assert replays[day]["blind_channels"] == frozenset()
        assert all(v is not None for v in reportable(replays[day]).values())


def test_dependency_map_is_complete_and_minimal():
    """The test that stops flows.DEPENDS_ON rotting.

    Perturb each input channel across many random samples and require, for
    every quantity, that the set of channels which MOVE it equals the set
    declared. Completeness is the safety direction -- an undeclared dependency
    means a fabricated number gets reported on a blind day. Minimality matters
    too: a map declaring everything would be safe and useless, refusing all of
    2026-08-27 including its valid inverter reconciliation.
    """
    rng = random.Random(11)
    moved = {q: set() for q in flows.DEPENDS_ON}

    def quantities(args):
        f = decompose(*args)
        out = dict(f)
        out.update(node_totals(f))
        return out

    for _ in range(400):
        base = [rng.uniform(0, 7000), rng.uniform(-5000, 5000),
                rng.uniform(-6000, 6000), rng.uniform(0, 9000),
                rng.uniform(0, 9000)]
        b = quantities(base)
        for i, channel in enumerate(flows.CHANNELS):
            alt = list(base)
            alt[i] = (rng.uniform(0, 7000) if channel in ("solar", "house", "tesla")
                      else rng.uniform(-6000, 6000))
            a = quantities(alt)
            for q in flows.DEPENDS_ON:
                if abs(a[q] - b[q]) > 1e-9:
                    moved[q].add(channel)

    for q, declared in sorted(flows.DEPENDS_ON.items()):
        assert moved[q] == set(declared), (
            "%s: declared %s, actually moved by %s"
            % (q, sorted(declared), sorted(moved[q])))


def test_unreportable_rejects_a_channel_name_that_does_not_exist():
    """A typo'd channel would otherwise silently refuse nothing."""
    with pytest.raises(ValueError, match="not input channels"):
        flows.unreportable(["telsa"])
    assert flows.unreportable([]) == frozenset()


def test_every_flow_and_node_total_is_in_the_dependency_map():
    keys = set(flows.FLOWS) | set(node_totals({k: 0.0 for k in flows.FLOWS}))
    assert set(flows.DEPENDS_ON) == keys


def test_replay_tesla_node_matches_the_tesla_energy_counter(replays):
    """Cross-check against sensor.tesla_home_charging_energy, which is an
    entirely separate integration in HA and knows nothing about this module.

    NOTE the counter is a LIFETIME total with no midnight reset, so the day's
    energy is last - first within the day, not `max`."""
    for day in TESLA_DAYS:
        c = replays[day]["tesla_counter"]
        want = c["last"] - c["first"]
        got = node_totals(replays[day]["flows"])["tesla"]
        if want < 0.1:
            assert got < 0.15, day
        else:
            assert abs(got - want) / want < 0.06, day


def test_tesla_power_samples_are_sufficient_not_merely_sparse(replays):
    """BRIEF_V2 section 11: do not judge a day by sample count. TeslaMate
    publishes on change, so five samples can be complete -- CLAUDE.md records a
    32 A plateau going 80 minutes without an update. Settle it by integrating
    the power channel and facing the independent energy counter."""
    for day in TESLA_DAYS:
        r = replays[day]
        c = r["tesla_counter"]
        want = c["last"] - c["first"]
        if want < 0.1:
            assert r["raw"]["tesla"] < 0.15, day
        else:
            assert abs(r["raw"]["tesla"] - want) / want < 0.06, day


def test_unsourced_export_is_dawn_dusk_rounding_not_a_measurement_fault(replays):
    """BRIEF_V2 section 10. Export is solar-only, so metered export beyond the
    solar reading has no legal source and is left unattributed rather than
    invented. Measured 0.020-0.049 kWh/day, i.e. under the 0.05 kWh threshold
    the brief sets for "leave it visible". If this ever exceeds it, that is a
    finding about the measurement chain and not something to paper over."""
    for day in DAYS:
        r = replays[day]
        assert r["unsourced_export"] < 0.05, "%s %.4f kWh" % (
            day, r["unsourced_export"])
        assert r["unsourced_export"] == pytest.approx(
            r["raw"]["grid_export"] - r["flows"]["solar_to_export"], abs=1e-9)


def test_house_node_matches_has_own_house_excluding_car_template(replays):
    """The strongest independent check on the House node.

    sensor.house_load_excluding_car is a template that ALREADY EXISTED in HA
    before any of this work, written by someone else for another purpose. The
    House node is built from a completely different route -- three decomposed
    flow meters summed -- and lands on the same number to 0.00% on all three
    Tesla-usable days. That simultaneously confirms the Tesla clamp, the
    Hr = house - T split, and the House node identity.

    2026-08-27 is excluded: that template was created the same afternoon as the
    Tesla one and is blind over the same window.
    """
    for day in TESLA_DAYS:
        r = replays[day]
        want = integrate_channel(r["doc"], r["house_excl_car"], r["end_epoch"])
        got = node_totals(replays[day]["flows"])["house"]
        assert want > 5.0, day
        assert abs(got - want) / want < 0.001, "%s %.4f vs %.4f" % (day, got, want)


def test_the_three_derived_node_states_are_exactly_their_inbound_sums(replays):
    """What the HA helper templates must compute, pinned so their Jinja and this
    Python cannot disagree about what a node IS.

    House, Tesla and Inverter have no sensor of their own -- each node's state
    must be the sum of its three daily utility_meters. Integration is linear,
    so summing three daily meters equals integrating the sum, and these
    identities carry from instantaneous watts to daily kWh unchanged.
    """
    for day in DAYS:
        f = replays[day]["flows"]
        n = node_totals(f)
        assert n["house"] == pytest.approx(
            f["solar_to_house"] + f["battery_to_house"] + f["grid_to_house"])
        assert n["tesla"] == pytest.approx(
            f["solar_to_tesla"] + f["battery_to_tesla"] + f["grid_to_tesla"])
        assert n["inverter"] == pytest.approx(
            f["solar_to_inverter"] + f["battery_to_inverter"]
            + f["grid_to_inverter"])


def test_house_gap_is_not_explained_by_the_inverter_node(replays):
    """Guards against a tidier story than the data supports.

    It is tempting to say House + Inverter reconciles to house_consumption_today
    -- the shapes look right and the magnitudes are close. They do not
    reconcile: the gap is 0.58 to 0.77 of the Inverter node, day by day, not
    1.0. Anyone who "notices" the near-agreement and writes it down as a
    finding will be wrong, so this test fails if the ratio ever looks like 1.
    """
    counters = load_counters()
    ratios = []
    for day in DAYS:
        r = replays[day]
        n = node_totals(r["flows"])
        gap = counters[day][COUNTER_KEY["house"]]["max"] - (n["house"] + n["tesla"])
        ratios.append(gap / n["inverter"])
    assert min(ratios) > 0.4 and max(ratios) < 0.9
    assert not any(0.97 < x < 1.03 for x in ratios)


def test_replay_tesla_is_charged_overwhelmingly_from_the_grid(replays):
    """Cross-check against operating policy rather than against this module.

    CLAUDE.md: the Tesla takes discrete ~7 kW Octopus slots inside the
    23:30-05:30 window, and the house battery's charge window exists partly to
    stop the pack feeding the car. If the decomposition were wrong about which
    source fed the Tesla, this is where it would show -- a large
    battery_to_tesla would contradict a policy the owner has verified.
    """
    for day in ("2026-08-29", "2026-08-30"):
        assert day in TESLA_DAYS
        f = replays[day]["flows"]
        total = f["grid_to_tesla"] + f["battery_to_tesla"] + f["solar_to_tesla"]
        assert total > 5.0, day
        assert f["grid_to_tesla"] / total > 0.95, day
        assert f["battery_to_tesla"] < 0.5, day


def test_replay_inverter_node_is_a_plausible_daily_parasitic(replays):
    """flow-law's fit predicts ~2.5 kWh/day of housekeeping. This is the number
    the brief asks for; the bound is wide because it is a measurement being
    reported, not a target being hit."""
    for day in DAYS:
        if day == PARTIAL_DAY:
            continue
        kwh = node_totals(replays[day]["flows"])["inverter"]
        assert 0.5 < kwh < 8.0, "%s %.3f kWh" % (day, kwh)


def test_replay_has_no_negative_or_nan_totals(replays):
    for day in DAYS:
        for k, v in replays[day]["flows"].items():
            assert v >= 0.0 and math.isfinite(v), "%s %s" % (day, k)


def test_replay_covers_essentially_the_whole_day(replays):
    for day in DAYS:
        r = replays[day]
        assert r["live_s"] > 0.98 * (r["live_s"] + r["gap_s"]), day


def test_replay_is_deterministic():
    a = replay_day("2026-08-29")["flows"]
    b = replay_day("2026-08-29")["flows"]
    assert a == b


def test_max_sub_interval_is_identity_for_a_left_riemann_sum():
    """The HA integration platform re-integrates every 60 s. For a left sum over
    a held value that is mathematically a no-op, and the replay must show it --
    otherwise the harness is not modelling the platform we will deploy."""
    doc = load_day("2026-08-29")
    coarse = rp.replay(doc, decompose, max_sub_interval=60.0)
    fine = rp.replay(doc, decompose, max_sub_interval=15.0)
    for k in flows.FLOWS:
        assert coarse["flows"][k] == pytest.approx(fine["flows"][k], rel=1e-9), k
    a = sum(dt for _t, dt, v in rp.intervals(doc, None, 60.0)
            if v["house"] is not None)
    b = sum(dt for _t, dt, v in rp.intervals(doc, None, 15.0)
            if v["house"] is not None)
    assert a == pytest.approx(b)


def test_the_five_minute_csv_agrees_with_the_replay_on_export():
    """An independent dataset from earlier work, quantised to 0.1 kWh. A coarse
    sanity check that the replay is not off by a factor or a sign."""
    path = os.path.join(FIX, "flow_5min_2026-08-30.csv")
    total = 0.0
    with open(path) as fh:
        header = fh.readline().strip().split(",")
        col = header.index("exp")
        for line in fh:
            parts = line.strip().split(",")
            if len(parts) > col and parts[col]:
                total += float(parts[col])
    assert total >= 0.0
    # The CSV covers 00:00-15:45 only; the replay day ends at the capture
    # instant, so the replay must be at least the CSV's total.
    r = replay_day(PARTIAL_DAY)
    assert r["flows"]["solar_to_export"] >= 0.9 * total


# --------------------------------------------------------------------------
# 12. The measurement table (run the module directly)
# --------------------------------------------------------------------------

def _table():  # pragma: no cover - reporting, exercised by the tests above
    counters = load_counters()
    for rule in ("proportional", "band_table"):
        flows.set_loss_rule(rule)
        print("\n########## LOSS_RULE = %s ##########" % rule)
        for day in DAYS:
            r = replay_day(day)
            f = r["flows"]
            n = node_totals(f)
            d = counter_facing(f)
            print("\n=== %s%s  gap %.0f s%s ===" % (
                day, " (PARTIAL)" if day == PARTIAL_DAY else "", r["gap_s"],
                "  TESLA-BLIND %.0f s" % r["tesla_assumed_s"]
                if r["blind_channels"] else ""))
            print("  %-18s %8s %8s %8s %8s %8s" % (
                "quantity", "flows", "counter", "err%", "raw-int", "vraw%"))
            for k in ("solar", "grid_import", "grid_export", "battery_charge",
                      "battery_discharge", "house"):
                c = counters[day][COUNTER_KEY[k]]["max"]
                e = pct(d[k], c)
                raw = r["raw"][k]
                ev = pct(d[k], raw)
                print("  %-18s %8.3f %8.3f %8s %8.3f %8s" % (
                    k, d[k], c, "%+.2f" % e if e is not None else "n/a", raw,
                    "%+.2f" % ev if ev is not None else "n/a"))
            tc = r["tesla_counter"]
            tw = tc["last"] - tc["first"]
            rep = reportable(r)
            house, tesla = rep["house"], rep["tesla"]
            if tesla is None:
                print("  %-18s %8s %8.3f %8s   (BLIND: sensor created 13:28; "
                      "CLAUDE.md records 8.384 kWh that night)"
                      % ("tesla node", "REFUSED", tw, "n/a"))
                print("  %-18s %8s   (unknowable while Tesla is blind)"
                      % ("house node", "REFUSED"))
            else:
                print("  %-18s %8.3f %8.3f %8s   (power-integral %.3f)" % (
                    "tesla node", tesla, tw,
                    "%+.2f" % pct(tesla, tw) if tw else "n/a", r["raw"]["tesla"]))
                print("  %-18s %8.3f  (house node, Tesla excluded)"
                      % ("house node", house))
            print("  %-18s %8.4f kWh over %d samples" % (
                "unsourced export", r["unsourced_export"], r["unsourced_samples"]))
            print("  %-18s %8.3f kWh" % ("INVERTER node", n["inverter"]))
            if tesla is not None:
                print("  tesla source split: grid %.3f  battery %.3f  solar %.3f" % (
                    f["grid_to_tesla"], f["battery_to_tesla"], f["solar_to_tesla"]))
            print("  source spend: solar %.3f  battery %.3f  grid %.3f" % (
                n["solar_spent"], n["battery_spent"], n["grid_spent"]))
            print("  inverter split: solar %.3f  battery %.3f  grid %.3f" % (
                f["solar_to_inverter"], f["battery_to_inverter"], f["grid_to_inverter"]))
    flows.set_loss_rule("proportional")


if __name__ == "__main__":  # pragma: no cover
    _table()
