"""Patch the Nest Legacy integration on HA to rotate its Google cookies every 10 minutes, as an open browser tab does.

usage:
  uv run --no-project python nest_rotate_patch.py --check   # exit 0 if patched, 1 if not (e.g. after a HACS update)
  uv run --no-project python nest_rotate_patch.py           # apply, keeping .orig backups; then restart HA

HACS overwrites the patch silently on every update of nest_legacy. Each edit is an exact-anchor string
replacement, so an upstream change that moves an anchor makes this fail loudly instead of half-applying."""
import os, subprocess, sys
from urllib.parse import urlparse
import site_env  # noqa: F401 - loads the repo's .env into os.environ
HA = os.environ.get('HA_URL', 'http://homeassistant.local:8123').rstrip('/')
SSH = ['ssh', '-p', '22222', '-o', 'BatchMode=yes', f'root@{urlparse(HA).hostname}']
ROOT = '/config/custom_components/nest_legacy'
MARKER = '_GOOGLE_ROTATE_COOKIES_URL'

CLIENT_EDITS = [
    ('''_NEST_AUTH_URL_JWT = "https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt"
''', '''_NEST_AUTH_URL_JWT = "https://nestauthproxyservice-pa.googleapis.com/v1/issue_jwt"
# A signed-in browser tab posts here every ~10 minutes to refresh __Secure-1PSIDTS
# and friends; the issueToken request alone never rotates those.
_GOOGLE_ROTATE_COOKIES_URL = "https://accounts.google.com/RotateCookies"
'''),
    ('''    @property
    def google_cookies(self) -> str | None:''', '''    async def async_rotate_google_cookies(self) -> list[str]:
        """Ask Google to rotate the session cookies, as a signed-in browser does.

        Returns the names of the cookies that changed. Raises
        BadCredentialsException when Google no longer accepts the session.
        """
        if not self._google_cookies:
            return []
        async with self._session.post(
            _GOOGLE_ROTATE_COOKIES_URL,
            headers={
                "Content-Type": "application/json",
                "Origin": "https://accounts.google.com",
                "User-Agent": _USER_AGENT,
                "cookie": self._google_cookies,
            },
            data='[000,"-0000000000000000000"]',
        ) as response:
            self._google_cookies, rotated = _merge_set_cookie_headers(
                self._google_cookies, response.headers.getall("Set-Cookie", [])
            )
            if response.status == 401:
                raise BadCredentialsException("Google rejected the cookie rotation")
            if response.status >= 400:
                raise NestServiceException(
                    f"Google cookie rotation failed with HTTP {response.status}"
                )
            return rotated

    @property
    def google_cookies(self) -> str | None:'''),
]

COORDINATOR_EDITS = [
    ('''INITIAL_BACKOFF_SECONDS = 0.1
''', '''INITIAL_BACKOFF_SECONDS = 0.1
GOOGLE_COOKIE_ROTATE_SECONDS = 600
'''),
    ('''        self._poll_task: asyncio.Task | None = None
''', '''        self._poll_task: asyncio.Task | None = None
        self._rotate_task: asyncio.Task | None = None
'''),
    ('''            else:
                _LOGGER.debug("Camera event polling is disabled, not starting it")
''', '''            else:
                _LOGGER.debug("Camera event polling is disabled, not starting it")
            if self.config_entry.data.get(CONF_ACCOUNT_TYPE) == "google":
                self._rotate_task = self.config_entry.async_create_background_task(
                    self.hass,
                    self._async_rotate_google_cookies(),
                    "nest-rotate-google-cookies",
                )
'''),
    ('''        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
''', '''        if self._poll_task:
            self._poll_task.cancel()
            self._poll_task = None
        if self._rotate_task:
            self._rotate_task.cancel()
            self._rotate_task = None

    async def _async_rotate_google_cookies(self) -> None:
        """Keep the Google session alive the way an open browser tab does."""
        while True:
            await asyncio.sleep(GOOGLE_COOKIE_ROTATE_SECONDS)
            async with self._auth_lock:
                try:
                    rotated = await self.client.async_rotate_google_cookies()
                except (ClientError, TimeoutError, PynestException) as err:
                    # The hourly issueToken request reports a dead session
                    # properly; this only keeps a live one going.
                    _LOGGER.debug("Google cookie rotation failed: %r", err)
                else:
                    if rotated:
                        _LOGGER.debug("Google rotated cookies: %s", ", ".join(rotated))
                finally:
                    self._async_save_google_cookies()
'''),
]

FILES = {'pynest/client.py': CLIENT_EDITS, 'coordinator.py': COORDINATOR_EDITS}


def ssh(cmd, data=None):
    return subprocess.run(SSH + [cmd], input=data, capture_output=True, check=True).stdout


def apply_edits(src, edits):
    for old, new in edits:
        if src.count(old) != 1:
            raise SystemExit(f'anchor not found exactly once, upstream changed; not patching:\n{old[:120]}')
        src = src.replace(old, new, 1)
    return src


def main():
    client = ssh(f'cat {ROOT}/pynest/client.py').decode()
    if MARKER in client:
        print('nest_legacy is patched')
        return 0
    if '--check' in sys.argv:
        print('nest_legacy is NOT patched (a HACS update probably replaced it); run nest_rotate_patch.py, then restart HA')
        return 1
    # Build both files before writing either, so a moved anchor leaves HA untouched.
    out = {rel: apply_edits(ssh(f'cat {ROOT}/{rel}').decode(), edits) for rel, edits in FILES.items()}
    for rel, src in out.items():
        path = f'{ROOT}/{rel}'
        ssh(f'cp -f {path} {path}.orig && cat > {path}.tmp && mv -f {path}.tmp {path}', src.encode())
        print('patched', rel)
    print('restart HA to load it')
    return 0


if __name__ == '__main__':
    sys.exit(main())
