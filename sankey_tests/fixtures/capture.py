"""Capture recorded Solis history from Home Assistant into offline fixtures.

Run manually (needs network + HA token):

    uv run --no-project python fixtures/capture.py

Writes fixtures/history_<YYYY-MM-DD>.json.gz (raw ~10 s power states) and
fixtures/counters.json (inverter daily counters per day).

The HA token is read in-process by the scratchpad `ha` helper from ~/.claude.json.
It is never printed and never written into any fixture.
"""
import datetime as dt
import gzip
import json
import os
import sys
import urllib.parse
import zoneinfo

# The `ha` REST helper lives outside the repo; point HA_HELPER_DIR at its folder.
sys.path.insert(0, os.environ["HA_HELPER_DIR"])
import ha  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
TZ = zoneinfo.ZoneInfo("Europe/London")

POWER = {
    "solar": "sensor.solis_inverter_solar_power",
    "battery": "sensor.solis_inverter_battery_power",
    "grid": "sensor.solis_inverter_grid_power",
    "house": "sensor.solis_inverter_house_load",
}
COUNTERS = {
    "solar_today": "sensor.solis_inverter_solar_today",
    "grid_import_today": "sensor.solis_inverter_grid_import_today",
    "grid_export_today": "sensor.solis_inverter_grid_export_today",
    "battery_charge_today": "sensor.solis_inverter_battery_charge_today",
    "battery_discharge_today": "sensor.solis_inverter_battery_discharge_today",
    "house_consumption_today": "sensor.solis_inverter_house_consumption_today",
}


def day_bounds(day):
    """Local-midnight-to-local-midnight, as UTC-aware datetimes."""
    d = dt.date.fromisoformat(day)
    start = dt.datetime.combine(d, dt.time(0, 0), tzinfo=TZ)
    return start, start + dt.timedelta(days=1)


def history(entity_ids, start, end):
    q = urllib.parse.urlencode({
        "filter_entity_id": ",".join(entity_ids),
        "end_time": end.isoformat(),
        "minimal_response": "",
        "no_attributes": "",
        "significant_changes_only": "0",
    })
    return ha.get("/api/history/period/%s?%s" % (start.isoformat(), q))


def series_of(raw):
    """[[epoch_seconds, state_string], ...] keyed by entity_id."""
    out = {}
    for block in raw:
        if not block:
            continue
        eid = block[0]["entity_id"]
        pts = []
        for p in block:
            ts = p.get("last_changed") or p.get("last_updated")
            pts.append([round(dt.datetime.fromisoformat(ts).timestamp(), 3), p["state"]])
        out[eid] = pts
    return out


def capture_day(day):
    start, end = day_bounds(day)
    raw = history(sorted(POWER.values()), start, end)
    by_eid = series_of(raw)
    doc = {
        "day": day,
        "tz": "Europe/London",
        "start_epoch": start.timestamp(),
        "end_epoch": end.timestamp(),
        "captured_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
        "source": "GET /api/history/period (raw states, minimal_response, "
                  "significant_changes_only=0) from homeassistant.local:8123",
        "series": {k: by_eid.get(v, []) for k, v in POWER.items()},
    }
    path = os.path.join(HERE, "history_%s.json.gz" % day)
    with gzip.open(path, "wt") as fh:
        json.dump(doc, fh, separators=(",", ":"))
    n = {k: len(v) for k, v in doc["series"].items()}
    print(day, "power points", n, "->", os.path.getsize(path), "bytes")
    return doc


def capture_counters(days):
    out = {}
    for day in days:
        start, end = day_bounds(day)
        raw = history(sorted(COUNTERS.values()), start, end)
        by_eid = series_of(raw)
        rec = {}
        for name, eid in COUNTERS.items():
            vals = []
            for ts, s in by_eid.get(eid, []):
                try:
                    vals.append(float(s))
                except ValueError:
                    continue  # unavailable / unknown
            rec[name] = {
                "max": max(vals) if vals else None,
                "last": vals[-1] if vals else None,
                "n": len(vals),
            }
        out[day] = rec
        print(day, {k: v["max"] for k, v in rec.items()})
    path = os.path.join(HERE, "counters.json")
    with open(path, "w") as fh:
        json.dump({
            "captured_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
            "source": "GET /api/history/period over each local day; 'max' is the "
                      "end-of-day value of a total_increasing counter that resets "
                      "at inverter local midnight (~23:59:52 BST).",
            "days": out,
        }, fh, indent=1, sort_keys=True)
    return out


if __name__ == "__main__":
    days = sys.argv[1:] or ["2026-08-27", "2026-08-28", "2026-08-29", "2026-08-30"]
    for d in days:
        capture_day(d)
    capture_counters(days)
