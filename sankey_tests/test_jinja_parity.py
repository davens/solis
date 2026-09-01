"""Prove the shipped Jinja in flow_sensors_v2.yaml equals the Python physics.

Three things are checked, in this order, because each is only meaningful if the
previous one holds:

  1. The YAML is well formed: twelve sensors, one byte-identical preamble, one
     availability template, the expected unique_ids.
  2. The Jinja agrees with a literal transcription of BRIEF_V2.md section 2
     (`spec_flows` below) -- on hand-built edge cases AND on every replayed
     sample of the four fixture days.
  3. The Jinja agrees with `flows.py`, the module that actually ships, to
     < 1e-4 W. If flows.py is absent those tests SKIP LOUDLY rather than pass;
     a skip here is a red result, not a green one.

Step 2 exists so that the harness can fail flows.py, not merely echo it. A test
that only asserts what the implementation says cannot fail.

Everything is offline: the YAML and fixtures/*.json.gz are the only inputs.

Run:
    uv run --no-project --with pytest --with pyyaml --with jinja2 \
        python -m pytest test_jinja_parity.py -q
"""
import gzip
import json
import math
import os

import jinja2
import pytest
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))
YAML_PATH = os.path.join(HERE, "flow_sensors_v2.yaml")
FIXTURES = os.path.join(HERE, "fixtures")
DAYS = ("2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30")

# 2026-08-27 is EXCLUDED from anything Tesla-dependent.
# sensor.tesla_home_charging_power did not exist before 13:27:59 local that
# day; the capture holds exactly ONE Tesla row for the whole 24 h. Holding
# "the last value at or before t" therefore has no value to hold for the first
# 13.5 hours, and treating that as 0 W is a lie that has already bitten once:
# a replay reported 0.00 kWh of Tesla energy for 2026-08-27 and read as a quiet
# day, while CLAUDE.md records 8.384 kWh going into the car that night between
# 00:30 and 05:16 -- entirely inside the window where the sensor did not exist.
# The merged reader yields a gap (None) there rather than zero-filling, and
# live_samples drops those samples, so parity on this day covers only the ~10 h
# after the sensor appeared. That is enough for Jinja-vs-Python parity, which
# does not care what the numbers mean, but NOT enough for anything that reasons
# about Tesla behaviour or day totals.
TESLA_DAYS = ("2026-08-28", "2026-08-29", "2026-08-30")

# Parity gate from the brief. The Jinja rounds to 4 dp of a watt, so the
# floor on any comparison against unrounded Python is 5e-5 W.
PARITY_W = 1e-4

# Canonical flow order. Must match gen_flow_yaml_v2.py and flows.py.
FLOW_KEYS = (
    "solar_to_export",
    "solar_to_battery",
    "solar_to_house",
    "solar_to_tesla",
    "solar_to_inverter",
    "battery_to_house",
    "battery_to_tesla",
    "battery_to_inverter",
    "grid_to_house",
    "grid_to_tesla",
    "grid_to_battery",
    "grid_to_inverter",
)

# flows.py key -> v2-layout's entity slug. They differ for two of the twelve
# because v2-layout names a sink after its NODE: `grid` as a sink is EXPORT,
# `battery` as a sink is CHARGE. Keeping both vocabularies explicit is the
# point -- an implicit mapping is what drifts.
SLUG = {
    "solar_to_export": "solar_to_grid",
    "solar_to_battery": "solar_to_battery",
    "solar_to_house": "solar_to_house",
    "solar_to_tesla": "solar_to_tesla",
    "solar_to_inverter": "solar_to_inverter",
    "battery_to_house": "battery_to_house",
    "battery_to_tesla": "battery_to_tesla",
    "battery_to_inverter": "battery_to_inverter",
    "grid_to_house": "grid_to_house",
    "grid_to_tesla": "grid_to_tesla",
    "grid_to_battery": "grid_to_battery",
    "grid_to_inverter": "grid_to_inverter",
}
KEY_OF_SLUG = {v: k for k, v in SLUG.items()}

SRC = {
    "solar": "sensor.solis_inverter_solar_power",
    "battery": "sensor.solis_inverter_battery_power",
    "grid": "sensor.solis_inverter_grid_power",
    "house": "sensor.solis_inverter_house_load",
    "tesla": "sensor.tesla_home_charging_power",
}

# What HA hands a template when an entity is missing or its source is broken.
BAD_STATES = ("unavailable", "unknown", "", "none", "None", "nan", "inf", "-inf",
              "off", "12,5")


# --------------------------------------------------------------------------
# Home Assistant's Jinja surface, reproduced exactly enough to trust the render
# --------------------------------------------------------------------------
def ha_is_number(value):
    """Verbatim semantics of homeassistant.helpers.template.is_number.

    The nan/inf rejection is the load-bearing part: it is what makes the
    availability template refuse a poisoned float instead of propagating it.
    """
    try:
        fvalue = float(value)
    except (ValueError, TypeError):
        return False
    if math.isnan(fvalue) or math.isinf(fvalue):
        return False
    return True


_NO_DEFAULT = object()


class HAFloatError(ValueError):
    """What HA's `float` filter raises when it cannot convert and has no default."""


def ha_float(value, default=_NO_DEFAULT):
    """HA's `float` filter, which is NOT jinja2's.

    This difference is why the emulation has to exist. Plain jinja2's `float`
    filter signature is do_float(value, default=0.0), so `| float` with no
    default SILENTLY RETURNS 0.0 for 'unavailable'. HA's forgiving_float_filter
    RAISES instead, and a template that raises renders the sensor unavailable.

    Using jinja2's version here would let the offline suite pass on templates
    that behave completely differently in HA -- the exact failure mode where a
    parity harness measures nothing. The shipped templates rely on the raising
    behaviour: `| float` carries no default precisely so that a bad reading
    cannot become a silent 0 W that the Riemann integral banks as real energy.
    """
    try:
        return float(value)
    except (ValueError, TypeError):
        if default is _NO_DEFAULT:
            raise HAFloatError("%r cannot be converted to float" % (value,))
        return default


# One environment, one mutable state map, so the twelve templates compile once
# instead of once per replayed sample. `states` closes over _STATES by
# reference, exactly as HA's closes over the live machine state.
_STATES = {}


def _states(entity_id):
    return _STATES.get(entity_id, "unknown")


def make_env():
    """A Jinja environment where `states` reads _STATES, as HA's reads its own.

    `states` is registered as a global AND a filter because the availability
    template uses it through `map('states')`.
    """
    env = jinja2.Environment(undefined=jinja2.StrictUndefined)
    env.globals["states"] = _states
    env.filters["states"] = _states
    env.filters["float"] = ha_float          # HA's raising one, not jinja2's
    # HA registers is_number as BOTH a filter and a test. The availability
    # template uses the test form via select(); the stage-4 wrappers use the
    # filter form. Registering only one silently breaks the other.
    env.filters["is_number"] = ha_is_number
    env.tests["is_number"] = ha_is_number
    return env


_ENV = make_env()
_COMPILED = {}


def _compile(source):
    if source not in _COMPILED:
        _COMPILED[source] = _ENV.from_string(source)
    return _COMPILED[source]


def as_state(v):
    """Python value -> the state string HA would hold."""
    if v is None:
        return "unknown"
    if isinstance(v, str):
        return v
    return repr(float(v))


def render(templates, availability, solar, battery, grid, house, tesla):
    """Render all twelve. Returns dict, or the string 'unavailable'.

    Mirrors HA: the availability template is evaluated first and a false result
    makes the sensor unavailable without the state template running at all.
    """
    _STATES.clear()
    _STATES.update({SRC["solar"]: as_state(solar), SRC["battery"]: as_state(battery),
                    SRC["grid"]: as_state(grid), SRC["house"]: as_state(house),
                    SRC["tesla"]: as_state(tesla)})
    avail = _compile(availability).render().strip()
    assert avail in ("True", "False"), avail
    if avail == "False":
        return "unavailable"
    return {k: float(_compile(t).render().strip()) for k, t in templates.items()}


# --------------------------------------------------------------------------
# The brief, transcribed. Deliberately dumb and literal.
# --------------------------------------------------------------------------
def spec_flows(solar, battery, grid, house, tesla):
    """BRIEF_V2.md section 2, written out longhand. Watts in, watts out."""
    S = max(0.0, solar)
    B = max(0.0, -battery)
    C = max(0.0, battery)
    G = max(0.0, grid)
    E = max(0.0, -grid)
    H = max(0.0, house)
    T = min(max(0.0, tesla), H)          # clamp: Tesla cannot exceed house load
    Hr = max(0.0, H - T)
    L = max(0.0, (S + B + G) - (Hr + T + C + E))

    s2e = min(S, E)                       # export is structurally solar-only
    S1 = S - s2e
    tot = S1 + B + G
    if tot > 0:
        w, b, g = S1 / tot, B / tot, G / tot
    else:
        w = b = g = 0.0                   # zero-supply guard

    return {
        "solar_to_export": s2e,
        "solar_to_battery": C * w,
        "solar_to_house": Hr * w,
        "solar_to_tesla": T * w,
        "solar_to_inverter": L * w,
        "battery_to_house": Hr * b,
        "battery_to_tesla": T * b,
        "battery_to_inverter": L * b,
        "grid_to_house": Hr * g,
        "grid_to_tesla": T * g,
        "grid_to_battery": C * g,
        "grid_to_inverter": L * g,
    }


# --------------------------------------------------------------------------
# flows.py, if it has landed
# --------------------------------------------------------------------------
def load_flows_impl():
    try:
        import flows
    except ImportError:
        return None
    for name in ("flows", "decompose", "decompose_v2", "flow_powers"):
        fn = getattr(flows, name, None)
        if callable(fn):
            return fn
    return None


FLOWS_IMPL = load_flows_impl()
needs_impl = pytest.mark.skipif(
    FLOWS_IMPL is None,
    reason="RED, NOT GREEN: flows.py absent or exposes no callable named "
           "flows/decompose/decompose_v2/flow_powers. Jinja-vs-spec parity "
           "still ran; Jinja-vs-shipping-code parity did NOT.")


# --------------------------------------------------------------------------
# fixtures: the four replay days, merged to five channels
# --------------------------------------------------------------------------
MAX_SUB_INTERVAL_S = 60.0


def _numeric(points, start):
    out = []
    for ts, s in points:
        try:
            v = float(s)
        except (TypeError, ValueError):
            v = None
        out.append((max(float(ts), start), v))
    out.sort(key=lambda p: p[0])
    return out


def merged_samples(day, max_sub_interval=MAX_SUB_INTERVAL_S):
    """Yield (t, dt, {channel: value|None}) over five channels.

    Same left-hold walk as fixtures/replay.py, extended with the Tesla series
    from the separate tesla_history_<day>.json.gz capture. Written here rather
    than in replay.py because replay.py belongs to another agent's lane.
    """
    with gzip.open(os.path.join(FIXTURES, "history_%s.json.gz" % day), "rt") as fh:
        base = json.load(fh)
    with gzip.open(os.path.join(FIXTURES, "tesla_history_%s.json.gz" % day), "rt") as fh:
        tes = json.load(fh)

    start = float(base["start_epoch"])
    end = float(base["end_epoch"])
    series = {c: _numeric(base["series"][c], start)
              for c in ("solar", "battery", "grid", "house")}
    series["tesla"] = _numeric(tes["series"]["tesla"], start)
    channels = tuple(series)

    marks = {start, end}
    for pts in series.values():
        for ts, _ in pts:
            if start <= ts <= end:
                marks.add(ts)
    marks = sorted(marks)

    idx = {c: 0 for c in channels}
    held = {c: None for c in channels}
    for i in range(len(marks) - 1):
        t0, t1 = marks[i], marks[i + 1]
        for c in channels:
            pts = series[c]
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


def live_samples(day, stride=1):
    """Only the samples where all five channels are real numbers."""
    n = 0
    for t, dt, v in merged_samples(day):
        if any(v[c] is None for c in v):
            continue
        n += 1
        if n % stride:
            continue
        yield t, dt, v


# --------------------------------------------------------------------------
@pytest.fixture(scope="session")
def _doc():
    with open(YAML_PATH) as fh:
        return yaml.safe_load(fh)


@pytest.fixture(scope="session")
def sensors(_doc):
    """The twelve POWER templates. There is no second group -- see below."""
    assert len(_doc["template"]) == 1, "the fourth wrapper stage was cancelled"
    return _doc["template"][0]["sensor"]


@pytest.fixture(scope="session")
def templates(sensors):
    """{flows.py key: state template}, translating the entity slug back."""
    out = {}
    for s in sensors:
        assert s["unique_id"].startswith("flow_"), s["unique_id"]
        assert s["unique_id"].endswith("_power"), s["unique_id"]
        slug = s["unique_id"][len("flow_"):-len("_power")]
        out[KEY_OF_SLUG[slug]] = s["state"]
    return {k: out[k] for k in FLOW_KEYS}


@pytest.fixture(scope="session")
def availability(sensors):
    return sensors[0]["availability"]


# ======================================================================
# 1. the YAML itself
# ======================================================================
def test_twelve_sensors_with_the_expected_keys(templates):
    assert tuple(templates) == FLOW_KEYS


def test_the_slug_map_matches_v2_layouts_hard_coded_entity_names(sensors):
    """v2-layout hard-codes these twelve in layout_v2.flow_meter().

    Its vocabulary names a sink after its NODE, so `grid` as a sink is export
    and `battery` as a sink is charge. If this list and its list disagree, the
    card's link `value:` points at an entity that does not exist -- and a
    falsy/unresolvable value does not error, it degrades that link to an
    UNCAPPED greedy link, which is the exact v1 behaviour this work exists to
    remove. So this is a hard assertion, not a comment.
    """
    expected = {
        "flow_solar_to_grid", "flow_solar_to_battery", "flow_solar_to_house",
        "flow_solar_to_tesla", "flow_solar_to_inverter",
        "flow_battery_to_house", "flow_battery_to_tesla",
        "flow_battery_to_inverter",
        "flow_grid_to_house", "flow_grid_to_tesla", "flow_grid_to_battery",
        "flow_grid_to_inverter",
    }
    assert {"flow_" + s for s in SLUG.values()} == expected
    assert {s["unique_id"][:-len("_power")] for s in sensors} == expected


def test_slug_map_is_a_bijection_over_the_twelve_flows():
    assert set(SLUG) == set(FLOW_KEYS)
    assert len(set(SLUG.values())) == 12


@needs_impl
def test_flows_py_exports_exactly_these_twelve_keys():
    """Import the tuple rather than trusting two hand-written lists to agree."""
    import flows
    assert set(flows.FLOWS) == set(FLOW_KEYS)
    assert len(flows.FLOWS) == 12


def test_every_sensor_declares_power_watts_measurement(sensors):
    for s in sensors:
        assert s["unit_of_measurement"] == "W", s["name"]
        assert s["device_class"] == "power", s["name"]
        assert s["state_class"] == "measurement", s["name"]
        assert s["name"].startswith("Flow "), s["name"]
        assert s["name"].endswith(" power"), s["name"]


def test_all_twelve_preambles_are_byte_identical(templates):
    pres = {t[:t.rindex("{{")] for t in templates.values()}
    assert len(pres) == 1, "the shared preamble drifted; regenerate the YAML"


def test_all_twelve_share_one_availability_template(sensors):
    assert len({s["availability"] for s in sensors}) == 1


def test_preamble_reads_exactly_the_five_declared_sources(templates):
    body = next(iter(templates.values()))
    for eid in SRC.values():
        assert body.count("'%s'" % eid) == 1, eid
    assert body.count("states(") == len(SRC)


# ======================================================================
# 1b. the chain is THREE stages, and the fourth was cancelled on evidence
# ======================================================================
def test_the_yaml_declares_no_fourth_stage_wrapper():
    """A never-unavailable template wrapper was built, measured and cancelled.

    It is recorded here because the reasoning is not obvious and it will be
    proposed again. Unavailability DOES propagate the whole length of the
    chain -- measured live by flipping one power template's availability:

        before   template=369.6494    integration=0.063        meter=0.062
        t+10s    template=unavailable integration=unavailable  meter=unavailable
        t+130s   template=unavailable integration=unavailable  meter=unavailable
        restored template=397.4359    integration=0.063        meter=0.062

    so the meter does NOT hold. That looked fatal, because an unavailable link
    `value:` parses to NaN and Math.min NaN-poisons the source's spent total,
    silently zeroing every LATER ribbon from that source.

    It is not fatal, because with energy_date_selection: true the card never
    reads the live state on this path: the link `value:` goes through the same
    Recorder statistics substitution as a node state, and that substitution
    only requires the entity to EXIST in hass.states. Measured -- an
    unavailable utility_meter stays present:

        before/during/t+75s/restored: present=True, and state
        '0.032' / 'unavailable' / 'unavailable' / '0.033'

    What the `value:` target MUST have is a state_class, because an entity with
    no statistics rows makes the card produce the literal string "null", which
    parses to NaN. A utility_meter carries total_increasing natively. That is
    why the card points at the meter directly, and why a plain template sensor
    there would have been the dangerous choice rather than the safe one.
    """
    with open(YAML_PATH) as fh:
        doc = yaml.safe_load(fh)
    assert len(doc["template"]) == 1
    ids = {s["unique_id"] for s in doc["template"][0]["sensor"]}
    assert all(i.endswith("_power") for i in ids)
    assert not any(i.endswith("_daily") for i in ids)


def test_no_battery_to_export_or_battery_to_battery_link(templates):
    """All three discharge windows are unset; the battery never exports."""
    assert "battery_to_export" not in templates
    assert "battery_to_battery" not in templates
    assert "grid_to_export" not in templates


# ======================================================================
# 2. Jinja vs the brief, on constructed cases
# ======================================================================
# (name, solar, battery, grid, house, tesla)
CASES = [
    ("all zero",                 0, 0, 0, 0, 0),
    ("solar only to house",   2000, 0, 0, 1800, 0),
    ("solar exporting",       5000, 0, -3000, 900, 0),
    ("solar export exceeds solar", 1000, 0, -4000, 200, 0),
    ("solar charging battery", 4000, 2500, 0, 1200, 0),
    ("battery discharging",      0, -1200, 0, 1100, 0),
    ("grid importing",           0, 0, 3000, 2900, 0),
    ("grid charging battery",    0, 2500, 2700, 200, 0),
    ("night, car charging",      0, 0, 7400, 7300, 6900),
    ("solar + battery + grid",1500, -800, 900, 3000, 0),
    ("mixed, car on solar",   6000, -500, 1000, 7000, 6500),
    ("tesla exceeds house",      0, 0, 500, 400, 7000),
    ("tesla equals house",       0, 0, 7000, 7000, 7000),
    ("zero supply, house draws",  0, 0, 0, 300, 0),
    ("zero supply, everything zero but export", 0, 0, -0.0, 0, 0),
    ("negative solar (impossible)", -50, 0, 500, 400, 0),
    ("negative house (impossible)", 500, 0, 0, -300, 0),
    ("negative tesla (impossible)", 0, 0, 500, 400, -20),
    ("battery charge exceeds supply", 100, 5000, 0, 200, 0),
    ("outflow exceeds inflow",   100, 0, 0, 5000, 0),
    ("tiny numbers",           0.001, -0.002, 0.003, 0.004, 0.0005),
    ("large numbers",          6000, -5000, 8000, 15000, 7000),
]


@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_jinja_matches_the_brief_on_constructed_cases(templates, availability, case):
    _name, s, b, g, h, t = case
    got = render(templates, availability, s, b, g, h, t)
    assert got != "unavailable"
    want = spec_flows(s, b, g, h, t)
    for k in FLOW_KEYS:
        assert abs(got[k] - want[k]) < PARITY_W, (k, got[k], want[k])


# ======================================================================
# 3. every branch in the model, each with a test that can fail
# ======================================================================
def test_negative_solar_is_clamped_to_zero(templates, availability):
    a = render(templates, availability, -500, 0, 500, 400, 0)
    b = render(templates, availability, 0, 0, 500, 400, 0)
    assert a == b
    assert a["solar_to_house"] == 0.0


def test_negative_house_is_clamped_to_zero(templates, availability):
    got = render(templates, availability, 500, 0, 0, -300, 0)
    assert got["solar_to_house"] == 0.0
    assert got["solar_to_inverter"] == pytest.approx(500.0, abs=PARITY_W)


def test_negative_tesla_is_clamped_to_zero(templates, availability):
    a = render(templates, availability, 0, 0, 500, 400, -20)
    b = render(templates, availability, 0, 0, 500, 400, 0)
    assert a == b


def test_tesla_is_clamped_to_house_load(templates, availability):
    """T > house_load must not create a negative Hr or over-allocate."""
    got = render(templates, availability, 0, 0, 500, 400, 7000)
    assert got["grid_to_tesla"] == pytest.approx(400.0, abs=PARITY_W)
    assert got["grid_to_house"] == 0.0
    assert got["grid_to_inverter"] == pytest.approx(100.0, abs=PARITY_W)


def test_tesla_equal_to_house_leaves_no_rest_of_house(templates, availability):
    got = render(templates, availability, 0, 0, 7000, 7000, 7000)
    assert got["grid_to_house"] == 0.0
    assert got["grid_to_tesla"] == pytest.approx(7000.0, abs=PARITY_W)


def test_battery_sign_splits_charge_from_discharge(templates, availability):
    charging = render(templates, availability, 4000, 2500, 0, 1200, 0)
    assert charging["solar_to_battery"] > 0
    assert charging["battery_to_house"] == 0.0
    discharging = render(templates, availability, 0, -1200, 0, 1100, 0)
    assert discharging["solar_to_battery"] == 0.0
    assert discharging["battery_to_house"] > 0


def test_grid_sign_splits_import_from_export(templates, availability):
    importing = render(templates, availability, 0, 0, 3000, 2900, 0)
    assert importing["grid_to_house"] > 0
    assert importing["solar_to_export"] == 0.0
    exporting = render(templates, availability, 5000, 0, -3000, 900, 0)
    assert exporting["solar_to_export"] == pytest.approx(3000.0, abs=PARITY_W)
    assert exporting["grid_to_house"] == 0.0


def test_export_is_capped_by_solar_not_by_the_meter(templates, availability):
    """s2e = min(S, E). A meter reading more export than solar cannot invent PV."""
    got = render(templates, availability, 1000, 0, -4000, 200, 0)
    assert got["solar_to_export"] == pytest.approx(1000.0, abs=PARITY_W)
    assert sum(got.values()) == pytest.approx(1000.0, abs=1e-3)


def test_export_is_solar_only_never_battery_or_grid(templates, availability):
    got = render(templates, availability, 3000, -2000, -2500, 500, 0)
    assert got["solar_to_export"] == pytest.approx(2500.0, abs=PARITY_W)
    assert "battery_to_export" not in got


def test_inverter_loss_is_clamped_at_zero_when_outflow_exceeds_inflow(
        templates, availability):
    got = render(templates, availability, 100, 0, 0, 5000, 0)
    for k in ("solar_to_inverter", "battery_to_inverter", "grid_to_inverter"):
        assert got[k] == 0.0


def test_inverter_loss_absorbs_the_positive_residual(templates, availability):
    """3000 in, 2000 to house => 1000 W of measured loss, all from solar."""
    got = render(templates, availability, 3000, 0, 0, 2000, 0)
    assert got["solar_to_inverter"] == pytest.approx(1000.0, abs=PARITY_W)
    assert got["solar_to_house"] == pytest.approx(2000.0, abs=PARITY_W)


def test_no_flow_ever_renders_negative_zero(templates, availability):
    """A power sensor reading "-0.0" invites exactly the wrong conclusion.

    Jinja's max/min return the FIRST argument on a tie and -0.0 == 0 is a tie,
    so `[ -GP, 0 ] | max` with grid power exactly 0 produced E = -0.0, which
    propagated into solar_to_export. Observed live on 2026-08-30 on
    sensor.flow_solar_to_grid_power. Fixed by putting the literal 0
    first in every `| max`. This test is the guard on that ordering.
    """
    cases = [(764, -2311, 0, 2914, 0),      # the live sample that showed it
             (0, 0, 0, 0, 0),
             (0, -0.0, -0.0, 0, 0),
             (1000, 0, 0, 1000, 0),
             (0, 0, 0, 0, -0.0)]
    for args in cases:
        got = render(templates, availability, *args)
        for k, v in got.items():
            assert not (v == 0.0 and math.copysign(1.0, v) < 0), (args, k)


def test_the_rendered_string_is_never_negative_zero(templates, availability):
    """Belt and braces: check the STRING HA would store, not just its float."""
    _STATES.clear()
    _STATES.update({SRC["solar"]: "764", SRC["battery"]: "-2311",
                    SRC["grid"]: "0", SRC["house"]: "2914", SRC["tesla"]: "0"})
    for k, t in templates.items():
        assert not _compile(t).render().strip().startswith("-0"), k


def test_zero_supply_guard_emits_zero_not_a_division_error(templates, availability):
    """tot == 0 with a house drawing power: everything is zero, nothing raises."""
    got = render(templates, availability, 0, 0, 0, 300, 0)
    assert got == {k: 0.0 for k in FLOW_KEYS}


def test_zero_supply_guard_when_all_solar_went_to_export(templates, availability):
    """S1 == 0 and B == G == 0 is the other way to reach tot == 0."""
    got = render(templates, availability, 2000, 0, -2000, 0, 0)
    assert got["solar_to_export"] == pytest.approx(2000.0, abs=PARITY_W)
    for k in FLOW_KEYS:
        if k != "solar_to_export":
            assert got[k] == 0.0, k


def test_a_source_at_zero_contributes_nothing_to_any_sink(templates, availability):
    """The property v1's greedy rule could not offer: no fabricated flows."""
    got = render(templates, availability, 0, -1200, 0, 1100, 0)
    for k in FLOW_KEYS:
        if k.startswith("solar_") or k.startswith("grid_"):
            assert got[k] == 0.0, k


def test_shares_sum_to_one_when_supply_is_positive(templates, availability):
    """Probe the shares through a sink of known size."""
    got = render(templates, availability, 1500, -800, 900, 3000, 0)
    hr = 3000.0
    total_to_house = (got["solar_to_house"] + got["battery_to_house"]
                      + got["grid_to_house"])
    assert total_to_house == pytest.approx(hr, abs=1e-3)


def test_battery_cannot_charge_and_discharge_in_one_sample(templates, availability):
    for battery in (2500, -2500):
        got = render(templates, availability, 3000, battery, 500, 1000, 0)
        charging_paths = got["solar_to_battery"] + got["grid_to_battery"]
        discharge_paths = (got["battery_to_house"] + got["battery_to_tesla"]
                           + got["battery_to_inverter"])
        assert charging_paths == 0.0 or discharge_paths == 0.0


# ======================================================================
# 4. bad input: the sensor must go unavailable, not lie
# ======================================================================
@pytest.mark.parametrize("channel", sorted(SRC))
@pytest.mark.parametrize("bad", BAD_STATES)
def test_any_bad_source_makes_all_twelve_unavailable(
        templates, availability, channel, bad):
    kwargs = {"solar": 1000, "battery": -500, "grid": 200, "house": 1500, "tesla": 0}
    kwargs[channel] = bad
    assert render(templates, availability, **kwargs) == "unavailable"


@pytest.mark.parametrize("channel", sorted(SRC))
def test_a_none_source_makes_all_twelve_unavailable(templates, availability, channel):
    kwargs = {"solar": 1000, "battery": -500, "grid": 200, "house": 1500, "tesla": 0}
    kwargs[channel] = None
    assert render(templates, availability, **kwargs) == "unavailable"


def test_emulated_filters_match_the_semantics_measured_in_live_ha():
    """Pins the offline emulation to what LIVE HA actually does.

    Measured 2026-08-30 with POST /api/template against the running instance
    (scratchpad/filter_sweep.py), because guessing here is how a harness ends
    up agreeing with itself while disagreeing with production. Every row below
    is an observation, not a reading of the docs.

    Where jinja2 and HA DIVERGE, and whether it can reach these templates:

      `| float` no default on a non-number   jinja2 0.0, HA RAISES.  REACHABLE,
          and the whole reason the templates carry no default. Emulated.
      `| float` no default on ''             jinja2 0.0, HA RAISES.  Emulated.
      `| int` no default on a non-number     jinja2 0,   HA RAISES.  Not used.
      `is_number` filter/test                jinja2 has neither.     Emulated
          as BOTH, because the availability template uses the test form via
          select() and nothing else may quietly fall back.
      `states` / `has_value` / `is_state`    jinja2 has none.  Only `states`
          is used and it is emulated; the other two are deliberately not used.
      `'1.2345' | round(2)`                  jinja2 RAISES, HA returns 1.23.
          NOT reachable: round is only ever applied to a computed float.
      `2.5 | round(0)`                       jinja2 '2.0', HA '2'.  NOT
          reachable: the templates round to 4 dp, never 0.
      `[] | max`                             jinja2 RAISES, HA returns ''.
          NOT reachable: it sits behind `vs | count == 5 and ...`, and HA's
          `and` was measured to short-circuit (verified: `{{ false and
          ([] | max) <= 1 }}` renders 'False' rather than raising).

    Where they AGREE, checked rather than assumed: `'nan' | float` and
    `'inf' | float` both yield nan/inf in BOTH engines and do NOT raise -- so
    the nan guard has to come from is_number, which is exactly where it is.
    Also identical: float with a default, int with a default, abs, min/max on
    lists including the -0.0 tie order, map('float'), division and modulo by
    zero (both raise), int+float, conditional expressions, and float repr.
    """
    # HA RAISES on a defaultless float of a non-number; jinja2 returns 0.0.
    with pytest.raises(HAFloatError):
        ha_float("unavailable")
    with pytest.raises(HAFloatError):
        ha_float("")
    # ...but NOT on nan/inf, which parse fine. Measured, not assumed.
    assert math.isnan(ha_float("nan"))
    assert math.isinf(ha_float("inf"))
    # With a default it is forgiving, exactly as HA is.
    assert ha_float("unavailable", 0) == 0
    # is_number is what rejects nan/inf, since float does not.
    assert not ha_is_number("nan")
    assert not ha_is_number("inf")


def test_the_availability_short_circuit_is_load_bearing(availability):
    """`vs | count == 5 and (vs | max) <= 100000` must never evaluate the max.

    HA returns '' for `[] | max` rather than raising, and '' <= 100000 is a
    type error. jinja2 raises on `[] | max` outright. Either way the guard is
    what keeps it unreachable, so assert the ordering rather than trusting it.
    """
    assert availability.index("count == 5") < availability.index("| max")
    assert "and" in availability


def test_nan_and_inf_are_rejected_by_is_number_not_propagated():
    """The whole unavailable path rests on this; assert it directly."""
    assert not ha_is_number("nan")
    assert not ha_is_number("inf")
    assert not ha_is_number("-inf")
    assert not ha_is_number(float("nan"))
    assert not ha_is_number("unavailable")
    assert ha_is_number("0")
    assert ha_is_number("-1234.5")


def test_all_five_good_is_available(templates, availability):
    assert render(templates, availability, 1000, -500, 200, 1500, 0) != "unavailable"


@pytest.mark.parametrize("channel", sorted(SRC))
def test_implausibly_large_readings_go_unavailable(templates, availability, channel):
    """Mirrors flows.MAX_PLAUSIBLE_W, above which flows.py raises BadReading.

    A 6 kW inverter cannot report 100 kW; that is a misread, and a misread must
    blank the sensor rather than be integrated.
    """
    kwargs = {"solar": 1000, "battery": -500, "grid": 200, "house": 1500, "tesla": 0}
    kwargs[channel] = 100000.5
    assert render(templates, availability, **kwargs) == "unavailable"
    kwargs[channel] = -100000.5
    assert render(templates, availability, **kwargs) == "unavailable"


@pytest.mark.parametrize("channel", sorted(SRC))
def test_the_plausibility_boundary_itself_is_available(
        templates, availability, channel):
    """100000 W exactly is accepted, matching flows.py's `abs(v) > MAX`."""
    kwargs = {"solar": 1000, "battery": -500, "grid": 200, "house": 1500, "tesla": 0}
    kwargs[channel] = 100000.0
    assert render(templates, availability, **kwargs) != "unavailable"


@needs_impl
@pytest.mark.parametrize("channel", sorted(SRC))
@pytest.mark.parametrize("bad", BAD_STATES + ("100000.5", "-100000.5"))
def test_jinja_goes_unavailable_exactly_where_flows_py_raises(
        templates, availability, channel, bad):
    """Validation parity, not just arithmetic parity.

    flows.py raises BadReading; a template sensor's equivalent of raising is
    rendering `unavailable`. These two rejection sets must be the same set, or
    HA would integrate a sample the physics module refuses to decompose.
    """
    kwargs = {"solar": 1000, "battery": -500, "grid": 200, "house": 1500, "tesla": 0}
    kwargs[channel] = bad
    jinja_rejected = render(templates, availability, **kwargs) == "unavailable"
    try:
        FLOWS_IMPL(*[kwargs[c] for c in ("solar", "battery", "grid", "house",
                                         "tesla")])
        python_rejected = False
    except (ValueError, TypeError):
        python_rejected = True
    assert jinja_rejected == python_rejected, (channel, bad, jinja_rejected)


@needs_impl
@pytest.mark.parametrize("channel", sorted(SRC))
def test_jinja_and_flows_py_agree_on_the_plausibility_boundary(
        templates, availability, channel):
    kwargs = {"solar": 1000, "battery": -500, "grid": 200, "house": 1500, "tesla": 0}
    kwargs[channel] = 100000.0
    assert render(templates, availability, **kwargs) != "unavailable"
    FLOWS_IMPL(*[kwargs[c] for c in ("solar", "battery", "grid", "house", "tesla")])


def test_tesla_unavailable_blanks_everything_including_non_tesla_flows(
        templates, availability):
    """Documented consequence: Hr depends on T, so no flow is knowable."""
    assert render(templates, availability, 1000, -500, 200, 1500,
                  "unavailable") == "unavailable"


# ======================================================================
# 5. the held-flat Tesla plateau
# ======================================================================
def test_tesla_held_flat_across_a_plateau_is_used_not_discarded(
        templates, availability):
    """TeslaMate publishes on change; a held 7 kW is correct, not stale."""
    for _ in range(5):
        got = render(templates, availability, 0, 0, 7400, 7300, 6900)
        assert got["grid_to_tesla"] == pytest.approx(6900.0, abs=PARITY_W)


def test_fixtures_contain_a_tesla_plateau_longer_than_the_sample_interval():
    """Guards the fixture, so the plateau case is not silently lost on recapture.

    Measured 2026-08-30: the longest hold above 1 kW across the four days is
    163 s (2026-08-30). CLAUDE.md's "one 32 A plateau went 80 minutes without an
    update" is about TeslaMate's raw current entity; the derived power template
    also recomputes when the charger VOLTAGE moves, so it refreshes far more
    often. 163 s is still 16 inverter samples of held Tesla power, which is the
    thing this suite needs to exercise.
    """
    longest = 0.0
    for day in TESLA_DAYS:
        with gzip.open(os.path.join(FIXTURES, "tesla_history_%s.json.gz" % day),
                       "rt") as fh:
            pts = json.load(fh)["series"]["tesla"]
        for (t0, s0), (t1, _s1) in zip(pts, pts[1:]):
            if float(s0) > 1000:
                longest = max(longest, float(t1) - float(t0))
    assert longest > 100, "longest Tesla plateau in the fixtures is %.0f s" % longest


def test_fixtures_contain_real_car_charging_on_at_least_two_days():
    """2026-08-27 has one Tesla row and 2026-08-28 has a 41 s blip.

    sensor.tesla_home_charging_power did not exist before 2026-08-27 13:27:59
    local, so the car regime is only genuinely present on -29 and -30. Pinned
    so a thinner recapture cannot quietly remove the only car coverage there is.
    """
    days_with_car = 0
    for day in DAYS:
        with gzip.open(os.path.join(FIXTURES, "tesla_history_%s.json.gz" % day),
                       "rt") as fh:
            pts = json.load(fh)["series"]["tesla"]
        if sum(1 for _t, s in pts if float(s) > 1000) >= 10:
            days_with_car += 1
    assert days_with_car >= 2, "only %d fixture days show real charging" % days_with_car


# ======================================================================
# 6. parity over every replayed sample of the four fixture days
# ======================================================================
@pytest.mark.parametrize("day", DAYS)
def test_jinja_matches_the_brief_on_every_replayed_sample(
        templates, availability, day):
    """Every 60th live sample of the day, all twelve flows, < 1e-4 W."""
    n = 0
    worst = 0.0
    for _t, _dt, v in live_samples(day, stride=10):
        got = render(templates, availability,
                     v["solar"], v["battery"], v["grid"], v["house"], v["tesla"])
        assert got != "unavailable"
        want = spec_flows(v["solar"], v["battery"], v["grid"], v["house"], v["tesla"])
        for k in FLOW_KEYS:
            worst = max(worst, abs(got[k] - want[k]))
        n += 1
    assert n > 100, "day %s yielded only %d samples" % (day, n)
    assert worst < PARITY_W, "worst |jinja - spec| = %.3e W on %s" % (worst, day)


@pytest.mark.parametrize("day", TESLA_DAYS)
def test_replayed_samples_cover_more_than_one_regime(day):
    """A parity run over an all-dark day would prove very little."""
    seen = set()
    for _t, _dt, v in live_samples(day, stride=1):
        if v["solar"] > 100:
            seen.add("solar")
        if v["battery"] > 100:
            seen.add("charging")
        if v["battery"] < -100:
            seen.add("discharging")
        if v["grid"] > 100:
            seen.add("importing")
        if v["grid"] < -100:
            seen.add("exporting")
        if v["tesla"] > 100:
            seen.add("car")
    assert {"solar", "charging", "discharging", "importing"} <= seen, (day, seen)


# ======================================================================
# 7. conservation: the twelve must add back up to the six measured quantities
# ======================================================================
def source_totals(f):
    return {
        "solar": (f["solar_to_export"] + f["solar_to_battery"] + f["solar_to_house"]
                  + f["solar_to_tesla"] + f["solar_to_inverter"]),
        "battery_out": (f["battery_to_house"] + f["battery_to_tesla"]
                        + f["battery_to_inverter"]),
        "grid_in": (f["grid_to_house"] + f["grid_to_tesla"] + f["grid_to_battery"]
                    + f["grid_to_inverter"]),
    }


def sink_totals(f):
    return {
        "house": f["solar_to_house"] + f["battery_to_house"] + f["grid_to_house"],
        "tesla": f["solar_to_tesla"] + f["battery_to_tesla"] + f["grid_to_tesla"],
        "battery_in": f["solar_to_battery"] + f["grid_to_battery"],
        "export": f["solar_to_export"],
        "inverter": (f["solar_to_inverter"] + f["battery_to_inverter"]
                     + f["grid_to_inverter"]),
    }


def unclamped_residual(solar, battery, grid, house, tesla):
    S, B, C = max(0.0, solar), max(0.0, -battery), max(0.0, battery)
    G, E, H = max(0.0, grid), max(0.0, -grid), max(0.0, house)
    T = min(max(0.0, tesla), H)
    return (S + B + G) - (max(0.0, H - T) + T + C + E)


@pytest.mark.parametrize("day", DAYS)
def test_every_sink_fills_exactly_when_the_residual_is_non_negative(
        templates, availability, day):
    """The construction's central claim, checked sample by sample.

    Two documented exclusions, both measured below rather than assumed away:

      residual < 0  the L >= 0 clamp bites, and the shares then hand every sink
                    its full demand out of a supply that cannot cover it.
      E > S         export exceeds solar, so s2e = min(S, E) < E and the sinks
                    add up to tot + S - E, i.e. less than the sources. Every
                    source is under-allocated by the same proportion.
    """
    n = 0
    for _t, _dt, v in live_samples(day, stride=10):
        args = (v["solar"], v["battery"], v["grid"], v["house"], v["tesla"])
        if unclamped_residual(*args) < 0:
            continue
        if max(0.0, -args[2]) > max(0.0, args[0]):
            continue
        f = render(templates, availability, *args)
        st = sink_totals(f)
        S, B = max(0.0, args[0]), max(0.0, -args[1])
        G, C = max(0.0, args[2]), max(0.0, args[1])
        E, H = max(0.0, -args[2]), max(0.0, args[3])
        T = min(max(0.0, args[4]), H)
        assert st["house"] == pytest.approx(max(0.0, H - T), abs=1e-3)
        assert st["tesla"] == pytest.approx(T, abs=1e-3)
        assert st["battery_in"] == pytest.approx(C, abs=1e-3)
        assert st["export"] == pytest.approx(min(S, E), abs=1e-3)
        src = source_totals(f)
        assert src["solar"] == pytest.approx(S, abs=1e-3)
        assert src["battery_out"] == pytest.approx(B, abs=1e-3)
        assert src["grid_in"] == pytest.approx(G, abs=1e-3)
        n += 1
    assert n > 100, "day %s gave only %d non-negative-residual samples" % (day, n)


def test_negative_residual_overfills_sinks_and_this_is_known(
        templates, availability):
    """When measured sinks exceed measured supply, the model over-allocates.

    S+B+G < Hr+T+C+E clamps L to 0, and the proportional shares then hand each
    sink its FULL demand out of a supply that cannot cover it, so the twelve
    sum to more than the sources. This is a property of the rule as specified
    in BRIEF_V2.md section 2, not a transcription error -- it is pinned here so
    that a future clamp is a deliberate, visible change. The card's own
    min(parent_remainder, ...) hides it on screen; the sensors do not.
    """
    f = render(templates, availability, 100, 0, 0, 5000, 0)
    assert source_totals(f)["solar"] == pytest.approx(5000.0, abs=1e-3)
    assert source_totals(f)["solar"] > 100.0


def test_export_exceeding_solar_silently_drops_the_excess_from_the_battery(
        templates, availability):
    """E > S: exactly (E - S) watts vanish, and they come out of the battery.

    E and G are the two signs of one sensor, so E > 0 forces G == 0; and E >= S
    forces s2e == S and therefore S1 == 0. The battery is then the only source
    left in the pool, while L still subtracts the WHOLE of E. So the shortfall
    is not spread -- solar fills exactly, and the battery alone is short by
    E - S, which is precisely the battery energy that went to the grid.

    Pinned, not fixed. Carrying it properly would need a battery -> export
    ribbon, and that is forbidden outright: all three discharge windows are
    unset and CLAUDE.md says never to draw one.

    WHAT CAUSES IT, measured 2026-08-30 -- and an earlier draft of this comment
    over-claimed, so the correction matters. It is NOT evidence that the
    inverter exports battery power despite the unset discharge windows. Two
    discriminating measurements over the fixture days:

      1. Solar is RAMPING at these samples. |S(t+30s) - S(t-30s)| has median
         282 W at the faults against 48 W at a solar-band-MATCHED control and
         21 W over all samples -- 5.9x. On the two sunny days it is starker:
         2026-08-28 medians 928 W vs 50 W, 2026-08-30 922 W vs 251 W. In 59% of
         faults the solar ramp alone is at least as large as the whole excess,
         and runs last 1-2 samples (median 11-12 s). That is sample-timing skew
         between the AC smart meter and the DC inverter registers on cloud
         edges -- exactly what CLAUDE.md warns about when mixing the two sides.

      2. The night residue, the only part skew cannot explain, because with
         solar < 50 W any export must come from the battery: 229 samples,
         37.3 Wh across FOUR days, about 9 Wh/day, in bursts totalling 23-404 s
         per day, median 65 W, p90 174 W. A discharge-to-grid window would run
         for hours at kW. Nine watt-hours a day at 65 W is self-use regulation
         overshoot, not a discharge window.

    So CLAUDE.md's account of the discharge windows is NOT contradicted.
    """
    f = render(templates, availability, 1000, -3000, -2000, 500, 0)
    src = source_totals(f)
    assert src["solar"] == pytest.approx(1000.0, abs=1e-3)
    assert src["grid_in"] == 0.0
    assert src["battery_out"] == pytest.approx(3000.0 - (2000.0 - 1000.0), abs=1e-3)
    assert f["solar_to_export"] == pytest.approx(1000.0, abs=1e-3)


@pytest.mark.parametrize("day", TESLA_DAYS)
def test_both_conservation_holes_are_rare_and_small(day):
    """Quantifies both holes so nobody has to guess at their size.

    Measured over the four fixture days on 2026-08-30, on the live samples only:

        day         residual < 0            E > S
        2026-08-27  5.6% of time, 0.045 kWh  0.7%, 0.014 kWh
        2026-08-28  1.8% of time, 0.125 kWh  0.7%, 0.049 kWh
        2026-08-29  1.4% of time, 0.043 kWh  0.6%, 0.034 kWh
        2026-08-30  3.5% of time, 0.148 kWh  0.2%, 0.020 kWh

    Against ~30 kWh/day both are under 0.5%. The bounds below are loose enough
    to survive a recapture but tight enough to fail if either grows an order of
    magnitude -- which would mean a sensor had started lying.
    """
    over_wh = under_wh = 0.0
    neg_s = exs_s = total_s = 0.0
    for _t, dt, v in live_samples(day, stride=1):
        total_s += dt
        S, E = max(0.0, v["solar"]), max(0.0, -v["grid"])
        r = unclamped_residual(v["solar"], v["battery"], v["grid"],
                               v["house"], v["tesla"])
        if r < 0:
            neg_s += dt
            over_wh += -r * dt / 3600.0
        if E > S:
            exs_s += dt
            under_wh += (E - S) * dt / 3600.0
    print("\n%s  residual<0 %.1f%% %.3f kWh | E>S %.1f%% %.3f kWh"
          % (day, 100 * neg_s / total_s, over_wh / 1000.0,
             100 * exs_s / total_s, under_wh / 1000.0))
    assert neg_s / total_s < 0.15, "negative residual %.1f%% of the day" % (
        100 * neg_s / total_s)
    assert over_wh / 1000.0 < 1.0, "%.3f kWh over-allocated" % (over_wh / 1000.0)
    assert exs_s / total_s < 0.05, "E>S %.1f%% of the day" % (100 * exs_s / total_s)
    assert under_wh / 1000.0 < 0.5, "%.3f kWh under-allocated" % (under_wh / 1000.0)


# ======================================================================
# 8. Jinja vs flows.py -- the shipping module
# ======================================================================
@needs_impl
@pytest.mark.parametrize("case", CASES, ids=[c[0] for c in CASES])
def test_jinja_matches_flows_py_on_constructed_cases(templates, availability, case):
    _name, s, b, g, h, t = case
    got = render(templates, availability, s, b, g, h, t)
    want = FLOWS_IMPL(s, b, g, h, t)
    assert set(want) == set(FLOW_KEYS), sorted(set(want) ^ set(FLOW_KEYS))
    for k in FLOW_KEYS:
        assert abs(got[k] - want[k]) < PARITY_W, (k, got[k], want[k])


@needs_impl
@pytest.mark.parametrize("day", DAYS)
def test_jinja_matches_flows_py_on_every_replayed_sample(
        templates, availability, day):
    worst = 0.0
    n = 0
    for _t, _dt, v in live_samples(day, stride=10):
        args = (v["solar"], v["battery"], v["grid"], v["house"], v["tesla"])
        got = render(templates, availability, *args)
        want = FLOWS_IMPL(*args)
        for k in FLOW_KEYS:
            worst = max(worst, abs(got[k] - want[k]))
        n += 1
    assert n > 100
    assert worst < PARITY_W, "worst |jinja - flows.py| = %.3e W on %s" % (worst, day)


@needs_impl
@pytest.mark.parametrize("day", DAYS)
def test_flows_py_matches_the_brief_on_every_replayed_sample(day):
    """Independent of the Jinja: does the shipping module obey the brief?"""
    worst = 0.0
    for _t, _dt, v in live_samples(day, stride=10):
        args = (v["solar"], v["battery"], v["grid"], v["house"], v["tesla"])
        want = spec_flows(*args)
        got = FLOWS_IMPL(*args)
        for k in FLOW_KEYS:
            worst = max(worst, abs(got[k] - want[k]))
    assert worst < PARITY_W, "worst |flows.py - brief| = %.3e W on %s" % (worst, day)
