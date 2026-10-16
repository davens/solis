"""Fridge's second carve-out, live fallback, and measured terminus, offline."""
import sys
from pathlib import Path

import pytest
import yaml
from hypothesis import given, settings, strategies as st

sys.path.insert(0, str(Path(__file__).parent))
sys.path.insert(0, str(Path(__file__).parent.parent))

import flows
import layout_v2 as layout
import test_jinja_parity as parity
from fixtures import replay
import sankey_apply
import sankey_helpers


@pytest.fixture(scope="module")
def fridge_templates():
    doc = yaml.safe_load(Path(parity.YAML_PATH).read_text())
    sensors = doc["template"][0]["sensor"]
    templates = {parity.KEY_OF_SLUG[s["unique_id"][5:-6]]: s["state"]
                 for s in sensors}
    return templates, sensors[0]["availability"]


@pytest.mark.parametrize("tesla,aircon,fridge,expected", [
    (600, 250, 100, (600, 250, 100, 50)),
    (600, 250, 9000, (600, 0, 400, 0)),
    (600, 400, 100, (600, 300, 100, 0)),
    (1200, 400, 100, (1000, 0, 0, 0)),
    (600, 9000, 100, (600, 300, 100, 0)),
    (-10, -20, -30, (0, 0, 0, 1000)),
])
def test_fridge_is_carved_after_tesla_before_aircon(tesla, aircon, fridge, expected):
    r = flows.readings(2000, -500, 300, 1000, tesla, aircon, fridge)
    assert tuple(r[k] for k in ("tesla", "aircon", "fridge", "house_rest")) == expected
    assert sum(expected) == r["house"]


@pytest.mark.parametrize("args", [
    (3000, -500, 400, 2600, 900, 500, 120),
    (0, 900, 3000, 1800, 600, 500, 120),
    (6000, 2000, -1500, 2000, 1200, 500, 120),
    (0, 0, 0, 1000, 300, 250, 100),
    (0, 0, 1200, 1000, 600, 400, 900),
    (0, 0, 1200, 1000, 1200, 400, 900),
    (0, 0, 1200, 1000, 600, 250, -30),
    (0.001, -0.002, 0.003, 0.004, 0.0005, 0.0005, 0.0005),
])
def test_fridge_templates_match_both_independent_reference_and_python(fridge_templates, args):
    templates, availability = fridge_templates
    rendered = parity.render(templates, availability, *args)
    assert rendered == pytest.approx(parity.spec_flows(*args), abs=parity.PARITY_W)
    assert rendered == pytest.approx(flows.decompose(*args), abs=parity.PARITY_W)


@pytest.mark.parametrize("reading", (parity.ABSENT,) + parity.AIRCON_FALLS_BACK_TO_ZERO)
def test_missing_or_unavailable_fridge_stays_inside_house(fridge_templates, reading):
    templates, availability = fridge_templates
    args = (3000, -500, 400, 2600, 900, 500)
    got = parity.render(templates, availability, *args, fridge=reading)
    zero = parity.render(templates, availability, *args, fridge=0)
    assert got == zero
    assert all(got[k] == 0 for k in parity.FRIDGE_KEYS)
    assert parity.FRIDGE_ENTITY not in availability
    assert availability.count("sensor.") == 5


@pytest.mark.parametrize("reading", parity.AIRCON_CLAMPED_NOT_ZERO)
def test_enormous_fridge_reading_is_clamped_to_what_tesla_left(fridge_templates, reading):
    templates, availability = fridge_templates
    got = parity.render(templates, availability, 0, 0, 1200, 1000, 600, 250, reading)
    assert got["grid_to_fridge"] == 400
    assert got["grid_to_tesla"] == 600
    assert got["grid_to_aircon"] == 0
    assert got["grid_to_house"] == 0


@given(s=st.floats(0, 9000), b=st.floats(-5000, 5000),
       g=st.floats(-5000, 9000), h=st.floats(0, 9000),
       t=st.floats(0, 9000), a=st.floats(0, 9000), f=st.floats(-100, 9000))
@settings(max_examples=300, deadline=None)
def test_fridge_preserves_tesla_inverter_and_combined_house_split(s, b, g, h, t, a, f):
    args = (s, b, g, h, t, a, f)
    got = flows.decompose(*args)
    assert got == pytest.approx(parity.spec_flows(*args), abs=1e-9)
    before = flows.decompose(s, b, g, h, t, a, 0)
    for source in flows.SOURCES:
        for sink in ("tesla", "inverter"):
            key = source + "_to_" + sink
            assert got[key] == pytest.approx(before[key], abs=1e-9)
        assert got[source + "_to_house"] + got[source + "_to_fridge"] + got[source + "_to_aircon"] == pytest.approx(
            before[source + "_to_house"] + before[source + "_to_aircon"], abs=1e-9)
    r = flows.readings(*args)
    assert r["house_rest"] + r["tesla"] + r["aircon"] + r["fridge"] == pytest.approx(h)


@pytest.mark.parametrize("aircon", [0, 100, 300, 400, 9000])
def test_aircon_overshoot_cannot_starve_fridge_on_any_source(fridge_templates, aircon):
    templates, availability = fridge_templates
    args = (1500, -500, 1000, 1000, 600)
    before = flows.decompose(*args, 0, 100)
    got = flows.decompose(*args, aircon, 100)
    rendered = parity.render(templates, availability, *args, aircon, 100)
    for source in flows.SOURCES:
        for sink in ("tesla", "fridge"):
            key = source + "_to_" + sink
            assert got[key] == before[key]
            assert rendered[key] == pytest.approx(before[key], abs=parity.PARITY_W)
        key = source + "_to_inverter"
        assert got[key] == pytest.approx(before[key], abs=1e-9)
    totals = flows.node_totals(got)
    assert totals["fridge"] == pytest.approx(100)
    assert totals["aircon"] == pytest.approx(min(aircon, 300))
    assert totals["house"] == pytest.approx(max(0, 300 - aircon))


def test_optional_channel_blindness_follows_the_carve_out_order():
    assert flows.DEPENDS_ON["fridge"] == frozenset(
        ("solar", "battery", "grid", "house", "tesla", "fridge"))
    assert flows.DEPENDS_ON["aircon"] == frozenset(flows.CHANNELS)
    for channel, sinks in (("tesla", ("house", "tesla", "fridge", "aircon")),
                           ("fridge", ("house", "fridge", "aircon")),
                           ("aircon", ("house", "aircon"))):
        dead = set(flows.FLOWS) & flows.unreportable((channel,))
        assert dead == {source + "_to_" + sink
                        for source in flows.SOURCES for sink in sinks}


@pytest.mark.parametrize("rule", flows.LOSS_RULES)
def test_fridge_fills_under_every_loss_rule_and_cannot_supply_a_charging_battery(rule):
    previous = flows.set_loss_rule(rule)
    try:
        got = flows.decompose(3000, 1000, 700, 2000, 600, 500, 120)
        assert flows.node_totals(got)["fridge"] == pytest.approx(120)
        assert got["battery_to_fridge"] == 0
        got["battery_to_fridge"] = 1
        with pytest.raises(flows.StructureError, match="charging battery"):
            flows.check_structure(got, flows.readings(3000, 1000, 700, 2000, 600, 500, 120))
    finally:
        flows.set_loss_rule(previous)


def test_fridge_is_a_teal_terminus_with_its_own_three_daily_meters():
    nodes, links, _ = layout.build()
    node = next(n for n in nodes if n["id"] == layout.FRIDGE)
    assert (node["name"], node["section"], node["color"]) == ("Fridge", 1, "#14b8a6")
    assert node.get("type", "entity") == "entity"
    sources = (layout.SOLAR, layout.BAT_OUT, layout.GRID_IN)
    meters = ["sensor.flow_" + s + "_to_fridge_daily" for s in flows.SOURCES]
    assert [node["entity_id"], *node["add_entities"]] == meters
    assert [(l["source"], l["value"]) for l in links if l["target"] == layout.FRIDGE] == list(zip(sources, meters))
    assert not any(l["source"] == layout.FRIDGE for l in links)
    assert node["color"] not in [n["color"] for n in nodes if n["id"] != layout.FRIDGE]
    assert len(nodes) == 10 and len(links) == 18
    assert not sankey_apply.check_layout(nodes, links)
    every, values = sankey_apply.entities_of(nodes, links)
    assert set(meters) <= values <= every
    wanted = {slug for slug, *_ in sankey_helpers.wanted()}
    assert {s + "_to_fridge" for s in flows.SOURCES} <= wanted


@pytest.mark.parametrize("day", parity.DAYS)
def test_recorded_fridge_hold_is_refused_and_combined_house_remains_measurable(day):
    doc = replay.load_day(day, tesla=True)
    r = replay.replay(doc, flows.decompose)
    assert "fridge" in r["blind_channels"]
    assert r["fridge_assumed_s"] == r["live_s"]
    assert replay.derived(r["flows"])["fridge"] == 0
    assert "fridge" in replay.unknowable(doc)
    assert set(parity.FRIDGE_KEYS) <= replay.blind_flows(doc)
    assert "house_and_aircon_fridge" not in replay.unknowable(doc)
