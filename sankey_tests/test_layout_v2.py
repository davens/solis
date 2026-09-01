"""Tests for the Sankey v2 card layout.

Three bands of test, in the order they matter:

1.  **Config identity.** Every node and every link is pinned by value -- id,
    section, colour, type, state source, per-link value entity. These are the
    tests that fail when someone "tidies" Tesla's white to a palette accent or
    puts air con back on yellow.

2.  **What the card would draw.** `allocator.py` is a faithful port of
    ha-sankey-chart 6.3.0's `_calcConnection`, so the layout is simulated
    rather than reasoned about. This band proves no ghost box is synthesised,
    no source starves a sink, nothing is stranded, and Stored fills exactly
    from Battery in.

3.  **Order invariance.** v1's ribbons depended on declaration order because
    the card's rule was `min(parent_remainder, child_remainder)`. v2 supplies
    a per-link `value` for every source->sink link, making the rule
    `min(parent_remainder, child_remainder, value)`. The sweeps below permute
    link order within each source and node order within each section and prove
    the resolved allocation is identical -- and, as a control, that the same
    sweep *without* values produces many different allocations.

Per-link values come from `flows.py`, the shipping decomposition. `_prop_ref()`
is the hand-written transcription of BRIEF_V2.md section 2 that these tests
were originally built on; it is kept solely as an independent second opinion,
and `test_flows_matches_the_hand_written_reference` asserts the two agree to
1e-9 W. If flows.py ever drifts from the brief's stated rule, that test is
where it shows up.


2026-08-27 IS DELIBERATELY NOT EXCLUDED WHOLESALE. DO NOT "TIDY" IT AWAY.
------------------------------------------------------------------------
That day's recorded Tesla power channel holds exactly ONE sample -- `0`, at
epoch 1787833679, which is **13.35 hours into a 23.86-hour day**. Before that
there is no reading at all, and the matching energy counter flaps
unknown -> 0.000 -> unknown -> 0.000: that is a sensor being set up, not a car
that provably did not charge. So zero-filling it would silently assert
something the data does not support.

The obvious response is to drop the day. That would be wrong, and it costs
13.35 hours of otherwise good data, because **the Inverter node does not
contain T at all**:

    Hr = house - T          (T is clamped to house, so Hr >= 0)
    Hr + T = house          for every T in [0, house]
    L  = (S + B + G) - (Hr + T + C + E)
       = (S + B + G) - (house + C + E)          <- no T

and the source shares depend only on S1, B and G, none of which involve T.
Therefore the Inverter node, every source's total spend, the export flow and
Battery in are all *mathematically invariant* under the car reading. An absent
Tesla channel cannot bias any of them, in either direction. Only the
House/Tesla split moves.

That is why there are two replay modes, and the split is not arbitrary:

  `_replay_states(day)`  -- default. DROPS every sample the car channel does
      not cover. Required for anything that depends on the House/Tesla split,
      and usable only on a day in TESLA_DAYS (derived mechanically from in-day
      sample count, not from a hard-coded date).

  `_replay_full(day)`    -- keeps the whole day, T = 0 where the channel does
      not reach. Legitimate ONLY for the T-invariant quantities above.

The licence for `_replay_full` is not an argument, it is two tests:
`test_inverter_node_is_independent_of_tesla` checks the algebra across
T/house = 0, 0.01, 0.25, 0.5, 0.75, 0.99 and 1.0, and
`test_replay_totals_are_invariant_under_tesla` replays each recorded day twice
-- once with T forced to 0, once with T forced to the entire house load -- and
asserts those quantities come out identical to 1e-12 while the House/Tesla
split does move (otherwise the test would prove nothing).

Concretely: excluding 08-27 wholesale moves its Inverter figure from 3.180 kWh
to 1.482 kWh. That drop is an artefact of a shorter day, not physics. 3.180 is
the honest number and it is the one to quote.
"""

import copy
import gzip
import itertools
import json
import math
import os
import random
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import allocator  # noqa: E402
import flows  # noqa: E402
import layout_v2 as L  # noqa: E402

FIXTURES = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fixtures")
sys.path.insert(0, FIXTURES)
import replay  # noqa: E402

TOL = 1e-9

SOURCES = (L.SOLAR, L.BAT_OUT, L.GRID_IN)
SINKS = (L.HOUSE, L.TESLA, L.BAT_IN, L.EXPORT, L.INVERTER)


# --------------------------------------------------------------------------
# Decomposition
# --------------------------------------------------------------------------


def _prop(solar, battery, grid, house, tesla):
    """flows.py's decomposition, keyed by (source id, sink id) for the layout.

    This is the shipping physics. The translation goes through
    layout_v2.FLOW_KEY so neither side restates the other's naming.
    """
    out = flows.decompose(solar, battery, grid, house, tesla)
    return {pair: out[key] for pair, key in L.FLOW_KEY.items()}


def _prop_ref(solar, battery, grid, house, tesla):
    """BRIEF_V2.md section 2 transcribed by hand, on one instantaneous sample.

    Kept as an independent second opinion on flows.py, not as a fallback. See
    test_flows_matches_the_hand_written_reference.
    """
    s = max(0.0, solar)
    b = max(0.0, -battery)
    c = max(0.0, battery)
    g = max(0.0, grid)
    e = max(0.0, -grid)
    t = min(max(0.0, tesla), max(0.0, house))
    hr = max(0.0, house - t)
    loss = max(0.0, (s + b + g) - (hr + t + c + e))

    s2e = min(s, e)
    s1 = s - s2e
    total = s1 + b + g
    if total > 0:
        share = {L.SOLAR: s1 / total, L.BAT_OUT: b / total, L.GRID_IN: g / total}
    else:
        share = {L.SOLAR: 0.0, L.BAT_OUT: 0.0, L.GRID_IN: 0.0}

    flows = {pair: 0.0 for pair in L.SOURCE_SINK_LINKS}
    flows[(L.SOLAR, L.EXPORT)] = s2e
    for sink, amount in ((L.HOUSE, hr), (L.TESLA, t), (L.BAT_IN, c), (L.INVERTER, loss)):
        for source in SOURCES:
            if (source, sink) in flows:
                flows[(source, sink)] += amount * share[source]
    return flows


def _integrate(samples):
    """[(dt_seconds, solar, battery, grid, house, tesla)] -> kWh per link."""
    out = {pair: 0.0 for pair in L.SOURCE_SINK_LINKS}
    for dt, solar, battery, grid, house, tesla in samples:
        hours = dt / 3_600_000.0  # W*s -> kWh
        for pair, watts in _prop(solar, battery, grid, house, tesla).items():
            out[pair] += watts * hours
    return out


def _counters(samples):
    """The five daily counters the same samples would produce, kWh."""
    out = dict.fromkeys((L.SOLAR, L.BAT_OUT, L.GRID_IN, L.BAT_IN, L.EXPORT), 0.0)
    for dt, solar, battery, grid, _house, _tesla in samples:
        hours = dt / 3_600_000.0
        out[L.SOLAR] += max(0.0, solar) * hours
        out[L.BAT_OUT] += max(0.0, -battery) * hours
        out[L.GRID_IN] += max(0.0, grid) * hours
        out[L.BAT_IN] += max(0.0, battery) * hours
        out[L.EXPORT] += max(0.0, -grid) * hours
    return out


# One synthetic day with every regime the graph has to survive. Every sample
# leaves a strictly positive residual for the Inverter node, so the flows sum
# exactly to the counters and the layout tests can assert equality rather than
# an envelope. The recorded days in section 4 are the ones that do not.
#
#   1-2  night: grid import, battery charging, Tesla on a 5 kW dispatch
#   3    dawn: battery discharging into a small house load, no sun
#   4-5  midday: strong sun, exporting, battery charging, Tesla on solar
#   6-7  evening: sun fading, battery carrying the house, small import
#   8    late: battery and grid together carrying a Tesla top-up
DAY = [
    # dt(s)  solar  battery   grid   house  tesla
    (3600.0, 0.0, 2500.0, 8300.0, 5700.0, 5000.0),
    (3600.0, 0.0, 2500.0, 3300.0, 700.0, 0.0),
    (3600.0, 0.0, -900.0, 0.0, 850.0, 0.0),
    (3600.0, 5800.0, 1500.0, -3500.0, 700.0, 0.0),
    (3600.0, 6000.0, 0.0, -2000.0, 3900.0, 3200.0),
    (3600.0, 900.0, -600.0, 300.0, 1550.0, 0.0),
    (3600.0, 0.0, -1200.0, 400.0, 1550.0, 0.0),
    (3600.0, 0.0, -3000.0, 1100.0, 4000.0, 3300.0),
]

# The live states main supplied for 2026-08-30. `house` and `tesla` here are the
# daily counters house_consumption_today / tesla_home_charging_energy. Neither
# is a v2 node -- House and Tesla are derived from their own inbound meters --
# but the pair is what the source counters have to accommodate.
LIVE_COUNTERS = {
    L.SOLAR: 24.5,
    L.BAT_OUT: 3.2,
    L.GRID_IN: 28.7,
    L.BAT_IN: 6.3,
    L.EXPORT: 12.7,
}
LIVE_HOUSE_TOTAL = 37.5
LIVE_TESLA_TOTAL = 27.573


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------


def _alloc(states, nodes=None, links=None):
    n, l, _s = L.build()
    return allocator.allocate(
        nodes if nodes is not None else n,
        links if links is not None else l,
        states,
        ignore_missing_entities=True,
    )


def _canon(allocation):
    """An allocation as an order-independent, comparable mapping."""
    out = {}
    for conn in allocation:
        key = (conn["parent"], conn["child"])
        out[key] = out.get(key, 0.0) + round(conn["state"], 9)
    return tuple(sorted(out.items()))


def _states_for(samples):
    flows = _integrate(samples)
    counters = _counters(samples)
    return L.states_from(counters, flows), flows, counters


def _node(node_id, nodes=None):
    for node in (L.NODES if nodes is None else nodes):
        if node["id"] == node_id:
            return node
    raise KeyError(node_id)


# ==========================================================================
# 1. Config identity
# ==========================================================================


EXPECTED_NODES = [
    # id, name, section, type, colour
    (L.SOLAR, "Solar", 0, "entity", "var(--warning-color)"),
    (L.BAT_OUT, "Battery out", 0, "entity", "var(--success-color)"),
    (L.GRID_IN, "Grid import", 0, "entity", "var(--error-color)"),
    (L.HOUSE, "House", 1, "entity", "var(--primary-color)"),
    (L.TESLA, "Tesla", 1, "entity", "#ffffff"),
    (L.BAT_IN, "Battery in", 1, "entity", "var(--success-color)"),
    (L.EXPORT, "Grid export", 1, "entity", "#a78bfa"),
    (L.INVERTER, "Inverter", 1, "entity", "#6b7280"),
]


def test_graph_is_exported_as_plain_importable_data():
    """layout_v2 is the ONLY declaration of the graph. Other suites import
    these symbols rather than re-declaring the nodes and links, because a
    re-declared copy drifts from what ships -- which is how v1's chart tests
    stopped testing the chart. This test pins the names and the shapes."""
    import json

    for name in ("NODES", "LINKS", "SECTIONS", "SOURCE_SINK_LINKS",
                 "COLORS", "FLOW_METERS", "FLOW_KEY"):
        assert name in L.__all__, name
        assert hasattr(L, name), name

    assert isinstance(L.NODES, list) and all(isinstance(n, dict) for n in L.NODES)
    assert isinstance(L.LINKS, list) and all(isinstance(l, dict) for l in L.LINKS)
    assert isinstance(L.SECTIONS, list) and all(isinstance(s, dict) for s in L.SECTIONS)
    # Plain data all the way down: it has to survive the round trip into a
    # Lovelace config over the websocket.
    assert json.loads(json.dumps(L.NODES)) == L.NODES
    assert json.loads(json.dumps(L.LINKS)) == L.LINKS
    assert json.loads(json.dumps(L.SECTIONS)) == L.SECTIONS

    for name in ("build", "check_spans", "sections_of", "states_from",
                 "card_config", "flow_meter"):
        assert callable(getattr(L, name)), name
        assert name in L.__all__, name


def test_node_ids_and_order_are_exact():
    assert [n["id"] for n in L.NODES] == [row[0] for row in EXPECTED_NODES]


@pytest.mark.parametrize("node_id,name,section,ntype,colour", EXPECTED_NODES)
def test_node_identity(node_id, name, section, ntype, colour):
    node = _node(node_id)
    assert node["name"] == name
    assert node["section"] == section
    assert node.get("type", "entity") == ntype
    assert node["color"] == colour


def test_battery_out_precedes_grid_import():
    """Allocation priority. With all discharge windows unset, battery out has
    the fewest possible destinations; being walked after grid import is what
    stranded it entirely on 2026-08-28."""
    order = [n["id"] for n in L.NODES]
    assert order.index(L.BAT_OUT) < order.index(L.GRID_IN)


def test_derived_nodes_are_the_sum_of_their_inbound_meters():
    """House, Tesla and Inverter have no counter of their own (BRIEF_V2.md
    section 1); each is its three inbound flow meters."""
    for sink in (L.HOUSE, L.TESLA, L.INVERTER):
        node = _node(sink)
        inbound = [L.flow_meter(source, sink) for source in SOURCES]
        assert node["entity_id"] == inbound[0]
        assert node["add_entities"] == inbound[1:]
        assert len(inbound) == 3
        assert "subtract_entities" not in node


def test_counter_nodes_carry_no_add_or_subtract():
    """Solar, both battery nodes and both grid nodes read their inverter or
    smart-meter daily counter directly, exactly as in v1."""
    for node_id in (L.SOLAR, L.BAT_OUT, L.GRID_IN, L.BAT_IN, L.EXPORT):
        node = _node(node_id)
        assert "add_entities" not in node
        assert "subtract_entities" not in node
        assert "entity_id" not in node
        assert node_id.startswith("sensor.")


def test_no_unaccounted_node():
    """Removed 2026-08-27 at the owner's request, and BRIEF_V2.md section 6
    forbids re-adding it. The Inverter node is a named loss, not a residual
    bucket."""
    names = {n["name"].lower() for n in L.NODES}
    ids = {n["id"].lower() for n in L.NODES}
    assert not any("unaccounted" in s for s in names | ids)


# --- colours ---------------------------------------------------------------

# CLAUDE.md's "Sankey node colours" table, transcribed. Each of these was
# chosen for a reason recorded there; none is decoration.
CLAUDE_MD_COLOURS = {
    L.GRID_IN: "var(--error-color)",     # most expensive flow on the chart
    L.EXPORT: "#a78bfa",                 # earns money; red was misleading
    L.TESLA: "#ffffff",                  # the owner's car is white
    L.HOUSE: "var(--primary-color)",
    L.SOLAR: "var(--warning-color)",
    L.BAT_OUT: "var(--success-color)",
    L.BAT_IN: "var(--success-color)",
}


@pytest.mark.parametrize("node_id,colour", sorted(CLAUDE_MD_COLOURS.items()))
def test_colour_matches_claude_md(node_id, colour):
    assert _node(node_id)["color"] == colour
    assert L.COLORS[node_id] == colour


def test_inverter_colour_is_unique_and_hueless():
    """The Inverter node is loss, not a flow. It must not collide with any
    assigned colour, and it must not read as one: neutral grey is the one
    swatch left with no hue at all."""
    colour = _node(L.INVERTER)["color"]
    others = [n["color"] for n in L.NODES if n["id"] != L.INVERTER]
    assert colour not in others
    assert colour.startswith("#") and len(colour) == 7
    red, green, blue = (int(colour[1:][i:i + 2], 16) for i in (0, 2, 4))
    assert max(red, green, blue) - min(red, green, blue) <= 24, "not neutral"
    assert 60 <= max(red, green, blue) <= 200, "must sit clear of both black and white"


def test_every_node_has_a_colour():
    for node in L.NODES:
        assert node.get("color"), node["id"]
        assert L.COLORS[node["id"]] == node["color"]


# --- links -----------------------------------------------------------------


EXPECTED_LINKS = [
    (L.SOLAR, L.EXPORT, "sensor.flow_solar_to_grid_daily"),
    (L.SOLAR, L.BAT_IN, "sensor.flow_solar_to_battery_daily"),
    (L.SOLAR, L.HOUSE, "sensor.flow_solar_to_house_daily"),
    (L.SOLAR, L.TESLA, "sensor.flow_solar_to_tesla_daily"),
    (L.SOLAR, L.INVERTER, "sensor.flow_solar_to_inverter_daily"),
    (L.BAT_OUT, L.HOUSE, "sensor.flow_battery_to_house_daily"),
    (L.BAT_OUT, L.TESLA, "sensor.flow_battery_to_tesla_daily"),
    (L.BAT_OUT, L.INVERTER, "sensor.flow_battery_to_inverter_daily"),
    (L.GRID_IN, L.TESLA, "sensor.flow_grid_to_tesla_daily"),
    (L.GRID_IN, L.HOUSE, "sensor.flow_grid_to_house_daily"),
    (L.GRID_IN, L.BAT_IN, "sensor.flow_grid_to_battery_daily"),
    (L.GRID_IN, L.INVERTER, "sensor.flow_grid_to_inverter_daily"),
]


def test_link_count():
    """12, one per source->sink flow. The layout went to two columns on
    2026-08-30, so there are no structural links left at all."""
    assert len(L.LINKS) == 12
    assert len(L.SOURCE_SINK_LINKS) == 12
    assert len(L.LINKS) == len(L.SOURCE_SINK_LINKS)
    per_source = {}
    for source, _sink in L.SOURCE_SINK_LINKS:
        per_source[source] = per_source.get(source, 0) + 1
    assert per_source == {L.SOLAR: 5, L.BAT_OUT: 3, L.GRID_IN: 4}


def test_links_are_exact_and_in_order():
    got = [(l["source"], l["target"], l.get("value")) for l in L.LINKS]
    assert got == EXPECTED_LINKS


@pytest.mark.parametrize("source,target,value", EXPECTED_LINKS)
def test_link_present_once(source, target, value):
    matches = [l for l in L.LINKS if l["source"] == source and l["target"] == target]
    assert len(matches) == 1
    assert matches[0].get("value") == value


def test_solar_to_export_is_declared_first():
    """Export is structurally solar-only. Even with per-link values it stays
    first, because that is the order the layout degrades to if a meter reads
    `unavailable`."""
    assert (L.LINKS[0]["source"], L.LINKS[0]["target"]) == (L.SOLAR, L.EXPORT)


def test_no_battery_to_grid_link():
    """All three discharge windows are unset and must stay unset. Merely
    declaring the link fabricated battery export in v1."""
    assert not any(
        l["source"] == L.BAT_OUT and l["target"] == L.EXPORT for l in L.LINKS
    )
    assert not any(l["target"] == L.EXPORT and l["source"] != L.SOLAR for l in L.LINKS)


def test_every_source_sink_link_has_a_distinct_value_meter():
    values = [l["value"] for l in L.LINKS if "value" in l]
    assert len(values) == 12
    assert len(set(values)) == 12
    assert set(values) == set(L.FLOW_METERS)


def test_every_link_carries_a_value():
    """Two columns means every link is a measured source->sink flow. Nothing
    is left to the card's greedy fill order."""
    assert len(L.LINKS) == 12
    for link in L.LINKS:
        assert link["value"] == L.flow_meter(link["source"], link["target"])


def test_flow_meter_naming():
    assert L.flow_meter(L.SOLAR, L.HOUSE) == "sensor.flow_solar_to_house_daily"
    assert L.flow_meter(L.BAT_OUT, L.INVERTER) == "sensor.flow_battery_to_inverter_daily"
    assert L.flow_meter(L.GRID_IN, L.BAT_IN) == "sensor.flow_grid_to_battery_daily"
    assert L.flow_meter(L.SOLAR, L.EXPORT) == "sensor.flow_solar_to_grid_daily"
    with pytest.raises(KeyError):
        L.flow_meter(L.HOUSE, L.TESLA)


# --- spans and sections ----------------------------------------------------


def test_every_link_spans_exactly_one_section():
    assert L.check_spans() == []


def test_check_spans_catches_a_two_hop_link():
    """The guard has to be able to fail. A link from section 0 to section 2 is
    what makes the card synthesise the unlabelled ghost box."""
    nodes, links, _ = L.build()
    nodes.append({"id": "far", "name": "Far", "section": 2})
    for link in links:
        if link["source"] == L.SOLAR and link["target"] == L.BAT_IN:
            link["target"] = "far"
    assert L.check_spans(nodes, links) == [(L.SOLAR, "far", 2)]


def test_check_spans_catches_a_same_section_link():
    nodes, links, _ = L.build()
    links.append({"source": L.HOUSE, "target": L.TESLA})
    assert (L.HOUSE, L.TESLA, 0) in L.check_spans(nodes, links)


def test_sections_config():
    assert len(L.SECTIONS) == 2
    assert [sec["sort_by"] for sec in L.SECTIONS] == ["none", "none"]
    counts = {}
    for node in L.NODES:
        counts[node["section"]] = counts.get(node["section"], 0) + 1
    assert counts == {0: 3, 1: 5}


def test_no_declared_passthrough_node():
    """A declared passthrough cannot be a terminus -- `willUpdate` throws
    `missing_child` when it has no children -- and renders as an unlabelled
    box at fill-opacity .4. Stored is a remaining_parent_state instead."""
    assert not any(n.get("type") == "passthrough" for n in L.NODES)


def test_build_returns_independent_copies():
    a_nodes, a_links, a_sections = L.build()
    b_nodes, b_links, b_sections = L.build()
    a_nodes[0]["color"] = "#000000"
    a_links[0]["value"] = "sensor.nope"
    a_sections[0]["min_width"] = 1
    assert b_nodes[0]["color"] == "var(--warning-color)"
    assert b_links[0]["value"] == "sensor.flow_solar_to_grid_daily"
    assert b_sections[0]["min_width"] == 170
    assert L.NODES[0]["color"] == "var(--warning-color)"
    assert L.LINKS[0]["value"] == "sensor.flow_solar_to_grid_daily"


def test_card_config_shape():
    card = L.card_config()
    assert card["type"] == "custom:sankey-chart"
    assert card["energy_date_selection"] is True
    assert card["unit_prefix"] == "k"
    assert [n["id"] for n in card["nodes"]] == [n["id"] for n in L.NODES]
    assert card["links"] == L.LINKS
    assert card["sections"] == L.SECTIONS


def test_sections_of_and_states_from():
    assert L.sections_of()[L.TESLA] == 1
    assert L.sections_of([{"id": "x"}]) == {"x": 0}
    states = L.states_from({L.SOLAR: 1.0}, {(L.SOLAR, L.HOUSE): 0.5})
    assert states == {L.SOLAR: 1.0, "sensor.flow_solar_to_house_daily": 0.5}
    # House/Tesla/Inverter are derived, never seeded.
    assert L.HOUSE not in states and L.TESLA not in states and L.INVERTER not in states


# ==========================================================================
# 2. What the card would draw
# ==========================================================================


def test_shipped_layout_synthesises_no_ghost():
    states, _flows, _counters_ = _states_for(DAY)
    result = _alloc(states)
    assert result.ghost_nodes == []
    assert result.has_ghosts is False
    assert result.warnings == []


def test_a_four_section_layout_would_synthesise_a_ghost():
    """Control for the test above: the detector has to be able to fire. This is
    the exact shape that once made the battery appear to charge itself."""
    nodes, links, _ = L.build()
    for node in nodes:
        if node["id"] in (L.BAT_IN, L.EXPORT):
            node["section"] = 2
    states, _f, _c = _states_for(DAY)
    result = _alloc(states, nodes, links)
    assert result.has_ghosts
    assert any(g.startswith(L.BAT_IN) for g in result.ghost_nodes)


def test_every_source_sink_ribbon_equals_its_meter():
    states, flows, _c = _states_for(DAY)
    result = _alloc(states)
    for pair in L.SOURCE_SINK_LINKS:
        assert result.state(*pair) == pytest.approx(flows[pair], abs=1e-9), pair


def test_no_sink_is_stranded():
    """A sink with a positive state and a zero allocation draws no ribbon at
    all and looks identical to a link that was never declared."""
    states, flows, _c = _states_for(DAY)
    result = _alloc(states)
    for pair in L.SOURCE_SINK_LINKS:
        if flows[pair] > 1e-6:
            assert result.state(*pair) > 0.0, pair


def test_no_source_is_left_unspent():
    states, _flows, counters = _states_for(DAY)
    result = _alloc(states)
    for source in SOURCES:
        assert result.spent[source] == pytest.approx(counters[source], abs=1e-9)


def test_derived_sinks_fill_exactly():
    """House, Tesla and Inverter are defined as their inbound sums, so they can
    only fail to fill if a source counter is short of its own meters."""
    states, flows, _c = _states_for(DAY)
    result = _alloc(states)
    for sink in (L.HOUSE, L.TESLA, L.INVERTER):
        want = sum(flows[(source, sink)] for source in SOURCES)
        assert result.filled[sink] == pytest.approx(want, abs=1e-9), sink


def test_every_sink_is_a_terminus():
    """Two columns: nothing in section 1 has a child, so no sink's energy can
    reappear further right and be counted twice."""
    states, _flows, _counters = _states_for(DAY)
    result = _alloc(states)
    sinks = {link["target"] for link in L.LINKS}
    assert sinks == {L.HOUSE, L.TESLA, L.BAT_IN, L.EXPORT, L.INVERTER}
    for sink in sinks:
        assert [l for l in L.LINKS if l["source"] == sink] == []
    assert sum(result.filled.get(n, 0.0) for n in sinks) == pytest.approx(
        sum(result.spent.values()), abs=1e-9)


def test_inverter_node_is_the_sum_of_its_three_meters():
    states, flows, _c = _states_for(DAY)
    result = _alloc(states)
    want = sum(flows[(source, L.INVERTER)] for source in SOURCES)
    assert want > 0.0, "the synthetic day must actually exercise the loss node"
    assert result.filled[L.INVERTER] == pytest.approx(want, abs=1e-9)


def test_tesla_ribbon_knows_its_source_mix():
    """The owner's ask: 'the tesla ribbon should know how much of grid/battery
    it took'. DAY charges the car once overnight on grid and once at midday on
    sun, so all three sources must reach it."""
    states, flows, _c = _states_for(DAY)
    for source in SOURCES:
        assert flows[(source, L.TESLA)] > 0.0, source
    result = _alloc(states)
    for source in SOURCES:
        assert result.state(source, L.TESLA) > 0.0, source


def test_zero_everything_draws_nothing_and_raises_nothing():
    states = L.states_from(
        dict.fromkeys((L.SOLAR, L.BAT_OUT, L.GRID_IN, L.BAT_IN, L.EXPORT), 0.0),
        {pair: 0.0 for pair in L.SOURCE_SINK_LINKS},
    )
    result = _alloc(states)
    assert result.ghost_nodes == []
    assert all(conn["state"] == 0.0 for conn in result)


def test_a_zero_sink_strands_nothing():
    """A day with no car at all: Tesla's three meters are zero, its node state
    is zero, and every other ribbon is unaffected."""
    no_car = [(dt, s, b, g, h, 0.0) for dt, s, b, g, h, _t in DAY]
    states, flows, counters = _states_for(no_car)
    result = _alloc(states)
    for source in SOURCES:
        assert result.state(source, L.TESLA) == 0.0
        assert result.spent[source] == pytest.approx(counters[source], abs=1e-9)
    assert flows[(L.SOLAR, L.EXPORT)] > 0.0
    assert result.state(L.SOLAR, L.EXPORT) == pytest.approx(
        flows[(L.SOLAR, L.EXPORT)], abs=1e-9
    )


def test_live_daily_states():
    """Main's live figures for 2026-08-30. `house` (37.5) and `tesla` (27.573)
    are counters, not v2 nodes; they enter only as the load the day had to
    carry. The five source/sink counters are pinned to the live values and the
    per-link meters are manufactured from a sample sequence that integrates to
    them."""
    scale = LIVE_COUNTERS[L.SOLAR] / _counters(DAY)[L.SOLAR]
    samples = [(dt, s * scale, b, g, h, t) for dt, s, b, g, h, t in DAY]
    states, flows, counters = _states_for(samples)
    assert counters[L.SOLAR] == pytest.approx(LIVE_COUNTERS[L.SOLAR], abs=1e-9)

    result = _alloc(states)
    assert result.ghost_nodes == []
    for pair in L.SOURCE_SINK_LINKS:
        assert result.state(*pair) == pytest.approx(flows[pair], abs=1e-9)


def test_source_counter_short_of_its_meters_truncates_from_the_tail():
    """The counters and the integrated meters are two different measurement
    paths and will not agree exactly (CLAUDE.md records a ~7.8% house gap). If
    a source counter comes in low, the card clamps: the shortfall is absorbed
    by the source's trailing links, no ribbon exceeds its own meter, nothing
    goes negative and nothing is fabricated.

    This is the one place where declaration order still bites, which is why the
    v1 order is kept in `SOURCE_SINK_LINKS`."""
    states, flows, counters = _states_for(DAY)
    short = sum(flows[(L.GRID_IN, sink)] for sink in SINKS if (L.GRID_IN, sink) in flows)
    assert short == pytest.approx(counters[L.GRID_IN], abs=1e-9)
    states[L.GRID_IN] = short - 0.5
    result = _alloc(states)

    assert result.spent[L.GRID_IN] == pytest.approx(short - 0.5, abs=1e-9)
    grid_sinks = [sink for source, sink in L.SOURCE_SINK_LINKS if source == L.GRID_IN]
    drawn = [result.state(L.GRID_IN, sink) for sink in grid_sinks]
    want = [flows[(L.GRID_IN, sink)] for sink in grid_sinks]
    assert all(0.0 <= got <= exp + 1e-9 for got, exp in zip(drawn, want))
    assert sum(want) - sum(drawn) == pytest.approx(0.5, abs=1e-9)
    # Leading links are untouched; the tail carries the loss.
    assert drawn[0] == pytest.approx(want[0], abs=1e-9)
    assert drawn[1] == pytest.approx(want[1], abs=1e-9)
    assert drawn[-1] == 0.0
    # The other two sources are unaffected.
    for source in (L.SOLAR, L.BAT_OUT):
        assert result.spent[source] == pytest.approx(counters[source], abs=1e-9)


# ==========================================================================
# 3. Order invariance
# ==========================================================================


def _links_grouped(links):
    """[(source, [link, ...])] in first-appearance order."""
    order, groups = [], {}
    for link in links:
        if link["source"] not in groups:
            order.append(link["source"])
            groups[link["source"]] = []
        groups[link["source"]].append(link)
    return [(source, groups[source]) for source in order]


def _link_permutations(links):
    """Every ordering of each source's own links, rebuilt into one list.

    Only the relative order *within* a source matters: the card walks nodes
    first and reads each node's `children` in links-array order.
    """
    grouped = _links_grouped(links)
    for combo in itertools.product(*[itertools.permutations(g) for _s, g in grouped]):
        out = []
        for group in combo:
            out.extend(group)
        yield out


def _node_permutations(nodes):
    """Every ordering of each section's own nodes."""
    sections = {}
    for node in nodes:
        sections.setdefault(node["section"], []).append(node)
    keys = sorted(sections)
    for combo in itertools.product(*[itertools.permutations(sections[k]) for k in keys]):
        out = []
        for group in combo:
            out.extend(group)
        yield out


def _strip_values(links):
    return [{k: v for k, v in l.items() if k != "value"} for l in links]


def test_permutation_space_sizes():
    """Pin the sweep sizes so a silently shrinking sweep is a failure."""
    nodes, links, _ = L.build()
    assert sum(1 for _ in _node_permutations(nodes)) == 720     # 3! * 5!
    assert sum(1 for _ in _link_permutations(links)) == 17_280  # 5! * 3! * 4!


def test_node_order_never_changes_the_allocation():
    states, _flows, _c = _states_for(DAY)
    nodes, links, _ = L.build()
    seen, count = set(), 0
    for permuted in _node_permutations(nodes):
        seen.add(_canon(_alloc(states, permuted, links)))
        count += 1
    assert count == 720
    assert len(seen) == 1


def test_link_order_never_changes_the_allocation():
    states, _flows, _c = _states_for(DAY)
    nodes, links, _ = L.build()
    seen, count = set(), 0
    for permuted in _link_permutations(links):
        seen.add(_canon(_alloc(states, nodes, permuted)))
        count += 1
    assert count == 17_280
    assert len(seen) == 1


def test_joint_node_and_link_order_never_changes_the_allocation():
    """The full joint space is 4,320 * 34,560 = 149,299,200, which is not
    runnable. This is a seeded random sample of it."""
    states, _flows, _c = _states_for(DAY)
    nodes, links, _ = L.build()
    node_space = list(_node_permutations(nodes))
    link_space = list(_link_permutations(links))
    rng = random.Random(20260830)
    seen = set()
    for _ in range(4_000):
        seen.add(
            _canon(
                _alloc(states, rng.choice(node_space), rng.choice(link_space))
            )
        )
    assert len(seen) == 1


def test_without_per_link_values_the_order_does_change_it():
    """The control. v1 had no `value` key and the same sweep collapsed ten
    distinct allocations into one only after the orders were fixed by hand. If
    this test ever finds a single allocation, the sweeps above are proving
    nothing."""
    states, _flows, _c = _states_for(DAY)
    nodes, links, _ = L.build()
    bare = _strip_values(links)
    seen = set()
    for permuted in _link_permutations(bare):
        seen.add(_canon(_alloc(states, nodes, permuted)))
    assert len(seen) > 1


def test_order_invariance_holds_on_the_live_numbers_too():
    scale = LIVE_COUNTERS[L.SOLAR] / _counters(DAY)[L.SOLAR]
    samples = [(dt, s * scale, b, g, h, t) for dt, s, b, g, h, t in DAY]
    states, _flows, _c = _states_for(samples)
    nodes, links, _ = L.build()
    seen = set()
    for permuted in _node_permutations(nodes):
        seen.add(_canon(_alloc(states, permuted, links)))
    assert len(seen) == 1


# ==========================================================================
# 4. Replay of the recorded days
# ==========================================================================


def _tesla_series(day):
    """Held (left-Riemann) Tesla power for one recorded day, and its coverage.

    Returns (lookup, in_day_samples). `lookup(t)` is the last value at or
    before t, or None before the first sample. TeslaMate publishes on change
    and a plateau can hold for ~80 minutes, so holding is correct, not stale.
    """
    path = os.path.join(FIXTURES, "tesla_history_%s.json.gz" % day)
    with gzip.open(path, "rt") as fh:
        doc = json.load(fh)
    start = float(doc["start_epoch"])
    points = []
    for ts, state in doc["series"]["tesla"]:
        try:
            points.append((float(ts), float(state)))
        except (TypeError, ValueError):
            points.append((float(ts), None))
    points.sort(key=lambda p: p[0])
    in_day = sum(1 for ts, _v in points if ts >= start)

    def lookup(t):
        held = None
        for ts, value in points:
            if ts <= t:
                held = value
            else:
                break
        return held

    return lookup, in_day


REPLAY_DAYS = ["2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30"]

# A day is only usable for anything Tesla-dependent if the recorded car channel
# actually covers it. 2026-08-27 carries a single `0` sample stamped BEFORE the
# day's first house reading, and its energy counter flaps unknown -> 0.000 ->
# unknown -> 0.000: that is a sensor being set up, not a car that provably did
# not charge. Zero-filling it would silently assert the second, so Tesla-
# dependent figures for that day are EXCLUDED, not filled. The rule is
# mechanical, not a hard-coded date.
MIN_TESLA_SAMPLES = 2
TESLA_DAYS = [d for d in REPLAY_DAYS if _tesla_series(d)[1] >= MIN_TESLA_SAMPLES]


def _replay_states(day, tesla_watts=None, require_tesla=True):
    """Integrate one recorded day into v2 flows and counters.

    Tesla power comes from the recorded car channel.

    `require_tesla=True` (the default) DROPS every sample the car channel does
    not cover, rather than zero-filling it. On 2026-08-27 that discards 13.35 h
    of 23.86 h, because the sensor's first reading lands 13.35 h into the day.
    Any figure that depends on the House/Tesla split must use this mode, and
    only on a day in TESLA_DAYS.

    `require_tesla=False` fills the uncovered samples with T = 0 and keeps the
    whole day. That is legitimate ONLY for quantities that are invariant under
    T -- the Inverter node, every source's total spend, export, and Stored --
    which test_inverter_node_is_independent_of_tesla and
    test_replay_totals_are_invariant_under_tesla establish. Throwing away 13.35
    real hours to protect a figure that provably cannot be biased would lose
    information for nothing.

    `tesla_watts` overrides the channel with a constant, for the synthetic
    plateau test only.
    """
    counters = replay.load_counters()["days"]
    doc = replay.load_day(day)
    end = replay.last_sample_epoch(doc) if day == max(counters) else None
    tesla_at, _in_day = _tesla_series(day)
    samples = []
    for t, dt, v in replay.intervals(doc, end):
        if any(v[c] is None for c in replay.CHANNELS):
            continue
        house = v["house"]
        if tesla_watts is not None:
            car = tesla_watts
        else:
            car = tesla_at(t)
            if car is None:
                if require_tesla:
                    continue
                car = 0.0
        samples.append((dt, v["solar"], v["battery"], v["grid"], house,
                        min(max(0.0, car), max(0.0, house))))
    return _states_for(samples)


def _replay_full(day):
    """The whole recorded day, T=0 where the car channel does not reach.

    Only for T-invariant quantities. See _replay_states.
    """
    return _replay_states(day, require_tesla=False)


@pytest.mark.parametrize("day", REPLAY_DAYS)
def test_replay_day_draws_cleanly(day):
    states, day_flows, counters = _replay_full(day)
    result = _alloc(states)
    assert result.ghost_nodes == []
    assert result.warnings == []
    for pair in L.SOURCE_SINK_LINKS:
        # The card can only ever shrink a ribbon, never invent one.
        assert 0.0 <= result.state(*pair) <= day_flows[pair] + 1e-9, (day, pair)
    for source in SOURCES:
        assert result.spent[source] <= counters[source] + 1e-9


@pytest.mark.parametrize("day", REPLAY_DAYS)
def test_replay_day_truncation_is_negligible(day):
    """The one seam in BRIEF_V2.md section 2's rule. When the measured residual
    (S+B+G)-(Hr+T+C+E) is negative -- which happens on individual samples,
    because solar and battery are DC while house and grid are AC -- `L` clamps
    to zero and the proportional shares then draw slightly more than the
    sources hold. The card absorbs it by truncating each source's trailing
    links.

    Measured over the four recorded days it is 0.02-0.13 kWh, under 0.3% of
    supply, so it is accepted rather than fixed here. This test is the alarm if
    it ever stops being negligible."""
    states, day_flows, counters = _replay_full(day)
    result = _alloc(states)
    truncated = sum(max(0.0, day_flows[p] - result.state(*p)) for p in L.SOURCE_SINK_LINKS)
    supply = sum(counters[source] for source in SOURCES)
    assert truncated <= 0.3
    assert truncated <= 0.005 * supply, (day, truncated, supply)


@pytest.mark.parametrize("day", REPLAY_DAYS)
def test_replay_day_inverter_node_is_positive_and_plausible(day):
    """BRIEF_V2.md section 2 expects roughly 2.5 kWh/day of named loss, to be
    cross-checked against flow-law's fitted parasitic. This test only pins the
    sign and a generous envelope; the measured numbers are reported, not
    asserted to a fitted constant."""
    _states, day_flows, counters = _replay_full(day)
    loss = sum(day_flows[(source, L.INVERTER)] for source in SOURCES)
    supply = sum(counters[source] for source in SOURCES)
    assert loss >= 0.0
    assert loss <= supply
    assert 0.2 <= loss <= 8.0, (day, loss)


@pytest.mark.parametrize("day", REPLAY_DAYS)
def test_replay_day_export_is_solar_only_and_slightly_underfills(day):
    """`s2e = min(S, E)` is the whole of the export flow -- no other source may
    ever reach Grid export, because all three discharge windows are unset.

    On individual samples the AC export meter briefly exceeds the DC solar
    reading, so `min` clips and Grid export ends the day marginally unfed:
    0.02-0.05 kWh on the recorded days. That is the honest answer under this
    rule; inventing a second source for it is exactly the fabrication v1's
    battery-to-grid link committed."""
    _states, day_flows, counters = _replay_full(day)
    solar_to_export = day_flows[(L.SOLAR, L.EXPORT)]
    assert not any(
        source != L.SOLAR for source, sink in L.SOURCE_SINK_LINKS if sink == L.EXPORT
    )
    assert 0.0 <= solar_to_export <= counters[L.EXPORT] + 1e-9
    shortfall = counters[L.EXPORT] - solar_to_export
    assert shortfall <= 0.15, (day, shortfall)


def test_replay_with_a_synthetic_car_reaches_tesla():
    """A held 7 kW plateau, the shape TeslaMate actually publishes. Synthetic
    on purpose -- it overrides the recorded channel to force the car above the
    house baseline on every sample."""
    states, day_flows, _c = _replay_states("2026-08-28", tesla_watts=7000.0)
    result = _alloc(states)
    assert sum(day_flows[(source, L.TESLA)] for source in SOURCES) > 1.0
    assert result.filled[L.TESLA] == pytest.approx(
        sum(day_flows[(source, L.TESLA)] for source in SOURCES), abs=1e-9
    )
    assert result.ghost_nodes == []


@pytest.mark.parametrize("day", TESLA_DAYS)
def test_replay_recorded_car_reaches_tesla(day):
    """The recorded car channel, not a synthetic one. Only days whose Tesla
    recording actually covers them appear here."""
    states, day_flows, _c = _replay_states(day)
    result = _alloc(states)
    total = sum(day_flows[(source, L.TESLA)] for source in SOURCES)
    assert result.filled.get(L.TESLA, 0.0) == pytest.approx(total, abs=1e-9)
    assert result.ghost_nodes == []


def test_tesla_days_exclude_the_day_with_no_recorded_car_channel():
    """2026-08-27 must be excluded, not zero-filled. Its recorded car channel
    holds a single `0` stamped before the day's first house reading, and its
    energy counter flaps unknown -> 0.000 -> unknown -> 0.000. Filling that
    with zero would assert 'the car did not charge', which the data does not
    support."""
    lookup, in_day = _tesla_series("2026-08-27")
    assert in_day < MIN_TESLA_SAMPLES
    assert "2026-08-27" in REPLAY_DAYS
    assert "2026-08-27" not in TESLA_DAYS
    assert TESLA_DAYS == ["2026-08-28", "2026-08-29", "2026-08-30"]
    assert callable(lookup)


@pytest.mark.parametrize("day", REPLAY_DAYS)
def test_replay_totals_are_invariant_under_tesla(day):
    """The same algebra as test_inverter_node_is_independent_of_tesla, but on
    every sample of a recorded day, and this is the test that licences
    `_replay_full`'s T = 0 fill.

    Replay each day twice -- once with T forced to 0, once with T forced to the
    whole house load -- and the Inverter node, every source's total spend, the
    export flow and Battery in must come out bit-for-bit identical. Only the
    House/Tesla split moves, which is precisely the class of figure excluded
    for 2026-08-27."""
    zero, zero_flows, zero_counters = _replay_states(day, tesla_watts=0.0)
    full, full_flows, full_counters = _replay_states(day, tesla_watts=1e9)

    assert zero_counters == full_counters
    for source in SOURCES:
        assert (sum(zero_flows[(source, sink)] for sink in SINKS
                    if (source, sink) in zero_flows)
                == pytest.approx(sum(full_flows[(source, sink)] for sink in SINKS
                                     if (source, sink) in full_flows), abs=1e-9))
        assert zero_flows[(source, L.INVERTER)] == pytest.approx(
            full_flows[(source, L.INVERTER)], abs=1e-12
        )
    assert zero_flows[(L.SOLAR, L.EXPORT)] == pytest.approx(
        full_flows[(L.SOLAR, L.EXPORT)], abs=1e-12
    )
    for source in (L.SOLAR, L.GRID_IN):
        assert zero_flows[(source, L.BAT_IN)] == pytest.approx(
            full_flows[(source, L.BAT_IN)], abs=1e-12
        )
    # The split itself does move -- otherwise this test proves nothing.
    zero_tesla = sum(zero_flows[(s, L.TESLA)] for s in SOURCES)
    full_tesla = sum(full_flows[(s, L.TESLA)] for s in SOURCES)
    assert zero_tesla == 0.0
    assert full_tesla > 0.0


def test_inverter_node_is_independent_of_tesla():
    """Why the 2026-08-27 Inverter figure survives that exclusion.

    T only redistributes between the House and Tesla sinks: Hr = house - T, so
    Hr + T = house whatever T is, and L = (S+B+G) - (Hr+T+C+E) therefore does
    not contain T at all. The source shares depend only on S1, B and G. So the
    Inverter node -- and every source's total spend -- is mathematically
    invariant under any T in [0, house], and an absent car channel cannot bias
    it in either direction.

    This is an algebraic claim, so it is checked across the whole range rather
    than at one point."""
    sample = (3600.0, 1500.0, -800.0, 2200.0, 3000.0)
    baseline = None
    for fraction in (0.0, 0.01, 0.25, 0.5, 0.75, 0.99, 1.0):
        dt, solar, battery, grid, house = sample
        got = _prop(solar, battery, grid, house, house * fraction)
        loss = tuple(round(got[(source, L.INVERTER)], 12) for source in SOURCES)
        spend = tuple(
            round(sum(got[(source, sink)] for sink in SINKS
                      if (source, sink) in got), 12)
            for source in SOURCES
        )
        split = sum(got[(source, L.TESLA)] for source in SOURCES)
        if baseline is None:
            baseline = (loss, spend)
            assert sum(loss) > 0.0, "the sample must exercise the loss node"
        assert (loss, spend) == baseline, fraction
        # ...while the House/Tesla split itself moves with T, which is exactly
        # the class of figure that must be excluded for 2026-08-27.
        assert split == pytest.approx(house * fraction, abs=1e-9)
        assert dt == 3600.0


@pytest.mark.parametrize("day", REPLAY_DAYS)
def test_replay_day_node_order_invariant(day):
    states, _flows, _c = _replay_full(day)
    nodes, links, _ = L.build()
    seen = set()
    for permuted in _node_permutations(nodes):
        seen.add(_canon(_alloc(states, permuted, links)))
    assert len(seen) == 1


# ==========================================================================
# 5. Bad input
# ==========================================================================


def test_unavailable_meter_poisons_only_its_own_source():
    """`Number('unavailable')` is NaN and `Math.min(m, y, NaN)` is NaN, which
    the card then writes into that parent's spent total. Every later link from
    the SAME source is lost; other sources are untouched. This is a real
    fragility of the per-link-value design and is recorded, not fixed here."""
    states, flows, _c = _states_for(DAY)
    states[L.flow_meter(L.SOLAR, L.BAT_IN)] = "unavailable"
    result = _alloc(states)
    assert result.state(L.SOLAR, L.EXPORT) == pytest.approx(
        flows[(L.SOLAR, L.EXPORT)], abs=1e-9
    )
    assert math.isnan(result.state(L.SOLAR, L.BAT_IN))
    for sink in (L.HOUSE, L.TESLA, L.INVERTER):
        assert result.state(L.SOLAR, sink) == 0.0
    # Battery and grid are unaffected.
    for sink in (L.HOUSE, L.TESLA, L.INVERTER):
        assert result.state(L.BAT_OUT, sink) == pytest.approx(
            flows[(L.BAT_OUT, sink)], abs=1e-9
        )


@pytest.mark.parametrize("bad", ["unknown", "unavailable", None, "", "n/a"])
def test_unavailable_source_counter_zeroes_that_source_only(bad):
    states, flows, _c = _states_for(DAY)
    states[L.BAT_OUT] = bad
    result = _alloc(states)
    for sink in (L.HOUSE, L.TESLA, L.INVERTER):
        assert result.state(L.BAT_OUT, sink) == 0.0
    for sink in (L.HOUSE, L.TESLA, L.BAT_IN, L.INVERTER, L.EXPORT):
        if (L.SOLAR, sink) in flows:
            assert result.state(L.SOLAR, sink) == pytest.approx(
                flows[(L.SOLAR, sink)], abs=1e-9
            )


def test_missing_meters_are_treated_as_zero():
    """ignore_missing_entities mirrors the card option; the helpers may not
    exist yet when the layout first lands."""
    counters = dict(LIVE_COUNTERS)
    result = _alloc(counters)
    assert result.ghost_nodes == []
    for pair in L.SOURCE_SINK_LINKS:
        assert result.state(*pair) == 0.0


def test_missing_meters_with_strict_entities_raises():
    nodes, links, _ = L.build()
    with pytest.raises(allocator.MissingEntityError):
        allocator.allocate(nodes, links, dict(LIVE_COUNTERS),
                           ignore_missing_entities=False)


def test_negative_meter_draws_no_backwards_ribbon():
    """A utility_meter cannot go negative, but a bad template feeding one
    could. A negative meter drags down the House node's own state too, because
    House is the sum of its inbound meters -- and the card's
    `Math.max(0, child_state - filled)` then makes the child remainder falsy,
    so the ribbon is dropped rather than drawn backwards."""
    states, flows, _c = _states_for(DAY)
    states[L.flow_meter(L.GRID_IN, L.HOUSE)] = -5.0
    result = _alloc(states)
    assert all(conn["state"] >= 0.0 for conn in result)
    assert result.state(L.GRID_IN, L.HOUSE) == 0.0
    # Everything not fed through House is untouched.
    assert result.state(L.SOLAR, L.EXPORT) == pytest.approx(
        flows[(L.SOLAR, L.EXPORT)], abs=1e-9
    )
    assert result.state(L.GRID_IN, L.BAT_IN) == pytest.approx(
        flows[(L.GRID_IN, L.BAT_IN)], abs=1e-9
    )


def test_infinite_meter_does_not_break_the_layout():
    states, flows, _c = _states_for(DAY)
    states[L.flow_meter(L.SOLAR, L.HOUSE)] = float("inf")
    result = _alloc(states)
    assert result.ghost_nodes == []
    # min() with the parent/child remainders still binds.
    assert result.state(L.SOLAR, L.HOUSE) <= sum(
        flows[(L.SOLAR, sink)] for sink in SINKS if (L.SOLAR, sink) in flows
    ) + 1e-9


def test_string_numbers_are_accepted():
    states, flows, _c = _states_for(DAY)
    states = {k: (str(v) if isinstance(v, float) else v) for k, v in states.items()}
    result = _alloc(states)
    for pair in L.SOURCE_SINK_LINKS:
        assert result.state(*pair) == pytest.approx(flows[pair], abs=1e-6), pair


# --- the reference decomposition's own branches ----------------------------
# These guard the fixture generator above, not shipping code: if `_prop` drifts
# the per-link values stop being consistent and the layout tests lose meaning.


def test_flow_key_bridges_the_layout_to_flows_py():
    """The twelve links this layout draws and the twelve flows flows.py
    computes must be the same twelve, named two different ways. Neither module
    restates the other's naming; FLOW_KEY is the single mapping."""
    assert set(L.FLOW_KEY) == set(L.SOURCE_SINK_LINKS)
    assert set(L.FLOW_KEY.values()) == set(flows.FLOWS)
    assert len(set(L.FLOW_KEY.values())) == 12
    # The absent combinations are the design, not an omission.
    assert "battery_to_battery" not in flows.FLOWS
    assert "battery_to_export" not in flows.FLOWS
    assert "grid_to_export" not in flows.FLOWS
    # Each mapped key really does name its own source and sink.
    for (source, sink), key in L.FLOW_KEY.items():
        left, right = key.split("_to_")
        assert left in flows.SOURCES and right in flows.SINKS
        assert left == _SOURCE_NAME[source]

    # Entity id and flows.py key agree on eleven of the twelve...
    def meter_slug(source, sink):
        return L.flow_meter(source, sink)[len("sensor.flow_"):-len("_daily")]

    differ = [pair for pair, key in L.FLOW_KEY.items() if meter_slug(*pair) != key]
    # ...and differ on exactly one, deliberately: the export sink is `grid` in
    # an entity id (the physical thing the meter measures, reading naturally
    # beside flow_grid_to_house_daily) and `export` in a flows.py key (the node
    # name). Both namings are already committed elsewhere, so the seam is
    # mapped rather than renamed. This test exists so it is never a surprise.
    assert differ == [(L.SOLAR, L.EXPORT)]
    assert meter_slug(L.SOLAR, L.EXPORT) == "solar_to_grid"
    assert L.FLOW_KEY[(L.SOLAR, L.EXPORT)] == "solar_to_export"


_SOURCE_NAME = {L.SOLAR: "solar", L.BAT_OUT: "battery", L.GRID_IN: "grid"}


@pytest.mark.parametrize(
    "sample",
    [
        (0.0, 2500.0, 8300.0, 5700.0, 5000.0),
        (0.0, -900.0, 0.0, 850.0, 0.0),
        (5800.0, 1500.0, -3500.0, 700.0, 0.0),
        (6000.0, 0.0, -2000.0, 3900.0, 3200.0),
        (900.0, -600.0, 300.0, 1550.0, 0.0),
        (0.0, -3000.0, 1100.0, 4000.0, 3300.0),
        (1000.0, -400.0, 2500.0, 3000.0, 0.0),
        (0.0, 0.0, 100.0, 900.0, 0.0),      # negative residual, L clamps
        (0.0, 0.0, 0.0, 0.0, 0.0),          # no supply at all
        (4000.0, -400.0, -3000.0, 900.0, 0.0),
        (600.0, -250.0, 1000.0, 800.0, 5000.0),  # tesla over house
    ],
)
def test_flows_matches_the_hand_written_reference(sample):
    """flows.py against BRIEF_V2.md section 2 transcribed independently. This
    is the test that fires if the shipping physics drifts from the stated
    rule -- in either direction."""
    got = _prop(*sample)
    want = _prop_ref(*sample)
    assert set(got) == set(want) == set(L.SOURCE_SINK_LINKS)
    for pair in L.SOURCE_SINK_LINKS:
        assert got[pair] == pytest.approx(want[pair], abs=1e-9), pair


def test_flows_matches_the_reference_across_a_replayed_day():
    """The same parity, integrated over every sample of a recorded day rather
    than on hand-picked ones."""
    counters = replay.load_counters()["days"]
    doc = replay.load_day("2026-08-29")
    tesla_at, _n = _tesla_series("2026-08-29")
    totals_flows = {pair: 0.0 for pair in L.SOURCE_SINK_LINKS}
    totals_ref = dict(totals_flows)
    seen = 0
    for t, dt, v in replay.intervals(doc, None):
        if any(v[c] is None for c in replay.CHANNELS):
            continue
        car = tesla_at(t)
        if car is None:
            continue
        hours = dt / 3_600_000.0
        args = (v["solar"], v["battery"], v["grid"], v["house"],
                min(max(0.0, car), max(0.0, v["house"])))
        got, want = _prop(*args), _prop_ref(*args)
        for pair in L.SOURCE_SINK_LINKS:
            totals_flows[pair] += got[pair] * hours
            totals_ref[pair] += want[pair] * hours
        seen += 1
    assert seen > 1000, seen
    assert "2026-08-29" in counters
    for pair in L.SOURCE_SINK_LINKS:
        assert totals_flows[pair] == pytest.approx(totals_ref[pair], abs=1e-9), pair


def test_prop_shares_sum_to_one():
    flows = _prop(3000.0, -800.0, 1200.0, 2000.0, 0.0)
    total = sum(flows.values())
    assert total == pytest.approx(3000.0 + 800.0 + 1200.0, abs=1e-9)


def test_prop_with_no_supply_is_all_zero():
    assert all(v == 0.0 for v in _prop(0.0, 0.0, 0.0, 0.0, 0.0).values())


def test_prop_clamps_tesla_to_house_load():
    flows = _prop(600.0, -250.0, 1000.0, 800.0, 5000.0)
    assert sum(flows[(s, L.TESLA)] for s in SOURCES) == pytest.approx(800.0, abs=1e-9)
    assert sum(flows[(s, L.HOUSE)] for s in SOURCES) == 0.0


def test_prop_loss_is_clamped_at_zero():
    """Outflow exceeding inflow (two AC/DC measurement paths disagreeing) must
    not make a negative loss."""
    flows = _prop(200.0, -300.0, 100.0, 900.0, 0.0)
    assert sum(flows[(s, L.INVERTER)] for s in SOURCES) == 0.0


# ==========================================================================
# 6. Polarity
# ==========================================================================
#
# Under proportional allocation a source with zero power contributes zero to
# every sink, so a polarity test with a zero source -- or with two equal
# sources -- is structurally incapable of catching a sign inversion: both
# polarities produce the same numbers and the test passes either way. This
# repository deliberately inverts two conventions at the HA boundary (grid is
# positive *importing* in HA but positive *exporting* at register 33257;
# battery is positive *charging* in HA, built from an unsigned magnitude plus
# the 33135 direction flag), and CLAUDE.md singles out sign inversion as the
# thing an agent helpfully flips the wrong way.
#
# So every test below drives all three sources non-zero and MUTUALLY UNEQUAL,
# and asserts an ordering that a flipped sign would reverse. The two cases
# where a source is necessarily zero -- exporting means the grid is not a
# source, charging means the battery is not a source -- say so explicitly, and
# there the zero itself is the assertion.

# Three unequal, non-zero magnitudes reused throughout: solar 1000 W, battery
# 400 W, grid 2500 W. No two are equal and no pair sums to the third, so every
# pairwise ordering below is a distinct discriminator.
POLARITY_SOLAR = 1000.0
POLARITY_BATTERY = 400.0
POLARITY_GRID = 2500.0


def test_grid_positive_is_importing():
    """HA's grid_power is positive IMPORTING. Flipped, this sample would read
    as a 2500 W export: Grid import would contribute nothing and solar would
    dump 1000 W into Grid export."""
    flows = _prop(POLARITY_SOLAR, -POLARITY_BATTERY, POLARITY_GRID, 3000.0, 0.0)
    assert sum(flows[(s, L.HOUSE)] for s in SOURCES) > 0.0
    assert flows[(L.SOLAR, L.EXPORT)] == 0.0
    # All three sources are live, and the ordering follows their magnitudes.
    assert (
        flows[(L.GRID_IN, L.HOUSE)]
        > flows[(L.SOLAR, L.HOUSE)]
        > flows[(L.BAT_OUT, L.HOUSE)]
        > 0.0
    )
    assert sum(flows[(L.GRID_IN, sink)] for sink in SINKS
               if (L.GRID_IN, sink) in flows) == pytest.approx(POLARITY_GRID, abs=1e-9)


def test_grid_negative_is_exporting():
    """The mirror. Grid import must be exactly zero here -- that zero IS the
    assertion, because a flipped sign would make it 1500 W and drive Grid
    export to zero. Solar and battery stay non-zero and unequal so the split
    between them is still discriminating."""
    flows = _prop(6000.0, -POLARITY_BATTERY, -1500.0, 3000.0, 0.0)
    assert flows[(L.SOLAR, L.EXPORT)] == pytest.approx(1500.0, abs=1e-9)
    for sink in SINKS:
        if (L.GRID_IN, sink) in flows:
            assert flows[(L.GRID_IN, sink)] == 0.0
    assert flows[(L.SOLAR, L.HOUSE)] > flows[(L.BAT_OUT, L.HOUSE)] > 0.0


def test_battery_negative_is_discharging():
    """HA's battery_power is positive CHARGING. Flipped, this 400 W discharge
    would appear as a 400 W charge: Battery out would go silent and Battery in
    would fill instead."""
    flows = _prop(POLARITY_SOLAR, -POLARITY_BATTERY, POLARITY_GRID, 3000.0, 0.0)
    out = sum(flows[(L.BAT_OUT, sink)] for sink in SINKS if (L.BAT_OUT, sink) in flows)
    into = sum(flows[(source, L.BAT_IN)] for source in SOURCES
               if (source, L.BAT_IN) in flows)
    assert out == pytest.approx(POLARITY_BATTERY, abs=1e-9)
    assert into == 0.0
    # Non-zero and unequal on all three, so the shares cannot coincide.
    assert (
        sum(flows[(L.GRID_IN, s)] for s in SINKS if (L.GRID_IN, s) in flows)
        > sum(flows[(L.SOLAR, s)] for s in SINKS if (L.SOLAR, s) in flows)
        > out
        > 0.0
    )


def test_battery_positive_is_charging():
    """The mirror. Battery out must be exactly zero -- a battery cannot charge
    and discharge in the same sample -- and the 1200 W lands in Battery in,
    split between solar and grid in their unequal proportions."""
    flows = _prop(6000.0, 1200.0, POLARITY_GRID, 3000.0, 0.0)
    out = sum(flows[(L.BAT_OUT, sink)] for sink in SINKS if (L.BAT_OUT, sink) in flows)
    assert out == 0.0
    assert sum(flows[(source, L.BAT_IN)] for source in SOURCES
               if (source, L.BAT_IN) in flows) == pytest.approx(1200.0, abs=1e-9)
    assert flows[(L.SOLAR, L.BAT_IN)] > flows[(L.GRID_IN, L.BAT_IN)] > 0.0


def test_export_is_solar_only_with_all_three_sources_live():
    """Export is structurally solar-only. Battery is discharging and the grid
    cannot be both importing and exporting, so this drives solar, battery and
    a small import together and shows the export flow tracking solar alone."""
    exporting = _prop(4000.0, -POLARITY_BATTERY, -3000.0, 900.0, 0.0)
    assert exporting[(L.SOLAR, L.EXPORT)] == pytest.approx(3000.0, abs=1e-9)
    importing = _prop(POLARITY_SOLAR, -POLARITY_BATTERY, POLARITY_GRID, 900.0, 0.0)
    assert importing[(L.SOLAR, L.EXPORT)] == 0.0
    assert not any(
        source != L.SOLAR for source, sink in L.SOURCE_SINK_LINKS if sink == L.EXPORT
    )


def test_polarity_survives_the_card():
    """The same three unequal sources through the allocator, so the assertion
    is about drawn ribbons and not only about the reference decomposer."""
    samples = [(3600.0, POLARITY_SOLAR, -POLARITY_BATTERY, POLARITY_GRID, 3000.0, 0.0)]
    states, flows, counters = _states_for(samples)
    result = _alloc(states)
    assert counters[L.SOLAR] != counters[L.BAT_OUT] != counters[L.GRID_IN]
    assert result.state(L.GRID_IN, L.HOUSE) > result.state(L.SOLAR, L.HOUSE) > \
        result.state(L.BAT_OUT, L.HOUSE) > 0.0
    assert result.filled.get(L.EXPORT, 0.0) == 0.0
    assert result.filled.get(L.BAT_IN, 0.0) == 0.0
    assert result.spent[L.BAT_OUT] == pytest.approx(flows[(L.BAT_OUT, L.HOUSE)]
                                                    + flows[(L.BAT_OUT, L.TESLA)]
                                                    + flows[(L.BAT_OUT, L.INVERTER)],
                                                    abs=1e-9)
