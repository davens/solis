"""Write the Sankey v2 layout onto the live energy-live dashboard.

Source of truth is sankey_tests/layout_v2.py. This script only transcribes it,
after checking the things that have actually gone wrong on this chart before.
Dry run unless --apply.

    uv run --no-project --with websockets python sankey_apply.py
    uv run --no-project --with websockets python sankey_apply.py --apply

The predecessor of this script lived in a session scratchpad and was lost with
it, which is why this one is in git.

The guards are not ceremony. Each corresponds to a failure that shipped:

  1  the card is found exactly once, by type, in one known dashboard
  2  every link spans exactly ONE section -- a multi-section link makes the card
     synthesise an unlabelled ghost box coloured like its target, which is what
     once made the battery appear to charge itself (CLAUDE.md, 2026-08-28)
  3  every link endpoint is a declared node
  4  every section-1 node is a terminus, so the chart stays two columns
  5  every entity the config names actually exists in hass.states
  6  every link `value:` entity has RECORDER STATISTICS ROWS. This is the
     subtle one and it must stay. With energy_date_selection the card reads
     sum(change) from statistics, and a target with no rows returns null ->
     Number("null") is NaN -> that NaN lands in the source's parent_spent ->
     every LATER ribbon from that source silently goes to zero. The property
     that matters is state_class, not availability: an `unavailable` entity is
     still in hass.states and still gets substituted.
"""
import argparse
import asyncio
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "sankey_tests"))

import layout_v2  # noqa: E402

HOST = os.environ.get("HA_HOST", "homeassistant.local:8123")
DASHBOARD = "energy-live"  # storage dashboards use a hyphen in the URL path
CARD_TYPE = "custom:sankey-chart"


def _token():
    """The long-lived token, read in process. Never write it to a file."""
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


class Ws:
    def __init__(self, ws):
        self._ws = ws
        self._id = 0

    async def cmd(self, payload):
        self._id += 1
        payload = dict(payload, id=self._id)
        await self._ws.send(json.dumps(payload))
        while True:
            msg = json.loads(await self._ws.recv())
            if msg.get("id") == payload["id"] and msg["type"] == "result":
                if not msg.get("success"):
                    raise SystemExit("HA rejected %s: %s" % (payload["type"], msg.get("error")))
                return msg["result"]


def find_card(config):
    """(card, path) for the one sankey card, or raise."""
    found = []

    def walk(node, path):
        if isinstance(node, dict):
            if node.get("type") == CARD_TYPE:
                found.append((node, list(path)))
            for key, value in node.items():
                walk(value, path + [key])
        elif isinstance(node, list):
            for i, value in enumerate(node):
                walk(value, path + [i])

    walk(config, [])
    if len(found) != 1:
        raise SystemExit("guard 1: expected exactly one %s, found %d" % (CARD_TYPE, len(found)))
    return found[0]


def check_layout(nodes, links):
    section = {n["id"]: n.get("section", 0) for n in nodes}
    problems = []

    for link in links:
        for end in ("source", "target"):
            if link[end] not in section:
                problems.append("guard 3: link %s -> %s names unknown node %r"
                                % (link["source"], link["target"], link[end]))
        if link["source"] in section and link["target"] in section:
            span = section[link["target"]] - section[link["source"]]
            if span != 1:
                problems.append("guard 2: link %s -> %s spans %d sections, not 1"
                                % (link["source"], link["target"], span))

    sources = {link["source"] for link in links}
    for node in nodes:
        if node.get("section", 0) == 1 and node["id"] in sources:
            problems.append("guard 4: %s is in section 1 but has outgoing links, "
                            "so it is not a terminus" % node["id"])
    return problems


def entities_of(nodes, links):
    """Every entity id the config names, split into (all, link values)."""
    values = {link["value"] for link in links if "value" in link}
    every = set(values)
    for node in nodes:
        if node["id"].startswith("sensor."):
            every.add(node["id"])
        if node.get("entity_id"):
            every.add(node["entity_id"])
        every.update(node.get("add_entities", []))
    return every, values


async def run(apply, backup_path):
    import websockets

    token = _token()
    async with websockets.connect("ws://%s/api/websocket" % HOST, max_size=50_000_000) as sock:
        await sock.recv()
        await sock.send(json.dumps({"type": "auth", "access_token": token}))
        await sock.recv()
        ws = Ws(sock)

        nodes, links, sections = layout_v2.build()
        problems = check_layout(nodes, links)

        every, values = entities_of(nodes, links)
        states = {s["entity_id"] for s in await ws.cmd({"type": "get_states"})}
        for entity in sorted(every - states):
            problems.append("guard 5: %s is in the layout but not in hass.states" % entity)

        stat_ids = {row["statistic_id"]
                    for row in await ws.cmd({"type": "recorder/list_statistic_ids"})}
        for entity in sorted(values - stat_ids):
            problems.append("guard 6: link value %s has NO statistics rows -- it would "
                            "return null, NaN-poison its source, and silently zero every "
                            "later ribbon from it" % entity)

        if problems:
            for p in problems:
                print("  " + p)
            raise SystemExit("%d guard failure(s); nothing written" % len(problems))

        config = await ws.cmd({"type": "lovelace/config", "url_path": DASHBOARD})
        card, path = find_card(config)
        print("card found at %s" % "/".join(str(p) for p in path))
        print("  nodes %d -> %d, links %d -> %d, sections %d -> %d"
              % (len(card.get("nodes", [])), len(nodes),
                 len(card.get("links", [])), len(links),
                 len(card.get("sections", [])), len(sections)))

        before = {k: card.get(k) for k in ("nodes", "links", "sections")}
        added = [n["id"] for n in nodes if n["id"] not in {c["id"] for c in card.get("nodes", [])}]
        removed = [n["id"] for n in card.get("nodes", []) if n["id"] not in {c["id"] for c in nodes}]
        print("  nodes added:   %s" % (added or "none"))
        print("  nodes removed: %s" % (removed or "none"))
        print("  all %d guards passed" % 6)

        if not apply:
            print("\nDRY RUN -- nothing written. Re-run with --apply.")
            return

        with open(backup_path, "w") as fh:
            json.dump(before, fh, indent=1)
        print("backed up the card's previous nodes/links/sections to %s" % backup_path)

        card["nodes"] = nodes
        card["links"] = links
        card["sections"] = sections
        await ws.cmd({"type": "lovelace/config/save", "url_path": DASHBOARD, "config": config})
        print("written to the %s dashboard" % DASHBOARD)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--apply", action="store_true", help="actually write (default is a dry run)")
    ap.add_argument("--backup", default="sankey_card_backup.json")
    args = ap.parse_args()
    asyncio.run(run(args.apply, args.backup))


if __name__ == "__main__":
    main()
