// Home kiosk card for a landscape iPad, as a single Lovelace card in a panel view. Two looks share all the logic:
// "Classic" (design 12 · Garage Band, the default) and "Walala" (Memphis: cream, ink borders, label tabs, patterns).
// Everything site-specific (entity ids, light names) comes from the card config, never from this file.
// Talks to HA only through `hass` (states + callService), so no token ever reaches the browser.
//
// Config:
//   type: custom:home-kiosk-card
//   battery: sensor.<inverter>_battery            house battery SOC %
//   kiln_warning: binary_sensor.kiln_warning_outstanding
//   kiln_firing: binary_sensor.kiln_firing
//   fridge: switch.<fridge_plug>                  the fridge's smart plug; off or offline raises the top band
//   fridge_stalled: binary_sensor.<fridge>_stalled   on = plug on but no power drawn for longer than a normal compressor gap
//   evening_gap: sensor.energy_ping_evening_gap   > 0 means the battery will not last to 23:30; shown as kWh short
//   low_power: binary_sensor.energy_ping_low_power   evening_gap held above 0 for 7.5 min (the phone ping waits 15)
//   ping_morning: input_datetime.energy_ping_last  when the 09:00 phone ping last went out
//   day_tight: binary_sensor.energy_day_tight      the 09:00 ping's own test, live; its "off" clears the low-energy line
//   solar_power: sensor.<inverter>_solar_power    W
//   house_load: sensor.<inverter>_house_load      W
//   grid_power: sensor.<inverter>_grid_power      W, positive = importing
//   battery_power: sensor.<inverter>_battery_power  W, positive = charging; shown beyond 200 W
//   garage: binary_sensor.garage_door             on = open (read-only: there is no garage control)
//   weather: weather.<home>
//   heating: climate.<thermostat>
//   hot_water: water_heater.<boiler>             its hot_water_schedule_active attribute draws the water runs on the heating strip
//   aircon: climate.<aircon>
//   aircon_room: Living room
//   aircon_energy: sensor.<aircon>_energy_today   daily Wh/kWh counter; its history shows when the air con was working
//   aircon_fallback: sensor.<room>_temperature   shown instead when the air con's own reading has not changed for 3 hours
//                                                (LG ThinQ's cloud push can freeze it silently)
//   car_climate: climate.<car>
//   car_battery: sensor.<car>_battery
//   car_range: sensor.<car>_range                 km
//   car_charging: sensor.<car>_charging_state     'Charging' while charging, 'Complete' when done
//   car_state: sensor.<car>_state                 asleep | driving | online ...
//   car_plugged: binary_sensor.<car>_plugged_in
//   car_locked: binary_sensor.<car>_locked
//   car_power: sensor.<car>_charger_power         kW
//   car_energy_added: sensor.<car>_charge_energy_added   kWh
//   car_time_to_full: sensor.<car>_time_to_full_charge   hours
//   car_speed: sensor.<car>_speed                 km/h
//   car_charge_limit: sensor.<car>_charge_limit   fallback when Octopus has no charge target
//   car_link: binary_sensor.teslamate_healthy     off = no link, show the last known charge
//   dishwasher: sensor.<dishwasher>_operation_state   Home Connect: run | delayedstart | finished | ready ...
//   dishwasher_programme: select.<dishwasher>_active_programme
//   dishwasher_finish: sensor.<dishwasher>_programme_finish_time   a timestamp
//   dishwasher_delay: number.<dishwasher>_start_in_relative   seconds until a delayed start begins
//   dishwasher_door: sensor.<dishwasher>_door      open | closed
//   car_alerts: [{entity: binary_sensor.<car>_trunk_open, name: "Boot open"}, {entity: ..., name: "Sentry", info: true}]
// Tonight's charge slots and target come from the Octopus Intelligent entities, found by their name.
//   money_import: sensor.<electricity>_current_accumulative_cost   (optional; auto-found)
//   money_gas: sensor.<gas>_current_accumulative_cost              (optional; auto-found)
//   money_export: input_number.daily_export_credit
//   lights: [{entity: light.x, name: "Room"}, ...]
//   heating_rooms: [{entity: sensor.x_temperature, name: "Room"}, ...]   one quiet line under the heating reading
//   aircon_extras: [{entity: weather.<home> | sensor.x, name: "Outside"}, ...]   the same under air con
// Under each reading, a strip shows when it ran since midnight, read from HA history.
// The air con reports only its mode, so its strip comes from its energy counter: standby uses ~11 Wh an hour.
// Long-press the clock to open the dashboard with ?disable_km, which brings HA's top bar back for that visit.
// Bands, most urgent first: the fridge off, its plug offline, or stalled (red, above everything); the garage open; the car charging off the house battery (beyond 1 kW, outside
// 23:30-05:30); the kiln firing; low power from 07:00 to 22:00 (low_power: the phone ping's rule,
// held 7.5 minutes so a kettle does not raise it).
// The dishwasher speaks in the battery's note line, never the band: running, waiting for a delayed start, or finished
// (for an hour, until the door opens). It outranks every other note, red ones included.
// Add #kiosk-demo=fridge|fridge-offline|fridge-stalled|garage|car-battery|kiln|kiln-warning|low-power to the URL to preview a band without the real event.
// Look: theme_entity (e.g. input_select.kiosk_theme with options Classic / Walala) picks it live; if that is missing or names
// no known look, theme: classic | walala; default classic. #kiosk-theme=walala in the URL previews a look and beats both.

const FONT_HREF = 'https://fonts.googleapis.com/css2?family=Archivo+Black&family=Instrument+Sans:wght@500;700&display=swap';
const C = { paper: '#F4EFE4', ink: '#161616', grey: '#3F3F3A', red: '#D3261A', blue: '#1F4FB0', yellow: '#F2C230', amber: '#F2A516', green: '#2E7355', warn: '#AE4230', caution: '#B8832A' };
const HOT_WATER = [['off', 'Off'], ['schedule', 'Schedule'], ['boost_30m', 'Boost 30&#160;min']];
const AIRCON = [['off', 'Off'], ['heat', 'Heat'], ['cool', 'Cool'], ['auto', 'Auto'], ['dry', 'Dry'], ['fan_only', 'Fan']];
const WEATHER = { 'clear-night': 'Clear', cloudy: 'Cloudy', exceptional: 'Unusual weather', fog: 'Foggy', hail: 'Hail', lightning: 'Thunder', 'lightning-rainy': 'Thunder and rain', partlycloudy: 'Partly cloudy', pouring: 'Heavy rain', rainy: 'Rain', snowy: 'Snow', 'snowy-rainy': 'Sleet', sunny: 'Sunny', windy: 'Windy', 'windy-variant': 'Windy' };
const DEAD = ['unavailable', 'unknown'];
// The fridge band by cause: headline, what to do, and what the count measures.
const FRIDGE = {
  off: ['The fridge is off.', 'Switch its plug back on.', 'off'],
  offline: ['The fridge plug is offline.', 'Check the fridge is running.', 'offline'],
  stalled: ['The fridge has stopped.', 'Its plug is on but it is using no power. Check it.', 'flagged'],
};
// Home Connect programme keys, as HA's select options end them, in the words on the machine.
const DISH_PROGRAMMES = { auto_1: 'Auto 35–45°', auto_2: 'Auto 45–65°', auto_3: 'Auto 65–75°', eco_50: 'Eco 50°', quick_45: 'Quick 45°', quick_65: 'Quick 65°',
  intensiv_70: 'Intensive 70°', intensiv_45: 'Intensive 45°', intensiv_power: 'Intensive', normal_45: 'Normal 45°', normal_65: 'Normal 65°', glas_40: 'Glass 40°',
  glass_care: 'Glass care', night_wash: 'Silent', pre_rinse: 'Pre-rinse', kurz_60: 'Short 60°', machine_care: 'Machine care', super_60: 'Super 60°', mixed_load: 'Mixed load',
  express_sparkle_65: 'Express 65°', maximum_cleaning: 'Max clean', magic_daily: 'Daily', steam_fresh: 'Steam fresh', quick_d: 'Quick' };
const dishProgramme = st => {
  const key = DEAD.includes(st) || !st ? '' : String(st).replace(/^.*_program_/, '');
  return DISH_PROGRAMMES[key] || key.replace(/_(\d+)$/, ' $1°').replace(/_/g, ' ').replace(/^./, c => c.toUpperCase());
};
const CAR_SVG = body => `<svg width="22" height="22" viewBox="0 0 22 22" aria-hidden="true">${body}</svg>`;
// fg draws the glyphs, hi the unlocked padlock, bolt is the charging bolt's paint.
const carIcons = (fg, hi, bolt) => ({
  plug: CAR_SVG(`<rect x="7" y="2" width="2.6" height="5" fill="${fg}"/><rect x="12.4" y="2" width="2.6" height="5" fill="${fg}"/><path d="M4 7 H18 V11 A7 7 0 0 1 4 11 Z" fill="${fg}"/><rect x="9.7" y="16" width="2.6" height="5" fill="${fg}"/>`),
  bolt: CAR_SVG(`<path d="M12.5 1 L3.5 12.5 H10 L8.5 21 L18.5 9 H12 Z" ${bolt}/>`),
  lock: CAR_SVG(`<path d="M6.5 10 V6.5 A4.5 4.5 0 0 1 15.5 6.5 V10" fill="none" stroke="${fg}" stroke-width="2.4"/><rect x="4" y="10" width="14" height="10" fill="${fg}"/>`),
  unlock: CAR_SVG(`<path d="M6.5 10 V6.5 A4.5 4.5 0 0 1 15.2 4.8" fill="none" stroke="${hi}" stroke-width="2.4"/><rect x="4" y="10" width="14" height="10" fill="${hi}"/>`),
  parked: CAR_SVG(`<rect x="2.5" y="2.5" width="17" height="17" fill="none" stroke="${fg}" stroke-width="2.4"/><path d="M8.5 16.5 V6 H12.2 A3.2 3.2 0 0 1 12.2 12.4 H8.5" fill="none" stroke="${fg}" stroke-width="2.4"/>`),
  moon: CAR_SVG(`<path d="M14.5 2.5 A8.5 8.5 0 1 0 19.5 15 A7 7 0 0 1 14.5 2.5 Z" fill="${fg}"/>`),
  drive: CAR_SVG(`<path d="M11 1.5 L19 20 L11 15.5 L3 20 Z" fill="${fg}"/>`),
  tick: CAR_SVG(`<circle cx="11" cy="11" r="10" fill="${C.green}"/><path d="M6 11.2 L9.6 14.6 L16 7.6" fill="none" stroke="#fff" stroke-width="2.6"/>`),
  nolink: CAR_SVG(`<path d="M9 6 H6 A5 5 0 0 0 6 16 H9 M13 6 H16 A5 5 0 0 1 16 16 H13" fill="none" stroke="${fg}" stroke-width="2.4"/><path d="M3 19 L19 3" stroke="${fg}" stroke-width="2.4"/>`),
});

// Walala colours. Red (warn) is kept for warnings and genuine alerts; heating is tangerine, decoration never red.
const W = { paper: '#F3ECDD', ink: '#161616', grey: '#45443F', blue: '#2347C5', pink: '#FF5CB0', orange: '#FF8A2A', mint: '#7FE3C6', yellow: '#FFD21F', warn: '#D3261A' };
// The colours each look writes into its markup (backgrounds and text set per panel, band and run strip).
const LOOKS = {
  classic: { icons: carIcons('#fff', C.yellow, `fill="${C.yellow}"`), bandH: 230, heatIcon: '#fff', lightsIcon: C.red, heat: C.red, heatFg: '#fff', heatRun: '#fff', waterRun: '#8FD6FF', ac: C.ink, acRun: C.yellow, car: C.blue, carFg: '#fff', caution: C.caution, cautionFg: '#fff', warn: C.warn, dish: '#0F6E64' },
  walala: { icons: carIcons(W.ink, W.ink, `fill="${W.yellow}" stroke="${W.ink}" stroke-width="1.5" stroke-linejoin="round"`), bandH: 196, heatIcon: W.orange, lightsIcon: W.blue, heat: W.orange, heatFg: W.ink, heatRun: W.ink, waterRun: W.blue, ac: W.blue, acRun: W.yellow, car: W.pink, carFg: W.ink, caution: W.yellow, cautionFg: W.ink, warn: W.warn, dish: '#0F6E64', dishFg: '#fff' },
};

const esc = s => String(s).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
const num = v => { if (v === null || v === undefined || v === '') { return null; } const n = Number(v); return Number.isFinite(n) ? n : null; };
const deg = v => `${Math.round(v)}&#176;`;
const deg1 = v => `${v.toFixed(1)}&#176;`;
const STALE_MS = 3 * 3600 * 1000;
const halfDeg = v => (Math.round(v * 2) / 2).toString().replace(/\.0$/, '') + '&#176;';
const money = gbp => { const p = Math.round(Math.abs(gbp) * 100); return p < 100 ? `${p}p` : `£${(p / 100).toFixed(2)}`; };
const dayKey = t => { const d = new Date(t); return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate(); };
const midnight = () => { const d = new Date(); d.setHours(0, 0, 0, 0); return d.getTime(); };
const dur = ms => { const m = Math.round(ms / 60000), h = Math.floor(m / 60); return h ? `${h}&#160;h${m % 60 ? ` ${m % 60}&#160;min` : ''}` : `${m}&#160;min`; };
const RUNS_REFRESH_MS = 300000;
const WORKING_WH_PER_H = 40;
const HOLD_MS = 800;

class HomeKioskCard extends HTMLElement {
  setConfig(config) {
    this._clearTimers();
    if (this._cfg && this._cfg.weather !== config.weather) {
      this._unsubscribe();
      this._forecast = null;
    }
    this._cfg = config;
    this._runs = null;     // {at, day, hist}: today's history for the heating and air con strips
    this._pend = {};       // key -> {want, until} optimistic state while a service call lands
    this._target = {};     // climate entity -> pending set-temperature from stepper taps
    this._gen = {};        // climate entity -> stepper adjustment generation
    this._ptrs = new Set();
    if (this.isConnected) {
      this._subscribeForecast();
    }
  }

  _clearTimers() {
    Object.values(this._timers || {}).forEach(clearTimeout);
    this._timers = {};
  }

  _later(key, fn, ms) {
    clearTimeout(this._timers[key]);
    this._timers[key] = setTimeout(() => { delete this._timers[key]; fn(); }, ms);
  }

  _unsubscribe() {
    if (this._unsubForecast) {
      this._unsubForecast.then(u => u()).catch(() => {});
      this._unsubForecast = null;
    }
  }

  getCardSize() { return 12; }

  connectedCallback() {
    if (!document.querySelector(`link[href="${FONT_HREF}"]`)) {
      const l = document.createElement('link');
      l.rel = 'stylesheet';
      l.href = FONT_HREF;
      document.head.appendChild(l);
    }
    if (!this.shadowRoot) {
      this.attachShadow({ mode: 'open' });
      // Hold renders while a finger is down so a rebuilt button cannot swallow the tap; render again after the click.
      const up = e => {
        this._ptrs.delete(e.pointerId);
        clearTimeout(this._timers.hold);
        delete this._timers.hold;
        this._later('up', () => this._render(), 300);
      };
      this.shadowRoot.addEventListener('pointerdown', e => {
        this._ptrs.add(e.pointerId);
        this._later('down', () => { this._ptrs.clear(); this._render(); }, 5000);
        if (e.target.closest('.clock')) {
          this._later('hold', () => { location.href = `${location.pathname}?disable_km`; }, HOLD_MS);
        }
      });
      this.shadowRoot.addEventListener('pointerup', up);
      this.shadowRoot.addEventListener('pointercancel', up);
      this.shadowRoot.addEventListener('click', e => { this._onClick(e); this._render(); });
    }
    this._tick = setInterval(() => this._render(), 15000);
    this._fit = () => this._render(true);
    window.addEventListener('resize', this._fit);
    this._subscribeForecast();
    this._render(true);
  }

  disconnectedCallback() {
    clearInterval(this._tick);
    window.removeEventListener('resize', this._fit);
    this._clearTimers();
    this._ptrs.clear();
    this._target = {};
    this._unsubscribe();
  }

  set hass(hass) {
    this._hass = hass;
    if (this.isConnected) {
      this._subscribeForecast();
    }
    this._render();
  }

  _subscribeForecast() {
    const h = this._hass, w = this._cfg && this._cfg.weather;
    if (!h || !w || this._unsubForecast || this._timers.forecast) {
      return;
    }
    let sub;
    sub = this._unsubForecast = h.connection.subscribeMessage(ev => {
      if (this._unsubForecast !== sub) {
        return;
      }
      this._fcDelay = 0;
      this._forecast = ev.forecast || [];
      this._render();
    }, { type: 'weather/subscribe_forecast', forecast_type: 'daily', entity_id: w });
    sub.catch(() => {
      if (this._unsubForecast !== sub) {
        return;
      }
      this._unsubForecast = null;
      this._fcDelay = Math.min(300000, (this._fcDelay || 2500) * 2);
      if (this.isConnected) {
        this._later('forecast', () => this._subscribeForecast(), this._fcDelay);
      }
    });
  }

  _s(id) { return id && this._hass ? this._hass.states[id] : undefined; }
  _on(id) { const s = this._s(id); return !!s && s.state === 'on'; }
  _n(id) { const s = this._s(id); return s ? num(s.state) : null; }

  // ---- derived state -------------------------------------------------------------------------------------

  _demo() {
    const m = /kiosk-demo=([\w-]+)/.exec(location.hash);
    return m ? m[1] : null;
  }

  // 'classic' or 'walala': the URL preview, then the theme entity, then the theme config key.
  _look() {
    const m = /kiosk-theme=([\w-]+)/.exec(location.hash);
    const ent = this._s(this._cfg.theme_entity);
    const pick = [m && m[1], ent && ent.state, this._cfg.theme].map(t => String(t || '').toLowerCase()).find(t => LOOKS[t]);
    return pick || 'classic';
  }

  _band() {
    const demo = this._demo();
    const f = this._s(this._cfg.fridge), st = this._s(this._cfg.fridge_stalled);
    const since = e => Math.max(0, Math.floor((Date.now() - Date.parse(e.last_changed)) / 60000));
    const fdemo = { fridge: 'off', 'fridge-offline': 'offline', 'fridge-stalled': 'stalled' }[demo];
    if (fdemo) {
      return { kind: 'fridge', why: fdemo, mins: 3 };
    }
    if (f && f.state === 'off') {
      return { kind: 'fridge', why: 'off', mins: since(f) };
    }
    // Offline only after 10 minutes, so an HA restart or a Zigbee blip does not flash the red band.
    if (f && DEAD.includes(f.state) && since(f) >= 10) {
      return { kind: 'fridge', why: 'offline', mins: since(f) };
    }
    if (st && st.state === 'on') {
      return { kind: 'fridge', why: 'stalled', mins: since(st) };
    }
    const g = this._s(this._cfg.garage);
    if (demo === 'garage' || (g && g.state === 'on')) {
      const mins = demo === 'garage' ? 20 : Math.max(0, Math.floor((Date.now() - Date.parse(g.last_changed)) / 60000));
      return { kind: 'garage', mins };
    }
    const drain = demo === 'car-battery' ? 2100 : this._carDrain();
    if (drain) {
      return { kind: 'car', kw: drain / 1000 };
    }
    if (demo === 'kiln' || demo === 'kiln-warning' || this._on(this._cfg.kiln_firing)) {
      return { kind: 'kiln', warning: demo === 'kiln-warning' || (demo !== 'kiln' && this._on(this._cfg.kiln_warning)) };
    }
    if (demo === 'low-power' || this._on(this._cfg.low_power)) {
      return { kind: 'low', gap: demo === 'low-power' ? 1.4 : Math.max(0, this._n(this._cfg.evening_gap) || 0) };
    }
    return null;
  }

  // Watts the house battery is giving while the car charges outside the cheap rate, or 0.
  _carDrain() {
    const cfg = this._cfg, now = new Date(), m = now.getHours() * 60 + now.getMinutes();
    const kw = this._n(cfg.car_power), bp = this._n(cfg.battery_power);
    const charging = (this._s(cfg.car_charging) || {}).state === 'Charging' || (kw !== null && kw > 0);
    const cheap = m >= 23 * 60 + 30 || m < 5 * 60 + 30;
    return charging && !cheap && bp !== null && bp < -1000 ? -bp : 0;
  }

  // The same rules as the phone pings (HA packages/energy_pings.yaml and kiln.yaml), cut down.
  // Like the pings, silence is the normal state: no rule firing means no line.
  _verdict(soc) {
    const now = new Date(), m = now.getHours() * 60 + now.getMinutes(), mon = now.getMonth() + 1;
    if (this._on(this._cfg.kiln_warning) || this._demo() === 'kiln-warning') {
      return { color: C.warn, text: 'Hold big loads<br>till 23:30.' };
    }
    if (m >= 23 * 60 + 30 || m < 5 * 60 + 30) {
      return { color: C.green, text: 'Cheap power now.<br>Run what you like.' };
    }
    if (this._on(this._cfg.low_power)) {
      return { color: C.warn, text: 'Low power.<br>Run loads after 23:30.' };
    }
    if (soc === null) {
      return { color: C.grey, text: 'Battery reading<br>not available.' };
    }
    // The morning ping (09:00) is the sunny-day rule March-September and the low-energy rule
    // October-February, so its stamp says which one went out today.
    const sent = this._s(this._cfg.ping_morning);
    const today = sent && new Date(sent.state.replace(' ', 'T')).toDateString() === now.toDateString();
    if (today && m >= 9 * 60 && m < 17 * 60) {
      if (mon >= 3 && mon <= 9) {
        return { color: C.green, text: 'Good sunny day.<br>Earlier the better.' };
      }
      // The ping's test re-run live: once the numbers say the day is fine (a sunnier day than
      // forecast, a corrected setting) the line clears itself. Only an explicit "off" clears it,
      // so a missing reading never hides a warning that went out.
      const tight = this._s(this._cfg.day_tight);
      if (tight && tight.state === 'off') {
        return { color: C.green, text: '' };
      }
      return { color: C.caution, text: 'Low energy day.<br>Be careful with power.' };
    }
    return { color: C.green, text: '' };
  }

  // The dishwasher's note, or null when it has nothing to say.
  _dish() {
    const cfg = this._cfg, st = this._s(cfg.dishwasher);
    if (!st) {
      return null;
    }
    const active = this._s(cfg.dishwasher_programme) || {};
    const prog = dishProgramme(active.state);
    const fin = Date.parse((this._s(cfg.dishwasher_finish) || {}).state);
    const hm = t => new Date(t).toTimeString().slice(0, 5);
    // Home Connect sometimes drops the 'run' event, leaving the state at 'ready'. An active programme
    // only exists while a cycle is on, so trust it for a few hours (HA reloads the link to fetch the rest).
    const missed = st.state === 'ready' && prog && Date.now() - Date.parse(active.last_changed) < 4 * 3600000;
    const run = st.state === 'run' || missed;
    if (run) {
      const when = fin ? `done ~${hm(fin)}` : prog;
      return { color: 'dish', text: `Dishwasher: running${when ? `<br>${when}.` : '.'}` };
    }
    if (st.state === 'delayedstart') {
      // While waiting, the finish time is set-time + programme length and ignores the delay, so the start is the delay
      // still to run and the finish adds that length (finish minus when it was set). Without the delay entity neither
      // time can be derived, so say none.
      const delay = parseFloat((this._s(cfg.dishwasher_delay) || {}).state);
      if (fin && delay >= 0) {
        const start = Date.now() + delay * 1000;
        return { color: 'dish', text: `Dishwasher: delayed start<br>starts ${hm(start)}, done ~${hm(start + fin - Date.parse(this._s(cfg.dishwasher_finish).last_changed))}.` };
      }
      return { color: 'dish', text: `Dishwasher: delayed start<br>${prog || 'Starts later'}.` };
    }
    // Opening the door clears it; the state alone may sit at 'finished' until then.
    const shut = (this._s(cfg.dishwasher_door) || {}).state !== 'open';
    if (st.state === 'finished' && shut && Date.now() - Date.parse(st.last_changed) < 3600000) {
      return { color: 'dish', text: 'Dishwasher done.<br>Empty me.' };
    }
    return null;
  }

  // Battery charging (up, green) or discharging (down, amber), shown only beyond 200 W either way.
  _flowHtml() {
    const w = this._n(this._cfg.battery_power);
    if (w === null || Math.abs(w) <= 200) {
      return '';
    }
    const up = w > 0, arrow = up ? 'M6 1 11 10H1z' : 'M6 11 11 2H1z', kw = `${(Math.abs(w) / 1000).toFixed(1)}&#160;kW`;
    if (this._walala) {
      // A sticker: white arrow and figure on green or ink on amber, both above 4.5:1.
      return `<span class="flow" style="color:${up ? C.green : C.caution}"><span class="fchip ${up ? 'up' : 'down'}"><svg viewBox="0 0 12 12" width="16" height="16"><path d="${arrow}"/></svg>${kw}</span></span>`;
    }
    return `<span class="flow" style="color:${up ? C.green : C.caution}"><svg viewBox="0 0 12 12" width="16" height="16"><path d="${arrow}" fill="currentColor"/></svg>${kw}</span>`;
  }

  // Solar, house and grid right now, as plain kW. Hidden when none of the three is configured.
  _liveHtml() {
    const cfg = this._cfg, kw = w => (Math.abs(w) / 1000).toFixed(1);
    const solar = this._n(cfg.solar_power), house = this._n(cfg.house_load), grid = this._n(cfg.grid_power);
    const cell = (label, w) => `<div><div class="lv">${w === null ? '–' : kw(w)}<small> kW</small></div><div class="ll">${label}</div></div>`;
    if (solar === null && house === null && grid === null) {
      return '';
    }
    let gridLabel = 'Grid';
    if (grid !== null && grid >= 50) {
      gridLabel = 'Buying';
    } else if (grid !== null && grid <= -50) {
      gridLabel = 'Selling';
    }
    return `<div class="live">${cell('Solar', solar)}${cell('House', house)}${cell(gridLabel, grid)}</div>`;
  }

  _findOctopus(kind) {
    const ids = Object.keys(this._hass.states).filter(k => k.startsWith(`sensor.octopus_energy_${kind}_`) && k.endsWith('_current_accumulative_cost') && !k.includes('export'));
    return ids.sort()[0];
  }

  _money() {
    const ids = [this._cfg.money_import || this._findOctopus('electricity'), this._cfg.money_gas || this._findOctopus('gas'), this._cfg.money_export];
    const [imp, gas, exp] = ids.map(id => this._n(id));
    // A net with a missing part is a wrong number, not a partial one.
    if (ids.every(id => !id) || ids.some((id, i) => id && [imp, gas, exp][i] === null)) {
      return null;
    }
    const net = (imp || 0) + (gas || 0) - (exp || 0);
    const bought = [], credit = [];
    for (const [v, what] of [[imp, 'electricity'], [gas, 'gas']]) {
      if (v !== null) {
        (v < 0 ? credit : bought).push(`${money(v)} of ${what}${v < 0 ? ' credit' : ''}`);
      }
    }
    let detail = bought.length ? `Bought ${bought.join(' and ')}` : '';
    if (exp !== null && exp > 0) {
      detail += `${detail ? ', sold' : 'Sold'} ${money(exp)} of sunshine`;
    }
    if (credit.length) {
      detail += `${detail ? ', ' : ''}${credit.join(' and ')}`;
    }
    return { net, detail: detail ? detail + '.' : '' };
  }

  _weather() {
    const w = this._s(this._cfg.weather);
    if (!w || DEAD.includes(w.state)) {
      return '';
    }
    let t = WEATHER[w.state] || esc(w.state);
    const tt = num(w.attributes.temperature);
    if (tt !== null) {
      t += `, ${deg(tt)}.`;
    }
    const f = this._forecast || [];
    const today = dayKey(Date.now());
    const next = f.find(d => dayKey(d.datetime) > today);
    if (next && num(next.temperature) !== null) {
      t += `<br>Tomorrow ${deg(next.temperature)}.`;
    }
    return t;
  }

  // ---- today's runs -----------------------------------------------------------------------------------------

  _fetchRuns() {
    const h = this._hass, ids = [this._cfg.heating, this._cfg.hot_water, this._cfg.aircon_energy].filter(Boolean);
    const day = dayKey(Date.now());
    if (!h || !h.callWS || !ids.length || this._runsBusy || (this._runs && this._runs.day === day && Date.now() - this._runs.at < RUNS_REFRESH_MS)) {
      return;
    }
    this._runsBusy = true;
    // Attribute changes matter (the thermostat's hvac_action), so ask for every change, not just significant ones.
    h.callWS({ type: 'history/history_during_period', start_time: new Date(midnight()).toISOString(), entity_ids: ids,
      significant_changes_only: false, minimal_response: false, no_attributes: false })
      .then(r => { this._runs = { at: Date.now(), day, hist: r || {} }; },
        () => { this._runs = { at: Date.now(), day, hist: null }; })
      .finally(() => {
        this._runsBusy = false;
        // Right after a finger lifts, the pending post-tap render owns the rebuild; one here could swallow the click.
        if (!this._timers.up) {
          this._render();
        }
      });
  }

  // Spans since midnight where isOn(state, attributes) held, from history plus the live state; null without history.
  _ran(id, isOn) {
    const r = this._runs;
    if (!r || !r.hist || r.day !== dayKey(Date.now())) {
      return null;
    }
    const start = midnight(), now = Date.now();
    let attrs = {};
    // Clamp every time into [midnight, now]: an iPad clock behind HA's must not count time that has not happened here.
    const clamp = t => Math.min(now, Math.max(start, t));
    const pts = (r.hist[id] || []).map(e => { attrs = e.a || attrs; return { t: clamp(e.lu * 1000), on: isOn(e.s, attrs) }; });
    const live = this._s(id);
    if (live && (!pts.length || Date.parse(live.last_updated) > pts[pts.length - 1].t)) {
      pts.push({ t: clamp(Date.parse(live.last_updated) || now), on: isOn(live.state, live.attributes) });
    }
    const spans = [];
    pts.forEach((p, i) => {
      const end = i + 1 < pts.length ? pts[i + 1].t : now;
      if (p.on && end > p.t) {
        const last = spans[spans.length - 1];
        if (last && last[1] === p.t) {
          last[1] = end;
        } else {
          spans.push([p.t, end]);
        }
      }
    });
    return { spans, total: spans.reduce((t, [a, b]) => t + b - a, 0) };
  }

  // Spans since midnight where the energy counter rose faster than standby; null without history.
  _worked(id) {
    const r = this._runs;
    if (!id || !r || !r.hist || r.day !== dayKey(Date.now())) {
      return null;
    }
    const start = midnight(), now = Date.now();
    const clamp = t => Math.min(now, Math.max(start, t));
    const live = this._s(id);
    const wh = live && live.attributes.unit_of_measurement === 'kWh' ? 1000 : 1;
    const pts = (r.hist[id] || []).map(e => ({ t: e.lu * 1000, v: num(e.s) })).filter(p => p.v !== null);
    if (live && num(live.state) !== null && (!pts.length || Date.parse(live.last_updated) > pts[pts.length - 1].t)) {
      pts.push({ t: Date.parse(live.last_updated), v: num(live.state) });
    }
    const spans = [];
    for (let i = 1; i < pts.length; i++) {
      const a = pts[i - 1], b = pts[i];
      // A drop is the midnight reset: what it reads now was used since midnight.
      const from = b.v < a.v ? Math.max(a.t, start) : a.t;
      const used = (b.v < a.v ? b.v : b.v - a.v) * wh;
      const t0 = clamp(from), t1 = clamp(b.t);
      if (t1 > t0 && b.t > from && used / ((b.t - from) / 3600000) > WORKING_WH_PER_H) {
        const last = spans[spans.length - 1];
        if (last && last[1] === t0) {
          last[1] = t1;
        } else {
          spans.push([t0, t1]);
        }
      }
    }
    const today = pts.length && pts[pts.length - 1].t >= start ? pts[pts.length - 1].v * wh : null;
    return { spans, total: spans.reduce((t, [a, b]) => t + b - a, 0), kwh: today };
  }

  _reading(item) {
    const s = this._s(item.entity);
    if (!s || DEAD.includes(s.state)) {
      return null;
    }
    const v = item.entity.startsWith('weather.') ? num(s.attributes.temperature) : num(s.state);
    if (v === null) {
      return null;
    }
    return `${esc(item.name)} ${s.attributes.unit_of_measurement === '%' ? `${Math.round(v)}%` : deg(v)}`;
  }

  _extraHtml(items, runs, label, color) {
    const parts = (items || []).map(i => this._reading(i)).filter(Boolean);
    let html = parts.length ? `<div class="rooms">${parts.join(' &#183; ')}</div>` : '';
    if (runs) {
      const day = 86400000, start = midnight(), pct = t => ((t - start) / day * 100).toFixed(2);
      html += `<div class="ranlbl">${label(runs)}</div>
        <div class="strip">${runs.spans.map(([a, b, bg]) => `<b style="left:${pct(a)}%;width:${(Math.max(b - a, 120000) / day * 100).toFixed(2)}%;background:${bg || color}"></b>`).join('')}<i style="left:${pct(Date.now())}%"></i></div>
        <div class="ticks"><span>00</span><span>06</span><span>12</span><span>18</span><span>24</span></div>`;
    }
    return html ? `<div class="extra">${html}</div>` : '';
  }

  // ---- actions --------------------------------------------------------------------------------------------

  _call(domain, service, data, pendKey, want) {
    const p = this._pend[pendKey] = { want, until: Date.now() + 60000 };
    this._hass.callService(domain, service, data).catch(() => {
      if (this._pend[pendKey] === p) {
        this._fail(pendKey);
      }
    });
    this._render(true);
  }

  _fail(key) {
    this._pend[key] = { fail: true, until: Date.now() + 8000 };
    this._render();
  }

  _pending(key, actual) {
    const p = this._pend[key];
    if (!p) {
      return null;
    }
    if (Date.now() > p.until) {
      if (p.fail) {
        delete this._pend[key];
        return null;
      }
      this._pend[key] = { fail: true, until: Date.now() + 8000 };
      return this._pend[key];
    }
    if (p.fail) {
      return p;
    }
    if (p.want === actual) {
      delete this._pend[key];
      return null;
    }
    return p;
  }

  _step(entity, dir) {
    const s = this._s(entity);
    if (!s || DEAD.includes(s.state) || num(s.attributes.temperature) === null) {
      return;
    }
    const a = s.attributes, step = num(a.target_temp_step) || 0.5;
    const base = this._target[entity] !== undefined ? this._target[entity] : num(a.temperature);
    const next = Math.min(num(a.max_temp) || 32, Math.max(num(a.min_temp) || 5, base + dir * step));
    const g = this._gen[entity] = (this._gen[entity] || 0) + 1;
    const mine = () => this._gen[entity] === g;
    this._target[entity] = next;
    delete this._pend[`step:${entity}`];
    // Taps accumulate for a moment and land as one call, so a run of presses is one change, not five.
    this._later(entity, () => {
      this._hass.callService('climate', 'set_temperature', { entity_id: entity, temperature: next })
        .catch(() => {
          if (mine()) {
            this._fail(`step:${entity}`);
          }
        })
        .finally(() => {
          if (mine() && this.isConnected) {
            this._later(`clean:${entity}`, () => {
              if (mine()) {
                delete this._target[entity];
                this._render(true);
              }
            }, 4000);
          }
        });
    }, 900);
    this._render(true);
  }

  _onClick(e) {
    const b = e.target.closest('button[data-act]');
    if (!b || b.disabled) {
      return;
    }
    const { act, entity, val } = b.dataset;
    if (act === 'step') {
      this._step(entity, Number(val));
    } else if (act === 'hw') {
      this._call('water_heater', 'set_operation_mode', { entity_id: entity, operation_mode: val }, entity, val);
    } else if (act === 'hvac') {
      this._call('climate', 'set_hvac_mode', { entity_id: entity, hvac_mode: val }, entity, val);
    } else if (act === 'car') {
      if (this._pend.car && !this._pend.car.fail) {
        return;
      }
      this._call('climate', val === 'on' ? 'turn_on' : 'turn_off', { entity_id: entity }, 'car', val);
    } else if (act === 'light') {
      this._call('homeassistant', val === 'on' ? 'turn_on' : 'turn_off', { entity_id: entity }, entity, val);
    }
  }

  // ---- render ---------------------------------------------------------------------------------------------

  _render(force) {
    if (!this.shadowRoot || !this._hass || !this._cfg) {
      return;
    }
    if (this._ptrs.size && !force) {
      return;
    }
    this._fetchRuns();
    const top = this.getBoundingClientRect().top;
    const h = Math.max(600, window.innerHeight - Math.max(0, top));
    const html = this._html(h);
    if (html !== this._last) {
      this._last = html;
      this.shadowRoot.innerHTML = html;
    }
  }

  _html(h) {
    const cfg = this._cfg, look = this._look();
    this._walala = look === 'walala';
    this._P = LOOKS[look];
    const band = this._band();
    const soc = this._n(cfg.battery);
    const v = this._verdict(soc);
    // The note line: the dishwasher unless a red note needs it. The bar keeps the battery's own colour.
    const dish = this._dish();
    const note = dish || v;
    const flow = this._flowHtml();
    const m = this._money();
    const pct = soc === null ? '–' : Math.round(soc) + '%';
    const now = new Date();
    const clock = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;

    const date = now.toLocaleDateString('en-GB', { weekday: 'long', day: 'numeric', month: 'long' });

    const calm = this._walala ? this._calmWalala({ band, soc, v, note, flow, m, pct, clock, date }) : `<div class="calm">
      <div class="clockrow"><div><span class="clock">${clock}</span><div class="date">${date}</div></div><span class="wx">${this._weather()}</span></div>
      <div>
        ${band ? `<div class="battrow"><span class="batt">${pct}</span><span class="capcol">${flow}<span class="cap">Home<br>battery</span></span></div>`
          : `<div class="caprow">${flow}<div class="cap">Home battery</div></div><div class="batt">${pct}</div>`}
        <div class="bar"><div style="width:${soc === null ? 0 : Math.max(0, Math.min(100, soc))}%;background:${v.color}"></div></div>
        ${this._liveHtml()}
        ${note.text && !(band && band.kind === 'low') ? `<div class="verdict${note.color === 'dish' ? ' small' : ''}"><span class="dot" style="background:${note.color === 'dish' ? this._P.dish : note.color}"></span><span>${note.text}</span></div>` : ''}
      </div>
      ${m ? `<div class="money">
        <div class="moneyrow"><span class="big${m.net < 0 ? ' earned' : ''}">${money(m.net)}</span> <span class="word">${m.net >= 0 ? 'spent' : 'earned'} today</span></div>
        <div class="detail">${esc(m.detail)}</div>
      </div>` : '<div></div>'}
    </div>`;
    return `<style>${CSS}${this._walala ? WALALA_CSS : ''}</style>
<div class="root${this._walala ? ' walala' : ''}${band ? '' : ' roomy'}" style="height:${h}px;grid-template-rows:${band ? `${this._P.bandH}px ` : ''}minmax(0,1fr)">
  ${band ? this._bandHtml(band) : ''}
  <div class="below">
    ${calm}
    <div class="right">
      <div class="toprow">${this._heatingHtml()}${this._airconHtml()}</div>
      <div class="botrow">${this._carHtml()}${this._lightsHtml()}</div>
    </div>
  </div>
</div>`;
  }

  // Walala's left column: the clock in a yellow block, the battery as a pink disc beside a gauge, the money in a dome.
  _calmWalala({ band, soc, v, note, flow, m, pct, clock, date }) {
    // _verdict's colours as Walala fills [pill, text, gauge]: mint for good news, yellow for care, red for a warning.
    const tones = { [C.green]: [W.mint, W.ink, W.blue], [C.caution]: [W.yellow, W.ink, W.yellow], [C.warn]: [W.warn, '#fff', W.warn], dish: [this._P.dish, this._P.dishFg] };
    const tone = tones[v.color] || ['#fff', W.ink, W.grey], pill = tones[note.color] || tone;
    const fill = soc === null ? 0 : Math.max(0, Math.min(100, soc));
    const live = this._liveHtml(), said = note.text && !(band && band.kind === 'low');
    // The live row and the verdict come and go; with both showing the battery packs into one row and the weather into one line.
    const more = (live ? 1 : 0) + (said ? 1 : 0);
    return `<div class="calm more${more}">
      <div class="clockrow"><div><span class="clock">${clock}</span><div class="rules"></div><div class="date">${date}</div></div><span class="wx">${this._weather().replace('<br>', band || more > 1 ? ' ' : '<br>')}</span></div>
      <div class="battery">
        <div class="battlbl"><span class="cap">Home battery</span>${flow || '<span class="shapes"><i></i><b></b></span>'}</div>
        <div class="orb"><span class="batt">${pct}</span></div>
        <div class="gauge"><div style="height:${fill}%;background:${tone[2]}"></div></div>
      </div>
      ${live}
      ${said ? `<div class="pill" style="background:${pill[0]};color:${pill[1]}"><div class="verdict${note.color === 'dish' ? ' small' : ''}"><span class="dot" style="background:${pill[1]}"></span><span>${note.text}</span></div></div>` : ''}
      ${m ? `<div class="money">
        <div class="moneyrow"><span class="big${m.net < 0 ? ' earned' : ''}">${money(m.net)}</span> <span class="word">${m.net >= 0 ? 'spent' : 'earned'} today</span></div>
        <div class="detail">${esc(m.detail)}</div>
      </div>` : ''}
    </div>`;
  }

  _bandHtml(b) {
    if (b.kind === 'fridge') {
      const big = b.mins < 120 ? b.mins : Math.floor(b.mins / 60);
      const unit = b.mins < 120 ? (b.mins === 1 ? 'minute' : 'minutes') : 'hours';
      return `<div class="band" style="background:${this._P.warn};color:#fff">
        <div class="disc"></div>
        <div class="bandtext">
          <div class="needs"><span class="sq" style="background:#fff"></span><span>Needs you now</span></div>
          <div class="headline">${FRIDGE[b.why][0]}</div>
          <div class="sub">${FRIDGE[b.why][1]}</div>
        </div>
        <div class="bandblock"><span class="count" style="color:${this._P.warn}">${big}</span><span class="unit" style="color:#fff">${unit} ${FRIDGE[b.why][2]}</span></div>
      </div>`;
    }
    if (b.kind === 'garage') {
      const big = b.mins < 120 ? b.mins : Math.floor(b.mins / 60);
      const unit = b.mins < 120 ? (b.mins === 1 ? 'minute open' : 'minutes open') : 'hours open';
      return `<div class="band" style="background:${C.amber};color:${C.ink}">
        <div class="disc"></div>
        <div class="bandtext">
          <div class="needs"><span class="sq"></span><span>Needs you</span></div>
          <div class="headline">The garage is open.</div>
          <div class="sub">${b.mins < 1 ? 'Just opened.' : 'Please close it when you can.'}</div>
        </div>
        <div class="bandblock"><span class="count" style="color:${C.amber}">${big}</span><span class="unit" style="color:${C.amber}">${unit}</span></div>
      </div>`;
    }
    if (b.kind === 'car') {
      return `<div class="band" style="background:${this._P.caution};color:${this._P.cautionFg}">
        <div class="disc"></div>
        <div class="bandtext">
          <div class="needs"><span class="sq" style="background:${this._P.cautionFg}"></span><span>Needs you</span></div>
          <div class="headline">The car is charging.</div>
          <div class="sub">From the house battery. Stop it unless it's needed.</div>
        </div>
        <div class="bandblock"><span class="count" style="color:${this._P.caution}">${b.kw.toFixed(1)}</span><span class="unit" style="color:#fff">kW from battery</span></div>
      </div>`;
    }
    if (b.kind === 'low') {
      return `<div class="band" style="background:${this._P.warn};color:#fff">
        <div class="disc"></div>
        <div class="bandtext">
          <div class="needs"><span class="sq" style="background:#fff"></span><span>Heads up</span></div>
          <div class="headline">Power is low.</div>
          <div class="sub">Run big loads after 23:30.</div>
        </div>
        <div class="bandblock"><span class="count" style="color:${this._P.warn}">${b.gap.toFixed(1)}</span><span class="unit" style="color:#fff">kWh short</span></div>
      </div>`;
    }
    return `<div class="band" style="background:${this._P.warn};color:#fff">
      <div class="disc"></div>
      <div class="bandtext">
        <div class="needs"><span class="sq" style="background:#fff"></span><span>Heads up</span></div>
        <div class="headline">The kiln is firing.</div>
        <div class="sub">${b.warning ? 'Hold big loads till 23:30.' : 'Nothing to do.'}</div>
      </div>
      <div class="bandblock"><svg width="110" height="96" viewBox="0 0 26 24" aria-hidden="true"><path d="M13 0 L26 24 L0 24 Z" fill="${this._P.warn}"></path></svg><span class="unit" style="color:#fff">Kiln</span></div>
    </div>`;
  }

  _stepper(entity, s, label) {
    const real = num(s.attributes.temperature);
    const set = this._target[entity] !== undefined ? this._target[entity] : real;
    const dis = real === null || s.state === 'off' || DEAD.includes(this._s(entity).state) ? 'disabled' : '';
    return `<div class="stepper">
      <button data-act="step" data-entity="${entity}" data-val="-1" aria-label="Lower the ${label}" ${dis}>&#8722;</button>
      <div class="set"><span>${s.state === 'off' ? 'Off' : 'Set to'}</span><b>${real === null || s.state === 'off' ? '' : halfDeg(set)}</b></div>
      <button data-act="step" data-entity="${entity}" data-val="1" aria-label="Raise the ${label}" ${dis}>+</button>
    </div>`;
  }

  _stepErr(entity) {
    const p = this._pending(`step:${entity}`);
    return p && p.fail ? ' <span class="err">· no response</span>' : '';
  }

  // The boiler's day on one strip: heating, hot water, and both at once as a bar split heating over water.
  _boilerRuns(id, hwId) {
    const heat = this._ran(id, (st, a) => a.hvac_action === 'heating');
    const water = hwId && this._s(hwId) ? this._ran(hwId, (st, a) => a.hot_water_schedule_active === true) : null;
    if (!heat || !water) {
      return heat && { spans: heat.spans, label: `Ran ${dur(heat.total)} today` };
    }
    const P = this._P, both = [];
    heat.spans.forEach(([a, b]) => water.spans.forEach(([c, d]) => {
      if (Math.min(b, d) > Math.max(a, c)) {
        both.push([Math.max(a, c), Math.min(b, d), `linear-gradient(${P.heatRun} 50%, ${P.waterRun} 50%)`]);
      }
    }));
    const key = c => `<span class="key" style="background:${c}"></span>`;
    return {
      spans: [...heat.spans, ...water.spans.map(([a, b]) => [a, b, P.waterRun]), ...both],
      label: `${key(P.heatRun)}Heating ${dur(heat.total)} &#183; ${key(P.waterRun)}Water ${dur(water.total)} today`,
    };
  }

  _heatingHtml() {
    const id = this._cfg.heating, s = this._s(id);
    if (!s) {
      return `<div class="field" style="background:${this._P.heat};color:${this._P.heatFg}"></div>`;
    }
    const cur = num(s.attributes.current_temperature);
    const act = s.attributes.hvac_action;
    const word = DEAD.includes(s.state) ? 'Offline.' : s.state === 'off' ? 'Turned off.' : act === 'heating' ? 'Heating up.' : 'Resting.';
    const hwId = this._cfg.hot_water, hw = this._s(hwId);
    let hwRow = '';
    if (hw) {
      const p = this._pending(hwId, hw.state.startsWith('boost') ? 'boost_30m' : hw.state);
      const mode = p && p.want ? p.want : hw.state;
      hwRow = `<div class="lbl">Hot water${p && p.fail ? ' <span class="err">· no response</span>' : ''}</div>
      <div class="seg" style="grid-template-columns:1fr 1.4fr 1.4fr">${HOT_WATER.map(([k, l]) => {
        const sel = k === mode || (k === 'boost_30m' && mode.startsWith('boost'));
        return `<button data-act="hw" data-entity="${hwId}" data-val="${k}" class="${sel ? 'sel' : ''}" aria-pressed="${sel}" ${DEAD.includes(hw.state) ? 'disabled' : ''}>${l}</button>`;
      }).join('')}</div>`;
    }
    return `<div class="field" style="background:${this._P.heat};color:${this._P.heatFg}">
      <div class="head"><svg width="22" height="20" viewBox="0 0 26 24" aria-hidden="true"><path d="M13 0 L26 24 L0 24 Z" fill="${this._P.heatIcon}"></path></svg><span>Heating${this._stepErr(id)}</span></div>
      <div class="reading"><span class="temp">${cur === null ? '–' : deg1(cur)}</span><span class="note">Hallway.<br>${word}</span></div>
      ${this._extraHtml(this._cfg.heating_rooms, this._boilerRuns(id, hwId), r => r.label, this._P.heatRun)}
      ${this._stepper(id, s, 'heating')}
      ${hwRow}
    </div>`;
  }

  // The air con's own reading, unless it has not changed for STALE_MS and the room sensor has a number.
  _acTemp(s) {
    const own = num(s.attributes.current_temperature);
    const fb = this._s(this._cfg.aircon_fallback), alt = fb ? num(fb.state) : null;
    const stale = own === null || Date.now() - new Date(s.last_updated).getTime() > STALE_MS;
    return stale && alt !== null ? alt : own;
  }

  _airconHtml() {
    const id = this._cfg.aircon, s = this._s(id);
    if (!s) {
      return `<div class="field" style="background:${this._P.ac};color:#fff"></div>`;
    }
    const p = this._pending(id, s.state);
    const mode = p && p.want ? p.want : s.state;
    const cur = this._acTemp(s);
    const fan = s.attributes.fan_mode;
    const note = DEAD.includes(s.state) ? 'Offline.' : mode === 'off' ? 'Turned off.' : fan ? `Fan on ${esc(String(fan).replace(/_/g, ' '))}.` : '';
    const modes = AIRCON.filter(([k]) => (s.attributes.hvac_modes || []).includes(k));
    return `<div class="field" style="background:${this._P.ac};color:#fff">
      <div class="head"><span class="sq" style="background:${this._P.acRun}"></span><span>Air con${this._stepErr(id)}</span></div>
      <div class="reading"><span class="temp">${cur === null ? '–' : deg1(cur)}</span><span class="note">${esc(this._cfg.aircon_room || 'Living room')}.<br>${note}</span></div>
      ${this._extraHtml(this._cfg.aircon_extras, this._worked(this._cfg.aircon_energy),
        r => `Working ${dur(r.total)} today${r.kwh === null ? '' : ` &#183; ${(r.kwh / 1000).toFixed(1)}&#160;kWh`}`, this._P.acRun)}
      ${this._stepper(id, { ...s, state: mode }, 'air con')}
      <div class="lbl">Mode${p && p.fail ? ' <span class="err">· no response</span>' : ''}</div>
      <div class="seg dark" style="grid-template-columns:repeat(${modes.length},minmax(0,1fr))">${modes.map(([k, l]) =>
        `<button data-act="hvac" data-entity="${id}" data-val="${k}" class="${k === mode ? 'sel' : ''}" aria-pressed="${k === mode}" ${DEAD.includes(s.state) ? 'disabled' : ''}>${l}</button>`).join('')}</div>
    </div>`;
  }

  // Octopus Intelligent entities share one device prefix; find it the way the Overview car tile does.
  _octo(sfx) {
    const states = this._hass ? this._hass.states : {};
    const dev = Object.keys(states).filter(k => k.includes('.octopus_energy_') && k.includes('_intelligent_'))
      .map(k => k.slice(k.indexOf('.') + 1, k.indexOf('_intelligent_'))).sort()[0];
    if (!dev) {
      return null;
    }
    for (const d of ['sensor', 'number', 'time', 'select', 'binary_sensor']) {
      const s = states[`${d}.${dev}_intelligent_${sfx}`];
      if (s && !DEAD.includes(s.state)) {
        return s;
      }
    }
    return null;
  }

  // Tonight's planned charge slots, adjacent ones merged, ending in the future; at most two.
  _carSlots() {
    const disp = this._octo('dispatching'), a = (disp && disp.attributes) || {}, now = Date.now();
    const ms = t => new Date(t).getTime();
    const plans = (Array.isArray(a.planned_dispatches) ? a.planned_dispatches : [])
      .filter(p => p && Number.isFinite(ms(p.start)) && Number.isFinite(ms(p.end)) && ms(p.end) > now)
      .sort((x, y) => ms(x.start) - ms(y.start));
    const runs = [];
    for (const p of plans) {
      const last = runs[runs.length - 1];
      if (last && ms(p.start) - last.end <= 60000) {
        last.end = Math.max(last.end, ms(p.end));
      } else {
        runs.push({ start: ms(p.start), end: ms(p.end) });
      }
    }
    if (!runs.length && Number.isFinite(ms(a.next_start)) && ms(a.next_end) > now) {
      runs.push({ start: ms(a.next_start), end: ms(a.next_end) });
    }
    return runs.slice(0, 2).map(r => ({ ...r, live: r.start <= now }));
  }

  _carHtml() {
    const cfg = this._cfg, id = cfg.car_climate, s = this._s(id), icons = this._P.icons;
    const soc = this._n(cfg.car_battery), km = this._n(cfg.car_range);
    const st = (this._s(cfg.car_state) || {}).state;
    const cs = (this._s(cfg.car_charging) || {}).state;
    const kw = this._n(cfg.car_power), added = this._n(cfg.car_energy_added), ttf = this._n(cfg.car_time_to_full);
    const plugged = this._on(cfg.car_plugged);
    const lock = (this._s(cfg.car_locked) || {}).state;
    const stale = (this._s(cfg.car_link) || {}).state === 'off' || !st || DEAD.includes(st) || soc === null;
    const charging = cs === 'Charging' || (kw !== null && kw > 0);
    const asleep = ['offline', 'asleep', 'suspended'].includes(st);
    const hm = t => new Date(t).toTimeString().slice(0, 5);
    const addedTxt = added !== null && added > 0 ? `+${added.toFixed(1)}&#160;kWh<br>added.` : '';

    // Climate goes over Bluetooth, not TeslaMate, so its line and button keep working when the TeslaMate link is down.
    const na = !s || DEAD.includes(s.state);
    const on = !na && s.state !== 'off';
    const a = na ? {} : s.attributes;
    const cur = num(a.current_temperature), tgt = num(a.temperature);
    const warm = cur !== null && tgt !== null && cur < tgt;
    // An unavailable car says nothing about whether it turned off, so it must not acknowledge a pending call.
    const p = this._pending('car', na ? undefined : on ? 'on' : 'off');
    const aim = tgt === null ? 'Climate on' : `${warm ? 'Warming' : 'Cooling'} to ${halfDeg(tgt)}`;
    let climate, btn, btnStyle = '';
    if (na) {
      climate = 'Climate out of range';
      btn = 'Car out<br>of range';
    } else if (p && p.fail) {
      climate = on ? aim : 'Climate off';
      btn = 'No answer<br><small>try again</small>';
    } else if (p) {
      climate = p.want === 'on' ? 'Turning on…' : 'Turning off…';
      btn = climate;
      btnStyle = 'opacity:.75';
    } else if (on) {
      climate = aim;
      btn = `Stop<br>${tgt === null ? 'climate' : warm ? 'warming' : 'cooling'}`;
    } else {
      climate = 'Climate off';
      btn = tgt === null ? 'Turn on<br>climate' : `${warm ? 'Warm' : 'Cool'} the car<br><small>to ${halfDeg(tgt)}</small>`;
    }

    // The states follow the Overview car tile: no link, charging, complete, plugged in, driving, asleep, parked.
    const slots = !stale && plugged ? this._carSlots() : [];
    const waiting = slots.find(r => !r.live);
    const evening = (() => { const h = new Date().getHours() + new Date().getMinutes() / 60; return h >= 18 || h < 5.5; })();
    let icon, status, side = '', lockShown = true;
    if (stale) {
      icon = icons.nolink;
      status = 'No link to TeslaMate';
      side = 'Last known.';
      lockShown = false;
    } else if (charging) {
      icon = icons.bolt;
      status = `Charging${kw !== null && kw > 0 ? ` &#183; ${kw.toFixed(1)}&#160;kW` : ''}`;
      side = addedTxt;
    } else if (plugged && cs === 'Complete') {
      icon = icons.tick;
      status = 'Charge complete';
      side = addedTxt;
    } else if (plugged) {
      icon = icons.plug;
      status = `Plugged in &#183; ${asleep ? 'Asleep' : 'Parked'}`;
      side = waiting && !slots.some(r => r.live) ? `Waiting for<br>${hm(waiting.start)}.` : slots.length ? '' : 'No slots<br>yet.';
    } else if (st === 'driving') {
      const mph = this._n(cfg.car_speed);
      icon = icons.drive;
      status = `Driving${mph !== null && mph > 0 ? ` &#183; ${Math.round(mph * 0.621371)}&#160;mph` : ''}`;
      lockShown = false;
    } else {
      icon = asleep ? icons.moon : icons.parked;
      status = asleep ? 'Asleep' : 'Parked';
    }
    const unplugged = !stale && !plugged && st !== 'driving' && evening;
    const chips = [];
    for (const al of stale ? [] : cfg.car_alerts || []) {
      if (this._on(al.entity)) {
        chips.push([esc(al.name || al.entity), !al.info]);
      }
    }
    if (unplugged) {
      // Beside the %, where a plugged-in car says what it is waiting for; as a chip when alerts share the space.
      if (chips.length) {
        chips.unshift(['Unplugged', true]);
      } else {
        side = '<span class="amber"><b>Not<br>plugged in.</b></span>';
      }
    }
    if (chips.length) {
      side = `<span class="chips">${chips.slice(0, 2).map(([t, w]) => `<span class="chip${w ? ' warn' : ''}">${t}</span>`).join('')}</span>`;
    } else if (side) {
      side = `<span class="carside">${side}</span>`;
    }

    // Tonight's plan replaces the tall button only while there is a slot still to come.
    const plan = slots.length > 0;
    let lockHtml = '';
    if (lockShown && lock === 'off') {
      lockHtml = `<span class="gap"></span>${icons.unlock}<span class="amber">Unlocked</span>`;
    } else if (lockShown && lock === 'on') {
      // The word fits only beside the short unplugged statuses.
      lockHtml = `<span class="gap"></span>${icons.lock}${plugged ? '' : '<span>Locked</span>'}`;
    }
    // Under a band the panel is short, so the range moves up beside the heading.
    const miles = km === null ? '' : `About ${Math.round(km * 0.621371)}&#160;miles`;
    const second = `${cur === null ? '' : `${deg(cur)} inside &#183; `}${climate}`;
    const button = `<button class="carbtn${plan ? ' short' : ''}${on && !p ? ' on' : ''}" data-act="car" data-entity="${id}" data-val="${on ? 'off' : 'on'}" ${na || (p && !p.fail) ? 'disabled' : ''} style="${btnStyle}">${btn}</button>`;
    let right = button;
    if (plan) {
      let next = false;
      const rows = slots.map(r => {
        let tag = '';
        if (r.live) {
          tag = '<span class="now"><i></i>Now</span>';
        } else if (!next) {
          next = true;
          tag = '<span class="tag">Next</span>';
        }
        return `<div class="slot${r.live ? ' live' : ''}"><span>${hm(r.start)}&#8211;${hm(r.end)}</span>${tag}</div>`;
      }).join('');
      const limS = this._octo('charge_target') || this._s(cfg.car_charge_limit), lim = limS ? num(limS.state) : null;
      const byS = this._octo('target_time'), by = byS ? String(byS.state).slice(0, 5) : null;
      const left = charging && ttf !== null && ttf > 0.02 ? `<div class="carleft">${dur(ttf * 3600000)} remaining</div>` : '';
      right = `<div class="plan"><div class="planhead">Tonight</div><div class="slots">${rows}</div>
        ${lim !== null && by ? `<div class="full">Full ${Math.round(lim)}% by ${by}</div>` : ''}${left}${button}</div>`;
    }
    return `<div class="field car${plan ? ' withplan' : ''}" style="background:${this._P.car};color:${this._P.carFg}">
      <div class="carinfo">
        <div class="head"><span class="sq" style="background:${this._P.carFg};border-radius:50%"></span><span>Car</span>${miles ? `<span class="carrange inhead">${miles}</span>` : ''}</div>
        <div class="carmain"><span class="temp">${soc === null ? '–' : `${Math.round(soc)}%`}</span>${side}</div>
        <div class="carbar"><div style="width:${soc === null ? 0 : Math.max(0, Math.min(100, soc))}%"></div></div>
        ${miles ? `<div class="carrange">${miles}</div>` : ''}
        <div class="carstatus"><div class="carrow">${icon}<span>${status}</span>${lockHtml}</div><div>${second}</div></div>
      </div>
      ${right}
    </div>`;
  }

  _lightsHtml() {
    const lights = (this._cfg.lights || []).map(l => (typeof l === 'string' ? { entity: l } : l))
      .map(l => ({ ...l, s: this._s(l.entity) }))
      .filter(l => l.s && !DEAD.includes(l.s.state));
    const btns = lights.map(l => {
      const p = this._pending(l.entity, l.s.state);
      const on = p && p.want ? p.want === 'on' : l.s.state === 'on';
      const b = num(l.s.attributes.brightness);
      const sub = !on ? 'Off' : b !== null ? `${Math.round(b / 2.55)}%` : 'On';
      const name = esc(l.name || l.s.attributes.friendly_name || l.entity);
      return `<button data-act="light" data-entity="${l.entity}" data-val="${on ? 'off' : 'on'}" class="light${on ? ' on' : ''}" aria-pressed="${on}">${name}<small>${sub}</small></button>`;
    });
    const anyOn = lights.some(l => l.s.state === 'on');
    return `<div class="field lights">
      <div class="head"><svg width="20" height="18" viewBox="0 0 26 24" aria-hidden="true"><path d="M13 0 L26 24 L0 24 Z" fill="${this._P.lightsIcon}"></path></svg><span>Lights</span></div>
      ${lights.length ? `<div class="lightgrid" style="grid-template-columns:repeat(${Math.min(2, lights.length)},minmax(0,1fr))">${btns.join('')}</div>`
        : '<div class="none">No lights<br>are reachable.</div>'}
      ${lights.length && !anyOn ? '<div class="alloff">All lights are off.</div>' : ''}
    </div>`;
  }
}

const CSS = `
:host { display:block; }
* { box-sizing:border-box; }
button { font:inherit; cursor:pointer; border:0; border-radius:0; padding:0; -webkit-tap-highlight-color:transparent; touch-action:manipulation; }
button:disabled { cursor:default; }
button:active:not(:disabled) { transform:scale(.97); }
.root { font-family:'Instrument Sans',sans-serif; background:${C.paper}; color:${C.ink}; padding:24px; display:grid; gap:14px; overflow:hidden; -webkit-user-select:none; user-select:none; }
.band { padding:24px 24px 24px 30px; display:grid; grid-template-columns:minmax(0,1fr) 300px; gap:24px; position:relative; overflow:hidden; }
.disc { position:absolute; left:-50px; bottom:-70px; width:160px; height:160px; border-radius:50%; background:${C.ink}; }
.bandtext { display:flex; flex-direction:column; justify-content:center; min-width:0; position:relative; padding-left:90px; }
.needs { display:flex; align-items:center; gap:14px; font-size:22px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }
.sq { width:20px; height:20px; background:${C.ink}; flex:none; display:inline-block; }
.headline { font-size:62px; font-weight:700; line-height:1; letter-spacing:-.03em; margin-top:12px; }
.sub { font-size:28px; font-weight:700; line-height:1.3; margin-top:10px; }
.bandblock { background:${C.ink}; display:flex; flex-direction:column; align-items:center; justify-content:center; gap:4px; }
.count { font-family:'Archivo Black',sans-serif; font-size:110px; line-height:.9; letter-spacing:-.04em; }
.unit { font-size:22px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }
.below { display:grid; grid-template-columns:360px minmax(0,1fr); gap:24px; min-height:0; }
.calm { display:flex; flex-direction:column; justify-content:space-between; min-width:0; min-height:0; }
.clockrow { display:flex; align-items:center; gap:18px; }
.clock { -webkit-user-select:none; user-select:none; -webkit-touch-callout:none; font-family:'Archivo Black',sans-serif; font-size:62px; line-height:.9; letter-spacing:-.04em; }
.clock { display:block; }
.date { font-size:19px; font-weight:600; line-height:1.2; margin-top:6px; color:${C.grey}; white-space:nowrap; }
.wx { font-size:19px; font-weight:500; line-height:1.3; color:${C.grey}; }
.caprow { display:flex; align-items:center; justify-content:space-between; gap:12px; }
.caprow .cap { margin-left:auto; }
.capcol { display:flex; flex-direction:column; align-items:flex-end; gap:6px; }
.flow { display:inline-flex; align-items:center; gap:5px; font-size:22px; font-weight:800; letter-spacing:-.01em; white-space:nowrap; }
.battrow { display:flex; align-items:flex-end; justify-content:space-between; gap:12px; }
.batt { font-family:'Archivo Black',sans-serif; font-size:80px; line-height:.88; letter-spacing:-.045em; }
.cap { font-size:18px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; text-align:right; line-height:1.2; }
.bar { height:12px; margin-top:12px; background:#DDD5C4; display:flex; }
.bar > div { height:100%; }
.live { display:grid; grid-template-columns:repeat(3,1fr); gap:8px; margin-top:16px; }
.lv { font-size:30px; font-weight:800; letter-spacing:-.02em; line-height:1; }
.lv small { font-size:16px; font-weight:600; color:${C.grey}; }
.ll { font-size:16px; font-weight:600; color:${C.grey}; margin-top:4px; }
.verdict { display:flex; align-items:flex-start; gap:12px; margin-top:16px; font-size:32px; font-weight:700; line-height:1.08; letter-spacing:-.02em; }
.dot { width:22px; height:22px; border-radius:50%; flex:none; margin-top:6px; }
.money { border-top:4px solid ${C.ink}; padding-top:12px; }
.moneyrow { display:flex; align-items:baseline; gap:12px; }
.big { font-family:'Archivo Black',sans-serif; font-size:56px; line-height:.95; letter-spacing:-.04em; }
.word { font-size:26px; font-weight:700; }
.big.earned { color:${C.green}; }
.detail { font-size:18px; font-weight:500; color:${C.grey}; margin-top:8px; line-height:1.3; }
.right { display:grid; grid-template-rows:minmax(0,1.8fr) minmax(0,1fr); gap:14px; min-height:0; min-width:0; }
.toprow { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; min-height:0; }
.botrow { display:grid; grid-template-columns:minmax(0,1fr) 250px; gap:14px; min-height:0; }
.field { padding:20px; display:flex; flex-direction:column; min-width:0; min-height:0; overflow:hidden; }
.head { display:flex; align-items:center; gap:10px; font-size:20px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }
.reading { display:flex; align-items:center; gap:12px; margin-top:8px; }
.extra { display:none; margin:16px 0 14px; }
.rooms { font-size:18px; font-weight:700; line-height:1.2; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
.ranlbl { font-size:16px; font-weight:700; opacity:.85; margin:12px 0 6px; }
.extra > .ranlbl:first-child { margin-top:0; }
.strip { position:relative; height:12px; background:rgba(255,255,255,.22); }
.strip b { position:absolute; top:0; bottom:0; }
.ranlbl .key { display:inline-block; width:11px; height:11px; margin-right:6px; vertical-align:0; }
.ranlbl .key:not(:first-child) { margin-left:4px; }
.strip i { position:absolute; top:-5px; bottom:-5px; width:3px; background:#fff; }
.ticks { display:flex; justify-content:space-between; font-size:14px; font-weight:700; opacity:.8; margin-top:5px; }
.temp { font-family:'Archivo Black',sans-serif; font-size:60px; line-height:.95; letter-spacing:-.04em; }
.note { font-size:19px; font-weight:500; line-height:1.25; }
.note.bold { font-weight:700; }
.stepper { display:grid; grid-template-columns:88px minmax(0,1fr) 88px; gap:8px; height:88px; margin-top:auto; flex:none; }
.stepper button { background:#fff; color:${C.ink}; font-family:'Archivo Black',sans-serif; font-size:44px; line-height:1; }
.stepper button:disabled { opacity:.35; }
.set { display:flex; flex-direction:column; align-items:center; justify-content:center; }
.set span { font-size:18px; font-weight:500; }
.set b { font-family:'Archivo Black',sans-serif; font-size:30px; line-height:1; font-weight:400; }
.lbl { font-size:18px; font-weight:700; margin-top:10px; flex:none; }
.err { color:${C.yellow}; }
.seg { display:grid; gap:6px; height:64px; margin-top:6px; flex:none; }
.seg button { border:3px solid #fff; background:transparent; color:#fff; font-size:19px; font-weight:700; }
.seg.dark button { border-width:2px; font-size:18px; }
.seg button.sel { border:0; background:#fff; color:${C.ink}; }
.seg.dark button.sel { background:${C.yellow}; }
.seg button:disabled { opacity:.4; }
.car { display:grid; grid-template-columns:minmax(0,1fr) 180px; gap:16px; }
.car.withplan { grid-template-columns:minmax(0,1fr) 196px; }
.carinfo { display:flex; flex-direction:column; min-width:0; min-height:0; }
.carmain { display:flex; align-items:center; gap:10px; margin-top:10px; min-width:0; }
.car .temp { font-size:56px; line-height:.9; letter-spacing:-.045em; }
.carside { font-size:16px; font-weight:500; line-height:1.22; white-space:nowrap; }
.chips { display:flex; flex-direction:column; align-items:flex-start; gap:6px; min-width:0; }
.chip { display:inline-flex; align-items:center; height:28px; padding:0 10px; box-shadow:inset 0 0 0 2px #fff; font-size:16px; font-weight:700; white-space:nowrap; }
.chip.warn { background:${C.yellow}; color:${C.ink}; box-shadow:none; }
.carbar { height:10px; margin-top:10px; background:rgba(255,255,255,.28); display:flex; flex:none; }
.carbar div { background:#fff; }
.carrange { font-size:19px; font-weight:700; line-height:1.2; margin-top:8px; }
.carrange.inhead { margin:0 0 0 auto; letter-spacing:0; text-transform:none; white-space:nowrap; }
.roomy .carrange.inhead, .root:not(.roomy) .carinfo > .carrange { display:none; }
/* Under a band the car panel is ~140px of content: smaller chips, and the plan keeps only its slots and the button. */
.root:not(.roomy) .chips { gap:4px; }
.root:not(.roomy) .chip { height:24px; padding:0 8px; font-size:15px; }
.root:not(.roomy) .planhead, .root:not(.roomy) .full, .root:not(.roomy) .carleft { display:none; }
.root:not(.roomy) .slots { margin-top:0; }
.root:not(.roomy) .slot { font-size:18px; }
.root:not(.roomy) .carbtn.short { height:56px; }
.carstatus { margin-top:auto; padding-top:8px; font-size:18px; font-weight:700; line-height:1.2; white-space:nowrap; }
.carstatus > div + div { margin-top:5px; }
.carrow { display:flex; align-items:center; gap:8px; }
.carrow svg { flex:none; }
.carrow .gap { width:4px; }
.amber { color:${C.yellow}; }
.carbtn { background:#fff; color:${C.ink}; padding:0 12px; font-family:'Archivo Black',sans-serif; font-size:26px; line-height:1.05; letter-spacing:-.02em; }
.carbtn small { font-family:'Instrument Sans',sans-serif; font-size:19px; font-weight:700; letter-spacing:0; }
.carbtn.short { margin-top:auto; height:80px; flex:none; padding:0 10px; font-size:22px; }
.carbtn.short small { font-size:16px; }
.carbtn.on { background:${C.yellow}; }
.carbtn:disabled { background:rgba(255,255,255,.35); color:#fff; }
.plan { display:flex; flex-direction:column; min-width:0; min-height:0; }
.planhead { font-size:16px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; line-height:1.2; }
.slots { display:flex; flex-direction:column; gap:4px; margin-top:7px; }
.slot { display:flex; justify-content:space-between; align-items:center; white-space:nowrap; height:30px; padding:0 10px; background:rgba(255,255,255,.16); font-size:20px; font-weight:700; font-variant-numeric:tabular-nums; letter-spacing:-.01em; }
.slot.live { background:${C.green}; }
.slot .tag, .slot .now { font-size:13px; font-weight:700; letter-spacing:.1em; text-transform:uppercase; }
.slot .tag { opacity:.8; }
.slot .now { display:flex; align-items:center; gap:6px; }
.slot .now i { width:10px; height:10px; border-radius:50%; background:#fff; }
.full { font-size:18px; font-weight:700; line-height:1.2; margin-top:10px; }
.carleft { font-size:16px; font-weight:700; line-height:1.2; margin-top:3px; opacity:.85; }
.lights { background:#fff; border:3px solid ${C.ink}; padding:14px; }
.lightgrid { display:grid; gap:8px; margin-top:auto; min-height:108px; flex:1 1 auto; max-height:100%; padding-top:10px; }
.light { background:#fff; border:3px solid ${C.ink}; color:${C.ink}; font-size:18px; font-weight:700; line-height:1.2; padding:0 6px; display:flex; flex-direction:column; align-items:center; justify-content:center; }
.light.on { background:${C.yellow}; border-color:${C.yellow}; }
.light small { font-size:18px; font-weight:500; }
.none, .alloff { font-size:18px; font-weight:700; margin-top:8px; }
.none { margin-top:auto; }
.roomy .clock { font-size:84px; }
.roomy .clockrow { flex-direction:column; align-items:flex-start; gap:10px; }
.roomy .date { font-size:22px; }
.roomy .wx { font-size:22px; }
.roomy .batt { font-size:120px; margin-top:10px; }
.roomy .bar { height:16px; margin-top:14px; }
.roomy .verdict { font-size:38px; }
.verdict.small, .roomy .verdict.small { font-size:27px; }
.verdict.small .dot { margin-top:3px; }
.roomy .dot { margin-top:8px; }
.roomy .money { padding-top:16px; }
.roomy .moneyrow { display:block; }
.roomy .big { font-size:84px; line-height:.9; display:block; }
.roomy .word { font-size:30px; display:block; margin-top:6px; }
.roomy .detail { font-size:21px; margin-top:12px; line-height:1.32; }
.roomy .reading { margin-top:12px; }
.roomy .extra { display:block; }
.roomy .temp { font-size:80px; }
.roomy .car .temp { font-size:72px; }
.roomy .carbar { height:12px; margin-top:12px; }
.roomy .carrange { font-size:21px; }
.roomy .stepper { grid-template-columns:104px minmax(0,1fr) 104px; height:104px; }
.roomy .lbl { margin-top:14px; }
.roomy .seg { height:80px; }
.roomy .seg.dark button { font-size:17px; }
`;

// Walala, laid over CSS: every rule is scoped to .walala, so the classic look never sees it.
const svgBg = (w, h, body) => `url("data:image/svg+xml,${encodeURIComponent(`<svg xmlns='http://www.w3.org/2000/svg' width='${w}' height='${h}'>${body}</svg>`)}")`;
const DASH = (x, y, dx, dy, c) => `<line x1='${x}' y1='${y}' x2='${x + dx}' y2='${y + dy}' stroke='${c}' stroke-width='11' stroke-linecap='round'/>`;
const PAT = {
  dashes: svgBg(240, 240, [[14, 30, 26, -18, W.blue], [74, 12, 16, 24, W.ink], [130, 34, 26, -18, W.orange], [196, 20, 26, -18, W.blue], [40, 84, 16, 24, W.pink],
    [100, 92, 26, -18, W.blue], [160, 76, 16, 24, W.ink], [214, 100, 16, 24, W.blue], [12, 150, 26, -18, W.ink], [76, 160, 26, -18, W.orange], [138, 140, 16, 24, W.blue],
    [196, 166, 26, -18, W.pink], [40, 206, 16, 24, W.blue], [104, 222, 26, -18, W.ink], [166, 206, 26, -18, W.blue], [222, 212, 16, 24, W.orange]].map(d => DASH(...d)).join('')),
  zigzag: svgBg(24, 40, `<rect width='24' height='40' fill='#fff'/><path d='M0 22 L12 10 L24 22 V32 L12 20 L0 32Z' fill='${W.ink}'/>`),
  dots: svgBg(20, 20, `<rect width='20' height='20' fill='#fff'/><circle cx='10' cy='10' r='5' fill='${W.pink}'/>`),
  check: svgBg(26, 26, `<rect width='26' height='26' fill='#fff'/><path d='M0 0H13V13H0ZM13 13H26V26H13Z' fill='${W.ink}'/>`),
  hazard: svgBg(32, 32, `<rect width='32' height='32' fill='${W.yellow}'/><path d='M0 16 L16 0 H32 L0 32Z M16 32 L32 16 V32Z' fill='${W.ink}'/>`),
  stripes: svgBg(32, 32, `<path d='M0 16 L16 0 H32 L0 32Z M16 32 L32 16 V32Z' fill='${W.ink}'/>`),
};
const WALALA_CSS = `
.root.walala { background:${W.paper} ${PAT.dashes}; color:${W.ink}; }
.walala .below { gap:22px; }
.walala .right, .walala .toprow, .walala .botrow { gap:18px; }
.walala:not(.roomy) .right { grid-template-rows:minmax(0,1.6fr) minmax(0,1fr); }
/* Bands: an ink frame, hazard stripes down the left, the label as an outlined tab. */
.walala .band { border:5px solid ${W.ink}; padding:16px 16px 16px 96px; gap:20px; }
.walala .disc { left:0; top:0; bottom:0; width:70px; height:auto; border-radius:0; border-right:5px solid ${W.ink}; background:${PAT.stripes}; }
.walala .bandtext { padding-left:0; }
.walala .needs { align-self:flex-start; border:3px solid currentColor; padding:3px 12px; font-size:18px; gap:10px; }
.walala .needs .sq { width:16px; height:16px; }
.walala .headline { font-family:'Archivo Black',sans-serif; font-weight:400; font-size:52px; letter-spacing:-.03em; margin-top:10px; }
.walala .sub { font-size:24px; margin-top:6px; }
.walala .count { font-size:96px; }
.walala .unit { font-size:19px; }
/* The left panel. */
.walala .calm { background:#fff; border:5px solid ${W.ink}; padding:16px; justify-content:flex-start; gap:14px; }
.walala .clockrow { flex-direction:column; align-items:flex-start; gap:4px; }
.walala .clock { display:inline-block; background:${W.yellow}; border:4px solid ${W.ink}; padding:8px 12px 2px; font-size:50px; }
.walala .rules { height:20px; margin-top:6px; border:4px solid ${W.ink}; background:linear-gradient(${W.ink},${W.ink}) center/100% 4px no-repeat; }
.walala .date { color:${W.ink}; font-size:18px; font-weight:700; margin-top:6px; }
.walala .wx { color:${W.grey}; font-size:17px; font-weight:600; }
.walala.roomy .clock { font-size:80px; }
.walala.roomy .rules { height:24px; }
.walala.roomy .date { font-size:21px; margin-top:10px; }
.walala.roomy .wx { font-size:21px; }
.walala .battery { display:grid; grid-template-columns:auto auto minmax(0,1fr); grid-template-areas:"orb gauge lbl"; gap:12px; align-items:center; }
.walala.roomy .battery { grid-template-columns:auto minmax(0,1fr); grid-template-areas:"lbl lbl" "orb gauge"; gap:12px 16px; }
.walala .battlbl { grid-area:lbl; display:flex; flex-direction:column; align-items:flex-start; gap:10px; min-width:0; }
.walala.roomy .battlbl { flex-direction:row; align-items:center; gap:14px; }
.walala .cap { background:${W.pink}; border:3px solid ${W.ink}; padding:4px 10px; font-size:15px; text-align:left; }
.walala.roomy .cap { font-size:17px; }
.walala .shapes { display:flex; align-items:center; gap:12px; }
.walala .shapes i { width:0; height:0; border-left:14px solid transparent; border-right:14px solid transparent; border-bottom:24px solid ${W.blue}; }
.walala .shapes b { width:22px; height:22px; background:${W.yellow}; border:3px solid ${W.ink}; }
.walala .orb { grid-area:orb; width:132px; height:132px; border-radius:50%; background:${W.pink}; border:5px solid ${W.ink}; display:flex; align-items:center; justify-content:center; }
.walala.roomy .orb { width:184px; height:184px; }
.walala .batt { font-size:38px; margin:0; line-height:1; }
/* Under a band with the live row and the verdict both showing, everything in the left panel packs tighter. */
.walala:not(.roomy) .calm.more2 { gap:10px; }
.walala:not(.roomy) .calm.more2 .clock { font-size:44px; }
.walala:not(.roomy) .calm.more2 .rules { height:16px; margin-top:5px; }
.walala:not(.roomy) .calm.more2 .orb, .walala:not(.roomy) .calm.more2 .gauge { height:104px; }
.walala:not(.roomy) .calm.more2 .orb { width:104px; }
.walala:not(.roomy) .calm.more2 .batt { font-size:30px; }
.walala:not(.roomy) .calm.more2 .lv { font-size:20px; }
.walala:not(.roomy) .calm.more2 .live > div { padding:4px 7px 3px; }
.walala:not(.roomy) .calm.more2 .pill { padding:7px 10px; }
.walala:not(.roomy) .calm.more2 .verdict { font-size:19px; }
.walala:not(.roomy) .calm.more2 .moneyrow { padding:8px 12px 5px; }
.walala:not(.roomy) .calm.more2 .big { font-size:38px; }
.walala:not(.roomy) .calm.more2 .detail { font-size:15px; margin-top:6px; }
.walala.roomy .batt { font-size:54px; margin:0; }
.walala .gauge { grid-area:gauge; width:44px; height:132px; border:5px solid ${W.ink}; background:#fff; display:flex; align-items:flex-end; }
.walala.roomy .gauge { width:84px; height:184px; }
.walala .gauge > div { width:100%; border-top:4px solid ${W.ink}; }
.walala.roomy .calm.more1 .orb, .walala.roomy .calm.more1 .gauge { height:146px; }
.walala.roomy .calm.more1 .orb { width:146px; }
.walala.roomy .calm.more1 .batt { font-size:46px; }
.walala.roomy .calm.more2 .battery { grid-template-columns:auto auto minmax(0,1fr); grid-template-areas:"orb gauge lbl"; gap:12px; }
.walala.roomy .calm.more2 .battlbl { flex-direction:column; align-items:flex-start; }
.walala.roomy .calm.more2 .orb { width:150px; height:150px; }
.walala.roomy .calm.more2 .gauge { width:52px; height:150px; }
.walala.roomy .calm.more2 .batt { font-size:44px; }
.walala.roomy .calm.more2 .shapes { display:none; }
.walala.roomy .calm.more2 .cap { font-size:15px; }
.walala.roomy .calm.more2 .moneyrow { padding-top:18px; }
.walala.roomy .calm.more2 .big { font-size:60px; }
.walala .flow { font-size:19px; }
.walala .fchip { display:inline-flex; align-items:center; gap:5px; padding:2px 9px; border:3px solid ${W.ink}; }
.walala .fchip.up { background:${C.green}; color:#fff; }
.walala .fchip.down { background:${W.yellow}; color:${W.ink}; }
.walala .fchip path { fill:currentColor; }
.walala .live { margin-top:0; gap:8px; }
.walala .live > div { border:3px solid ${W.ink}; padding:6px 8px 5px; background:#fff; }
.walala .live > div:first-child { background:${W.yellow}; }
.walala .live > div:nth-child(2) { background:${W.mint}; }
.walala .lv { font-size:24px; }
.walala .lv small, .walala .ll { color:${W.ink}; font-size:15px; }
.walala .pill { border:4px solid ${W.ink}; padding:10px 12px; }
.walala .verdict { margin-top:0; font-size:22px; line-height:1.1; gap:10px; }
.walala.roomy .verdict { font-size:25px; }
.walala .verdict.small { font-size:21px; }
.walala .verdict.small .dot { margin-top:3px; }
.walala .dot { width:16px; height:16px; margin-top:4px; }
.walala.roomy .dot { margin-top:5px; }
.walala .money { margin-top:auto; border-top:0; padding-top:0; }
.walala .moneyrow { background:${W.blue}; color:#fff; border:5px solid ${W.ink}; border-radius:90px 90px 0 0; padding:12px 14px 8px; justify-content:center; }
.walala.roomy .moneyrow { border-radius:170px 170px 0 0; padding:24px 14px 10px; text-align:center; }
.walala .big { color:${W.yellow}; font-size:44px; }
.walala .big.earned { color:${W.mint}; }
.walala.roomy .big { font-size:72px; }
.walala .word { font-size:20px; }
.walala.roomy .word { font-size:24px; margin-top:4px; }
.walala .detail { color:${W.ink}; font-size:16px; font-weight:600; margin-top:8px; }
.walala.roomy .detail { font-size:18px; margin-top:10px; }
/* Panels: a thick ink frame, a patterned strip across the top and a white label tab over it. */
.walala .field { position:relative; border:5px solid ${W.ink}; padding:52px 16px 12px; }
.walala .field::before { content:''; position:absolute; left:0; right:0; top:0; height:40px; border-bottom:4px solid ${W.ink}; background:${PAT.zigzag}; }
.walala .head { position:absolute; top:5px; left:10px; z-index:1; height:30px; padding:0 11px; background:#fff; color:${W.ink}; border:3px solid ${W.ink}; font-size:16px; gap:8px; max-width:calc(100% - 20px); }
.walala .head svg path { stroke:${W.ink}; stroke-width:2.4px; stroke-linejoin:round; }
.walala .head .sq { width:16px; height:16px; box-shadow:0 0 0 2px ${W.ink}; }
.walala .toprow > .field:nth-child(2) { border-color:${W.pink}; }
.walala .toprow > .field:nth-child(2)::before { background:${PAT.dots}; }
.walala .reading { margin-top:0; }
.walala .temp { font-size:52px; }
.walala.roomy .temp { font-size:80px; }
.walala .note { font-weight:700; font-size:18px; }
.walala .extra { margin:12px 0 10px; }
.walala .rooms { font-size:17px; }
.walala .ranlbl { font-size:15px; opacity:1; margin:8px 0 5px; }
.walala .strip { height:14px; background:#fff; box-shadow:inset 0 0 0 2px ${W.ink}; }
.walala .toprow > .field:nth-child(2) .strip { background:rgba(255,255,255,.3); box-shadow:none; }
.walala .strip i { background:currentColor; }
.walala .ranlbl .key { box-shadow:0 0 0 1.5px ${W.ink}; }
.walala .ticks { font-size:13px; opacity:1; margin-top:4px; }
.walala .stepper { grid-template-columns:80px minmax(0,1fr) 80px; height:80px; gap:10px; }
.walala.roomy .stepper { grid-template-columns:100px minmax(0,1fr) 100px; height:100px; }
.walala .stepper button { border:4px solid ${W.ink}; font-size:40px; }
.walala .stepper button:disabled { opacity:.5; }
.walala .set { justify-self:center; height:100%; aspect-ratio:1; border-radius:50%; border:4px solid ${W.ink}; background:${W.yellow}; color:${W.ink}; }
.walala .toprow > .field:nth-child(2) .set { background:${W.mint}; }
.walala .set span { font-size:15px; font-weight:700; }
.walala .set b { font-size:26px; }
.walala .lbl { font-size:17px; margin-top:8px; }
.walala .seg { height:56px; }
.walala.roomy .seg { height:76px; }
.walala .seg button, .walala .seg.dark button { border:3px solid ${W.ink}; background:#fff; color:${W.ink}; font-size:18px; }
.walala .seg button.sel { border:3px solid ${W.ink}; background:${W.yellow}; color:${W.ink}; }
.walala .seg button:disabled { opacity:.5; }
.walala .err { display:inline-block; background:${W.warn}; color:#fff; padding:0 6px; letter-spacing:.02em; }
/* The car: pink in a blue frame, chequered strip. */
.walala .car { border-color:${W.blue}; grid-template-columns:minmax(0,1fr) 160px; }
.walala .car.withplan { grid-template-columns:minmax(0,1fr) 196px; }
.walala .car::before { background:${PAT.check}; }
.walala .car .temp { font-size:48px; }
.walala.roomy .car .temp { font-size:58px; }
.walala .carbar { height:14px; background:#fff; border:3px solid ${W.ink}; }
.walala.roomy .carbar { height:16px; }
.walala .carbar div { background:${W.blue}; }
.walala .carside { font-weight:700; white-space:normal; min-width:0; }
.walala .amber { display:inline-block; background:${W.ink}; color:${W.yellow}; padding:1px 6px; }
.walala .chip { box-shadow:inset 0 0 0 3px ${W.ink}; background:#fff; }
.walala .chip.warn { background:${W.ink}; color:${W.yellow}; }
.walala .carrange.inhead { font-size:14px; letter-spacing:.06em; text-transform:uppercase; border-left:3px solid ${W.ink}; padding-left:9px; margin-left:4px; }
.walala .carstatus { font-size:17px; }
.walala .carbtn { background:#fff; border:4px solid ${W.ink}; color:${W.ink}; }
.walala .carbtn.on { background:${W.yellow}; }
.walala .carbtn:disabled { background:rgba(255,255,255,.55); color:${W.ink}; }
.walala .slot { height:28px; padding:0 8px; background:#fff; border:3px solid ${W.ink}; color:${W.ink}; font-size:18px; }
.walala .slot .tag, .walala .slot .now { font-size:12px; letter-spacing:.06em; }
.walala .slots { margin-top:5px; }
.walala .planhead { font-size:15px; }
.walala .full { font-size:16px; margin-top:6px; }
.walala .carleft { font-size:15px; opacity:1; }
.walala .carbtn.short { height:58px; font-size:20px; }
.walala:not(.roomy) .slot { font-size:17px; }
.walala .slot.live { background:${C.green}; color:#fff; }
.walala .slot .tag { opacity:1; }
/* Lights: white, hazard strip. */
.walala .lights { border:5px solid ${W.ink}; padding:52px 14px 12px; }
.walala .lights::before { background:${PAT.hazard}; }
.walala .light { border:4px solid ${W.ink}; }
.walala .light.on { background:${W.yellow}; border-color:${W.ink}; }
`;

if (!customElements.get('home-kiosk-card')) {
  customElements.define('home-kiosk-card', HomeKioskCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: 'home-kiosk-card', name: 'Home kiosk', description: 'Full-screen iPad home (Classic or Walala look).' });
}
