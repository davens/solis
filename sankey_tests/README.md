# Sankey attribution tests

Run offline from the repository root with cached dependencies:

    UV_OFFLINE=1 uv run --no-project --with pytest --with jinja2 --with pyyaml --with hypothesis python -m pytest sankey_tests -q

The v2 chart uses `flows.py` (seven input channels, eighteen directed flows),
`layout_v2.py` (two columns, ten nodes), and `allocator.py` (a faithful port of
ha-sankey-chart 6.3.0). `contract.py` and the v1 tests remain as comparisons.
Generate the template YAML with `UV_OFFLINE=1 uv run --no-project python sankey_tests/gen_flow_yaml_v2.py`;
`--check` verifies that it matches the generator.

Tesla, Fridge and Air con are carved out of House in that order. A dead
TeslaMate unmoors twelve of eighteen flows; the common availability guard
still counts exactly five entities. Air con and fridge readings fall back to
zero in live templates, leaving those loads inside House when unavailable.
Fridge flows read Tesla only; air-con flows read Tesla and fridge. An hourly
air-con overshoot can consume only House rest. Tesla ribbons and Inverter
remain unchanged because Hr + T + F + A == H.

Recorded fixtures have neither air-con nor fridge power readings. Both are
held at zero for arithmetic and marked blind for reporting: House, Air con and Fridge
are refused individually, while House + Air con + Fridge remains the knowable
total excluding the car. Synthetic profiles exercise both appliance channels.

Solar and battery are DC-side while house and grid are AC-side. The Inverter
node carries the measured residual; no efficiency is fitted on the default
path. Overstated ribbons are clamped by the card, as the regression tests pin.
