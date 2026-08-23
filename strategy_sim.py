"""Day simulator: which battery strategy is cheapest for a given amount of sun?

  uv run --no-project python strategy_sim.py                 # default sweep
  uv run --no-project python strategy_sim.py --solar 10 30 45 --n 500
  uv run --no-project python strategy_sim.py --ac 0 --car-day 20 --seed 3

Half-hourly, one day from 05:30 to 05:30 so the off-peak window (23:30-05:30)
is a single block at the end and the battery's state at 05:30 is the thing each
strategy is judged on. Cost = peak import x 30.46p + cheap import x 6.9p -
export x 12p, standing charge 54.46p included. Loads are drawn at
random - base, cooking, AC compressor cycling, washing machine, dishwasher - and
the same draws are replayed for every strategy, so differences between columns
are strategy, not luck. Stdlib only; nothing here touches the inverter.

Strategies (all self-use, 43110 bit0):
  full      charge window on at the fill rate, no daytime limit - today's setup
  none      no charge window at all
  taper N   full overnight, then daytime charge capped at TAPER_KW once SOC
            reaches N% (the 43117 rule: one write to taper, one to restore)
  stop N    same, but charging stops entirely at N%
  partial N overnight charge stops at N% so solar fills the rest
  late N    overnight charge to N%, no solar charging until LATE_HOUR, then
            LATE_KW so the room left is filled from the midday surplus an export
            cap would otherwise clip. Only makes sense with --export-limit; at
            3.68 kW it wins ~25p on a 50 kWh day and loses 12-23p on 30-40 kWh
            days, so it needs a forecast gate to break even. Two writes/day.
  hold      full, plus the charge window forced on for the length of a daytime
            Tesla dispatch so the pack is held (and topped up at 6.9p) instead
            of draining into the car. Two writes per dispatch.
  oracle    perfect-knowledge bound: charges from solar only what the evening
            will actually need. Not buildable; it shows how much is on the table.
"""
import argparse
import random

# --- hardware -----------------------------------------------------------------
PACK_KWH = 15.4           # three Fox LV5200, nameplate
SOC_MIN = 10              # over-discharge floor (owner-stated); below this nothing is served
BATT_KW = 5.0             # owner-stated ceiling either way
INVERTER_KW = 6.0         # AC ceiling; DC surplus above it can still charge the pack
EXPORT_KW = None          # grid export cap (G98 3.68, or a 5 kW G99 limit); None = no cap
PV_KWP = 8.0
EFF = 0.95                # one-way, ~90% round trip
TAPER_KW = 1.0            # ~20 A at 53 V
OVERNIGHT_KW = 2.65       # 50 A at 53 V, the applied 43141
LATE_HOUR = 11.0          # `late` strategy: solar charging released at this hour
LATE_KW = 1.0             # ...and at this rate, so it takes only the surplus above the cap

# --- tariff, pence ------------------------------------------------------------
CHEAP, PEAK, EXPORT, STANDING = 6.9, 30.46, 12.0, 54.46
WINDOW_START, WINDOW_END = 23.5, 5.5        # hours, local

STEP = 0.5
STEPS = 48
T0 = WINDOW_END                             # day starts at 05:30


def hour_of(i):
    return (T0 + i * STEP) % 24


def in_window(h):
    return h >= WINDOW_START or h < WINDOW_END


# --- solar ----------------------------------------------------------------------
def solar_profile(day_kwh, rng, cloud=0.0):
    """DC kW per step summing to day_kwh. Late-August daylight 06:00-20:30.

    A sin^1.5 bell, plus a SW-heavy skew because 12 of the 20 panels face
    207 deg - the afternoon is stronger than the morning. `cloud` adds random
    passing-cloud dips so 'the same kWh' can be a flat grey day or a broken one.
    """
    rise, sett = 6.0, 20.5
    shape = []
    for i in range(STEPS):
        h = hour_of(i)
        if rise <= h <= sett:
            x = (h - rise) / (sett - rise)
            v = max(0.0, (x * (1 - x)) ** 1.5 * 6.35) * (0.85 + 0.3 * x)
            if cloud and rng.random() < cloud:
                v *= rng.uniform(0.2, 0.7)
            shape.append(v)
        else:
            shape.append(0.0)
    total = sum(shape) * STEP
    scale = day_kwh / total if total else 0.0
    return [min(v * scale, PV_KWP) for v in shape]


# --- load ---------------------------------------------------------------------
def load_profile(rng, scale=1.0, ac_hours=6.0, car_day_kwh=0.0, car_night_kwh=10.0):
    """AC kW per step. Returns (house, car) so the car can be billed separately."""
    house = [0.0] * STEPS
    car = [0.0] * STEPS
    for i in range(STEPS):
        h = hour_of(i)
        base = 0.25 if (h < 7 or h >= 23) else 0.4
        base *= rng.uniform(0.8, 1.25)
        if 17.5 <= h < 20.5:
            base += rng.uniform(0.3, 1.2)        # cooking, TV, lights
        house[i] = base
    # AC: compressor ~1.25 kW at a duty cycle through the hot part of the day,
    # 60 W standby otherwise. ac_hours is how long the room is being cooled.
    if ac_hours > 0:
        start = rng.uniform(12.0, 15.0)
        for i in range(STEPS):
            h = hour_of(i)
            on = start <= h < start + ac_hours
            house[i] += 1.25 * rng.uniform(0.3, 0.55) if on else 0.06
    # Washing machine: 2 kW heat for 30 min then 0.3 kW for an hour.
    if rng.random() < 0.8:
        s = int((rng.uniform(8, 20) - T0) / STEP)
        house[s] += 2.0
        for k in (1, 2):
            house[(s + k) % STEPS] += 0.3
    # Dishwasher: 1.2 kWh over 90 min, usually evening.
    if rng.random() < 0.7:
        s = int((rng.uniform(19, 22) - T0) / STEP)
        for k, w in enumerate((1.6, 0.6, 0.2)):
            house[(s + k) % STEPS] += w
    house = [v * scale for v in house]
    # Car: overnight dispatch at 7 kW inside the window; an optional daytime
    # dispatch (Octopus bills it at the cheap rate, but it lands on the house).
    if car_night_kwh > 0:
        s = int((rng.uniform(0, 2.5) + 24 - T0) / STEP)
        for k in range(int(car_night_kwh / 7.0 / STEP) + 1):
            car[(s + k) % STEPS] += 7.0
    if car_day_kwh > 0:
        s = int((rng.uniform(10, 16) - T0) / STEP)
        for k in range(int(car_day_kwh / 7.0 / STEP) + 1):
            car[(s + k) % STEPS] += 7.0
    return house, car


# --- battery + dispatch ---------------------------------------------------------
def simulate(pv, house, car, strategy, soc0=100.0, trace=None):
    """Run one day. Returns dict of kWh totals, pence cost, writes, SOC at 05:30.

    Pass a list as `trace` to collect one dict per half-hour step (kW flows and
    SOC) - what the day-profile charts are drawn from."""
    kind, arg = strategy
    kwh = PACK_KWH * soc0 / 100
    tapered = False
    writes = 0
    tot = {"peak": 0.0, "cheap": 0.0, "export": 0.0, "clip": 0.0}
    future_need = _future_need(pv, house) if kind == "oracle" else None
    for i in range(STEPS):
        h = hour_of(i)
        soc = kwh / PACK_KWH * 100
        room = (PACK_KWH - kwh) / EFF
        avail = max(0.0, kwh - PACK_KWH * SOC_MIN / 100) * EFF
        load = house[i] + car[i]
        window = in_window(h) and kind != "none"
        if kind == "hold" and car[i] > 0:
            window = True
        charge = discharge = grid = export = 0.0
        if window:
            # Timed charge: grid fills the pack at the applied rate, house and
            # car ride on the grid. Battery never discharges in the window.
            limit = OVERNIGHT_KW
            if kind in ("partial", "late") and soc >= arg:
                limit = 0.0
            charge = min(limit, room / STEP, BATT_KW)
            grid = load + charge - pv[i]
            if grid < 0:
                export, grid = -grid, 0.0
        else:
            surplus = pv[i] - load
            if surplus >= 0:
                limit = BATT_KW
                if kind in ("taper", "stop"):
                    if not tapered and soc >= arg:
                        tapered = True
                        writes += 1
                    if tapered:
                        limit = TAPER_KW if kind == "taper" else 0.0
                elif kind == "late":
                    limit = 0.0 if h < LATE_HOUR else LATE_KW
                elif kind == "oracle":
                    if avail >= future_need[i]:
                        limit = 0.0
                charge = min(surplus, limit, room / STEP)
                if kind == "oracle" and EXPORT_KW is not None:
                    # The bound may bank what the export cap would otherwise
                    # throw away; a 43117 rule cannot, so taper/stop do not.
                    charge = max(charge, min(surplus - EXPORT_KW, BATT_KW, room / STEP))
                ac = min(pv[i] - charge, INVERTER_KW)
                export = max(0.0, ac - load)
                if EXPORT_KW is not None:
                    export = min(export, EXPORT_KW)
                tot["clip"] += (pv[i] - charge - load - export) * STEP if pv[i] - charge > load else 0.0
            else:
                discharge = min(-surplus, BATT_KW, avail / STEP)
                grid = -surplus - discharge
        kwh += charge * STEP * EFF - discharge * STEP / EFF
        if trace is not None:
            trace.append({"h": h, "pv": pv[i], "load": load, "charge": charge,
                          "discharge": discharge, "grid": grid, "export": export,
                          "soc": kwh / PACK_KWH * 100})
        # Octopus Intelligent bills the whole house at cheap rate during a car
        # dispatch, day or night - so grid in a car step is cheap import.
        cheap_step = in_window(h) or car[i] > 0
        tot["cheap" if cheap_step else "peak"] += grid * STEP
        tot["export"] += export * STEP
    if tapered:
        writes += 1            # the restore before 23:30
    pence = tot["peak"] * PEAK + tot["cheap"] * CHEAP - tot["export"] * EXPORT + STANDING
    return {**tot, "pence": pence, "writes": writes, "soc_end": kwh / PACK_KWH * 100}


def _future_need(pv, house):
    """For each step, battery kWh the house will draw before 23:30 (oracle only)."""
    need = [0.0] * STEPS
    acc = 0.0
    for i in range(STEPS - 1, -1, -1):
        if not in_window(hour_of(i)):
            acc += max(0.0, min(house[i] - pv[i], BATT_KW)) * STEP
        else:
            acc = 0.0
        need[i] = acc
    return need


# --- sweep ----------------------------------------------------------------------
BURN_IN = 3   # days simulated before the one that is scored


def chained(sun, k, strategy, a, draws):
    """Score day k after BURN_IN days of the same strategy, so the battery
    starts where the strategy itself left it - scoring one day from a fixed
    100% gave 'no charge window' a free full pack every morning."""
    soc = a.soc0
    r = None
    for d in range(BURN_IN + 1):
        j = (k + d) % len(draws)
        house, car = draws[j][1]
        pv = solar_profile(sun, random.Random(a.seed * 7 + j), a.cloud)
        r = simulate(pv, house, car, strategy, soc)
        soc = r["soc_end"]
    return r


STRATEGIES = [
    ("full", None), ("none", None),
    ("taper", 80), ("taper", 90), ("stop", 80), ("stop", 90),
    ("partial", 60), ("partial", 80), ("late", 40), ("late", 60), ("hold", None), ("oracle", None),
]


def label(s):
    return s[0] if s[1] is None else f"{s[0]} {s[1]}"


def main():
    p = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    p.add_argument("--solar", type=float, nargs="+", default=[5, 10, 15, 20, 25, 30, 35, 40, 45])
    p.add_argument("--n", type=int, default=200, help="load draws per cell")
    p.add_argument("--load-scale", type=float, default=1.0)
    p.add_argument("--ac", type=float, default=6.0, help="hours of AC per day")
    p.add_argument("--car-day", type=float, default=0.0, help="daytime car kWh")
    p.add_argument("--car-night", type=float, default=10.0)
    p.add_argument("--cloud", type=float, default=0.0, help="0-1 passing-cloud chance per step")
    p.add_argument("--soc0", type=float, default=50.0, help="SOC before the burn-in days")
    p.add_argument("--export-limit", type=float, default=None, help="kW export cap, e.g. 3.68 or 5")
    p.add_argument("--seed", type=int, default=1)
    a = p.parse_args()
    global EXPORT_KW
    EXPORT_KW = a.export_limit

    draws = []
    for k in range(a.n):
        rng = random.Random(a.seed * 100003 + k)
        draws.append((rng, load_profile(rng, a.load_scale, a.ac, a.car_day, a.car_night)))
    house_kwh = sum(sum(h) * STEP for _, (h, _) in draws) / a.n
    print(f"house {house_kwh:.1f} kWh/day avg, car night {a.car_night} + day {a.car_day} kWh, "
          f"AC {a.ac} h, {a.n} draws, each scored after {BURN_IN} burn-in days, "
          f"export cap {a.export_limit or 'none'} kW")
    print("£/day (peak + cheap import - export + standing). Lower is better.\n")
    head = f"{'sun kWh':>8} " + " ".join(f"{label(s):>11}" for s in STRATEGIES)
    print(head)
    for sun in a.solar:
        row = []
        for s in STRATEGIES:
            pence = 0.0
            for k in range(a.n):
                pence += chained(sun, k, s, a, draws)["pence"]
            row.append(pence / a.n / 100)
        best = min(row)
        cells = " ".join(f"{v:>10.2f}{'*' if v == best else ' '}" for v in row)
        print(f"{sun:>8.0f} {cells}")
    print("\n* cheapest in that row.  taper/stop = 2 register writes on the day they fire.")
    _detail(a, draws)


def _detail(a, draws):
    """One worked day at 30 kWh so the kWh flows behind the £ are visible."""
    print("\nWorked day, 30 kWh of sun, kWh:")
    print(f"{'strategy':>11} {'peak in':>8} {'cheap in':>9} {'export':>7} {'clip':>5} {'SOC 05:30':>10} {'£':>6}")
    for s in STRATEGIES:
        acc = {"peak": 0, "cheap": 0, "export": 0, "clip": 0, "soc_end": 0, "pence": 0}
        for k in range(a.n):
            r = chained(30, k, s, a, draws)
            for key in acc:
                acc[key] += r[key] / a.n
        print(f"{label(s):>11} {acc['peak']:>8.1f} {acc['cheap']:>9.1f} {acc['export']:>7.1f} "
              f"{acc['clip']:>5.1f} {acc['soc_end']:>9.0f}% {acc['pence'] / 100:>6.2f}")


if __name__ == "__main__":
    main()
