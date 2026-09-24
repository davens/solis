"""Swap the Overview Car tile's `w` template (and optional card-level keys) in the live dashboard.
Backs the whole dashboard up to backups/ first.
usage: uv run --with websockets python apply_tile.py options/<id>/template.js card_extra.json [--dry]
HA_URL (default http://homeassistant.local:8123) and HA_TOKEN come from the environment or the repo's .env; without
HA_TOKEN the token is read from the home-assistant MCP entry for this repo in ~/.claude.json. It is held in memory only."""
import asyncio, json, os, sys, time, websockets
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import site_env  # noqa: F401,E402 - loads the repo's .env into os.environ
HA = os.environ.get('HA_URL', 'http://homeassistant.local:8123').rstrip('/')
WS = HA.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1) + '/api/websocket'
def ha_token():
    if os.environ.get('HA_TOKEN'):
        return os.environ['HA_TOKEN']
    d = json.loads((Path.home() / '.claude.json').read_text())
    repo = str(Path(__file__).resolve().parent.parent)
    return d['projects'][repo]['mcpServers']['home-assistant']['headers']['Authorization'].replace('Bearer ', '')
tok = ha_token()
args = [a for a in sys.argv[1:] if not a.startswith('--')]
dry = '--dry' in sys.argv
tpl = Path(args[0]).read_text().strip()
extra = json.loads(Path(args[1]).read_text()) if len(args) > 1 else {}
N = [0]
async def call(ws, msg):
    N[0] += 1; msg['id'] = N[0]
    await ws.send(json.dumps(msg))
    while True:
        m = json.loads(await ws.recv())
        if m.get('id') == N[0]:
            return m
async def main():
    async with websockets.connect(WS, max_size=None) as ws:
        await ws.recv(); await ws.send(json.dumps({'type': 'auth', 'access_token': tok})); await ws.recv()
        cfg = (await call(ws, {'type': 'lovelace/config', 'url_path': None}))['result']
        stamp = time.strftime('%Y%m%d-%H%M%S')
        if not dry:
            bdir = Path(__file__).parent / 'backups'
            bdir.mkdir(exist_ok=True)
            (bdir / f'overview_{stamp}.json').write_text(json.dumps(cfg, indent=1))
        card = cfg['views'][0]['sections'][1]['cards'][3]
        assert card.get('type') == 'custom:button-card' and card.get('entity') == 'sensor.tesla_battery', 'car tile moved'
        card['custom_fields']['w'] = '[[[ ' + tpl + ' ]]]'
        for k, v in extra.items():
            if v is None:
                card.pop(k, None)
            else:
                card[k] = v
        print('backup', 'skipped (dry run)' if dry else f'backups/overview_{stamp}.json', '| extra keys', list(extra))
        if dry:
            print('dry run, not saved'); return
        r = await call(ws, {'type': 'lovelace/config/save', 'url_path': None, 'config': cfg})
        print('saved', r.get('success'), r.get('error'))
        back = (await call(ws, {'type': 'lovelace/config', 'url_path': None}))['result']['views'][0]['sections'][1]['cards'][3]
        print('verified', back['custom_fields']['w'] == card['custom_fields']['w'] and all(back.get(k) == v for k, v in extra.items() if v is not None))
asyncio.run(main())
