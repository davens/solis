"""Tests for the ha-sankey-chart 6.3.0 allocator port.

Numbers marked "documented" come from CLAUDE.md and were observed on the live
dashboard. Numbers marked "reconstructed" are ours: they reproduce the *shape*
of a documented failure whose inputs were never recorded.
"""

import math

import pytest

from allocator import (
    InvalidConfigError,
    MissingEntityError,
    allocate,
)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def n(node_id, section=0, **kw):
    return dict(id=node_id, section=section, **kw)


def l(source, target, value=None):
    link = {"source": source, "target": target}
    if value is not None:
        link["value"] = value
    return link


def states_of(result):
    return [c["state"] for c in result]


def pairs(result):
    return [(c["parent"], c["child"], c["state"]) for c in result]


# The repository's own three-section layout, with the corrected node order
# (solar, battery out, grid import) recorded in CLAUDE.md.
def repo_nodes():
    return [
        n("solar", 0),
        n("battery_discharge", 0),
        n("grid_import", 0),
        n("house", 1),
        n("battery_charge", 1),
        n("grid_export", 1),
    ]


def repo_links():
    return [
        l("solar", "grid_export"),
        l("solar", "battery_charge"),
        l("solar", "house"),
        l("grid_import", "house"),
        l("grid_import", "battery_charge"),
        l("battery_discharge", "house"),
    ]


# --------------------------------------------------------------------------
# 1. basic greedy
# --------------------------------------------------------------------------


class TestBasicGreedy:
    def test_exact_fit(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"a": 5.0, "b": 5.0})
        assert states_of(r) == [5.0]

    def test_source_starved(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"a": 2.0, "b": 5.0})
        assert states_of(r) == [2.0]
        assert r.filled["b"] == 2.0

    def test_sink_starved(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"a": 9.0, "b": 3.0})
        assert states_of(r) == [3.0]
        assert r.spent["a"] == 3.0

    def test_zero_source_gives_zero(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"a": 0.0, "b": 5.0})
        assert states_of(r) == [0.0]
        # The falsy branch never touches the bookkeeping maps.
        assert "a" not in r.spent and "b" not in r.filled

    def test_zero_sink_gives_zero(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"a": 5.0, "b": 0.0})
        assert states_of(r) == [0.0]
        assert "a" not in r.spent

    def test_one_source_two_sinks_fills_in_order(self):
        r = allocate(
            [n("a", 0), n("b", 1), n("c", 1)],
            [l("a", "b"), l("a", "c")],
            {"a": 10.0, "b": 4.0, "c": 9.0},
        )
        assert states_of(r) == [4.0, 6.0]

    def test_second_sink_starves_when_source_exhausted(self):
        r = allocate(
            [n("a", 0), n("b", 1), n("c", 1)],
            [l("a", "b"), l("a", "c")],
            {"a": 4.0, "b": 4.0, "c": 9.0},
        )
        assert states_of(r) == [4.0, 0.0]

    def test_two_sources_one_sink(self):
        r = allocate(
            [n("a", 0), n("b", 0), n("c", 1)],
            [l("a", "c"), l("b", "c")],
            {"a": 3.0, "b": 8.0, "c": 10.0},
        )
        assert states_of(r) == [3.0, 7.0]

    def test_second_source_starves_when_sink_full(self):
        r = allocate(
            [n("a", 0), n("b", 0), n("c", 1)],
            [l("a", "c"), l("b", "c")],
            {"a": 10.0, "b": 8.0, "c": 10.0},
        )
        assert states_of(r) == [10.0, 0.0]

    def test_one_connection_per_link(self):
        r = allocate(repo_nodes(), repo_links(), _repo_states())
        assert len(r) == len(repo_links())

    def test_duplicate_link_is_a_second_connection(self):
        # The card does not deduplicate; the second declaration simply finds
        # nothing left.
        r = allocate(
            [n("a", 0), n("b", 1)],
            [l("a", "b"), l("a", "b")],
            {"a": 5.0, "b": 5.0},
        )
        assert states_of(r) == [5.0, 0.0]


def _repo_states():
    # Documented live numbers, CLAUDE.md "Node order is allocation priority".
    return {
        "solar": 2.7,
        "grid_import": 5.4,
        "battery_discharge": 1.2,
        "house": 4.2,
        "battery_charge": 4.8,
        "grid_export": 0.5,
    }


# --------------------------------------------------------------------------
# 2. ordering
# --------------------------------------------------------------------------


class TestOrdering:
    def test_node_order_is_allocation_priority(self):
        bad = [
            n("solar", 0),
            n("grid_import", 0),
            n("battery_discharge", 0),
            n("house", 1),
            n("battery_charge", 1),
            n("grid_export", 1),
        ]
        r = allocate(bad, repo_links(), _repo_states())
        assert r.state("battery_discharge", "house") == pytest.approx(0.0)

    def test_corrected_node_order_frees_the_battery(self):
        r = allocate(repo_nodes(), repo_links(), _repo_states())
        assert r.state("battery_discharge", "house") == pytest.approx(1.2)

    def test_node_order_changes_only_the_story_not_the_totals(self):
        bad = [
            n("solar", 0),
            n("grid_import", 0),
            n("battery_discharge", 0),
            n("house", 1),
            n("battery_charge", 1),
            n("grid_export", 1),
        ]
        good = allocate(repo_nodes(), repo_links(), _repo_states())
        worse = allocate(bad, repo_links(), _repo_states())
        # House is filled either way; what changes is which source filled it.
        assert good.filled["house"] == pytest.approx(worse.filled["house"])
        assert good.state("grid_import", "house") != pytest.approx(
            worse.state("grid_import", "house")
        )

    def test_link_order_export_first_versus_last(self):
        nodes = [n("solar", 0), n("house", 1), n("battery_charge", 1), n("grid_export", 1)]
        st = {"solar": 16.3, "house": 20.0, "battery_charge": 4.5, "grid_export": 5.1}
        export_last = allocate(
            nodes,
            [l("solar", "battery_charge"), l("solar", "house"), l("solar", "grid_export")],
            st,
        )
        export_first = allocate(
            nodes,
            [l("solar", "grid_export"), l("solar", "battery_charge"), l("solar", "house")],
            st,
        )
        assert export_last.state("solar", "grid_export") == pytest.approx(0.0)
        assert export_first.state("solar", "grid_export") == pytest.approx(5.1)

    def test_link_order_within_one_node_only(self):
        # Reordering links belonging to *different* parents does not reorder
        # allocation, because node order dominates.
        nodes = [n("a", 0), n("b", 0), n("c", 1)]
        st = {"a": 4.0, "b": 4.0, "c": 5.0}
        one = allocate(nodes, [l("a", "c"), l("b", "c")], st)
        two = allocate(nodes, [l("b", "c"), l("a", "c")], st)
        assert one.state("a", "c") == pytest.approx(4.0)
        assert two.state("a", "c") == pytest.approx(4.0)
        assert one.state("b", "c") == pytest.approx(1.0)
        assert two.state("b", "c") == pytest.approx(1.0)

    def test_sections_iterate_ascending_even_when_declared_out_of_order(self):
        nodes = [n("late", 1), n("early", 0), n("sink", 2)]
        r = allocate(
            nodes,
            [l("early", "late"), l("late", "sink")],
            {"early": 5.0, "late": 5.0, "sink": 5.0},
        )
        assert pairs(r)[0][0] == "early"
        assert pairs(r)[1][0] == "late"

    def test_connection_output_order_is_sections_then_nodes_then_links(self):
        nodes = [n("s1", 0), n("s2", 0), n("m", 1), n("t", 2)]
        r = allocate(
            nodes,
            [l("s2", "m"), l("s1", "m"), l("m", "t")],
            {"s1": 1.0, "s2": 1.0, "m": 2.0, "t": 2.0},
        )
        # s1 before s2 (node order), then m's link last (section order).
        assert [(c["parent"], c["child"]) for c in r] == [
            ("s1", "m"),
            ("s2", "m"),
            ("m", "t"),
        ]

    def test_node_order_within_a_section_survives_interleaving(self):
        nodes = [n("a", 0), n("x", 1), n("b", 0), n("y", 1)]
        r = allocate(
            nodes,
            [l("a", "x"), l("b", "y")],
            {"a": 1.0, "b": 1.0, "x": 1.0, "y": 1.0},
        )
        assert [(c["parent"], c["child"]) for c in r] == [("a", "x"), ("b", "y")]


# --------------------------------------------------------------------------
# 3. the `value` cap  -- min(parent_rem, child_rem, value)
# --------------------------------------------------------------------------


class TestValueCap:
    NODES = [n("a", 0), n("b", 1)]
    LINKS = [l("a", "b", "sensor.cap")]

    def test_value_below_both_remainders(self):
        r = allocate(self.NODES, self.LINKS, {"a": 10.0, "b": 8.0, "sensor.cap": 3.0})
        assert states_of(r) == [3.0]

    def test_value_above_both_remainders(self):
        r = allocate(self.NODES, self.LINKS, {"a": 10.0, "b": 8.0, "sensor.cap": 99.0})
        assert states_of(r) == [8.0]

    def test_value_between_the_two_remainders(self):
        r = allocate(self.NODES, self.LINKS, {"a": 10.0, "b": 8.0, "sensor.cap": 9.0})
        assert states_of(r) == [8.0]

    def test_value_between_when_it_is_the_middle_term(self):
        r = allocate(self.NODES, self.LINKS, {"a": 4.0, "b": 8.0, "sensor.cap": 6.0})
        assert states_of(r) == [4.0]

    def test_value_exactly_equal_to_parent_remainder(self):
        r = allocate(self.NODES, self.LINKS, {"a": 5.0, "b": 8.0, "sensor.cap": 5.0})
        assert states_of(r) == [5.0]

    def test_value_exactly_equal_to_child_remainder(self):
        r = allocate(self.NODES, self.LINKS, {"a": 9.0, "b": 5.0, "sensor.cap": 5.0})
        assert states_of(r) == [5.0]

    def test_value_zero_resolves_to_zero_but_still_books_the_maps(self):
        r = allocate(self.NODES, self.LINKS, {"a": 10.0, "b": 8.0, "sensor.cap": 0.0})
        assert states_of(r) == [0.0]
        # The `if (m && y)` branch was taken, so the maps were written -- with
        # zero. That is observably different from the falsy branch.
        assert r.spent["a"] == 0.0
        assert r.filled["b"] == 0.0

    def test_value_missing_entity_is_zero_when_ignoring(self):
        r = allocate(self.NODES, self.LINKS, {"a": 10.0, "b": 8.0})
        assert states_of(r) == [0.0]

    def test_value_missing_entity_raises_in_strict_mode(self):
        with pytest.raises(MissingEntityError):
            allocate(self.NODES, self.LINKS, {"a": 10.0, "b": 8.0},
                     ignore_missing_entities=False)

    def test_value_unavailable_poisons_the_connection_with_nan(self):
        r = allocate(self.NODES, self.LINKS,
                     {"a": 10.0, "b": 8.0, "sensor.cap": "unavailable"})
        assert math.isnan(states_of(r)[0])

    def test_nan_value_poisons_the_parent_for_later_links(self):
        r = allocate(
            [n("a", 0), n("b", 1), n("c", 1)],
            [l("a", "b", "sensor.cap"), l("a", "c")],
            {"a": 10.0, "b": 8.0, "c": 8.0, "sensor.cap": "unavailable"},
        )
        assert math.isnan(r[0]["state"])
        # a's spent is now NaN, so max(0, 10 - NaN) is NaN, which is falsy.
        assert r[1]["state"] == 0.0

    def test_value_cap_leaves_remainder_for_the_next_link(self):
        r = allocate(
            [n("a", 0), n("b", 1), n("c", 1)],
            [l("a", "b", "sensor.cap"), l("a", "c")],
            {"a": 10.0, "b": 8.0, "c": 8.0, "sensor.cap": 3.0},
        )
        assert states_of(r) == [3.0, 7.0]

    def test_value_is_not_consulted_when_parent_remainder_is_zero(self):
        r = allocate(
            [n("a", 0), n("b", 1), n("c", 1)],
            [l("a", "b"), l("a", "c", "sensor.cap")],
            {"a": 5.0, "b": 5.0, "c": 5.0, "sensor.cap": 4.0},
        )
        assert states_of(r) == [5.0, 0.0]

    def test_value_is_not_consulted_when_child_remainder_is_zero(self):
        r = allocate(
            [n("a", 0), n("b", 0), n("c", 1)],
            [l("a", "c"), l("b", "c", "sensor.cap")],
            {"a": 5.0, "b": 5.0, "c": 5.0, "sensor.cap": 4.0},
        )
        assert states_of(r) == [5.0, 0.0]

    def test_negative_value_entity_wins_the_min(self):
        # Nothing clamps the cap. A negative sensor draws a negative ribbon.
        r = allocate(self.NODES, self.LINKS, {"a": 10.0, "b": 8.0, "sensor.cap": -2.0})
        assert states_of(r) == [-2.0]

    def test_links_without_value_are_uncapped(self):
        r = allocate(self.NODES, [l("a", "b")], {"a": 10.0, "b": 8.0, "sensor.cap": 1.0})
        assert states_of(r) == [8.0]


# --------------------------------------------------------------------------
# 4. regressions from CLAUDE.md
# --------------------------------------------------------------------------


class TestDocumentedRegressions:
    def test_stranded_battery_discharge_box(self):
        """Documented: battery out resolved to 0.00 of 1.20, battery in 1.40 unfed."""
        bad_order = [
            n("solar", 0),
            n("grid_import", 0),
            n("battery_discharge", 0),
            n("house", 1),
            n("battery_charge", 1),
            n("grid_export", 1),
        ]
        r = allocate(bad_order, repo_links(), _repo_states())
        assert r.state("battery_discharge", "house") == pytest.approx(0.0)
        unfed = 4.8 - r.filled["battery_charge"]
        assert unfed == pytest.approx(1.4)

    def test_stranded_box_is_invisible_not_absent(self):
        bad_order = [
            n("solar", 0),
            n("grid_import", 0),
            n("battery_discharge", 0),
            n("house", 1),
            n("battery_charge", 1),
            n("grid_export", 1),
        ]
        r = allocate(bad_order, repo_links(), _repo_states())
        # The connection exists and is declared; only its state is zero. This is
        # exactly why CLAUDE.md says to read base.__connections, not the picture.
        assert ("battery_discharge", "house") in [(c["parent"], c["child"]) for c in r]

    def test_fixed_node_order_lets_grid_fall_back_to_the_battery(self):
        r = allocate(repo_nodes(), repo_links(), _repo_states())
        assert r.state("battery_discharge", "house") == pytest.approx(1.2)
        assert r.state("grid_import", "house") == pytest.approx(3.0)
        assert r.filled["battery_charge"] == pytest.approx(4.6)

    def test_starved_export_floats_despite_its_state(self):
        """Documented: solar fed battery then house before export, so export floated."""
        nodes = [n("solar", 0), n("house", 1), n("battery_charge", 1), n("grid_export", 1)]
        st = {"solar": 16.3, "house": 20.0, "battery_charge": 4.5, "grid_export": 5.1}
        r = allocate(
            nodes,
            [l("solar", "battery_charge"), l("solar", "house"), l("solar", "grid_export")],
            st,
        )
        assert r.state("solar", "grid_export") == pytest.approx(0.0)
        assert r.filled.get("grid_export", 0.0) == 0.0

    def test_export_first_is_the_documented_fix(self):
        nodes = [n("solar", 0), n("house", 1), n("battery_charge", 1), n("grid_export", 1)]
        st = {"solar": 16.3, "house": 20.0, "battery_charge": 4.5, "grid_export": 5.1}
        r = allocate(
            nodes,
            [l("solar", "grid_export"), l("solar", "battery_charge"), l("solar", "house")],
            st,
        )
        assert r.state("solar", "grid_export") == pytest.approx(5.1)
        assert r.state("solar", "battery_charge") == pytest.approx(4.5)
        assert r.state("solar", "house") == pytest.approx(6.7)

    def test_grid_import_to_battery_resolves_to_zero_when_solar_filled_it(self):
        nodes = [n("solar", 0), n("grid_import", 0), n("house", 1), n("battery_charge", 1)]
        r = allocate(
            nodes,
            [
                l("solar", "battery_charge"),
                l("solar", "house"),
                l("grid_import", "house"),
                l("grid_import", "battery_charge"),
            ],
            {"solar": 10.0, "grid_import": 3.0, "house": 6.0, "battery_charge": 4.0},
        )
        assert r.state("solar", "battery_charge") == pytest.approx(4.0)
        assert r.state("grid_import", "battery_charge") == pytest.approx(0.0)

    def test_declaring_battery_to_grid_fabricates_battery_export(self):
        """CLAUDE.md: the link was removed because merely declaring it invented flow."""
        nodes = [n("battery_discharge", 0), n("house", 1), n("grid_export", 1)]
        st = {"battery_discharge": 4.0, "house": 1.0, "grid_export": 5.0}
        r = allocate(
            nodes,
            [l("battery_discharge", "house"), l("battery_discharge", "grid_export")],
            st,
        )
        assert r.state("battery_discharge", "grid_export") == pytest.approx(3.0)

    def test_removing_battery_to_grid_leaves_an_unspent_remainder(self):
        nodes = [n("battery_discharge", 0), n("house", 1), n("grid_export", 1)]
        st = {"battery_discharge": 4.0, "house": 1.0, "grid_export": 5.0}
        r = allocate(nodes, [l("battery_discharge", "house")], st)
        # A shorter bar, not an error, and not a link to invent a target for.
        assert r.spent["battery_discharge"] == pytest.approx(1.0)

    def test_repo_three_section_layout_has_no_span_warnings(self):
        r = allocate(repo_nodes(), repo_links(), _repo_states())
        assert r.warnings == []
        assert r.ghost_nodes == []


# --------------------------------------------------------------------------
# 5. passthrough / ghost boxes
# --------------------------------------------------------------------------


def four_section_nodes():
    """The pre-2026-08-28 layout: battery_charge sat two sections downstream."""
    return [
        n("solar", 0),
        n("grid_import", 0),
        n("house", 1),
        n("battery_charge", 2),
        n("grid_export", 3),
    ]


def four_section_links():
    return [
        l("solar", "grid_export"),
        l("solar", "battery_charge"),
        l("solar", "house"),
        l("grid_import", "house"),
        l("grid_import", "battery_charge"),
    ]


def four_section_states():
    return {
        "solar": 16.3,
        "grid_import": 14.0,
        "house": 22.3,
        "battery_charge": 5.6,
        "grid_export": 6.7,
    }


class TestPassthroughGhosts:
    def test_span_two_is_reported(self):
        r = allocate(four_section_nodes(), four_section_links(), four_section_states())
        kinds = {w["kind"] for w in r.warnings}
        assert "ghost_passthrough" in kinds

    def test_ghost_nodes_are_synthesised(self):
        r = allocate(four_section_nodes(), four_section_links(), four_section_states())
        assert "battery_charge__passthrough_1__auto" in r.ghost_nodes

    def test_ghost_id_format_matches_the_bundle(self):
        r = allocate(
            [n("a", 0), n("b", 3)],
            [l("a", "b")],
            {"a": 1.0, "b": 1.0},
        )
        assert r.ghost_nodes == ["b__passthrough_1__auto", "b__passthrough_2__auto"]

    def test_one_ghost_per_intermediate_section(self):
        r = allocate([n("a", 0), n("b", 4)], [l("a", "b")], {"a": 1.0, "b": 1.0})
        assert len(r.ghost_nodes) == 3

    def test_ghost_is_reused_by_a_second_link_to_the_same_target(self):
        r = allocate(
            [n("a", 0), n("b", 0), n("c", 2)],
            [l("a", "c"), l("b", "c")],
            {"a": 1.0, "b": 1.0, "c": 2.0},
        )
        assert r.ghost_nodes == ["c__passthrough_1__auto"]

    def test_passthrough_does_not_change_the_arithmetic(self):
        four = allocate(four_section_nodes(), four_section_links(), four_section_states())
        three = allocate(
            [
                n("solar", 0),
                n("grid_import", 0),
                n("house", 1),
                n("battery_charge", 1),
                n("grid_export", 1),
            ],
            four_section_links(),
            four_section_states(),
        )
        assert pairs(four) == pairs(three)

    def test_collapsing_to_three_sections_stops_the_synthesis(self):
        three = allocate(
            [
                n("solar", 0),
                n("grid_import", 0),
                n("house", 1),
                n("battery_charge", 1),
                n("grid_export", 1),
            ],
            four_section_links(),
            four_section_states(),
        )
        assert three.ghost_nodes == []
        assert three.warnings == []

    def test_the_real_parent_and_child_survive_the_ghost(self):
        r = allocate(four_section_nodes(), four_section_links(), four_section_states())
        conn = next(
            c for c in r.connections
            if c.parent.id == "solar" and c.child.id == "battery_charge"
        )
        assert [p.id for p in conn.passthroughs] == ["battery_charge__passthrough_1__auto"]

    def test_ghost_inherits_the_targets_colour(self):
        r = allocate(
            [n("a", 0), n("b", 2, color="green")],
            [l("a", "b")],
            {"a": 1.0, "b": 1.0},
        )
        ghost = next(x for x in r.nodes if x["id"] == "b__passthrough_1__auto")
        assert ghost["color"] == "green"
        assert ghost["type"] == "passthrough"

    def test_same_section_link_is_reported(self):
        r = allocate([n("a", 0), n("b", 0)], [l("a", "b")], {"a": 1.0, "b": 1.0})
        assert [w["kind"] for w in r.warnings] == ["same_section"]

    def test_backwards_link_is_reported(self):
        r = allocate([n("a", 1), n("b", 0)], [l("a", "b")], {"a": 1.0, "b": 1.0})
        assert [w["kind"] for w in r.warnings] == ["backwards"]

    def test_dangling_target_raises_like_the_card(self):
        # Pe() attaches the child string regardless, then _updateConnections
        # throws common.missing_child when it cannot resolve it.
        with pytest.raises(InvalidConfigError):
            allocate([n("a", 0)], [l("a", "nowhere")], {"a": 1.0})

    def test_dangling_source_is_silently_skipped(self):
        # Pe() guards the source lookup (`if (s) {...}`), so the link vanishes
        # with no error and no ribbon.
        r = allocate([n("a", 0), n("b", 1)], [l("ghost", "b"), l("a", "b")],
                     {"a": 1.0, "b": 1.0})
        assert [(c["parent"], c["child"]) for c in r] == [("a", "b")]
        assert [w["kind"] for w in r.warnings] == ["dangling_link"]

    def test_passthrough_node_is_never_a_connection_parent(self):
        r = allocate(four_section_nodes(), four_section_links(), four_section_states())
        assert not any(c["parent"].endswith("__auto") for c in r)
        assert not any(c["child"].endswith("__auto") for c in r)


# --------------------------------------------------------------------------
# 6. remaining_parent_state / remaining_child_state
# --------------------------------------------------------------------------


class TestRemainingStates:
    def test_remaining_parent_state_absorbs_the_leftover(self):
        r = allocate(
            [n("a", 0), n("b", 0), n("house", 1),
             n("untracked", 1, type="remaining_parent_state")],
            [l("a", "house"), l("a", "untracked"), l("b", "house"), l("b", "untracked")],
            {"a": 10.0, "b": 10.0, "house": 18.0},
        )
        assert r.filled["untracked"] == pytest.approx(2.0)

    def test_remaining_parent_state_matches_the_true_residual_when_nothing_starves(self):
        r = allocate(
            [n("a", 0), n("house", 1), n("untracked", 1, type="remaining_parent_state")],
            [l("a", "house"), l("a", "untracked")],
            {"a": 10.0, "house": 7.0},
        )
        assert r.state("a", "untracked") == pytest.approx(3.0)

    def test_remaining_parent_state_overstates_when_a_sink_starves(self):
        """Documented shape: 'Untracked / losses' showed 3.4 kWh, real residual 0.1.

        The input numbers here are RECONSTRUCTED -- CLAUDE.md records the two
        outputs but not the config or sensor values behind them. What is being
        pinned is the mechanism: remaining_parent_state sums greedy upstream
        leftovers, so a starved sink upstream inflates it far past the balance.
        """
        nodes = [
            n("solar", 0),
            n("grid_import", 0),
            n("battery_discharge", 0),
            n("house", 1),
            n("battery_charge", 1),
            n("grid_export", 1),
            n("untracked", 1, type="remaining_parent_state"),
        ]
        links = [
            l("solar", "battery_charge"),
            l("solar", "house"),
            l("solar", "grid_export"),
            l("solar", "untracked"),
            l("grid_import", "house"),
            l("grid_import", "battery_charge"),
            l("grid_import", "grid_export"),
            l("grid_import", "untracked"),
            l("battery_discharge", "house"),
            l("battery_discharge", "untracked"),
        ]
        st = {
            "solar": 16.3,
            "grid_import": 14.0,
            "battery_discharge": 3.4,
            "house": 22.3,
            "battery_charge": 5.6,
            "grid_export": 5.7,
        }
        real_residual = (16.3 + 14.0 + 3.4) - (22.3 + 5.6 + 5.7)
        assert real_residual == pytest.approx(0.1)

        r = allocate(nodes, links, st)
        assert r.filled["untracked"] == pytest.approx(3.4)
        assert r.filled["untracked"] > real_residual * 10
        # ... and the reason: export starved.
        assert r.filled["grid_export"] == pytest.approx(2.4)

    def test_remaining_parent_state_with_no_incoming_link_raises(self):
        with pytest.raises(InvalidConfigError):
            allocate(
                [n("x", 0, type="remaining_parent_state"), n("b", 1)],
                [l("x", "b")],
                {"b": 1.0},
            )

    def test_remaining_child_state_does_not_split_what_it_received(self):
        """FINDING: it is a sum-of-children node, not a leftover node.

        _getEntityState reduces connectionsByParent with `ready ? acc+state :
        Infinity`, so while its own outgoing links are unresolved the node's
        state is Infinity and every child fills to *its own* capacity. The node
        then emits more than it was given, and nothing in the card notices.
        """
        r = allocate(
            [n("a", 0), n("mid", 1, type="remaining_child_state"), n("x", 2), n("y", 2)],
            [l("a", "mid"), l("mid", "x"), l("mid", "y")],
            {"a": 10.0, "x": 4.0, "y": 9.0},
        )
        assert r.state("mid", "x") == pytest.approx(4.0)
        assert r.state("mid", "y") == pytest.approx(9.0)
        assert r.spent["mid"] == pytest.approx(13.0)
        assert r.filled["mid"] == pytest.approx(10.0)

    def test_remaining_child_state_children_resolve_in_reverse_declaration_order(self):
        # The recursion in _calcConnection forces the *later* sibling to resolve
        # first, because computing mid->x pre-resolves mid->y.
        r = allocate(
            [n("a", 0), n("mid", 1, type="remaining_child_state"), n("x", 2), n("y", 2)],
            [l("a", "mid"), l("mid", "x"), l("mid", "y")],
            {"a": 10.0, "x": 4.0, "y": 9.0},
        )
        # Both still get their full capacity, which is the point: neither
        # starves the other, because the parent looked infinite to both.
        assert r.state("mid", "x") + r.state("mid", "y") > r.state("a", "mid")

    def test_remaining_child_state_with_no_outgoing_links_is_zero(self):
        r = allocate(
            [n("a", 0), n("mid", 1, type="remaining_child_state")],
            [l("a", "mid")],
            {"a": 10.0},
        )
        assert states_of(r) == [0.0]

    def test_remaining_child_state_inflow_is_capped_by_its_children_not_the_reverse(self):
        r = allocate(
            [n("a", 0), n("mid", 1, type="remaining_child_state"), n("x", 2), n("y", 2)],
            [l("a", "mid"), l("mid", "x"), l("mid", "y")],
            {"a": 3.0, "x": 4.0, "y": 9.0},
        )
        # a->mid is min(a=3, children total=13) -- the children bound the
        # *inflow*. They are not themselves bounded by the inflow.
        assert r.state("a", "mid") == pytest.approx(3.0)
        assert r.state("mid", "x") + r.state("mid", "y") == pytest.approx(13.0)

    def test_remaining_child_state_inflow_is_capped_when_children_are_small(self):
        r = allocate(
            [n("a", 0), n("mid", 1, type="remaining_child_state"), n("x", 2)],
            [l("a", "mid"), l("mid", "x")],
            {"a": 10.0, "x": 2.0},
        )
        assert r.state("a", "mid") == pytest.approx(2.0)
        assert r.state("mid", "x") == pytest.approx(2.0)

    def test_remaining_parent_state_is_not_an_arithmetic_balance(self):
        # Two sources, one starved sink, one remaining node. The residual is
        # zero but the remaining node still shows the leftover.
        r = allocate(
            [n("a", 0), n("b", 0), n("small", 1),
             n("untracked", 1, type="remaining_parent_state")],
            [l("a", "small"), l("a", "untracked"), l("b", "small"), l("b", "untracked")],
            {"a": 5.0, "b": 5.0, "small": 10.0},
        )
        assert r.filled.get("untracked", 0.0) == pytest.approx(0.0)

    def test_infinity_state_is_not_memoised(self):
        # If Infinity were cached, the second remaining connection would also
        # absorb everything and the total would exceed the sources.
        r = allocate(
            [n("a", 0), n("b", 0), n("untracked", 1, type="remaining_parent_state")],
            [l("a", "untracked"), l("b", "untracked")],
            {"a": 4.0, "b": 6.0},
        )
        assert r.filled["untracked"] == pytest.approx(10.0)
        assert r.state("a", "untracked") + r.state("b", "untracked") == pytest.approx(10.0)

    def test_remaining_parent_state_recursion_pre_resolves_siblings(self):
        r = allocate(
            [n("a", 0), n("b", 0), n("house", 1),
             n("untracked", 1, type="remaining_parent_state")],
            [l("a", "house"), l("a", "untracked"), l("b", "house"), l("b", "untracked")],
            {"a": 10.0, "b": 10.0, "house": 18.0},
        )
        # b->house was forced to resolve before a->untracked, so b filled house
        # to capacity and only b's own leftover reached untracked.
        assert r.state("b", "house") == pytest.approx(8.0)
        assert r.state("b", "untracked") == pytest.approx(2.0)
        assert r.state("a", "untracked") == pytest.approx(0.0)


class TestForcedRecalcBranch:
    """The bundle's re-entry guard covers only half of its own condition.

        t.ready = !0,
        ( !n && "remaining_parent_state"===g.type && (add||sub) && y===1/0
          || "remaining_child_state"===_.type && (add||sub) && m===1/0
        ) && (i.set(...), e.set(...), this._calcConnection(t, e, i, !0))

    `&&` binds tighter than `||`, so `!n` guards the first disjunct only. A
    remaining_child_state *parent* carrying add/subtract entities re-enters
    itself with force=true, recomputes the same infinite parent remainder, and
    recurses again. In the browser that is a stack overflow.
    """

    def test_remaining_parent_state_branch_is_guarded_and_terminates(self):
        r = allocate(
            [n("a", 0), n("b", 0), n("house", 1),
             n("un", 1, type="remaining_parent_state", add_entities=["pad"])],
            [l("a", "house"), l("a", "un"), l("b", "house"), l("b", "un")],
            {"a": 10.0, "b": 10.0, "house": 18.0, "pad": 5.0},
        )
        assert not any(w["kind"] == "recursion_limit" for w in r.warnings)
        assert r.filled["un"] == pytest.approx(2.0)

    def test_remaining_child_state_branch_is_unguarded(self):
        r = allocate(
            [n("a", 0),
             n("mid", 1, type="remaining_child_state", subtract_entities=["pad"]),
             n("x", 2), n("y", 2)],
            [l("a", "mid"), l("mid", "x"), l("mid", "y")],
            {"a": 10.0, "x": 4.0, "y": 9.0, "pad": 1.0},
        )
        assert any(w["kind"] == "recursion_limit" for w in r.warnings)

    def test_remaining_child_state_without_extras_does_not_recurse(self):
        r = allocate(
            [n("a", 0), n("mid", 1, type="remaining_child_state"), n("x", 2), n("y", 2)],
            [l("a", "mid"), l("mid", "x"), l("mid", "y")],
            {"a": 10.0, "x": 4.0, "y": 9.0},
        )
        assert r.warnings == []


# --------------------------------------------------------------------------
# 7. add_entities / subtract_entities
# --------------------------------------------------------------------------


class TestAddSubtractEntities:
    def test_add_entities_raises_the_node_state(self):
        r = allocate(
            [n("a", 0), n("b", 1, add_entities=["extra"])],
            [l("a", "b")],
            {"a": 10.0, "b": 4.0, "extra": 3.0},
        )
        assert states_of(r) == [7.0]

    def test_multiple_add_entities_accumulate(self):
        r = allocate(
            [n("a", 0), n("b", 1, add_entities=["e1", "e2"])],
            [l("a", "b")],
            {"a": 10.0, "b": 1.0, "e1": 2.0, "e2": 3.0},
        )
        assert states_of(r) == [6.0]

    def test_subtract_entities_lowers_the_node_state(self):
        r = allocate(
            [n("a", 0), n("b", 1, subtract_entities=["car"])],
            [l("a", "b")],
            {"a": 30.0, "b": 20.0, "car": 5.3},
        )
        assert states_of(r) == [pytest.approx(14.7)]

    def test_subtract_is_clamped_and_cannot_go_negative(self):
        r = allocate(
            [n("a", 0), n("b", 1, subtract_entities=["car"])],
            [l("a", "b")],
            {"a": 30.0, "b": 4.0, "car": 9.0},
        )
        assert states_of(r) == [0.0]

    def test_subtract_exactly_equal_gives_zero(self):
        r = allocate(
            [n("a", 0), n("b", 1, subtract_entities=["car"])],
            [l("a", "b")],
            {"a": 30.0, "b": 4.0, "car": 4.0},
        )
        assert states_of(r) == [0.0]

    def test_successive_subtracts_clamp_independently(self):
        r = allocate(
            [n("a", 0), n("b", 1, subtract_entities=["c1", "c2"])],
            [l("a", "b")],
            {"a": 30.0, "b": 5.0, "c1": 4.0, "c2": 4.0},
        )
        # 5 - min(4,5) = 1, then 1 - min(4,1) = 0.
        assert states_of(r) == [0.0]

    def test_add_is_applied_before_subtract(self):
        r = allocate(
            [n("a", 0), n("b", 1, add_entities=["plus"], subtract_entities=["minus"])],
            [l("a", "b")],
            {"a": 30.0, "b": 2.0, "plus": 5.0, "minus": 6.0},
        )
        # (2 + 5) - 6 = 1.  If subtract ran first it would clamp to 0 then add 5.
        assert states_of(r) == [1.0]

    def test_subtract_works_on_a_node_that_also_has_outgoing_links(self):
        """CLAUDE.md: verified in the bundle at offset 62236, clamped at zero."""
        r = allocate(
            [n("supply", 0), n("house", 1, subtract_entities=["tesla"]), n("rest", 2)],
            [l("supply", "house"), l("house", "rest")],
            {"supply": 30.0, "house": 20.0, "tesla": 5.3, "rest": 30.0},
        )
        assert r.state("supply", "house") == pytest.approx(14.7)
        assert r.state("house", "rest") == pytest.approx(14.7)

    def test_add_entities_missing_raises_in_strict_mode(self):
        with pytest.raises(MissingEntityError):
            allocate(
                [n("a", 0), n("b", 1, add_entities=["nope"])],
                [l("a", "b")],
                {"a": 1.0, "b": 1.0},
                ignore_missing_entities=False,
            )

    def test_subtract_entities_missing_is_zero_when_ignoring(self):
        r = allocate(
            [n("a", 0), n("b", 1, subtract_entities=["nope"])],
            [l("a", "b")],
            {"a": 5.0, "b": 5.0},
        )
        assert states_of(r) == [5.0]

    def test_add_entities_on_a_remaining_parent_state_node(self):
        # `xt` in the bundle guards only high/low_carbon_energy, so add/subtract
        # do apply to remaining_* nodes.
        r = allocate(
            [n("a", 0), n("house", 1),
             n("untracked", 1, type="remaining_parent_state", add_entities=["pad"])],
            [l("a", "house"), l("a", "untracked")],
            {"a": 10.0, "house": 7.0, "pad": 1.0},
        )
        assert r.state("a", "untracked") == pytest.approx(3.0)

    def test_filters_multiply_divide_offset(self):
        r = allocate(
            [n("a", 0), n("b", 1, filters=[{"multiply": 2}, {"offset": 1}])],
            [l("a", "b")],
            {"a": 100.0, "b": 4.0},
        )
        assert states_of(r) == [9.0]

    def test_filter_divide(self):
        r = allocate(
            [n("a", 0, filters=[{"divide": 1000}]), n("b", 1)],
            [l("a", "b")],
            {"a": 4200.0, "b": 100.0},
        )
        assert states_of(r) == [4.2]


# --------------------------------------------------------------------------
# 8. invariants
# --------------------------------------------------------------------------


SCENARIOS = [
    ("repo_good", repo_nodes(), repo_links(), _repo_states()),
    (
        "repo_bad_order",
        [
            n("solar", 0),
            n("grid_import", 0),
            n("battery_discharge", 0),
            n("house", 1),
            n("battery_charge", 1),
            n("grid_export", 1),
        ],
        repo_links(),
        _repo_states(),
    ),
    ("four_section", four_section_nodes(), four_section_links(), four_section_states()),
    (
        "starved_export",
        [n("solar", 0), n("house", 1), n("battery_charge", 1), n("grid_export", 1)],
        [l("solar", "battery_charge"), l("solar", "house"), l("solar", "grid_export")],
        {"solar": 16.3, "house": 20.0, "battery_charge": 4.5, "grid_export": 5.1},
    ),
    (
        "capped_links",
        [n("a", 0), n("b", 1), n("c", 1)],
        [l("a", "b", "cap1"), l("a", "c", "cap2")],
        {"a": 10.0, "b": 8.0, "c": 8.0, "cap1": 3.0, "cap2": 2.0},
    ),
    (
        "all_zero",
        [n("a", 0), n("b", 1)],
        [l("a", "b")],
        {"a": 0.0, "b": 0.0},
    ),
    (
        "chain",
        [n("a", 0), n("b", 1), n("c", 2)],
        [l("a", "b"), l("b", "c")],
        {"a": 5.0, "b": 4.0, "c": 3.0},
    ),
]

SCENARIO_IDS = [s[0] for s in SCENARIOS]


@pytest.mark.parametrize("name,nodes,links,states", SCENARIOS, ids=SCENARIO_IDS)
class TestInvariants:
    def test_no_negative_connection_state(self, name, nodes, links, states):
        r = allocate(nodes, links, states)
        assert all(c["state"] >= 0 for c in r)

    def test_no_source_overspends(self, name, nodes, links, states):
        r = allocate(nodes, links, states)
        for node_id, spent in r.spent.items():
            declared = states.get(node_id)
            if declared is None:
                continue
            assert spent <= declared + 1e-9

    def test_no_sink_overfills(self, name, nodes, links, states):
        r = allocate(nodes, links, states)
        for node_id, filled in r.filled.items():
            declared = states.get(node_id)
            if declared is None:
                continue
            assert filled <= declared + 1e-9

    def test_total_allocated_is_bounded_by_both_sides(self, name, nodes, links, states):
        r = allocate(nodes, links, states)
        parents = {c["parent"] for c in r}
        children = {c["child"] for c in r}
        source_total = sum(states.get(p, 0.0) for p in parents)
        sink_total = sum(states.get(c, 0.0) for c in children)
        allocated = sum(c["state"] for c in r)
        assert allocated <= min(source_total, sink_total) + 1e-9

    def test_spent_equals_the_sum_of_outgoing_states(self, name, nodes, links, states):
        r = allocate(nodes, links, states)
        for node_id, spent in r.spent.items():
            outgoing = sum(c["state"] for c in r if c["parent"] == node_id)
            assert spent == pytest.approx(outgoing)

    def test_filled_equals_the_sum_of_incoming_states(self, name, nodes, links, states):
        r = allocate(nodes, links, states)
        for node_id, filled in r.filled.items():
            incoming = sum(c["state"] for c in r if c["child"] == node_id)
            assert filled == pytest.approx(incoming)

    def test_deterministic_across_repeats(self, name, nodes, links, states):
        first = pairs(allocate(nodes, links, states))
        for _ in range(4):
            assert pairs(allocate(nodes, links, states)) == first

    def test_inputs_are_not_mutated(self, name, nodes, links, states):
        before_nodes = [dict(x) for x in nodes]
        before_links = [dict(x) for x in links]
        before_states = dict(states)
        allocate(nodes, links, states)
        assert nodes == before_nodes
        assert links == before_links
        assert states == before_states


# --------------------------------------------------------------------------
# 9. determinism and numeric edges
# --------------------------------------------------------------------------


class TestDeterminism:
    def test_repeated_allocation_is_identical(self):
        runs = [pairs(allocate(repo_nodes(), repo_links(), _repo_states())) for _ in range(10)]
        assert all(run == runs[0] for run in runs)

    def test_result_is_a_plain_list_of_the_contract_shape(self):
        r = allocate(repo_nodes(), repo_links(), _repo_states())
        assert isinstance(r, list)
        assert all(set(c) == {"parent", "child", "state"} for c in r)

    def test_no_shared_state_between_calls(self):
        first = allocate(repo_nodes(), repo_links(), _repo_states())
        allocate(four_section_nodes(), four_section_links(), four_section_states())
        second = allocate(repo_nodes(), repo_links(), _repo_states())
        assert pairs(first) == pairs(second)


class TestNumericEdges:
    def test_string_states_are_coerced_like_javascript(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"a": "5.0", "b": "3"})
        assert states_of(r) == [3.0]

    def test_unavailable_source_zeroes_the_connection(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")],
                     {"a": "unavailable", "b": 3.0})
        # NaN is falsy in JS, so the `if (m && y)` guard fails: state 0, not NaN.
        assert states_of(r) == [0.0]

    def test_unavailable_sink_zeroes_the_connection(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")],
                     {"a": 3.0, "b": "unknown"})
        assert states_of(r) == [0.0]

    def test_unavailable_source_does_not_poison_its_other_links(self):
        r = allocate(
            [n("a", 0), n("b", 1), n("c", 1)],
            [l("a", "b"), l("a", "c")],
            {"a": "unavailable", "b": 3.0, "c": 3.0},
        )
        assert states_of(r) == [0.0, 0.0]

    def test_negative_source_state_is_treated_as_empty(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"a": -5.0, "b": 3.0})
        assert states_of(r) == [0.0]

    def test_negative_sink_state_is_treated_as_full(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"a": 5.0, "b": -3.0})
        assert states_of(r) == [0.0]

    def test_missing_source_entity_raises_in_strict_mode(self):
        with pytest.raises(MissingEntityError):
            allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"b": 1.0},
                     ignore_missing_entities=False)

    def test_missing_source_entity_is_zero_when_ignoring(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"b": 1.0})
        assert states_of(r) == [0.0]

    def test_entity_id_overrides_node_id_for_lookup(self):
        r = allocate(
            [n("a", 0, entity_id="sensor.real"), n("b", 1)],
            [l("a", "b")],
            {"sensor.real": 4.0, "b": 9.0},
        )
        assert states_of(r) == [4.0]

    def test_tiny_floats_do_not_round_to_zero(self):
        r = allocate([n("a", 0), n("b", 1)], [l("a", "b")], {"a": 0.001, "b": 5.0})
        assert states_of(r) == [0.001]

    def test_state_lookup_is_memoised_per_call_not_across_calls(self):
        st = {"a": 5.0, "b": 5.0}
        first = allocate([n("a", 0), n("b", 1)], [l("a", "b")], st)
        st["a"] = 1.0
        second = allocate([n("a", 0), n("b", 1)], [l("a", "b")], st)
        assert states_of(first) == [5.0]
        assert states_of(second) == [1.0]


# --------------------------------------------------------------------------
# 10. shapes the repository actually cares about
# --------------------------------------------------------------------------


class TestRepositoryShapes:
    def test_house_splits_into_tesla_ac_and_rest(self):
        nodes = [
            n("solar", 0),
            n("house", 1),
            n("tesla", 2),
            n("aircon", 2),
            n("rest", 2, type="remaining_parent_state"),
        ]
        links = [
            l("solar", "house"),
            l("house", "tesla"),
            l("house", "aircon"),
            l("house", "rest"),
        ]
        r = allocate(
            nodes, links,
            {"solar": 25.0, "house": 20.0, "tesla": 8.4, "aircon": 0.13},
        )
        assert r.state("house", "tesla") == pytest.approx(8.4)
        assert r.state("house", "aircon") == pytest.approx(0.13)
        assert r.state("house", "rest") == pytest.approx(20.0 - 8.4 - 0.13)

    def test_solar_reaches_the_battery_before_the_house(self):
        # CLAUDE.md: do not move solar->house ahead of solar->battery_charge.
        r = allocate(repo_nodes(), repo_links(), _repo_states())
        assert r.state("solar", "battery_charge") > 0

    def test_moving_solar_to_house_first_starves_the_battery_link(self):
        links = [
            l("solar", "grid_export"),
            l("solar", "house"),
            l("solar", "battery_charge"),
            l("grid_import", "house"),
            l("grid_import", "battery_charge"),
            l("battery_discharge", "house"),
        ]
        r = allocate(repo_nodes(), links, _repo_states())
        assert r.state("solar", "battery_charge") == pytest.approx(0.0)

    def test_no_battery_to_grid_link_can_be_drawn_without_declaring_one(self):
        r = allocate(repo_nodes(), repo_links(), _repo_states())
        assert ("battery_discharge", "grid_export") not in [
            (c["parent"], c["child"]) for c in r
        ]
