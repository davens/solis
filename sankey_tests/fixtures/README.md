# Replay fixtures

Everything here is captured data. `test_history_replay.py` runs entirely offline
from it; nothing in the test suite opens a socket (verified by running the suite
with `socket.socket` monkeypatched to raise).

| File | What |
|---|---|
| `history_<day>.json.gz` | Raw `/api/history/period` states for the four live power sensors over one Europe/London local day: `solar`, `battery`, `grid`, `house`. `[epoch_seconds, state_string]` pairs; `state_string` may be `unavailable`. |
| `counters.json` | The six inverter daily counters per day. `max` is the end-of-day value (they are `total_increasing` and reset at inverter local midnight, ~23:59:52 BST). |
| `flow_5min_2026-08-30.csv` | INDEPENDENT dataset from earlier work: 190 five-minute buckets, 2026-08-30 00:00-15:45. Every column but `tesla` is quantised to 0.1 kWh; `tesla` to a 1.2 kW quantum. Coarse sanity check only. |
| `capture.py` | Re-capture tool. Needs network + the HA long-lived token, which it reads in-process from `~/.claude.json` and never writes anywhere. |
| `replay.py` | The offline integrator. Pure stdlib. `uv run --no-project python fixtures/replay.py` prints the reconciliation table. |

## How and when

Captured **2026-08-30 ~15:10 UTC** from `http://homeassistant.local:8123`
(HA 2026.8.3, Europe/London) with:

    GET /api/history/period/<local-midnight>?filter_entity_id=<4 sensors>
        &end_time=<next local midnight>
        &minimal_response&no_attributes&significant_changes_only=0

`significant_changes_only=0` matters: without it HA drops samples it judges
insignificant and the integral loses energy.

Days are 2026-08-27, -28, -29 (complete) and 2026-08-30 (PARTIAL, up to the
capture instant). Those three are every complete day the recorder holds — the
`solis_solarman` integration only went live 2026-08-26 ~15:55 UTC.

Re-capture with:

    uv run --no-project python fixtures/capture.py 2026-08-31 2026-09-01

## Appliance channels (2026-10-11)

These captures contain neither air-con nor fridge power readings. The offline
replay holds both at zero to run the arithmetic and marks both blind, with
`aircon_assumed_s` and `fridge_assumed_s` covering the live window. House,
Air con and Fridge are refused individually. `house_and_aircon_fridge` is the
knowable total excluding Tesla; it keeps the counter reconciliations honest.
The synthetic DAY and BALANCED_PROFILE in the layout and chart tests do carry
nonzero fridge power. No recorded sample was backfilled.
