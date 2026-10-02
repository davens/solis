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
    location: { hash: '' },
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
  return { card, hass, calls, pendingCalls, subs, ws, timers, advance, html, button, click, update, setNow: t => { now = t; } };
}

const tests = [];
const test = (name, fn) => tests.push([name, fn]);

test('null attributes: car asleep shows no 0 degrees and no "to 0"', async () => {
  const w = world({ states: { 'climate.car': { state: 'off', attributes: { temperature: null, current_temperature: null } } } });
  assert.ok(!w.html().includes('0&#176;'), 'no 0 degree reading');
  assert.ok(w.html().includes('Turn on<br>climate'));
  w.update({ 'climate.car': { state: 'heat_cool' } });
  assert.ok(w.html().includes('Climate on.'));
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
  w.card.shadowRoot.fire('pointerdown', { pointerId: 1 });
  w.update({ 'climate.heat': { attributes: { current_temperature: 23 } } });
  assert.ok(!w.html().includes('23&#176;'), 'held while pressed');
  w.card.shadowRoot.fire('pointerup', { pointerId: 1 });
  await w.advance(0);
  assert.ok(!w.html().includes('23&#176;'), 'not rebuilt before the click');
  w.card.shadowRoot.fire('click', { target: { closest: () => null } });
  assert.ok(w.html().includes('23&#176;'), 'rendered after the click');
  w.card.shadowRoot.fire('pointerdown', { pointerId: 2 });     // never released
  w.update({ 'climate.heat': { attributes: { current_temperature: 24 } } });
  await w.advance(5000);
  assert.ok(w.html().includes('24&#176;'), 'safety reset');
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
    },
    config: {
      heating_rooms: [{ entity: 'sensor.m', name: 'Bedroom' }, { entity: 'sensor.e', name: 'Nursery' }],
      aircon_extras: [{ entity: 'weather.home', name: 'Outside' }, { entity: 'sensor.rh', name: 'Humidity' }],
    },
    history: {
      // Only the first entry carries attributes when they do not change; later ones inherit them.
      'climate.heat': [{ s: 'heat', a: { hvac_action: 'idle' }, lu: at(0, 0) }, { s: 'heat', a: { hvac_action: 'heating' }, lu: at(6, 0) },
        { s: 'heat', a: { hvac_action: 'idle' }, lu: at(7, 30) }, { s: 'heat', a: { hvac_action: 'heating' }, lu: at(12, 30) },
        { s: 'heat', a: { hvac_action: 'idle' }, lu: at(13, 0) }],
      'climate.ac': [{ s: 'off', a: {}, lu: at(0, 0) }, { s: 'cool', lu: at(13, 47) }],
    },
  });
  await w.advance(0);
  w.card._render(true);
  const h = w.html();
  assert.ok(h.includes('Bedroom 21&#176;') && !h.includes('Nursery'), 'rooms line skips an unavailable sensor');
  assert.ok(h.includes('Outside 18&#176; &#183; Humidity 55%'), 'extras read weather temperature and % sensors');
  assert.ok(h.includes('Ran 2&#160;h today'), 'heating counts hvac_action heating only');
  assert.ok(h.includes('On 45&#160;min today'), 'air con counts any mode but off, up to now');
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
