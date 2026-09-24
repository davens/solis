# quiet-b — "Place row"

No map, and the header is untouched. "Where is the car" becomes the first row of the info column and is
always present when the tracker exists: house glyph + `Home` (dim, same weight as the temperature row); a pin
+ zone name / `4.9 mi NE` / `Office · 7.7 mi NE` / `Away` when away; a small arrow rotated to
`sensor.tesla_heading` when driving. The garage is treated as a property of that place, not of the car: at
home the same row turns amber and reads `Home · garage open 12 min` with the house glyph's door drawn open;
when the car is away and the door is open, an amber `Garage open 40 min` row sits directly under the pin row,
so house facts and car facts stay grouped at the top of the column. Closed = nothing, anywhere.

**Everyday height: unchanged, measured.** Baseline 144.6 px at 360 and 470; quiet-b 144.6 px in the everyday
(Home + temp + slot = 3 rows), away and driving scenarios; busiest 181.3 / 163.3 px, identical to baseline.
The trick that makes a permanent row free is that the hero row's height is set by the 36 px number plus the
miles line (~57 px), and three 11.5 px info rows with 4 px gaps are 53 px. A fourth row (away + garage open +
plugged in + temperature) would add ~15 px, but the car never charges away from home.

Degrades to today's tile byte-for-byte when the new entities are absent or `unknown`/`unavailable` (verified by
string comparison against the baseline template); the legacy geofence row is the fallback only when the
tracker is missing. Stale-link dimming is applied per row rather than to the whole column, so an open-garage
row stays at full strength when TeslaMate is down (the `Home` claim is dropped in that case because the
tracker is TeslaMate's too).

Recommended card config: `hold_action: {action: more-info, entity: device_tracker.tesla_location}` for HA's
full map; optionally `double_tap_action` to `binary_sensor.garage_door`. Distance is in miles to match the
range line; drop the `* 0.621371` and the ` mi` literal for km.

Weaknesses (honest):
- `Home` is shown all day, every day. It is information (like `Locked`), not an all-clear, but it is one more
  dim word on a tile whose owner values quiet; quiet-a is the answer if that grates.
- The amber `Home · garage open 12 min` row colours the word `Home` amber too; a two-tone row would be
  cleaner but the whole-row colour is what makes it readable across the room.
- The house glyph is reused for both "the car is home" and "the garage door"; the open-door drawing carries
  the second meaning, and at 11 px it depends on the amber to be unambiguous.
- Same GPS-edge flicker and re-render-cadence caveats as quiet-a: no hysteresis on the 100 m zone edge, and
  the open duration only refreshes when some tracked state changes.
