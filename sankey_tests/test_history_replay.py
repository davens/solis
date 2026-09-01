"""Replay REAL recorded inverter history through the v2 flow decomposition and
check it against the inverter's own daily energy counters.

Everything here runs OFFLINE from `fixtures/`. Nothing in this file talks to
Home Assistant; re-capturing is a separate, explicit step:

    uv run --no-project python fixtures/capture.py          # needs network
    uv run --no-project python fixtures/replay.py           # prints the table

Run the suite:

    uv run --no-project --with pytest python -m pytest sankey_tests/test_history_replay.py -q

What the fixtures are
---------------------
`fixtures/history_<day>.json.gz` holds raw `/api/history/period` states (the
integration's ~10 s poll, `significant_changes_only=0`) for the four live power
sensors, over one Europe/London local day. `fixtures/tesla_history_<day>.json.gz`
is a SEPARATE, later capture holding the car's charging power and its own daily
energy counter. `fixtures/counters.json` holds the end-of-day value of the six
inverter daily counters, taken as the max over the day because they are
`total_increasing` and reset at inverter local midnight (~23:59:52 BST).

`fixtures/flow_5min_2026-08-30.csv` is an INDEPENDENT dataset from earlier work:
190 five-minute buckets for 2026-08-30 00:00-15:45. Every column except `tesla`
is quantised to 0.1 kWh and `tesla` to a 1.2 kW quantum, so it is a coarse
sanity check on shape and sign, NOT ground truth. It is used that way here.

What this suite does and does not prove
---------------------------------------
It proves the *arithmetic* chain: recorded power -> left-Riemann integral ->
twelve directed flows -> the daily counters. It cannot prove the attribution
itself, because the inverter never measures which source fed which sink
(CLAUDE.md, "Sankey: presentation, not measurement"). A flow that reconciles to
its counter is consistent, not verified.

What changed from v1
--------------------
v1 referred DC to AC with fitted constants and could not close: it left a
documented 2.5-3.1 kWh/day residual with nowhere to put it, which surfaced as
"leaks" -- grid_to_* missing grid_import_today by up to 10%, battery_to_house
missing battery_discharge_today by up to 18%. Several tests here existed only to
bound those leaks.

v2 has no fitted constant and a named Inverter node that IS the residual, so
those leaks are structurally impossible and the tests that bounded them have no
subject. They are marked skip pending the lead's confirmation to delete rather
than being quietly removed; each one names its v2 replacement.
"""
import csv
import gzip
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures"))

from flows import FLOWS, decompose  # noqa: E402  (owned by another agent; test the contract)
import replay  # noqa: E402  fixtures/replay.py - the offline integrator

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")

# Days captured. The integration only went live 2026-08-26 ~15:55 UTC, so these
# are every complete day the recorder holds, plus the running day.
FULL_DAYS = ("2026-08-27", "2026-08-28", "2026-08-29")
PARTIAL_DAY = "2026-08-30"
ALL_DAYS = FULL_DAYS + (PARTIAL_DAY,)

# The Tesla channel does NOT cover every day. Measured from the fixture:
# 2026-08-27 carries a single sample, at 13:27 local, and 48 049 s of the day
# (55.6%) precede it. The other three days are complete from 00:00:00.
#
# For 2026-08-27 the replay assumes 0 W before the first sample. That is not a
# guess: the day's own tesla_home_charging_energy counter reads max 0.000 kWh,
# so the car demonstrably did not charge at all that day. The assumption is
# asserted rather than trusted -- see
# test_the_tesla_assumption_on_2026_08_27_is_backed_by_its_own_counter.
TESLA_COMPLETE_DAYS = ("2026-08-28", "2026-08-29")
TESLA_ASSUMED_DAY = "2026-08-27"

QUANTITIES = ("house", "grid_export", "grid_import",
              "battery_charge", "battery_discharge", "solar")


# ---------------------------------------------------------------------------
# Tolerances. Every one is measured, not guessed; the measurement is in the
# comment. Re-run fixtures/replay.py to reproduce any of them.
# ---------------------------------------------------------------------------

# The daily counters are single u16 words at 0.1 kWh (CLAUDE.md register map),
# so any comparison against one carries +/-0.05 kWh of pure quantisation before
# any physics is involved. On a 5 kWh flow that alone is 1%.
COUNTER_QUANTUM_KWH = 0.1

# Recorder gaps. `unavailable` runs are the logger's one-session-at-a-time limit
# biting (CLAUDE.md), not a network fault. Measured 131-534 s per day, i.e.
# 0.15-0.62% of the window. 1% leaves headroom without hiding a real outage.
MAX_GAP_FRACTION = 0.01

# Integrating a power register and reading the inverter's own energy counter are
# two different measurement paths. Measured 2026-08-27..30, integral vs counter:
# import -0.3..+0.4%, export -2.8..-0.7%, battery charge -1.4..+0.9%,
# battery discharge -1.0..+2.4%, solar +1.8..+2.2%. 4% covers all five with
# margin while still catching a real regression.
POWER_VS_COUNTER_TOL_PCT = 4.0

# house_load (register 33147) is the exception and does NOT sit in that band.
# Its integral runs BELOW house_consumption_today every day measured:
# -8.3%, -14.1%, -9.7% on the three full days, a roughly constant 1.9-2.4 kWh
# (~60-85 W) absolute shortfall. CLAUDE.md records the same effect at 7.8% over
# one night and leaves it unattributed. Asserted as a one-sided bound because
# the sign is the reproducible part; the magnitude is not.
HOUSE_SHORTFALL_PCT_MIN = 1.0
HOUSE_SHORTFALL_PCT_MAX = 20.0

# v2's sinks fill EXACTLY -- that is the whole design claim of BRIEF_V2.md
# section 8. Measured deviation of house, tesla and battery-charge inflows from
# their own integrals, on all four days: 0.000 kWh to three decimal places.
# 5e-3 kWh is floating-point slack on a ~30 kWh day, not a physics allowance.
EXACT_KWH = 5e-3

# Export is the one sink that can be left short, and only in one direction.
# `s2e = min(S, E)` caps export at the solar reading, so metered export that
# momentarily out-reads solar has no source to attribute it to. Physically that
# is the battery exporting, which CLAUDE.md says cannot happen (all three
# discharge windows unset), and it is correspondingly tiny.
# Measured shortfall: 0.020, 0.049, 0.034, 0.020 kWh. Never an overshoot.
EXPORT_SHORTFALL_MIN_KWH = 0.0
EXPORT_SHORTFALL_MAX_KWH = 0.08

# Sources are the elastic side in v2. They are spent exactly except where the
# L >= 0 clamp binds (over-spend) or the export cap binds (under-spend).
# Measured deviation of each source's total outflow from its own integral:
#   solar   +0.015 .. +0.097 kWh
#   battery -0.011 .. +0.055 kWh
#   grid    +0.005 .. +0.025 kWh
# Bounded at +/-0.15 kWh, i.e. inside 1.5 counter words, on days of 13-34 kWh.
SOURCE_DEV_KWH = 0.15

# The Tesla's own daily energy counter is an INDEPENDENT measurement of the same
# quantity the replay integrates. Measured agreement: 0.032 kWh on 2026-08-29
# (5.287 vs 5.319) and 0.113 kWh on 2026-08-30 (22.086 vs 22.199) -- 0.6% and
# 0.5%. It is a left-Riemann integral of an on-change sensor against HA's own
# integration of the same signal, so exact agreement is not expected.
TESLA_COUNTER_TOL_KWH = 0.25

# Deploy grade. A Sankey ribbon on a 10-40 kWh day is read to about a tenth of a
# kWh; 5% is where the picture starts telling a different story.
DEPLOY_TOL_PCT = 5.0

# flow_5min CSV: 0.1 kWh quantum on 190 buckets. Worst-case accumulated
# quantisation is 190*0.05 = 9.5 kWh, but errors are not systematic; empirically
# the column totals land within ~1 kWh of the replay. 1.5 kWh is a sanity band.
CSV_SANITY_KWH = 1.5

# BRIEF_V2.md section 2 predicts the Inverter node at "~2.5 kWh/day" and asks
# for a cross-check against flow-law's fitted AC = 0.9974*DC - 105.9 W.
# MEASURED: 3.180, 3.097, 2.509 kWh on the three full days (133, 129, 105 W
# mean) and 1.662 kWh over 16.1 h of the partial day (103 W). The two days that
# sit on ~105 W are the two LOW-solar days, and the two at ~130 W are the high
# ones -- a fixed ~106 W parasitic plus a proportional term, which is exactly
# the shape flow-law fitted independently. Nothing here was tuned to hit it.
INVERTER_MIN_KWH = 2.0
INVERTER_MAX_KWH = 4.0
INVERTER_MEAN_W_MIN = 90.0
INVERTER_MEAN_W_MAX = 150.0


# ---------------------------------------------------------------------------
# Session-cached replay
# ---------------------------------------------------------------------------

_CACHE = {}


def result(day):
    if day not in _CACHE:
        doc = replay.load_day(day, tesla=True)
        end = replay.last_sample_epoch(doc) if day == PARTIAL_DAY else None
        r = replay.replay(doc, decompose, end_epoch=end)
        r["doc"] = doc
        r["derived"] = replay.derived(r["flows"])
        _CACHE[day] = r
    return _CACHE[day]


def counters(day):
    return replay.load_counters()["days"][day]


def counter(day, quantity):
    return counters(day)[replay.COUNTER_KEY[quantity]]["max"]


def tesla_counter(day):
    """The car's own daily energy counter, from the sidecar capture."""
    doc = result(day)["doc"]
    stats = doc["tesla_counters"]["tesla_home_charging_energy"]
    return stats["max"] - stats["min"]


def hours_of(day):
    r = result(day)
    return (r["gap_s"] + r["live_s"]) / 3600.0


def within_counter_tolerance(got, want, pct_tol):
    """Agreement with a daily counter, as ONE always-evaluated expression.

    The counters are single u16 words at 0.1 kWh, so a difference smaller than
    one word says nothing either way -- but expressing that as an early `return`
    made the percentage assertion below it UNREACHABLE. Coverage caught exactly
    that: on these fixtures four of the six counter tests took the early exit on
    every day, so they passed without ever evaluating an assertion. They were
    green and vacuous.

    Folding both arms into one expression keeps the same physics and makes the
    test able to fail again.
    """
    return abs(got - want) <= COUNTER_QUANTUM_KWH or abs(signed_pct(got, want)) <= pct_tol


def signed_pct(got, want):
    assert want, "counter is zero/None - cannot form a percentage"
    return 100.0 * (got - want) / want


def source_spend(day, source):
    f = result(day)["flows"]
    return sum(v for k, v in f.items() if k.startswith(source + "_to_"))


# ===========================================================================
# 1. Fixture and capture integrity
# ===========================================================================

def test_fixture_directory_exists():
    assert os.path.isdir(FIXTURES)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_history_fixture_present(day):
    p = os.path.join(FIXTURES, "history_%s.json.gz" % day)
    assert os.path.isfile(p) and os.path.getsize(p) > 10_000


@pytest.mark.parametrize("day", ALL_DAYS)
def test_tesla_fixture_present(day):
    """v2 needs a fifth channel, captured separately and later."""
    p = os.path.join(FIXTURES, "tesla_history_%s.json.gz" % day)
    assert os.path.isfile(p) and os.path.getsize(p) > 5_000


def test_counters_fixture_present_and_covers_every_day():
    doc = replay.load_counters()
    assert set(doc["days"]) == set(ALL_DAYS)
    assert doc["captured_utc"].startswith("2026-")
    assert "history/period" in doc["source"]


def test_capture_provenance_recorded_in_every_history_fixture():
    for day in ALL_DAYS:
        doc = replay.load_day(day)
        assert doc["tz"] == "Europe/London"
        assert doc["captured_utc"].startswith("2026-")
        assert "history/period" in doc["source"]
        assert "significant_changes_only=0" in doc["source"]


def test_capture_provenance_recorded_in_every_tesla_fixture():
    for day in ALL_DAYS:
        with gzip.open(os.path.join(FIXTURES, "tesla_history_%s.json.gz" % day), "rt") as fh:
            import json
            doc = json.load(fh)
        assert doc["tz"] == "Europe/London"
        assert "significant_changes_only=0" in doc["source"]
        assert "tesla" in doc["series"]


def test_fixtures_contain_no_credentials():
    """The capture reads a long-lived token in-process. Nothing may leak into a fixture."""
    needles = ("Bearer ", "eyJ", "access_token", "Authorization", "long_lived")
    for name in sorted(os.listdir(FIXTURES)):
        path = os.path.join(FIXTURES, name)
        if not os.path.isfile(path):
            continue
        opener = gzip.open if name.endswith(".gz") else open
        with opener(path, "rt", errors="replace") as fh:
            blob = fh.read()
        for n in needles:
            if name == "capture.py" and n in ("access_token", "Authorization", "long_lived"):
                continue  # capture.py documents the mechanism; it stores no value
            assert n not in blob, "%s leaks %r" % (name, n)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_day_window_is_exactly_one_local_day(day):
    doc = replay.load_day(day)
    span = doc["end_epoch"] - doc["start_epoch"]
    # No DST transition in this window, so every day is 86400 s. A 82800/90000
    # span here would mean the capture straddled a clock change.
    assert span == 86400.0


@pytest.mark.parametrize("day", ALL_DAYS)
def test_every_inverter_channel_has_a_sample_at_or_before_day_start(day):
    """HA clamps the first history row to start_time, so a forward-filled
    integral has a defined value from the first second. Without this the day
    would silently start from nothing."""
    doc = replay.load_day(day)
    for ch in replay.CHANNELS:
        pts = doc["series"][ch]
        assert pts, "%s has no samples on %s" % (ch, day)
        assert pts[0][0] <= doc["start_epoch"] + 1.0


@pytest.mark.parametrize("day", TESLA_COMPLETE_DAYS + (PARTIAL_DAY,))
def test_the_tesla_channel_covers_these_days_from_midnight(day):
    doc = replay.load_day(day, tesla=True)
    assert doc["series"]["tesla"][0][0] <= doc["start_epoch"] + 1.0
    assert result(day)["tesla_assumed_s"] == 0.0


def test_the_tesla_channel_does_NOT_cover_2026_08_27():
    """Asserted, not worked around. The car's sensor did not exist for the first
    55.6% of that day, and a test suite that silently treated the assumed period
    as measured would be lying about its own coverage."""
    doc = replay.load_day(TESLA_ASSUMED_DAY, tesla=True)
    first = doc["series"]["tesla"][0][0]
    assert first - doc["start_epoch"] > 40_000.0
    assert len(doc["series"]["tesla"]) == 1
    assert result(TESLA_ASSUMED_DAY)["tesla_assumed_s"] > 40_000.0


def test_the_tesla_assumption_on_2026_08_27_is_backed_by_its_own_counter():
    """The replay assumes 0 W before the first Tesla sample. On 2026-08-27 that
    is verifiable rather than merely plausible: the car's own daily energy
    counter for the whole day reads 0.000 kWh, so it did not charge at all.

    This is the test that makes 2026-08-27 usable. If a future re-capture ever
    lands a day with a real gap AND a non-zero counter, this fires, and the
    assumption must be revisited rather than inherited.
    """
    assert tesla_counter(TESLA_ASSUMED_DAY) == 0.0
    assert result(TESLA_ASSUMED_DAY)["derived"]["tesla"] == pytest.approx(0.0, abs=EXACT_KWH)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_samples_are_within_the_day_window(day):
    doc = replay.load_day(day, tesla=True)
    for ch in replay.channels_of(doc):
        for ts, _ in doc["series"][ch]:
            assert doc["start_epoch"] - 1.0 <= ts <= doc["end_epoch"] + 1.0


def test_three_full_days_and_one_partial():
    assert len(FULL_DAYS) >= 3
    assert PARTIAL_DAY not in FULL_DAYS


# ===========================================================================
# 2. Data hygiene: gaps and sign conventions
# ===========================================================================

@pytest.mark.parametrize("day", ALL_DAYS)
def test_recorder_gaps_are_small_and_explicit(day):
    """`unavailable` runs are treated as gaps that accrue nothing - the same
    thing HA's integration platform does - never interpolated to zero."""
    r = result(day)
    total = r["gap_s"] + r["live_s"]
    assert total > 0
    assert r["gap_s"] / total < MAX_GAP_FRACTION, (
        "%s: %.0f s of %.0f s unavailable" % (day, r["gap_s"], total))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_unavailable_states_are_present_and_parsed_as_gaps(day):
    """If this ever stops finding non-numeric states the capture has started
    silently dropping them, and the gap accounting above becomes a lie."""
    doc = replay.load_day(day)
    nonnum = 0
    for ch in replay.CHANNELS:
        for _ts, s in doc["series"][ch]:
            try:
                float(s)
            except ValueError:
                nonnum += 1
    assert nonnum > 0, "%s: expected some unavailable states" % day


@pytest.mark.parametrize("day", ALL_DAYS)
def test_solar_is_never_negative(day):
    doc = replay.load_day(day)
    vals = [v for _t, v in replay._numeric(doc["series"]["solar"], doc["start_epoch"])
            if v is not None]
    assert min(vals) >= 0.0


@pytest.mark.parametrize("day", ALL_DAYS)
def test_house_is_never_negative(day):
    doc = replay.load_day(day)
    vals = [v for _t, v in replay._numeric(doc["series"]["house"], doc["start_epoch"])
            if v is not None]
    assert min(vals) >= 0.0


@pytest.mark.parametrize("day", ALL_DAYS)
def test_tesla_is_never_negative(day):
    doc = replay.load_day(day, tesla=True)
    vals = [v for _t, v in replay._numeric(doc["series"]["tesla"], doc["start_epoch"])
            if v is not None]
    assert min(vals) >= 0.0


def test_battery_and_grid_take_both_signs():
    """Confirms the HA sign conventions really are signed here (CLAUDE.md:
    battery positive = charging, grid positive = importing). If either came
    back one-signed the decomposition would be exercised on half its domain."""
    for day in FULL_DAYS:
        doc = replay.load_day(day)
        for ch in ("battery", "grid"):
            vals = [v for _t, v in replay._numeric(doc["series"][ch], doc["start_epoch"])
                    if v is not None]
            assert min(vals) < 0 < max(vals), "%s %s is one-signed" % (day, ch)


# ===========================================================================
# 3. Integrator properties
# ===========================================================================

def test_intervals_tile_the_window_exactly():
    doc = replay.load_day(FULL_DAYS[0])
    total = sum(dt for _t, dt, _v in replay.intervals(doc))
    assert abs(total - 86400.0) < 1e-6


def test_intervals_tile_the_window_exactly_with_five_channels():
    """Adding the Tesla series adds marks to the timeline; it must not change
    the total time covered."""
    doc = replay.load_day(FULL_DAYS[1], tesla=True)
    total = sum(dt for _t, dt, _v in replay.intervals(doc))
    assert abs(total - 86400.0) < 1e-6


def test_intervals_are_contiguous_and_ordered():
    doc = replay.load_day(FULL_DAYS[0], tesla=True)
    prev_end = doc["start_epoch"]
    for t, dt, _v in replay.intervals(doc):
        assert abs(t - prev_end) < 1e-6
        assert dt > 0
        prev_end = t + dt


def test_no_interval_exceeds_max_sub_interval():
    doc = replay.load_day(FULL_DAYS[0], tesla=True)
    for _t, dt, _v in replay.intervals(doc):
        assert dt <= replay.MAX_SUB_INTERVAL_S + 1e-9


def test_max_sub_interval_is_identity_for_left_riemann():
    """HA's `integration` platform with max_sub_interval re-integrates the HELD
    value on a timer. For a left Riemann sum that is exactly a subdivision of
    the same rectangle, so the daily total must not move. This is why choosing
    max_sub_interval is a liveness decision, not an accuracy one.

    It matters more in v2 than v1: the Tesla sensor publishes on change and can
    hold a plateau for ~80 minutes, so it produces exactly the long held runs
    this property is about.
    """
    doc = replay.load_day(FULL_DAYS[1], tesla=True)
    a = replay.replay(doc, decompose, max_sub_interval=60.0)
    b = replay.replay(doc, decompose, max_sub_interval=86400.0)
    for k in a["flows"]:
        assert abs(a["flows"][k] - b["flows"][k]) < EXACT_KWH, k
    for k in a["raw"]:
        assert abs(a["raw"][k] - b["raw"][k]) < EXACT_KWH, k


def test_left_riemann_on_a_synthetic_step():
    """1000 W held for the first hour, 0 W after, must integrate to 1.000 kWh.
    Catches a right-Riemann or trapezoid slip, which would give 0 or 0.5."""
    start = 1_000_000.0
    doc = {"start_epoch": start, "end_epoch": start + 7200.0,
           "series": {"solar": [[start, "1000"], [start + 3600, "0"]],
                      "battery": [[start, "0"]],
                      "grid": [[start, "-1000"], [start + 3600, "0"]],
                      "house": [[start, "0"]],
                      "tesla": [[start, "0"]]}}
    r = replay.replay(doc, decompose)
    assert abs(r["raw"]["solar"] - 1.0) < 1e-9
    assert abs(r["raw"]["grid_export"] - 1.0) < 1e-9
    assert abs(r["flows"]["solar_to_export"] - 1.0) < 1e-9


def test_a_synthetic_tesla_plateau_integrates_as_a_held_value():
    """The 80-minute plateau, in miniature: 7 kW held for one hour with no
    intermediate samples must integrate to 7 kWh, not to zero and not to a
    decayed value. A held TeslaMate reading is correct, not stale."""
    start = 1_000_000.0
    doc = {"start_epoch": start, "end_epoch": start + 3600.0,
           "series": {"solar": [[start, "0"]],
                      "battery": [[start, "0"]],
                      "grid": [[start, "7000"]],
                      "house": [[start, "7000"]],
                      "tesla": [[start, "7000"]]}}
    r = replay.replay(doc, decompose)
    assert r["raw"]["tesla"] == pytest.approx(7.0, abs=1e-9)
    assert r["flows"]["grid_to_tesla"] == pytest.approx(7.0, abs=1e-9)
    assert r["flows"]["grid_to_house"] == pytest.approx(0.0, abs=1e-12)


def test_gap_intervals_contribute_no_energy():
    start = 1_000_000.0
    doc = {"start_epoch": start, "end_epoch": start + 7200.0,
           "series": {"solar": [[start, "1000"], [start + 3600, "unavailable"]],
                      "battery": [[start, "0"]],
                      "grid": [[start, "-1000"]],
                      "house": [[start, "0"]],
                      "tesla": [[start, "0"]]}}
    r = replay.replay(doc, decompose)
    assert abs(r["raw"]["solar"] - 1.0) < 1e-9
    assert abs(r["gap_s"] - 3600.0) < 1e-6
    assert abs(r["live_s"] - 3600.0) < 1e-6


def test_a_missing_tesla_reading_is_assumed_zero_and_counted():
    """The assumption is made visible in the return value rather than hidden.
    An hour with no Tesla reading accrues no car energy AND reports the hour."""
    start = 1_000_000.0
    doc = {"start_epoch": start, "end_epoch": start + 7200.0,
           "series": {"solar": [[start, "0"]],
                      "battery": [[start, "0"]],
                      "grid": [[start, "1000"]],
                      "house": [[start, "1000"]],
                      "tesla": [[start + 3600, "0"]]}}
    r = replay.replay(doc, decompose)
    assert r["tesla_assumed_s"] == pytest.approx(3600.0, abs=1e-6)
    assert r["raw"]["tesla"] == pytest.approx(0.0, abs=1e-12)
    assert r["gap_s"] == 0.0, "a missing tesla reading is not an inverter gap"


def test_replay_is_deterministic():
    doc = replay.load_day(FULL_DAYS[2], tesla=True)
    a = replay.replay(doc, decompose)
    b = replay.replay(doc, decompose)
    assert a["flows"] == b["flows"] and a["raw"] == b["raw"]


def test_four_channel_replay_still_works_unchanged():
    """replay.py gained a fifth channel additively. A four-channel doc must walk
    exactly as before, because other agents' harnesses depend on it."""
    doc = replay.load_day(FULL_DAYS[0])
    assert replay.channels_of(doc) == replay.CHANNELS
    total = sum(dt for _t, dt, _v in replay.intervals(doc))
    assert abs(total - 86400.0) < 1e-6


# ===========================================================================
# 4. Raw power integral vs the inverter's own counters
#
# This is the harness calibration step, BEFORE any attribution. If these do not
# hold, nothing downstream can be trusted.
# ===========================================================================

@pytest.mark.parametrize("day", ALL_DAYS)
@pytest.mark.parametrize("quantity", [q for q in QUANTITIES if q != "house"])
def test_raw_integral_matches_counter(day, quantity):
    r = result(day)
    got, want = r["raw"][quantity], counter(day, quantity)
    assert within_counter_tolerance(got, want, POWER_VS_COUNTER_TOL_PCT), (
        "%s %s: integral %.3f vs counter %.3f (%+.2f%%)"
        % (day, quantity, got, want, signed_pct(got, want)))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_house_power_integral_runs_below_its_counter(day):
    """The documented odd one out. Asserted as a signed one-sided bound because
    the direction reproduces every day and the magnitude does not."""
    r = result(day)
    got, want = r["raw"]["house"], counter(day, "house")
    err = -signed_pct(got, want)  # positive = integral is short
    assert HOUSE_SHORTFALL_PCT_MIN <= err <= HOUSE_SHORTFALL_PCT_MAX, (
        "%s house: integral %.3f vs counter %.3f (%+.2f%%)"
        % (day, got, want, -err))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_house_shortfall_is_a_roughly_constant_offset_not_a_scale_error(day):
    """1.9-2.4 kWh/day on the full days, i.e. ~60-85 W held continuously, while
    the percentage swings 2.7-14.1% with the size of the day. That shape says
    offset, not gain - which is why no scale factor will fix it."""
    r = result(day)
    absent = counter(day, "house") - r["raw"]["house"]
    assert 0.5 <= absent <= 3.0, "%s: %.3f kWh unaccounted" % (day, absent)


@pytest.mark.parametrize("day", FULL_DAYS)
def test_counters_close_the_energy_balance_to_about_one_percent(day):
    """solar - charge + discharge  ==  house - import + export, using ONLY the
    counters. Measured residual +0.2, +0.2, +0.3 kWh on 19-34 kWh days.

    Bounded in kWh, not percent, because six counters at a 0.1 kWh word give
    6*0.05 = 0.30 kWh of worst-case quantisation on their own; a real ~2%
    conversion loss on a 15 kWh DC net is another 0.3. 0.5 kWh covers both and
    still fails loudly if a counter's meaning is wrong."""
    c = counters(day)
    dc = (c["solar_today"]["max"] - c["battery_charge_today"]["max"]
          + c["battery_discharge_today"]["max"])
    ac = (c["house_consumption_today"]["max"]
          - c["grid_import_today"]["max"] + c["grid_export_today"]["max"])
    assert abs(dc - ac) <= 0.5, "%s: DC %.2f vs AC %.2f" % (day, dc, ac)


@pytest.mark.parametrize("day", FULL_DAYS)
def test_the_same_balance_does_not_close_from_the_power_sensors(day):
    """The finding that motivated the Inverter node. The four live power sensors
    leave a 2.5-3.1 kWh/day residual where the counters leave 0.2-0.3 kWh. No
    decomposition can be more self-consistent than its inputs, so ~3 kWh/day has
    to be parked somewhere on the chart -- and v2 parks it somewhere NAMED."""
    r = result(day)["raw"]
    assert r["dc_ac_residual"] > 1.5, "%s: residual %.3f kWh" % (day, r["dc_ac_residual"])
    assert r["dc_ac_residual"] < 5.0


# ===========================================================================
# 5. The v2 decomposition over real history
# ===========================================================================

@pytest.mark.parametrize("day", ALL_DAYS)
def test_every_integrated_flow_is_non_negative(day):
    for k, v in result(day)["flows"].items():
        assert v >= -1e-12, "%s %s = %.6f" % (day, k, v)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_all_twelve_flows_are_present(day):
    assert set(result(day)["flows"]) == set(FLOWS)
    assert len(FLOWS) == 12


@pytest.mark.parametrize("day", ALL_DAYS)
def test_no_battery_to_grid_flow_can_exist(day):
    """All three discharge windows are unset and must stay unset (CLAUDE.md).
    The contract has no key for it, so this is structural, not numeric."""
    assert not any("battery_to" in f and "export" in f for f in FLOWS)
    assert "battery_to_grid" not in result(day)["flows"]
    assert "battery_to_export" not in result(day)["flows"]


@pytest.mark.parametrize("day", ALL_DAYS)
def test_house_inflows_reproduce_the_house_load_excluding_the_car(day):
    """v2's House node is house_load MINUS the car, and its three inflows must
    fill it to the last watt-second.

    v1 compared against the full house_load and could not close, because it had
    no Tesla term and no Inverter node. The comparison target changed; the
    standard got STRICTER, from a 0.15 kWh carve-out to floating-point noise.
    """
    r = result(day)
    house_excl_car = r["raw"]["house"] - r["raw"]["tesla"]
    assert r["derived"]["house"] == pytest.approx(house_excl_car, abs=EXACT_KWH)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_tesla_inflows_reproduce_the_clamped_tesla_integral(day):
    r = result(day)
    assert r["derived"]["tesla"] == pytest.approx(r["raw"]["tesla"], abs=EXACT_KWH)


@pytest.mark.parametrize("day", TESLA_COMPLETE_DAYS + (PARTIAL_DAY,))
def test_the_replayed_tesla_energy_agrees_with_the_cars_own_counter(day):
    """An INDEPENDENT check, and the only one available for the Tesla channel.

    The replay left-Riemann-integrates sensor.tesla_home_charging_power; the
    counter is HA's own integration of the same signal, accumulated live. They
    are not the same computation, so exact agreement is not expected -- but a
    real disagreement would mean the replay is mis-integrating an on-change
    sensor, which is precisely the risk with an 80-minute plateau.

    Measured: 5.287 vs 5.319 kWh (0.6%) and 22.086 vs 22.199 kWh (0.5%).
    """
    want = tesla_counter(day)
    if want < 0.1:
        pytest.skip("%s: the car barely charged; no signal to compare" % day)
    got = result(day)["raw"]["tesla"]
    assert got == pytest.approx(want, abs=TESLA_COUNTER_TOL_KWH), (
        "%s tesla: replay %.3f vs counter %.3f" % (day, got, want))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_battery_charge_inflows_fill_the_charge_integral_exactly(day):
    r = result(day)
    assert r["derived"]["battery_charge"] == pytest.approx(
        r["raw"]["battery_charge"], abs=EXACT_KWH)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_export_is_solar_only_and_short_by_at_most_the_export_cap(day):
    """Export is structurally solar's, by elimination -- the battery has no legal
    path to the grid. The only shortfall permitted is `s2e = min(S, E)` biting,
    and it may only ever be a shortfall, never an overshoot."""
    r = result(day)
    assert r["flows"]["solar_to_export"] == r["derived"]["grid_export"]
    short = r["raw"]["grid_export"] - r["flows"]["solar_to_export"]
    assert EXPORT_SHORTFALL_MIN_KWH <= short <= EXPORT_SHORTFALL_MAX_KWH, (
        "%s export short by %.4f kWh" % (day, short))


@pytest.mark.parametrize("day", ALL_DAYS)
@pytest.mark.parametrize("source,raw_key", [
    ("solar", "solar"), ("battery", "battery_discharge"), ("grid", "grid_import")])
def test_each_source_spends_its_own_integral(day, source, raw_key):
    """v2's headline claim, on real data. There is no leak to bound any more --
    every source's outbound flows sum to its own measured integral, within the
    small cost of the L >= 0 clamp and the export cap.

    This REPLACES v1's grid_import_leak and battery_discharge_leak tests, which
    bounded a -25% shortfall. Measured deviation here: -0.011 to +0.097 kWh on
    days of 13-34 kWh.
    """
    r = result(day)
    got, have = source_spend(day, source), r["raw"][raw_key]
    assert got == pytest.approx(have, abs=SOURCE_DEV_KWH), (
        "%s %s: spent %.3f vs measured %.3f (%+.3f kWh)"
        % (day, source, got, have, got - have))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_the_decomposition_conserves_energy_between_sources_and_sinks(day):
    """Rewritten. The v1 version of this test could never fail: it summed the
    same six flow keys twice in a different order and asserted abs(x - x) < 1e-9.

    This version groups by SOURCE on one side and by SINK on the other, which
    are genuinely different partitions of the twelve flows, so a flow that were
    counted in one grouping and not the other would show up.
    """
    f = result(day)["flows"]
    by_source = sum(source_spend(day, s) for s in ("solar", "battery", "grid"))
    by_sink = sum(v for k, v in f.items())
    assert by_source == pytest.approx(by_sink, abs=1e-9)
    # ...and the sink-side grouping really is a partition of all twelve keys.
    sinks = ("house", "tesla", "battery", "export", "inverter")
    counted = sum(v for k, v in f.items() if k.split("_to_")[1] in sinks)
    assert counted == pytest.approx(by_sink, abs=1e-9)
    assert len(f) == 12


# ===========================================================================
# 6. The Inverter node over real days -- BRIEF_V2.md section 2's open question
# ===========================================================================

@pytest.mark.parametrize("day", FULL_DAYS)
def test_the_inverter_node_lands_where_the_brief_predicted(day):
    """The brief predicted ~2.5 kWh/day and asked for the number to be reported
    rather than tuned to. MEASURED: 3.180, 3.097, 2.509 kWh."""
    got = result(day)["derived"]["inverter"]
    assert INVERTER_MIN_KWH <= got <= INVERTER_MAX_KWH, (
        "%s inverter node %.3f kWh" % (day, got))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_the_inverter_node_as_a_mean_power_matches_flow_laws_parasitic(day):
    """The independent cross-check the brief asked for.

    flow-law fitted AC = 0.9974*DC - 105.9 W from a different dataset by a
    different method. Expressed as a mean power the Inverter node gives 133,
    129, 105 and 103 W across the four days -- straddling 105.9 W, with the two
    high figures on the two high-solar days, which is exactly what a fixed
    parasitic plus a proportional term looks like.

    Nothing in v2 was fitted to produce this. That is the point: two unrelated
    methods agreeing is evidence, whereas one method reproducing its own
    constant would be tautology.
    """
    r = result(day)
    mean_w = 1000.0 * r["derived"]["inverter"] / hours_of(day)
    assert INVERTER_MEAN_W_MIN <= mean_w <= INVERTER_MEAN_W_MAX, (
        "%s inverter mean %.0f W" % (day, mean_w))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_the_inverter_node_is_never_negative_over_a_whole_day(day):
    for key in ("solar_to_inverter", "battery_to_inverter", "grid_to_inverter"):
        assert result(day)["flows"][key] >= 0.0


@pytest.mark.parametrize("day", FULL_DAYS)
def test_the_inverter_node_absorbs_the_dc_ac_residual(day):
    """The residual the four power sensors leave (2.5-3.1 kWh/day) is what the
    Inverter node displays. They are computed completely differently -- one from
    (solar - battery) - (house - grid), the other by summing three flows -- so
    their agreement is a real check that the residual landed where it was
    supposed to and did not get smeared onto a source."""
    r = result(day)
    assert r["derived"]["inverter"] == pytest.approx(
        r["raw"]["dc_ac_residual"], abs=1.0), (
        "%s: inverter %.3f vs residual %.3f"
        % (day, r["derived"]["inverter"], r["raw"]["dc_ac_residual"]))


# ===========================================================================
# 7. The swappable loss rule -- BRIEF_V2.md section 8 asks for a measurement
# ===========================================================================

def test_the_selected_loss_rule_is_the_documented_default():
    """A rule swap changes every ribbon on the chart. It must be a decision, not
    a leftover from an experiment."""
    import flows as fmod
    assert fmod.LOSS_RULE == "proportional"
    assert set(fmod.LOSS_RULES) == {"proportional", "band_table"}


def test_both_loss_rules_fill_every_sink_exactly_on_a_real_day():
    """The property that must survive ANY rule: each rule returns share rows
    summing to 1, so every sink fills exactly regardless of which ran. If a new
    rule breaks this, the chart silently under-draws."""
    import flows as fmod
    doc = replay.load_day(FULL_DAYS[1], tesla=True)
    previous = fmod.LOSS_RULE
    try:
        for name in fmod.LOSS_RULES:
            fmod.set_loss_rule(name)
            r = replay.replay(doc, fmod.decompose)
            d = replay.derived(r["flows"])
            house_excl_car = r["raw"]["house"] - r["raw"]["tesla"]
            assert d["house"] == pytest.approx(house_excl_car, abs=EXACT_KWH), name
            assert d["tesla"] == pytest.approx(r["raw"]["tesla"], abs=EXACT_KWH), name
            assert d["battery_charge"] == pytest.approx(
                r["raw"]["battery_charge"], abs=EXACT_KWH), name
    finally:
        fmod.set_loss_rule(previous)


def test_report_the_two_loss_rules_measured_over_every_replay_day(capsys):
    """BRIEF_V2.md section 8: "MEASURE both over the four replay days, and
    report. Default stays aggregate-proportional unless the measurement says
    otherwise."

    This is the measurement. It asserts only the properties that must hold under
    both rules -- it deliberately does NOT assert which one wins, because the
    brief reserves that decision. Run with -s to read the table.
    """
    import flows as fmod
    previous = fmod.LOSS_RULE
    rows = []
    try:
        for name in ("proportional", "band_table"):
            fmod.set_loss_rule(name)
            for day in ALL_DAYS:
                doc = replay.load_day(day, tesla=True)
                end = replay.last_sample_epoch(doc) if day == PARTIAL_DAY else None
                r = replay.replay(doc, fmod.decompose, end_epoch=end)
                d = replay.derived(r["flows"])
                rows.append((name, day, d["inverter"],
                             d["solar"] - r["raw"]["solar"],
                             d["battery_discharge"] - r["raw"]["battery_discharge"],
                             d["grid_import"] - r["raw"]["grid_import"]))
    finally:
        fmod.set_loss_rule(previous)

    lines = ["", "%-14s %-11s %9s %9s %9s %9s"
             % ("loss rule", "day", "inv kWh", "solar dev", "batt dev", "grid dev")]
    for row in rows:
        lines.append("%-14s %-11s %9.3f %+9.3f %+9.3f %+9.3f" % row)
    print("\n".join(lines))

    # The Inverter node is a property of the readings, not of the rule: the rule
    # only decides who is CHARGED for the loss, never how much there was.
    by_day = {}
    for name, day, inv, _s, _b, _g in rows:
        by_day.setdefault(day, {})[name] = inv
    for day, both in by_day.items():
        assert both["proportional"] == pytest.approx(both["band_table"], abs=EXACT_KWH), (
            "%s: the loss rule changed the SIZE of the loss, which it must not" % day)


# ===========================================================================
# 8. Flows vs the inverter's daily counters
# ===========================================================================

@pytest.mark.parametrize("day", ALL_DAYS)
def test_house_plus_tesla_flows_match_house_consumption_today(day):
    """v2 splits house_load into House and Tesla, so the counter comparison is
    against their SUM. Bounded by house_load's own shortfall against the
    counter, which the decomposition inherits and cannot fix (see section 4)."""
    r = result(day)
    got = r["derived"]["house"] + r["derived"]["tesla"]
    want = counter(day, "house")
    err = signed_pct(got, want)
    assert -HOUSE_SHORTFALL_PCT_MAX <= err <= DEPLOY_TOL_PCT, (
        "%s house+tesla: flows %.3f vs counter %.3f (%+.2f%%)" % (day, got, want, err))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_solar_to_export_matches_grid_export_today(day):
    r = result(day)
    got, want = r["flows"]["solar_to_export"], counter(day, "grid_export")
    assert within_counter_tolerance(got, want, DEPLOY_TOL_PCT), (
        "%s export: flows %.3f vs counter %.3f (%+.2f%%)"
        % (day, got, want, signed_pct(got, want)))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_battery_inflows_match_battery_charge_today(day):
    r = result(day)
    got, want = r["derived"]["battery_charge"], counter(day, "battery_charge")
    assert within_counter_tolerance(got, want, DEPLOY_TOL_PCT), (
        "%s battery charge: flows %.3f vs counter %.3f (%+.2f%%)"
        % (day, got, want, signed_pct(got, want)))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_all_four_grid_outflows_match_grid_import_today(day):
    """v1's version of this was xfail at -2.6% to -10.1%, because it summed only
    grid_to_house and grid_to_battery. v2's grid has FOUR destinations -- house,
    tesla, battery and inverter -- and summing all four closes."""
    r = result(day)
    got, want = r["derived"]["grid_import"], counter(day, "grid_import")
    assert within_counter_tolerance(got, want, DEPLOY_TOL_PCT), (
        "%s import: flows %.3f vs counter %.3f (%+.2f%%)"
        % (day, got, want, signed_pct(got, want)))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_all_three_battery_outflows_match_battery_discharge_today(day):
    """v1's version was xfail at -3.1% to -17.8%, because battery_to_house was
    capped at the house load and discharge occurring while DC solar already
    covered the AC house reading was dropped entirely. v2 has no such cap and
    three destinations, so it closes."""
    r = result(day)
    got, want = r["derived"]["battery_discharge"], counter(day, "battery_discharge")
    assert within_counter_tolerance(got, want, DEPLOY_TOL_PCT), (
        "%s discharge: flows %.3f vs counter %.3f (%+.2f%%)"
        % (day, got, want, signed_pct(got, want)))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_all_five_solar_outflows_match_solar_today(day):
    """v1 recorded this as a quantified shortfall (-0.9% to -5.4%) because
    solar's spend was the residual after the constrained sources were paid. v2
    spends solar exactly, so what remains is only the integral-vs-counter gap
    already bounded in section 4."""
    r = result(day)
    got, want = r["derived"]["solar"], counter(day, "solar")
    assert within_counter_tolerance(got, want, POWER_VS_COUNTER_TOL_PCT), (
        "%s solar: flows %.3f vs counter %.3f (%+.2f%%)"
        % (day, got, want, signed_pct(got, want)))


# ===========================================================================
# 9. Retired v1 tests, kept skipped pending the lead's confirmation to delete.
#
# Each of these tested a property of v1's sequential-greedy decomposition with
# late AC referral. v2 has neither, so they have no subject -- they are not
# failing, they are meaningless. They are listed here rather than deleted so
# that the decision is visible and reversible; every one names its replacement.
# ===========================================================================

_RETIRED = "v1-only: superseded by v2. Pending confirmation to delete."


@pytest.mark.skip(reason=_RETIRED + " Replaced by "
                  "test_report_the_two_loss_rules_measured_over_every_replay_day.")
def test_loss_treatments_measured_over_a_real_day():
    """Drove decompose.LOSS_TREATMENTS over residual_on_solar / derived_solar_ac
    / fixed_efficiency. None of those exist in v2: they were three ways of
    referring DC to AC with a fitted constant, and v2 has no fitted constant."""


@pytest.mark.skip(reason=_RETIRED + " Replaced by "
                  "test_the_selected_loss_rule_is_the_documented_default.")
def test_selected_loss_treatment_is_the_documented_default():
    """Asserted LOSS_TREATMENT == 'residual_on_solar'."""


@pytest.mark.skip(reason=_RETIRED + " Replaced by "
                  "test_all_four_grid_outflows_match_grid_import_today.")
def test_grid_to_house_plus_grid_to_battery_matches_grid_import_today():
    """Summed two of what are now four grid destinations and called the
    difference a defect."""


@pytest.mark.skip(reason=_RETIRED + " No leak exists in v2; see "
                  "test_each_source_spends_its_own_integral.")
def test_grid_import_leak_is_bounded():
    """Bounded a -25% shortfall that is now structurally impossible."""


@pytest.mark.skip(reason=_RETIRED + " Replaced by "
                  "test_all_three_battery_outflows_match_battery_discharge_today.")
def test_battery_to_house_matches_battery_discharge_today():
    """battery_to_house was capped at the house load in v1; in v2 the battery
    has three destinations and no cap."""


@pytest.mark.skip(reason=_RETIRED + " No leak exists in v2; see "
                  "test_each_source_spends_its_own_integral.")
def test_battery_discharge_leak_is_bounded():
    """Bounded a -25% shortfall that is now structurally impossible."""


@pytest.mark.skip(reason=_RETIRED + " Replaced by "
                  "test_all_five_solar_outflows_match_solar_today.")
def test_solar_flows_vs_solar_today_dc_shortfall_is_quantified_not_asserted_equal():
    """Recorded solar's spend as a bound rather than an equality, because v1's
    solar absorbed a fitted residual. v2 spends solar exactly."""


# ===========================================================================
# 10. Cross-check against the independent 5-minute dataset
#
# COARSE ONLY. Every column but `tesla` is quantised to 0.1 kWh over 190
# buckets, and `tesla` to a 1.2 kW quantum. Shape and sign, not ground truth.
# ===========================================================================

def _csv_rows():
    with open(os.path.join(FIXTURES, "flow_5min_2026-08-30.csv")) as fh:
        return list(csv.DictReader(fh))


def _csv_total(col):
    return sum(float(r[col]) for r in _csv_rows())


def test_csv_fixture_shape():
    rows = _csv_rows()
    assert len(rows) == 190
    assert rows[0]["time"] == "00:00" and rows[-1]["time"] == "15:45"


def test_csv_totals_match_the_values_recorded_in_the_brief():
    """Pins the file itself, so a later divergence is visibly the file changing."""
    for col, want in (("solar", 21.5), ("house", 35.6), ("imp", 28.7),
                      ("exp", 11.6), ("bin", 5.8), ("bout", 2.8)):
        assert abs(_csv_total(col) - want) < 0.15, col
    assert abs(_csv_total("tesla") - 22.199) < 0.05


def _csv_window_replay():
    """Replay 2026-08-30 truncated to the CSV's 00:00-15:50 window."""
    doc = replay.load_day(PARTIAL_DAY, tesla=True)
    end = doc["start_epoch"] + (15 * 3600 + 50 * 60)
    r = replay.replay(doc, decompose, end_epoch=end)
    r["derived"] = replay.derived(r["flows"])
    return r


@pytest.mark.parametrize("col,key", [
    ("solar", "solar"), ("house", "house"), ("imp", "grid_import"),
    ("exp", "grid_export"), ("bin", "battery_charge"), ("bout", "battery_discharge"),
])
def test_csv_raw_columns_agree_with_the_replay_within_quantisation(col, key):
    r = _csv_window_replay()
    got, want = r["raw"][key], _csv_total(col)
    assert abs(got - want) <= CSV_SANITY_KWH, (
        "%s: replay %.3f vs csv %.3f" % (col, got, want))


def test_csv_tesla_column_agrees_with_the_replay():
    """The CSV's own tesla column is an independent third measurement of the car,
    alongside the replay integral and the HA counter. Coarse -- a 1.2 kW
    quantum -- but it is a different pipeline again."""
    r = _csv_window_replay()
    assert abs(r["raw"]["tesla"] - _csv_total("tesla")) <= CSV_SANITY_KWH


def test_csv_and_replay_agree_that_export_is_solar_only():
    """The CSV's own `exp` column totals 11.6 kWh but its `solar_to_exp` column
    totals only 9.6, so the earlier work left 2.0 kWh of measured export
    unattributed. v2 assigns export to solar in full by elimination, capped only
    at the solar reading. Both cannot be right; v2 matches the meter."""
    r = _csv_window_replay()
    gap = r["flows"]["solar_to_export"] - _csv_total("solar_to_exp")
    assert 1.5 <= gap <= 2.5, "export attribution gap moved to %.3f kWh" % gap
    assert abs(r["flows"]["solar_to_export"] - _csv_total("exp")) <= CSV_SANITY_KWH


def test_csv_house_split_sums_to_its_own_house_column():
    """The one property the CSV holds exactly, and the same one v2 holds: the
    house sink is filled to the last bucket."""
    t = {c: _csv_total(c) for c in ("batt_to_house", "solar_to_house", "grid_to_house", "house")}
    assert abs(t["batt_to_house"] + t["solar_to_house"] + t["grid_to_house"] - t["house"]) < 0.05


def test_csv_import_export_overlap_is_rare_and_pinned():
    """`both_ways` is the earlier work's own consistency flag: a 5-minute bucket
    holding both import and export means the bucketing smeared a real
    transition. Exactly one bucket does (10:30), so the file is usable as a
    coarse check - but that bucket is why it is coarse."""
    firing = [r["time"] for r in _csv_rows() if float(r["both_ways"])]
    assert firing == ["10:30"]


# ===========================================================================
# 11. The report. Not an assertion - run with -s to read the table.
# ===========================================================================

def test_report_reconciliation_table(capsys):
    import flows as fmod
    lines = ["", "flows.py loss rule: %s" % fmod.LOSS_RULE]
    hdr = "%-11s %-18s %9s %9s %9s %9s" % (
        "day", "quantity", "flows", "counter", "raw-int", "err%vs-ctr")
    for day in ALL_DAYS:
        r = result(day)
        lines.append("")
        lines.append("%s%s   gap %.0f s of %.0f s (%.2f%%)   tesla assumed %.0f s" % (
            day, "  PARTIAL" if day == PARTIAL_DAY else "",
            r["gap_s"], r["gap_s"] + r["live_s"],
            100.0 * r["gap_s"] / (r["gap_s"] + r["live_s"]), r["tesla_assumed_s"]))
        lines.append(hdr)
        for q in QUANTITIES:
            c = counter(day, q)
            lines.append("%-11s %-18s %9.3f %9.3f %9.3f %+9.2f" % (
                day, q, r["derived"][q], c, r["raw"][q], signed_pct(r["derived"][q], c)))
        lines.append("%-11s %-18s %9.3f %9s %9.3f" % (
            day, "tesla", r["derived"]["tesla"], "-", r["raw"]["tesla"]))
        lines.append("%-11s %-18s %9.3f %9s %9.3f" % (
            day, "INVERTER node", r["derived"]["inverter"], "-", r["raw"]["dc_ac_residual"]))
        lines.append("%-11s %-18s %9.0f" % (
            day, "inverter mean W", 1000.0 * r["derived"]["inverter"] / hours_of(day)))
    print("\n".join(lines))
    # Not vacuous: the table above cannot be produced at all unless every day
    # replays, so assert the thing that makes printing it meaningful.
    assert len(_CACHE) == len(ALL_DAYS)
