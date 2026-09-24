// Screenshot an HA dashboard path in headless Chrome, authenticated with the long-lived token held
// in memory only: off-the-record browser context, token injected before the app boots, profile deleted.
// usage: node hashot.js <path> <width> <height> <waitSec> <out.png>
// HA_URL (default http://homeassistant.local:8123) and HA_TOKEN come from the environment; without HA_TOKEN the
// token is read from the home-assistant MCP entry for this repo in ~/.claude.json.
const fs = require('fs'), os = require('os'), path = require('path'), { spawn } = require('child_process');
const [p, W, H, waitS, out] = process.argv.slice(2);
const HA = (process.env.HA_URL || 'http://homeassistant.local:8123').replace(/\/+$/, '');
const tok = process.env.HA_TOKEN || JSON.parse(fs.readFileSync(path.join(os.homedir(), '.claude.json'), 'utf8'))
  .projects[fs.realpathSync(path.join(__dirname, '..'))].mcpServers['home-assistant'].headers.Authorization.replace('Bearer ', '');
const prof = fs.mkdtempSync(path.join(__dirname, 'chrome-prof-'));
const chrome = spawn('/Applications/Google Chrome.app/Contents/MacOS/Google Chrome',
  ['--headless=new', '--disable-gpu', '--hide-scrollbars', '--remote-debugging-port=9333', `--user-data-dir=${prof}`, 'about:blank'], { stdio: 'ignore' });
const sleep = ms => new Promise(r => setTimeout(r, ms));
(async () => {
  let ver;
  for (let i = 0; i < 50 && !ver; i++) { try { ver = await (await fetch('http://127.0.0.1:9333/json/version')).json(); } catch { await sleep(200); } }
  const ws = new WebSocket(ver.webSocketDebuggerUrl);
  await new Promise(r => ws.onopen = r);
  let id = 0; const pend = {};
  ws.onmessage = e => { const m = JSON.parse(e.data); if (m.id && pend[m.id]) { pend[m.id](m); delete pend[m.id]; } };
  const send = (method, params = {}, sessionId) => new Promise(r => { const i = ++id; pend[i] = r; ws.send(JSON.stringify({ id: i, method, params, sessionId })); });
  const { result: { browserContextId } } = await send('Target.createBrowserContext');
  const { result: { targetId } } = await send('Target.createTarget', { url: 'about:blank', browserContextId });
  const { result: { sessionId } } = await send('Target.attachToTarget', { targetId, flatten: true });
  const S = (m, prm) => send(m, prm, sessionId);
  await S('Page.enable');
  await S('Emulation.setDeviceMetricsOverride', { width: +W, height: +H, deviceScaleFactor: 2, mobile: +W < 600 });
  const tokens = { access_token: tok, token_type: 'Bearer', expires_in: 1e9, hassUrl: HA, clientId: HA + '/', expires: Date.now() + 1e11, refresh_token: '' };
  await S('Page.addScriptToEvaluateOnNewDocument', { source: `try{localStorage.setItem('hassTokens', ${JSON.stringify(JSON.stringify(tokens))});localStorage.setItem('selectedTheme','{"dark":true}');}catch(e){}` });
  await S('Page.navigate', { url: HA + p });
  await sleep(+waitS * 1000);
  const shot = await S('Page.captureScreenshot', { format: 'png', captureBeyondViewport: false });
  fs.writeFileSync(out, Buffer.from(shot.result.data, 'base64'));
  const err = await S('Runtime.evaluate', { expression: 'document.title', returnByValue: true });
  console.log('title:', err.result && err.result.result && err.result.result.value, '->', out);
  ws.close(); chrome.kill();
  await sleep(500); fs.rmSync(prof, { recursive: true, force: true });
})().catch(e => { console.error('FAILED', e.message); chrome.kill(); fs.rmSync(prof, { recursive: true, force: true }); process.exit(1); });
