# quiet-a — "Header door"

No map. The garage door lives in the header beside the lock, because that is where the tile already keeps
house-side security facts (Locked/Unlocked): closed = a small door glyph at 22% white, no text, so it reads
as a dormant affordance like the lock; open = amber door glyph + "Garage open 40 min" (duration from
`last_changed`, rendered as `just now` / `40 min` / `1 h 20 min` / `2 d`), followed by a faint dot and the lock.
The car's whereabouts is a pin row in the info column that exists ONLY when the car is away: a zone name,
`Office · 7.7 mi NE`, `4.9 mi NE` (haversine + 8-point bearing from `zone.home`), or plain `Away` when there
are no coordinates. While driving the pin becomes a small arrow rotated to `sensor.tesla_heading`. At home
there is no row at all -- "home" is the default and absence is the quiet signal; the header glyph is the only
everyday change.

**Everyday height: unchanged, measured.** Baseline 144.6 px at 360 and 470; quiet-a 144.6 px in the everyday,
away and driving scenarios; busiest scenario 181.3 / 163.3 px, identical to baseline (the chip strip is
untouched). The away pin row is a third info row, which still fits inside the 36 px hero number's column
(3 rows = 53 px < 57 px), so even away states do not grow the tile.

Degrades to today's tile byte-for-byte when `binary_sensor.garage_door` and `device_tracker.tesla_location`
are absent or `unknown`/`unavailable` (verified by comparing the HTML string against the baseline template).
The legacy `sensor.tesla_geofence` row is kept as a fallback only when the tracker is missing. The header
garage element is deliberately outside the stale-link dimming: the door is a house sensor, not TeslaMate.

Recommended card config: `hold_action: {action: more-info, entity: device_tracker.tesla_location}` (opens
HA's full map for the "where exactly" question this variant never tries to answer) and, optionally,
`double_tap_action: {action: more-info, entity: binary_sensor.garage_door}`. Distance is in miles to match the
range line; switch to km by dropping the `* 0.621371` and the ` mi` literal.

Weaknesses (honest):
- The closed-door glyph is a permanent element, however faint. The brief forbids "permanent placeholders";
  I judged a 22% glyph as an affordance rather than a placeholder, but it is a matter of taste and the
  owner may prefer quiet-b's nothing-at-all. Removing it is a one-line change (`else if (gKnown)` branch).
- Never says "Home". If the tracker ever reports `not_home` spuriously (GPS drift at the edge of the 100 m
  zone) a `0.1 mi N` row will flicker in and out; no hysteresis is applied.
- On a 360 px phone the header can get crowded when the car is charging, the door has been open for hours
  and the car is unlocked: `bolt TESLA | Garage open 2 h 15 min · Unlocked` still fits but with little slack.
- The garage duration only refreshes when button-card re-renders (`triggers_update: all`, so any tracked
  state change), not on a timer; it can read a few minutes behind at 03:00 when nothing else changes.
