"""End-to-end: does the chart DRAW the truth?

Composes the three v2 pieces for the exact configuration that will ship:

    flows.decompose()   the physics  -- five readings to twelve directed flows
    layout_v2           the graph    -- 8 nodes, 12 links, 2 sections
    allocator.allocate  the card     -- a port of ha-sankey-chart 6.3.0

Nothing here touches Home Assistant, the network, or the live dashboard; it is
a pure offline simulation.

The layout is IMPORTED from layout_v2 rather than restated. That is deliberate:
a copy of the node and link lists in this file could agree with itself forever
while drifting from the thing actually deployed, and BRIEF_V2.md section 7 asks
for a test that fails if any link's value, source, target or section span
changes. Importing is what makes that true.

What v2 changed, and why this file was rewritten rather than patched
-------------------------------------------------------------------
v1 had six source->sink links and three of the source flows carried no `value`,
so the card fell back to greedy allocation and the drawn ribbons depended on
node and link order. The headline bug was grid->battery resolving to exactly
0.0 and rendering as an invisible ribbon, indistinguishable from a link that
was never declared.

v2 gives all twelve source->sink links a `value:` pointing at a per-flow daily
utility_meter, so the card resolves

    min(parent_remainder, child_remainder, value_state)

and order sensitivity collapses. The greedy-contrast tests are kept, because
they are what make the value-carrying tests mean something: a test that the new
scheme is order-independent is worthless without a demonstration that the old
one was not.
"""
import itertools
import math
import random

import pytest

import layout_v2 as layout
from flows import FLOWS, decompose, node_totals

allocator = pytest.importorskip(
    "allocator",
    reason="allocator.py is owned by another agent and was not present",
)
allocate = allocator.allocate


# --------------------------------------------------------------------------
# Node ids, taken from the layout module so a rename cannot desync this file.
# --------------------------------------------------------------------------
SOLAR = layout.SOLAR
BAT_OUT = layout.BAT_OUT
GRID_IN = layout.GRID_IN
BAT_IN = layout.BAT_IN
EXPORT = layout.EXPORT
HOUSE = layout.HOUSE
TESLA = layout.TESLA
INVERTER = layout.INVERTER

SOURCE_NODE = {"solar": SOLAR, "battery": BAT_OUT, "grid": GRID_IN}
# NOTE the deliberate asymmetry, and do not "tidy" it away. flows.py names the
# export sink `export`; layout_v2 slugs it `grid`, because the meter it feeds is
# `sensor.flow_solar_to_grid_daily` and the node is the grid-export counter.
# Both are right inside their own module. This table is written out longhand
# rather than reusing layout_v2._SINK_SLUG precisely so that it is an
# INDEPENDENT statement of the correspondence -- reusing the private dict would
# make the mapping test below agree with itself and prove nothing.
SINK_NODE = {"house": HOUSE, "tesla": TESLA, "battery": BAT_IN,
             "export": EXPORT, "inverter": INVERTER}

# (source node, sink node) -> the flows.py key that link carries.
LINK_FLOW_KEY = {
    (SOURCE_NODE[s], SINK_NODE[t]): "%s_to_%s" % (s, t)
    for s in SOURCE_NODE for t in SINK_NODE
    if "%s_to_%s" % (s, t) in FLOWS
}


def flow_key(source, sink):
    return LINK_FLOW_KEY[(source, sink)]


COUNTER_NODES = (SOLAR, BAT_OUT, GRID_IN, BAT_IN, EXPORT)


def pairs_from(flows):
    """{flow key: kWh} -> {(source node, sink node): kWh}, the shape the layout
    wants. Derived from the FLOWS key names so a new flow cannot be silently
    dropped on the floor here."""
    out = {}
    for key, value in flows.items():
        source, sink = key.split("_to_")
        out[(SOURCE_NODE[source], SINK_NODE[sink])] = value
    return out


def resolve(states, nodes=None, links=None):
    """Run the allocator and return {(parent, child): state}."""
    built_nodes, built_links, _ = layout.build()
    result = allocate(
        list(nodes if nodes is not None else built_nodes),
        list(links if links is not None else built_links),
        dict(states),
    )
    return {(c["parent"], c["child"]): float(c["state"]) for c in result}


def greedy_links():
    """The same links with every `value` stripped -- i.e. how v1 drew."""
    _n, links, _s = layout.build()
    for link in links:
        link.pop("value", None)
    return links


def states_for(counters, flows):
    return layout.states_from(dict(counters), pairs_from(flows))


# --------------------------------------------------------------------------
# A simulated day, so the flow meters are genuinely produced by decompose()
# rather than hand-written.
#
# Two profiles on purpose:
#   BALANCED_PROFILE derives grid from the instantaneous balance, so every
#       sample closes exactly and the Inverter node is zero. That isolates the
#       card: any discrepancy is the allocator, not the physics.
#   LOSSY_PROFILE holds back a few per cent, so the Inverter node is non-zero
#       and its ribbons are actually drawn. Without it the five Inverter links
#       would be exercised only at zero, which is the kind of coverage that
#       looks complete and proves nothing.
# --------------------------------------------------------------------------

# (hours, solar W, battery W (+charging), house W incl. car, tesla W)
BALANCED_PROFILE = (
    (5.0, 0.0, 2500.0, 700.0, 0.0),        # 23:30-05:30 timed grid charge
    (1.0, 0.0, 0.0, 650.0, 0.0),           # pre-dawn
    (2.0, 900.0, 400.0, 800.0, 0.0),       # morning ramp
    (3.0, 3800.0, 1200.0, 900.0, 0.0),     # late morning
    (4.0, 5200.0, 400.0, 1100.0, 0.0),     # midday export
    (2.0, 2600.0, 0.0, 10400.0, 7000.0),   # afternoon, car on a 7 kW slot
    (3.0, 400.0, -1200.0, 2100.0, 0.0),    # evening, battery covers load
    (4.0, 0.0, -300.0, 900.0, 0.0),        # night
)

# The same day with ~106 W of fixed parasitic held back on every sample -- the
# figure CLAUDE.md's flow-law work fitted, and the one the Inverter node exists
# to display.
PARASITIC_W = 106.0


def simulate(profile, parasitic=0.0):
    """Integrate a profile to daily kWh: node counters and per-flow meters.

    `grid` is derived so the AC side balances, minus any parasitic held back.
    Both sides come from the same samples, so this is the honest end-to-end
    case rather than two independently invented number sets.
    """
    counters = {node: 0.0 for node in COUNTER_NODES}
    flows = {key: 0.0 for key in FLOWS}
    for hours, solar, battery, house, tesla in profile:
        grid = house + battery - solar + parasitic
        counters[SOLAR] += solar * hours / 1000.0
        counters[BAT_IN] += max(0.0, battery) * hours / 1000.0
        counters[BAT_OUT] += max(0.0, -battery) * hours / 1000.0
        counters[GRID_IN] += max(0.0, grid) * hours / 1000.0
        counters[EXPORT] += max(0.0, -grid) * hours / 1000.0
        for key, watts in decompose(solar, battery, grid, house, tesla).items():
            flows[key] += watts * hours / 1000.0
    return counters, flows


SIM_COUNTERS, SIM_FLOWS = simulate(BALANCED_PROFILE)
SIM_STATES = states_for(SIM_COUNTERS, SIM_FLOWS)

LOSSY_COUNTERS, LOSSY_FLOWS = simulate(BALANCED_PROFILE, PARASITIC_W)
LOSSY_STATES = states_for(LOSSY_COUNTERS, LOSSY_FLOWS)

# The node states the card should derive for the three add_entities nodes.
SIM_NODE_TOTALS = node_totals(SIM_FLOWS)
LOSSY_NODE_TOTALS = node_totals(LOSSY_FLOWS)


# ==========================================================================
# 1. The layout itself -- BRIEF_V2.md section 7's per-link and per-node gate
# ==========================================================================

def test_the_layout_is_twelve_links_and_nothing_else():
    """BRIEF_V2.md section 1 says 16. It was 15, then 14 when "Stored" went on
    2026-08-30, and is now 12 -- the device split went the same day and every
    remaining link is a measured source->sink flow. Sections 4 and 7 of the
    brief always said 12 source->sink; the 16 was an off-by-one in section 1."""
    assert len(layout.SOURCE_SINK_LINKS) == 12
    assert len(layout.LINKS) == 12
    assert len(layout.FLOW_METERS) == 12


def test_every_flow_key_has_exactly_one_link_and_vice_versa():
    """The physics and the picture must enumerate the same twelve flows. If
    flows.py gains a key and the layout does not, the ribbon is never drawn and
    nothing else in this file would notice."""
    assert set(LINK_FLOW_KEY.values()) == set(FLOWS)
    assert len(LINK_FLOW_KEY) == 12
    declared = {tuple(p) for p in layout.SOURCE_SINK_LINKS}
    assert declared == set(LINK_FLOW_KEY)


def test_every_source_sink_link_carries_its_own_meter_as_value():
    """Per BRIEF_V2.md section 4 the key is literally `value` and holds an
    entity id. Without it the card falls back to greedy and the whole exercise
    is undone."""
    for link in layout.LINKS:
        pair = (link["source"], link["target"])
        assert pair in [tuple(p) for p in layout.SOURCE_SINK_LINKS]
        assert link["value"] == layout.flow_meter(*pair)


def test_no_link_is_left_to_greedy_allocation():
    """There are no structural links any more, so there is nowhere for the
    card's declaration-order fill to show through."""
    assert all("value" in link for link in layout.LINKS)


def test_meter_names_follow_the_documented_scheme():
    """`sensor.flow_<source>_to_<sink>_daily`, where battery means discharge on
    the source side and charge on the sink side. 36 HA helpers hang off this
    naming; a silent change orphans all of them."""
    assert layout.flow_meter(SOLAR, HOUSE) == "sensor.flow_solar_to_house_daily"
    assert layout.flow_meter(BAT_OUT, TESLA) == "sensor.flow_battery_to_tesla_daily"
    assert layout.flow_meter(GRID_IN, BAT_IN) == "sensor.flow_grid_to_battery_daily"
    assert layout.flow_meter(SOLAR, EXPORT) == "sensor.flow_solar_to_grid_daily"
    assert len(set(layout.FLOW_METERS)) == 12


def test_no_battery_to_export_link_is_declared():
    """All three discharge windows are unset and must stay unset (CLAUDE.md).
    Merely DECLARING this link fabricated battery export on the real chart."""
    assert (BAT_OUT, EXPORT) not in [tuple(p) for p in layout.SOURCE_SINK_LINKS]
    assert not any(l["source"] == BAT_OUT and l["target"] == EXPORT
                   for l in layout.LINKS)
    assert not any(f.startswith("battery_to_") and f.endswith("_export")
                   for f in FLOWS)


def test_no_grid_to_export_link_is_declared():
    """Import and export never coexist on one signed meter reading."""
    assert not any(l["source"] == GRID_IN and l["target"] == EXPORT
                   for l in layout.LINKS)


def test_house_no_longer_feeds_tesla():
    """The v2 split: Tesla is a peer of House in section 1, fed directly by the
    three sources, not a child of House. If both existed the car would be
    double-counted."""
    assert not any(l["source"] == HOUSE and l["target"] == TESLA
                   for l in layout.LINKS)
    assert layout.sections_of()[TESLA] == 1


def test_house_tesla_and_inverter_are_the_sum_of_their_inbound_meters():
    """The v2 node-identity decision (BRIEF_V2.md section 1). It is what makes
    House exclude the Tesla without a subtract_entities trick, and it is why
    House is a slug rather than house_consumption_today."""
    by_id = {n["id"]: n for n in layout.NODES}
    for node_id, source_nodes in (
        (HOUSE, (SOLAR, BAT_OUT, GRID_IN)),
        (TESLA, (SOLAR, BAT_OUT, GRID_IN)),
        (INVERTER, (SOLAR, BAT_OUT, GRID_IN)),
    ):
        node = by_id[node_id]
        wanted = [layout.flow_meter(s, node_id) for s in source_nodes]
        assert [node["entity_id"]] + node["add_entities"] == wanted
        assert "subtract_entities" not in node


def test_the_counter_nodes_keep_their_inverter_counter_identity():
    """Solar, Battery out/in and Grid import/export stay on the inverter's own
    daily counters -- they are measured, and re-deriving them from the flow
    meters would throw away the only independent check the chart has."""
    by_id = {n["id"]: n for n in layout.NODES}
    for node_id in COUNTER_NODES:
        assert "add_entities" not in by_id[node_id]
        assert "entity_id" not in by_id[node_id]


@pytest.mark.parametrize("node_id,section", [
    (SOLAR, 0), (BAT_OUT, 0), (GRID_IN, 0),
    (HOUSE, 1), (TESLA, 1), (BAT_IN, 1), (EXPORT, 1), (INVERTER, 1),
])
def test_each_node_sits_in_its_intended_section(node_id, section):
    assert layout.sections_of()[node_id] == section


def test_two_sections_exactly_as_designed():
    """Both second-hop nodes went on 2026-08-30 -- "Stored", then House's
    Air con / Rest of house split. Dropping the split is what makes every sink
    a terminus, so all five sit at the same x as each other."""
    assert sorted({n["section"] for n in layout.NODES}) == [0, 1]
    assert len(layout.SECTIONS) == 2
    for node in layout.NODES:
        assert node.get("type", "entity") == "entity", node["id"]


def test_every_sink_is_a_terminus():
    sinks = {l["target"] for l in layout.LINKS}
    assert sinks == {HOUSE, TESLA, BAT_IN, EXPORT, INVERTER}
    for sink in sinks:
        assert [l for l in layout.LINKS if l["source"] == sink] == []


# ==========================================================================
# 2. Section spans and ghost synthesis
# ==========================================================================

def test_every_live_link_spans_exactly_one_section():
    """CLAUDE.md: a link spanning more than one section makes the card
    synthesise an unlabelled ghost box coloured like its target -- the thing
    that made the battery appear to charge itself. sankey_fix.py aborts on it;
    so does the layout's own guard."""
    assert layout.check_spans() == []


def test_the_greedy_configuration_also_spans_one_section():
    assert layout.check_spans(links=greedy_links()) == []


def test_a_mis_sectioned_link_is_detected():
    """Battery in moved back to its own section 2 -- the 2026-08-28 ghost."""
    broken = [dict(n, section=2) if n["id"] == BAT_IN else dict(n)
              for n in layout.NODES]
    bad = layout.check_spans(nodes=broken)
    assert (SOLAR, BAT_IN, 2) in bad
    assert (GRID_IN, BAT_IN, 2) in bad


def test_a_zero_span_link_is_detected():
    links = list(layout.LINKS) + [{"source": SOLAR, "target": GRID_IN}]
    assert (SOLAR, GRID_IN, 0) in layout.check_spans(links=links)


def test_a_backwards_link_is_detected():
    links = list(layout.LINKS) + [{"source": HOUSE, "target": SOLAR}]
    assert (HOUSE, SOLAR, -1) in layout.check_spans(links=links)


def test_the_card_synthesises_no_ghost_boxes_for_the_live_config():
    """The allocator's own passthrough synthesis must never fire."""
    nodes, links, _ = layout.build()
    result = allocate(nodes, links, dict(SIM_STATES))
    assert result.ghost_nodes == []
    assert result.warnings == []
    assert not result.has_ghosts


def test_the_card_would_synthesise_ghosts_for_a_mis_sectioned_battery():
    """Reproduces the 2026-08-28 unlabelled green box beside Battery out. This
    is the contrast that makes the previous test meaningful."""
    nodes, links, _ = layout.build()
    broken = [dict(n, section=2) if n["id"] == BAT_IN else dict(n) for n in nodes]
    result = allocate(broken, links, dict(SIM_STATES))
    assert result.has_ghosts
    assert any(w["kind"] == "ghost_passthrough" for w in result.warnings)


def test_ghost_synthesis_does_not_change_the_arithmetic():
    """CLAUDE.md: passthroughs preserve the real parent/child. Only the picture
    lies; the numbers do not."""
    nodes, links, _ = layout.build()
    broken = [dict(n, section=2) if n["id"] == BAT_IN else dict(n) for n in nodes]
    result = allocate(broken, links, dict(SIM_STATES))
    got = {(c["parent"], c["child"]): c["state"] for c in result}
    assert got[(SOLAR, BAT_IN)] == pytest.approx(SIM_FLOWS["solar_to_battery"],
                                                 abs=1e-9)
    assert got[(GRID_IN, BAT_IN)] == pytest.approx(SIM_FLOWS["grid_to_battery"],
                                                   abs=1e-9)


# ==========================================================================
# 3. The chart draws the decomposition exactly
# ==========================================================================

def test_chart_draws_every_decomposed_flow_exactly():
    """THE headline. All twelve ribbons resolve to their own meter."""
    got = resolve(SIM_STATES)
    for source, sink in layout.SOURCE_SINK_LINKS:
        key = flow_key(source, sink)
        assert got[(source, sink)] == pytest.approx(SIM_FLOWS[key], abs=1e-9), key


def test_chart_draws_the_lossy_day_exactly_including_the_inverter_ribbons():
    """The Inverter links must be exercised at a non-zero value, or their
    coverage is a lie."""
    got = resolve(LOSSY_STATES)
    for source, sink in layout.SOURCE_SINK_LINKS:
        key = flow_key(source, sink)
        assert got[(source, sink)] == pytest.approx(LOSSY_FLOWS[key], abs=1e-9), key
    drawn_loss = sum(got[(s, INVERTER)] for s in (SOLAR, BAT_OUT, GRID_IN))
    assert drawn_loss > 2.0, "the lossy profile must actually load the Inverter node"
    assert drawn_loss == pytest.approx(LOSSY_NODE_TOTALS["inverter"], abs=1e-9)


def test_decomposed_flows_close_against_every_simulated_counter():
    """Every sink is exactly the sum of its inbound flows, and on this
    internally consistent profile every source is exactly its own counter too."""
    assert SIM_NODE_TOTALS["export"] == pytest.approx(SIM_COUNTERS[EXPORT], abs=1e-9)
    assert SIM_NODE_TOTALS["battery_in"] == pytest.approx(SIM_COUNTERS[BAT_IN], abs=1e-9)
    assert SIM_NODE_TOTALS["solar_spent"] == pytest.approx(SIM_COUNTERS[SOLAR], abs=1e-9)
    assert SIM_NODE_TOTALS["battery_spent"] == pytest.approx(SIM_COUNTERS[BAT_OUT], abs=1e-9)
    assert SIM_NODE_TOTALS["grid_spent"] == pytest.approx(SIM_COUNTERS[GRID_IN], abs=1e-9)


def test_the_lossy_day_still_closes_on_every_sink_and_source():
    """Adding a parasitic must not break closure -- it must move into Inverter.
    That is the entire claim of BRIEF_V2.md section 8."""
    assert LOSSY_NODE_TOTALS["export"] == pytest.approx(LOSSY_COUNTERS[EXPORT], abs=1e-9)
    assert LOSSY_NODE_TOTALS["battery_in"] == pytest.approx(LOSSY_COUNTERS[BAT_IN], abs=1e-9)
    assert LOSSY_NODE_TOTALS["solar_spent"] == pytest.approx(LOSSY_COUNTERS[SOLAR], abs=1e-9)
    assert LOSSY_NODE_TOTALS["grid_spent"] == pytest.approx(LOSSY_COUNTERS[GRID_IN], abs=1e-9)
    assert LOSSY_NODE_TOTALS["inverter"] > 0.0


def test_the_energy_balance_of_the_simulated_day_closes():
    supply = SIM_COUNTERS[SOLAR] + SIM_COUNTERS[GRID_IN] + SIM_COUNTERS[BAT_OUT]
    sink = SIM_COUNTERS[EXPORT] + SIM_COUNTERS[BAT_IN] + SIM_NODE_TOTALS["house"] \
        + SIM_NODE_TOTALS["tesla"] + SIM_NODE_TOTALS["inverter"]
    assert supply == pytest.approx(sink, abs=1e-9)


def test_simulated_day_has_a_real_grid_to_battery_flow():
    """The v1 headline bug in one line: this ribbon used to resolve to 0.0 and
    render invisible."""
    assert SIM_FLOWS["grid_to_battery"] > 0.0
    assert resolve(SIM_STATES)[(GRID_IN, BAT_IN)] > 0.0


def test_simulated_day_has_a_real_grid_to_tesla_flow():
    """The owner's actual question -- "the tesla ribbon should know how much of
    grid/battery it took" -- answered on the chart."""
    assert SIM_FLOWS["grid_to_tesla"] > 0.0
    assert resolve(SIM_STATES)[(GRID_IN, TESLA)] > 0.0


def test_the_tesla_ribbon_reports_its_own_source_mix():
    got = resolve(SIM_STATES)
    drawn = {s: got[(s, TESLA)] for s in (SOLAR, BAT_OUT, GRID_IN)}
    assert sum(drawn.values()) == pytest.approx(SIM_NODE_TOTALS["tesla"], abs=1e-9)
    assert drawn[SOLAR] > 0.0 and drawn[GRID_IN] > 0.0


def test_all_decomposed_flows_are_non_negative():
    assert all(v >= 0.0 for v in SIM_FLOWS.values())
    assert all(v >= 0.0 for v in LOSSY_FLOWS.values())


def test_instantaneous_decomposition_matches_the_hourly_integration():
    total = 0.0
    for hours, solar, battery, house, tesla in BALANCED_PROFILE:
        grid = house + battery - solar
        total += sum(decompose(solar, battery, grid, house, tesla).values()) * hours / 1000.0
    assert total == pytest.approx(sum(SIM_FLOWS.values()), abs=1e-9)


# ==========================================================================
# 4. The greedy contrast -- why the `value` keys are there at all
# ==========================================================================

def test_greedy_misdraws_the_simulated_day():
    """Without values the card allocates min(parent_rem, child_rem) in
    declaration order, and the picture stops matching the physics."""
    got = resolve(SIM_STATES, links=greedy_links())
    mismatched = [
        (s, t) for s, t in layout.SOURCE_SINK_LINKS
        if not math.isclose(got[(s, t)], SIM_FLOWS[flow_key(s, t)], abs_tol=1e-6)
    ]
    assert mismatched, "greedy happened to be right; pick a harder profile"


def test_greedy_strands_a_ribbon_that_values_rescue():
    """The 2026-08-28 scenario, in v2 shape: Battery out declared after Grid
    import loses its only possible ribbon entirely."""
    counters = {SOLAR: 2.7, BAT_OUT: 1.2, GRID_IN: 5.4, BAT_IN: 4.8, EXPORT: 0.5}
    flows = {k: 0.0 for k in FLOWS}
    flows.update({"solar_to_export": 0.5, "solar_to_battery": 1.4,
                  "solar_to_house": 0.8, "grid_to_house": 2.2,
                  "grid_to_battery": 3.2, "battery_to_house": 1.2})
    states = states_for(counters, flows)

    nodes, links, _ = layout.build()
    reordered = ([n for n in nodes if n["id"] == SOLAR]
                 + [n for n in nodes if n["id"] == GRID_IN]
                 + [n for n in nodes if n["id"] == BAT_OUT]
                 + [n for n in nodes if n["section"] != 0])
    stranded = resolve(states, nodes=reordered, links=greedy_links())
    assert stranded[(BAT_OUT, HOUSE)] == 0.0

    rescued = resolve(states, nodes=reordered)
    assert rescued[(BAT_OUT, HOUSE)] == pytest.approx(1.2, abs=1e-9)


def test_no_ribbon_is_stranded_on_the_simulated_day():
    got = resolve(SIM_STATES)
    for source, sink in layout.SOURCE_SINK_LINKS:
        key = flow_key(source, sink)
        if SIM_FLOWS[key] > 0.0:
            assert got[(source, sink)] > 0.0, "%s stranded" % key


def test_v1_style_attribution_did_not_close_but_v2_does():
    """Kept from v1, where it documented a defect; now it documents the fix.

    v1's briefed attribution over-summed the grid counter by 0.4 kWh and left
    solar 0.4 kWh short, because the DC and AC sensors cannot be reconciled and
    v1 had nowhere to park the difference. That non-closure is what made link
    order matter. v2 has the Inverter node, so the same measurement closes.
    """
    v1_solar = 11.6 + 2.4 + 7.1
    v1_grid = 25.7 + 3.4
    assert v1_solar == pytest.approx(21.1, abs=1e-9)   # counter said 21.5
    assert v1_grid == pytest.approx(29.1, abs=1e-9)    # counter said 28.7
    assert v1_grid > 28.7

    # v2, same shape of day, closes on every source and every sink.
    assert LOSSY_NODE_TOTALS["grid_spent"] == pytest.approx(
        LOSSY_COUNTERS[GRID_IN], abs=1e-9)
    assert LOSSY_NODE_TOTALS["solar_spent"] == pytest.approx(
        LOSSY_COUNTERS[SOLAR], abs=1e-9)


# ==========================================================================
# 5. Clamping is safe in the right direction
# ==========================================================================

def test_overstated_flow_is_clamped_and_fabricates_nothing():
    flows = dict(SIM_FLOWS)
    flows["solar_to_house"] = 999.0
    got = resolve(states_for(SIM_COUNTERS, flows))
    spent = sum(v for (p, _), v in got.items() if p == SOLAR)
    assert spent <= SIM_COUNTERS[SOLAR] + 1e-9


def test_overstating_never_exceeds_the_parent_counter():
    flows = {key: 100.0 for key in FLOWS}
    got = resolve(states_for(SIM_COUNTERS, flows))
    for node in (SOLAR, BAT_OUT, GRID_IN):
        spent = sum(v for (p, _), v in got.items() if p == node)
        assert spent <= SIM_COUNTERS[node] + 1e-9


def test_overstating_never_exceeds_the_child_counter():
    """Only the counter-backed sinks can be checked this way: House, Tesla and
    Inverter are DEFINED as the sum of their inbound meters, so overstating a
    meter enlarges the node too and there is nothing to clamp against. That is
    a real property of the v2 node identity, not an omission."""
    flows = {key: 100.0 for key in FLOWS}
    got = resolve(states_for(SIM_COUNTERS, flows))
    for node in (BAT_IN, EXPORT):
        filled = sum(v for (_, c), v in got.items() if c == node)
        assert filled <= SIM_COUNTERS[node] + 1e-9


def test_understated_flow_shrinks_the_ribbon_quantifiably():
    """The dangerous direction: the picture simply lies smaller."""
    flows = dict(SIM_FLOWS)
    true = flows["grid_to_battery"]
    flows["grid_to_battery"] = 1.0
    got = resolve(states_for(SIM_COUNTERS, flows))
    assert got[(GRID_IN, BAT_IN)] == pytest.approx(1.0, abs=1e-9)
    assert got[(GRID_IN, BAT_IN)] < true


def test_understating_to_zero_strands_the_ribbon_completely():
    """A zero-valued flow meter recreates exactly the bug v2 exists to fix."""
    flows = dict(SIM_FLOWS)
    flows["grid_to_battery"] = 0.0
    got = resolve(states_for(SIM_COUNTERS, flows))
    assert got[(GRID_IN, BAT_IN)] == 0.0


def test_clamp_is_the_minimum_of_all_three_terms_everywhere():
    """Recomputes the card's rule independently, in the card's own order."""
    got = resolve(SIM_STATES)
    spent, filled = {}, {}
    node_state = dict(SIM_STATES)
    node_state[HOUSE] = SIM_NODE_TOTALS["house"]
    node_state[TESLA] = SIM_NODE_TOTALS["tesla"]
    node_state[INVERTER] = SIM_NODE_TOTALS["inverter"]
    for source, sink in layout.SOURCE_SINK_LINKS:
        key = flow_key(source, sink)
        p_rem = max(0.0, node_state[source] - spent.get(source, 0.0))
        c_rem = max(0.0, node_state[sink] - filled.get(sink, 0.0))
        expected = min(p_rem, c_rem, SIM_FLOWS[key]) if p_rem and c_rem else 0.0
        assert got[(source, sink)] == pytest.approx(expected, abs=1e-9), key
        spent[source] = spent.get(source, 0.0) + expected
        filled[sink] = filled.get(sink, 0.0) + expected


def test_negative_flow_value_DOES_produce_a_negative_ribbon():
    """HAZARD, faithful to the card: `value` is not clamped at zero.

    Math.min(parent_rem, child_rem, -5) is -5. Unlike subtract_entities, which
    the bundle clamps with `r.state -= Math.min(i, r.state)`, a connection
    entity goes straight into Math.min. decompose() is non-negative by
    construction and flows.py's check_structure enforces it, so the guard is
    upstream -- but any flow sensor that can go negative will draw a negative
    ribbon.
    """
    flows = dict(SIM_FLOWS)
    flows["solar_to_house"] = -5.0
    got = resolve(states_for(SIM_COUNTERS, flows))
    assert got[(SOLAR, HOUSE)] == pytest.approx(-5.0, abs=1e-9)


def test_decompose_never_emits_a_negative_flow_so_the_hazard_stays_theoretical():
    for hours, solar, battery, house, tesla in BALANCED_PROFILE:
        grid = house + battery - solar
        assert all(v >= 0.0 for v in decompose(solar, battery, grid, house, tesla).values())


# ==========================================================================
# 6. Totals are untouched
# ==========================================================================

@pytest.mark.parametrize("node", list(COUNTER_NODES))
def test_counter_node_totals_are_untouched_by_allocation(node):
    states = dict(SIM_STATES)
    before = states[node]
    resolve(states)
    assert states[node] == before == SIM_COUNTERS[node]


def test_allocate_does_not_mutate_the_states_it_is_given():
    states = dict(SIM_STATES)
    snapshot = dict(states)
    resolve(states)
    assert states == snapshot


def test_allocate_does_not_mutate_the_layout_module():
    """layout.build() hands out deep copies precisely so the permutation tests
    below cannot corrupt the shipped configuration. Assert it actually does."""
    before_nodes = [dict(n) for n in layout.NODES]
    before_links = [dict(l) for l in layout.LINKS]
    nodes, links, _ = layout.build()
    nodes.reverse()
    for link in links:
        link.pop("value", None)
    allocate(nodes, links, dict(SIM_STATES))
    assert [dict(n) for n in layout.NODES] == before_nodes
    assert [dict(l) for l in layout.LINKS] == before_links


def test_flow_meters_never_appear_as_node_totals():
    """Flow meters are link constraints and node ingredients; they must not
    become boxes of their own."""
    got = resolve(SIM_STATES)
    drawn = {n for pair in got for n in pair}
    assert not (drawn & set(layout.FLOW_METERS))


# ==========================================================================
# 7. The sinks
#
# House's Air con / Rest of house split lived here until 2026-08-30. It went
# with the third column, so the only claims left are about the sinks
# themselves. The car is no longer hidden inside House either way: it has been
# a peer of House since v2, and House now reads house-only.
# ==========================================================================

def test_house_reads_house_only_and_excludes_the_car():
    got = resolve(SIM_STATES)
    assert SIM_NODE_TOTALS["tesla"] > 0.0
    house = sum(got[(source, HOUSE)] for source in (SOLAR, BAT_OUT, GRID_IN))
    assert house == pytest.approx(SIM_NODE_TOTALS["house"], abs=1e-6)
    v1_style = SIM_NODE_TOTALS["house"] + SIM_NODE_TOTALS["tesla"]
    assert house == pytest.approx(v1_style - SIM_NODE_TOTALS["tesla"], abs=1e-6)


def test_battery_in_fills_exactly_from_its_two_sources():
    """BRIEF_V2.md section 7 asks for this by name. It used to be asserted one
    hop further along, at "Stored"; that node is gone, so it is asserted at
    Battery in itself."""
    got = resolve(SIM_STATES)
    fed = got[(SOLAR, BAT_IN)] + got[(GRID_IN, BAT_IN)]
    assert fed == pytest.approx(SIM_COUNTERS[BAT_IN], abs=1e-9)


# ==========================================================================
# 8. Degenerate days
# ==========================================================================

ZERO_COUNTERS = {node: 0.0 for node in COUNTER_NODES}
ZERO_FLOWS = {key: 0.0 for key in FLOWS}


def test_all_zero_day_draws_nothing_and_does_not_crash():
    got = resolve(states_for(ZERO_COUNTERS, ZERO_FLOWS))
    assert all(v == 0.0 for v in got.values())


def test_all_zero_day_without_values_also_draws_nothing():
    got = resolve(states_for(ZERO_COUNTERS, ZERO_FLOWS),
                  links=greedy_links())
    assert all(v == 0.0 for v in got.values())


def test_solar_only_no_load_exports_everything():
    counters = dict(ZERO_COUNTERS, **{SOLAR: 10.0, EXPORT: 10.0})
    flows = dict(ZERO_FLOWS, solar_to_export=10.0)
    got = resolve(states_for(counters, flows))
    assert got[(SOLAR, EXPORT)] == pytest.approx(10.0, abs=1e-9)
    assert got[(SOLAR, HOUSE)] == 0.0
    assert got[(SOLAR, BAT_IN)] == 0.0
    assert got[(SOLAR, TESLA)] == 0.0


def test_deep_night_grid_charging_only():
    counters = dict(ZERO_COUNTERS, **{GRID_IN: 4.0, BAT_IN: 3.0})
    flows = dict(ZERO_FLOWS, grid_to_house=1.0, grid_to_battery=3.0)
    got = resolve(states_for(counters, flows))
    assert got[(GRID_IN, BAT_IN)] == pytest.approx(3.0, abs=1e-9)
    assert got[(GRID_IN, HOUSE)] == pytest.approx(1.0, abs=1e-9)


def test_a_day_with_no_solar_at_all():
    counters = dict(ZERO_COUNTERS, **{BAT_OUT: 2.0, GRID_IN: 10.0})
    flows = dict(ZERO_FLOWS, grid_to_house=10.0, battery_to_house=2.0)
    got = resolve(states_for(counters, flows))
    assert got[(SOLAR, EXPORT)] == 0.0
    assert got[(BAT_OUT, HOUSE)] == pytest.approx(2.0, abs=1e-9)
    assert got[(GRID_IN, HOUSE)] == pytest.approx(10.0, abs=1e-9)


def test_a_day_where_the_car_never_charges():
    """Every Tesla ribbon at zero, and the node with it -- without lying about
    anything else."""
    counters = dict(ZERO_COUNTERS, **{SOLAR: 10.0, GRID_IN: 2.0, BAT_IN: 3.0})
    flows = dict(ZERO_FLOWS, solar_to_house=7.0, solar_to_battery=3.0,
                 grid_to_house=2.0)
    got = resolve(states_for(counters, flows))
    for source in (SOLAR, BAT_OUT, GRID_IN):
        assert got[(source, TESLA)] == 0.0


def test_a_day_with_no_inverter_loss_leaves_those_ribbons_at_zero():
    got = resolve(SIM_STATES)
    assert SIM_NODE_TOTALS["inverter"] == pytest.approx(0.0, abs=1e-9)
    for source in (SOLAR, BAT_OUT, GRID_IN):
        assert got[(source, INVERTER)] == 0.0


def test_missing_flow_meter_strands_its_own_ribbon_but_nothing_else():
    """A meter that does not exist yet reads 0 under ignore_missing_entities,
    so its ribbon vanishes -- the failure v2 fixes, reintroduced by a typo in a
    sensor name. Everything else survives."""
    states = dict(SIM_STATES)
    del states[layout.flow_meter(GRID_IN, BAT_IN)]
    got = resolve(states)
    assert got[(GRID_IN, BAT_IN)] == 0.0
    assert got[(GRID_IN, HOUSE)] == pytest.approx(SIM_FLOWS["grid_to_house"], abs=1e-9)
    assert got[(SOLAR, EXPORT)] == pytest.approx(SIM_FLOWS["solar_to_export"], abs=1e-9)


def test_unavailable_meter_into_a_COUNTER_sink_poisons_just_that_ribbon():
    """HAZARD, faithful to the card. `unavailable`/None on a link value gives
    Number(...) -> NaN, and Math.min is NaN-poisoning, so the ribbon resolves to
    NaN -- not 0, and not a greedy fallback. It does not crash, and it
    fabricates nothing.

    This is the v1 behaviour, and it survives ONLY for the two sinks whose node
    state is still an inverter counter: Battery in and Grid export.
    """
    states = dict(LOSSY_STATES)
    states[layout.flow_meter(GRID_IN, BAT_IN)] = None
    got = resolve(states)
    assert math.isnan(got[(GRID_IN, BAT_IN)])


@pytest.mark.parametrize("bad", [None, "unavailable", "unknown"])
def test_unavailable_meter_into_a_COUNTER_sink_zeroes_that_sources_later_links(bad):
    """The NaN lands in the parent's spend, so every link declared AFTER it on
    that source sees a NaN remainder, which is JS-falsy, and resolves to a hard
    0. Grid's declaration order is tesla, house, battery, inverter -- so a bad
    grid->battery meter takes grid->inverter with it, and leaves the two
    declared before it untouched.

    With 36 helpers now in the chain this is likelier than it was with six, not
    less.
    """
    states = dict(LOSSY_STATES)
    states[layout.flow_meter(GRID_IN, BAT_IN)] = bad
    got = resolve(states)
    assert LOSSY_FLOWS["grid_to_inverter"] > 0.0, "need a non-zero later link"
    assert got[(GRID_IN, INVERTER)] == 0.0
    assert got[(GRID_IN, TESLA)] == pytest.approx(LOSSY_FLOWS["grid_to_tesla"], abs=1e-9)
    assert got[(GRID_IN, HOUSE)] == pytest.approx(LOSSY_FLOWS["grid_to_house"], abs=1e-9)


@pytest.mark.parametrize("bad", [None, "unavailable", "unknown"])
def test_unavailable_meter_into_a_DERIVED_sink_collapses_the_whole_node(bad):
    """NEW IN v2, and a materially different failure mode. Report it as such.

    House, Tesla and Inverter are DEFINED as the sum of their inbound meters
    (`entity_id` + `add_entities`), so an unavailable meter does not merely
    poison one ribbon -- it makes the NODE's own state NaN. The card treats a
    NaN endpoint as falsy, so the entire node collapses: all three inbound
    ribbons AND everything it feeds in section 2 go to zero, and no NaN ribbon
    is drawn at all.

    So under v1 a bad meter cost you the rest of ONE SOURCE's ribbons; under v2
    it costs you a whole SINK -- all three of its inbound ribbons at once. That
    is more visible (a box vanishes rather than a ribbon quietly shrinking),
    which is the better direction to fail in, but it is a bigger blast radius
    and it is worth knowing before deployment.
    """
    states = dict(SIM_STATES)
    states[layout.flow_meter(GRID_IN, HOUSE)] = bad
    got = resolve(states)
    for source in (SOLAR, BAT_OUT, GRID_IN):
        assert got[(source, HOUSE)] == 0.0
    assert not any(math.isnan(v) for v in got.values())


def test_unavailable_flow_meter_does_not_touch_other_sources():
    """In both failure modes, a source with no bad meter of its own is intact."""
    for bad_link, states_in in (((GRID_IN, HOUSE), SIM_STATES),
                                ((GRID_IN, BAT_IN), LOSSY_STATES)):
        states = dict(states_in)
        flows = SIM_FLOWS if states_in is SIM_STATES else LOSSY_FLOWS
        states[layout.flow_meter(*bad_link)] = "unavailable"
        got = resolve(states)
        assert got[(SOLAR, EXPORT)] == pytest.approx(flows["solar_to_export"], abs=1e-9)
        assert got[(SOLAR, BAT_IN)] == pytest.approx(flows["solar_to_battery"], abs=1e-9)


def test_unavailable_node_state_zeroes_rather_than_poisons():
    """The asymmetry, straight from the bundle: `if (m && y)` treats a NaN
    endpoint remainder as falsy, so an unavailable NODE gives 0, while an
    unavailable link VALUE gives NaN."""
    states = dict(SIM_STATES)
    states[BAT_OUT] = "unavailable"
    got = resolve(states)
    assert got[(BAT_OUT, HOUSE)] == 0.0


def test_missing_node_state_does_not_fabricate_energy():
    states = dict(SIM_STATES)
    states[BAT_OUT] = 0.0
    got = resolve(states)
    assert got[(BAT_OUT, HOUSE)] == 0.0


# ==========================================================================
# 9. Ordering independence -- the real proof the greedy artefact is gone
#
# SAMPLING NOTE, stated rather than hidden. v1 permuted six links exhaustively
# (6! = 720). Twelve links is 12! = 479 001 600, and combined with the node
# orders it is ~2.9e9, so exhaustive is not available. The replacement is
# strictly broader in kind rather than narrower:
#
#   - all 3! = 6 orders of the three source nodes, exhaustively;
#   - 300 seeded FULL shuffles of all 14 links, which reach orderings that a
#     within-source permutation never would (interleaving one source's links
#     with another's);
#   - and the two combined.
#
# The seed is fixed, so a failure is reproducible.
# ==========================================================================

LINK_SHUFFLE_SAMPLES = 300
SHUFFLE_SEED = 20260830


def _source_node_permutations():
    nodes, _l, _s = layout.build()
    head = [n for n in nodes if n["section"] == 0]
    tail = [n for n in nodes if n["section"] != 0]
    for perm in itertools.permutations(head):
        yield list(perm) + tail


def _shuffled_links(n=LINK_SHUFFLE_SAMPLES, seed=SHUFFLE_SEED):
    rng = random.Random(seed)
    for _ in range(n):
        _n, links, _s = layout.build()
        rng.shuffle(links)
        yield links


def test_node_order_permutations_are_stable():
    baseline = resolve(SIM_STATES)
    for nodes in _source_node_permutations():
        assert resolve(SIM_STATES, nodes=nodes) == pytest.approx(baseline, abs=1e-9)


def test_link_order_permutations_are_stable():
    baseline = resolve(SIM_STATES)
    for links in _shuffled_links():
        assert resolve(SIM_STATES, links=links) == pytest.approx(baseline, abs=1e-9)


def test_node_and_link_order_permuted_together_are_stable():
    baseline = resolve(SIM_STATES)
    for nodes in _source_node_permutations():
        for links in _shuffled_links(n=40):
            got = resolve(SIM_STATES, nodes=nodes, links=links)
            assert got == pytest.approx(baseline, abs=1e-9)


def test_order_independence_also_holds_on_the_lossy_day():
    """The Inverter ribbons are the newest links; they must be order-stable too."""
    baseline = resolve(LOSSY_STATES)
    for links in _shuffled_links(n=60):
        assert resolve(LOSSY_STATES, links=links) == pytest.approx(baseline, abs=1e-9)


def test_greedy_is_NOT_order_independent():
    """The contrast that makes the three previous tests meaningful. If this ever
    stops failing to vary, the permutation tests above have become vacuous."""
    results = set()
    for links in _shuffled_links(n=60):
        got = resolve(SIM_STATES, links=links)
        results.add(tuple(sorted((k, round(v, 6)) for k, v in got.items())))
    assert len(results) == 1, "values should have made this stable"

    greedy_results = set()
    for links in _shuffled_links(n=60):
        for link in links:
            link.pop("value", None)
        got = resolve(SIM_STATES, links=links)
        greedy_results.add(tuple(sorted((k, round(v, 6)) for k, v in got.items())))
    assert len(greedy_results) > 1, "greedy should have been order sensitive"


def test_greedy_is_node_order_insensitive_HERE_and_still_wrong():
    """A nuance worth pinning rather than papering over.

    Node order only matters to greedy when a source is over-supplied relative
    to the sinks it can reach. The balanced profile closes exactly, so greedy is
    stable under node permutation on it -- and still draws the wrong ribbons.
    Node order is not the only greedy artefact, and a test that only checked
    node-order stability would have passed v1 while v1 was visibly wrong.
    """
    results = set()
    for nodes in _source_node_permutations():
        got = resolve(SIM_STATES, nodes=nodes, links=greedy_links())
        results.add(tuple(sorted((k, round(v, 6)) for k, v in got.items())))
    assert len(results) == 1

    greedy = resolve(SIM_STATES, links=greedy_links())
    wrong = [(s_, t) for s_, t in layout.SOURCE_SINK_LINKS
             if not math.isclose(greedy[(s_, t)], SIM_FLOWS[flow_key(s_, t)],
                                 abs_tol=1e-6)]
    assert wrong, "stable AND correct would make this test pointless"


def test_greedy_node_order_changes_the_story_when_a_source_is_over_supplied():
    """The 2026-08-28 shape, where it genuinely does bite."""
    counters = {SOLAR: 2.7, BAT_OUT: 1.2, GRID_IN: 5.4, BAT_IN: 4.8, EXPORT: 0.5}
    flows = {k: 0.0 for k in FLOWS}
    flows.update({"solar_to_export": 0.5, "solar_to_battery": 1.4,
                  "solar_to_house": 0.8, "grid_to_house": 2.2,
                  "grid_to_battery": 3.2, "battery_to_house": 1.2})
    states = states_for(counters, flows)
    results = set()
    for nodes in _source_node_permutations():
        got = resolve(states, nodes=nodes, links=greedy_links())
        results.add(tuple(sorted((k, round(v, 6)) for k, v in got.items())))
    assert len(results) > 1

    # ...and honest values make the same scenario order-independent.
    stable = set()
    for nodes in _source_node_permutations():
        got = resolve(states, nodes=nodes)
        stable.add(tuple(sorted((k, round(v, 6)) for k, v in got.items())))
    assert len(stable) == 1


def test_the_house_node_is_stable_under_supply_permutation():
    baseline = resolve(SIM_STATES)
    for links in _shuffled_links(n=60):
        got = resolve(SIM_STATES, links=links)
        for source in (SOLAR, BAT_OUT, GRID_IN):
            assert got[(source, HOUSE)] == pytest.approx(baseline[(source, HOUSE)],
                                                         abs=1e-9)
