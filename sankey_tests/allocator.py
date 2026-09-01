"""Faithful Python port of ha-sankey-chart 6.3.0's connection allocator.

Ported by reading the minified bundle, not from memory. The pieces ported, and
the minified names they came from:

    Te(nodes, links)              auto-passthrough synthesis for multi-hop links
    Pe(nodes, links, sections)    nodes+links -> sections[].entities[].children[]
    _updateConnections()          sections -> ordered `connections` list
    _calcConnections()            fresh spent/filled maps, one pass in order
    _calcConnection(c,e,i,n)      the greedy rule itself
    _getEntityState(t)            remaining_parent_state / remaining_child_state
    _getMemoizedState(t)          filters, add_entities, subtract_entities, cache

The rule, verbatim from the bundle:

    const m = Math.max(0, p - t.prevParentState);      // parent remainder
    const y = Math.max(0, f - t.prevChildState);       // child remainder
    if (m && y) {
      if (t.connection_entity_id) t.state = Math.min(m, y, e);
      else                        t.state = Math.min(m, y);
      i.set(_, t.prevParentState + t.state);
      e.set(g, t.prevChildState + t.state);
    } else t.state = 0;

Iteration order is sections ascending -> the `nodes` array order within a
section -> that node's links in `links` declaration order. `sort_by` repaints
afterwards and cannot change allocation.

Deliberate deviations from the bundle, all of them things that cannot affect
the resolved numbers for this repository's config:

  * `je()` unit-prefix conversion is the identity here. `states` is already a
    dict of plain floats, so there is nothing to scale.
  * `_findRelatedRealEntity` is not ported. In the bundle it only supplies
    attributes (unit_of_measurement) to a remaining_* node; the node's `state`
    is the reduce() result either way.
  * `_reconcileConnections` (the `parents_sum` / `children_sum` options) is not
    ported. It is a no-op unless a node declares one of those keys, and this
    repository's config declares neither.
  * Passthrough node *display* state is not computed. Passthrough nodes are
    never a connection endpoint, so they are outside the allocation.
  * The bundle's forced-recalc branch can recurse without bound (see
    RECURSION_LIMIT below). We cap it and record a warning instead of
    overflowing the stack.

JavaScript semantics that had to be reproduced explicitly, because Python
disagrees with them:

  * `Math.min`/`Math.max` are NaN-poisoning; Python's min/max silently ignore
    NaN depending on argument order. See _js_min / _js_max.
  * `if (m && y)` is JS truthiness: 0 is falsy and **NaN is falsy**, so an
    `unavailable` sensor on an endpoint zeroes the connection rather than
    poisoning it, while an `unavailable` sensor on a link *value* poisons it.
  * `?? 0` catches null/undefined only, so NaN passes straight through.
"""

import math

__all__ = [
    "allocate",
    "Allocation",
    "Connection",
    "MissingEntityError",
    "InvalidConfigError",
    "RECURSION_LIMIT",
]

# The bundle's forced-recalc branch is `(!n && A) || B` -- `&&` binds tighter
# than `||`, so the `!n` re-entry guard covers only the first disjunct. Branch B
# (a remaining_child_state *parent* carrying add/subtract entities, with an
# infinite parent remainder) can therefore recurse into itself forever. We stop
# and record a warning rather than blowing the stack.
RECURSION_LIMIT = 64

NAN = float("nan")
INF = math.inf


class MissingEntityError(KeyError):
    """Bundle: throw new Error('Entity not found "' + e + '"')."""


class InvalidConfigError(ValueError):
    """Bundle: missing_child, or 'Invalid entity config' from _getEntityState."""


# --------------------------------------------------------------------------
# JS numeric semantics
# --------------------------------------------------------------------------


def _is_nan(x):
    return x != x


def _js_min(*vals):
    """Math.min: any NaN argument makes the whole result NaN."""
    out = INF
    for v in vals:
        if _is_nan(v):
            return NAN
        if v < out:
            out = v
    return out


def _js_max(*vals):
    """Math.max: any NaN argument makes the whole result NaN."""
    out = -INF
    for v in vals:
        if _is_nan(v):
            return NAN
        if v > out:
            out = v
    return out


def _truthy(x):
    """JS truthiness for a number: 0, -0 and NaN are falsy."""
    return not (x == 0 or _is_nan(x))


def _num(value):
    """JS Number(state). Non-numeric strings ('unavailable') give NaN."""
    if isinstance(value, bool):
        return 1.0 if value else 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if value is None:
        return NAN
    try:
        text = str(value).strip()
        if text == "":
            return 0.0
        return float(text)
    except (TypeError, ValueError):
        return NAN


# --------------------------------------------------------------------------
# Node / connection records
# --------------------------------------------------------------------------

class _Node:
    """One entry of the card's `sections[].entities[]`.

    Identity, not id, is the map key in the bundle (`connectionsByParent` is a
    Map keyed by the entity object), so this class deliberately keeps default
    object identity/hash. Two nodes sharing an id in different sections are two
    distinct allocation participants.
    """

    def __init__(self, raw):
        self.raw = dict(raw)
        self.id = raw.get("id") or raw.get("entity_id")
        self.section = raw.get("section") or 0
        self.type = raw.get("type") or "entity"
        self.entity_id = raw.get("entity_id")
        self.add_entities = list(raw.get("add_entities") or [])
        self.subtract_entities = list(raw.get("subtract_entities") or [])
        self.filters = list(raw.get("filters") or [])
        self.children = []

    def copy_for_section(self):
        """Pe() shallow-copies each node and gives the copy an empty children."""
        return _Node(self.raw)

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<_Node %s s%s %s>" % (self.id, self.section, self.type)


class Connection:
    """One resolved parent -> child ribbon."""

    __slots__ = (
        "parent",
        "child",
        "state",
        "prev_parent_state",
        "prev_child_state",
        "ready",
        "calculating",
        "passthroughs",
        "connection_entity_id",
    )

    def __init__(self, parent, child, passthroughs, connection_entity_id):
        self.parent = parent
        self.child = child
        self.state = 0.0
        self.prev_parent_state = 0.0
        self.prev_child_state = 0.0
        self.ready = False
        self.calculating = False
        self.passthroughs = passthroughs
        self.connection_entity_id = connection_entity_id

    def as_dict(self):
        return {"parent": self.parent.id, "child": self.child.id, "state": self.state}

    def __repr__(self):  # pragma: no cover - debugging aid
        return "<Connection %s->%s %s>" % (self.parent.id, self.child.id, self.state)


class Allocation(list):
    """The ordered `[{parent, child, state}]` the contract asks for.

    It is a plain list, so it compares and iterates exactly as the contract
    specifies. The extra attributes carry what the card would have *drawn* but
    the contract's return shape has no room for:

      warnings     -- list of dicts. Every link whose section span is not
                      exactly 1, plus any recursion-limit hit. This is the
                      known-bad class from CLAUDE.md: a span > 1 makes the card
                      synthesise an unlabelled ghost box coloured like its
                      target, which is what once made the battery appear to
                      charge itself.
      ghost_nodes  -- ids of the passthrough nodes the card would synthesise.
      connections  -- the rich Connection objects, including `passthroughs`.
      nodes        -- the post-synthesis node list (raw dicts).
      links        -- the post-synthesis link list (raw dicts), with the
                      rewritten targets Te() produces.
      spent        -- {node id: total allocated out of it}
      filled       -- {node id: total allocated into it}
    """

    def __init__(self, iterable=()):
        super().__init__(iterable)
        self.warnings = []
        self.ghost_nodes = []
        self.connections = []
        self.nodes = []
        self.links = []
        self.spent = {}
        self.filled = {}

    @property
    def has_ghosts(self):
        """True if the card would synthesise a passthrough box for this graph.

        A ghost is always a bug in the layout, never in the data: it means a
        link spans more than one section. Kept as a property so it cannot
        drift out of step with ghost_nodes.
        """
        return bool(self.ghost_nodes)

    def by_pair(self, parent, child):
        """All resolved states for one parent->child pair, in order."""
        return [c["state"] for c in self if c["parent"] == parent and c["child"] == child]

    def state(self, parent, child):
        """The single resolved state for one pair. Raises if not exactly one."""
        found = self.by_pair(parent, child)
        if len(found) != 1:
            raise KeyError("expected one %s->%s connection, found %d" % (parent, child, len(found)))
        return found[0]


# --------------------------------------------------------------------------
# Config normalisation: Te() then Pe()
# --------------------------------------------------------------------------


def _me(child):
    """Bundle: function Me(t){return "string"==typeof t?t:t.id||t.entity_id}."""
    if isinstance(child, str):
        return child
    return child.get("id") or child.get("entity_id")


def _section_of(raw):
    return raw.get("section") or 0


def _span_warnings(nodes_raw, links_raw):
    """Report every link whose section span is not exactly 1.

    The bundle only *acts* on span > 1 (`if(h-d<=1)continue`), synthesising a
    passthrough per intermediate section. Span 0 and negative spans are left
    alone and render as an intra-section or backwards ribbon. All three are
    reported here, because all three are configuration mistakes in a layered
    Sankey and only one of them announces itself on screen.
    """
    by_id = {}
    for raw in nodes_raw:
        by_id.setdefault(raw.get("id") or raw.get("entity_id"), raw)
    out = []
    for index, link in enumerate(links_raw):
        source = by_id.get(link["source"])
        target = by_id.get(link["target"])
        if source is None or target is None:
            out.append(
                {
                    "kind": "dangling_link",
                    "link_index": index,
                    "source": link["source"],
                    "target": link["target"],
                    "detail": "link endpoint is not in `nodes`; the card silently skips it",
                }
            )
            continue
        span = _section_of(target) - _section_of(source)
        if span == 1:
            continue
        if span > 1:
            kind = "ghost_passthrough"
            detail = (
                "spans %d sections; the card synthesises %d unlabelled passthrough "
                "box(es) coloured like the target" % (span, span - 1)
            )
        elif span == 0:
            kind = "same_section"
            detail = "source and target share section %d" % _section_of(source)
        else:
            kind = "backwards"
            detail = "target sits %d section(s) before the source" % (-span)
        out.append(
            {
                "kind": kind,
                "link_index": index,
                "source": link["source"],
                "target": link["target"],
                "span": span,
                "detail": detail,
            }
        )
    return out


def _omit(raw, keys):
    return {k: v for k, v in raw.items() if k not in keys}


def _synthesise_passthroughs(nodes_raw, links_raw):
    """Port of Te(). Mutates copies of nodes_raw / links_raw in place.

        for (let a = 0; a < r; a++) {          // r = links.length, snapshotted
          ...
          if (h - d <= 1) continue;
          for (let i = d + 1; i < h; i++) { ...push `${target}__passthrough_${i}__auto` }
          r.target = _[0];
          for (...) if (!exists) links.push({source: _[t], target: _[t+1]});
          if (!exists) links.push({source: last, target: target});
        }
    """
    ghosts = []

    def has_link(source, target):
        return any(l["source"] == source and l["target"] == target for l in links_raw)

    original_count = len(links_raw)
    for index in range(original_count):
        link = links_raw[index]
        source = next((n for n in nodes_raw if n.get("id") == link["source"]), None)
        target = next((n for n in nodes_raw if n.get("id") == link["target"]), None)
        if source is None or target is None:
            continue
        low = _section_of(source)
        high = _section_of(target)
        if high - low <= 1:
            continue
        target_id = link["target"]
        chain = []
        for section in range(low + 1, high):
            ghost_id = "%s__passthrough_%d__auto" % (target_id, section)
            if not any(n.get("id") == ghost_id for n in nodes_raw):
                ghost = _omit(target, ("id", "section", "type"))
                ghost.update({"id": ghost_id, "section": section, "type": "passthrough"})
                nodes_raw.append(ghost)
                ghosts.append(ghost_id)
            chain.append(ghost_id)
        link["target"] = chain[0]
        for i in range(len(chain) - 1):
            if not has_link(chain[i], chain[i + 1]):
                links_raw.append({"source": chain[i], "target": chain[i + 1]})
        if not has_link(chain[-1], target_id):
            links_raw.append({"source": chain[-1], "target": target_id})
    return ghosts


def _build_sections(nodes_raw, links_raw):
    """Port of Pe(). Returns [(section_index, [_Node, ...]), ...] ascending."""
    by_section = {}
    originals = [_Node(raw) for raw in nodes_raw]
    for node in originals:
        by_section.setdefault(node.section, []).append(node.copy_for_section())

    for link in links_raw:
        source = next((n for n in originals if n.id == link["source"]), None)
        if source is None:
            continue
        holder = next(
            (n for n in by_section.get(source.section, []) if n.id == link["source"]), None
        )
        if holder is None:
            continue
        value = link.get("value")
        if value:
            holder.children.append({"entity_id": link["target"], "connection_entity_id": value})
        else:
            holder.children.append(link["target"])

    return [(index, by_section[index]) for index in sorted(by_section)]


def _build_connections(sections):
    """Port of _updateConnections(). Order: sections -> nodes -> children."""
    by_id = {}
    for _index, entities in sections:
        for entity in entities:
            by_id[entity.id] = entity

    connections = []
    by_parent = {}
    by_child = {}
    for _index, entities in sections:
        for entity in entities:
            if entity.type == "passthrough":
                # The bundle `return`s out of the forEach callback here, so a
                # passthrough node never becomes a connection parent.
                continue
            for child_ref in entity.children:
                passthroughs = []
                target = by_id.get(_me(child_ref))
                while target is not None and target.type == "passthrough":
                    passthroughs.append(target)
                    nxt = target.children[0] if target.children else None
                    if nxt is None:
                        raise InvalidConfigError("missing child " + str(_me(child_ref)))
                    target = by_id.get(_me(nxt))
                if target is None:
                    raise InvalidConfigError("missing child " + str(_me(child_ref)))
                connection_entity_id = (
                    child_ref.get("connection_entity_id")
                    if isinstance(child_ref, dict)
                    else None
                )
                connection = Connection(entity, target, passthroughs, connection_entity_id)
                connections.append(connection)
                by_parent.setdefault(id(entity), []).append(connection)
                by_child.setdefault(id(target), []).append(connection)
    return connections, by_parent, by_child


# --------------------------------------------------------------------------
# The allocator
# --------------------------------------------------------------------------


class _Allocator:
    def __init__(self, connections, by_parent, by_child, states, ignore_missing_entities):
        self.connections = connections
        self.by_parent = by_parent
        self.by_child = by_child
        self.states = states
        self.ignore_missing_entities = ignore_missing_entities
        # Cleared once per render() in the bundle, i.e. once per allocate().
        self.entity_states = {}
        self.warnings = []

    # -- state lookup -------------------------------------------------------

    def _raw_state(self, entity_id):
        if entity_id not in self.states:
            if self.ignore_missing_entities:
                return 0.0
            raise MissingEntityError('Entity not found "%s"' % entity_id)
        return _num(self.states[entity_id])

    def _get_entity_state(self, node):
        """Port of _getEntityState. Returns a float."""
        if isinstance(node, str):
            return self._raw_state(node)

        if node.type == "remaining_parent_state":
            incoming = self.by_child.get(id(node))
            if incoming is None:
                raise InvalidConfigError("Invalid entity config " + str(node.id))
            total = 0.0
            for connection in incoming:
                total = total + connection.state if connection.ready else INF
            return total

        if node.type == "remaining_child_state":
            outgoing = self.by_parent.get(id(node))
            if outgoing is None:
                if node.children:
                    raise InvalidConfigError("Invalid entity config " + str(node.id))
                return 0.0
            total = 0.0
            for connection in outgoing:
                total = total + connection.state if connection.ready else INF
            return total

        return self._raw_state(node.entity_id or node.id)

    @staticmethod
    def _memo_key(key):
        # The bundle's entityStates is a JS Map: keyed by value for the string
        # form (a connection_entity_id) and by object identity for a node.
        return key if isinstance(key, str) else id(key)

    def _get_memoized_state(self, key):
        """Port of _getMemoizedState. `key` is a _Node or a bare entity id."""
        memo_key = self._memo_key(key)
        cached = self.entity_states.get(memo_key)
        if cached is not None:
            return cached

        node = key if isinstance(key, _Node) else _Node({"id": key, "type": "entity"})
        state = self._get_entity_state(node)

        for spec in node.filters:
            if "multiply" in spec:
                state *= spec["multiply"]
            elif "divide" in spec:
                state /= spec["divide"]
            elif "offset" in spec:
                state += spec["offset"]

        # `xt` in the bundle is
        #     t => "high_carbon_energy"===t || "low_carbon_energy"===t
        # -- nothing to do with remaining_* types, so add/subtract apply to
        # remaining_parent_state and remaining_child_state nodes as well.
        if node.type not in ("high_carbon_energy", "low_carbon_energy"):
            for extra in node.add_entities:
                state += self._raw_state(extra)
            for extra in node.subtract_entities:
                # r.state -= Math.min(i, r.state) -- clamped, cannot go negative.
                state -= _js_min(self._raw_state(extra), state)

        if state == INF:
            # The bundle returns without caching an Infinity, so a
            # remaining_* node is recomputed on every touch until it settles.
            return state

        self.entity_states[memo_key] = state
        return state

    # -- the greedy rule ----------------------------------------------------

    def _calc_connection(self, connection, child_filled, parent_spent, force=False, depth=0):
        if connection.ready and not force:
            return

        if depth > RECURSION_LIMIT:
            self.warnings.append(
                {
                    "kind": "recursion_limit",
                    "source": connection.parent.id,
                    "target": connection.child.id,
                    "detail": (
                        "the bundle's forced-recalc branch `(!n && A) || B` does not "
                        "guard B against re-entry; this input would recurse without "
                        "bound in the browser"
                    ),
                }
            )
            return

        parent = connection.parent
        child = connection.child

        if not connection.calculating:
            connection.calculating = True
            for node in (parent, child):
                if node.type == "remaining_child_state":
                    for sibling in self.by_parent.get(id(node), []):
                        if sibling.ready:
                            continue
                        for other in self.by_child.get(id(sibling.child), []):
                            if other is not connection and not other.calculating:
                                self._calc_connection(other, child_filled, parent_spent, False, depth + 1)
                elif node.type == "remaining_parent_state":
                    for sibling in self.by_child.get(id(node), []):
                        if sibling.ready:
                            continue
                        for other in self.by_parent.get(id(sibling.parent), []):
                            if other is not connection and not other.calculating:
                                self._calc_connection(other, child_filled, parent_spent, False, depth + 1)

        parent_state = self._get_memoized_state(parent)
        connection.prev_parent_state = parent_spent.get(id(parent), 0.0)
        parent_remainder = _js_max(0.0, parent_state - connection.prev_parent_state)

        child_state = self._get_memoized_state(child)
        connection.prev_child_state = child_filled.get(id(child), 0.0)
        child_remainder = _js_max(0.0, child_state - connection.prev_child_state)

        if _truthy(parent_remainder) and _truthy(child_remainder):
            if connection.connection_entity_id:
                cap = self._get_memoized_state(connection.connection_entity_id)
                connection.state = _js_min(parent_remainder, child_remainder, cap)
            else:
                connection.state = _js_min(parent_remainder, child_remainder)
            parent_spent[id(parent)] = connection.prev_parent_state + connection.state
            child_filled[id(child)] = connection.prev_child_state + connection.state
        else:
            connection.state = 0.0

        connection.ready = True

        child_has_extras = bool(child.add_entities or child.subtract_entities)
        parent_has_extras = bool(parent.add_entities or parent.subtract_entities)
        retry = (
            not force
            and child.type == "remaining_parent_state"
            and child_has_extras
            and child_remainder == INF
        ) or (
            parent.type == "remaining_child_state"
            and parent_has_extras
            and parent_remainder == INF
        )
        if retry:
            parent_spent[id(parent)] = connection.prev_parent_state
            child_filled[id(child)] = connection.prev_child_state
            self._calc_connection(connection, child_filled, parent_spent, True, depth + 1)

    def run(self):
        child_filled = {}
        parent_spent = {}
        for connection in self.connections:
            connection.ready = False
            connection.calculating = False
        for connection in self.connections:
            self._calc_connection(connection, child_filled, parent_spent)
        return child_filled, parent_spent


# --------------------------------------------------------------------------
# Public entry point
# --------------------------------------------------------------------------


def allocate(nodes, links, states, ignore_missing_entities=True):
    """Resolve what ha-sankey-chart 6.3.0 would draw.

    nodes:  ordered list of {id, section?, type?, entity_id?, add_entities?,
                             subtract_entities?, filters?}
    links:  ordered list of {source, target, value?}   (value = entity id)
    states: {entity_id: float}

    Returns an `Allocation` -- a list of {parent, child, state} in the card's
    own resolution order, carrying `.warnings`, `.ghost_nodes`, `.spent`,
    `.filled` and the rich `.connections`.

    ignore_missing_entities mirrors the card option of the same name. The card
    defaults it off and throws; this port defaults it *on* (missing -> 0) so a
    test can probe a half-configured graph without an exception, and flips to
    the card's behaviour when set False.
    """
    nodes_raw = [dict(n) for n in nodes]
    links_raw = [dict(l) for l in links]

    warnings = _span_warnings(nodes_raw, links_raw)
    ghosts = _synthesise_passthroughs(nodes_raw, links_raw)

    sections = _build_sections(nodes_raw, links_raw)
    connections, by_parent, by_child = _build_connections(sections)

    allocator = _Allocator(connections, by_parent, by_child, states, ignore_missing_entities)
    child_filled, parent_spent = allocator.run()

    result = Allocation(c.as_dict() for c in connections)
    result.warnings = warnings + allocator.warnings
    result.ghost_nodes = ghosts
    result.connections = connections
    result.nodes = nodes_raw
    result.links = links_raw

    node_by_key = {}
    for _index, entities in sections:
        for entity in entities:
            node_by_key[id(entity)] = entity
    result.spent = {
        node_by_key[k].id: v for k, v in parent_spent.items() if k in node_by_key
    }
    result.filled = {
        node_by_key[k].id: v for k, v in child_filled.items() if k in node_by_key
    }
    return result
