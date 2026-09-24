// Render a button-card `custom_fields.w` template body against scenario states.
// usage: node harness.js <template.js> <outdir>   -> <outdir>/<scenario>.html + preview.html
const fs = require('fs');
const path = require('path');
const [tplPath, outDir] = process.argv.slice(2);
const body = fs.readFileSync(tplPath, 'utf8');
const base = JSON.parse(fs.readFileSync(path.join(__dirname, 'states_base.json'), 'utf8'));
const scenarios = require('./scenarios.js');
fs.mkdirSync(outDir, { recursive: true });
const CARD = (inner, width) => `<div style="width:${width}px;box-sizing:border-box;background:#161616;border:1px solid rgba(255,255,255,0.06);border-radius:12px;padding:14px 16px 13px;font-family:Roboto,'Noto Sans',system-ui,sans-serif;color:#e1e1e1;">${inner}</div>`;
let preview = '';
let failed = 0;
for (const sc of scenarios.list) {
  const states = {};
  for (const s of base) { states[s.entity_id] = JSON.parse(JSON.stringify(s)); }
  sc.apply(states, scenarios.helpers);
  const entity = states['sensor.tesla_battery'];
  let html;
  try {
    const fn = new Function('states', 'entity', 'hass', 'variables', 'user', body);
    html = fn(states, entity, { states }, {}, { name: 'Owner' });
  } catch (e) {
    failed++;
    html = `<div style="color:#f44336;font:12px monospace">TEMPLATE THREW: ${String(e && e.stack || e).replace(/</g, '&lt;')}</div>`;
  }
  fs.writeFileSync(path.join(outDir, sc.id + '.html'), html);
  preview += `<h3 style="font:600 12px system-ui;color:#999;margin:18px 0 6px">${sc.title}</h3>` + CARD(html, 360) + '<div style="height:10px"></div>' + CARD(html, 470);
}
fs.writeFileSync(path.join(outDir, 'preview.html'), `<!doctype html><meta charset="utf-8"><body style="background:#0e0e0e;padding:16px">${preview}</body>`);
console.log(`${scenarios.list.length} scenarios -> ${outDir}` + (failed ? `  (${failed} THREW)` : ''));
process.exit(failed ? 1 : 0);
