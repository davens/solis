# Other Lovelace cards: Helios, the grid-voltage chart, mini-graph-card and the Car tile

*Split out of `CLAUDE.md` on 2026-09-13 to keep the always-loaded file small. This is the same text, except that site-specific values are placeholders whose real values are in the gitignored `CLAUDE.local.md`. The rules and traps that apply even when you are NOT reading this file stayed in `CLAUDE.md`.*

### Helios and grid-voltage chart

Helios highlights the OSM building nearest the configured home. OSM has no building polygon for
the house: 70 buildings lie within 250 m, none contains the address point, and the nearest centre
is 35 m away; Overpass also finds no addressed feature on the street. Separately, HA's old home point
was **98 m wrong** (96 m north, 20 m east), which affected sun times, weather and `zone.home`.

`zone.home` is deliberately at the owner's actual building centroid (coordinates in `CLAUDE.local.md`),
about 35 m from the postal point but inside the right footprint. That keeps ring, chips and highlighted
house together and stays within the 100 m presence radius. **Do not "correct" it to the postal
coordinate** without revisiting this trade.

It used to revert to the postal point on every restart. The cause was `/config/configuration.yaml`
carrying those coordinates under `homeassistant:`, and **YAML core config is applied at startup and
wins** -- a `config/core/update` websocket call sets `config_source: storage` only until the next
restart. **Fixed 2026-09-07: configuration.yaml now carries the centroid coordinates**, verified after a
restart as `config_source: yaml` with `zone.home` at the same point. Do not edit those two lines back.

Helios `home-latitude`/`home-longitude` move only the building highlight, not ring, chips or camera,
so they are intentionally unused. A dragged camera stores `helios:camera-pose:<lat>:<lon>` in browser
localStorage and overrides `camera-pitch-deg`; clear `helios*` keys to restore configured framing.
Weather rendering is off because its grey veil flattens the scene. Buildings need high opacity with
the custom palette. `camera-pitch-deg` wins only while `camera-locked` is true; an unlocked stored
pose wins.

The 24-hour grid-voltage plot shows raw `sensor.solis_inverter_grid_voltage` plus hourly min/max as a
translucent band. It fixes the y-axis near 212-257 V and marks UK supply limits, 230 V +10% / -6% =
**253.0/216.2 V**, because the question is upper-limit headroom and autoranging hides it. This is a
live healthy-grid view, not a revival of the retired logger; its statistics band begins only when the
integration's own long-term statistics begin.

**Export power was added as a second trace on 2026-08-30** so voltage peaks can be traced to their
cause, and on the first day the correlation was unmistakable: flat ~240 V overnight at zero export,
rising to 248-253 V from the moment export starts at about 09:20. Three things about it are
load-bearing:

- It is derived as `max(0, -v)/1000` from `sensor.solis_inverter_grid_power`, which is **positive
  importing** -- plotting that series raw would draw import and call it export.
- It sits on a second y-axis fixed at **0-8 kW**, not autoranged: an autoranged axis rescales per
  window, so a calm day and a clipping day would look identical and the chart would stop answering
  the question.
- It is declared FIRST in `entities` so plotly draws it beneath the voltage lines. The band's
  `fill: tonexty` is unaffected because it fills to the trace immediately before it, still Hourly max.

The violet matches Grid export on the Sankey. Script `scratchpad/voltage_export_apply.py`, backup
`grid_voltage_pre_export.json`.

**With `raw_plotly_config: true` the card does not map data automatically.** Every trace needs
explicit `x: $ex xs` and `y: $ex ys`; without them the axes and shapes render but no data does, often
with a plausible-looking -1..6 numeric x-axis and no console error.

### mini-graph-card on the Overview dashboard

The two climate cards fold their state row up into the title row via card_mod, taking each card from
195 px to 125 px. The saving is real: mini-graph-card's `.states` row costs 56 px (40 px of 33.6 px
type plus 16 px padding) purely to restate two numbers that fit beside the title. `ha-card` is a
column flex, so the mod turns it into a wrapping row -- header and states share line one, `.graph`
takes line two at `flex: 1 0 100%`.

**`.header` must be the elastic side and `.states` the rigid one.** This is not a preference; the
reverse renders differently per engine. `.header` is a *nested* flex container, so `flex: 0 0 auto` on
it makes the layout depend on an auto basis resolving to content width: Blink resolves toward
max-content and looks correct, WebKit resolves toward min-content, collapsing the title's
`overflow: hidden` span so it ellipsises while `.states` absorbs the slack. Verified 2026-08-29 --
fine in Chrome, truncated in the iPad companion app at the same card width. So `.states` carries
`flex: 0 0 auto` with `white-space: nowrap`, `.header` takes the leftover with `min-width: 0`, and
`.name`, `.icon` and `.state` all carry explicit `flex` for the same reason. **Test any change to this
card in the iPad app, not only in Chrome.** Also: `.state` needs `align-items: baseline` or the unit
floats about 5 px above the digits' baseline, and the readings need a wider gap between them (22 px)
than between a value and its unit, or the pair reads as one blob.

**The Power card already carries its own unrelated card_mod** -- a four-column grid for its four
states and the legend -- and is deliberately excluded from the fold, because four states plus a legend
wrap into a stack and make the card *taller*. A script that assigns `card["card_mod"]` across every
`custom:mini-graph-card` destroys it; that happened on 2026-08-29 and had to be restored from a
backup. Match on card name, and back the dashboard config up before writing it.

### The Overview Car tile and what "stale" means

This is the `custom:button-card` at view 0 / section 1 / card 3, keyed on `sensor.tesla_battery`. It is
a different artefact from the Car tile in `dash.py`.

**`sensor.tesla_state` = `offline` is a sleeping car, not a fault.** Measured over the seven days to
2026-08-29: offline **73.4%**, online 14.9%, suspended 5.2%, driving 3.7%, charging 2.9% -- while
`binary_sensor.teslamate_healthy` was `on` for 98.9% of the same window. Tesla lets the car stop
answering the API to avoid vampire drain, and TeslaMate reports that as offline. The tile used to list
`offline` alongside `unknown` and `unavailable`, so for roughly three-quarters of every week it showed
a red "No link - data stale", a 55%-white hero number, a half-opacity bar and 45%-opacity info rows,
for a car that was fine.

**There is no age caveat to add, either.** The car pushes a full update whenever it wakes, so the
displayed SOC, range and temperature are always the last *true* readings rather than a decaying guess.
A time-since-update heuristic would fire constantly during normal overnight sleep and would be wrong
every time. Do not add one.

So `offline`/`asleep`/`suspended` are one `resting` state, shown at full brightness as "Asleep" (or
"Plugged in - Asleep"), and `stale` now means only that the link to TeslaMate is genuinely broken:
`teslamate_healthy` off, `sensor.tesla_state` unknown/unavailable, or no SOC to display. That is the
only honest signal available.


**The alert chips were folded into the tile on 2026-09-13.** The Overview Car tile used to be followed
by a `custom:mushroom-chips-card` of fourteen conditional chips; the owner asked for them gone, on the
grounds that the tile itself has room. They are now a wrapping chip strip rendered *inside* the
button-card's `w` custom field, below the status line, and the chips card was deleted from view 0 /
section 1. Four of the fourteen conditions were dropped as duplicates rather than moved: "unlocked" and
"no car data" are already the header lock indicator and the `l1` status line, and the climate chip's
inside temperature is already an info row. What moved: openings (merged into one amber chip,
`Doors · boot open`), TPMS warnings (merged into one amber chip, `FL 2.9 · RR 2.8 bar`), climate /
preconditioning (one cyan chip, precon winning), sentry and software update (blue).

Two things about it are load-bearing. **The strip renders nothing at all when no condition is true**, so
the tile's normal height is unchanged -- that is the whole point of folding it in, and a placeholder or
an "All clear" chip would give the height straight back. And **the chips lost their individual
`tap_action: more-info`**: button-card custom fields are one HTML blob with one tap target, so the whole
tile still opens `sensor.tesla_battery` and nothing else. That was accepted, not overlooked.

The glyphs are hand-rolled inline SVG like the rest of the card, not `<ha-icon>`, for the same reason
the car and lock glyphs are. Preview harness: extract the `[[[ ... ]]]` body into a CommonJS function,
feed it a snapshot of `/api/states` with conditions flipped, and render the returned HTML on a `#161616`
card -- that catches a template throw without waiting for a real boot-open. Backup of the pre-change
dashboard: `overview_pre_tesla_chips_backup.json` in the repo root.

**Contiguous dispatch slots are merged for display (2026-09-14).** Octopus prices and schedules in
half-hour blocks and hands the API a *list*, so one continuous overnight charge arrives as several
touching slots -- `01:30-05:00` plus `05:00-05:30` rendered as two windows on the tile and read as two
separate charges. The clock row now sorts the future planned dispatches, merges any pair separated by
**60 seconds or less**, and shows the first two runs. A real gap (`21:30-22:00 · 22:30-23:00`, the
documented discrete-slot behaviour) still renders as two, which is the point: the detailed list is
preferred over the entity's own `next_start`/`next_end` precisely so a gap stays visible, and merging
touching blocks is what makes that distinction mean something. The merge builds new objects rather than
mutating the attribute array. Verified against the extracted template with contiguous, gapped,
out-of-order, four-touching, three-run, empty, all-past and malformed slot lists.

**Garage door and map location were folded in on 2026-09-24** (`car_tile/`, options on a private canvas).
Seven designs were drawn; the live one is `backdrop-b`, "Away map fade". At home the tile is unchanged. When
`device_tracker.tesla_location` leaves `home`, a darkened OpenStreetMap fades in behind the right-hand info
column, with a pin row ("4.1 mi NE", or the zone name), a heading arrow while driving, and the speed on the
status line. An open garage door shows in the header, left of the lock, as amber "Garage open · 40 min";
closed shows nothing. Everyday height is unchanged in every state. Two things are load-bearing:
`styles.custom_fields.w` must keep `overflow: visible`, or button-card clips the map 16 px short of the card
edge; and the tiles must not go back to keyless CARTO, which is now watermarked "API KEY REQUIRED". Hold
on the tile opens the tracker's full map; tap still opens `sensor.tesla_battery`. The tracker and
`sensor.tesla_heading` are retained MQTT discovery configs on TeslaMate's topics. Switching design,
rendering without HA and the per-option trade-offs: `car_tile/README.md`.
