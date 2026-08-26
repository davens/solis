"""Read-only backend for the Solis inverter: poller + JSON API.

A background thread holds one Solarman session and refreshes a cache;
/api/state serves that cache and /api/health reports whether it is fresh.
No HTML lives here - dash.py is the browser front end (run via
settings_dash.py), and Home Assistant consumes /api/state directly
(see homeassistant.md).

  uv run --no-project --with flask --with pysolarmanv5 python solis_api.py

This process never writes a holding register: the write path (/api/write and
the rules engine) was removed 2026-08-26 at the owner's request, so nothing
network-reachable can change inverter state. control.py is the only way to
write. Charge side only: discharge windows are all unset and 43142 does not
cap house supply, so the backend does not read them either.
"""
from datetime import date, datetime, timedelta
import json
import os
import threading
import time

from flask import Flask, jsonify

import control
import octopus
import solar_forecast
import solis_net

app = Flask(__name__)

POLL_SECONDS = 10
# Served LAN-wide so a phone can watch the tiles - safe because this process
# has no write path at all.
HOST = "0.0.0.0"
PORT = int(os.environ.get("SOLIS_PORT", "5051"))

# Input registers (FC 0x04), read as two blocks. Verified live 2026-08-21 by
# closing the energy balance: PV 2033 W + battery 1063 W = house load 3008 W.
REG_GRID_VOLTAGE = 33073

# Daily yield counters. 33036 read 31.7 kWh for 2026-08-20, matching the 32 kWh
# the owner recorded that day, which is what confirms the scaling and the pair.
REG_ENERGY_TODAY = 33035      # x0.1 kWh, resets at midnight

# Daily grid and house counters, one 33171..33180 block. Identified by closing
# yesterday's energy balance:
# PV 31.7 + import 15.2 + battery out 6.2 = 53.1 against house 21.8 + export
# 22.7 + battery in 8.2 = 52.7, i.e. 0.4 kWh apart.
# Smart meter grid power, s32 across 33257/33258: positive exporting, negative
# importing. Verified 2026-08-21 twice - +4984 against a measured 4925 W export
# (PV 5834 W DC, AC out 5800 W, house 875 W), then a raw 4294967253 while the
# battery covered the house, which is -43 wrapped, i.e. a 43 W import.
REG_METER_POWER = 33257

ENERGY_DAY_BASE = 33171
ENERGY_DAY_COUNT = 10         # 33171..33180
REG_GRID_IMPORT_TODAY = 33171  # x0.1 kWh
REG_GRID_EXPORT_TODAY = 33175  # x0.1 kWh
REG_HOUSE_TODAY = 33179        # x0.1 kWh
REG_HOUSE_YESTERDAY = 33180    # x0.1 kWh

PV_BASE = 33049
PV_COUNT = 10  # 33049..33058: four string voltage/current pairs, then total power
REG_PV_POWER = 33057      # u32 across 33057/33058, watts

TELEMETRY_BASE = 33133
TELEMETRY_COUNT = 18  # 33133..33150
REG_BATTERY_VOLTAGE = 33133   # x0.1 V
REG_BATTERY_CURRENT = 33134   # x0.1 A - NOT 33135, which is the direction flag
REG_BATTERY_DIRECTION = 33135  # 0 = charging, 1 = discharging
REG_BATTERY_SOC = 33139
REG_BATTERY_SOH = 33140
REG_HOUSE_LOAD = 33147
REG_BATTERY_POWER = 33149     # u32 across 33149/33150, watts, unsigned

_lock = threading.Lock()
_modbus = None
_state = {"ok": False, "error": "starting up", "host": None, "read_at": None}


def _session():
    """Return a live session, opening one if needed. Caller holds _lock."""
    global _modbus
    if _modbus is None:
        host = solis_net.resolve_host(verbose=False)
        _modbus = solis_net.connect(host)
        _state["host"] = host
    return _modbus


def _drop_session():
    """Forget the session so the next read reconnects."""
    global _modbus
    _modbus = None


def _read_all(modbus):
    """One full sweep of telemetry and settings. Caller holds _lock."""
    block = modbus.read_input_registers(register_addr=TELEMETRY_BASE, quantity=TELEMETRY_COUNT)
    pv = modbus.read_input_registers(register_addr=PV_BASE, quantity=PV_COUNT)
    grid_voltage = modbus.read_input_registers(register_addr=REG_GRID_VOLTAGE, quantity=1)[0]
    energy = modbus.read_input_registers(register_addr=REG_ENERGY_TODAY, quantity=2)
    day = modbus.read_input_registers(register_addr=ENERGY_DAY_BASE,
                                      quantity=ENERGY_DAY_COUNT)
    meter = modbus.read_input_registers(register_addr=REG_METER_POWER, quantity=2)
    # Stamp the sweep here, not in the dict below: _forecast() can block on the
    # network and would otherwise backdate itself onto the register reads.
    read_at = datetime.now()

    def tele(addr):
        return block[addr - TELEMETRY_BASE]

    def u32(base, words, first):
        """Solis reports powers as a 32-bit big-endian pair."""
        i = base - first
        return (words[i] << 16) + words[i + 1]

    house_load = tele(REG_HOUSE_LOAD)
    battery_voltage = tele(REG_BATTERY_VOLTAGE) * 0.1
    battery_current = tele(REG_BATTERY_CURRENT) * 0.1
    # The power register is unsigned; 33135 carries the sign.
    charging = tele(REG_BATTERY_DIRECTION) == 0
    battery_power = u32(REG_BATTERY_POWER, block, TELEMETRY_BASE)

    # Grid flow, straight from the meter. Deriving it from PV and battery
    # instead is systematically wrong: those are DC registers, so the
    # inverter's conversion loss (~2%, over 100 W near full output) lands in
    # the residual and reads as phantom export. The meter is signed positive
    # for export; the dashboard's convention is positive for import.
    meter_power = (meter[0] << 16) + meter[1]
    if meter_power >= 1 << 31:
        meter_power -= 1 << 32  # s32; an unsigned read shows imports as ~4.29e9
    grid_power = -meter_power

    strings = []
    for i in range(0, 8, 2):
        volts, amps = pv[i] * 0.1, pv[i + 1] * 0.1
        if volts > 0 or amps > 0:
            # Carry the register's own index. Dead strings are skipped, so the
            # client cannot label by array position: the two planes face SW and
            # SE and so wake and fade at different times, and a dawn where only
            # the SE string is live would otherwise show it as "PV1".
            strings.append({"string": i // 2 + 1,
                            "volts": round(volts, 1), "amps": round(amps, 1),
                            "watts": round(volts * amps)})

    mode = modbus.read_holding_registers(register_addr=control.REG_MODE, quantity=1)[0]
    charge = modbus.read_holding_registers(register_addr=control.REG_CHARGE_CURRENT, quantity=1)[0]

    # Charge windows only. The discharge slots are all unset and 43142 does not
    # cap house supply, so reading them showed six registers of zeroes and cost
    # a third of the poll. control.py still exposes them if they are ever needed.
    slots = []
    for slot in (1, 2, 3):
        addr = control.slot_addr(slot, "charge")
        vals = modbus.read_holding_registers(register_addr=addr, quantity=4)
        slots.append({
            "slot": slot,
            "addr": addr,
            "start": f"{vals[0]:02d}:{vals[1]:02d}",
            "end": f"{vals[2]:02d}:{vals[3]:02d}",
            "start_minutes": vals[0] * 60 + vals[1],
            "end_minutes": vals[2] * 60 + vals[3],
            "unset": vals == [0, 0, 0, 0],
        })

    solar_yesterday = round(energy[1] * 0.1, 1)
    grid_import_today = round(day[REG_GRID_IMPORT_TODAY - ENERGY_DAY_BASE] * 0.1, 1)
    grid_export_today = round(day[REG_GRID_EXPORT_TODAY - ENERGY_DAY_BASE] * 0.1, 1)
    _record_actual(solar_yesterday)
    car = _car()

    return {
        "ok": True,
        "error": None,
        "forecast": _forecast(),
        "car": car,
        "cost": _cost_state(grid_import_today, grid_export_today, car),
        "host": _state.get("host"),
        "read_at": read_at.strftime("%H:%M:%S"),
        "read_at_epoch": read_at.timestamp(),
        "telemetry": {
            "grid_voltage": round(grid_voltage * 0.1, 1),
            "battery_voltage": round(battery_voltage, 1),
            "battery_current": round(battery_current, 1),
            "battery_power": battery_power,
            "battery_charging": charging,
            "soc": tele(REG_BATTERY_SOC),
            "soh": tele(REG_BATTERY_SOH),
            "house_load": house_load,
            "pv_power": u32(REG_PV_POWER, pv, PV_BASE),
            "pv_strings": strings,
            "solar_today": round(energy[0] * 0.1, 1),
            "solar_yesterday": solar_yesterday,
            "grid_power": grid_power,
            "grid_import_today": grid_import_today,
            "grid_export_today": grid_export_today,
            "house_today": round(day[REG_HOUSE_TODAY - ENERGY_DAY_BASE] * 0.1, 1),
            "house_yesterday": round(day[REG_HOUSE_YESTERDAY - ENERGY_DAY_BASE] * 0.1, 1),
        },
        "settings": {
            "mode": mode,
            "mode_bits": [control.MODE_BITS.get(b, f"bit{b}") for b in range(16) if mode >> b & 1],
            "charge_current": round(charge * control.CURRENT_SCALING, 1),
            "slots": slots,
        },
        # Whatever in this block is still unmapped; shown raw so the map can be
        # extended from observed behaviour later.
        "raw_battery_block": {str(TELEMETRY_BASE + i): v for i, v in enumerate(block)},
    }


def _record_actual(yesterday_kwh):
    """Append yesterday's generation (33036) to the forecast's actuals file.

    The calibration dataset was fed by hand; the poller sees the number every
    sweep anyway, so record it once per day. Advisory - never fails the sweep.
    """
    global _actuals_day
    yesterday = (date.today() - timedelta(days=1)).isoformat()
    if _actuals_day == yesterday or yesterday_kwh <= 0:
        return
    try:
        if yesterday not in solar_forecast.load_actuals():
            solar_forecast.record(yesterday, yesterday_kwh)
        _actuals_day = yesterday
    except Exception:
        pass


_actuals_day = None

# SOLIS_DATA_DIR moves the mutable files onto a volume when containerised
# (solar_forecast.py honours the same variable); unset, they stay beside the code.
DATA_DIR = os.environ.get("SOLIS_DATA_DIR") or os.path.dirname(os.path.abspath(__file__))
COST_FILE = os.path.join(DATA_DIR, "energy_cost.json")
_cost = None


def _cost_buckets(import_kwh, cheap_now):
    """Split today's grid import into cheap and peak kWh.

    The inverter only publishes a daily total (33171), so the split is built by
    bucketing each sweep's delta by whether the *current* moment bills cheap -
    inside 23:30-05:30, or during a car dispatch, when Intelligent Go extends
    the off-peak price to the whole house. Persisted so a restart keeps the
    day's split; kWh imported while the dashboard was down land in whichever
    bucket is current when it comes back, and a cold start attributes the
    day-so-far to cheap, which is this house's dominant case (the overnight
    battery fill is the bulk of all import).
    """
    global _cost
    today = date.today().isoformat()
    if _cost is None:
        try:
            with open(COST_FILE) as handle:
                _cost = json.load(handle)
        except (OSError, ValueError):
            _cost = None
    if (not _cost or _cost.get("date") != today
            or import_kwh < _cost.get("last_import", 0.0)):
        seed_cheap = import_kwh if _cost is None else 0.0
        _cost = {"date": today, "cheap_kwh": seed_cheap, "peak_kwh": 0.0,
                 "last_import": import_kwh}
        _save_cost()
    delta = round(import_kwh - _cost["last_import"], 3)
    if delta > 0:
        _cost["cheap_kwh" if cheap_now else "peak_kwh"] = round(
            _cost["cheap_kwh" if cheap_now else "peak_kwh"] + delta, 3)
        _cost["last_import"] = import_kwh
        _save_cost()
    return _cost


def _save_cost():
    try:
        with open(COST_FILE, "w") as handle:
            json.dump(_cost, handle, indent=2)
    except OSError:
        pass


def _cost_state(import_kwh, export_kwh, car):
    """Today's grid £ both ways, or None when Octopus rates are unavailable."""
    cheap_now = (octopus._in_window(datetime.now(octopus.TZ))
                 or bool(car and car.get("charging")))
    buckets = _cost_buckets(import_kwh, cheap_now)
    rates = octopus.tariff() if os.environ.get("OCTOPUS_API_KEY") else None
    if not rates:
        return None
    imp, exp = rates.get("import") or {}, rates.get("export") or {}
    result = {"cheap_kwh": buckets["cheap_kwh"], "peak_kwh": buckets["peak_kwh"]}
    if None not in (imp.get("cheap"), imp.get("peak"), imp.get("standing")):
        result["standing_p"] = round(imp["standing"], 2)
        result["import_gbp"] = round((buckets["cheap_kwh"] * imp["cheap"]
                                      + buckets["peak_kwh"] * imp["peak"]
                                      + imp["standing"]) / 100, 2)
    export_rate = exp.get("peak") if exp.get("peak") is not None else exp.get("cheap")
    if export_rate is not None:
        result["export_gbp"] = round(export_kwh * export_rate / 100, 2)
    return result if ("import_gbp" in result or "export_gbp" in result) else None


# A failed forecast fetch must not be retried every sweep: with the network
# down each attempt blocks the poller (and the write lock) for the full HTTP
# timeout. Back off instead, serving the last good answer - but only same-day,
# because a value cached before midnight names the wrong "tomorrow" (the
# _weather_memo lesson in solar_forecast.py).
FORECAST_RETRY_SECONDS = 300
_forecast_memo = {"value": None, "failed_at": 0.0}


def _forecast():
    """Tomorrow's estimate and verdict, or None. Never fatal: it is advisory."""
    value = _forecast_memo["value"]
    cached = value if value and value.get("today_date") == date.today().isoformat() else None
    if time.time() - _forecast_memo["failed_at"] < FORECAST_RETRY_SECONDS:
        return cached or {"kwh": None, "today_kwh": None, "band": "unknown",
                          "text": "forecast unavailable - retrying in a few minutes"}
    try:
        tomorrow = (date.today() + timedelta(days=1)).isoformat()
        kwh = solar_forecast.tomorrow_kwh()
        band, text = solar_forecast.verdict(round(kwh, 1) if kwh is not None else None)
        # The headline already carries the kWh figure; the CLI keeps the prefix.
        text = text.split(": ", 1)[-1]
        today = solar_forecast.today_kwh()
        fresh = {"kwh": round(kwh, 1) if kwh is not None else None, "band": band,
                 "text": text, "date": tomorrow,
                 "today_kwh": round(today, 1) if today is not None else None,
                 "today_date": date.today().isoformat(),
                 "hourly_kw": solar_forecast.hourly_kw(tomorrow),
                 "today_hourly_kw": solar_forecast.hourly_kw(date.today().isoformat()),
                 "weather": solar_forecast.tomorrow_weather()}
        _forecast_memo["value"] = fresh
        _forecast_memo["failed_at"] = 0.0
        return fresh
    except Exception as exc:
        _forecast_memo["failed_at"] = time.time()
        return cached or {"kwh": None, "today_kwh": None, "band": "unknown",
                          "text": f"forecast unavailable: {exc}"}


def _car():
    """Octopus car-charging status, or None when no key is configured.

    octopus.state() enforces its own 3-minute poll floor and caches failures,
    so calling it every sweep is safe. None (no credentials in the environment)
    hides the row entirely; an error keeps the row with describe()'s
    "unavailable" wording, because a configured-but-broken lookup is worth
    seeing while a never-configured one is noise.
    """
    if not os.environ.get("OCTOPUS_API_KEY"):
        return None
    current = octopus.state()

    # Dispatch spans for the 24 h timeline, as wall-clock minutes so they draw
    # on the same recurring-day track as the charge windows (wrapping midnight
    # the same way). Only the active dispatch and slots starting inside the
    # next 24 h are included - anything further out would draw on today's
    # track as if it were today, which is a lie; the tooltip carries the real
    # day. Epoch comparisons, not datetimes, for the DST-fold reason
    # documented in octopus.py.
    now = datetime.now(octopus.TZ)
    horizon = now.timestamp() + 24 * 3600
    dispatches = ([current["current"]] if current["current"] else []) + current["planned"]
    spans, seen = [], set()
    for dispatch in dispatches:
        start, end = dispatch["start"], dispatch["end"]
        if end is None or end.timestamp() < now.timestamp() or start.timestamp() > horizon:
            continue
        # The active dispatch can appear twice: once as current (bracketed from
        # the completed list) and again in planned, which is unfiltered.
        key = (start.timestamp(), end.timestamp())
        if key in seen:
            continue
        seen.add(key)
        kwh = dispatch["kwh"]
        label = f"car {start:%a %H:%M}–{end:%H:%M}"
        if kwh:
            # Octopus reports dispatch energy as a signed delta, negative for
            # energy into the car - the magnitude is the readable part.
            label += f" {round(abs(kwh), 1):g} kWh"
        spans.append({"start_minutes": start.hour * 60 + start.minute,
                      "end_minutes": end.hour * 60 + end.minute,
                      "label": label,
                      "outside": dispatch["outside_window"]})

    # ---- structured fields for the Car tile (the timeline row keeps prose).
    rates = octopus.tariff()
    cheap = ((rates or {}).get("import") or {}).get("cheap")

    active = current["current"]
    until = f"{active['end']:%H:%M}" if active and active["end"] else None
    active_kwh = round(abs(active["kwh"]), 1) if active and active["kwh"] else None

    upcoming = [d for d in current["planned"]
                if d["start"].timestamp() > now.timestamp()]
    nxt = None
    if upcoming:
        start, final = upcoming[0]["start"], upcoming[-1]
        days = (start.date() - now.date()).days
        day = (("tonight" if start.hour >= 18 else "today") if days <= 0
               else "tomorrow" if days == 1 else start.strftime("%a"))
        nxt = {"start": f"{start:%H:%M}", "day": day,
               "end": f"{final['end']:%H:%M}" if final["end"] else None,
               "slots": len(upcoming)}
    kwhs = [abs(d["kwh"]) for d in upcoming if d["kwh"]]
    planned_kwh = round(sum(kwhs), 1) if kwhs else None
    # Prices the plan at the off-peak rate (Intelligent Go bills dispatches
    # cheap even outside the window). An estimate: slots get cancelled/resized.
    planned_gbp = (round(planned_kwh * cheap / 100, 2)
                   if planned_kwh and cheap is not None else None)

    # The tile headline: the SmartFlexDeviceState enum, folded to a plug
    # word. CAPABLE means plugged in awaiting control and NOT_AVAILABLE means
    # the car is unplugged or away - the community mapping (HA integration),
    # observed consistent live 2026-08-22, but not documented by Octopus.
    state_text = str(current["current_state"] or "").upper()
    plug = ("charging" if current["charging_now"]
            else "plugged in" if ("CAPABLE" in state_text
                                  or "IN_PROGRESS" in state_text
                                  or "BOOSTING" in state_text)
            else "unplugged" if "NOT_AVAILABLE" in state_text
            else "no link" if "LOST_CONNECTION" in state_text
            else "unknown")  # setup/auth/test states; rare and worth noticing

    prefs = octopus.preferences()
    target = None
    if prefs:
        # The ready-by moment is the coming morning, so weekday/weekend
        # follows tomorrow's date, not today's.
        weekend = (now.date() + timedelta(days=1)).weekday() >= 5
        soc = prefs.get("weekendTargetSoc" if weekend else "weekdayTargetSoc")
        ready = prefs.get("weekendTargetTime" if weekend else "weekdayTargetTime")
        if soc is not None and ready:
            target = {"soc": soc, "time": ready}

    return {"ok": current["ok"],
            "text": octopus.describe(current),
            "charging": bool(current["charging_now"]),
            "daytime": bool(current["daytime_dispatch"]),
            "spans": spans,
            "until": until,
            "active_kwh": active_kwh,
            "next": nxt,
            "planned_kwh": planned_kwh,
            "planned_gbp": planned_gbp,
            "plug": plug,
            "target": target}


def _refresh():
    """Re-read everything into _state. Caller holds _lock."""
    global _state
    try:
        _state = _read_all(_session())
    except (Exception, SystemExit) as exc:
        # SystemExit is not an Exception: solis_net.resolve_host raises it when
        # nothing is reachable, and uncaught it kills the poller thread - the
        # page then sits "stale" forever with no reconnect.
        _drop_session()
        _state = {
            "ok": False,
            "error": f"{type(exc).__name__}: {exc}",
            "host": _state.get("host"),
            "read_at": datetime.now().strftime("%H:%M:%S"),
            "read_at_epoch": time.time(),
        }


def _poller():
    while True:
        with _lock:
            _refresh()
        time.sleep(POLL_SECONDS)


@app.route("/api/state")
def api_state():
    return jsonify(_state)


@app.route("/api/health")
def api_health():
    """Liveness for Docker and Home Assistant: 200 while sweeps are fresh."""
    age = time.time() - (_state.get("read_at_epoch") or 0)
    fresh = bool(_state.get("ok")) and age < 3 * POLL_SECONDS
    return jsonify({"ok": fresh, "age_seconds": round(age, 1),
                    "error": _state.get("error")}), (200 if fresh else 503)


def serve():
    """Start the poller and serve. Callers may register extra routes first."""
    threading.Thread(target=_poller, daemon=True).start()
    app.run(host=HOST, port=PORT)


if __name__ == "__main__":
    print(f"Solis read-only API on http://{HOST}:{PORT}/api/state")
    serve()
