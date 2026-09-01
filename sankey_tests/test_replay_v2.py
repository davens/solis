"""Replay of the Sankey v2 decomposition against four real captured days.

Offline and hermetic: everything comes from `fixtures/`, nothing opens a socket.

What this adds over v1's `test_history_replay.py` is the fifth channel. v1
replayed four sensors; v2 needs Tesla too, and Tesla lives in a *separate*
capture (`tesla_history_<day>.json.gz`) taken at a different instant from the
inverter capture. Merging them correctly is most of the work in this file, and
three of the decisions are load-bearing enough to state up front:

**The end of a partial day is set by the four inverter channels only.** They poll
every ~10 s, so their last sample really is the edge of the data. `tesla` is a
TeslaMate on-change value: on 2026-08-30 its last sample is at 05:07 because the
car stopped charging, not because the recorder stopped. Using it would truncate
the day to five hours. CLAUDE.md is explicit that a held TeslaMate value is
correct, not stale.

**2026-08-27 has no usable Tesla data, and its House and Tesla figures are
UNKNOWABLE.** The sensor's first state ever is `unknown` at 13:28 that day; the
car charged 8.384 kWh overnight (CLAUDE.md, verified end to end) entirely inside
that blind window. Tesla is held at 0 W for the integration, which is a
deliberate ruling and is harmless to the six node totals that are blind to T --
but House and Tesla themselves are reported as None on that day, never as a
number.

An earlier version of this file "justified" the hold by asserting that
2026-08-27's `tesla_home_charging_energy` really did read 0.000. That was a
tautology wearing the clothes of evidence: the energy sensor is the Riemann
INTEGRAL of the power sensor that did not exist, so its zero is the same absence
a second time. Three separate agents reached for that same justification
independently, so `test_a_derived_sensors_zero_is_not_evidence_about_its_source`
now pins the trap rather than the conclusion.

**A gap on any of the four inverter channels drops the interval.** An
`unavailable` state is missing data, not zero watts, and CLAUDE.md's rule that a
zeroed reading is indistinguishable from a still night applies to integration
just as much as to the chart.

Run the reconciliation tables directly:

    uv run --no-project python test_replay_v2.py
"""
import csv
import gzip
import json
import math
import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
FIXTURES = os.path.join(HERE, "fixtures")
sys.path.insert(0, HERE)

flows = pytest.importorskip(
    "flows",
    reason="flows.py is written by agent v2-physics; these tests run against it",
)
decompose = flows.decompose

FULL_DAYS = ("2026-08-27", "2026-08-28", "2026-08-29")
PARTIAL_DAY = "2026-08-30"
ALL_DAYS = FULL_DAYS + (PARTIAL_DAY,)

# Days on which the Tesla sensor covered the whole window. Derived, not
# hard-coded, so a re-capture cannot silently re-admit a blind day -- and
# `test_the_usable_tesla_days_are_the_ones_we_think_they_are` fails if the set
# ever changes without someone noticing.
TESLA_DAYS = ("2026-08-28", "2026-08-29", "2026-08-30")
TESLA_BLIND_DAYS = ("2026-08-27",)

INVERTER_CHANNELS = ("solar", "battery", "grid", "house")
CHANNELS = INVERTER_CHANNELS + ("tesla",)

# HA's `integration` platform re-integrates at least this often. For a LEFT
# Riemann sum over a held value it is mathematically a no-op, but the deployed
# helpers will carry it, so the harness carries it too.
MAX_SUB_INTERVAL_S = 60.0

SOURCES = ("solar", "battery", "grid")


# ---------------------------------------------------------------------------
# Loading and merging
# ---------------------------------------------------------------------------

_CACHE = {}


def _load(path):
    with gzip.open(path, "rt") as fh:
        return json.load(fh)


def history(day):
    key = ("h", day)
    if key not in _CACHE:
        _CACHE[key] = _load(os.path.join(FIXTURES, "history_%s.json.gz" % day))
    return _CACHE[key]


def tesla_history(day):
    key = ("t", day)
    if key not in _CACHE:
        _CACHE[key] = _load(os.path.join(FIXTURES, "tesla_history_%s.json.gz" % day))
    return _CACHE[key]


def counters():
    if "c" not in _CACHE:
        with open(os.path.join(FIXTURES, "counters.json")) as fh:
            _CACHE["c"] = json.load(fh)
    return _CACHE["c"]


def _numeric(points, start):
    """[[ts, state]] -> [(ts, float|None)], clipped to `start`. None marks a gap."""
    out = []
    for ts, s in points:
        try:
            v = float(s)
        except (TypeError, ValueError):
            v = None
        out.append((max(float(ts), start), v))
    out.sort(key=lambda p: p[0])
    return out


def series(day):
    """The five merged channels for one day, as {name: [(ts, value|None)]}."""
    h, t = history(day), tesla_history(day)
    start = float(h["start_epoch"])
    assert float(t["start_epoch"]) == start, (
        "%s: the tesla capture covers a different window from the inverter "
        "capture; merging them would misalign the day" % day)
    s = {c: _numeric(h["series"][c], start) for c in INVERTER_CHANNELS}
    s["tesla"] = _numeric(t["series"]["tesla"], start)
    return s


def day_end(day):
    """The honest end of the day.

    A complete day runs to its window end. A partial day runs to the earliest
    'last sample' among the FOUR INVERTER channels -- never tesla, which is an
    on-change sensor whose silence means 'unchanged', not 'no more data'.
    """
    h = history(day)
    if day != PARTIAL_DAY:
        return float(h["end_epoch"])
    return min(float(h["series"][c][-1][0]) for c in INVERTER_CHANNELS)


def intervals(day, end=None, max_sub_interval=MAX_SUB_INTERVAL_S):
    """(t, dt, {channel: value|None}) over the merged five-channel timeline."""
    s = series(day)
    start = float(history(day)["start_epoch"])
    end = day_end(day) if end is None else end

    marks = {start, end}
    for pts in s.values():
        for ts, _ in pts:
            if start <= ts <= end:
                marks.add(ts)
    marks = sorted(marks)

    idx = {c: 0 for c in CHANNELS}
    # Tesla starts held at 0 W; see the module docstring and the test that
    # justifies it from the day's own energy counter.
    held = {c: None for c in INVERTER_CHANNELS}
    held["tesla"] = 0.0
    for i in range(len(marks) - 1):
        t0, t1 = marks[i], marks[i + 1]
        for c in CHANNELS:
            pts = s[c]
            while idx[c] < len(pts) and pts[idx[c]][0] <= t0:
                held[c] = pts[idx[c]][1]
                idx[c] += 1
        if t1 <= t0:
            continue
        t = t0
        while t < t1:
            step = min(max_sub_interval, t1 - t)
            yield t, step, dict(held)
            t += step


# ---------------------------------------------------------------------------
# Integration
# ---------------------------------------------------------------------------

def replay(day, fn=None, end=None):
    """Integrate the decomposed flows and the raw channels over one day, kWh."""
    fn = decompose if fn is None else fn
    out = {}
    raw = dict.fromkeys(
        ("solar", "house", "tesla", "house_rest", "grid_import", "grid_export",
         "battery_charge", "battery_discharge", "supply"), 0.0)
    gap_s = live_s = 0.0
    tesla_clamped_s = 0.0
    for _t, dt, v in intervals(day, end):
        if any(v[c] is None for c in CHANNELS):
            gap_s += dt
            continue
        live_s += dt
        h = dt / 3_600_000.0  # W*s -> kWh
        solar, battery, grid, house, tesla = (
            v["solar"], v["battery"], v["grid"], v["house"], v["tesla"])
        t_clamped = min(max(0.0, tesla), max(0.0, house))
        if tesla > house + 1e-9:
            tesla_clamped_s += dt
        raw["solar"] += max(0.0, solar) * h
        raw["house"] += max(0.0, house) * h
        raw["tesla"] += t_clamped * h
        raw["house_rest"] += (max(0.0, house) - t_clamped) * h
        raw["grid_import"] += max(0.0, grid) * h
        raw["grid_export"] += max(0.0, -grid) * h
        raw["battery_charge"] += max(0.0, battery) * h
        raw["battery_discharge"] += max(0.0, -battery) * h
        raw["supply"] += (max(0.0, solar) + max(0.0, -battery)
                          + max(0.0, grid)) * h
        for k, w in fn(solar, battery, grid, house, tesla).items():
            out[k] = out.get(k, 0.0) + w * h
    return {"flows": out, "raw": raw, "gap_s": gap_s, "live_s": live_s,
            "tesla_clamped_s": tesla_clamped_s}


def _r(day, fn=None):
    key = ("r", day, getattr(fn, "__name__", "default"),
           getattr(flows, "LOSS_RULE", None))
    if key not in _CACHE:
        _CACHE[key] = replay(day, fn)
    return _CACHE[key]


def outbound(f, source):
    return sum(v for k, v in f.items() if k.startswith(source + "_to_"))


def inbound(f, sink):
    return sum(v for k, v in f.items() if k.endswith("_to_" + sink))


def tesla_counter_kwh(day):
    """Energy on `sensor.tesla_home_charging_energy` over the day.

    It is a `total_increasing` accumulator, not a daily counter, so the day's
    energy is last minus first. `first`/`last` come from the capture itself.
    """
    c = tesla_history(day)["counters"]["tesla_home_charging_energy"]
    return float(c["last"]) - float(c["first"])


def reported_nodes(day):
    """The eight node totals for one day, with None where a figure is unknowable.

    House and Tesla are None on a Tesla-blind day. They are the ONE thing the
    dead sensor decides: `Hr + T == house_load` whatever T is, so every other
    node total survives, but the split between these two is exactly what is
    missing. A number there would be fabricated, and on 2026-08-27 it would be
    fabricated by 8.384 kWh in a known direction.
    """
    f = _r(day)["flows"]
    blind = not tesla_usable(day)
    return {
        "solar": outbound(f, "solar"),
        "battery_out": outbound(f, "battery"),
        "grid_import": outbound(f, "grid"),
        "grid_export": inbound(f, "export"),
        "battery_in": inbound(f, "battery"),
        "inverter": inbound(f, "inverter"),
        "house": None if blind else inbound(f, "house"),
        "tesla": None if blind else inbound(f, "tesla"),
    }


COUNTER_KEY = {
    "solar": "solar_today",
    "grid_import": "grid_import_today",
    "grid_export": "grid_export_today",
    "battery_charge": "battery_charge_today",
    "battery_discharge": "battery_discharge_today",
    "house": "house_consumption_today",
}


def counter(day, name):
    return counters()["days"][day][COUNTER_KEY[name]]["max"]


def pct(got, want):
    return float("nan") if not want else 100.0 * (got - want) / want


# ---------------------------------------------------------------------------
# Tolerances.
#
# Every number below is a bound on something MEASURED, chosen after reading the
# tables printed by `__main__`, with headroom over the observed value. None of
# them is wide enough to hide a regression that matters; each says in words what
# it is protecting.
# ---------------------------------------------------------------------------

# The recorder is essentially complete on these four days.
MAX_GAP_FRACTION = 0.01

# Integrated source conservation. v2 has no fitted constants, so per sample this
# is arithmetic rather than physics -- but only inside the well-conditioned
# regime, and MEASURED over these four days real data leaves that regime for
# 1226-3792 s/day. So the day total is near-exact, not exact:
#
#     day          solar     battery       grid     regime-2 energy
#     2026-08-27  +0.236%     +0.097%    +0.161%       0.108 kWh
#     2026-08-28  +0.112%     +0.691%    +0.070%       0.125 kWh
#     2026-08-29  +0.113%     -0.176%    +0.053%       0.043 kWh
#     2026-08-30  +0.421%     +0.887%    +0.021%       0.148 kWh
#
# The absolute bound is the meaningful one: the error is a roughly fixed
# ~0.1 kWh/day of over-spend, so it looks worst on the battery simply because
# the battery's daily total is the smallest. Both bounds are checked; the
# percentage one exists to catch a scale error the absolute one would miss on a
# big day, and vice versa.
SOURCE_CONSERVATION_PCT = 1.5
SOURCE_CONSERVATION_KWH = 0.30

# The regime-2 over-spend that causes the above, bounded directly so a
# regression is attributed rather than merely observed.
MAX_REGIME_ENERGY_KWH = 0.40

# Sink conservation for the four shared sinks is exact by construction.
SINK_EXACT_KWH = 5e-3

# The daily counters are quantised to 0.1 kWh and integrate a different
# measurement path from the power sensors, so a few percent against them is the
# floor of what is achievable, not a defect in the decomposition. v1 measured
# the same gap.
COUNTER_TOL_PCT = 6.0

# House is compared against house_consumption_today MINUS the car. house_load
# itself runs low against its own counter every day -- CLAUDE.md records ~60-85 W
# and v1's t-replay measured the same. Measured here as a mean deficit:
#
#     2026-08-27  100.7 W    2026-08-29   79.5 W
#     2026-08-28   75.7 W    2026-08-30   52.7 W (16.1 h)
#
# Bounded in WATTS, not per cent. As a percentage the same fixed offset reads
# -6% on a heavy day and -14% on a light one, so a percentage bound wide enough
# for 2026-08-28 would let a genuine scale error through on 2026-08-30.
HOUSE_DEFICIT_W_MIN = 25.0
HOUSE_DEFICIT_W_MAX = 150.0

# Tesla node vs its own energy sensor. The node is the clamped integral of the
# same power series, so the only legitimate difference is the clamp.
TESLA_TOL_PCT = 3.0

# The Inverter node, as a fraction of the day's supply. flow-law fitted a ~106 W
# fixed parasitic, which is ~2.5 kWh/day; against a 20-40 kWh/day supply that is
# roughly 5-15%. Anything far outside says the residual is not the parasitic.
INVERTER_MIN_PCT_OF_SUPPLY = 1.0
INVERTER_MAX_PCT_OF_SUPPLY = 30.0

# The 5-minute CSV is quantised to 0.1 kWh per bucket (1.2 kW for tesla), so it
# is a sanity check and nothing more.
CSV_TOL_KWH = 3.0


# ===========================================================================
# 1. The fixtures themselves
# ===========================================================================

@pytest.mark.parametrize("day", ALL_DAYS)
def test_both_captures_exist_for_every_day(day):
    for name in ("history_%s.json.gz", "tesla_history_%s.json.gz"):
        p = os.path.join(FIXTURES, name % day)
        assert os.path.exists(p), p


@pytest.mark.parametrize("day", ALL_DAYS)
def test_the_two_captures_cover_the_same_local_day(day):
    h, t = history(day), tesla_history(day)
    assert h["start_epoch"] == t["start_epoch"]
    assert h["end_epoch"] == t["end_epoch"]
    assert h["tz"] == "Europe/London"
    span = float(h["end_epoch"]) - float(h["start_epoch"])
    assert span == 86400.0, "%s spans %.0f s" % (day, span)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_every_inverter_channel_has_a_sample_at_or_before_the_day_start(day):
    """Without one, the first interval has no held value and the integral loses
    however long it takes the channel to report."""
    s = series(day)
    for c in INVERTER_CHANNELS:
        assert s[c] and s[c][0][0] <= float(history(day)["start_epoch"]) + 1.0, \
            "%s/%s first sample is %.0f s into the day" % (
                day, c, s[c][0][0] - float(history(day)["start_epoch"]))


def tesla_usable(day):
    """True when the Tesla power sensor covered the whole window."""
    s = series(day)
    return bool(s["tesla"]) and \
        s["tesla"][0][0] <= float(history(day)["start_epoch"]) + 1.0


def test_the_usable_tesla_days_are_the_ones_we_think_they_are():
    """TESLA_DAYS is a claim about the fixtures. Derive it and compare, so a
    re-capture that fixes 2026-08-27 -- or breaks another day -- is noticed
    instead of silently changing which days carry Tesla numbers."""
    derived = tuple(d for d in ALL_DAYS if tesla_usable(d))
    assert derived == TESLA_DAYS
    assert tuple(d for d in ALL_DAYS if not tesla_usable(d)) == TESLA_BLIND_DAYS


def test_2026_08_27_has_no_tesla_sensor_for_most_of_the_day():
    """The exclusion, stated from the data. Its first power sample is 13.5 h
    into the day, and CLAUDE.md records the car taking 8.384 kWh AC-side that
    same morning at 00:30-01:00, 02:00-02:30 and 05:00-05:16 -- entirely inside
    the blind window. House and Tesla are therefore unknowable on this day."""
    day = "2026-08-27"
    start = float(history(day)["start_epoch"])
    first = series(day)["tesla"][0][0]
    assert first - start > 12 * 3600, "%s: first tesla sample at +%.0f s" % (
        day, first - start)
    assert not tesla_usable(day)


def test_a_derived_sensors_zero_is_not_evidence_about_its_source():
    """The trap three agents walked into, pinned so a fourth does not.

    `sensor.tesla_home_charging_energy` is the Riemann integral OF
    `sensor.tesla_home_charging_power`. On 2026-08-27 the energy sensor's own
    first recorded state is `unknown`, at essentially the same instant the power
    sensor was created -- so it never had the opportunity to observe the morning
    either. Its 0.000 is the same absence wearing a second hat, and it
    corroborates nothing.

    The general shape, worth carrying: a DERIVED sensor reading zero looks like
    independent confirmation of its own source, and never is.
    """
    day = "2026-08-27"
    start = float(history(day)["start_epoch"])
    points = tesla_history(day)["counters"]["tesla_home_charging_energy"]["points"]
    first_energy_ts = min(float(t) for t, _ in points)
    first_power_ts = series(day)["tesla"][0][0]
    assert first_energy_ts - start > 12 * 3600, (
        "the energy sensor DID observe the morning; this reasoning needs redoing")
    assert abs(first_energy_ts - first_power_ts) < 3600.0, (
        "the two sensors were not created together, so the argument above "
        "no longer holds: energy at +%.0f s, power at +%.0f s"
        % (first_energy_ts - start, first_power_ts - start))
    assert points[0][1] in ("unknown", "unavailable"), (
        "the energy sensor's first state is %r, not an explicit unknown"
        % (points[0][1],))


@pytest.mark.parametrize("day", TESLA_DAYS)
def test_tesla_power_sampling_is_complete_on_every_usable_day(day):
    """Settled by MEASUREMENT, not by sample count.

    TeslaMate publishes on change, so a day with five samples may be perfectly
    complete -- CLAUDE.md records a 32 A plateau going 80 minutes without an
    update. Integrating the power with the same left/step rule the HA helper
    uses and comparing against `sensor.tesla_home_charging_energy` settles it:

        2026-08-28    5 samples    0.0551 kWh integrated vs 0.0550 counted
        2026-08-29  122 samples    5.3194 kWh integrated vs 5.3190 counted
        2026-08-30  391 samples   22.199  kWh integrated vs 22.199  counted

    2026-08-28's five samples are a genuine 41-second burst, not a gap.
    """
    pts = [(t, v) for t, v in series(day)["tesla"] if v is not None]
    end = float(history(day)["end_epoch"])
    total = 0.0
    for i, (t, v) in enumerate(pts):
        nxt = pts[i + 1][0] if i + 1 < len(pts) else end
        total += v * (nxt - t) / 3_600_000.0
    want = tesla_counter_kwh(day)
    assert abs(total - want) <= max(0.001, 0.01 * want), (
        "%s: integrating tesla power gives %.4f kWh against a counter delta of "
        "%.4f kWh -- the capture is missing samples" % (day, total, want))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_recorder_gaps_are_small_and_explicit(day):
    r = _r(day)
    total = r["gap_s"] + r["live_s"]
    assert total > 0
    assert r["gap_s"] / total <= MAX_GAP_FRACTION, (
        "%s: %.0f s of %.0f s is unavailable" % (day, r["gap_s"], total))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_tesla_power_is_never_negative_in_the_capture(day):
    for ts, v in series(day)["tesla"]:
        assert v is None or v >= 0.0, "%s: tesla %r at %.0f" % (day, v, ts)


def test_the_tesla_capture_actually_contains_a_charging_session():
    """A replay against four days of zero car is not a test of the Tesla split.
    2026-08-29 and -30 both hold real ~7 kW sessions."""
    assert tesla_counter_kwh("2026-08-29") > 5.0
    assert tesla_counter_kwh(PARTIAL_DAY) > 20.0
    peak = max(v for _, v in series(PARTIAL_DAY)["tesla"] if v is not None)
    assert peak > 6000.0, "peak tesla power %.0f W" % peak


def test_the_car_charges_in_discrete_high_power_slots_as_claude_md_records():
    """CLAUDE.md: 'discrete roughly 7 kW slots', not one continuous session, and
    hourly statistics understate the rate by nearly 2x. If a future capture
    showed a flat 2.5 kW all night, that would be the house battery being
    misattributed to the car and every Tesla number here would be wrong."""
    vals = [v for _, v in series(PARTIAL_DAY)["tesla"] if v]
    above = [v for v in vals if v > 6000.0]
    assert len(above) > 0.5 * len(vals), (
        "only %d of %d non-zero tesla samples are above 6 kW"
        % (len(above), len(vals)))


# ===========================================================================
# 2. The merged timeline
# ===========================================================================

@pytest.mark.parametrize("day", ALL_DAYS)
def test_intervals_tile_the_window_exactly(day):
    start = float(history(day)["start_epoch"])
    end = day_end(day)
    total = sum(dt for _, dt, _ in intervals(day))
    assert abs(total - (end - start)) < 1e-6


@pytest.mark.parametrize("day", ALL_DAYS)
def test_intervals_are_contiguous_and_ordered(day):
    prev = None
    for t, dt, _ in intervals(day):
        if prev is not None:
            assert abs(t - prev) < 1e-6, "gap or overlap at %.3f" % t
        prev = t + dt


@pytest.mark.parametrize("day", ALL_DAYS)
def test_no_interval_exceeds_max_sub_interval(day):
    for _, dt, _ in intervals(day):
        assert dt <= MAX_SUB_INTERVAL_S + 1e-9


def test_max_sub_interval_is_identity_for_a_left_riemann_sum():
    """Splitting a held value into sub-intervals cannot change a left Riemann
    sum. If this ever fails, the harness has started interpolating and every
    number in this file is suspect."""
    a = replay(PARTIAL_DAY)
    coarse = {}
    for _t, dt, v in intervals(PARTIAL_DAY, max_sub_interval=1e12):
        if any(v[c] is None for c in CHANNELS):
            continue
        h = dt / 3_600_000.0
        for k, w in decompose(v["solar"], v["battery"], v["grid"],
                              v["house"], v["tesla"]).items():
            coarse[k] = coarse.get(k, 0.0) + w * h
    for k in a["flows"]:
        assert abs(a["flows"][k] - coarse.get(k, 0.0)) < 1e-6, k


def test_the_partial_day_is_not_truncated_by_the_on_change_tesla_sensor():
    """The trap this file exists to avoid. On 2026-08-30 tesla's last sample is
    at 05:07 while the inverter channels run to ~16:10. Letting tesla set the
    end would throw away eleven hours -- including all of the day's solar."""
    end = day_end(PARTIAL_DAY)
    start = float(history(PARTIAL_DAY)["start_epoch"])
    tesla_last = series(PARTIAL_DAY)["tesla"][-1][0]
    assert tesla_last - start < 6 * 3600, "fixture changed; rethink this test"
    assert end - start > 12 * 3600, (
        "partial day is only %.1f h long" % ((end - start) / 3600.0))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_unavailable_is_carried_as_a_gap_and_never_substituted_with_zero(day):
    """BRIEF section 11's general rule, and the same bug class as the Jinja
    `| float(0)` trap: a sensor reading 0 and a sensor not existing are
    indistinguishable downstream, so `unavailable` must become None and drop the
    interval, not become 0.0 W and quietly contribute nothing while looking like
    a measurement.

    Checked on the parser rather than on the totals, because a totals-level test
    passes either way -- 0 W over an interval also contributes nothing to the
    integral. The difference only shows in whether the time is COUNTED as
    measured, which is what `gap_s` records.
    """
    raw = history(day)["series"]
    saw_unavailable = False
    for channel in INVERTER_CHANNELS:
        parsed = dict(_numeric(raw[channel], float(history(day)["start_epoch"])))
        for ts, state in raw[channel]:
            try:
                float(state)
            except (TypeError, ValueError):
                saw_unavailable = True
                assert parsed[max(float(ts), float(history(day)["start_epoch"]))] \
                    is None, "%s/%s: %r became a number" % (day, channel, state)
    assert saw_unavailable or _r(day)["gap_s"] == 0.0, (
        "%s reports %.0f s of gap but no channel ever read unavailable"
        % (day, _r(day)["gap_s"]))


def test_a_gap_is_not_worth_the_same_as_a_measured_zero():
    """Self-check on the rule above: prove the two are distinguishable at all.

    A channel held at 0 W contributes 0 kWh and counts as LIVE time; a channel
    reading `unavailable` contributes 0 kWh and counts as GAP time. If the
    harness ever conflated them, `gap_s` would go to zero and nothing else would
    change -- so `gap_s` is the only place the distinction is observable, and it
    must not be decoration.
    """
    total = sum(_r(d)["gap_s"] for d in ALL_DAYS)
    assert total > 0.0, (
        "no day records any gap; either the fixtures changed or unavailable is "
        "being read as a number")


@pytest.mark.parametrize("day", ALL_DAYS)
def test_gap_intervals_contribute_no_energy(day):
    """An `unavailable` reading is missing data, not 0 W."""
    seen = 0.0
    for _t, dt, v in intervals(day):
        if any(v[c] is None for c in CHANNELS):
            seen += dt
    assert abs(seen - _r(day)["gap_s"]) < 1e-6


def test_replay_is_deterministic():
    a = replay("2026-08-29")
    b = replay("2026-08-29")
    assert a["flows"] == b["flows"]


# ===========================================================================
# 3. Invariants that must survive integration
# ===========================================================================

@pytest.mark.parametrize("day", ALL_DAYS)
def test_every_integrated_flow_is_non_negative(day):
    for k, v in _r(day)["flows"].items():
        assert v >= 0.0, "%s: %s = %r" % (day, k, v)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_all_twelve_flows_are_present_over_a_real_day(day):
    assert len(_r(day)["flows"]) == 12


@pytest.mark.parametrize("day", ALL_DAYS)
def test_no_battery_or_grid_energy_ever_reaches_the_export_node(day):
    f = _r(day)["flows"]
    assert set(k for k in f if k.endswith("_to_export")) == {"solar_to_export"}


def regimes(day):
    """Time and energy spent OUTSIDE the well-conditioned regime, per day.

    Regime 1 is `E > S`: solar cannot cover the metered export, so Export is
    starved and the other two sources under-spend. Regime 2 is a negative
    residual: the sinks read higher than the sources, `L` clamps to zero and
    every source over-spends. Both are consequences of five sensors polled ~10 s
    apart, not of the rule.
    """
    out = {"reg1_s": 0.0, "reg2_s": 0.0, "reg1_kwh": 0.0, "reg2_kwh": 0.0}
    for _t, dt, v in intervals(day):
        if any(v[c] is None for c in CHANNELS):
            continue
        S = max(0.0, v["solar"])
        B, C = max(0.0, -v["battery"]), max(0.0, v["battery"])
        G, E = max(0.0, v["grid"]), max(0.0, -v["grid"])
        H = max(0.0, v["house"])
        T = min(max(0.0, v["tesla"]), H)
        residual = (S + B + G) - ((H - T) + T + C + E)
        h = dt / 3_600_000.0
        if E > S:
            out["reg1_s"] += dt
            out["reg1_kwh"] += (E - S) * h
        if residual < 0.0:
            out["reg2_s"] += dt
            out["reg2_kwh"] += -residual * h
    return out


@pytest.mark.parametrize("day", ALL_DAYS)
def test_source_conservation_holds_over_a_whole_day(day):
    """Integrated over a whole day of real, skewed, ten-second data.

    NOT exact -- see the tolerance block. The brief calls source conservation
    EXACT, and per sample inside the well-conditioned regime it is; real data
    leaves that regime for up to 4.4% of the day, which shows up here as a
    roughly fixed ~0.1 kWh/day over-spend. Bounded both ways so neither a
    scale error nor an offset error can hide behind the other.
    """
    r = _r(day)
    for src, key in (("solar", "solar"), ("battery", "battery_discharge"),
                     ("grid", "grid_import")):
        got, want = outbound(r["flows"], src), r["raw"][key]
        assert abs(got - want) <= SOURCE_CONSERVATION_KWH, (
            "%s %s: %.4f kWh spent of %.4f kWh delivered (%+.4f kWh)"
            % (day, src, got, want, got - want))
        assert abs(pct(got, want)) <= SOURCE_CONSERVATION_PCT, (
            "%s %s: %.3f kWh spent of %.3f kWh delivered (%+.2f%%)"
            % (day, src, got, want, pct(got, want)))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_the_conservation_error_is_bounded_by_the_regime_energy_that_causes_it(day):
    """Attribution, not just observation.

    The over-spend on each source can only come from regime 2, so the total
    over-spend across all three sources must be no larger than the regime-2
    energy. If a future change made a source over-spend for some OTHER reason,
    this fails while the tolerance test above would still pass.
    """
    r, g = _r(day), regimes(day)
    over = sum(max(0.0, outbound(r["flows"], src) - r["raw"][key])
               for src, key in (("solar", "solar"),
                                ("battery", "battery_discharge"),
                                ("grid", "grid_import")))
    assert over <= g["reg2_kwh"] + 1e-6, (
        "%s: %.4f kWh over-spent but only %.4f kWh of negative residual exists"
        % (day, over, g["reg2_kwh"]))
    assert g["reg2_kwh"] <= MAX_REGIME_ENERGY_KWH, (
        "%s: %.3f kWh of negative residual" % (day, g["reg2_kwh"]))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_both_broken_regimes_are_actually_exercised_by_the_fixtures(day):
    """Guard against vacuity. If the captured days never left the
    well-conditioned regime, the two regime tests in
    `test_flows_invariants.py` would be theory with no evidence behind them,
    and the tolerances above would be unjustifiably generous."""
    g = regimes(day)
    assert g["reg2_s"] > 60.0, (
        "%s never has a negative residual; the regime-2 tolerances are unearned"
        % day)
    assert g["reg1_s"] > 10.0, (
        "%s never has export exceeding solar; the export-starvation test is "
        "untested against real data" % day)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_the_export_shortfall_is_small_and_is_the_only_sink_that_starves(day):
    """Regime 1 costs the Export ribbon 0.02-0.05 kWh/day -- visible in the
    arithmetic, invisible on a chart rounded to 0.1 kWh. The other four sinks
    fill exactly whatever the regime."""
    r, g = _r(day), regimes(day)
    short = r["raw"]["grid_export"] - inbound(r["flows"], "export")
    assert short >= -1e-9, "%s: export over-filled by %.4f kWh" % (day, -short)
    assert abs(short - g["reg1_kwh"]) <= 1e-6, (
        "%s: export short by %.4f kWh but regime 1 only accounts for %.4f"
        % (day, short, g["reg1_kwh"]))
    assert short <= 0.20, "%s: export short by %.3f kWh" % (day, short)


@pytest.mark.parametrize("day", ALL_DAYS)
@pytest.mark.parametrize("sink,key", [("house", "house_rest"),
                                      ("tesla", "tesla"),
                                      ("battery", "battery_charge")])
def test_sink_conservation_holds_over_a_whole_day(day, sink, key):
    """House, Tesla and Battery-in fill exactly in every regime, so integration
    must not move them at all."""
    r = _r(day)
    got, want = inbound(r["flows"], sink), r["raw"][key]
    assert abs(got - want) <= SINK_EXACT_KWH, (
        "%s %s: %.4f vs %.4f kWh" % (day, sink, got, want))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_export_is_solar_only_and_never_exceeds_the_metered_export(day):
    r = _r(day)
    assert r["flows"]["solar_to_export"] <= r["raw"]["grid_export"] + 1e-9


@pytest.mark.parametrize("day", ALL_DAYS)
def test_house_and_tesla_together_reproduce_the_house_load_integral(day):
    """The split is a partition of one metered load, so the two halves must add
    back up to it. A gap here would mean the Tesla clamp is destroying energy."""
    r = _r(day)
    got = inbound(r["flows"], "house") + inbound(r["flows"], "tesla")
    assert abs(got - r["raw"]["house"]) <= SINK_EXACT_KWH, (
        "%s: house %.4f + tesla %.4f != house_load %.4f"
        % (day, inbound(r["flows"], "house"), inbound(r["flows"], "tesla"),
           r["raw"]["house"]))


# ===========================================================================
# 4. Against the inverter's own daily counters
# ===========================================================================

@pytest.mark.parametrize("day", ALL_DAYS)
@pytest.mark.parametrize("node,key", [
    ("solar", "solar"),
    ("grid_import", "grid_import"),
    ("grid_export", "grid_export"),
    ("battery_charge", "battery_charge"),
    ("battery_discharge", "battery_discharge"),
])
def test_each_node_total_tracks_its_daily_counter(day, node, key):
    r = _r(day)
    got = {
        "solar": outbound(r["flows"], "solar"),
        "grid_import": outbound(r["flows"], "grid"),
        "grid_export": inbound(r["flows"], "export"),
        "battery_charge": inbound(r["flows"], "battery"),
        "battery_discharge": outbound(r["flows"], "battery"),
    }[node]
    want = counter(day, key)
    assert abs(pct(got, want)) <= COUNTER_TOL_PCT, (
        "%s %s: %.3f kWh vs counter %.3f kWh (%+.2f%%)"
        % (day, node, got, want, pct(got, want)))


@pytest.mark.parametrize("day", TESLA_DAYS)
def test_the_house_node_tracks_its_counter_once_the_car_is_removed(day):
    """The House node is now the rest of the house, so it faces
    `house_consumption_today` MINUS the car -- not the raw counter. Comparing it
    to the raw counter would look like a 22 kWh error on 2026-08-30.

    The residual gap is the known house_load deficit, so it is checked as a
    roughly constant power offset rather than a percentage: see the tolerance
    block for why a percentage bound cannot distinguish the two failure shapes.
    """
    r = _r(day)
    hours = (day_end(day) - float(history(day)["start_epoch"])) / 3600.0
    got = inbound(r["flows"], "house")
    want = counter(day, "house") - tesla_counter_kwh(day)
    deficit_w = 1000.0 * (want - got) / hours
    assert HOUSE_DEFICIT_W_MIN <= deficit_w <= HOUSE_DEFICIT_W_MAX, (
        "%s house: %.3f kWh vs counter-minus-car %.3f kWh -- a %.1f W mean "
        "deficit, outside the known %.0f-%.0f W house_load offset"
        % (day, got, want, deficit_w, HOUSE_DEFICIT_W_MIN, HOUSE_DEFICIT_W_MAX))


@pytest.mark.parametrize("day", TESLA_DAYS)
def test_the_house_deficit_is_an_offset_not_a_scale_error(day):
    """The same gap expressed as a percentage swings 6% to 14% across these four
    days while the wattage barely moves. That is the signature of a fixed offset
    in the house_load sensor, not of the decomposition losing a fraction of the
    house -- and it is the reason the test above is in watts."""
    r = _r(day)
    got = inbound(r["flows"], "house")
    want = counter(day, "house") - tesla_counter_kwh(day)
    assert got < want, "%s: house node exceeds its counter" % day
    assert want - got <= 3.0, (
        "%s: %.3f kWh of house is unaccounted, too much for the known offset"
        % (day, want - got))


@pytest.mark.parametrize("day", TESLA_DAYS)
def test_the_tesla_node_tracks_its_own_energy_sensor(day):
    """Tesla node vs `sensor.tesla_home_charging_energy`. Both are AC-side and
    derive from the same power series, so the only legitimate difference is the
    `T <= house_load` clamp -- and the clamp can only reduce, never inflate."""
    want = tesla_counter_kwh(day)
    got = inbound(_r(day)["flows"], "tesla")
    if want < 0.5:
        # 2026-08-28 is 55 Wh of car in a 41-second burst, and the clamp takes
        # 34 Wh of it because a 7.2 kW tesla sample lands beside a lower
        # house_load reading. As a percentage that is -62%, which says nothing
        # about a normal day; as energy it is 34 Wh. Bounded in energy.
        assert want - got <= 0.05, (
            "%s tesla: node %.4f kWh vs sensor %.4f kWh" % (day, got, want))
        return
    assert abs(pct(got, want)) <= TESLA_TOL_PCT, (
        "%s tesla: node %.3f kWh vs sensor %.3f kWh (%+.2f%%)"
        % (day, got, want, pct(got, want)))


@pytest.mark.parametrize("day", TESLA_DAYS)
def test_the_tesla_clamp_can_only_reduce_the_car_never_inflate_it(day):
    got = inbound(_r(day)["flows"], "tesla")
    assert got <= tesla_counter_kwh(day) + SINK_EXACT_KWH, (
        "%s: node %.3f kWh exceeds the sensor's %.3f kWh -- the clamp is "
        "adding energy" % (day, got, tesla_counter_kwh(day)))


@pytest.mark.parametrize("day", ("2026-08-29", PARTIAL_DAY))
def test_the_car_is_fed_mostly_by_grid_and_battery_not_by_solar(day):
    """CLAUDE.md: the car takes ~7 kW slots inside the 23:30-05:30 window. A
    Tesla figure attributed mostly to solar would mean the attribution has the
    time of day wrong, which is exactly the class of defect the proportional
    rule is supposed to make impossible."""
    f = _r(day)["flows"]
    total = inbound(f, "tesla")
    assert total > 0.5
    solar_share = f["solar_to_tesla"] / total
    assert solar_share < 0.25, (
        "%s: %.0f%% of the car came from solar (%.3f of %.3f kWh)"
        % (day, 100 * solar_share, f["solar_to_tesla"], total))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_the_inverter_node_is_a_plausible_parasitic_not_a_dumping_ground(day):
    """The Inverter node is the residual made explicit. flow-law fitted a fixed
    ~106 W parasitic, i.e. ~2.5 kWh/day. This bounds the residual as a share of
    the day's supply; a residual far outside it is not housekeeping, it is a
    sign convention or a channel mismatch."""
    r = _r(day)
    got = inbound(r["flows"], "inverter")
    share = 100.0 * got / r["raw"]["supply"]
    assert INVERTER_MIN_PCT_OF_SUPPLY <= share <= INVERTER_MAX_PCT_OF_SUPPLY, (
        "%s: Inverter node %.3f kWh = %.1f%% of %.3f kWh supplied"
        % (day, got, share, r["raw"]["supply"]))


@pytest.mark.parametrize("day", TESLA_BLIND_DAYS)
def test_house_and_tesla_are_reported_as_unknowable_on_a_tesla_blind_day(day):
    """Not zero, not a number with a caveat -- unknowable.

    The split of `house_load` between House and Tesla is the ONE thing the dead
    sensor decides, so on a blind day neither figure means anything. Reporting
    House as "-8.31% against its counter" there is a fabricated measurement:
    the true House is 8.384 kWh lower and the true Tesla is 8.384 kWh higher.
    `reported_nodes()` returns None for both, and this is the test that keeps it
    that way.
    """
    got = reported_nodes(day)
    assert got["house"] is None and got["tesla"] is None
    for other in ("solar", "battery_out", "grid_import", "grid_export",
                  "battery_in", "inverter"):
        assert got[other] is not None, (
            "%s is reported None on a Tesla-blind day, but it does not depend "
            "on the Tesla reading at all" % other)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_six_node_totals_are_provably_blind_to_the_tesla_reading(day):
    """Why 2026-08-27's other figures survive a dead car sensor.

    `Hr + T == house_load` for every T, and the three shares depend on S1, B and
    G alone, so replacing the whole Tesla series with any other series leaves
    the six non-Tesla node totals untouched. Demonstrated on real data by
    replaying each day twice -- once with the true Tesla channel and once with
    it forced to zero -- rather than argued from the algebra.

    Six, not ten: the three `*_to_house` and three `*_to_tesla` flows DO depend
    on T. It is their SUM that does not.
    """
    truth = _r(day)["flows"]

    def _blind(solar, battery, grid, house, tesla):
        # Force T to its MAXIMUM, not to zero. Forcing zero would compare the
        # held-zero replay against itself on 2026-08-27 and prove nothing --
        # the same shape of tautology as justifying the hold by the derived
        # energy counter. T = house is the far end of the legal range.
        return decompose(solar, battery, grid, house, max(0.0, house))

    blinded = replay(day, fn=_blind)["flows"]
    for node, fn in (("solar", lambda f: outbound(f, "solar")),
                     ("battery_out", lambda f: outbound(f, "battery")),
                     ("grid_import", lambda f: outbound(f, "grid")),
                     ("grid_export", lambda f: inbound(f, "export")),
                     ("battery_in", lambda f: inbound(f, "battery")),
                     ("inverter", lambda f: inbound(f, "inverter"))):
        assert abs(fn(truth) - fn(blinded)) < 1e-9, (
            "%s: %s moves by %.6f kWh when the Tesla channel is blanked"
            % (day, node, fn(truth) - fn(blinded)))
    assert abs(inbound(truth, "house") - inbound(blinded, "house")) > 0.5, (
        "%s: House did NOT move when T was forced to the top of its range, so "
        "this day cannot demonstrate the dependency it is supposed to" % day)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_metered_export_that_vanishes_because_solar_reads_zero(day):
    """BRIEF section 10, measured on real days rather than reasoned about.

    `s2e = min(S, E)`, so a metered export coinciding with solar reading exactly
    zero is drawn with no inbound ribbon at all and its energy leaves the chart.
    What that costs here:

        day          worst sample   duration   energy dropped
        2026-08-27      138 W          93 s      0.0018 kWh
        2026-08-28      155 W         104 s      0.0023 kWh
        2026-08-29     1903 W         303 s      0.0165 kWh
        2026-08-30        0 W           0 s      0      kWh

    The worst input is not the biggest export -- it is the biggest export while
    solar reads zero, and that was 1903 W on 2026-08-29. On a live chart that is
    a two-kilowatt Export box with nothing entering it, which reads as a fault;
    on a daily chart it is 16.5 Wh and rounds to 0.0. Both bounds are asserted,
    because only one of them is reassuring.
    """
    worst_w, lost_kwh, blind_s = 0.0, 0.0, 0.0
    for _t, dt, v in intervals(day):
        if any(v[c] is None for c in CHANNELS):
            continue
        S, E = max(0.0, v["solar"]), max(0.0, -v["grid"])
        if S == 0.0 and E > 0.0:
            worst_w = max(worst_w, E)
            blind_s += dt
            lost_kwh += E * dt / 3_600_000.0
    assert lost_kwh <= 0.05, (
        "%s: %.4f kWh of metered export has no source at all" % (day, lost_kwh))
    assert worst_w <= 3000.0, (
        "%s: %.0f W of export drawn with no inbound ribbon -- large enough to "
        "read as a fault on a live chart" % (day, worst_w))
    assert blind_s <= 900.0, "%s: %.0f s with export unattributable" % (day, blind_s)


@pytest.mark.parametrize("day", TESLA_DAYS)
def test_the_tesla_clamp_costs_little_on_a_day_with_a_real_charge(day):
    """The clamp is required -- house_load minus a held TeslaMate value can go
    negative on the sample where a charge stops -- but it discards energy, so
    the amount is bounded rather than assumed small. Measured: 0.023-0.034 kWh
    per day, over 21-59 s. That is 0.6% of 2026-08-29's 5.3 kWh charge and 62%
    of 2026-08-28's 55 Wh one, which is why the tolerance elsewhere is in energy
    and not per cent for the tiny day."""
    clamped = 0.0
    for _t, dt, v in intervals(day):
        if any(v[c] is None for c in CHANNELS):
            continue
        t_raw, h = max(0.0, v["tesla"]), max(0.0, v["house"])
        if t_raw > h:
            clamped += (t_raw - h) * dt / 3_600_000.0
    assert clamped <= 0.10, "%s: the clamp discarded %.4f kWh" % (day, clamped)


@pytest.mark.parametrize("day", ALL_DAYS)
def test_the_inverter_node_agrees_with_the_independently_fitted_parasitic(day):
    """flow-law fitted `AC = 0.9974*DC - 105.9 W` over 87.7 h and 64,084 samples
    -- a roughly FIXED ~106 W of inverter housekeeping, not a percentage.

    Nothing in v2 is tuned to that number: the Inverter node is the raw measured
    residual. So this is a genuine independent cross-check, and it is expressed
    as a mean wattage because that is the shape flow-law fitted. Measured here:
    132.5, 129.0, 104.5 and 103.1 W across the four days, mean ~117 W. The band
    is deliberately wider than that spread but far narrower than any value a
    sign-convention or channel-mismatch error would produce.
    """
    r = _r(day)
    hours = (day_end(day) - float(history(day)["start_epoch"])) / 3600.0
    got = inbound(r["flows"], "inverter")
    watts = 1000.0 * got / hours
    assert 60.0 <= watts <= 200.0, (
        "%s: Inverter node %.3f kWh over %.2f h = %.1f W mean, against "
        "flow-law's fitted 105.9 W parasitic" % (day, got, hours, watts))


def test_the_inverter_node_is_not_merely_tracking_the_days_supply():
    """A residual proportional to throughput would be a conversion-efficiency
    term, not a parasitic, and would justify a completely different node. The
    four days span 30.4-54.5 kWh of supply; if the residual were proportional,
    its wattage would spread as widely as the supply does."""
    watts, supply = [], []
    for day in ALL_DAYS:
        r = _r(day)
        hours = (day_end(day) - float(history(day)["start_epoch"])) / 3600.0
        watts.append(1000.0 * inbound(r["flows"], "inverter") / hours)
        supply.append(r["raw"]["supply"] / hours)
    assert max(supply) / min(supply) > 1.5, "the fixture days are too alike"
    assert max(watts) / min(watts) < 1.5, (
        "Inverter node varies %.1f-%.1f W while supply varies %.2f-%.2f kW; "
        "that is a proportional loss, not a fixed parasitic"
        % (min(watts), max(watts), min(supply), max(supply)))


@pytest.mark.parametrize("day", ALL_DAYS)
def test_the_decomposition_creates_no_energy_over_a_whole_day(day):
    r = _r(day)
    total = sum(r["flows"].values())
    assert total <= r["raw"]["supply"] * 1.005, (
        "%s: %.3f kWh of flow from %.3f kWh of supply"
        % (day, total, r["raw"]["supply"]))


# ===========================================================================
# 5. The independent 5-minute CSV
# ===========================================================================

def _csv_rows():
    with open(os.path.join(FIXTURES, "flow_5min_2026-08-30.csv")) as fh:
        return list(csv.DictReader(fh))


def _csv_end_epoch():
    """The CSV covers 00:00-15:45 local; its last bucket starts at 15:45."""
    rows = _csv_rows()
    hh, mm = rows[-1]["time"].split(":")
    return float(history(PARTIAL_DAY)["start_epoch"]) + int(hh) * 3600 + int(mm) * 60


@pytest.mark.parametrize("col,node", [
    ("solar", "solar"), ("imp", "grid_import"), ("exp", "grid_export"),
    ("bin", "battery_charge"), ("bout", "battery_discharge"),
])
def test_the_independent_five_minute_csv_agrees_on_the_raw_channels(col, node):
    """A completely separate dataset from earlier work, quantised to 0.1 kWh per
    bucket. It cannot validate the decomposition, but it can catch a merge that
    silently dropped or duplicated a stretch of the day."""
    end = _csv_end_epoch()
    r = replay(PARTIAL_DAY, end=end)
    want = sum(float(row[col]) for row in _csv_rows())
    got = r["raw"][node]
    assert abs(got - want) <= CSV_TOL_KWH, (
        "%s: replay %.2f kWh vs csv %.2f kWh" % (node, got, want))


def test_the_five_minute_csv_and_the_tesla_capture_agree_to_the_watt_hour():
    """The strongest independent check in this file, and it was luck rather than
    design: the CSV's `tesla` column sums to 22.199 kWh and
    `sensor.tesla_home_charging_energy` moved 22.199 kWh on the same day. Two
    captures taken at different times by different code agree exactly, so the
    Tesla channel this file merges is beyond doubt."""
    csv_total = sum(float(row["tesla"]) for row in _csv_rows())
    assert abs(csv_total - tesla_counter_kwh(PARTIAL_DAY)) < 0.01, (
        "csv %.3f kWh vs counter %.3f kWh"
        % (csv_total, tesla_counter_kwh(PARTIAL_DAY)))
    node = inbound(_r(PARTIAL_DAY)["flows"], "tesla")
    assert abs(node - csv_total) / csv_total < TESLA_TOL_PCT / 100.0, (
        "tesla node %.3f kWh vs an independent capture's %.3f kWh"
        % (node, csv_total))


def test_the_csv_is_too_coarse_to_settle_anything_finer():
    """Guard on the guard: if the CSV quantum ever tightened, the tolerance
    above should tighten with it rather than staying loose out of habit."""
    vals = [float(r["solar"]) for r in _csv_rows()]
    quanta = set(round(v * 10) for v in vals)
    assert all(abs(v * 10 - round(v * 10)) < 1e-6 for v in vals), \
        "the CSV is no longer quantised to 0.1 kWh; retighten CSV_TOL_KWH"
    assert len(quanta) < 60


# ===========================================================================
# 6. LOSS_RULES, measured rather than argued
# ===========================================================================

def _set_rule(name):
    for fn in ("set_loss_rule", "set_loss_treatment", "set_rule"):
        if hasattr(flows, fn):
            return getattr(flows, fn)(name)
    previous = flows.LOSS_RULE
    flows.LOSS_RULE = name
    return previous


def loss_rule_table():
    """{rule: {day: {metric: value}}} over every rule and every day."""
    rules = sorted(getattr(flows, "LOSS_RULES", {"default": None}))
    previous = getattr(flows, "LOSS_RULE", None)
    table = {}
    try:
        for rule in rules:
            if previous is not None:
                _set_rule(rule)
            table[rule] = {}
            for day in ALL_DAYS:
                r = replay(day)
                f = r["flows"]
                table[rule][day] = {
                    "solar_err": pct(outbound(f, "solar"), counter(day, "solar")),
                    "batt_out_err": pct(outbound(f, "battery"),
                                        counter(day, "battery_discharge")),
                    "grid_in_err": pct(outbound(f, "grid"),
                                       counter(day, "grid_import")),
                    "inverter_kwh": inbound(f, "inverter"),
                    "tesla_solar_pct": (
                        100.0 * f["solar_to_tesla"] / inbound(f, "tesla")
                        if inbound(f, "tesla") > 0.01 else float("nan")),
                }
    finally:
        if previous is not None:
            _set_rule(previous)
    return table


@pytest.mark.skipif(len(getattr(flows, "LOSS_RULES", {})) < 2,
                    reason="only one loss rule implemented")
def test_the_two_loss_rules_are_measurably_different_over_real_days():
    """BRIEF section 8 asks for both to be MEASURED, not argued about. A seam
    whose two arms produce the same day totals is decoration."""
    t = loss_rule_table()
    rules = sorted(t)
    a, b = t[rules[0]], t[rules[1]]
    diffs = [abs(a[d]["batt_out_err"] - b[d]["batt_out_err"]) for d in ALL_DAYS]
    assert max(diffs) > 0.5, (
        "the two loss rules agree to within %.3f%% on battery-out across every "
        "day, so the seam measures nothing" % max(diffs))


@pytest.mark.skipif(len(getattr(flows, "LOSS_RULES", {})) < 2,
                    reason="only one loss rule implemented")
@pytest.mark.parametrize("rule", sorted(getattr(flows, "LOSS_RULES", {})))
@pytest.mark.parametrize("day", ALL_DAYS)
def test_every_loss_rule_survives_a_full_day_replay(rule, day):
    """The seam may change WHERE the shared loss is charged. It may not make a
    flow negative, starve a sink that the shares are supposed to fill exactly,
    or move the Inverter node -- the residual is measured before any rule runs,
    so it must be identical under both."""
    previous = _set_rule(rule)
    try:
        r = replay(day)
        f = r["flows"]
        for k, v in f.items():
            assert v >= 0.0 and math.isfinite(v), (rule, day, k, v)
        for sink, key in (("house", "house_rest"), ("tesla", "tesla"),
                          ("battery", "battery_charge")):
            assert abs(inbound(f, sink) - r["raw"][key]) <= SINK_EXACT_KWH, (
                "%s/%s: %s filled %.4f of %.4f kWh"
                % (rule, day, sink, inbound(f, sink), r["raw"][key]))
        assert f["solar_to_export"] <= r["raw"]["grid_export"] + 1e-9
    finally:
        _set_rule(previous)


@pytest.mark.skipif(len(getattr(flows, "LOSS_RULES", {})) < 2,
                    reason="only one loss rule implemented")
def test_the_inverter_node_is_identical_under_every_loss_rule():
    """The residual is measured from the readings before any rule runs, so the
    rule can only redistribute it. A rule that changed the Inverter total would
    be inventing or destroying the loss rather than attributing it."""
    t = loss_rule_table()
    for day in ALL_DAYS:
        vals = [t[rule][day]["inverter_kwh"] for rule in t]
        assert max(vals) - min(vals) < 1e-9, (
            "%s: Inverter node varies %r across loss rules" % (day, vals))


@pytest.mark.skipif(len(getattr(flows, "LOSS_RULES", {})) < 2,
                    reason="only one loss rule implemented")
def test_the_default_loss_rule_is_the_one_that_conserves_the_sources_better():
    """BRIEF section 8: default stays aggregate-proportional 'unless the
    measurement says otherwise'. This IS that measurement, and it must be
    re-run rather than trusted if either rule changes.

    Read with the caveat that proportional is exact-by-construction against
    these same readings, so the comparison partly measures its own definition.
    The independent argument is that band_table buys its delivery claim with a
    five-band fitted efficiency table -- exactly the fitted constants v2 set out
    to remove -- and cannot be validated offline.
    """
    t = loss_rule_table()
    score = {rule: sum(abs(t[rule][d][k]) for d in ALL_DAYS
                       for k in ("solar_err", "batt_out_err", "grid_in_err"))
             for rule in t}
    best = min(score, key=score.get)
    assert best == getattr(flows, "LOSS_RULE"), (
        "the selected rule is %r but %r conserves the sources better: %r"
        % (getattr(flows, "LOSS_RULE"), best, score))


# ===========================================================================
# 7. Report
# ===========================================================================

def _report():
    print("Sankey v2 replay -- %s" % ", ".join(ALL_DAYS))
    print("loss rule in force: %r" % getattr(flows, "LOSS_RULE", "n/a"))
    for day in ALL_DAYS:
        r = replay(day)
        f = r["flows"]
        span = (day_end(day) - float(history(day)["start_epoch"])) / 3600.0
        print("\n=== %s%s  %.2f h  gap %.0f s  tesla-clamped %.0f s ===" % (
            day, "  (PARTIAL)" if day == PARTIAL_DAY else "", span,
            r["gap_s"], r["tesla_clamped_s"]))
        n = reported_nodes(day)
        blind = not tesla_usable(day)
        print("  %-22s %9s %9s %9s" % ("node", "flows", "counter", "err%"))
        rows = [
            ("Solar (out)", n["solar"], counter(day, "solar")),
            ("Battery out", n["battery_out"], counter(day, "battery_discharge")),
            ("Grid import (out)", n["grid_import"], counter(day, "grid_import")),
            ("Grid export (in)", n["grid_export"], counter(day, "grid_export")),
            ("Battery in", n["battery_in"], counter(day, "battery_charge")),
            ("House (in)", n["house"],
             None if blind else counter(day, "house") - tesla_counter_kwh(day)),
            ("Tesla (in)", n["tesla"], None if blind else tesla_counter_kwh(day)),
            ("Inverter (in)", n["inverter"], None),
        ]
        for name, got, want in rows:
            if got is None:
                print("  %-22s %9s %9s %9s   (Tesla sensor did not exist)"
                      % (name, "n/a", "n/a", "n/a"))
            elif want is None:
                print("  %-22s %9.3f %9s %9s" % (name, got, "-", "-"))
            else:
                print("  %-22s %9.3f %9.3f %+9.2f"
                      % (name, got, want, pct(got, want)))
        print("  supply %.3f kWh   house_load %.3f   house_rest %s   tesla %s"
              % (r["raw"]["supply"], r["raw"]["house"],
                 "n/a" if blind else "%.3f" % r["raw"]["house_rest"],
                 "n/a" if blind else "%.3f" % r["raw"]["tesla"]))
        print("  inverter node = %.1f%% of supply"
              % (100.0 * inbound(f, "inverter") / r["raw"]["supply"]))
        t = 0.0 if blind else inbound(f, "tesla")
        if t > 0.01:
            print("  tesla split: solar %.1f%%  battery %.1f%%  grid %.1f%%" % (
                100 * f["solar_to_tesla"] / t, 100 * f["battery_to_tesla"] / t,
                100 * f["grid_to_tesla"] / t))
        # On a Tesla-blind day the six *_to_house and *_to_tesla flows are
        # individually unknowable, even though their pairwise sums are not.
        blind_keys = set() if not blind else {
            k for k in f if k.endswith(("_to_house", "_to_tesla"))}
        print("  flows: " + ", ".join(
            "%s=%s" % (k, "n/a" if k in blind_keys else "%.3f" % v)
            for k, v in sorted(f.items())))
        if blind:
            print("         (the six *_to_house / *_to_tesla flows depend on the "
                  "dead Tesla sensor; their pairwise sums do not)")

    if len(getattr(flows, "LOSS_RULES", {})) >= 2:
        print("\n=== LOSS_RULES comparison ===")
        t = loss_rule_table()
        print("  %-14s %-12s %8s %8s %8s %9s %9s" % (
            "rule", "day", "solar%", "battout%", "gridin%", "inv kWh", "tesla<-sun%"))
        for rule in sorted(t):
            for day in ALL_DAYS:
                m = t[rule][day]
                print("  %-14s %-12s %+8.2f %+8.2f %+8.2f %9.3f %9.1f" % (
                    rule, day, m["solar_err"], m["batt_out_err"],
                    m["grid_in_err"], m["inverter_kwh"], m["tesla_solar_pct"]))


if __name__ == "__main__":
    _report()
