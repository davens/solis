const S = id => (states[id] || {}).state;
const OCTO = 'octopus_energy_00000000_0000_0000_0000_000000000000_';
const g = id => { for (const d of ['sensor','number','time','select','binary_sensor']) { const s = states[d + '.' + id]; if (s && s.state !== 'unavailable' && s.state !== 'unknown') { return s; } } return null; };
const st = S('sensor.tesla_state') || 'unknown';
const cs = S('sensor.tesla_charging_state');
const kw = Number(S('sensor.tesla_charger_power'));
const chg = cs === 'Charging' || (isFinite(kw) && kw > 0);
const plugged = S('binary_sensor.tesla_plugged_in') === 'on';
const lockedS = S('binary_sensor.tesla_locked');
const socN = Number(S('sensor.tesla_battery'));
const ok = isFinite(socN);
const resting = ['offline','asleep','suspended'].includes(st);
const stale = S('binary_sensor.teslamate_healthy') === 'off' || ['unknown','unavailable'].includes(st) || !ok;
const soc = ok ? Math.max(0, Math.min(100, socN)) : 0;
const km = Number(S('sensor.tesla_range'));
const mi = isFinite(km) ? Math.round(km * 0.621371) : null;
const limS = g(OCTO + 'intelligent_charge_target') || states['sensor.tesla_charge_limit'];
const lim = limS ? Number(limS.state) : NaN;
const byS = g(OCTO + 'intelligent_target_time');
const by = byS ? String(byS.state).slice(0, 5) : null;
const disp = g(OCTO + 'intelligent_dispatching');
const da = (disp && disp.attributes) || {};
const fmt = t => { const d = new Date(t); return isNaN(d) ? null : d.toTimeString().slice(0, 5); };
const plans = Array.isArray(da.planned_dispatches) ? da.planned_dispatches : [];
const now = Date.now();
const raw = plans.filter(p => p && p.start && p.end && !isNaN(new Date(p.start)) && !isNaN(new Date(p.end)) && new Date(p.end).getTime() > now).sort((a, b) => new Date(a.start) - new Date(b.start));
const runs = [];
for (const p of raw) { const last = runs[runs.length - 1]; if (last && new Date(p.start).getTime() - new Date(last.end).getTime() <= 60000) { if (new Date(p.end).getTime() > new Date(last.end).getTime()) { last.end = p.end; } } else { runs.push({ start: p.start, end: p.end }); } }
const future = runs.slice(0, 2);
if (!future.length && da.next_start && da.next_end && !isNaN(new Date(da.next_start)) && new Date(da.next_end).getTime() > now) { future.push({ start: da.next_start, end: da.next_end }); }
const GREEN = 'rgb(76,175,80)';
const RED = 'rgb(244,67,54)';
const dim = 'rgba(255,255,255,0.45)';
const barCol = ok && soc <= 20 ? RED : GREEN;
const asleep = resting;
let l1 = ''; let l1c = dim; let l2 = '';
const ttf = Number(S('sensor.tesla_time_to_full_charge'));
let left = '';
if (isFinite(ttf) && ttf > 0.02) { if (ttf < 1) { left = Math.round(ttf * 60) + ' min remaining'; } else { left = Math.floor(ttf) + ' h ' + Math.round((ttf % 1) * 60) + ' min remaining'; } }
const add = Number(S('sensor.tesla_charge_energy_added'));
if (stale) { l1 = 'No link to TeslaMate'; l1c = 'rgba(244,67,54,0.85)'; }
else if (chg) { l1 = 'Charging' + (isFinite(kw) && kw > 0 ? ' · ' + kw.toFixed(1) + ' kW' : '') + (left ? ' · ' + left : ''); l1c = GREEN; if (isFinite(add) && isFinite(lim) && by) { l2 = '+' + add.toFixed(1) + ' kWh · → ' + Math.round(lim) + '% by ' + by; } }
else if (plugged && cs === 'Complete') { l1 = 'Charge complete'; if (isFinite(add) && add > 0) { l2 = '+' + add.toFixed(1) + ' kWh added'; } }
else if (plugged) { l1 = 'Plugged in' + (asleep ? ' · Asleep' : ' · Parked'); if (isFinite(lim) && by) { l2 = '→ ' + Math.round(lim) + '% by ' + by; } }
else if (st === 'driving') { l1 = 'Driving'; }
else if (asleep) { l1 = 'Asleep'; }
else { l1 = 'Parked'; }
const AMB = 'rgba(255,193,7,0.92)';
const BLU = 'rgba(66,165,245,0.92)';
const CYN = 'rgba(38,198,218,0.92)';
const dimCol = 'rgba(255,255,255,0.50)';
const trk = states['device_tracker.tesla_location'];
const trkS = trk && trk.state && !['unknown','unavailable',''].includes(String(trk.state)) ? String(trk.state) : null;
const atHome = trkS === 'home';
const zh = (states['zone.home'] || {}).attributes || {};
const ta = (trk && trk.attributes) || {};
const hLat = Number(zh.latitude); const hLon = Number(zh.longitude); const cLat = Number(ta.latitude); const cLon = Number(ta.longitude);
let distMi = null; let brg = '';
if ([hLat, hLon, cLat, cLon].every(isFinite)) { const r = Math.PI / 180; const dLat = (cLat - hLat) * r; const dLon = (cLon - hLon) * r; const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) + Math.cos(hLat * r) * Math.cos(cLat * r) * Math.sin(dLon / 2) * Math.sin(dLon / 2); distMi = 2 * 6371 * Math.asin(Math.sqrt(a)) * 0.621371; const y = Math.sin(dLon) * Math.cos(cLat * r); const x = Math.cos(hLat * r) * Math.sin(cLat * r) - Math.sin(hLat * r) * Math.cos(cLat * r) * Math.cos(dLon); const b = (Math.atan2(y, x) * 180 / Math.PI + 360) % 360; brg = ['N','NE','E','SE','S','SW','W','NW'][Math.round(b / 45) % 8]; }
const distTxt = distMi === null ? '' : (distMi < 10 ? distMi.toFixed(1) : String(Math.round(distMi))) + ' mi' + (brg ? ' ' + brg : '');
const zoneName = trkS && !atHome && trkS !== 'not_home' ? trkS : '';
const placeTxt = trkS && !atHome ? (zoneName ? zoneName + (distTxt ? ' · ' + distTxt : '') : (distTxt || 'Away')) : '';
const hdg = Number(S('sensor.tesla_heading'));
const gd = states['binary_sensor.garage_door'];
const gKnown = !!gd && (gd.state === 'on' || gd.state === 'off');
const gOpen = gKnown && gd.state === 'on';
const gMs = gOpen ? now - new Date(gd.last_changed).getTime() : NaN;
const durTxt = ms => { if (!isFinite(ms) || ms < 0) { return ''; } const m = Math.round(ms / 60000); if (m < 1) { return 'just now'; } if (m < 60) { return m + ' min'; } const h = Math.floor(m / 60); if (h < 24) { return h + ' h' + (m % 60 ? ' ' + (m % 60) + ' min' : ''); } return Math.floor(h / 24) + ' d'; };
const gTxt = gOpen ? 'Garage open' + (durTxt(gMs) ? ' ' + durTxt(gMs) : '') : '';
const gGlyph = (c, open) => `<svg width='11' height='11' viewBox='0 0 12 12' style='flex:none;display:block;'><path d='M1.5 11.3 V5.2 L6 1.6 L10.5 5.2 V11.3' fill='none' stroke='${c}' stroke-width='1.4' stroke-linejoin='round' stroke-linecap='round'/><path d='M3.6 11.3 V6.9 H8.4 V11.3' fill='none' stroke='${c}' stroke-width='1.3' stroke-linejoin='round'/>${open ? '' : `<path d='M3.6 8.5 H8.4 M3.6 10 H8.4' stroke='${c}' stroke-width='1.1' stroke-linecap='round'/>`}</svg>`;
const lockTxt = lockedS === 'on' ? 'Locked' : (lockedS === 'off' ? 'Unlocked' : '');
const lockCol = lockedS === 'off' ? 'rgba(255,193,7,0.85)' : 'rgba(255,255,255,0.40)';
const lockGlyph = lockTxt ? `<svg width='9' height='11' viewBox='0 0 10 12' style='margin-right:4px;'><rect x='1' y='5.5' width='8' height='6' rx='1.5' fill='${lockCol}'/><path d='M 3 5.5 V 3.4 A 2 2 0 0 1 7 3.4 ${lockedS === 'off' ? '' : 'V 5.5'}' fill='none' stroke='${lockCol}' stroke-width='1.4'/></svg>` : '';
let garageHdr = '';
if (gOpen) { garageHdr = `<span style='display:flex;align-items:center;gap:4px;color:${AMB};white-space:nowrap;'>${gGlyph(AMB, true)}<span>${gTxt}</span></span>${lockTxt ? `<span style='color:rgba(255,255,255,0.22);margin:0 7px;'>·</span>` : ''}`; }
else if (gKnown) { garageHdr = `<span style='display:flex;align-items:center;margin-right:${lockTxt ? 9 : 0}px;' title='Garage closed'>${gGlyph('rgba(255,255,255,0.22)', false)}</span>`; }
const heroCol = stale ? 'rgba(255,255,255,0.55)' : 'rgba(255,255,255,0.95)';
const carCol = chg ? GREEN : 'rgba(255,255,255,0.75)';
const carGlyph = `<svg width='19' height='10' viewBox='0 0 26 13' style='flex:none;display:block;'><path d='M1.5 9 C1.5 7 2.5 6.2 4.5 5.8 C6.5 5.4 8 5.2 9.5 5.1 C10.5 3.3 12 2.3 14 2.2 C16 2.2 18 3 20.5 4.6 C22.5 5 24 5.8 24.5 7 C24.8 7.8 24.8 8.5 24.6 9' fill='none' stroke='${carCol}' stroke-width='1.6' stroke-linecap='round'/><circle cx='6.5' cy='10.5' r='1.9' fill='none' stroke='${carCol}' stroke-width='1.4'/><circle cx='19.5' cy='10.5' r='1.9' fill='none' stroke='${carCol}' stroke-width='1.4'/></svg>`;
const boltG = chg ? `<svg width='7' height='10' viewBox='0 0 8 12' style='flex:none;'><path d='M4.5 0.5 L1 6.5 H3.5 L2.5 11.5 L7 5 H4 L5.5 0.5 Z' fill='${GREEN}'/></svg>` : '';
const rowH = (glyph, txt, col) => `<div style='display:flex;align-items:center;justify-content:flex-end;gap:5px;font-size:11.5px;line-height:1.3;color:${col};white-space:nowrap;font-variant-numeric:tabular-nums;'>${glyph}<span>${txt}</span></div>`;
const pin = c => `<svg width='10' height='11' viewBox='0 0 12 14' style='flex:none;'><path d='M6 1 C8.8 1 11 3.2 11 6 C11 9.5 6 13 6 13 C6 13 1 9.5 1 6 C1 3.2 3.2 1 6 1 Z' fill='none' stroke='${c}' stroke-width='1.5'/><circle cx='6' cy='6' r='1.6' fill='${c}'/></svg>`;
const nav = c => `<svg width='10' height='10' viewBox='0 0 12 12' style='flex:none;${isFinite(hdg) ? `transform:rotate(${Math.round(hdg)}deg);` : ''}'><path d='M6 1.2 L10.2 10.6 L6 8.4 L1.8 10.6 Z' fill='none' stroke='${c}' stroke-width='1.4' stroke-linejoin='round'/></svg>`;
const thermo = c => `<svg width='9' height='12' viewBox='0 0 10 14' style='flex:none;'><path d='M3.5 8.2 V2.5 A1.5 1.5 0 0 1 6.5 2.5 V8.2 A3.2 3.2 0 1 1 3.5 8.2 Z' fill='none' stroke='${c}' stroke-width='1.4'/><circle cx='5' cy='10.6' r='1.4' fill='${c}'/></svg>`;
const clockG = c => `<svg width='10' height='10' viewBox='0 0 12 12' style='flex:none;'><circle cx='6' cy='6' r='5' fill='none' stroke='${c}' stroke-width='1.4'/><path d='M6 3.5 V6 L8 7.5' fill='none' stroke='${c}' stroke-width='1.4' stroke-linecap='round'/></svg>`;
let rows = '';
if (placeTxt) { rows += rowH(st === 'driving' ? nav(dimCol) : pin(dimCol), placeTxt, dimCol); }
else if (!trkS) { const geo = S('sensor.tesla_geofence'); if (geo && !['unknown','unavailable','none',''].includes(String(geo).trim().toLowerCase())) { rows += rowH(pin(dimCol), geo, dimCol); } }
const ti = Number(S('sensor.tesla_inside_temp'));
if (isFinite(ti)) { rows += rowH(thermo(dimCol), ti.toFixed(1) + '°', dimCol); }
if (plugged) { if (future.length) { const live = new Date(future[0].start).getTime() <= now; const scol = live ? 'rgba(76,175,80,0.95)' : dimCol; rows += rowH(clockG(scol), future.map(p => fmt(p.start) + '–' + fmt(p.end)).join(' · '), scol); } else { rows += rowH(clockG(dimCol), 'no slots', dimCol); } }
const info = `<div style='display:flex;flex-direction:column;gap:4px;opacity:${stale ? 0.45 : 1};'>${rows}</div>`;
const tint = c => c.replace('0.92', '0.13');
const chipH = (glyph, txt, col) => `<span style='display:inline-flex;align-items:center;gap:5px;padding:3px 8px;border-radius:6px;background:${tint(col)};color:${col};font-size:11px;line-height:1.25;white-space:nowrap;'>${glyph}<span>${txt}</span></span>`;
const gDoor = c => `<svg width='10' height='11' viewBox='0 0 12 13' style='flex:none;'><path d='M1.2 12 V3.2 L8.2 1 V12' fill='none' stroke='${c}' stroke-width='1.4' stroke-linejoin='round'/><circle cx='6.6' cy='7' r='0.9' fill='${c}'/><path d='M9.8 12 H11.2' stroke='${c}' stroke-width='1.4' stroke-linecap='round'/></svg>`;
const gEye = c => `<svg width='12' height='10' viewBox='0 0 14 11' style='flex:none;'><path d='M1 5.5 C3.2 2 10.8 2 13 5.5 C10.8 9 3.2 9 1 5.5 Z' fill='none' stroke='${c}' stroke-width='1.3'/><circle cx='7' cy='5.5' r='1.7' fill='${c}'/></svg>`;
const gWind = c => `<svg width='12' height='10' viewBox='0 0 13 11' style='flex:none;'><path d='M1 3 H7.4 A1.7 1.7 0 1 0 5.7 1.3' fill='none' stroke='${c}' stroke-width='1.3' stroke-linecap='round'/><path d='M1 6 H9.4 A1.7 1.7 0 1 1 7.7 7.7' fill='none' stroke='${c}' stroke-width='1.3' stroke-linecap='round'/><path d='M1 9 H5' fill='none' stroke='${c}' stroke-width='1.3' stroke-linecap='round'/></svg>`;
const gDown = c => `<svg width='10' height='11' viewBox='0 0 12 13' style='flex:none;'><path d='M6 1 V8.4' fill='none' stroke='${c}' stroke-width='1.4' stroke-linecap='round'/><path d='M3.2 5.8 L6 8.8 L8.8 5.8' fill='none' stroke='${c}' stroke-width='1.4' stroke-linecap='round' stroke-linejoin='round'/><path d='M1.6 11.4 H10.4' fill='none' stroke='${c}' stroke-width='1.4' stroke-linecap='round'/></svg>`;
const gTyre = c => `<svg width='11' height='11' viewBox='0 0 12 12' style='flex:none;'><circle cx='6' cy='6' r='5' fill='none' stroke='${c}' stroke-width='1.4'/><circle cx='6' cy='6' r='1.8' fill='none' stroke='${c}' stroke-width='1.3'/></svg>`;
const chips = [];
const openings = [['binary_sensor.tesla_doors_open', 'doors'], ['binary_sensor.tesla_windows_open', 'windows'], ['binary_sensor.tesla_trunk_open', 'boot'], ['binary_sensor.tesla_frunk_open', 'frunk']].filter(o => S(o[0]) === 'on').map(o => o[1]);
if (openings.length) { const t = openings.join(' · ') + ' open'; chips.push(chipH(gDoor(AMB), t.charAt(0).toUpperCase() + t.slice(1), AMB)); }
const tp = [['fl', 'FL'], ['fr', 'FR'], ['rl', 'RL'], ['rr', 'RR']].filter(w => S('binary_sensor.tesla_tpms_warn_' + w[0]) === 'on').map(w => { const b = Number(S('sensor.tesla_tpms_' + w[0])); return w[1] + (isFinite(b) ? ' ' + b.toFixed(1) : ''); });
if (tp.length) { chips.push(chipH(gTyre(AMB), tp.join(' · ') + ' bar', AMB)); }
if (S('binary_sensor.tesla_climate_on') === 'on' || S('binary_sensor.tesla_preconditioning') === 'on') { chips.push(chipH(gWind(CYN), S('binary_sensor.tesla_preconditioning') === 'on' ? 'Preconditioning' : 'Climate on', CYN)); }
if (S('binary_sensor.tesla_sentry') === 'on') { chips.push(chipH(gEye(BLU), 'Sentry', BLU)); }
if (S('binary_sensor.tesla_update_available') === 'on') { const ip = Number(S('sensor.tesla_install_perc')); chips.push(chipH(gDown(BLU), isFinite(ip) && ip > 0 && ip < 100 ? 'Installing ' + Math.round(ip) + '%' : 'Update ready', BLU)); }
const alerts = chips.length ? `<div style='display:flex;flex-wrap:wrap;gap:6px;margin-top:-1px;opacity:${stale ? 0.45 : 1};'>${chips.join('')}</div>` : '';
const notch = isFinite(lim) && lim > 0 && lim < 100 ? `<div style='position:absolute;top:-2px;height:8px;width:2px;border-radius:1px;left:calc(${Math.round(lim)}% - 1px);background:rgba(255,255,255,0.55);'></div>` : '';
return `<div style='display:flex;flex-direction:column;gap:9px;text-align:left;'><div style='display:flex;justify-content:space-between;align-items:center;'><span style='display:flex;align-items:center;gap:6px;'>${carGlyph}${boltG}<span style='font-size:10.5px;letter-spacing:0.10em;text-transform:uppercase;color:rgba(255,255,255,0.40);'>Tesla</span></span><span style='display:flex;align-items:center;font-size:11px;color:${lockCol};'>${garageHdr}${lockGlyph}${lockTxt}</span></div><div style='display:flex;align-items:center;justify-content:space-between;gap:10px;'><div style='flex:none;'><div style='font-size:36px;font-weight:600;line-height:1;color:${heroCol};font-variant-numeric:tabular-nums;'>${ok ? Math.round(socN) : '—'}<span style='font-size:17px;font-weight:400;color:rgba(255,255,255,0.45);'>%</span></div><div style='font-size:12px;color:${dim};margin-top:5px;'>${mi !== null ? mi + ' mi' : ''}</div></div>${info}</div><div style='position:relative;height:4px;border-radius:2px;background:rgba(255,255,255,0.14);'><div style='position:absolute;left:0;top:0;bottom:0;width:${soc}%;border-radius:2px;background:${barCol};opacity:${stale ? 0.5 : 1};'></div>${notch}</div><div style='min-height:15px;display:flex;flex-wrap:wrap;align-items:flex-end;justify-content:space-between;column-gap:12px;row-gap:3px;'><div style='flex:0 1 auto;min-width:0;overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:12px;line-height:1.3;color:${l1c};'>${l1}</div>${l2 ? `<div style='flex:none;white-space:nowrap;font-size:11.5px;line-height:1.3;color:rgba(255,255,255,0.40);'>${l2}</div>` : ''}</div></div>${alerts}</div>`;
