"""Regime tests for decompose(): one test per situation this inverter actually
gets into, at magnitudes this site actually produces.

Site facts these numbers come from CLAUDE.md: 6 kW hybrid inverter, three Fox
LV5200 in parallel (~15.4 kWh, 5 kW delivery), 43141 = 50 A so the timed
window charges at ~2.65 kW at 53 V, house baseline ~660 W overnight, Tesla
takes discrete ~7 kW slots, cheap window 23:30-05:30.

Every regime also asserts the documented structural constraint: all three
discharge windows are unset, so the battery can never export.
"""
import math

import pytest

from contract import FLOWS
from decompose import (
    ETA,
    LOW_BAND_ETA,
    PARASITIC_W,
    REFERRAL_SLOPE,
    BATTERY_RULES,
    ETA_FLOOR,
    LOSS_TREATMENTS,
    MAX_PLAUSIBLE_W,
    BadReading,
    decompose,
    set_battery_rule,
    set_loss_treatment,
)

TOL = 1e-6

# The site's recurring magnitudes.
BASELINE = 660.0        # overnight house load, W
CHARGE_50A = 2650.0     # 50 A at 53 V, the timed-window charge rate, W
TESLA = 7000.0          # one Tesla dispatch slot, W
CLIP_AC = 6000.0        # inverter AC ceiling, W
BATT_MAX = 5000.0       # battery delivery ceiling, W


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _num(x):
    """Same tolerance decompose() applies, so the shared checks can run on the
    malformed-input regimes too."""
    try:
        v = float(x)
    except (TypeError, ValueError):
        return 0.0
    return v if math.isfinite(v) else 0.0


def law(solar, battery, grid, house):
    """decompose() plus the checks that must hold in every single regime."""
    f = decompose(solar, battery, grid, house)

    assert set(f) == set(FLOWS), "flow keys are the contract; do not add or drop"
    for k, v in f.items():
        assert v >= 0.0, "%s went negative: %r" % (k, v)
        assert math.isfinite(v), "%s is not finite: %r" % (k, v)

    # The battery can never export: there is no key for it, and its only
    # outflow is bounded by what it actually discharged.
    assert "battery_to_grid" not in f
    assert f["battery_to_house"] <= max(0.0, -_num(battery)) + TOL

    return f


def sinks(f):
    """What each sink was told it received."""
    return {
        "house": f["solar_to_house"] + f["battery_to_house"] + f["grid_to_house"],
        "battery": f["solar_to_battery"] + f["grid_to_battery"],
        "export": f["solar_to_export"],
    }


def solar_spend(f):
    return f["solar_to_house"] + f["solar_to_battery"] + f["solar_to_export"]


def grid_spend(f):
    return f["grid_to_house"] + f["grid_to_battery"]


def arrives(discharge):
    """What the AC referral says actually reaches the house from a DC
    discharge. Hand-check: 660 W DC -> 552.4 W AC, 100 W DC -> 22.0 W (the
    measured low-band floor, where the line would say nothing arrives)."""
    return max(LOW_BAND_ETA * discharge, REFERRAL_SLOPE * discharge - PARASITIC_W)


def assert_sinks_filled(f, house, battery, grid):
    """Battery-in and export are still filled exactly -- both are measured
    sinks on the same side of the converter as the sources that feed them.

    House is deliberately NOT asserted here any more. It is no longer a
    determined quantity: metered import lands in full (so House can exceed
    house_load), while the battery is credited only with what survives the
    ~106 W parasitic (so House can fall short of it). The owner settled this
    on 2026-08-30 by redefining House on the dashboard as the sum of its three
    inbound flow meters, which makes house_load an input to the comparison
    rather than the truth the flows must reproduce.
    """
    s = sinks(f)
    assert s["battery"] == pytest.approx(max(0.0, battery), abs=TOL)
    assert s["export"] == pytest.approx(max(0.0, -grid), abs=TOL)


# --------------------------------------------------------------------------
# quiescent
# --------------------------------------------------------------------------

def test_deep_night_nothing_happening():
    f = law(0, 0, 0, 0)
    assert all(v == 0.0 for v in f.values())


def test_deep_night_nothing_happening_no_battery_export():
    f = law(0, 0, 0, 0)
    assert f["battery_to_house"] == 0.0
    assert f["solar_to_export"] == 0.0


def test_deep_night_baseline_on_grid():
    f = law(0, 0, BASELINE, BASELINE)
    assert f["grid_to_house"] == pytest.approx(BASELINE)
    assert f["solar_to_house"] == 0.0
    assert f["battery_to_house"] == 0.0
    assert_sinks_filled(f, BASELINE, 0, BASELINE)


def test_deep_night_baseline_on_battery():
    f = law(0, -BASELINE, 0, BASELINE)
    assert f["battery_to_house"] == pytest.approx(552.384)
    assert f["battery_to_house"] == pytest.approx(arrives(BASELINE))
    assert f["grid_to_house"] == 0.0
    assert f["solar_to_export"] == 0.0
    # The ~108 W the parasitic ate is left unspent on the Battery-out bar.
    assert BASELINE - f["battery_to_house"] == pytest.approx(107.6, abs=0.1)


def test_deep_night_baseline_split_battery_and_grid():
    f = law(0, -400, 260, BASELINE)
    assert f["battery_to_house"] == pytest.approx(arrives(400))
    assert f["grid_to_house"] == pytest.approx(260)
    assert f["solar_to_house"] == 0.0
    assert_sinks_filled(f, BASELINE, 0, 260)


def test_deep_night_battery_takes_priority_over_grid_for_the_load():
    """Battery discharge has exactly one legal outlet, so it is spent first.
    Ordering it after grid import is what stranded the Battery-out box on
    2026-08-28."""
    f = law(0, -1200, 300, 1500)
    assert f["battery_to_house"] == pytest.approx(arrives(1200))
    assert f["grid_to_house"] == pytest.approx(300)


# --------------------------------------------------------------------------
# the timed cheap window, 23:30-05:30
# --------------------------------------------------------------------------

def test_window_grid_charges_battery():
    imp = BASELINE + CHARGE_50A / ETA
    f = law(0, CHARGE_50A, imp, BASELINE)
    assert f["grid_to_battery"] == pytest.approx(CHARGE_50A)
    assert f["grid_to_house"] >= BASELINE


def test_window_battery_is_not_discharging_to_the_house():
    imp = BASELINE + CHARGE_50A / ETA
    f = law(0, CHARGE_50A, imp, BASELINE)
    assert f["battery_to_house"] == 0.0


def test_window_no_solar_contribution_at_night():
    imp = BASELINE + CHARGE_50A / ETA
    f = law(0, CHARGE_50A, imp, BASELINE)
    assert f["solar_to_house"] == 0.0
    assert f["solar_to_battery"] == 0.0
    assert f["solar_to_export"] == 0.0


def test_window_import_is_spent_in_full_and_the_loss_lands_on_the_house():
    """Drawing 2760 W AC to store 2650 W DC means ~110 W went somewhere other
    than the battery. The meter is authoritative, so it is not left unspent:
    it lands on the house, where the card clamps it against the house counter.
    Leaving it on the grid bar instead cost 6.3-9.8% of daily import."""
    imp = BASELINE + CHARGE_50A / ETA
    f = law(0, CHARGE_50A, imp, BASELINE)
    assert grid_spend(f) == pytest.approx(imp)
    excess = f["grid_to_house"] - BASELINE
    assert excess == pytest.approx(CHARGE_50A / ETA - CHARGE_50A, abs=1.0)
    assert 100 < excess < 130


def test_window_sinks_filled_exactly():
    imp = BASELINE + CHARGE_50A / ETA
    f = law(0, CHARGE_50A, imp, BASELINE)
    assert_sinks_filled(f, BASELINE, CHARGE_50A, imp)


def test_window_lower_charge_current_30a():
    charge = 30 * 53.0
    imp = BASELINE + charge / ETA
    f = law(0, charge, imp, BASELINE)
    assert f["grid_to_battery"] == pytest.approx(charge)
    assert f["battery_to_house"] == 0.0


def test_window_battery_nearly_full_taper():
    """Near 97-98% the BMS tapers; the window then barely charges."""
    imp = BASELINE + 313
    f = law(0, 300, imp, BASELINE)
    assert f["grid_to_battery"] == pytest.approx(300)
    assert f["grid_to_house"] == pytest.approx(imp - 300)
    assert grid_spend(f) == pytest.approx(imp)


def test_window_with_tesla_slot():
    house = BASELINE + TESLA
    imp = house + CHARGE_50A / ETA
    f = law(0, CHARGE_50A, imp, house)
    assert f["grid_to_house"] >= house
    assert f["grid_to_battery"] == pytest.approx(CHARGE_50A)
    assert grid_spend(f) == pytest.approx(imp)


def test_window_with_tesla_battery_is_not_feeding_the_car():
    """The whole point of holding the window open across 23:30-05:30: the
    house pack must not discharge into an unpredictably scheduled Tesla slot."""
    house = BASELINE + TESLA
    imp = house + CHARGE_50A / ETA
    f = law(0, CHARGE_50A, imp, house)
    assert f["battery_to_house"] == 0.0
    assert f["solar_to_house"] == 0.0


def test_window_with_tesla_at_the_stated_96kw_shape():
    f = law(0, CHARGE_50A, 9600, 6950)
    assert f["grid_to_house"] == pytest.approx(6950)
    assert f["grid_to_battery"] == pytest.approx(CHARGE_50A)
    assert f["battery_to_house"] == 0.0


def test_window_tesla_slot_ends_battery_still_charging():
    imp = BASELINE + CHARGE_50A / ETA
    f = law(0, CHARGE_50A, imp, BASELINE)
    assert f["grid_to_house"] < 1000, "Tesla gone, only the baseline remains"


def test_window_self_use_not_yet_started_battery_idle():
    f = law(0, 0, BASELINE, BASELINE)
    assert f["grid_to_battery"] == 0.0
    assert f["solar_to_battery"] == 0.0


def test_window_2500w_charge_is_the_house_pack_not_the_car():
    """A 2.5 kW draw is the pack at 50 A; a 7 kW draw is the car. Never
    attribute one to the other."""
    imp = CHARGE_50A / ETA
    f = law(0, CHARGE_50A, imp, 0)
    assert f["grid_to_battery"] == pytest.approx(CHARGE_50A)
    assert f["grid_to_house"] == pytest.approx(imp - CHARGE_50A)


# --------------------------------------------------------------------------
# dawn and dusk: solar present but under the baseline
# --------------------------------------------------------------------------

def test_dawn_tiny_solar_under_baseline():
    f = law(40, 0, 620, BASELINE)
    assert f["grid_to_house"] == pytest.approx(620)
    assert f["solar_to_house"] == pytest.approx(40)
    assert f["solar_to_export"] == 0.0


def test_dawn_tiny_solar_never_exports():
    f = law(40, 0, 620, BASELINE)
    assert f["solar_to_export"] == 0.0
    assert f["solar_to_battery"] == 0.0


def test_dusk_solar_with_battery_covering_the_rest():
    f = law(120, -540, 0, BASELINE)
    assert f["battery_to_house"] == pytest.approx(arrives(540))
    assert f["grid_to_house"] == 0.0


def test_post_sunset_16w_is_correct_not_a_fault():
    """16 W at 20:15 with sunset 20:08 is real, not a broken sensor."""
    f = law(16, -644, 0, BASELINE)
    assert f["battery_to_house"] == pytest.approx(arrives(644))


def test_dawn_solar_all_to_house_no_surplus():
    f = law(300, 0, 360, BASELINE)
    assert_sinks_filled(f, BASELINE, 0, 360)
    assert f["solar_to_house"] == pytest.approx(300)


def test_dusk_solar_and_grid_together():
    f = law(200, 0, 460, BASELINE)
    assert f["solar_to_house"] + f["grid_to_house"] == pytest.approx(BASELINE)
    assert f["battery_to_house"] == 0.0


# --------------------------------------------------------------------------
# solar exactly equal to the house load
# --------------------------------------------------------------------------

def test_solar_exactly_equals_house():
    f = law(1800, 0, 0, 1800)
    assert f["solar_to_house"] == pytest.approx(1800)
    assert f["grid_to_house"] == 0.0
    assert f["battery_to_house"] == 0.0
    assert f["solar_to_export"] == 0.0
    assert f["solar_to_battery"] == 0.0


def test_solar_equals_house_the_loss_shows_as_a_small_import():
    """Physically, DC solar equal to the AC load leaves a small import: the
    converter takes its cut. The import is attributed, the sinks stay exact."""
    imp = 1800 * (1 - ETA)
    f = law(1800, 0, imp, 1800)
    assert f["grid_to_house"] == pytest.approx(imp)
    assert f["solar_to_house"] == pytest.approx(1800 - imp)
    assert_sinks_filled(f, 1800, 0, imp)


def test_solar_equals_house_solar_spend_never_exceeds_the_reading():
    imp = 1800 * (1 - ETA)
    f = law(1800, 0, imp, 1800)
    assert solar_spend(f) <= 1800 + TOL


# --------------------------------------------------------------------------
# solar surplus
# --------------------------------------------------------------------------

def test_surplus_charges_battery_no_export():
    f = law(4300, 3400, 0, 830)
    assert f["solar_to_battery"] == pytest.approx(3400)
    assert f["solar_to_house"] == pytest.approx(830)
    assert f["solar_to_export"] == 0.0


def test_surplus_charges_battery_leaves_grid_untouched():
    f = law(4300, 3400, 0, 830)
    assert f["grid_to_house"] == 0.0
    assert f["grid_to_battery"] == 0.0


def test_surplus_exports_battery_full():
    f = law(5400, 0, -4300, 900)
    assert f["solar_to_export"] == pytest.approx(4300)
    assert f["solar_to_house"] == pytest.approx(900)
    assert f["solar_to_battery"] == 0.0


def test_surplus_export_is_attributed_entirely_to_solar():
    """No other source may feed export. Battery-to-grid is forbidden and
    import-straight-back-out is not physical here, so export is solar's by
    elimination."""
    f = law(5400, 0, -4300, 900)
    assert f["solar_to_export"] == pytest.approx(-(-4300))
    assert sinks(f)["export"] == pytest.approx(4300)


def test_surplus_split_between_battery_and_export():
    f = law(5800, 2650, -2200, 800)
    assert f["solar_to_battery"] == pytest.approx(2650)
    assert f["solar_to_export"] == pytest.approx(2200)
    assert f["solar_to_house"] == pytest.approx(800)


def test_surplus_split_no_grid_import_invented():
    f = law(5800, 2650, -2200, 800)
    assert grid_spend(f) == 0.0


def test_surplus_battery_at_5kw_delivery_ceiling():
    f = law(5900, BATT_MAX, 0, 700)
    assert f["solar_to_battery"] == pytest.approx(BATT_MAX)
    assert f["solar_to_house"] == pytest.approx(700)


def test_surplus_bright_midday_house_low():
    f = law(5950, 2650, -2500, 620)
    assert_sinks_filled(f, 620, 2650, -2500)
    assert f["battery_to_house"] == 0.0


def test_surplus_solar_spend_stays_within_the_reading():
    for args in [(4300, 3400, 0, 830), (5400, 0, -4300, 900), (5800, 2650, -2200, 800)]:
        f = law(*args)
        assert solar_spend(f) <= args[0] + TOL


# --------------------------------------------------------------------------
# solar deficit
# --------------------------------------------------------------------------

def test_deficit_covered_by_battery():
    f = law(950, -1600, 0, 2500)
    assert f["battery_to_house"] == pytest.approx(arrives(1600))
    assert f["grid_to_house"] == 0.0


def test_deficit_covered_by_battery_and_grid():
    f = law(950, -2400, 1500, 4800)
    assert f["battery_to_house"] == pytest.approx(arrives(2400))
    assert f["grid_to_house"] == pytest.approx(1500)
    assert_sinks_filled(f, 4800, 0, 1500)


def test_deficit_grid_only_battery_at_floor():
    """Below the 10% floor the pack stops; everything falls to the grid."""
    f = law(0, 0, 3200, 3200)
    assert f["grid_to_house"] == pytest.approx(3200)
    assert f["battery_to_house"] == 0.0


def test_deficit_battery_at_5kw_ceiling_grid_tops_up():
    f = law(0, -BATT_MAX, 1800, 6800)
    assert f["battery_to_house"] == pytest.approx(arrives(BATT_MAX))
    assert f["grid_to_house"] == pytest.approx(1800)
    # 97.5% at 5 kW: the parasitic barely registers at full delivery.
    assert f["battery_to_house"] / BATT_MAX > 0.97


def test_deficit_evening_peak_cooking():
    f = law(0, -3400, 900, 4300)
    assert_sinks_filled(f, 4300, 0, 900)
    assert f["solar_to_house"] == 0.0


def test_deficit_battery_reading_exceeds_the_load():
    """Discharge above the house load would mean battery-to-grid. The excess
    is refused: battery_to_house is capped at the load."""
    f = law(0, -2000, 0, 500)
    assert f["battery_to_house"] == pytest.approx(500)
    assert sum(f.values()) == pytest.approx(500)


def test_deficit_cloud_passing_battery_picks_up():
    f = law(1400, -2000, 0, 3400)
    assert f["battery_to_house"] == pytest.approx(arrives(2000))


# --------------------------------------------------------------------------
# inverter clipping at 6 kW AC
# --------------------------------------------------------------------------

def test_clipping_at_6kw_ac_exporting():
    f = law(6900, 0, -(CLIP_AC - 700), 700)
    assert f["solar_to_export"] == pytest.approx(CLIP_AC - 700)
    assert f["solar_to_house"] == pytest.approx(700)


def test_clipping_ac_side_sums_to_the_ceiling():
    f = law(6900, 0, -(CLIP_AC - 700), 700)
    ac_out = f["solar_to_house"] + f["solar_to_export"]
    assert ac_out == pytest.approx(CLIP_AC)


def test_clipping_relieved_by_charging_the_battery():
    """Charging is DC-to-DC, so it sidesteps the AC ceiling: 6 kW out plus
    2.65 kW into the pack from a 8.6 kW DC array."""
    f = law(8900, CHARGE_50A, -(CLIP_AC - 700), 700)
    assert f["solar_to_battery"] == pytest.approx(CHARGE_50A)
    assert f["solar_to_house"] + f["solar_to_export"] == pytest.approx(CLIP_AC)


def test_clipping_never_produces_battery_export():
    f = law(8900, CHARGE_50A, -(CLIP_AC - 700), 700)
    assert f["battery_to_house"] == 0.0
    assert "battery_to_grid" not in f


def test_clipping_solar_unspent_remainder_is_the_clipped_dc():
    f = law(6900, 0, -(CLIP_AC - 700), 700)
    assert 6900 - solar_spend(f) == pytest.approx(900)


# --------------------------------------------------------------------------
# battery idle at 0 W
# --------------------------------------------------------------------------

def test_battery_idle_solar_and_load_both_nonzero():
    f = law(2400, 0, 0, 2400)
    assert f["battery_to_house"] == 0.0
    assert f["solar_to_battery"] == 0.0
    assert f["grid_to_battery"] == 0.0
    assert f["solar_to_house"] == pytest.approx(2400)


def test_battery_idle_while_exporting():
    f = law(3600, 0, -2800, 800)
    assert f["battery_to_house"] == 0.0
    assert f["solar_to_export"] == pytest.approx(2800)


def test_battery_idle_while_importing():
    f = law(1200, 0, 1500, 2700)
    assert f["battery_to_house"] == 0.0
    assert f["grid_to_house"] == pytest.approx(1500)
    assert f["solar_to_house"] == pytest.approx(1200)


def test_battery_idle_no_flow_touches_the_battery():
    f = law(2400, 0, 0, 2400)
    assert f["solar_to_battery"] + f["grid_to_battery"] + f["battery_to_house"] == 0.0


# --------------------------------------------------------------------------
# the live sample, and what the DC/AC loss does to it
# --------------------------------------------------------------------------

LIVE = dict(solar=2982, battery=-457, grid=-98, house=3078)


def test_live_sample_export_is_not_understated():
    """The bug this module exists to fix. The provisional law returned
    solar_to_export = 0 because DC solar was fully absorbed by the AC house
    load, while the meter measured 98 W leaving the property."""
    f = law(**LIVE)
    assert f["solar_to_export"] == pytest.approx(98)


def test_live_sample_full_resolution():
    f = law(**LIVE)
    assert f["battery_to_house"] == pytest.approx(349.912, abs=1e-3)
    assert f["solar_to_house"] == pytest.approx(3078 - 349.912, abs=1e-3)
    assert f["solar_to_export"] == pytest.approx(98)
    assert f["solar_to_battery"] == 0.0
    assert f["grid_to_house"] == 0.0
    assert f["grid_to_battery"] == 0.0


def test_live_sample_battery_credited_with_what_arrives_not_what_it_emitted():
    """457 W DC leaves the pack; 350 W reaches the house. The 107 W difference
    is the fixed parasitic, and it stays visible as an unspent Battery-out
    remainder rather than being credited to the house."""
    f = law(**LIVE)
    assert f["battery_to_house"] == pytest.approx(arrives(457))
    assert 457 - f["battery_to_house"] == pytest.approx(107.1, abs=0.1)


def test_live_sample_sinks_are_exact():
    f = law(**LIVE)
    assert_sinks_filled(f, LIVE["house"], LIVE["battery"], LIVE["grid"])


def test_live_sample_loss_lands_as_unspent_solar():
    """Solar still ends up with an unspent remainder; it is smaller now,
    because the battery's share of the parasitic moved off the battery and
    onto solar when the battery stopped being credited with it."""
    f = law(**LIVE)
    assert LIVE["solar"] - solar_spend(f) == pytest.approx(155.9, abs=0.1)
    assert 0 < LIVE["solar"] - solar_spend(f) < 263


def test_live_sample_solar_spend_equals_the_derived_ac_equivalent_under_load_capped():
    """The identity solar_spend == house - grid + battery holds only while the
    battery is credited with its full DC discharge. AC referral deliberately
    breaks it: what the battery does not deliver has to come from somewhere,
    and solar is the elastic source."""
    previous = set_battery_rule("load_capped")
    try:
        f = decompose(**LIVE)
        derived = LIVE["house"] - LIVE["grid"] + LIVE["battery"]
        assert solar_spend(f) == pytest.approx(derived)
    finally:
        set_battery_rule(previous)


def test_live_sample_no_battery_export():
    f = law(**LIVE)
    assert "battery_to_grid" not in f
    assert f["battery_to_house"] <= 457 + TOL


# --------------------------------------------------------------------------
# impossible and malformed samples
# --------------------------------------------------------------------------

def test_simultaneous_import_and_export_is_unrepresentable():
    """grid is one signed sensor, so both directions at once cannot be
    expressed. Whichever sign arrives is handled; the other is zero."""
    imp = law(0, 0, 500, 500)
    exp = law(1000, 0, -500, 500)
    assert imp["grid_to_house"] == pytest.approx(500)
    assert imp["solar_to_export"] == 0.0
    assert exp["solar_to_export"] == pytest.approx(500)
    assert grid_spend(exp) == 0.0


def test_signed_zero_grid_is_treated_as_neither_direction():
    f = law(1000, 0, -0.0, 1000)
    assert f["solar_to_export"] == 0.0
    assert grid_spend(f) == 0.0


def test_contradictory_battery_exporting_is_refused():
    """Discharging 2 kW into a 500 W house with 1.5 kW leaving the meter would
    be battery-to-grid. There is no key for it, and solar generated nothing, so
    the export is attributed to nobody rather than to a forbidden ribbon."""
    f = law(0, -2000, -1500, 500)
    assert f["battery_to_house"] == pytest.approx(500)
    assert f["solar_to_export"] == 0.0
    assert solar_spend(f) == 0.0


def test_negative_solar_is_clamped():
    f = law(-50, 0, 700, 700)
    assert f["solar_to_house"] == 0.0
    assert f["grid_to_house"] == pytest.approx(700)


def test_negative_house_is_clamped():
    f = law(1000, 0, -1000, -20)
    assert f["solar_to_house"] == 0.0
    assert f["solar_to_export"] == pytest.approx(1000)


@pytest.mark.parametrize("position", range(4))
def test_nan_raises_in_every_position(position):
    """float("nan") parses, and max(0.0, nan) then returns 0.0, so without an
    explicit guard a broken sensor decomposes byte-identically to an idle one
    and the chart draws a confident wrong picture."""
    args = [2982, -457, -98, 3078]
    args[position] = float("nan")
    with pytest.raises(BadReading):
        decompose(*args)


@pytest.mark.parametrize("bad", [
    None, "unavailable", "unknown", "", "   ", "\t", "\n", "nan", "NaN",
    [], {}, (0,), object(), True, False,
])
@pytest.mark.parametrize("position", range(4))
def test_absent_readings_raise_in_every_position(position, bad):
    """A template sensor that throws renders `unavailable`; one that returns 0
    renders a confident lie. The logger allows one Modbus session at a time, so
    contention -- not darkness -- is the usual reason a reading is missing."""
    args = [2982, -457, -98, 3078]
    args[position] = bad
    with pytest.raises(BadReading):
        decompose(*args)


def test_bad_reading_is_catchable_as_either_builtin():
    assert issubclass(BadReading, ValueError)
    assert issubclass(BadReading, TypeError)
    with pytest.raises(ValueError):
        decompose(0, 0, "unavailable", 0)
    with pytest.raises(TypeError):
        decompose(0, 0, None, 0)


def test_infinite_input_raises():
    with pytest.raises(BadReading):
        decompose(float("inf"), 0, 0, 500)


def test_numeric_strings_are_the_normal_path():
    """HA states are always strings."""
    f = law("2982", "-457", "-98", "3078")
    assert f["battery_to_house"] == pytest.approx(arrives(457))
    assert f["solar_to_export"] == pytest.approx(98)


@pytest.mark.parametrize("text", ["3000", " 3000 ", "3e3", "3000.0", "+3000"])
def test_string_number_formats_all_parse(text):
    f = law("0", "0", text, text)
    assert f["grid_to_house"] == pytest.approx(3000)


def test_u32_sign_misread_is_rejected_not_clamped():
    """CLAUDE.md records this exact misread: register 33257 read unsigned gives
    4294967253 = 2^32 - 43 when the truth is -43 W. 4.29 GW of grid import must
    not be quietly clamped into a plausible-looking chart."""
    with pytest.raises(BadReading):
        decompose(0, 0, 4294967253, 500)


def test_u32_misread_does_not_resolve_like_the_true_reading():
    """The companion assertion: rejecting it must not be indistinguishable from
    accepting it. The true -43 W decomposes fine."""
    true_reading = law(0, 0, -43, 500)
    assert true_reading["solar_to_export"] == 0.0  # solar is 0 at the time
    with pytest.raises(BadReading):
        decompose(0, 0, 4294967253, 500)


@pytest.mark.parametrize("position", range(4))
def test_implausible_magnitude_raises_in_every_position(position):
    args = [0, 0, 0, 0]
    args[position] = MAX_PLAUSIBLE_W * 10
    with pytest.raises(BadReading):
        decompose(*args)


def test_plausible_site_peak_is_accepted():
    """The ceiling must not reject anything this site can actually do: a Tesla
    slot plus the baseline plus the timed charge is about 10.4 kW."""
    house = BASELINE + TESLA
    f = law(0, CHARGE_50A, house + CHARGE_50A / ETA, house)
    assert grid_spend(f) > 9000


def test_sinks_exceeding_sources_never_goes_negative():
    """Physically inconsistent, but a skewed 10 s poll can produce it."""
    f = law(100, 3000, 0, 3000)
    assert all(v >= 0.0 for v in f.values())


# --------------------------------------------------------------------------
# the bound on solar overshoot, and the night hard-zero
# --------------------------------------------------------------------------

def test_no_solar_flows_when_solar_is_zero():
    """Unconditional. Metered export at night belongs to nobody, not to a
    source that generated nothing."""
    for args in [(0, 0, -500, 0), (0, 0, -500, 500), (0, -2000, -1500, 500),
                 (0, CHARGE_50A, 3400, BASELINE), (0, 0, 0, 0)]:
        f = law(*args)
        assert f["solar_to_house"] == 0.0, args
        assert f["solar_to_battery"] == 0.0, args
        assert f["solar_to_export"] == 0.0, args


def test_night_export_is_attributed_to_nobody():
    f = law(0, 0, -500, 0)
    assert sum(f.values()) == 0.0


def test_solar_overshoot_is_bounded_by_eta_floor():
    """A stale solar reading may be credited with covering a plausible
    conversion loss, but not with arbitrary energy."""
    f = law(1000, 0, -4000, 500)
    assert solar_spend(f) == pytest.approx(1000 / ETA_FLOOR)
    assert solar_spend(f) < 1200


def test_solar_overshoot_bound_trims_battery_charge_first():
    f = law(100, 3000, 0, 3000)
    assert f["solar_to_battery"] == 0.0
    assert solar_spend(f) == pytest.approx(100 / ETA_FLOOR)


def test_solar_overshoot_bound_keeps_the_export_ribbon_last():
    """Export is trimmed last: a vanished export ribbon is the failure this
    module exists to fix."""
    f = law(1000, 0, -4000, 500)
    assert f["solar_to_export"] > 0
    assert f["solar_to_house"] == 0.0


def test_solar_spend_stays_within_the_eta_floor_bound():
    """Solar may now exceed its own reading even on a well-formed sample: AC
    referral moves the battery's parasitic onto solar, which is the elastic
    source. The bound that still holds is the ETA_FLOOR one."""
    for args in REGIME_ARGS:
        f = law(*args)
        assert solar_spend(f) <= args[0] / ETA_FLOOR + TOL, args


def test_live_sample_is_well_inside_the_bound():
    f = law(**LIVE)
    assert solar_spend(f) < LIVE["solar"] / ETA_FLOOR


# --------------------------------------------------------------------------
# invariants swept across every regime above
# --------------------------------------------------------------------------

REGIMES = [
    ("deep night idle", 0, 0, 0, 0),
    ("night baseline on grid", 0, 0, BASELINE, BASELINE),
    ("night baseline on battery", 0, -BASELINE, 0, BASELINE),
    ("timed window", 0, CHARGE_50A, BASELINE + CHARGE_50A / ETA, BASELINE),
    ("timed window + tesla", 0, CHARGE_50A, BASELINE + TESLA + CHARGE_50A / ETA,
     BASELINE + TESLA),
    ("dawn under baseline", 40, 0, 620, BASELINE),
    ("dusk on battery", 120, -540, 0, BASELINE),
    ("solar equals house", 1800, 0, 0, 1800),
    ("surplus to battery", 4300, 3400, 0, 830),
    ("surplus to export", 5400, 0, -4300, 900),
    ("surplus split", 5800, 2650, -2200, 800),
    ("deficit on battery", 950, -1600, 0, 2500),
    ("deficit on battery + grid", 950, -2400, 1500, 4800),
    ("clipping", 6900, 0, -(CLIP_AC - 700), 700),
    ("battery idle", 2400, 0, 0, 2400),
    ("live sample", 2982, -457, -98, 3078),
]

REGIME_IDS = [r[0] for r in REGIMES]
REGIME_ARGS = [r[1:] for r in REGIMES]


@pytest.mark.parametrize("args", REGIME_ARGS, ids=REGIME_IDS)
def test_regime_flows_are_non_negative_and_finite(args):
    law(*args)


@pytest.mark.parametrize("args", REGIME_ARGS, ids=REGIME_IDS)
def test_regime_battery_never_exports(args):
    solar, battery, grid, house = args
    f = law(*args)
    assert "battery_to_grid" not in f
    # The battery's only outflow, and it can never exceed the house load.
    assert f["battery_to_house"] <= max(0.0, house) + TOL
    assert f["battery_to_house"] <= max(0.0, -battery) + TOL


@pytest.mark.parametrize("args", REGIME_ARGS, ids=REGIME_IDS)
def test_regime_sinks_are_filled_exactly(args):
    solar, battery, grid, house = args
    assert_sinks_filled(law(*args), house, battery, grid)


@pytest.mark.parametrize("args", REGIME_ARGS, ids=REGIME_IDS)
def test_regime_solar_spend_stays_within_the_bound(args):
    f = law(*args)
    assert solar_spend(f) <= args[0] / ETA_FLOOR + TOL


@pytest.mark.parametrize("args", REGIME_ARGS, ids=REGIME_IDS)
def test_regime_metered_import_is_spent_in_full(args):
    """The smart meter is authoritative and its reading must land somewhere.
    Sizing grid_to_house against a house-derived deficit instead leaked
    6.3-9.8% of daily import, in the direction the chart cannot detect."""
    f = law(*args)
    assert grid_spend(f) == pytest.approx(max(0.0, args[2]), abs=TOL)


@pytest.mark.parametrize("args", REGIME_ARGS, ids=REGIME_IDS)
def test_regime_grid_spend_never_exceeds_the_import_reading(args):
    f = law(*args)
    assert grid_spend(f) <= max(0.0, args[2]) + TOL


# --------------------------------------------------------------------------
# the swappable loss treatment
# --------------------------------------------------------------------------

@pytest.fixture
def treatment():
    previous = None

    def use(name):
        nonlocal previous
        previous = set_loss_treatment(name)

    yield use
    if previous is not None:
        set_loss_treatment(previous)


@pytest.mark.parametrize("name", sorted(LOSS_TREATMENTS))
def test_every_treatment_keeps_the_invariants(name, treatment):
    treatment(name)
    for args in REGIME_ARGS:
        f = decompose(*args)
        assert set(f) == set(FLOWS)
        assert all(v >= 0.0 and math.isfinite(v) for v in f.values())
        assert "battery_to_grid" not in f


def test_derived_treatment_agrees_with_the_default_under_load_capped(treatment):
    """The two loss treatments coincide only while the battery is paid its
    full DC discharge; AC referral pushes solar past the derived cap."""
    previous = set_battery_rule("load_capped")
    try:
        default = decompose(**LIVE)
        treatment("derived_solar_ac")
        assert decompose(**LIVE) == pytest.approx(default)
    finally:
        set_battery_rule(previous)


def test_battery_rule_seam_offers_both_rules():
    assert set(BATTERY_RULES) == {"ac_referral", "load_capped"}


def test_load_capped_credits_more_than_arrives(treatment):
    """Kept as the documented alternative, and this is why it is not the
    default: it credits the house with the full DC discharge."""
    previous = set_battery_rule("load_capped")
    try:
        f = decompose(**LIVE)
        assert f["battery_to_house"] == pytest.approx(457)
        assert f["battery_to_house"] > arrives(457)
    finally:
        set_battery_rule(previous)


def test_unknown_battery_rule_is_rejected():
    with pytest.raises(ValueError):
        set_battery_rule("pay_it_all")


def test_fixed_efficiency_understates_the_battery_ribbon(treatment):
    """Recorded here as the reason it is not the default: scaling discharge to
    an AC equivalent strands part of the Battery-out box."""
    treatment("fixed_efficiency")
    f = decompose(**LIVE)
    assert f["battery_to_house"] < 457


def test_unknown_treatment_is_rejected():
    with pytest.raises(ValueError):
        set_loss_treatment("wishful_thinking")
