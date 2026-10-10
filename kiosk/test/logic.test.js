// Logic tests for home-kiosk-card.js: loads the real file in a vm with a stubbed DOM, fake timers and a fake hass.
// Run: node kiosk/test/logic.test.js
'use strict';
const fs = require('fs'), path = require('path'), vm = require('vm'), assert = require('assert');
const SRC = fs.readFileSync(path.join(__dirname, '..', 'home-kiosk-card.js'), 'utf8');
const RealDate = Date;
const flush = () => new Promise(r => setImmediate(r));
const rejections = [];
process.on('unhandledRejection', e => rejections.push(e));

function deferred() {
  let resolve, reject;
  const promise = new Promise((a, b) => { resolve = a; reject = b; });
  return { promise, resolve, reject };
}

function world({ time = [2026, 9, 2, 14, 32], states = {}, config = {}, history } = {}) {
  let now = new RealDate(...time).getTime();
  let seq = 0;
  const timers = new Map();
  const later = (fn, ms, every) => { const id = ++seq; timers.set(id, { at: now + (ms || 0), fn, every }); return id; };
  const clear = id => { timers.delete(id); };
  async function advance(ms) {
    await flush();
    const end = now + ms;
    for (;;) {
      const due = [...timers.entries()].filter(([, t]) => t.at <= end).sort((a, b) => a[1].at - b[1].at)[0];
      if (!due) {
        break;
      }
      const [id, t] = due;
      now = t.at;
      if (t.every) {
        t.at += t.every;
      } else {
        timers.delete(id);
      }
      t.fn();
      await flush();
    }
    now = end;
    await flush();
  }
  class FakeDate extends RealDate {
    constructor(...a) { if (a.length === 0) { super(now); } else { super(...a); } }
    static now() { return now; }
  }
  const registry = new Map();
  class HTMLElement {
    constructor() { this.isConnected = false; }
    getBoundingClientRect() { return { top: 0 }; }
    attachShadow() {
      const ls = {};
      this.shadowRoot = { innerHTML: '', addEventListener: (t, f) => { (ls[t] = ls[t] || []).push(f); }, fire: (t, e) => (ls[t] || []).forEach(f => f(e)) };
      return this.shadowRoot;
    }
  }
  const ctx = {
    HTMLElement, Date: FakeDate, Promise, console,
    setTimeout: (fn, ms) => later(fn, ms), clearTimeout: clear,
    setInterval: (fn, ms) => later(fn, ms, ms), clearInterval: clear,
    customElements: { get: n => registry.get(n), define: (n, c) => { if (registry.has(n)) { throw new Error('already defined'); } registry.set(n, c); } },
    document: { querySelector: () => ({}), createElement: () => ({}), head: { appendChild() {} } },
    location: { hash: '', pathname: '/home-kiosk/home' },
  };
  ctx.window = ctx;
  ctx.window.innerHeight = 820;
  ctx.window.addEventListener = () => {};
  ctx.window.removeEventListener = () => {};
  vm.createContext(ctx);
  vm.runInContext(SRC, ctx);
  vm.runInContext(`{${SRC}}`, ctx); // a second load in its own scope, as a re-imported module, must not throw on define
  const Card = registry.get('home-kiosk-card');
  const calls = [], pendingCalls = [], subs = [], ws = [];
  const hass = {
    states: {
      'climate.heat': { state: 'heat', attributes: { temperature: 21, current_temperature: 19, target_temp_step: 0.5, min_temp: 5, max_temp: 30 } },
      'climate.ac': { state: 'cool', attributes: { temperature: 22, current_temperature: 24, hvac_modes: ['off', 'cool', 'heat', 'fan_only', 'dry'], min_temp: 16, max_temp: 30 } },
      'climate.car': { state: 'off', attributes: { temperature: 21, current_temperature: 25 } },
      'water_heater.hw': { state: 'off', attributes: {} },
      'weather.home': { state: 'sunny', attributes: { temperature: 18 } },
      'sensor.imp': { state: '1.20', attributes: {} },
      'sensor.gas': { state: '0.80', attributes: {} },
      'input_number.exp': { state: '0.50', attributes: {} },
      ...states,
    },
    // Each call returns a promise the test settles by hand.
    callService: (d, s, data) => { const x = deferred(); calls.push([d, s, data]); pendingCalls.push(x); return x.promise; },
    connection: { subscribeMessage: (cb, msg) => { const x = deferred(); subs.push({ cb, msg, ...x }); return x.promise; } },
    callWS: msg => { ws.push(msg); return history ? Promise.resolve(history) : Promise.reject(new Error('no history')); },
  };
  const card = new Card();
  card.setConfig({ heating: 'climate.heat', aircon: 'climate.ac', car_climate: 'climate.car', hot_water: 'water_heater.hw', weather: 'weather.home', money_import: 'sensor.imp', money_gas: 'sensor.gas', money_export: 'input_number.exp', ...config });
  card.isConnected = true;
  card.connectedCallback();
  card.hass = hass;
  const html = () => card.shadowRoot.innerHTML;
  const button = (act, entity, val) => {
    const re = /<button([^>]*)>/g;
    let m;
    while ((m = re.exec(html()))) {
      const a = m[1];
      if (a.includes(`data-act="${act}"`) && a.includes(`data-entity="${entity}"`) && (val === undefined || a.includes(`data-val="${val}"`))) {
        const v = /data-val="([^"]*)"/.exec(a)[1];
        return { disabled: /\sdisabled(\s|$)/.test(a), dataset: { act, entity, val: v } };
      }
    }
    return null;
  };
  const click = (act, entity, val) => {
    const b = button(act, entity, val);
    assert.ok(b, `button ${act} ${entity} ${val} not rendered`);
    card.shadowRoot.fire('click', { target: { closest: () => b } });
  };
  const update = patch => {
    for (const [id, st] of Object.entries(patch)) {
      hass.states[id] = { ...hass.states[id], ...st, attributes: { ...(hass.states[id] || {}).attributes, ...(st.attributes || {}) } };
    }
    card.hass = { ...hass };
  };
  return { card, hass, calls, pendingCalls, subs, ws, timers, advance, html, button, click, update, location: ctx.location, setNow: t => { now = t; } };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('null attributes: car asleep shows no 0 degrees and no "to 0"', async () => {
  const w = world({ states: { 'climate.car': { state: 'off', attributes: { temperature: null, current_temperature: null } } } });
  assert.ok(!/[^\d.]0(\.0)?&#176;/.test(w.html()), 'no 0 degree reading');
  assert.ok(w.html().includes('Turn on<br>climate'));
  w.update({ 'climate.car': { state: 'heat_cool' } });
  assert.ok(w.html().includes('Climate on'));
  assert.ok(!/to 0/.test(w.html()));
});

test('null attributes: air con fan_only disables the stepper and a forced tap sends nothing', async () => {
  const w = world({ states: { 'climate.ac': { state: 'fan_only', attributes: { temperature: null, current_temperature: 24, hvac_modes: ['off', 'cool', 'fan_only'], min_temp: 16 } } } });
  assert.ok(w.button('step', 'climate.ac', '1').disabled);
  w.card._step('climate.ac', 1);
  await w.advance(2000);
  assert.strictEqual(w.calls.length, 0);
});

test('unavailable heating disables the stepper and _step refuses', async () => {
  const w = world({ states: { 'climate.heat': { state: 'unavailable', attributes: { temperature: 21 } } } });
  assert.ok(w.button('step', 'climate.heat', '1').disabled);
  w.card._step('climate.heat', 1);
  await w.advance(2000);
  assert.strictEqual(w.calls.length, 0);
});

test('stepper generation race: old cleanup does not drop a newer target', async () => {
  const w = world();
  w.click('step', 'climate.heat', '1');
  await w.advance(900);
  w.pendingCalls[0].resolve();      // call 1 lands; its cleanup is due 4 s from now
  await w.advance(3000);
  w.click('step', 'climate.heat', '1');
  await w.advance(900);             // call 2 sent, still in flight
  await w.advance(200);             // old cleanup time has passed
  assert.strictEqual(w.card._target['climate.heat'], 22);
  w.click('step', 'climate.heat', '1');
  await w.advance(900);
  assert.deepStrictEqual(w.calls.map(c => c[2].temperature), [21.5, 22, 22.5]);
  w.pendingCalls[1].resolve();
  w.pendingCalls[2].resolve();
  await w.advance(4100);
  assert.strictEqual(w.card._target['climate.heat'], undefined);
});

test('set_temperature failure is caught and shown for the current adjustment only', async () => {
  const w = world();
  w.click('step', 'climate.heat', '1');
  await w.advance(900);
  w.pendingCalls[0].reject(new Error('timeout'));
  await w.advance(10);
  assert.ok(/Heating <span class="err">· no response/.test(w.html()));
  assert.strictEqual(rejections.length, 0, 'no unhandled rejection');
  w.click('step', 'climate.heat', '1');   // a new adjustment clears the marker
  assert.ok(!w.html().includes('no response'));
  // A failure of the older adjustment after a newer one began shows nothing.
  await w.advance(900);
  w.click('step', 'climate.ac', '1');
  await w.advance(900);
  w.click('step', 'climate.ac', '1');
  w.pendingCalls[2].reject(new Error('late'));
  await w.advance(10);
  assert.ok(!/Air con <span class="err">/.test(w.html()));
});

test('pending timeout shows "no response" for ~8 s, then clears', async () => {
  const w = world();
  w.click('hvac', 'climate.ac', 'heat');
  await w.advance(61000);
  w.card._render(true);
  assert.ok(/Mode <span class="err">· no response/.test(w.html()));
  await w.advance(3000);
  w.card._render(true);
  assert.ok(w.html().includes('no response'), 'still visible on the next render');
  await w.advance(6000);
  w.card._render(true);
  assert.ok(!w.html().includes('no response'));
});

test('an older failed call does not overwrite a newer pending state', async () => {
  const w = world();
  w.click('hvac', 'climate.ac', 'heat');
  w.click('hvac', 'climate.ac', 'off');
  w.pendingCalls[0].reject(new Error('old'));
  await w.advance(10);
  assert.strictEqual(w.card._pend['climate.ac'].want, 'off');
  assert.ok(!w.html().includes('no response'));
  w.pendingCalls[1].reject(new Error('new'));
  await w.advance(10);
  assert.ok(w.card._pend['climate.ac'].fail);
});

test('car: unavailable does not acknowledge a pending off', async () => {
  const w = world({ states: { 'climate.car': { state: 'heat_cool', attributes: { temperature: 21, current_temperature: 30 } } } });
  w.click('car', 'climate.car', 'off');
  w.update({ 'climate.car': { state: 'unavailable' } });
  assert.ok(w.card._pend.car && w.card._pend.car.want === 'off');
  w.update({ 'climate.car': { state: 'heat_cool' } });
  assert.ok(w.html().includes('Turning off…'));
  w.update({ 'climate.car': { state: 'off' } });
  assert.strictEqual(w.card._pend.car, undefined);
});

test('hot water: Boost pending clears on any boost* state', async () => {
  const w = world();
  w.click('hw', 'water_heater.hw', 'boost_30m');
  assert.ok(w.card._pend['water_heater.hw']);
  w.update({ 'water_heater.hw': { state: 'boost' } });
  assert.strictEqual(w.card._pend['water_heater.hw'], undefined);
  assert.ok(w.button('hw', 'water_heater.hw', 'boost_30m') && /class="sel"/.test(w.html()));
});

test('forecast: failure retries with backoff, not on every hass update', async () => {
  const w = world();
  assert.strictEqual(w.subs.length, 1);
  w.subs[0].reject(new Error('no'));
  await w.advance(10);
  for (let i = 0; i < 5; i++) {
    w.update({});
  }
  assert.strictEqual(w.subs.length, 1, 'no resubscribe storm');
  await w.advance(5000);
  assert.strictEqual(w.subs.length, 2, 'retried after 5 s');
  w.subs[1].reject(new Error('no'));
  await w.advance(9000);
  assert.strictEqual(w.subs.length, 2, 'backoff doubles');
  await w.advance(1100);
  assert.strictEqual(w.subs.length, 3);
});

test('forecast: weather change unsubscribes, clears, and a stale rejection keeps the new subscription', async () => {
  const w = world();
  const unsub = { n: 0 };
  w.subs[0].cb({ forecast: [{ datetime: new Date(Date.now() + 86400000).toISOString(), temperature: 17 }] });
  w.subs[0].resolve(() => { unsub.n++; });
  await w.advance(10);
  assert.ok(w.card._forecast.length);
  w.card.setConfig({ ...w.card._cfg, weather: 'weather.other' });
  await w.advance(10);
  assert.strictEqual(unsub.n, 1);
  assert.strictEqual(w.card._forecast, null);
  assert.strictEqual(w.subs.length, 2);
  assert.strictEqual(w.subs[1].msg.entity_id, 'weather.other');
  w.subs[0].cb({ forecast: [{ temperature: 99 }] });    // a late event from the old subscription is ignored
  assert.strictEqual(w.card._forecast, null);
  // Race: subscription A still pending, B replaces it, then A rejects.
  w.card.setConfig({ ...w.card._cfg, weather: 'weather.third' });
  const current = w.card._unsubForecast;
  w.subs[1].reject(new Error('stale'));
  await w.advance(10);
  assert.strictEqual(w.card._unsubForecast, current);
  assert.strictEqual(w.card._timers.forecast, undefined, 'no retry scheduled for a stale failure');
});

test('tomorrow is the first entry after today, also just after midnight', async () => {
  const w = world({ time: [2026, 9, 3, 0, 30] });
  const at = (d, h) => new Date(2026, 9, d, h).toISOString();
  w.subs[0].cb({ forecast: [{ datetime: at(2, 12), temperature: 11 }, { datetime: at(3, 12), temperature: 14 }, { datetime: at(4, 12), temperature: 17 }] });
  w.card._render(true);
  assert.ok(w.html().includes('Tomorrow 17&#176;'), w.html().match(/Tomorrow[^<]*/));
});

test('disconnect and setConfig clear every timer; a tap within 900 ms sends nothing', async () => {
  const w = world();
  w.click('step', 'climate.heat', '1');
  w.subs[0].reject(new Error('x'));
  await w.advance(10);
  w.card.disconnectedCallback();
  w.card.isConnected = false;
  assert.strictEqual(w.timers.size, 0, 'no timers left');
  await w.advance(10000);
  assert.strictEqual(w.calls.length, 0);
  const v = world();
  v.click('step', 'climate.heat', '1');
  v.card.setConfig(v.card._cfg);
  await v.advance(2000);
  assert.strictEqual(v.calls.length, 0);
});

test('a stuck pointer is released after 5 s; render waits for the click', async () => {
  const w = world();
  w.card.shadowRoot.fire('pointerdown', { pointerId: 1, target: { closest: () => null } });
  w.update({ 'climate.heat': { attributes: { current_temperature: 23 } } });
  assert.ok(!w.html().includes('23.0&#176;'), 'held while pressed');
  w.card.shadowRoot.fire('pointerup', { pointerId: 1 });
  await w.advance(0);
  assert.ok(!w.html().includes('23.0&#176;'), 'not rebuilt before the click');
  w.card.shadowRoot.fire('click', { target: { closest: () => null } });
  assert.ok(w.html().includes('23.0&#176;'), 'rendered after the click');
  w.card.shadowRoot.fire('pointerdown', { pointerId: 2, target: { closest: () => null } });     // never released
  w.update({ 'climate.heat': { attributes: { current_temperature: 24 } } });
  await w.advance(5000);
  assert.ok(w.html().includes('24.0&#176;'), 'safety reset');
});

test('long-pressing the clock opens the dashboard with Kiosk Mode off; a short tap does not', async () => {
  const w = world();
  const clock = { closest: sel => (sel === '.clock' ? {} : null) };
  w.card.shadowRoot.fire('pointerdown', { pointerId: 1, target: clock });
  await w.advance(300);
  w.card.shadowRoot.fire('pointerup', { pointerId: 1 });
  await w.advance(2000);
  assert.strictEqual(w.location.href, undefined, 'short tap stays put');
  w.card.shadowRoot.fire('pointerdown', { pointerId: 2, target: clock });
  await w.advance(800);
  assert.strictEqual(w.location.href, '/home-kiosk/home?disable_km');
});

test('money: an unavailable configured part hides the net; negative cost reads as credit', async () => {
  const w = world({ states: { 'sensor.imp': { state: 'unavailable', attributes: {} } } });
  assert.ok(!w.html().includes('class="money"'));
  const v = world({ states: { 'sensor.gas': { state: '-0.30', attributes: {} } } });
  assert.ok(v.html().includes('Bought £1.20 of electricity, sold 50p of sunshine, 30p of gas credit.'), v.html().match(/class="detail">[^<]*/));
  assert.ok(v.html().includes('40p</span> <span class="word">spent today'));
});

test('weather: unknown state is escaped', async () => {
  const w = world({ states: { 'weather.home': { state: '<b>x</b>', attributes: {} } } });
  assert.ok(w.html().includes('&lt;b&gt;x&lt;/b&gt;'));
});

test('today: rooms line, extras, and run strips from history', async () => {
  const at = (h, m) => new RealDate(2026, 9, 2, h, m).getTime() / 1000;
  const w = world({
    states: {
      'sensor.m': { state: '21.4', attributes: { unit_of_measurement: '°C' } },
      'sensor.e': { state: 'unavailable', attributes: {} },
      'sensor.rh': { state: '55.2', attributes: { unit_of_measurement: '%' } },
      'sensor.ace': { state: '2208', last_updated: new RealDate(2026, 9, 2, 13, 49).toISOString(), attributes: { unit_of_measurement: 'Wh' } },
    },
    config: {
      heating_rooms: [{ entity: 'sensor.m', name: 'Bedroom' }, { entity: 'sensor.e', name: 'Nursery' }],
      aircon_extras: [{ entity: 'weather.home', name: 'Outside' }, { entity: 'sensor.rh', name: 'Humidity' }],
      aircon_energy: 'sensor.ace',
    },
    history: {
      // Only the first entry carries attributes when they do not change; later ones inherit them.
      'climate.heat': [{ s: 'heat', a: { hvac_action: 'idle' }, lu: at(0, 0) }, { s: 'heat', a: { hvac_action: 'heating' }, lu: at(6, 0) },
        { s: 'heat', a: { hvac_action: 'idle' }, lu: at(7, 30) }, { s: 'heat', a: { hvac_action: 'heating' }, lu: at(12, 30) },
        { s: 'heat', a: { hvac_action: 'idle' }, lu: at(13, 0) }],
      // The LG counter updates hourly at :49: ~200 Wh an hour while working, 11 Wh on standby; yesterday's total at midnight.
      'sensor.ace': [[0, 0, '1605'], [0, 49, '152'], [1, 49, '346'], [2, 49, 'unavailable'], [3, 49, '760'], [4, 49, '960'], [5, 49, '1169'],
        [6, 49, '1385'], [7, 49, '1600'], [8, 49, '1820'], [9, 49, '2027'], [10, 49, '2175'], [11, 49, '2186'], [12, 49, '2197']]
        .map(([hh, mm, v]) => ({ s: v, lu: at(hh, mm) })),
    },
  });
  await w.advance(0);
  w.card._render(true);
  const h = w.html();
  assert.ok(h.includes('Bedroom 21&#176;') && !h.includes('Nursery'), 'rooms line skips an unavailable sensor');
  assert.ok(h.includes('Outside 18&#176; &#183; Humidity 55%'), 'extras read weather temperature and % sensors');
  assert.ok(h.includes('Ran 2&#160;h today'), 'heating counts hvac_action heating only');
  assert.ok(h.includes('Working 10&#160;h 49&#160;min today &#183; 2.2&#160;kWh'), 'air con counts hours above standby from its energy counter');
  assert.strictEqual((h.match(/background:#F2C230"><\/b>/g) || []).length, 1, 'one merged working span, standby hours left empty');
  assert.strictEqual(w.ws.length, 1);
  assert.strictEqual(w.ws[0].significant_changes_only, false);
  await w.advance(60000);
  assert.strictEqual(w.ws.length, 1, 'no refetch inside five minutes');
  await w.advance(300000);
  assert.strictEqual(w.ws.length, 2, 'refetched after five minutes');
});

test('today: no history means no strip, and a failed fetch is not retried on every render', async () => {
  const w = world();
  await w.advance(0);
  assert.ok(!w.html().includes('today</div>') && !w.html().includes('class="strip"'));
  await w.advance(60000);
  assert.strictEqual(w.ws.length, 1);
});

const PWR = { battery: 'sensor.soc', evening_gap: 'sensor.gap', low_power: 'binary_sensor.lp', ping_morning: 'input_datetime.ping', kiln_warning: 'binary_sensor.kw' };
const pwr = (soc, o = {}) => Object.assign({ 'sensor.soc': { state: String(soc), attributes: {} }, 'sensor.gap': { state: '0', attributes: {} }, 'binary_sensor.lp': { state: 'off', attributes: {} },
  'input_datetime.ping': { state: '2026-01-01 09:00:00', attributes: {} }, 'binary_sensor.kw': { state: 'off', attributes: {} } }, o);
const pinged = mon => ({ 'input_datetime.ping': { state: `2026-${String(mon).padStart(2, '0')}-15 09:00:00`, attributes: {} } });
const on = (w, mon, h, m) => { w.setNow(new Date(2026, mon - 1, 15, h, m).getTime()); w.card._render(true); };
const line = w => (w.html().match(/class="verdict">.*?<span>(.*?)<\/span><\/div>/) || [])[1] || '';

test('battery line: says what the phone pings said today, silent otherwise', async () => {
  const w = world({ config: PWR, states: pwr(95) });
  on(w, 10, 14, 0);
  assert.strictEqual(line(w), '', 'a normal day says nothing, at 95%');
  w.update(pwr(79)); on(w, 10, 14, 0);
  assert.strictEqual(line(w), '', 'nor at 79%');
  w.update(pwr(60, pinged(10))); on(w, 10, 9, 5);
  assert.strictEqual(line(w), 'Low energy day.<br>Be careful with power.');
  on(w, 10, 18, 0);
  assert.strictEqual(line(w), '', 'the morning message is gone by the evening');
  w.update(pwr(90, pinged(7))); on(w, 7, 10, 0);
  assert.strictEqual(line(w), 'Good sunny day.<br>Earlier the better.');
  w.update(pwr(90, pinged(6))); on(w, 7, 10, 0);
  assert.strictEqual(line(w), '', 'an old stamp is not today');
  w.update(pwr(30, { 'sensor.gap': { state: '1.2', attributes: {} }, 'binary_sensor.lp': { state: 'on', attributes: {} } })); on(w, 10, 19, 0);
  assert.strictEqual(line(w), '', 'low power is the band now, not the line');
  w.update(pwr(30, { 'binary_sensor.kw': { state: 'on', attributes: {} } })); on(w, 10, 19, 0);
  assert.strictEqual(line(w), 'Hold big loads<br>till 23:30.');
  w.update(pwr(30)); on(w, 10, 23, 45);
  assert.strictEqual(line(w), 'Cheap power now.<br>Run what you like.');
  on(w, 10, 5, 30);
  assert.strictEqual(line(w), '', 'cheap rate ends at 05:30');
});

test('battery flow: arrow only beyond 200 W, up and green when charging', async () => {
  const w = world({ config: { battery: 'sensor.soc', battery_power: 'sensor.bp' }, states: { 'sensor.soc': { state: '60', attributes: {} }, 'sensor.bp': { state: '150', attributes: {} } } });
  w.card._render(true);
  assert.ok(!w.html().includes('class="flow"'), '150 W shows nothing');
  w.update({ 'sensor.bp': { state: '1450', attributes: {} } });
  assert.ok(/class="flow" style="color:#2E7355">.*M6 1 .*1\.4&#160;kW/.test(w.html()), 'charging: green, up, 1.4 kW');
  w.update({ 'sensor.bp': { state: '-650', attributes: {} } });
  assert.ok(/class="flow" style="color:#B8832A">.*M6 11 .*0\.7&#160;kW/.test(w.html()), 'discharging: amber, down');
});

const band = w => (w.html().match(/class="headline">(.*?)<\/div>/) || [])[1] || '';

test('bands: low power, and the car charging off the house battery outside the cheap rate', async () => {
  const cfg = { ...PWR, battery_power: 'sensor.bp', car_charging: 'sensor.cch', car_power: 'sensor.ckw', garage: 'binary_sensor.g', kiln_firing: 'binary_sensor.kf' };
  const st = (o = {}) => pwr(30, Object.assign({ 'sensor.bp': { state: '-1500', attributes: {} }, 'sensor.cch': { state: 'Stopped', attributes: {} }, 'sensor.ckw': { state: '0', attributes: {} },
    'binary_sensor.g': { state: 'off', attributes: {} }, 'binary_sensor.kf': { state: 'off', attributes: {} } }, o));
  const gap = { 'sensor.gap': { state: '1.2', attributes: {} }, 'binary_sensor.lp': { state: 'on', attributes: {} } }, car = { 'sensor.cch': { state: 'Charging', attributes: {} }, 'sensor.ckw': { state: '7.1', attributes: {} } };
  const w = world({ config: cfg, states: st() });
  on(w, 10, 19, 0);
  assert.strictEqual(band(w), '', 'no gap, no car: no band');
  w.update(st({ 'sensor.gap': { state: '0.8', attributes: {} } })); on(w, 10, 19, 0);
  assert.strictEqual(band(w), '', 'a gap not yet held for 7.5 minutes (a kettle) is not a band');
  w.update(st(gap)); on(w, 10, 19, 0);
  assert.strictEqual(band(w), 'Power is low.');
  assert.ok(/1\.2<\/span><span class="unit"[^>]*>kWh short/.test(w.html()));
  assert.strictEqual(line(w), '', 'the small line does not repeat the band');
  w.update(st(car)); on(w, 10, 13, 0);
  assert.strictEqual(band(w), 'The car is charging.');
  assert.ok(/1\.5<\/span><span class="unit"[^>]*>kW from battery/.test(w.html()));
  w.update(st({ ...car, 'sensor.bp': { state: '-900', attributes: {} } })); on(w, 10, 13, 0);
  assert.strictEqual(band(w), '', '1 kW or less from the battery is not a band');
  w.update(st(car)); on(w, 10, 0, 30);
  assert.strictEqual(band(w), '', 'not in the cheap rate');
  w.update(st({ ...car, ...gap, 'binary_sensor.kf': { state: 'on', attributes: {} } })); on(w, 10, 19, 0);
  assert.strictEqual(band(w), 'The car is charging.', 'car beats kiln and low power');
  w.update(st({ ...gap, 'binary_sensor.kf': { state: 'on', attributes: {} } })); on(w, 10, 19, 0);
  assert.strictEqual(band(w), 'The kiln is firing.', 'kiln beats low power');
  w.update(st({ ...car, 'binary_sensor.g': { state: 'on', attributes: {}, last_changed: new Date().toISOString() } })); on(w, 10, 13, 0);
  assert.strictEqual(band(w), 'The garage is open.', 'garage beats everything');
});

const CAR = { car_battery: 'sensor.cb', car_state: 'sensor.cst', car_charging: 'sensor.cch', car_plugged: 'binary_sensor.cpl', car_locked: 'binary_sensor.clk', car_link: 'binary_sensor.tm', car_power: 'sensor.ckw',
  car_alerts: [{ entity: 'binary_sensor.boot', name: 'Boot open' }, { entity: 'binary_sensor.sentry', name: 'Sentry', info: true }] };
const carStates = (o = {}) => ({ 'sensor.cb': { state: '40', attributes: {} }, 'sensor.cst': { state: 'asleep', attributes: {} }, 'sensor.cch': { state: 'Stopped', attributes: {} },
  'binary_sensor.cpl': { state: 'off', attributes: {} }, 'binary_sensor.clk': { state: 'on', attributes: {} }, 'binary_sensor.tm': { state: 'on', attributes: {} }, 'sensor.ckw': { state: '0', attributes: {} }, ...o });
const at = (w, h, m) => { const d = new Date(); d.setHours(h, m, 0, 0); w.setNow(d.getTime()); w.card._render(true); };

test('car: unplugged shows the live layout with the HA status; "Not plugged in" only in the evening', async () => {
  const w = world({ config: CAR, states: carStates() });
  at(w, 14, 0);
  assert.ok(w.html().includes('<span>Asleep</span>') && w.html().includes('<span>Locked</span>'));
  assert.ok(!w.html().includes('Not plugged in') && !w.html().includes('Tonight'));
  at(w, 21, 40);
  assert.ok(w.html().includes('Not<br>plugged in.'));
  w.update({ 'binary_sensor.clk': { state: 'off' }, 'binary_sensor.boot': { state: 'on' }, 'binary_sensor.sentry': { state: 'on' } });
  assert.ok(w.html().includes('class="amber">Unlocked'));
  assert.strictEqual((w.html().match(/class="chip( warn)?"/g) || []).length, 2, 'at most two chips');
  assert.ok(w.html().includes('chip warn">Unplugged'), 'with alerts, the evening note becomes the first chip');
});

test('car: plugged in with slots shows Tonight, Waiting for the next slot, and NOW once it starts', async () => {
  const w = world({ config: CAR, states: carStates({ 'binary_sensor.cpl': { state: 'on', attributes: {} } }) });
  const base = new Date(); base.setHours(21, 40, 0, 0);
  const iso = m => new Date(base.getTime() + m * 60000).toISOString();
  w.update({ 'binary_sensor.octopus_energy_x_intelligent_dispatching': { state: 'off', attributes: {
    planned_dispatches: [{ start: iso(180), end: iso(190) }, { start: iso(190), end: iso(200) }, { start: iso(260), end: iso(290) }] } },
    'number.octopus_energy_x_intelligent_charge_target': { state: '80', attributes: {} }, 'time.octopus_energy_x_intelligent_target_time': { state: '07:00:00', attributes: {} } });
  at(w, 21, 40);
  const h = w.html();
  assert.ok(h.includes('Tonight') && h.includes('Full 80% by 07:00') && h.includes('Waiting for<br>00:40.'));
  assert.ok(h.includes('00:40&#8211;01:00'), 'adjacent slots merge');
  assert.ok(h.includes('carbtn short'));
  w.setNow(base.getTime() + 185 * 60000);
  w.update({ 'sensor.cch': { state: 'Charging' }, 'sensor.ckw': { state: '7.2' } });
  assert.ok(w.html().includes('slot live') && w.html().includes('Charging &#183; 7.2&#160;kW'));
});

test('car: complete with no slots left goes back to the tall button; no link keeps climate working', async () => {
  const w = world({ config: CAR, states: carStates({ 'binary_sensor.cpl': { state: 'on', attributes: {} }, 'sensor.cch': { state: 'Complete', attributes: {} } }) });
  assert.ok(w.html().includes('Charge complete') && !w.html().includes('Tonight') && !w.html().includes('carbtn short'));
  w.update({ 'binary_sensor.tm': { state: 'off' } });
  assert.ok(w.html().includes('No link to TeslaMate') && w.html().includes('Last known.'));
  assert.ok(!w.html().includes('Charge complete') && !/data-act="car"[^>]*disabled/.test(w.html()));
});

test('clock: the date sits under the time', () => {
  const w = world({ config: PWR, states: pwr(95) });
  on(w, 10, 14, 0);
  assert.ok(w.html().includes('<div class="date">Thursday 15 October</div>'));
});

test('look: the theme entity switches Classic and Walala live; missing or unknown falls back to the theme key, then classic', () => {
  const w = world({ config: { theme_entity: 'input_select.kiosk_theme', theme: 'walala' }, states: { 'input_select.kiosk_theme': { state: 'Classic', attributes: {} } } });
  assert.ok(!w.html().includes('walala'), 'Classic from the entity beats theme: walala');
  w.update({ 'input_select.kiosk_theme': { state: 'Walala' } });
  assert.ok(w.html().includes('class="root walala'), 'Walala as soon as the entity says so');
  w.update({ 'input_select.kiosk_theme': { state: 'unavailable' } });
  assert.ok(w.html().includes('class="root walala'), 'an unavailable entity falls back to theme: walala');
  const d = world();
  assert.ok(!d.html().includes('walala'), 'no theme config at all is classic');
});

test('hero temperatures show one decimal; a frozen air con reading falls back to the room sensor', async () => {
  const at = h => new Date(2026, 9, 2, h, 32).toISOString();
  const w = world({
    states: {
      'climate.heat': { state: 'heat', attributes: { temperature: 21, current_temperature: 22.36, min_temp: 5, max_temp: 30 } },
      'climate.ac': { state: 'heat', last_updated: at(13), attributes: { temperature: 21, current_temperature: 23.5, hvac_modes: ['off', 'heat'], min_temp: 16, max_temp: 30 } },
      'sensor.room': { state: '23.0', attributes: { unit_of_measurement: '°C' } },
    },
    config: { aircon_fallback: 'sensor.room' },
  });
  assert.ok(w.html().includes('>22.4&#176;</span>'), 'heating hero at one decimal');
  assert.ok(w.html().includes('>23.5&#176;</span>'), 'a fresh air con reading is its own');
  w.update({ 'climate.ac': { last_updated: at(10), attributes: { current_temperature: 25.5 } } });
  assert.ok(w.html().includes('>23.0&#176;</span>') && !w.html().includes('25.5'), 'stale for 4 h: the room sensor');
  w.update({ 'sensor.room': { state: 'unavailable' } });
  assert.ok(w.html().includes('>25.5&#176;</span>'), 'no room number: keep the stale reading rather than a dash');
});

(async () => {
  let failed = 0;
  for (const [name, fn] of tests) {
    try {
      await fn();
      console.log(`ok   ${name}`);
    } catch (e) {
      failed++;
      console.log(`FAIL ${name}\n     ${e && e.stack ? e.stack.split('\n').slice(0, 3).join('\n     ') : e}`);
    }
  }
  await flush();
  if (rejections.length) {
    failed++;
    console.log(`FAIL unhandled rejections: ${rejections.map(String).join('; ')}`);
  }
  console.log(`${tests.length - failed}/${tests.length} passed`);
  process.exit(failed ? 1 : 0);
})();
