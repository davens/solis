"""Solar forecast: will tomorrow's sun refill the battery on its own?

Vendored from the repo's solar_forecast.py (the calibration side stays there).
Irradiance is the MEAN of five weather models - do not "simplify" back to one
model or to Open-Meteo's best_match: over seven recorded actuals the ensemble
scores RMSE 2.19 kWh where the best single model manages 3.26 and the worst
5.63, and which model is worst keeps moving. EFFECTIVE_KWP is fitted against
recorded generation (PR ~0.79 of the 8.0 kWp nameplate); recalibration happens
in the repo, and a new fit ships here as a release.

Everything in this module is synchronous - call it via an executor job.
"""
import json
import time
import urllib.request
from datetime import date, datetime, time as dtime, timedelta
from zoneinfo import ZoneInfo

# Site .
LAT, LON = REDACTED_LAT, REDACTED_LON

# (azimuth, tilt, share): Open-Meteo azimuth, 0 = south, -90 = east, 90 = west.
# Geometry from the owner's roof survey - two planes, different pitches.
PLANES = [
    (27, 21, 12 / 20),   # 12 x 400 W, 207 deg SW, 21 deg pitch
    (-63, 24, 8 / 20),   # 8 x 400 W, 117 deg SE, 24 deg pitch
]

MODELS = ["ecmwf_ifs025", "gfs_seamless", "icon_seamless", "ukmo_seamless",
          "meteofrance_seamless"]

EFFECTIVE_KWP = 6.33
INVERTER_W = 6000

# Below this, tomorrow's sun will not refill the ~15.4 kWh pack by itself.
LOW_KWH = 25.0
UNCERTAINTY_KWH = 2.0

FORECAST_API = "https://api.open-meteo.com/v1/forecast"
CACHE_SECONDS = 3600

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

_memo = None
_weather_memo = None


def _gti(azimuth, tilt):
    """{model: {timestamp: W/m2}} of global tilted irradiance for one plane."""
    url = (f"{FORECAST_API}?latitude={LAT}&longitude={LON}"
           f"&hourly=global_tilted_irradiance&tilt={tilt}&azimuth={azimuth}"
           f"&timezone=Europe%2FLondon&models={','.join(MODELS)}&forecast_days=3")
    with urllib.request.urlopen(url, timeout=15) as response:
        hourly = json.load(response)["hourly"]
    out = {}
    for model in MODELS:
        key = f"global_tilted_irradiance_{model}"
        if key in hourly:
            out[model] = {t: (v or 0) for t, v in zip(hourly["time"], hourly[key])}
    return out


def _series():
    """{model: {day: [hourly W/m2, ...]}}, irradiance weighted across the planes."""
    global _memo
    if _memo and time.time() - _memo["at"] < CACHE_SECONDS:
        return _memo["series"]
    stamps = {}
    for azimuth, tilt, share in PLANES:
        for model, readings in _gti(azimuth, tilt).items():
            for stamp, watts in readings.items():
                per_model = stamps.setdefault(model, {})
                per_model[stamp] = per_model.get(stamp, 0) + watts * share
    series = {}
    for model, readings in stamps.items():
        for stamp, watts in sorted(readings.items()):
            series.setdefault(model, {}).setdefault(stamp[:10], []).append(watts)
    _memo = {"at": time.time(), "series": series}
    return series


def _day_kwh(day, series):
    """Mean kWh across the models that reported the whole day.

    A model with no data (or a partial horizon) is dropped, not averaged in as
    darkness - Open-Meteo pads short horizons with zeroes.
    """
    ceiling = INVERTER_W / 1000
    totals = []
    for days in series.values():
        hours = days.get(day) or ()
        if len(hours) != 24:
            continue
        total = sum(min(watts * EFFECTIVE_KWP / 1000, ceiling) for watts in hours)
        if total > 0:
            totals.append(total)
    return sum(totals) / len(totals) if totals else 0.0


def _wh_hours(series):
    """{tz-aware ISO hour: Wh} for today+tomorrow - the Energy dashboard's
    native forecast line (energy platform contract: interval Wh, not W).

    Same model as _day_kwh - per-hour mean across models that reported the
    whole day, clipped at the inverter ceiling - so the hours sum to the
    calibrated daily total. Peaks read ~20% low by construction (energy-fitted
    EFFECTIVE_KWP); that is accepted for an energy-per-hour line.
    """
    tz = ZoneInfo("Europe/London")
    out = {}
    for offset in (0, 1):
        day = date.today() + timedelta(days=offset)
        columns = []
        for days_map in series.values():
            hours = days_map.get(day.isoformat()) or ()
            if len(hours) != 24:
                continue
            kws = [min(watts * EFFECTIVE_KWP / 1000, INVERTER_W / 1000)
                   for watts in hours]
            if sum(kws) > 0:
                columns.append(kws)
        if not columns:
            continue
        for hour in range(24):
            wh = round(sum(c[hour] for c in columns) / len(columns) * 1000)
            if wh > 0:
                stamp = datetime.combine(day, dtime(hour=hour), tzinfo=tz)
                out[stamp.isoformat()] = wh
    return out


def _verdict(kwh):
    if kwh is None:
        return "unknown", "no forecast - leave the charge window as it is"
    if kwh < LOW_KWH - UNCERTAINTY_KWH:
        return "low", (f"{kwh:.0f} kWh: sun alone will not carry the day - "
                       "hold the overnight rate up so the battery starts full")
    if kwh > LOW_KWH + UNCERTAINTY_KWH:
        return "high", (f"{kwh:.0f} kWh: solar should refill the battery during "
                        "the day - leaves room to lower the overnight rate")
    return "borderline", (f"{kwh:.0f} kWh: within +/-{UNCERTAINTY_KWH:.0f} kWh of "
                          f"the {LOW_KWH:.0f} kWh line - too close to call, "
                          "leave the rate alone")


def _sky(code, cloud):
    """Precipitation from the code; otherwise mean cloud - the daily
    weather_code is a worst-of-day aggregate and reads "overcast" next to a
    high kWh number."""
    if code is not None and code >= 45:
        return WEATHER_CODES.get(code, "unsettled")
    if cloud is None:
        return WEATHER_CODES.get(code, "unsettled")
    for limit, text in ((15, "clear"), (40, "mostly sunny"),
                        (70, "partly cloudy"), (90, "mostly cloudy")):
        if cloud < limit:
            return text
    return "overcast"


def _daylight_sky(payload, day, sunrise, sunset):
    """Describe sunrise..sunset only, or None - an overnight shower must not
    name the whole day."""
    hourly = payload.get("hourly") or {}
    rows = [i for i, stamp in enumerate(hourly.get("time", []))
            if stamp[:10] == day]
    if len(rows) != 24:
        return None
    clouds = hourly.get("cloud_cover")
    codes = hourly.get("weather_code")
    if clouds is None or codes is None:
        return None

    def hour(text):
        try:
            return int(str(text).split("T")[-1][:2])
        except (ValueError, IndexError):
            return None

    first, last = hour(sunrise), hour(sunset)
    if first is None or last is None or not 0 <= first < last <= 23:
        return None
    span = [rows[h] for h in range(first, last + 1)]
    code = max(codes[i] or 0 for i in span)
    cloud = sum(clouds[i] or 0 for i in span) / len(span)
    return _sky(code, cloud)


def _tomorrow_weather():
    """{summary, sunrise, sunset} for tomorrow, or None. Keyed on the target
    date, not just age - a memo built at 23:55 is answering for the wrong day
    ten minutes later."""
    global _weather_memo
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    if (_weather_memo and _weather_memo["day"] == tomorrow
            and time.time() - _weather_memo["at"] < CACHE_SECONDS):
        return _weather_memo["weather"]
    url = (f"{FORECAST_API}?latitude={LAT}&longitude={LON}"
           "&timezone=Europe%2FLondon&forecast_days=2"
           "&daily=weather_code,cloud_cover_mean,sunrise,sunset"
           "&hourly=cloud_cover,weather_code")
    try:
        with urllib.request.urlopen(url, timeout=15) as response:
            payload = json.load(response)
        daily = payload["daily"]
        i = daily["time"].index(tomorrow)
    except Exception:
        return None

    def field(name):
        values = daily.get(name) or []
        return values[i] if i < len(values) else None

    daylight = _daylight_sky(payload, tomorrow, field("sunrise"), field("sunset"))
    weather = {
        "summary": daylight or _sky(field("weather_code"), field("cloud_cover_mean")),
        "sunrise": (field("sunrise") or "")[-5:],
        "sunset": (field("sunset") or "")[-5:],
    }
    _weather_memo = {"at": time.time(), "day": tomorrow, "weather": weather}
    return weather


def fetch_forecast():
    """Everything the forecast sensors need, in one synchronous call."""
    series = _series()
    today = _day_kwh(date.today().isoformat(), series)
    tomorrow = _day_kwh((date.today() + timedelta(days=1)).isoformat(), series)
    tomorrow = tomorrow if tomorrow > 0 else None
    level, advice = _verdict(tomorrow)
    weather = _tomorrow_weather() or {}
    return {
        "forecast_today": round(today, 1) if today > 0 else None,
        "forecast_tomorrow": round(tomorrow, 1) if tomorrow else None,
        "verdict": level,
        "advice": advice,
        "tomorrow_summary": weather.get("summary"),
        "tomorrow_sunrise": weather.get("sunrise"),
        "tomorrow_sunset": weather.get("sunset"),
        "wh_hours": _wh_hours(series),
    }
