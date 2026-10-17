# Sankey attribution tests

Run offline from the repository root with cached dependencies:

    UV_OFFLINE=1 uv run --no-project --with pytest --with jinja2 --with pyyaml --with hypothesis python -m pytest sankey_tests -q

The v2 chart uses `flows.py` (seven input channels, eighteen directed flows),
`layout_v2.py` (two columns, ten nodes), and `allocator.py` (a faithful port of
ha-sankey-chart 6.3.0). `contract.py` and the v1 tests remain as comparisons.
Generate the template YAML with `UV_OFFLINE=1 uv run --no-project python sankey_tests/gen_flow_yaml_v2.py`;
`--check` verifies that it matches the generator.

Updated 2026-10-17: House (33147) includes Tesla and Fridge, carved out in that
order. EPS (33148, `sensor.solis_inverter_backup_load`) is a separate measured AC
output feeding Air con, with unchanged `*_to_aircon` keys and helper ids.
`T = min(max(0, tesla), H)`, `F = min(max(0, fridge), H-T)`, `Hr = H-T-F`,
`A = max(0, eps)`, `L = max(0, S+B+G-(Hr+T+F+A+C+E))`. Total load is H+A.
The six-entity availability guard includes EPS, read without a float default.
Only fridge falls back to zero, leaving it inside House.

Recorded fixtures lack EPS and fridge readings. Both are held at zero for
arithmetic and marked blind. House + Fridge is knowable excluding the car;
House + Tesla + Fridge equals H even with a blind car. Without EPS, Air con,
Inverter and source-spent totals are refused. House and Fridge individually are
refused without fridge. Export and battery-charge totals remain knowable.
Synthetic profiles exercise both measured AC outputs.

The circuit identification (air con plus a small charger) is owner-stated;
separation from House and the register behaviour are owner-supplied verified
facts, not live checks performed by this offline suite. See `../docs/sankey.md`.

Solar and battery are DC-side while house and grid are AC-side. The Inverter
node carries the measured residual; no efficiency is fitted on the default
path. Overstated ribbons are clamped by the card, as the regression tests pin.
