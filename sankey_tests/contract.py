"""Shared contract for the Sankey attribution work. Both halves build against this.

Two independent pieces, deliberately decoupled:

  decompose(...)  - the physics: instantaneous power -> six directed flows.
  allocate(...)   - the card: a faithful port of ha-sankey-chart 6.3.0's
                    _calcConnection, so we can predict the drawn ribbons.

Composing them answers the only question that matters: given our flow sensors,
does the chart draw the truth?
"""

# The six directed flows, in a fixed order. Keys are stable; tests key off these.
FLOWS = (
    "solar_to_house",
    "solar_to_battery",
    "solar_to_export",
    "battery_to_house",
    "grid_to_house",
    "grid_to_battery",
)

# Sign conventions, as the HA integration exposes them (NOT the raw registers):
#   solar   >= 0        W, DC side
#   battery >  0 charging, < 0 discharging   W, DC side
#   grid    >  0 importing, < 0 exporting    W, AC side
#   house   >= 0        W, AC side
SIGNS = {
    "solar": "non-negative, DC",
    "battery": "positive = charging, DC",
    "grid": "positive = importing, AC",
    "house": "non-negative, AC",
}


def decompose(solar, battery, grid, house):
    """Instantaneous W -> {flow: W}. Every value must be >= 0.

    Implemented in decompose.py. Must satisfy every invariant in
    test_invariants.py regardless of which DC/AC loss treatment is chosen.
    """
    raise NotImplementedError


def allocate(nodes, links, states):
    """Faithful port of ha-sankey-chart 6.3.0 _calcConnection.

    nodes: ordered list of dicts {id, section, type?}
    links: ordered list of dicts {source, target, value?}   value = entity id
    states: {entity_id: float}

    Returns an ordered list of {parent, child, state}, matching what the card
    would resolve. The rule the port must reproduce exactly:

        parent_remainder = max(0, parent_state - already_spent[parent])
        child_remainder  = max(0, child_state  - already_filled[child])
        if parent_remainder and child_remainder:
            state = min(parent_remainder, child_remainder, value_state)  # value optional
        else:
            state = 0

    Iteration order is sections -> nodes order -> that node's links in order.
    Implemented in allocator.py.
    """
    raise NotImplementedError
