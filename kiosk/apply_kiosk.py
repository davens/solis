"""Deploy the home kiosk card: copy the JS to /config/www/kiosk over SSH, register it as a Lovelace resource
(cache-busted by content hash), create the `home-kiosk` dashboard if missing, and save a single panel view.
usage: uv run --no-project --with websockets python kiosk/apply_kiosk.py [--dry]
Card config comes from kiosk/card_config.local.json (gitignored; see card_config.example.json).
HA_URL / HA_TOKEN as in car_tile/apply_tile.py; the token is held in memory only."""
import asyncio, hashlib, json, os, subprocess, sys, websockets
from pathlib import Path
from urllib.parse import urlparse
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import site_env  # noqa: F401,E402 - loads the repo's .env into os.environ
HA = os.environ.get('HA_URL', 'http://homeassistant.local:8123').rstrip('/')
WS = HA.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1) + '/api/websocket'
SSH = ['ssh', '-p', '22222', '-o', 'BatchMode=yes', f'root@{urlparse(HA).hostname}']
DEST = '/config/www/kiosk/home-kiosk-card.js'
URL_PATH = 'home-kiosk'
def ha_token():
    if os.environ.get('HA_TOKEN'):
        return os.environ['HA_TOKEN']
    d = json.loads((Path.home() / '.claude.json').read_text())
    return d['projects'][str(HERE.parent)]['mcpServers']['home-assistant']['headers']['Authorization'].replace('Bearer ', '')
dry = '--dry' in sys.argv
js = (HERE / 'home-kiosk-card.js').read_bytes()
card = json.loads((HERE / 'card_config.local.json').read_text())
res_url = f'/local/kiosk/home-kiosk-card.js?v={hashlib.sha1(js).hexdigest()[:10]}'
N = [0]
async def call(ws, msg):
    N[0] += 1; msg['id'] = N[0]
    await ws.send(json.dumps(msg))
    while True:
        m = json.loads(await ws.recv())
        if m.get('id') == N[0]:
            if not m.get('success', True):
                raise SystemExit(f'{msg["type"]} failed: {m.get("error")}')
            return m.get('result')
async def main():
    if dry:
        print('dry run: would upload', len(js), 'bytes via', SSH[-1], 'and set resource', res_url); return
    async with websockets.connect(WS, max_size=None) as ws:
        await ws.recv(); await ws.send(json.dumps({'type': 'auth', 'access_token': ha_token()}))
        auth = json.loads(await ws.recv())
        if auth.get('type') != 'auth_ok':
            raise SystemExit(f'auth failed: {auth.get("message", auth.get("type"))}')
        # Upload beside the live file and rename over it, so HA never serves a half-written card.
        subprocess.run(SSH + [f'mkdir -p {os.path.dirname(DEST)} && cat > {DEST}.tmp && mv -f {DEST}.tmp {DEST}'], input=js, check=True)
        print('uploaded', len(js), 'bytes')
        mine = [r for r in await call(ws, {'type': 'lovelace/resources'}) if r['url'].startswith('/local/kiosk/home-kiosk-card.js')]
        for r in mine:
            await call(ws, {'type': 'lovelace/resources/update', 'resource_id': r['id'], 'res_type': 'module', 'url': res_url})
        if not mine:
            await call(ws, {'type': 'lovelace/resources/create', 'res_type': 'module', 'url': res_url})
        print('resource', res_url)
        if not any(d['url_path'] == URL_PATH for d in await call(ws, {'type': 'lovelace/dashboards/list'})):
            await call(ws, {'type': 'lovelace/dashboards/create', 'url_path': URL_PATH, 'title': 'Home kiosk', 'icon': 'mdi:tablet-dashboard', 'mode': 'storage', 'show_in_sidebar': True, 'require_admin': False})
            print('created dashboard', URL_PATH)
        # Kiosk Mode (HACS) hides HA's top bar and sidebar here; ?disable_km on the URL brings them back for one visit.
        cfg = {'kiosk_mode': {'hide_header': True, 'hide_sidebar': True},
               'views': [{'title': 'Home', 'path': 'home', 'type': 'panel', 'cards': [card]}]}
        await call(ws, {'type': 'lovelace/config/save', 'url_path': URL_PATH, 'config': cfg})
        back = await call(ws, {'type': 'lovelace/config', 'url_path': URL_PATH})
        ok = back['views'][0]['cards'][0] == card
        print('saved, verified', ok)
        if not ok:
            sys.exit(1)
asyncio.run(main())
