"""Create/update the Home Assistant helpers behind the Sankey v2 flow meters.

Source of truth is sankey_tests/flow_sensors_v2.yaml, which is itself generated
by sankey_tests/gen_flow_yaml_v2.py. Each flow is a chain of three helpers:

    flow_<s>_to_<t>_power    template, W       (the Jinja from the YAML)
    flow_<s>_to_<t>_energy   integration, kWh  (Riemann, left, max_sub 60 s)
    flow_<s>_to_<t>_daily    utility_meter, daily cycle

They are config-entry helpers rather than YAML because there is no write path
to /config from the dev machine; the YAML is the readable form and the thing
the parity tests render.

Dry run unless --apply.

    uv run --no-project --with pyyaml --with websockets python sankey_helpers.py
    uv run --no-project --with pyyaml --with websockets python sankey_helpers.py --apply

ORDER MATTERS, and the script enforces it: existing power templates are updated
BEFORE any new chain is created. Updating first briefly leaves the air-con watts
assigned to no link at all, which the card renders as a slightly shorter source
bar and nothing else. Creating first would briefly count them twice, once inside
House and once in Air con, which is a wrong number rather than a missing one.
"""
import argparse
import asyncio
import json
import os
import sys
import urllib.error
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, "sankey_tests"))

YAML_PATH = os.path.join(HERE, "sankey_tests", "flow_sensors_v2.yaml")
HOST = os.environ.get("HA_HOST", "homeassistant.local:8123")


def _token():
    with open(os.path.expanduser("~/.claude.json")) as fh:
        cfg = json.load(fh)

    def find(o):
        if isinstance(o, dict):
            servers = o.get("mcpServers")
            if isinstance(servers, dict) and "home-assistant" in servers:
                for k, v in servers["home-assistant"].get("headers", {}).items():
                    if k.lower() == "authorization":
                        return v
            for v in o.values():
                got = find(v)
                if got:
                    return got
        return None

    tok = find(cfg)
    if not tok:
        raise SystemExit("no Home Assistant token found in ~/.claude.json")
    return tok.replace("Bearer ", "")


def wanted():
    """[(slug, title, state jinja, availability jinja)] from the YAML."""
    import yaml

    with open(YAML_PATH) as fh:
        doc = yaml.safe_load(fh)
    out = []
    for sensor in doc["template"][0]["sensor"]:
        slug = sensor["unique_id"][len("flow_"):-len("_power")]
        out.append((slug, sensor["name"], sensor["state"], sensor["availability"]))
    return out


class Rest:
    """Config and options flows are REST-only.

    There is no `config_entries/options/flow` websocket command -- HA answers it
    with a bare `unknown_command`, which is what the first --apply run hit. The
    frontend drives both flow kinds over REST, so this does too. A flow step
    takes the user input as the whole body, NOT wrapped in a `user_input` key.
    """

    def __init__(self, host, token):
        self._base = "http://%s/api/config/config_entries" % host
        self._token = token

    def _post(self, path, body):
        req = urllib.request.Request(
            self._base + path,
            data=json.dumps(body).encode(),
            headers={"Authorization": "Bearer " + self._token,
                     "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as exc:
            raise SystemExit("HA rejected POST %s: %s %s"
                             % (path, exc.code, exc.read().decode()[:400]))

    def start(self, handler, options=False):
        return self._post("/options/flow" if options else "/flow",
                          {"handler": handler, "show_advanced_options": True})

    def step(self, flow, user_input, options=False):
        got = self._post("%s/%s" % ("/options/flow" if options else "/flow", flow["flow_id"]),
                         user_input)
        if got.get("type") == "form" and got.get("errors"):
            raise SystemExit("flow step returned errors: %s" % got["errors"])
        return got


class Ws:
    def __init__(self, sock):
        self._sock = sock
        self._id = 0

    async def cmd(self, payload):
        self._id += 1
        payload = dict(payload, id=self._id)
        await self._sock.send(json.dumps(payload))
        while True:
            msg = json.loads(await self._sock.recv())
            if msg.get("id") == payload["id"] and msg["type"] == "result":
                if not msg.get("success"):
                    raise SystemExit("HA rejected %s: %s" % (payload["type"], msg.get("error")))
                return msg["result"]


async def main_async(apply_changes):
    import websockets

    token = _token()
    async with websockets.connect("ws://%s/api/websocket" % HOST, max_size=50_000_000) as sock:
        await sock.recv()
        await sock.send(json.dumps({"type": "auth", "access_token": token}))
        await sock.recv()
        ws = Ws(sock)
        rest = Rest(HOST, token)

        entries = await ws.cmd({"type": "config_entries/get"})
        by_title = {}
        for entry in entries:
            by_title.setdefault(entry["domain"], {})[entry["title"]] = entry

        # `config_entries/get` returns a summary without `options`, so there is no
        # cheap way to tell an already-correct template from a stale one. Every
        # existing template is therefore rewritten unconditionally; writing the
        # same Jinja back is a no-op, and guessing wrong would leave one sensor
        # on the old preamble, which is precisely the silent half-migration this
        # script exists to prevent.
        updates, creations = [], []
        for slug, title, state, availability in wanted():
            existing = by_title.get("template", {}).get(title)
            if existing is None:
                creations.append((slug, title, state, availability))
            else:
                updates.append((slug, title, state, availability, existing["entry_id"]))

        missing_chain = []
        for slug, title, _, _ in wanted():
            for domain, suffix in (("integration", "energy"), ("utility_meter", "daily")):
                name = "Flow %s %s" % (slug.replace("_", " "), suffix)
                if name not in by_title.get(domain, {}):
                    missing_chain.append((domain, name, slug))

        print("power templates to REWRITE (unconditional): %d" % len(updates))
        for slug, *_ in updates:
            print("    %s" % slug)
        print("power templates to CREATE: %d" % len(creations))
        for slug, *_ in creations:
            print("    %s" % slug)
        print("downstream helpers to CREATE: %d" % len(missing_chain))
        for domain, name, _ in missing_chain:
            print("    %-14s %s" % (domain, name))

        if not apply_changes:
            print("\nDRY RUN -- nothing written. Re-run with --apply.")
            return

        # 1. Update the existing templates first. See the module docstring.
        for slug, title, state, availability, entry_id in updates:
            flow = rest.start(entry_id, options=True)
            rest.step(flow, {
                "state": state,
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
                "additional_options": {"availability": availability},
            }, options=True)
            print("updated  %s" % slug)

        # 2. New power templates.
        for slug, title, state, availability in creations:
            flow = rest.start("template")
            if flow.get("step_id") == "user":
                flow = rest.step(flow, {"next_step_id": "sensor"})
            rest.step(flow, {
                "name": title,
                "state": state,
                "unit_of_measurement": "W",
                "device_class": "power",
                "state_class": "measurement",
                "additional_options": {"availability": availability},
            })
            print("created  template %s" % slug)

        # 3. Riemann integration and daily utility_meter for the new chains.
        for domain, name, slug in missing_chain:
            if domain == "integration":
                user_input = {
                    "name": name,
                    "source": "sensor.flow_%s_power" % slug,
                    "method": "left",
                    "round": 3,
                    "unit_prefix": "k",
                    "unit_time": "h",
                    "max_sub_interval": {"hours": 0, "minutes": 1, "seconds": 0},
                }
            else:
                user_input = {
                    "name": name,
                    "source": "sensor.flow_%s_energy" % slug,
                    "cycle": "daily",
                    "always_available": False,
                    "delta_values": False,
                    "net_consumption": False,
                    "offset": 0,
                    "periodically_resetting": True,
                    "tariffs": [],
                }
            flow = rest.start(domain)
            rest.step(flow, user_input)
            print("created  %-14s %s" % (domain, name))

        print("\ndone")


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    args = ap.parse_args()
    asyncio.run(main_async(args.apply))


if __name__ == "__main__":
    main()
