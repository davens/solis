# Backdrop B: "Away-only right fade"

At home the tile is today's tile, flat #161616, nothing added. The moment the car leaves the home zone
a dark map fades in across the right 62% of the tile behind the existing info column, with the car
marked at about the 42% point of that region and a new pin row "7.9 km NE" (or "Work · 12 km NW" in a
named zone) in the info column. The map answers "where is it" only when that is a question; driving
swaps the dot for a heading arrow and the status line gains the speed ("Driving · 30 mph"). Garage
door lives in the header row, left of the lock: quiet (nothing) when closed, amber
"Garage open · 40 min" with the door glyph when open, at zero height in every state.

Tiles: OpenStreetMap standard (`tile.openstreetmap.org/{z}/{x}/{y}.png`, keyless, 256 px), z14 so
village/district names can answer "where", inverted to dark with
`invert(1) hue-rotate(180deg) brightness(0.55) contrast(0.95) saturate(0.35)` on a tiles-only div (the
marker stays white). "© OpenStreetMap" at 8 px / 0.28 alpha in the bottom-right corner of the map
region, only while a map is drawn.

Legibility: the region fades in from the hero side (scrim 1.0 -> 0.3), darkens again at the right
edge and along the bottom third under the status line; a text-shadow on the content layer only while
the map shows. The hero number never sits over map.

## Everyday height (measured, headless Chrome, harness card incl. 1 px border)
- All four scenarios match the baseline exactly: 144.6 / 144.6 / 181.3 (163.3 at 470) / 144.6 px.
  The garage state adds no height even when open.

## Card-level config recommended
- `hold_action: {action: more-info, entity: device_tracker.tesla_location}` -- HA's full map on hold.
- Root uses `margin:-14px -16px -13px; padding:14px 16px 13px` so the fade reaches the card edge;
  verified in the harness, **verify in button-card** (add `width:calc(100% + 32px)` if the container
  does not stretch).

## Weaknesses (honest)
- Header crowding on a 360 px phone when the door is open: "Garage open · 40 min" + "Locked" sit
  together at the right; it fits, but "Unlocked" plus a long duration ("2 h 35 min") is the tightest
  case. The zone/distance row is `white-space:nowrap`; a very long zone name is clipped by the hero
  row's flex, not wrapped.
- OSM place labels are data-dependent and can land under the pin row (scenario 2 at 360 px puts
  "Waterbeach" behind "7.9 km NE"); the text-shadow keeps the row legible but it is not clean.
- 4-6 tile requests per render (typically 4); the `<img>` mosaic cannot pan. OSM's usage policy
  tolerates light dashboard use only.
- Nothing at home means the owner never sees the map unless the car is out; if he wants the "surface"
  feel every day, A is the one.
