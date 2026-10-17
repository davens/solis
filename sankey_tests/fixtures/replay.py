"""Offline replay of recorded power history, with blind-channel reporting.
Captures lack EPS and fridge: both are held at zero for arithmetic only.
House + Fridge = H - T is knowable when Tesla is known; House + Tesla +
Fridge = H always. EPS blindness refuses Air con, Inverter and source-spent
totals. Export and battery-charge totals remain knowable. Updated 2026-10-17.
"""
import gzip
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))

# v1's four channels. Kept under this name because other modules import it.
CHANNELS = ("solar", "battery", "grid", "house")

# v2 adds the car. `tesla` lives in a SEPARATE capture (tesla_history_<day>.json.gz)
# because it was captured later, from different entities, and only 2026-08-28
# onward has full-day coverage -- 2026-08-27 has a single sample at 13:27 local.
# load_day() merges it in when it is present, and everything downstream keys off
# what the doc actually carries rather than a hard-coded tuple, so a four-channel
# doc still replays exactly as it did before.
CHANNELS_V2 = CHANNELS + ("tesla",)

ALL_CHANNELS = CHANNELS_V2 + ("eps", "fridge")

# The watts fed to the blind channel. A hold, not a reading.
EPS_HOLD_W = 0.0
FRIDGE_HOLD_W = 0.0


def channels_of(doc):
    """The channels this doc actually carries. Four or five."""
    return CHANNELS_V2 if "tesla" in doc.get("series", {}) else CHANNELS


def blind_channels(doc):
    return frozenset(ALL_CHANNELS) - frozenset(channels_of(doc))

# HA's `integration` platform re-integrates at least this often when
# max_sub_interval is set. For a LEFT Riemann sum over a held value this is
# mathematically a no-op (see test_max_sub_interval_is_identity_for_left_riemann);
# it is modelled so the harness matches the platform we will actually deploy.
MAX_SUB_INTERVAL_S = 60.0


def load_day(day, tesla=False):
    """One day of recorded power history.

    tesla=False gives the original four channels, byte-for-byte as before.
    tesla=True merges the sidecar capture and adds a fifth, so the same walker
    serves both v1 and v2 without a second implementation of the left-hold
    integrator -- there were three of those in the tree at one point.
    """
    path = os.path.join(HERE, "history_%s.json.gz" % day)
    with gzip.open(path, "rt") as fh:
        doc = json.load(fh)
    if tesla:
        with gzip.open(os.path.join(HERE, "tesla_history_%s.json.gz" % day), "rt") as fh:
            side = json.load(fh)
        doc = dict(doc, series=dict(doc["series"], tesla=side["series"]["tesla"]))
        doc["tesla_counters"] = side.get("counters", {})
    return doc


def load_counters():
    with open(os.path.join(HERE, "counters.json")) as fh:
        return json.load(fh)


def _numeric(points, start):
    """[[ts, state]] -> [(ts, float|None)], clipped to start, None = gap."""
    out = []
    for ts, s in points:
        try:
            v = float(s)
        except (TypeError, ValueError):
            v = None  # 'unavailable' / 'unknown'
        out.append((max(float(ts), start), v))
    out.sort(key=lambda p: p[0])
    return out


def intervals(doc, end_epoch=None, max_sub_interval=MAX_SUB_INTERVAL_S):
    """Yield (t, dt, {channel: value|None}) over the merged event timeline.

    Values are held from the last sample at or before t (left Riemann).
    """
    start = float(doc["start_epoch"])
    end = float(end_epoch if end_epoch is not None else doc["end_epoch"])
    channels = channels_of(doc)
    series = {c: _numeric(doc["series"][c], start) for c in channels}

    marks = {start, end}
    for pts in series.values():
        for ts, _ in pts:
            if start <= ts <= end:
                marks.add(ts)
    marks = sorted(marks)

    idx = {c: 0 for c in channels}
    held = {c: None for c in channels}
    for i in range(len(marks) - 1):
        t0, t1 = marks[i], marks[i + 1]
        for c in channels:
            pts = series[c]
            while idx[c] < len(pts) and pts[idx[c]][0] <= t0:
                held[c] = pts[idx[c]][1]
                idx[c] += 1
        if t1 <= t0:
            continue
        t = t0
        while t < t1:
            step = min(max_sub_interval, t1 - t)
            yield t, step, dict(held)
            t += step


def replay(doc, decompose, end_epoch=None, max_sub_interval=MAX_SUB_INTERVAL_S):
    """Integrate decomposed flows and raw channels over one day. kWh."""
    flows = {}
    raw = {"solar": 0.0, "house": 0.0, "grid_import": 0.0, "grid_export": 0.0,
           "battery_charge": 0.0, "battery_discharge": 0.0,
           # H-only residual: includes unmeasured EPS as well as conversion/aux
           # loss. This diagnostic cannot measure inverter loss without EPS.
           "dc_ac_residual": 0.0}
    gap_s = 0.0
    live_s = 0.0
    tesla_assumed_s = 0.0
    eps_assumed_s = 0.0
    fridge_assumed_s = 0.0
    five = "tesla" in channels_of(doc)
    for _t, dt, v in intervals(doc, end_epoch, max_sub_interval):
        if any(v[c] is None for c in CHANNELS):
            gap_s += dt
            continue
        live_s += dt
        h = dt / 3_600_000.0  # W*s -> kWh
        solar, battery, grid, house = v["solar"], v["battery"], v["grid"], v["house"]
        raw["solar"] += max(0.0, solar) * h
        raw["house"] += max(0.0, house) * h
        raw["grid_import"] += max(0.0, grid) * h
        raw["grid_export"] += max(0.0, -grid) * h
        raw["battery_charge"] += max(0.0, battery) * h
        raw["battery_discharge"] += max(0.0, -battery) * h
        raw["dc_ac_residual"] += ((solar - battery) - (house - grid)) * h
        if five:
            # Before the car's first recorded sample the channel is None. Treating
            # it as 0 W is an ASSUMPTION, not a measurement, so the time it covers
            # is returned alongside the totals and never silently absorbed.
            tesla = v["tesla"]
            if tesla is None:
                tesla = 0.0
                tesla_assumed_s += dt
            raw["tesla"] = raw.get("tesla", 0.0) + min(max(0.0, tesla), max(0.0, house)) * h
        else:
            # A four-channel doc has no car reading at all, so the whole window
            # runs on the same assumption rather than part of it.
            tesla = 0.0
            tesla_assumed_s += dt
        eps_assumed_s += dt
        fridge_assumed_s += dt
        out = decompose(solar, battery, grid, house, tesla, EPS_HOLD_W, FRIDGE_HOLD_W)
        for k, w in out.items():
            flows[k] = flows.get(k, 0.0) + w * h
    return {"flows": flows, "raw": raw, "gap_s": gap_s, "live_s": live_s,
            "tesla_assumed_s": tesla_assumed_s,
            "eps_assumed_s": eps_assumed_s,
            "fridge_assumed_s": fridge_assumed_s,
            "blind_channels": blind_channels(doc)}


def last_sample_epoch(doc):
    """Earliest 'last sample' across the inverter channels - the honest end of a
    partial day.

    Deliberately CHANNELS and not channels_of(doc): the Tesla sensor publishes
    on change and can go 80 minutes without an update (CLAUDE.md), so its last
    sample says nothing about when the recording stopped. Using it would
    truncate a partial day by over an hour for no reason.
    """
    return min(float(doc["series"][c][-1][0]) for c in CHANNELS)


def derived(flows):
    """The quantities that face the inverter's own daily counters.

    Handles both shapes. v2's eighteen flows give the battery and the grid more
    than one destination each, so `grid_import` is the sum of FOUR flows rather
    than two and `battery_discharge` of three -- summing only the v1 subset is
    what made v1's "leak" tests look like defects when they were really
    incomplete sums.
    """
    if "solar_to_tesla" in flows:
        src = ("solar", "battery", "grid")
        return {
            "house": sum(flows[s + "_to_house"] for s in src),
            "tesla": sum(flows[s + "_to_tesla"] for s in src),
            "aircon": sum(flows[s + "_to_aircon"] for s in src),
            "fridge": sum(flows[s + "_to_fridge"] for s in src),
            "house_and_fridge": sum(flows[s + "_to_house"] for s in src)
                                + sum(flows[s + "_to_fridge"] for s in src),
            "inverter": sum(flows[s + "_to_inverter"] for s in src),
            "grid_export": flows["solar_to_export"],
            "grid_import": sum(v for k, v in flows.items() if k.startswith("grid_to_")),
            "battery_charge": flows["solar_to_battery"] + flows["grid_to_battery"],
            "battery_discharge": sum(v for k, v in flows.items()
                                     if k.startswith("battery_to_")),
            "solar": sum(v for k, v in flows.items() if k.startswith("solar_to_")),
        }
    return {
        "house": flows["solar_to_house"] + flows["battery_to_house"] + flows["grid_to_house"],
        "grid_export": flows["solar_to_export"],
        "grid_import": flows["grid_to_house"] + flows["grid_to_battery"],
        "battery_charge": flows["solar_to_battery"] + flows["grid_to_battery"],
        "battery_discharge": flows["battery_to_house"],
        "solar": flows["solar_to_house"] + flows["solar_to_battery"] + flows["solar_to_export"],
    }


DERIVED_QUANTITY = {
    "house": "house",
    "tesla": "tesla",
    "aircon": "aircon",
    "fridge": "fridge",
    "inverter": "inverter",
    "grid_export": "export",
    "grid_import": "grid_spent",
    "battery_charge": "battery_in",
    "battery_discharge": "battery_spent",
    "solar": "solar_spent",
}


def unknowable(doc):
    """derived() keys this doc's blind channels make unreportable.

    The caller reports None for each, never 0.0. Delegates the dependency rules
    to `flows.unreportable()` rather than restating them, because a hardcoded
    list rots: add a sixteenth flow reading a blind channel and a stale list
    happily reports a fabricated number for it.
    """
    import flows  # local: this module is otherwise import-free stdlib
    blind = blind_channels(doc)
    dead = flows.unreportable(blind)
    out = set(k for k, q in DERIVED_QUANTITY.items() if q in dead)
    if (flows.DEPENDS_ON["house"] - {"fridge"}) & blind:
        out.add("house_and_fridge")
    return frozenset(out)


def blind_flows(doc):
    import flows  # local: this module is otherwise import-free stdlib
    return frozenset(q for q in flows.unreportable(blind_channels(doc))
                     if "_to_" in q)


COUNTER_KEY = {
    "house": "house_consumption_today",
    "grid_export": "grid_export_today",
    "grid_import": "grid_import_today",
    "battery_charge": "battery_charge_today",
    "battery_discharge": "battery_discharge_today",
    "solar": "solar_today",
}


def pct(got, want):
    return None if not want else 100.0 * (got - want) / want


if __name__ == "__main__":
    import sys
    sys.path.insert(0, os.path.dirname(HERE))
    from flows import decompose  # noqa: E402

    counters = load_counters()["days"]
    for day in sorted(counters):
        doc = load_day(day, tesla=True)
        partial = day == max(counters)
        end = last_sample_epoch(doc) if partial else None
        r = replay(doc, decompose, end_epoch=end)
        d = derived(r["flows"])
        dead = unknowable(doc)
        rawv = dict(r["raw"])
        rawv["house_and_fridge"] = r["raw"]["house"] - r["raw"].get("tesla", 0.0)
        # The reportable House figure is house-excluding-car, so the counter it
        # faces is house_consumption_today MINUS the car. Against the raw
        # counter it would read as a 22 kWh error on a heavy charging day.
        car = doc.get("tesla_counters", {}).get("tesla_home_charging_energy")
        car_kwh = (float(car["last"]) - float(car["first"])) if car else 0.0
        print("\n=== %s %s  gap %.0f s of %.0f s  blind: %s ===" % (
            day, "(PARTIAL)" if partial else "", r["gap_s"],
            r["gap_s"] + r["live_s"],
            ", ".join(sorted(r["blind_channels"])) or "none"))
        print("  (house+fridge rows are house-excluding-car on both sides; "
              "aircon and fridge are held at %g W and refused)" % EPS_HOLD_W)
        print("  %-18s %8s %8s %8s %8s" % ("quantity", "flows", "counter", "raw-int", "err%"))
        for k in ("house", "eps", "fridge", "house_and_fridge", "grid_export",
                  "grid_import", "battery_charge", "battery_discharge",
                  "solar"):
            if k in dead:
                print("  %-18s %8s %8s %8s %8s   (blind: %s)" % (
                    k, "n/a", "n/a", "n/a", "n/a",
                    ", ".join(sorted(r["blind_channels"]))))
                continue
            c = counters[day][COUNTER_KEY[k]]["max"] if k in COUNTER_KEY else None
            if k == "house_and_fridge":
                # The one quantity with no COUNTER_KEY of its own: it faces
                # house_consumption_today MINUS the car, which is the only
                # figure on the counter side that means the same thing.
                c = counters[day]["house_consumption_today"]["max"] - car_kwh
            if c is None:
                print("  %-18s %8.3f %8s %8.3f %8s" % (
                    k, d[k], "-", rawv[k], "-"))
                continue
            e = pct(d[k], c)
            print("  %-18s %8.3f %8.3f %8.3f %+8.2f" % (
                k, d[k], c, rawv[k], e if e is not None else float("nan")))
        dead_flows = blind_flows(doc)
        print("  flows: " + ", ".join(
            "%s=%s" % (k, "n/a" if k in dead_flows else "%.3f" % v)
            for k, v in sorted(r["flows"].items())))
        print("  H-only residual (includes unmeasured EPS): %+.3f kWh"
              % r["raw"]["dc_ac_residual"])
        c = counters[day]
        cres = ((c["solar_today"]["max"] - c["battery_charge_today"]["max"]
                 + c["battery_discharge_today"]["max"])
                - (c["house_consumption_today"]["max"]
                   - c["grid_import_today"]["max"] + c["grid_export_today"]["max"]))
        print("  dc/ac residual from counters:      %+.3f kWh" % cres)
        print("  flows vs RAW INTEGRAL err%%: " + ", ".join(
            "%s=%s" % (k, "n/a" if k in dead else "%+.2f" % pct(d[k], rawv[k]))
            for k in ("house_and_fridge", "grid_export", "grid_import",
                      "battery_charge", "battery_discharge", "solar")))
