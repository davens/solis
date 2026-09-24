# inset-a — "Home stage" (always-on rectangular map inset)

## Rationale
- A 52 px-tall rounded map strip sits in the hero row between the SOC block and the info column, always on. At home it is a fixed, quiet picture of the street captioned "Home"; the moment the car leaves, the picture changes and the caption becomes the distance ("7.9 km", "650 m", zone name if the tracker is in a named zone). The change of picture is the signal.
- Car marker: dot (white; green while charging); a rotated arrow from `sensor.tesla_heading` while driving (blue, like the rest of the driving palette).
- Garage door is treated as home information, not car information: when open it appears as an amber row at the top of the right-hand info column ("Garage open · 40 min", duration from `last_changed`), i.e. the slot the never-showing geofence pin row occupies today. Closed/unknown renders nothing. Same treatment at home and away, so it never has to be re-learned.
- Everything the baseline computes is byte-identical (`template.js` = baseline logic verbatim + additions; see `build.js`/`additions.js`). With the new entities absent or unknown, the output equals the baseline byte for byte (verified for all 4 scenarios, `out/degrade.js`).

## Everyday height (measured, headless Chrome, same card chrome as the harness)
- Baseline: 144.6 px at 360 and 470; busiest case 181.3 / 163.3.
- inset-a: **144.6 / 144.6** in every everyday scenario; busiest case **181.3 / 163.3**. No change at all. The map box is 52 px + 1 px border, inside the ~55 px hero row.

## Card-level config recommended
- `hold_action: { action: more-info, entity: device_tracker.tesla_location }` — the inset is a thumbnail; the hold opens HA's full map.
- Keep `tap_action` as is (more-info of `sensor.tesla_battery`).

## Map mechanics (for whoever ports this)
- OpenStreetMap standard tiles (`https://tile.openstreetmap.org/{z}/{x}/{y}.png`, 256 px, keyless, clean) as absolutely positioned `<img>`s inside an `overflow:hidden` box, mosaic anchored at the box centre, so the crop is correct at any card width (box is `flex:1`, min 64, max 150 px). Up to 2×2 tiles.
- Darkened by CSS on the mosaic wrapper: `filter:invert(1) hue-rotate(180deg) brightness(.75) contrast(.9) saturate(.3)`. Tuned down from brightness .85 / saturate .35 so the inset sits under the SOC number instead of competing with it; street names and building outlines stay legible at 52 px.
- Zoom is chosen for design reasons only: z16 at home and parked (~300 m across the 128 px box), z15 while driving (~600 m, enough context for a moving car).
- OSM attribution: "©OSM" at 7.5 px, rgba(255,255,255,0.28), top-right corner of the box, rendered only when a map is drawn.
- No CARTO code remains (the watermark-dodging zoom picker is gone).

## Weaknesses (honest)
- At home the map is decoration 73 % of the time — a fixed picture of your own street. That is the price of "always-on"; inset-b is the alternative.
- Up to four network image fetches per render at home (OSM sends `cache-control: no-cache`, so the browser revalidates rather than serving silently from cache — verified with curl). Offline, the box shows as a dark rounded rectangle with the dot and caption — acceptable but not nothing.
- OSM has no @2x tiles, so on a 2× phone the map is upscaled and slightly soft.
- OSM's tile usage policy expects a real browser Referer/User-Agent; HA's dashboard provides both, but heavy polling dashboards are not what the policy is for. One tile set per state change is well within it.
- 128×52 px is a small window; in open countryside (the "away parked" scenario) it is mostly empty — the caption carries the meaning there.
- The garage row costs ~110 px of the right column when open; at 360 px the map shrinks to its 64 px minimum in the worst case (not hit in the scenarios: 128 px at 360 with the garage row on).
