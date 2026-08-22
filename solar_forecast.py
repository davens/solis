"""Rough daily solar estimate, for deciding how hard to charge overnight.

The only question this answers: will tomorrow's sun refill the battery on its
own, or should the off-peak window do it? One number, one verdict.

The lever is the charge RATE (43141), not the window: 23:30-05:30 is the cheap
tariff and the window also holds the battery against the Tesla. See CLAUDE.md.

  uv run --no-project python solar_forecast.py                # today + tomorrow
  uv run --no-project python solar_forecast.py record 2026-08-20 32
  uv run --no-project python solar_forecast.py calibrate      # refit from actuals

Source is Open-Meteo's tilted-irradiance forecast, which is free, needs no key,
and - unlike forecast.solar - also serves past days, so the model can be refit
against what the roof actually produced. forecast.solar was tried first and
low-balled badly: it never showed the 6 kW peaks this array really hits.

Irradiance is the MEAN of five weather models, not one. Do not "simplify" this
back to a single model or to Open-Meteo's default best_match: over seven
recorded actuals the ensemble scores RMSE 2.19 kWh where the best single model
manages 3.26 and the worst 5.63. Which model is worst keeps moving - ECMWF read
2026-08-20 far better than UKMO (5.90 kWh/m2 against 3.65) yet is the weakest of
the five across the set as a whole - so there is no one model to promote, which
is the argument for averaging. best_match resolves to UKMO at this latitude.

EFFECTIVE_KWP is fitted, and at 8.0 kWp nameplate (20 x 400 W) it implies a
performance ratio around 0.79 - which is what a real system does, a good sign
the model is measuring the roof rather than absorbing weather error. A pvlib
Perez transposition was also tried and scored no better than Open-Meteo's own
tilted irradiance, so that dependency was dropped.

Remaining error is weather, not fit. Good for a threshold decision, rough as a
yield figure; verdict() declines to call days within UNCERTAINTY_KWH of the
line rather than guessing.
"""
import json
import os
import sys
import time
import urllib.request
from datetime import date, timedelta

# Site. From REDACTED  via api.postcodes.io.
LAT, LON = REDACTED_LAT, REDACTED_LON

# Arrays: (name, azimuth, tilt, share of panel count). Azimuth is Open-Meteo's
# convention - 0 = south, -90 = east, 90 = west - confirmed empirically, not
# assumed. Geometry is from the owner's roof survey (2026-08-21), which gives
# compass aspects; azimuth = compass - 180. Both planes face further west than
# the earlier guess of -15/-100, and they have different pitches, so tilt is
# per-plane rather than one roof-wide constant.
PLANES = [
    ("roof 1", 27, 21, 12 / 20),   # 12 x 400 W, 207 deg SW, 21 deg pitch, 25.1 m2
    ("roof 2", -63, 24, 8 / 20),   # 8 x 400 W, 117 deg SE, 24 deg pitch, 18 m2
]

# Weather models averaged for every estimate. See the note above before changing.
MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "ukmo_seamless",
          "meteofrance_seamless"]

# Fitted against the actuals below; see calibrate().
EFFECTIVE_KWP = 6.33
INVERTER_W = 6000

# Below this many kWh, tomorrow's sun will not refill the battery by itself.
# Raised 20 -> 25 by the owner on 2026-08-21, after the pack turned out to be
# ~15.4 kWh rather than the 5.1 this repo had assumed. 20 was roughly 5 kWh of
# battery plus a day's house load; the battery half of that is three times
# bigger, so the line had to move with it. Still untested at the low end - no
# genuinely dull day has been recorded yet.
LOW_KWH = 25.0
# Observed RMSE against recorded actuals. Estimates this close to the threshold
# are not worth acting on; verdict() calls them borderline rather than guessing.
UNCERTAINTY_KWH = 2.0

HERE = os.path.dirname(os.path.abspath(__file__))
ACTUALS_FILE = os.path.join(HERE, "solar_actuals.json")
CACHE_FILE = os.path.join(HERE, ".solar_cache.json")
CACHE_SECONDS = 3600

FORECAST_API = "https://api.open-meteo.com/v1/forecast"
HISTORY_API = "https://historical-forecast-api.open-meteo.com/v1/forecast"

_memo = None


def _gti(base, azimuth, tilt, extra):
    """{model: {timestamp: W/m2}} of global tilted irradiance for one plane."""
    url = (f"{base}?latitude={LAT}&longitude={LON}&hourly=global_tilted_irradiance"
           f"&tilt={tilt}&azimuth={azimuth}&timezone=Europe%2FLondon"
           f"&models={','.join(MODELS)}&{extra}")
    with urllib.request.urlopen(url, timeout=15) as response:
        hourly = json.load(response)["hourly"]
    out = {}
    for model in MODELS:
        key = f"global_tilted_irradiance_{model}"
        if key in hourly:
            out[model] = {t: (v or 0) for t, v in zip(hourly["time"], hourly[key])}
    return out


def _series(history_from=None):
    """{model: {day: [hourly W/m2, ...]}}, irradiance weighted across the planes."""
    global _memo
    key = "v2|" + (history_from or "") + "|" + repr(PLANES)
    if _memo and _memo["key"] == key and time.time() - _memo["at"] < CACHE_SECONDS:
        return _memo["series"]
    if not history_from and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE) as handle:
                cached = json.load(handle)
            if cached["key"] == key and time.time() - cached["at"] < CACHE_SECONDS:
                _memo = cached
                return cached["series"]
        except (ValueError, KeyError):
            pass  # a corrupt cache is not worth reporting; just refetch

    stamps = {}
    for _, azimuth, tilt, share in PLANES:
        sources = [(FORECAST_API, "forecast_days=3")]
        if history_from:
            yesterday = (date.today() - timedelta(days=1)).isoformat()
            sources.append((HISTORY_API, f"start_date={history_from}&end_date={yesterday}"))
        for base, extra in sources:
            for model, readings in _gti(base, azimuth, tilt, extra).items():
                for stamp, watts in readings.items():
                    per_model = stamps.setdefault(model, {})
                    per_model[stamp] = per_model.get(stamp, 0) + watts * share

    # Sorted, so each day's list is 24 chronological hours and index == hour,
    # which is what hourly_kw() indexes by. day_kwh() would not care.
    series = {}
    for model, readings in stamps.items():
        for stamp, watts in sorted(readings.items()):
            series.setdefault(model, {}).setdefault(stamp[:10], []).append(watts)

    _memo = {"key": key, "at": time.time(), "series": series}
    if not history_from:
        with open(CACHE_FILE, "w") as handle:
            json.dump(_memo, handle)
    return series


def day_kwh(day, kwp=None, series=None):
    """Estimated kWh for one ISO date: the mean across models that reported it.

    Hourly irradiance integrates directly, so each hour contributes its own kW.
    A model with no data for a day totals zero and is dropped rather than
    averaged in as darkness - Open-Meteo pads short horizons with zeroes.
    """
    kwp = EFFECTIVE_KWP if kwp is None else kwp
    series = _series() if series is None else series
    ceiling = INVERTER_W / 1000
    totals = []
    for days in series.values():
        hours = days.get(day) or ()
        # A short horizon is missing data, not a dark afternoon. hourly_kw()
        # already drops these; day_kwh() averaged them in and pulled the daily
        # total down, so the two disagreed about the same models.
        if len(hours) != 24:
            continue
        total = sum(min(watts * kwp / 1000, ceiling) for watts in hours)
        if total > 0:
            totals.append(total)
    return sum(totals) / len(totals) if totals else 0.0


def hourly_kw(day):
    """Estimated AC kW for each hour of one ISO date, or [] if nothing reported.

    Same mean-across-models rule as day_kwh, applied hour by hour, so the hours
    sum back to the daily figure. Partial days are dropped: a model whose
    horizon stops mid-day would otherwise pull the afternoon down.
    """
    ceiling = INVERTER_W / 1000
    curves = []
    for days in _series().values():
        hours = days.get(day) or []
        if len(hours) == 24 and sum(hours) > 0:
            curves.append([min(w * EFFECTIVE_KWP / 1000, ceiling) for w in hours])
    if not curves:
        return []
    return [round(sum(c[h] for c in curves) / len(curves), 2) for h in range(24)]


def today_kwh():
    """Estimated kWh for the whole of today, or None if no model reported it."""
    total = day_kwh(date.today().isoformat())
    return total if total > 0 else None


def tomorrow_kwh():
    """Estimated kWh for tomorrow, or None if the forecast does not reach it."""
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    total = day_kwh(tomorrow)
    return total if total > 0 else None


def verdict(kwh):
    """Turn an estimate into what to do with tonight's charge window."""
    if kwh is None:
        return "unknown", "no forecast - leave the charge window as it is"
    if kwh < LOW_KWH - UNCERTAINTY_KWH:
        return "low", (f"{kwh:.0f} kWh: sun alone will not carry the day - "
                       "hold the overnight rate up so the battery starts full")
    if kwh > LOW_KWH + UNCERTAINTY_KWH:
        return "high", (f"{kwh:.0f} kWh: solar should refill the battery during the day - "
                        "leaves room to lower the overnight rate")
    return "borderline", (f"{kwh:.0f} kWh: within +/-{UNCERTAINTY_KWH:.0f} kWh of the "
                          f"{LOW_KWH:.0f} kWh line - too close to call, leave the rate alone")


# WMO weather codes, condensed to what is worth showing on a dashboard.
WEATHER_CODES = {
    0: "clear", 1: "mainly clear", 2: "partly cloudy", 3: "overcast",
    45: "fog", 48: "freezing fog", 51: "light drizzle", 53: "drizzle",
    55: "heavy drizzle", 56: "freezing drizzle", 57: "freezing drizzle",
    61: "light rain", 63: "rain", 65: "heavy rain", 66: "freezing rain",
    67: "freezing rain", 71: "light snow", 73: "snow", 75: "heavy snow",
    77: "snow grains", 80: "light showers", 81: "showers", 82: "heavy showers",
    85: "snow showers", 86: "snow showers", 95: "thunderstorm",
    96: "thunderstorm with hail", 99: "thunderstorm with hail",
}

_weather_memo = None


def _sky(code, cloud):
    """Describe the day in a way that agrees with the irradiance estimate.

    Open-Meteo's daily weather_code is a worst-of-day aggregate, so it will say
    "overcast" for a day that is mostly sunny with one dull hour - which reads
    as a contradiction next to a high kWh number. Precipitation is worth
    reporting from the code; otherwise mean cloud describes the sky better.
    """
    if code is not None and code >= 45:
        return WEATHER_CODES.get(code, "unsettled")
    if cloud is None:
        return WEATHER_CODES.get(code, "unsettled")
    for limit, text in ((15, "clear"), (40, "mostly sunny"),
                        (70, "partly cloudy"), (90, "mostly cloudy")):
        if cloud < limit:
            return text
    return "overcast"


def _hourly_weather(hourly, day):
    """Tomorrow's 24 hours of cloud, temperature and rain chance, or None."""
    if not hourly:
        return None
    rows = [i for i, stamp in enumerate(hourly.get("time", [])) if stamp[:10] == day]
    if len(rows) != 24:
        return None

    def column(name):
        values = hourly.get(name)
        if values is None:
            return None
        return [values[i] if i < len(values) and values[i] is not None else 0 for i in rows]

    columns = {"cloud": column("cloud_cover"), "temp": column("temperature_2m"),
               "rain": column("precipitation_probability"),
               "code": column("weather_code")}
    # An absent field is missing data, not a clear sky: refuse the lot so the
    # caller falls back to the daily fields rather than inventing a forecast.
    if any(values is None for values in columns.values()):
        return None
    return columns


def _daylight_sky(hourly, sunrise, sunset):
    """Describe only the hours the sun is up, or None if that is not possible.

    The daily weather_code covers midnight to midnight, so an overnight shower
    names the whole day - the dashboard would read "light drizzle" above four
    dry daylight blocks. Restricting to sunrise..sunset is the fix; it is also
    the only span that matters for generation.
    """
    if not hourly:
        return None

    def hour(text):
        # "2026-08-22T06:05" today, but take the hour from the front of the time
        # part rather than the end of the string, so a seconds field or a UTC
        # offset cannot shift it by an hour.
        try:
            return int(str(text).split("T")[-1][:2])
        except (ValueError, IndexError):
            return None

    first, last = hour(sunrise), hour(sunset)
    if first is None or last is None or not 0 <= first < last <= 23:
        return None
    span = range(first, last + 1)
    return _sky(max(hourly["code"][h] for h in span),
                sum(hourly["cloud"][h] for h in span) / len(span))


def tomorrow_weather():
    """Plain forecast for tomorrow, or None if the lookup fails.

    Deliberately uses Open-Meteo's default model rather than the MODELS
    ensemble: this is descriptive text and a temperature, not the irradiance
    the estimate depends on.
    """
    global _weather_memo
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    # Keyed on the target date, not just age: a memo built at 23:55 is still
    # young at 00:05 but is now answering for the wrong day.
    if (_weather_memo and _weather_memo["day"] == tomorrow
            and time.time() - _weather_memo["at"] < CACHE_SECONDS):
        return _weather_memo["weather"]
    url = (f"{FORECAST_API}?latitude={LAT}&longitude={LON}&timezone=Europe%2FLondon"
           "&forecast_days=2&daily=weather_code,cloud_cover_mean,sunrise,sunset"
           "&hourly=cloud_cover,temperature_2m,precipitation_probability,weather_code")
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.load(response)
        daily = payload["daily"]
        i = daily["time"].index(tomorrow)
    except Exception:
        return None

    def field(name, row=i):
        values = daily.get(name) or []
        return values[row] if row < len(values) else None

    try:
        today_row = daily["time"].index(date.today().isoformat())
    except ValueError:
        today_row = None

    hourly = _hourly_weather(payload.get("hourly"), tomorrow)
    daylight = _daylight_sky(hourly, field("sunrise"), field("sunset"))
    today_hourly = (None if today_row is None
                    else _hourly_weather(payload.get("hourly"), date.today().isoformat()))
    today_daylight = (None if today_row is None else _daylight_sky(
        today_hourly, field("sunrise", today_row), field("sunset", today_row)))
    weather = {
        "summary": daylight or _sky(field("weather_code"), field("cloud_cover_mean")),
        "sunrise": (field("sunrise") or "")[-5:],
        "sunset": (field("sunset") or "")[-5:],
        "hourly": hourly,
        "today": None if today_row is None else {
            "summary": today_daylight or _sky(field("weather_code", today_row),
                                              field("cloud_cover_mean", today_row)),
            "sunrise": (field("sunrise", today_row) or "")[-5:],
            "sunset": (field("sunset", today_row) or "")[-5:],
            "hourly": today_hourly,
        },
    }
    _weather_memo = {"at": time.time(), "day": tomorrow, "weather": weather}
    return weather


def load_actuals():
    if not os.path.exists(ACTUALS_FILE):
        return {}
    with open(ACTUALS_FILE) as handle:
        return json.load(handle)


def record(day, kwh):
    actuals = load_actuals()
    actuals[day] = float(kwh)
    with open(ACTUALS_FILE, "w") as handle:
        json.dump(actuals, handle, indent=2, sort_keys=True)
    print(f"recorded {day} = {kwh} kWh ({len(actuals)} actuals on file)")


def calibrate():
    """Refit EFFECTIVE_KWP against every recorded actual."""
    actuals = load_actuals()
    if not actuals:
        raise SystemExit(f"no actuals yet - record some first ({ACTUALS_FILE})")
    series = _series(history_from=min(actuals))

    def error(kwp):
        return sum((day_kwh(day, kwp, series) - actual) ** 2 for day, actual in actuals.items())

    best = min((error(c / 100), c / 100) for c in range(300, 2001))[1]
    print(f"{len(actuals)} actuals, fitted EFFECTIVE_KWP = {best:.2f}"
          f"  (currently {EFFECTIVE_KWP})\n")
    for day, actual in sorted(actuals.items()):
        modelled = day_kwh(day, best, series)
        print(f"  {day}  modelled {modelled:5.1f}  actual {actual:5.1f}  ({modelled - actual:+.1f})")
    if abs(best - EFFECTIVE_KWP) > 0.05:
        print(f"\nedit EFFECTIVE_KWP in {os.path.basename(__file__)} to {best:.2f}")


def main():
    args = sys.argv[1:]
    if args and args[0] == "record":
        if len(args) != 3:
            raise SystemExit("usage: solar_forecast.py record YYYY-MM-DD KWH")
        return record(args[1], args[2])
    if args and args[0] == "calibrate":
        return calibrate()
    if args:
        raise SystemExit(f"unknown command {args[0]!r}")

    for offset, name in ((0, "today"), (1, "tomorrow")):
        day = (date.today() + timedelta(days=offset)).isoformat()
        print(f"{day}  {day_kwh(day):5.1f} kWh  {name}")
    print()
    print(verdict(tomorrow_kwh())[1])


if __name__ == "__main__":
    main()
