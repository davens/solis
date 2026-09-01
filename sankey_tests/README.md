# Sankey attribution tests

Run:

    uv run --no-project --with pytest --with hypothesis python -m pytest sankey_tests -q

Two decoupled pieces, both defined in `contract.py`:

- `decompose.py` — instantaneous power to six directed flows (the physics).
- `allocator.py` — a faithful port of ha-sankey-chart 6.3.0's `_calcConnection`
  (the card), so we can predict the drawn ribbons before touching the dashboard.

Known open issue the suite exists to pin down: solar and battery are DC-side
while house and grid are AC-side, so the four live sensors cannot all be
reconciled. Understating a flow silently shrinks a ribbon; overstating one is
clamped harmlessly by the card. Weight accordingly.
