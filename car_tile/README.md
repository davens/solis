# Overview Car tile: options and tooling

The Overview "Tesla" tile is one `custom:button-card` (view 0 / section 1 / card 3) whose whole body is the
JavaScript template in its `custom_fields.w`. This folder holds every designed version of that template, a
harness to render them without Home Assistant, and the script that swaps one onto the live dashboard.

Designs are compared side by side on the "Car tile options" canvas:
https://claude.ai/artifact/TVDZ9NNQAQKt44AJKyc7nT (private to the owner).

| id | name | map | garage door |
|---|---|---|---|
| `original.js` | the tile before 2026-09-24 | none | none |
| `quiet-a` | Header door | none; "4.9 mi NE" row when away | header, beside the lock |
| `quiet-b` | Place row | none; permanent "Home" row | inside the place row |
| `inset-a` | Home stage | always-on 52 px strip | amber info row |
| `inset-b` | Away puck | round puck, away only | header |
| `backdrop-a` | Map surface | full-tile map, always | amber chip (adds a row while open) |
| **`backdrop-b`** | **Away map fade -- LIVE since 2026-09-24** | right-side fade, away only | header |

`backdrop-b` was changed after the canvas render: distances are miles, to match the range and speed. The
inset options still say km; convert them the same way (`fmtDist`) before applying one.

## Switching

    uv run --with websockets python apply_tile.py options/<id>/template.js card_extra.json

It backs the whole Overview config up to `backups/` first. `card_extra.json` sets two card-level keys every
option can share: `styles.custom_fields.w` with `overflow: visible`, and `hold_action` -> more-info of
`device_tracker.tesla_location` (HA's full map). **The overflow is load-bearing for the map options:**
button-card gives `#w` `overflow: hidden`, which clips a full-bleed map to the content box, 16 px short of
the card edge (verified 2026-09-24 by measuring the live DOM). `backups/overview_pre_garage_map.json` is
the dashboard before any of this.

## Rendering without HA

    ./render.sh options/<id>/template.js /tmp/out      # -> /tmp/out/preview.png, 4 scenarios x 360/470 px

`scenarios.js` layers four situations over `states_base.json` (a live snapshot). Scenario coordinates are
fictional (central Cambridge), never the owner's home. The harness renders the template in Node and
screenshots with headless Chrome; map tiles load from the network.

## Map tiles

OpenStreetMap standard tiles, darkened with a CSS filter on the mosaic. **Keyless CARTO basemaps are
watermarked "API KEY REQUIRED" on every tile** (verified 2026-09-24), so do not switch back to them without
a key. OSM's tile policy expects light use and attribution; each map option draws "© OpenStreetMap" at 8 px.

## Entities the options read

- `binary_sensor.garage_door` -- SONOFF SNZB-04P on ZHA, on = open; duration from `last_changed`.
- `device_tracker.tesla_location`, `sensor.tesla_heading` -- added 2026-09-24 as retained MQTT discovery
  configs (`homeassistant/device_tracker/teslamate_1/location/config`, `.../sensor/teslamate_1/heading/config`)
  on TeslaMate's `teslamate/cars/1/location` and `/heading` topics, on the existing "Tesla" MQTT device.
- `zone.home` for distance and bearing.

Every option degrades to exactly the original tile when those entities are missing or unavailable.
