// Home kiosk card: design "12 · Garage Band" for a landscape iPad, as a single Lovelace card in a panel view.
// Everything site-specific (entity ids, light names) comes from the card config, never from this file.
// Talks to HA only through `hass` (states + callService), so no token ever reaches the browser.
//
// Config:
//   type: custom:home-kiosk-card
//   battery: sensor.<inverter>_battery            house battery SOC %
//   kiln_warning: binary_sensor.kiln_warning_outstanding
//   kiln_firing: binary_sensor.kiln_firing
//   evening_gap: sensor.energy_ping_evening_gap   > 0 means the battery will not last to 23:30
//   garage: binary_sensor.garage_door             on = open (read-only: there is no garage control)
//   weather: weather.<home>
//   heating: climate.<thermostat>
//   hot_water: water_heater.<boiler>
//   aircon: climate.<aircon>
//   aircon_room: Living room
//   car_climate: climate.<car>
//   car_battery: sensor.<car>_battery
//   car_range: sensor.<car>_range                 km
//   car_charging: sensor.<car>_charging_state     'Charging' while charging
//   money_import: sensor.<electricity>_current_accumulative_cost   (optional; auto-found)
//   money_gas: sensor.<gas>_current_accumulative_cost              (optional; auto-found)
//   money_export: input_number.daily_export_credit
//   lights: [{entity: light.x, name: "Room"}, ...]
// Add #kiosk-demo=garage|kiln|kiln-warning to the URL to preview a band without the real event.

const FONT_HREF = 'https://fonts.googleapis.com/css2?family=Archivo+Black&family=Instrument+Sans:wght@500;700&display=swap';
const C = { paper: '#F4EFE4', ink: '#161616', grey: '#3F3F3A', red: '#D3261A', blue: '#1F4FB0', yellow: '#F2C230', amber: '#F2A516', green: '#2E7355', warn: '#AE4230', caution: '#B8832A' };
const HOT_WATER = [['off', 'Off'], ['schedule', 'Schedule'], ['boost_1h', 'Boost 1 hr']];
const AIRCON = [['off', 'Off'], ['heat', 'Heat'], ['cool', 'Cool'], ['auto', 'Auto'], ['dry', 'Dry'], ['fan_only', 'Fan']];
const WEATHER = { 'clear-night': 'Clear', cloudy: 'Cloudy', exceptional: 'Unusual weather', fog: 'Foggy', hail: 'Hail', lightning: 'Thunder', 'lightning-rainy': 'Thunder and rain', partlycloudy: 'Partly cloudy', pouring: 'Heavy rain', rainy: 'Rain', snowy: 'Snow', 'snowy-rainy': 'Sleet', sunny: 'Sunny', windy: 'Windy', 'windy-variant': 'Windy' };
const DEAD = ['unavailable', 'unknown'];

const esc = s => String(s).replace(/[&<>"']/g, ch => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[ch]);
const num = v => { if (v === null || v === undefined || v === '') { return null; } const n = Number(v); return Number.isFinite(n) ? n : null; };
const deg = v => `${Math.round(v)}&#176;`;
const halfDeg = v => (Math.round(v * 2) / 2).toString().replace(/\.0$/, '') + '&#176;';
const money = gbp => { const p = Math.round(Math.abs(gbp) * 100); return p < 100 ? `${p}p` : `£${(p / 100).toFixed(2)}`; };
const dayKey = t => { const d = new Date(t); return d.getFullYear() * 10000 + (d.getMonth() + 1) * 100 + d.getDate(); };

class HomeKioskCard extends HTMLElement {
  setConfig(config) {
    this._clearTimers();
    if (this._cfg && this._cfg.weather !== config.weather) {
      this._unsubscribe();
      this._forecast = null;
    }
    this._cfg = config;
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
        this._later('up', () => this._render(), 300);
      };
      this.shadowRoot.addEventListener('pointerdown', e => {
        this._ptrs.add(e.pointerId);
        this._later('down', () => { this._ptrs.clear(); this._render(); }, 5000);
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

  _band() {
    const demo = this._demo();
    const g = this._s(this._cfg.garage);
    if (demo === 'garage' || (g && g.state === 'on')) {
      const mins = demo === 'garage' ? 20 : Math.max(0, Math.floor((Date.now() - Date.parse(g.last_changed)) / 60000));
      return { kind: 'garage', mins };
    }
    if (demo === 'kiln' || demo === 'kiln-warning' || this._on(this._cfg.kiln_firing)) {
      return { kind: 'kiln', warning: demo === 'kiln-warning' || (demo !== 'kiln' && this._on(this._cfg.kiln_warning)) };
    }
    return null;
  }

  _verdict(soc) {
    const now = new Date(), m = now.getHours() * 60 + now.getMinutes();
    if (this._on(this._cfg.kiln_warning) || this._demo() === 'kiln-warning') {
      return { color: C.warn, text: 'Hold big loads<br>till 23:30.' };
    }
    if (m >= 23 * 60 + 30 || m < 5 * 60 + 30) {
      return { color: C.green, text: 'Cheap power now.<br>Run what you like.' };
    }
    if ((this._n(this._cfg.evening_gap) || 0) > 0) {
      return { color: C.warn, text: 'Low power.<br>Run loads after 23:30.' };
    }
    if (soc === null) {
      return { color: C.grey, text: 'Battery reading<br>not available.' };
    }
    if (soc >= 100) {
      return { color: C.green, text: 'Battery full.<br>Run what you like.' };
    }
    if (soc >= 80) {
      return { color: C.green, text: 'Plenty of power.<br>Run what you like.' };
    }
    if (soc >= 40) {
      return { color: C.green, text: 'Fine for<br>everyday use.' };
    }
    return { color: C.caution, text: 'Be careful with power.<br>Big loads after 23:30.' };
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
    const top = this.getBoundingClientRect().top;
    const h = Math.max(600, window.innerHeight - Math.max(0, top));
    const html = this._html(h);
    if (html !== this._last) {
      this._last = html;
      this.shadowRoot.innerHTML = html;
    }
  }

  _html(h) {
    const cfg = this._cfg;
    const band = this._band();
    const soc = this._n(cfg.battery);
    const v = this._verdict(soc);
    const m = this._money();
    const pct = soc === null ? '–' : Math.round(soc) + '%';
    const now = new Date();
    const clock = `${String(now.getHours()).padStart(2, '0')}:${String(now.getMinutes()).padStart(2, '0')}`;

    return `<style>${CSS}</style>
<div class="root${band ? '' : ' roomy'}" style="height:${h}px;grid-template-rows:${band ? '230px ' : ''}minmax(0,1fr)">
  ${band ? this._bandHtml(band) : ''}
  <div class="below">
    <div class="calm">
      <div class="clockrow"><span class="clock">${clock}</span><span class="wx">${this._weather()}</span></div>
      <div>
        ${band ? `<div class="battrow"><span class="batt">${pct}</span><span class="cap">Home<br>battery</span></div>`
          : `<div class="cap">Home battery</div><div class="batt">${pct}</div>`}
        <div class="bar"><div style="width:${soc === null ? 0 : Math.max(0, Math.min(100, soc))}%;background:${v.color}"></div></div>
        <div class="verdict"><span class="dot" style="background:${v.color}"></span><span>${v.text}</span></div>
      </div>
      ${m ? `<div class="money">
        <div class="moneyrow"><span class="big">${money(m.net)}</span> <span class="word">${m.net >= 0 ? 'spent' : 'earned'} today</span></div>
        <div class="detail">${esc(m.detail)}</div>
      </div>` : '<div></div>'}
    </div>
    <div class="right">
      <div class="toprow">${this._heatingHtml()}${this._airconHtml()}</div>
      <div class="botrow">${this._carHtml()}${this._lightsHtml()}</div>
    </div>
  </div>
</div>`;
  }

  _bandHtml(b) {
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
    return `<div class="band" style="background:${C.warn};color:#fff">
      <div class="disc"></div>
      <div class="bandtext">
        <div class="needs"><span class="sq" style="background:#fff"></span><span>Heads up</span></div>
        <div class="headline">The kiln is firing.</div>
        <div class="sub">${b.warning ? 'Hold big loads till 23:30.' : 'Nothing to do.'}</div>
      </div>
      <div class="bandblock"><svg width="110" height="96" viewBox="0 0 26 24" aria-hidden="true"><path d="M13 0 L26 24 L0 24 Z" fill="${C.warn}"></path></svg><span class="unit" style="color:#fff">Kiln</span></div>
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

  _heatingHtml() {
    const id = this._cfg.heating, s = this._s(id);
    if (!s) {
      return '<div class="field" style="background:#D3261A;color:#fff"></div>';
    }
    const cur = num(s.attributes.current_temperature);
    const act = s.attributes.hvac_action;
    const word = DEAD.includes(s.state) ? 'Offline.' : s.state === 'off' ? 'Turned off.' : act === 'heating' ? 'Heating up.' : 'Resting.';
    const hwId = this._cfg.hot_water, hw = this._s(hwId);
    let hwRow = '';
    if (hw) {
      const p = this._pending(hwId, hw.state.startsWith('boost') ? 'boost_1h' : hw.state);
      const mode = p && p.want ? p.want : hw.state;
      hwRow = `<div class="lbl">Hot water${p && p.fail ? ' <span class="err">· no response</span>' : ''}</div>
      <div class="seg" style="grid-template-columns:1fr 1.4fr 1.4fr">${HOT_WATER.map(([k, l]) => {
        const sel = k === mode || (k === 'boost_1h' && mode.startsWith('boost'));
        return `<button data-act="hw" data-entity="${hwId}" data-val="${k}" class="${sel ? 'sel' : ''}" aria-pressed="${sel}" ${DEAD.includes(hw.state) ? 'disabled' : ''}>${l}</button>`;
      }).join('')}</div>`;
    }
    return `<div class="field" style="background:${C.red};color:#fff">
      <div class="head"><svg width="22" height="20" viewBox="0 0 26 24" aria-hidden="true"><path d="M13 0 L26 24 L0 24 Z" fill="#fff"></path></svg><span>Heating${this._stepErr(id)}</span></div>
      <div class="reading"><span class="temp">${cur === null ? '–' : deg(cur)}</span><span class="note">Hallway.<br>${word}</span></div>
      ${this._stepper(id, s, 'heating')}
      ${hwRow}
    </div>`;
  }

  _airconHtml() {
    const id = this._cfg.aircon, s = this._s(id);
    if (!s) {
      return '<div class="field" style="background:#161616;color:#fff"></div>';
    }
    const p = this._pending(id, s.state);
    const mode = p && p.want ? p.want : s.state;
    const cur = num(s.attributes.current_temperature);
    const fan = s.attributes.fan_mode;
    const note = DEAD.includes(s.state) ? 'Offline.' : mode === 'off' ? 'Turned off.' : fan ? `Fan on ${esc(String(fan).replace(/_/g, ' '))}.` : '';
    const modes = AIRCON.filter(([k]) => (s.attributes.hvac_modes || []).includes(k));
    return `<div class="field" style="background:${C.ink};color:#fff">
      <div class="head"><span class="sq" style="background:${C.yellow}"></span><span>Air con${this._stepErr(id)}</span></div>
      <div class="reading"><span class="temp">${cur === null ? '–' : deg(cur)}</span><span class="note">${esc(this._cfg.aircon_room || 'Living room')}.<br>${note}</span></div>
      ${this._stepper(id, { ...s, state: mode }, 'air con')}
      <div class="lbl">Mode${p && p.fail ? ' <span class="err">· no response</span>' : ''}</div>
      <div class="seg dark" style="grid-template-columns:repeat(${modes.length},minmax(0,1fr))">${modes.map(([k, l]) =>
        `<button data-act="hvac" data-entity="${id}" data-val="${k}" class="${k === mode ? 'sel' : ''}" aria-pressed="${k === mode}" ${DEAD.includes(s.state) ? 'disabled' : ''}>${l}</button>`).join('')}</div>
    </div>`;
  }

  _carHtml() {
    const cfg = this._cfg, id = cfg.car_climate, s = this._s(id);
    const soc = this._n(cfg.car_battery), km = this._n(cfg.car_range);
    const ch = this._s(cfg.car_charging);
    let line = soc === null ? '' : `${Math.round(soc)}%`;
    if (km !== null) {
      line += `, about ${Math.round(km * 0.621371)}&#160;miles`;
    }
    if (ch && ch.state === 'Charging') {
      line += ' · Charging';
    }
    const na = !s || DEAD.includes(s.state);
    const on = !na && s.state !== 'off';
    const a = na ? {} : s.attributes;
    const cur = num(a.current_temperature), tgt = num(a.temperature);
    const warm = cur !== null && tgt !== null && cur < tgt;
    // An unavailable car says nothing about whether it turned off, so it must not acknowledge a pending call.
    const p = this._pending('car', na ? undefined : on ? 'on' : 'off');
    const aim = tgt === null ? 'Climate on.' : `${warm ? 'Warming' : 'Cooling'} to ${halfDeg(tgt)}.`;
    let status, btn, btnStyle = '';
    if (na) {
      status = 'Out of range.';
      btn = 'Car out<br>of range';
    } else if (p && p.fail) {
      status = on ? aim : 'Climate off.';
      btn = 'No answer<br><small>try again</small>';
    } else if (p) {
      status = p.want === 'on' ? 'Turning on…' : 'Turning off…';
      btn = status;
      btnStyle = 'opacity:.75';
    } else if (on) {
      status = aim;
      btn = `Stop<br>${tgt === null ? 'climate' : warm ? 'warming' : 'cooling'}`;
    } else {
      status = 'Climate off.';
      btn = tgt === null ? 'Turn on<br>climate' : `${warm ? 'Warm' : 'Cool'} the car<br><small>to ${halfDeg(tgt)}</small>`;
    }
    return `<div class="field car" style="background:${C.blue};color:#fff;${na ? 'opacity:.6' : ''}">
      <div class="carinfo">
        <div class="head"><span class="sq" style="background:#fff;border-radius:50%"></span><span>Car</span></div>
        <div><div class="carread"><span class="temp">${cur === null ? '–' : deg(cur)}</span><span class="note bold">inside.</span></div>
        <div class="carstatus">${status}</div></div>
        <div class="carline">${line}</div>
      </div>
      <button class="carbtn${on && !p ? ' on' : ''}" data-act="car" data-entity="${id}" data-val="${on ? 'off' : 'on'}" ${na || (p && !p.fail) ? 'disabled' : ''} style="${btnStyle}">${btn}</button>
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
      <div class="head"><svg width="20" height="18" viewBox="0 0 26 24" aria-hidden="true"><path d="M13 0 L26 24 L0 24 Z" fill="${C.red}"></path></svg><span>Lights</span></div>
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
.clock { font-family:'Archivo Black',sans-serif; font-size:62px; line-height:.9; letter-spacing:-.04em; }
.wx { font-size:19px; font-weight:500; line-height:1.3; color:${C.grey}; }
.battrow { display:flex; align-items:flex-end; justify-content:space-between; gap:12px; }
.batt { font-family:'Archivo Black',sans-serif; font-size:80px; line-height:.88; letter-spacing:-.045em; }
.cap { font-size:18px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; text-align:right; line-height:1.2; }
.bar { height:12px; margin-top:12px; background:#DDD5C4; display:flex; }
.bar > div { height:100%; }
.verdict { display:flex; align-items:flex-start; gap:12px; margin-top:16px; font-size:32px; font-weight:700; line-height:1.08; letter-spacing:-.02em; }
.dot { width:22px; height:22px; border-radius:50%; flex:none; margin-top:6px; }
.money { border-top:4px solid ${C.ink}; padding-top:12px; }
.moneyrow { display:flex; align-items:baseline; gap:12px; }
.big { font-family:'Archivo Black',sans-serif; font-size:56px; line-height:.95; letter-spacing:-.04em; }
.word { font-size:26px; font-weight:700; }
.detail { font-size:18px; font-weight:500; color:${C.grey}; margin-top:8px; line-height:1.3; }
.right { display:grid; grid-template-rows:minmax(0,1.8fr) minmax(0,1fr); gap:14px; min-height:0; min-width:0; }
.toprow { display:grid; grid-template-columns:repeat(2,minmax(0,1fr)); gap:14px; min-height:0; }
.botrow { display:grid; grid-template-columns:minmax(0,1fr) 250px; gap:14px; min-height:0; }
.field { padding:20px; display:flex; flex-direction:column; min-width:0; min-height:0; overflow:hidden; }
.head { display:flex; align-items:center; gap:10px; font-size:20px; font-weight:700; letter-spacing:.12em; text-transform:uppercase; }
.reading { display:flex; align-items:center; gap:12px; margin-top:8px; }
.temp { font-family:'Archivo Black',sans-serif; font-size:60px; line-height:.95; letter-spacing:-.04em; }
.note { font-size:19px; font-weight:500; line-height:1.25; }
.note.bold { font-weight:700; }
.carread { display:flex; align-items:baseline; gap:10px; }
.carstatus { font-size:22px; font-weight:700; margin-top:6px; }
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
.car { display:grid; grid-template-columns:minmax(0,1fr) 200px; gap:16px; }
.carinfo { display:flex; flex-direction:column; justify-content:space-between; min-width:0; }
.car .temp { font-size:46px; }
.carline { font-size:18px; font-weight:500; }
.carbtn { background:#fff; color:${C.ink}; padding:0 12px; font-family:'Archivo Black',sans-serif; font-size:26px; line-height:1.05; letter-spacing:-.02em; }
.carbtn small { font-family:'Instrument Sans',sans-serif; font-size:19px; font-weight:700; letter-spacing:0; }
.carbtn.on { background:${C.yellow}; }
.carbtn:disabled { background:rgba(255,255,255,.35); color:#fff; }
.lights { background:#fff; border:3px solid ${C.ink}; padding:14px; }
.lightgrid { display:grid; gap:8px; margin-top:auto; min-height:108px; flex:1 1 auto; max-height:100%; padding-top:10px; }
.light { background:#fff; border:3px solid ${C.ink}; color:${C.ink}; font-size:18px; font-weight:700; line-height:1.2; padding:0 6px; display:flex; flex-direction:column; align-items:center; justify-content:center; }
.light.on { background:${C.yellow}; border-color:${C.yellow}; }
.light small { font-size:18px; font-weight:500; }
.none, .alloff { font-size:18px; font-weight:700; margin-top:8px; }
.none { margin-top:auto; }
.roomy .clock { font-size:84px; }
.roomy .clockrow { flex-direction:column; align-items:flex-start; gap:10px; }
.roomy .wx { font-size:22px; }
.roomy .batt { font-size:120px; margin-top:10px; }
.roomy .bar { height:16px; margin-top:14px; }
.roomy .verdict { font-size:38px; }
.roomy .dot { margin-top:8px; }
.roomy .money { padding-top:16px; }
.roomy .moneyrow { display:block; }
.roomy .big { font-size:84px; line-height:.9; display:block; }
.roomy .word { font-size:30px; display:block; margin-top:6px; }
.roomy .detail { font-size:21px; margin-top:12px; line-height:1.32; }
.roomy .reading { margin-top:12px; }
.roomy .temp { font-size:80px; }
.roomy .car .temp { font-size:60px; }
.roomy .carstatus { font-size:24px; }
.roomy .stepper { grid-template-columns:104px minmax(0,1fr) 104px; height:104px; }
.roomy .lbl { margin-top:14px; }
.roomy .seg { height:80px; }
.roomy .seg.dark button { font-size:17px; }
`;

if (!customElements.get('home-kiosk-card')) {
  customElements.define('home-kiosk-card', HomeKioskCard);
  window.customCards = window.customCards || [];
  window.customCards.push({ type: 'home-kiosk-card', name: 'Home kiosk', description: 'Full-screen iPad home (Garage Band design).' });
}
