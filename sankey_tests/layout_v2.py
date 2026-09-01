"""The Sankey v2 card layout, as data.

This module is the single source of truth for the v2 `nodes`, `links` and
`sections` blocks of the `custom:sankey-chart` card on the `energy-live`
dashboard. It holds no Home Assistant dependency and performs no I/O, so the
tests and the apply script consume exactly the same objects.

The graph (BRIEF_V2.md section 1)::

    section 0            section 1
    Solar        ->      Grid export
    Battery out  ->      Battery in
    Grid import  ->      House
                         Tesla
                         Inverter

    solar   -> grid_export | battery_in | house | tesla | inverter
    bat_out -> house | tesla | inverter
    grid    -> tesla | house | battery_in | inverter

That is **12** links, one per source->sink flow (solar 5, battery out 3, grid
import 4), each carrying a per-link `value`. Every sink is a terminus, so the
chart is two columns and every box in the right-hand column is at the same x.

**Two nodes were deliberately removed on 2026-08-30, in this order.** Do not
re-add either without being asked.

*   "Stored", a remaining_parent_state child of Battery in at section 2. It
    only ever restated Battery in's own number one column to the right.
*   "Air con" and "Rest of house", House's device split at section 2. Dropping
    them is what makes House a terminus and lets Tesla sit level with every
    other sink -- which is the whole reason the split went. The air-con
    entity still exists in HA and is unaffected; it is simply not on this
    chart. CLAUDE.md's older note that the owner wants the breakdown "only in
    the Sankey" is superseded by that choice.

`NODES`, `LINKS`, `SECTIONS`, `SOURCE_SINK_LINKS`, `COLORS` and `FLOW_METERS`
are plain module-level data and are the *only* declaration of the graph.
Import them; never re-declare them in a test. A re-declared copy silently
drifts from what ships, which is how v1's chart tests stopped testing the
chart. Use `build()` when you need mutable copies.

Three facts from ha-sankey-chart 6.3.0 shape everything below. Offsets are into
the minified `ha-sankey-chart.js` shipped by HACS, cross-read against a
beautified copy.

1.  **`value` on a flat link is the per-connection cap.** `Pe()` (minified
    char offset 40117) rewrites a link into a child ref::

        e.value ? t.children.push({entity_id: e.target,
                                   connection_entity_id: e.value})
                : t.children.push(e.target)

    and `_calcConnection` (minified char offset 60678) consumes it::

        if (t.connection_entity_id) {
          const e = this._getMemoizedState(t.connection_entity_id).state ?? 0;
          t.state = Math.min(m, y, e);
        } else t.state = Math.min(m, y);

    So the rule really is `min(parent_remainder, child_remainder, value)`, and
    supplying every source->sink value collapses the card's order sensitivity.

2.  **A `type: "passthrough"` node cannot be a terminus, and renders as a
    ghost.** `willUpdate` skips a passthrough as a connection *parent*
    (`else if ("passthrough" === e.type) return;`, minified char offset 58755)
    and, when resolving a child, walks *through* it (offset 58963)::

        for (; "passthrough" === s?.type;) {
          n.push(s);
          const e = s.children[0];
          if (!e) throw this.error = new Error(missing_child ...);
          s = t.get(Me(e));
        }

    A declared passthrough with no children therefore throws `missing_child`
    outright. Even with a child it is invisible as a box: the icon is gated by
    `l = "passthrough" !== n.config.type` (offset 54613), the label and state
    by `if ("passthrough" === t.config.type || (!r && !a)) return null`
    (offset 55685), and the bar is dimmed by
    `.box.type-passthrough .color-bar{fill-opacity:.4}` (offset 48739). Its
    state is not its own either -- `_getEntityState` returns the *child's*
    state (offset 69939) and `_getMemoizedState` overwrites it with the sum of
    the connections passing through it (offset 61850).

    A declared passthrough is thus exactly the unlabelled ghost box CLAUDE.md
    warns about, deliberately requested. Any second-hop node reintroduced here
    must be a `remaining_parent_state` instead -- the shape the removed "Rest
    of house" used, which was known to work.

3.  **Every link must span exactly one section.** `Te()` (minified char
    offset 41735) synthesises `${target}__passthrough_${i}__auto`
    boxes for any span > 1. `check_spans()` below is the guard, and the apply
    script must keep it.

    This is what pinned Tesla to section 1 while section 2 existed. Moving
    the car to section 2 needs a section-1 parent to feed it, which means
    House becoming the whole-site total with the car back inside it; that was
    proposed and rejected on 2026-08-30, because the owner's reason for the
    node existing at all is seeing which source charged the car. The layout
    went to two columns instead. Do not propose the House-hub shape again.

Node identity follows BRIEF_V2.md section 1. House, Tesla and Inverter are the
*sum of their own inbound flow meters*, expressed with the card's
`add_entities` so that no further helper is needed: the node's `entity_id` is
the first inbound meter and `add_entities` carries the other two. Solar,
Battery out/in and Grid import/export keep their existing daily counters.
"""

import copy

__all__ = [
    "BAT_IN",
    "BAT_OUT",
    "COLORS",
    "EXPORT",
    "FLOW_KEY",
    "FLOW_METERS",
    "GRID_IN",
    "HOUSE",
    "INVERTER",
    "LINKS",
    "NODES",
    "SECTIONS",
    "SOLAR",
    "SOURCE_SINK_LINKS",
    "TESLA",
    "build",
    "card_config",
    "check_spans",
    "flow_meter",
    "sections_of",
    "states_from",
]

# --------------------------------------------------------------------------
# Entities
# --------------------------------------------------------------------------

# Existing inverter / meter daily counters, unchanged from v1.
SOLAR = "sensor.solis_inverter_solar_today"
BAT_OUT = "sensor.solis_inverter_battery_discharge_today"
GRID_IN = "sensor.solis_inverter_grid_import_today"
BAT_IN = "sensor.solis_inverter_battery_charge_today"
EXPORT = "sensor.solis_inverter_grid_export_today"

# Node ids that are slugs rather than entities.
HOUSE = "house"
TESLA = "tesla"
INVERTER = "inverter"

# The 12 daily utility_meter helpers, one per source->sink flow. Naming is
# `sensor.flow_<source>_to_<sink>_daily`, where `battery` means discharge on
# the source side and charge on the sink side, and `grid` means import on the
# source side and export on the sink side. Each is fed by a Riemann
# integration of a template power sensor (BRIEF_V2.md section 4).
_SOURCE_SLUG = {SOLAR: "solar", BAT_OUT: "battery", GRID_IN: "grid"}
_SINK_SLUG = {
    HOUSE: "house",
    TESLA: "tesla",
    BAT_IN: "battery",
    EXPORT: "grid",
    INVERTER: "inverter",
}


def flow_meter(source, sink):
    """The utility_meter entity id carrying the source->sink daily flow."""
    return "sensor.flow_%s_to_%s_daily" % (_SOURCE_SLUG[source], _SINK_SLUG[sink])


# Declaration order is the card's allocation order. With every source->sink
# link capped by its own meter the order no longer changes the result, but it
# is kept in the v1 order anyway: it is the order the physics justifies, and it
# is what the layout degrades to if a meter ever reads `unavailable`.
SOURCE_SINK_LINKS = [
    (SOLAR, EXPORT),
    (SOLAR, BAT_IN),
    (SOLAR, HOUSE),
    (SOLAR, TESLA),
    (SOLAR, INVERTER),
    (BAT_OUT, HOUSE),
    (BAT_OUT, TESLA),
    (BAT_OUT, INVERTER),
    (GRID_IN, TESLA),
    (GRID_IN, HOUSE),
    (GRID_IN, BAT_IN),
    (GRID_IN, INVERTER),
]

FLOW_METERS = [flow_meter(source, sink) for source, sink in SOURCE_SINK_LINKS]

# The bridge to flows.py. That module names the same twelve flows as flat
# strings (`solar_to_house`, ...); this layout names them as (source id, sink
# id) pairs. Both namings are load-bearing -- flows.py's keys reach the Jinja
# templates, these pairs reach the card config -- so the mapping lives here, in
# one place, and is asserted against `flows.FLOWS` rather than being restated
# on either side.
#
# ONE deliberate difference, and it is the only one: the export sink is
# `grid` in an entity id and `export` in a flows.py key. The entity ids read
# as the physical thing the meter measures (energy going to the grid, next to
# `flow_grid_to_house_daily` coming from it), while flows.py names the node.
# Both are already committed -- the entity ids are durable and user-facing,
# flows.py's keys reach the templates -- so the seam is mapped here rather
# than renamed on either side. test_flow_key_bridges_the_layout_to_flows_py
# pins it so nobody meets it by surprise.
_FLOWS_SINK_SLUG = dict(_SINK_SLUG, **{EXPORT: "export"})

FLOW_KEY = {
    (source, sink): "%s_to_%s" % (_SOURCE_SLUG[source], _FLOWS_SINK_SLUG[sink])
    for source, sink in SOURCE_SINK_LINKS
}

# --------------------------------------------------------------------------
# Colours
# --------------------------------------------------------------------------

# Straight from CLAUDE.md's "Sankey node colours" table. These carry meaning
# and were chosen deliberately; Tesla is white because the owner's car is
# white, and air con must never be yellow or amber because Solar's
# --warning-color resolves to rgb(255,166,0) on this theme.
SOLAR_COLOR = "var(--warning-color)"
BATTERY_COLOR = "var(--success-color)"
GRID_IN_COLOR = "var(--error-color)"
EXPORT_COLOR = "#a78bfa"
HOUSE_COLOR = "var(--primary-color)"
TESLA_COLOR = "#ffffff"

# New in v2. The Inverter node is conversion loss and parasitic draw -- energy
# that arrived and did no work. Neutral grey is the only choice left that
# carries the right meaning and cannot be confused with a real flow: it is the
# one swatch on the chart with no hue at all, so it reads as inert next to
# seven saturated flows, and mid-luminance keeps it clearly apart from Tesla's
# white. A literal hex rather than a theme token, because the theme tokens are
# spoken for and --disabled-text-color / --secondary-text-color resolve
# differently per theme.
INVERTER_COLOR = "#6b7280"

COLORS = {
    SOLAR: SOLAR_COLOR,
    BAT_OUT: BATTERY_COLOR,
    GRID_IN: GRID_IN_COLOR,
    HOUSE: HOUSE_COLOR,
    TESLA: TESLA_COLOR,
    BAT_IN: BATTERY_COLOR,
    EXPORT: EXPORT_COLOR,
    INVERTER: INVERTER_COLOR,
}

# --------------------------------------------------------------------------
# The layout
# --------------------------------------------------------------------------

# Node order is allocation priority within a section. Sources keep the v1
# order -- Battery out ahead of Grid import, because with all three discharge
# windows unset `bat_out -> house` was once that source's only link and being
# allocated last stranded it entirely (CLAUDE.md, 2026-08-28).
NODES = [
    {"id": SOLAR, "name": "Solar", "section": 0, "color": SOLAR_COLOR},
    {"id": BAT_OUT, "name": "Battery out", "section": 0, "color": BATTERY_COLOR},
    {"id": GRID_IN, "name": "Grid import", "section": 0, "color": GRID_IN_COLOR},

    # House / Tesla / Inverter are each the sum of their own inbound meters.
    {
        "id": HOUSE,
        "name": "House",
        "section": 1,
        "color": HOUSE_COLOR,
        "entity_id": flow_meter(SOLAR, HOUSE),
        "add_entities": [flow_meter(BAT_OUT, HOUSE), flow_meter(GRID_IN, HOUSE)],
    },
    {
        "id": TESLA,
        "name": "Tesla",
        "section": 1,
        "color": TESLA_COLOR,
        "entity_id": flow_meter(SOLAR, TESLA),
        "add_entities": [flow_meter(BAT_OUT, TESLA), flow_meter(GRID_IN, TESLA)],
    },
    {"id": BAT_IN, "name": "Battery in", "section": 1, "color": BATTERY_COLOR},
    {"id": EXPORT, "name": "Grid export", "section": 1, "color": EXPORT_COLOR},
    {
        "id": INVERTER,
        "name": "Inverter",
        "section": 1,
        "color": INVERTER_COLOR,
        "entity_id": flow_meter(SOLAR, INVERTER),
        "add_entities": [flow_meter(BAT_OUT, INVERTER), flow_meter(GRID_IN, INVERTER)],
    },

]

LINKS = [
    {"source": source, "target": sink, "value": flow_meter(source, sink)}
    for source, sink in SOURCE_SINK_LINKS
]

SECTIONS = [
    {"min_width": 170, "sort_by": "none"},
    {"min_width": 190, "sort_by": "none"},
]


# --------------------------------------------------------------------------
# Accessors
# --------------------------------------------------------------------------


def build():
    """Fresh deep copies of (nodes, links, sections).

    Every caller gets its own objects: the apply script writes these straight
    into a live dashboard config, and the permutation tests reorder them.
    """
    return (
        copy.deepcopy(NODES),
        copy.deepcopy(LINKS),
        copy.deepcopy(SECTIONS),
    )


def sections_of(nodes=None):
    """{node id: section index}."""
    return {n["id"]: n.get("section", 0) for n in (NODES if nodes is None else nodes)}


def check_spans(nodes=None, links=None):
    """Every link whose section span is not exactly 1.

    A span > 1 makes the card synthesise an unlabelled passthrough box
    coloured like the target; a span of 0 or less renders a sideways or
    backwards ribbon. All three are layout bugs. Returns a list of
    (source, target, span); empty means the layout is safe.
    """
    section = sections_of(nodes)
    bad = []
    for link in (LINKS if links is None else links):
        span = section[link["target"]] - section[link["source"]]
        if span != 1:
            bad.append((link["source"], link["target"], span))
    return bad


def states_from(counters, flows):
    """Assemble the card's `states` dict.

    counters: {entity id: kWh} for the five daily counters.
    flows:    {(source id, sink id): kWh} for the 12 source->sink flows.

    House, Tesla and Inverter deliberately have no entry: their state is
    derived by the card from the flow meters via `add_entities`.
    """
    states = dict(counters)
    for (source, sink), value in flows.items():
        states[flow_meter(source, sink)] = value
    return states


def card_config():
    """The full `custom:sankey-chart` card dict."""
    nodes, links, sections = build()
    return {
        "type": "custom:sankey-chart",
        "energy_date_selection": True,
        "unit_prefix": "k",
        "round": 1,
        "min_box_height": 3,
        "min_box_distance": 5,
        "show_states": True,
        "show_names": True,
        "show_units": True,
        "sections": sections,
        "nodes": nodes,
        "links": links,
    }
