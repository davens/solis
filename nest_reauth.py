"""Refresh the Nest Legacy integration's Google sign-in from a dedicated Firefox profile.

usage:
  uv run --no-project --with websockets python nest_reauth.py login   # sign in once, then push
  uv run --no-project --with websockets python nest_reauth.py         # push the profile's current cookies
  ... [--profile DIR] to read another Firefox profile instead of the dedicated one

`login` opens Firefox on home.nest.com with its own profile; sign in with Google, see your home, then quit
that Firefox window (Cmd-Q). Do not sign out: that kills the session the cookies belong to.
The issue_token is reused from the live config entry, so nothing is stored here. The cookies are read
from the profile and go only to HA; neither value is printed or written to disk outside Firefox's own files.
HA_URL / HA_TOKEN as in car_tile/apply_tile.py; the token is held in memory only."""
import asyncio, json, os, shutil, sqlite3, subprocess, sys, tempfile, time, urllib.error, urllib.request, websockets
from pathlib import Path
from urllib.parse import urlparse
HERE = Path(__file__).resolve().parent
import site_env  # noqa: F401,E402 - loads the repo's .env into os.environ
HA = os.environ.get('HA_URL', 'http://homeassistant.local:8123').rstrip('/')
WS = HA.replace('https://', 'wss://', 1).replace('http://', 'ws://', 1) + '/api/websocket'
SSH = ['ssh', '-p', '22222', '-o', 'BatchMode=yes', f'root@{urlparse(HA).hostname}']
FIREFOX = '/Applications/Firefox.app/Contents/MacOS/firefox'
PROFILE = Path.home() / 'Library/Application Support/nest-reauth-firefox'
DOMAIN = 'nest_legacy'
# The request HA replays is https://accounts.google.com/o/oauth2/iframerpc, so send what a browser would send there.
TARGET_HOST, TARGET_PATH = 'accounts.google.com', '/o/oauth2/iframerpc'
SESSION_COOKIES = ('SID', '__Secure-1PSID', '__Secure-3PSID')


def ha_token():
    if os.environ.get('HA_TOKEN'):
        return os.environ['HA_TOKEN']
    d = json.loads((Path.home() / '.claude.json').read_text())
    return d['projects'][str(HERE)]['mcpServers']['home-assistant']['headers']['Authorization'].replace('Bearer ', '')


def login(profile):
    profile.mkdir(parents=True, exist_ok=True)
    print('Firefox is opening on its own profile. Sign in to Nest with Google, wait until you see your home, then quit it with Cmd-Q.')
    subprocess.run([FIREFOX, '-profile', str(profile), '-no-remote', 'https://home.nest.com'], check=False,
                   stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def domain_matches(host):
    host = host.lstrip('.')
    return TARGET_HOST == host or TARGET_HOST.endswith('.' + host)


def path_matches(path):
    return TARGET_PATH == path or TARGET_PATH.startswith(path.rstrip('/') + '/') or path == '/'


def read_cookies(profile):
    db = profile / 'cookies.sqlite'
    if not db.exists():
        raise SystemExit(f'no cookies in {profile}; run `nest_reauth.py login` first')
    # Firefox holds the database open and keeps recent writes in the -wal file, so read a copy of both.
    with tempfile.TemporaryDirectory() as tmp:
        for suffix in ('', '-wal'):
            src = profile / f'cookies.sqlite{suffix}'
            if src.exists():
                shutil.copy2(src, Path(tmp) / src.name)
        con = sqlite3.connect(Path(tmp) / 'cookies.sqlite')
        rows = con.execute('SELECT name, value, host, path, expiry, originAttributes FROM moz_cookies').fetchall()
        con.close()
    now = time.time()
    picked = []
    for name, value, host, path, expiry, attrs in rows:
        # Newer Firefox stores expiry in milliseconds. Partitioned (third-party iframe) cookies are not what a direct request sends.
        exp = expiry / 1000 if expiry > 1e11 else expiry
        if attrs == '' and domain_matches(host) and path_matches(path) and exp > now:
            picked.append((len(path), len(host), name, value))
    # Most specific first, and one value per name, as a browser orders its Cookie header.
    picked.sort(reverse=True)
    seen, pairs = set(), []
    for _, _, name, value in picked:
        if name not in seen:
            seen.add(name)
            pairs.append(f'{name}={value}')
    if seen.isdisjoint(SESSION_COOKIES):
        raise SystemExit('the profile has no live Google session; run `nest_reauth.py login` and sign in')
    return '; '.join(pairs), len(pairs)


def rest(method, path, body=None):
    req = urllib.request.Request(HA + path, method=method, data=None if body is None else json.dumps(body).encode(),
                                 headers={'Authorization': f'Bearer {ha_token()}', 'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read() or 'null')
    except urllib.error.HTTPError as e:
        raise SystemExit(f'{method} {path} failed: HTTP {e.code} {e.read()[:300].decode(errors="replace")}')


async def ws_call(msg):
    async with websockets.connect(WS, max_size=None) as ws:
        await ws.recv()
        await ws.send(json.dumps({'type': 'auth', 'access_token': ha_token()}))
        auth = json.loads(await ws.recv())
        if auth.get('type') != 'auth_ok':
            raise SystemExit(f'auth failed: {auth.get("message", auth.get("type"))}')
        await ws.send(json.dumps({'id': 1, **msg}))
        while True:
            m = json.loads(await ws.recv())
            if m.get('id') == 1:
                if not m.get('success'):
                    raise SystemExit(f'{msg["type"]} failed: {m.get("error")}')
                return m['result']


def stored_issue_token(entry_id):
    # Config-entry data is not exposed over the API, so read it from HA's storage over SSH; it stays in memory.
    raw = subprocess.run(SSH + ['cat /config/.storage/core.config_entries'], capture_output=True, check=True).stdout
    for e in json.loads(raw)['data']['entries']:
        if e['entry_id'] == entry_id:
            return e['data']['issue_token']
    raise SystemExit('entry vanished from core.config_entries')


def push(cookies):
    entries = rest('GET', f'/api/config/config_entries/entry?domain={DOMAIN}')
    if len(entries) != 1:
        raise SystemExit(f'expected one {DOMAIN} entry, found {len(entries)}')
    entry = entries[0]
    issue_token = stored_issue_token(entry['entry_id'])
    flows = asyncio.run(ws_call({'type': 'config_entries/flow/progress'}))
    reauth = [f for f in flows if f['handler'] == DOMAIN and f['context'].get('entry_id') == entry['entry_id']]
    if reauth:
        flow = rest('GET', f'/api/config/config_entries/flow/{reauth[0]["flow_id"]}')
        print('continuing the pending', reauth[0]['context'].get('source'), 'flow')
    else:
        flow = rest('POST', '/api/config/config_entries/flow', {'handler': DOMAIN, 'entry_id': entry['entry_id']})
        print('started a reconfigure flow')
    sent_creds = False
    while flow['type'] == 'form':
        if flow.get('errors'):
            raise SystemExit(f'HA rejected the credentials: {flow["errors"]} (run `nest_reauth.py login` if the session has expired)')
        if flow['step_id'] == 'user':
            body = {'account_type': 'google', 'field_test': False}
        elif flow['step_id'] == 'google_account' and not sent_creds:
            body, sent_creds = {'issue_token': issue_token, 'cookies': cookies}, True
        else:
            raise SystemExit(f'unexpected step {flow["step_id"]}')
        flow = rest('POST', f'/api/config/config_entries/flow/{flow["flow_id"]}', body)
    if flow['type'] != 'abort' or not flow['reason'].endswith('_successful'):
        raise SystemExit(f'flow ended with {flow["type"]}: {flow.get("reason")}')
    print('HA accepted the credentials:', flow['reason'])
    for _ in range(30):
        time.sleep(2)
        state = rest('GET', f'/api/config/config_entries/entry?domain={DOMAIN}')[0]['state']
        if state not in ('setup_in_progress', 'not_loaded', 'setup_retry'):
            break
    print('entry state:', state)
    if state != 'loaded':
        sys.exit(1)


def main():
    args = sys.argv[1:]
    profile = PROFILE
    if '--profile' in args:
        profile = Path(args[args.index('--profile') + 1]).expanduser()
    if 'login' in args:
        login(profile)
    cookies, n = read_cookies(profile)
    print(f'read {n} Google cookies from the profile')
    push(cookies)


if __name__ == '__main__':
    main()
