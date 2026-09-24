# inset-b — "Away puck" (round map, away-only; garage in the header)

## Rationale
- At home the tile is exactly today's tile: no map, no placeholder. A map of your own driveway answers nothing.
- When `device_tracker.tesla_location` is anything but `home`, a 52 px round map "puck" appears beside the SOC block, centred on the car (dot; rotated heading arrow while driving; green while charging). The distance / zone name goes into the right-hand info column as a pin row ("7.9 km", "650 m", "Work · 3.2 km") — the geofence pin slot that never shows today, so the column's shape is familiar.
- Garage door belongs to the house, so it lives in the header with the lock: `Garage open · 40 min | Locked`, amber, duration from `last_changed`. Nothing at all when closed/unknown. Same place whether the car is home or away, and it never touches the hero row or the chip strip, so it costs zero height even in the busiest case.
- Everything the baseline computes is byte-identical (`template.js` = baseline logic verbatim + additions; see `build.js`/`additions.js`). With the new entities absent or unknown, the output equals the baseline byte for byte (verified for all 4 scenarios, `out/degrade.js`).

## Everyday height (measured, headless Chrome, same card chrome as the harness)
- Baseline: 144.6 px at 360 and 470; busiest case 181.3 / 163.3.
- inset-b: **144.6 / 144.6** in every everyday scenario; busiest case **181.3 / 163.3**. No change at all. The puck is 52 px + 1 px border, inside the ~55 px hero row.

## Card-level config recommended
- `hold_action: { action: more-info, entity: device_tracker.tesla_location }` — the puck is a locator, the hold opens HA's full map.
- Keep `tap_action` as is (more-info of `sensor.tesla_battery`).

## Map mechanics (for whoever ports this)
- OpenStreetMap standard tiles (`https://tile.openstreetmap.org/{z}/{x}/{y}.png`, 256 px, keyless, clean) as absolutely positioned `<img>`s inside an `overflow:hidden; border-radius:50%` box, mosaic anchored at the centre. A 52 px window needs 1–4 tiles.
- Darkened by CSS on the mosaic wrapper: `filter:invert(1) hue-rotate(180deg) brightness(.75) contrast(.9) saturate(.3)` (tuned down from brightness .85 / saturate .35 so the puck reads calmly next to the SOC number).
- Zoom chosen for design reasons only: z16 parked (~125 m across the puck — the street the car is on), z15 while driving (~250 m).
- OSM attribution: "©OSM" at 7.5 px, rgba(255,255,255,0.28), centred at the bottom of the puck (a circle has no corners), rendered only when the puck is drawn.
- No CARTO code remains (the watermark-dodging zoom picker is gone).

## Weaknesses (honest)
- 52 px of map is a locator, not a map: you can see "a junction near the park", maybe half a street name. It says "away, roughly here" and relies on the distance row for the number; the hold action is the real map.
- The header can get long at 360 px: "Garage open · 1 h 20 | Unlocked" plus the left group fits, but it is the tightest line in the design (no truncation is applied; it would push the TESLA label if the lock text were longer than "Unlocked").
- OSM has no @2x tiles, so on a 2× phone the puck is upscaled and slightly soft; OSM sends `cache-control: no-cache`, so tiles are revalidated on each render while away (verified with curl).
- The hero row looks different home vs away (puck appears/disappears). That is intentional, but it means the right column shifts left by 62 px when the car leaves.
- 1–4 tile fetches per render while away; nothing at home.
