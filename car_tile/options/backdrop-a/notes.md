# Backdrop A: "Always-on map surface"

The whole tile becomes a dark map drawn full-bleed behind today's content, with the car marked. It is a
*surface*, not a widget: the map costs zero height and zero layout, and reads as street texture at home
(car dot inside a faint 100 m home-zone ring at ~58% of the width, between the hero number and the info
column) and as an answer when away (marker plus a new pin row "7.9 km NE" / "Work · 12 km NW" in the
existing right-hand info column, which today never shows). Driving swaps the dot for a heading-rotated
arrow and appends the speed to the status line ("Driving · 30 mph"). Garage door is an amber alert
chip, "Garage open · 40 min", first in the existing chip strip, so it appears only while the door is
open, exactly like the other chips.

Tiles: OpenStreetMap standard (`tile.openstreetmap.org/{z}/{x}/{y}.png`, keyless, 256 px), z15 for
street-level texture rather than city-name labels, inverted to dark with
`invert(1) hue-rotate(180deg) brightness(0.55) contrast(0.95) saturate(0.35)` on a tiles-only div (the
marker and ring sit outside the filter so they stay white). "© OpenStreetMap" at 8 px / 0.28 alpha in
the bottom-right corner, only when a map is drawn.

Legibility: a horizontal scrim (0.97 at the hero, easing to ~0.3 on the right) plus a vertical scrim
(dark at the top edge and bottom third, where the status line and chips live) and a subtle text-shadow
on the content layer only when a map is present.

## Everyday height (measured, headless Chrome, harness card incl. 1 px border)
- Home asleep: 144.6 px at 360 and 470 -- **identical to baseline** (144.6).
- Driving: 144.6 (baseline 144.6). Busy charging case: 181.3 / 163.3 (baseline 181.3 / 163.3).
- Away + garage open: 163.3 (baseline 144.6): the chip row, only while the door is open.

## Card-level config recommended
- `hold_action: {action: more-info, entity: device_tracker.tesla_location}` -- HA's full map on hold.
- The template's root uses `margin:-14px -16px -13px; padding:14px 16px 13px` to go full-bleed. It
  renders correctly in the harness (plain padded div); in button-card the `#w` field container should
  behave the same, but **verify on HA** and add `width:calc(100% + 32px)` to the root if it does not.

## Weaknesses (honest)
- Full-bleed means 3-8 OSM tile requests per render (typically 6); cached by the browser, but the
  brief suggested 2-4. B uses fewer. OSM's tile usage policy tolerates light dashboard use; it is
  not a bulk source.
- At home the map is always the same picture; that is the point of a surface, but it is ornamentation
  73% of the week and the tile is busier than today's flat #161616. Street labels near the home
  position (whatever they happen to be) are faintly readable behind the marker.
- Small dim rows (11.5 px at 0.4-0.5 white) sit over inverted labels/roads; legible in the renders
  thanks to the text-shadow, but with less margin than on the flat card.
- Negative-margin full-bleed inside button-card is unverified on the live card (see above).
- The map is an `<img>` mosaic: it cannot pan, and it re-fetches when lat/lon changes (fine while
  parked, a few tiles per minute while driving).
