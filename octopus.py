"""Octopus Intelligent Go: is the Tesla charging, and when is it next planned to.

The house battery holds a charge window at 23:30-05:30 partly so it does not
discharge into the car overnight (see CLAUDE.md). Octopus controls the car's
charging and can schedule a "dispatch" OUTSIDE that window - a daytime
bump-charge - and nothing in this repo currently sees one coming. So this module
answers two questions and no others: is the car charging now, and when is the
next planned slot.

  uv run --no-project python octopus.py show
  uv run --no-project python octopus.py devices     # find the device id once

Credentials come from the environment, never from a file here:
OCTOPUS_API_KEY (sk_live_...) and OCTOPUS_ACCOUNT (A-12345678). Optionally
OCTOPUS_DEVICE_ID to skip the device lookup, which is one request of rate-limit
budget saved per process.

Read-only: the only mutation ever issued is obtainKrakenToken, which is how the
API key is exchanged for a JWT. Nothing here touches the inverter.

Nothing raises into a caller. Every failure comes back as {"ok": False, "error":
"..."} in the same null-and-retry spirit as the poller, because settings_dash.py
imports this and must not be taken down by Octopus being unreachable.
"""
import base64
import json
import os
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

API_URL = "https://api.octopus.energy/v1/graphql/"
HTTP_TIMEOUT = 20

# The inverter's charge window is LOCAL time and every timestamp Octopus returns
# is UTC. During BST a local 23:30 is 22:30Z, so comparing the two naively is an
# hour out for half the year - convert first, always.
TZ = ZoneInfo("Europe/London")

# The off-peak window the house battery charges in. A dispatch that falls wholly
# inside it is the normal, expected case; anything sticking out of it is the
# daytime bump-charge this module was written to surface. Kept as (h, m) rather
# than read from the inverter deliberately: this module makes no Modbus calls.
WINDOW_START = (23, 30)
WINDOW_END = (5, 30)

# The rate limiter RATCHETS. KT-CT-1199 means too many requests, and repeatedly
# tripping the dynamic limit progressively cuts the allowance - reportedly as far
# as one request an hour, needing manual unblocking by Octopus. That is the most
# damaging thing that can happen in this file, so the floor is enforced HERE and
# not left to the caller: state() serves cache until MIN_POLL_SECONDS has passed
# whatever it is asked, and a 1199 parks every request for RATE_LIMIT_BACKOFF.
MIN_POLL_SECONDS = 300
RATE_LIMIT_BACKOFF = 900

# Refresh this far before the token's own expiry, so a slow request cannot land
# after it.
TOKEN_MARGIN = 300

# "unable to fetch planned dispatches" - routine rather than a fault, and
# whitelisted by the Home Assistant integration too. An empty dispatch list is
# equally normal; the car is simply not scheduled.
TOLERATED_CODES = {"KT-CT-4340"}
RATE_LIMIT_CODE = "KT-CT-1199"
AUTH_CODE = "KT-CT-1111"

AUTH_QUERY = """
mutation ObtainToken($key: String!) {
  obtainKrakenToken(input: { APIKey: $key }) { token refreshToken }
}
"""

REFRESH_QUERY = """
mutation Refresh($rt: String!) {
  obtainKrakenToken(input: { refreshToken: $rt }) { token refreshToken }
}
"""

DEVICES_QUERY = """
query Devices($account: String!) {
  devices(accountNumber: $account) { id deviceType provider }
}
"""

# flexPlannedDispatches, NOT plannedDispatches. The unprefixed field was
# deprecated 2025-05-27 with removal scheduled on or after 2026-01-16, a date
# already past. Every older recipe online uses the old name, so a future reader
# will be tempted to "fix" this back - don't.
POLL_QUERY = """
query Dispatches($account: String!, $device: String!) {
  devices(accountNumber: $account, deviceId: $device) { status { currentState } }
  flexPlannedDispatches(deviceId: $device) { start end type energyAddedKwh }
  completedDispatches(accountNumber: $account) { start end delta }
}
"""


class OctopusError(Exception):
    """Anything that stopped a poll. Carries the Kraken code when there was one."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


_token = None
_token_expires = 0.0
_refresh_token = None
# The Authorization header format is inconsistent in the wild: the Home
# Assistant integration sends "JWT <token>", other clients send the bare token
# and both are reported working. Try JWT first, fall back once on a 401 or
# KT-CT-1111, and remember which one worked so only the first request pays.
_auth_prefix = "JWT "
_device_id = None
_blocked_until = 0.0
_cache = None
# Serialises the poll in state(). Without it two dashboard threads seeing the
# same stale cache both go to the network - and a cold poll is auth + device
# lookup + data, so the race multiplies into a burst of requests against a
# rate limiter that ratchets. Confirmed live with two threads pre-fix.
_poll_lock = threading.Lock()


# ---------------------------------------------------------------- transport


def _env(name):
    value = os.environ.get(name)
    if not value:
        raise OctopusError(f"{name} is not set")
    return value


def _error_details(payload):
    """(codes, message) from a GraphQL errors array, or ([], None)."""
    errors = payload.get("errors") or []
    if not errors:
        return [], None
    codes, messages = [], []
    for error in errors:
        extensions = error.get("extensions") or {}
        code = extensions.get("errorCode") or extensions.get("errorType")
        message = str(error.get("message") or "")
        # The code is not always in extensions; some responses only carry it in
        # the message text, and the whole backoff hangs off recognising 1199.
        if not code and "KT-CT-" in message:
            start = message.index("KT-CT-")
            code = message[start:start + 10]  # KT-CT- plus four digits
        codes.append(str(code or ""))
        messages.append(message or str(code or "error"))
    return codes, "; ".join(m for m in messages if m) or "Octopus returned an error"


def _http(payload, headers):
    request = urllib.request.Request(
        API_URL, data=json.dumps(payload).encode(), headers=headers, method="POST")
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT) as response:
            return response.getcode(), json.load(response)
    except urllib.error.HTTPError as error:
        # GraphQL usually answers 200 with an errors array, but auth failures do
        # come back as real 4xx - and the body is still the useful part.
        try:
            return error.code, json.loads(error.read().decode())
        except (ValueError, OSError):
            if error.code == 429:
                # No parseable body to find KT-CT-1199 in, but a 429 is a rate
                # limit whatever the body says - arm the backoff here or a bare
                # 429 gets retried on the 3-min floor forever.
                _rate_limited()
                raise OctopusError("rate limited by Octopus (HTTP 429)", RATE_LIMIT_CODE)
            raise OctopusError(f"HTTP {error.code} from Octopus")
    except urllib.error.URLError as error:
        raise OctopusError(f"cannot reach Octopus: {error.reason}")
    except (TimeoutError, OSError) as error:
        raise OctopusError(f"cannot reach Octopus: {error}")
    except ValueError:
        raise OctopusError("Octopus returned something that is not JSON")


def _graphql(query, variables, token=None, tolerate=()):
    """One request. Raises OctopusError on anything not in `tolerate`.

    A GraphQL endpoint answers HTTP 200 with an `errors` array, so the status
    code alone proves nothing - the errors are what decide.
    """
    global _auth_prefix
    remaining = _blocked_until - time.time()
    if remaining > 0:
        raise OctopusError(
            f"backing off after a rate limit, {remaining / 60:.0f} min left",
            RATE_LIMIT_CODE)

    payload = {"query": query, "variables": variables}
    for attempt in (0, 1):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = _auth_prefix + token
        status, body = _http(payload, headers)
        codes, message = _error_details(body)

        # HTTP 429 counts as a rate limit even when the body carries no
        # KT-CT-1199 - an empty or non-GraphQL 429 body must still arm the
        # backoff, or the 15-min block never engages on exactly the responses
        # a stressed rate limiter is most likely to send.
        if RATE_LIMIT_CODE in codes or status == 429:
            _rate_limited()
            raise OctopusError(f"rate limited by Octopus ({message})", RATE_LIMIT_CODE)

        rejected = AUTH_CODE in codes or status in (401, 403)
        if rejected and token and attempt == 0 and _auth_prefix:
            _auth_prefix = ""  # try the bare-token form once before giving up
            continue
        if codes and not all(c in tolerate for c in codes):
            raise OctopusError(message, codes[0] or None)
        data = body.get("data")
        if data is None:
            raise OctopusError(message or "Octopus returned no data")
        return data


def _rate_limited():
    global _blocked_until
    _blocked_until = time.time() + RATE_LIMIT_BACKOFF


# ---------------------------------------------------------------------- auth


def _jwt_expiry(token):
    """Epoch seconds from the JWT's own `exp` claim, or None.

    Token lifetime is widely quoted as 60 minutes but that is not documented
    anywhere official, so it is not hardcoded - the token says when it expires
    and that is the only trustworthy answer. base64url segments arrive without
    padding, hence the manual pad to a multiple of four.
    """
    try:
        segment = token.split(".")[1]
        segment += "=" * (-len(segment) % 4)
        claims = json.loads(base64.urlsafe_b64decode(segment).decode())
        expiry = claims.get("exp")
        return float(expiry) if expiry is not None else None
    except (AttributeError, IndexError, ValueError, TypeError, UnicodeDecodeError):
        return None


def _store_token(result):
    global _token, _token_expires, _refresh_token
    _token = result.get("token")
    if not _token:
        raise OctopusError("Octopus returned no token")
    _refresh_token = result.get("refreshToken") or _refresh_token
    expiry = _jwt_expiry(_token)
    # No readable exp is not a reason to refuse the token; treat it as
    # short-lived instead and re-authenticate sooner than needed.
    _token_expires = (expiry or time.time() + 600) - TOKEN_MARGIN
    return _token


def _authenticate():
    key = _env("OCTOPUS_API_KEY")
    data = _graphql(AUTH_QUERY, {"key": key})
    return _store_token(data.get("obtainKrakenToken") or {})


def token():
    """A live JWT: cached, else refreshed, else obtained from the API key."""
    if _token and time.time() < _token_expires:
        return _token
    if _refresh_token:
        try:
            data = _graphql(REFRESH_QUERY, {"rt": _refresh_token})
            return _store_token(data.get("obtainKrakenToken") or {})
        except OctopusError as error:
            if error.code == RATE_LIMIT_CODE:
                raise
            # A refresh token expires (7 days is the reported life) or is
            # revoked; the API key still works, so fall through rather than fail.
    return _authenticate()


# ------------------------------------------------------------------- devices


def devices():
    """Every device on the account: id, deviceType, provider."""
    account = _env("OCTOPUS_ACCOUNT")
    data = _graphql(DEVICES_QUERY, {"account": account}, token())
    return data.get("devices") or []


def device_id():
    """The car's device UUID. Dispatches are keyed by this, not by account."""
    global _device_id
    if _device_id:
        return _device_id
    override = os.environ.get("OCTOPUS_DEVICE_ID")
    if override:
        _device_id = override
        return _device_id
    found = devices()
    if not found:
        raise OctopusError("no devices on this account")
    # Prefer an EV over a heat pump or anything else that may appear later; fall
    # back to the first device rather than failing, since a single-device
    # account is the normal case here.
    for device in found:
        if "VEHICLE" in str(device.get("deviceType") or "").upper():
            _device_id = device.get("id")
            break
    else:
        _device_id = found[0].get("id")
    if not _device_id:
        raise OctopusError("device has no id")
    return _device_id


# ------------------------------------------------------------------- parsing


def _local(stamp):
    """A UTC ISO timestamp from Octopus as an aware Europe/London datetime."""
    if not stamp:
        return None
    text = str(stamp).strip().replace("Z", "+00:00")
    try:
        moment = datetime.fromisoformat(text)
    except ValueError:
        return None
    # The schema says DateTime! and every observed value carries a Z, but a
    # naive value would silently be read as local time - assume UTC instead,
    # which is what the field actually means.
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(TZ)


def _number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _in_window(moment):
    """Is this local instant inside the 23:30-05:30 off-peak window?"""
    minutes = moment.hour * 60 + moment.minute
    start = WINDOW_START[0] * 60 + WINDOW_START[1]
    end = WINDOW_END[0] * 60 + WINDOW_END[1]
    if start <= end:
        return start <= minutes < end
    return minutes >= start or minutes < end  # the window crosses midnight


def _outside_window(start, end):
    """Does any part of this dispatch fall outside the off-peak window?

    Sampled minute by minute rather than solved as interval arithmetic: the
    window wraps midnight, so it is two spans on every calendar day, and on the
    DST change days a local day is 23 or 25 hours long. Stepping absolute time
    and asking each instant what the local clock says sidesteps both. Slots are
    hours, not days, so the cost is trivial.
    """
    if end is None or end <= start:
        return not _in_window(start)
    moment, step, guard = start, timedelta(minutes=1), 0
    while moment < end and guard < 24 * 60:
        if not _in_window(moment):
            return True
        moment += step
        guard += 1
    # The end instant is exclusive, so test the last minute rather than `end`.
    return not _in_window(end - step)


def _dispatch(entry):
    """One dispatch as local times plus its kWh, or None if it has no start."""
    start, end = _local(entry.get("start")), _local(entry.get("end"))
    if start is None:
        return None
    return {
        "start": start,
        "end": end,
        "kwh": _number(entry.get("energyAddedKwh")) or _number(entry.get("delta")),
        "type": entry.get("type"),
        "outside_window": _outside_window(start, end),
    }


def _dispatches(entries):
    parsed = [_dispatch(e) for e in (entries or []) if isinstance(e, dict)]
    return sorted((d for d in parsed if d), key=lambda d: d["start"])


def _brackets(dispatch, now):
    # Compared as epoch seconds, never as datetimes: two aware datetimes with
    # the SAME tzinfo compare by wall clock, and in the repeated 01:00 hour of
    # the autumn change that ordering is wrong - a dispatch that started
    # 01:45 BST read as "not started" at 01:15 GMT, 30 real minutes later.
    return (dispatch["start"].timestamp() <= now.timestamp()
            and (dispatch["end"] is None
                 or now.timestamp() < dispatch["end"].timestamp()))


def _build_state(payload):
    """Turn one poll's GraphQL data into the dict state() hands out.

    Pure: no network, no globals. All the traps live in here, so this is the
    part worth testing against synthetic payloads.
    """
    now = datetime.now(TZ)
    device = (payload.get("devices") or [{}])
    status = device[0].get("status") or {}
    current_state = status.get("currentState")

    planned = _dispatches(payload.get("flexPlannedDispatches"))
    completed = _dispatches(payload.get("completedDispatches"))

    # THE TRAP: a planned dispatch VANISHES from flexPlannedDispatches the moment
    # it starts. An empty planned list therefore reads exactly backwards - it
    # looks like "nothing scheduled" at the one moment the car is actually
    # pulling power. So charging-now is decided from the device state and from a
    # completed entry bracketing right now as well, never from planned alone.
    active = [d for d in completed if _brackets(d, now)]
    active += [d for d in planned if _brackets(d, now)]
    state_text = str(current_state or "").upper()
    # The currentState enum is not published anywhere verifiable, so match on
    # substrings and let an unrecognised state mean "not charging" - the
    # bracketing dispatch above is the signal that does not depend on guessing.
    # SMART_CONTROL_IN_PROGRESS is NOT charging: it is set for the whole time
    # the car sits plugged in with a plan, and read as "charging" it lit the
    # tile at 16:14 with the first dispatch ten hours away (2026-08-23). Only
    # a boost, or a dispatch bracketing now, means current is flowing.
    state_says_charging = "BOOST" in state_text
    charging = bool(active) or state_says_charging

    # .timestamp() for the same fold reason as _brackets.
    upcoming = [d for d in planned if d["start"].timestamp() > now.timestamp()]
    following = upcoming[0] if upcoming else None
    current = active[0] if active else None

    return {
        "ok": True,
        "error": None,
        "charging_now": charging,
        "current_state": current_state,
        "current": current,
        "next_start": following["start"] if following else None,
        "next_end": following["end"] if following else None,
        "next_kwh": following["kwh"] if following else None,
        "planned": planned,
        # The whole point of the module: a dispatch, planned or already running,
        # that reaches outside 23:30-05:30 and would therefore drain the house
        # battery into the car at a time nothing else here watches.
        "daytime_dispatch": any(d["outside_window"] for d in upcoming)
                            or bool(current and current["outside_window"]),
        "read_at_epoch": time.time(),
    }


def _failed(message):
    return {
        "ok": False,
        "error": message,
        "charging_now": False,
        "current_state": None,
        "current": None,
        "next_start": None,
        "next_end": None,
        "next_kwh": None,
        "planned": [],
        "daytime_dispatch": False,
        "read_at_epoch": time.time(),
    }


# ------------------------------------------------------------------- tariff

TARIFF_QUERY = """
query Tariff($account: String!) {
  account(accountNumber: $account) {
    electricityAgreements(active: true) {
      meterPoint { direction }
      tariff {
        ... on StandardTariff { unitRate standingCharge }
        ... on HalfHourlyTariff {
          standingCharge
          unitRates { value }
        }
        ... on DayNightTariff { dayRate nightRate standingCharge }
        ... on ThreeRateTariff {
          dayRate nightRate offPeakRate standingCharge
        }
      }
    }
  }
}
"""

# Tariff rates change on the order of price-cap events, not minutes, so this is
# fetched at most once per calendar day (owner's explicit call, to protect the
# rate-limit budget for the dispatch poll). A failed fetch serves yesterday's
# answer and retries no sooner than an hour later.
_tariff_cache = {"day": None, "value": None, "last_attempt": 0.0}
TARIFF_RETRY_SECONDS = 3600


def _parse_tariff(data):
    """{'import': {...}, 'export': {...}} from the agreements, values in pence."""
    result = {}
    agreements = ((data.get("account") or {}).get("electricityAgreements")) or []
    for agreement in agreements:
        direction = str(((agreement.get("meterPoint") or {}).get("direction")) or "").upper()
        tariff = agreement.get("tariff") or {}
        rates = [_number(tariff.get(k)) for k in ("unitRate", "dayRate", "nightRate",
                                                  "offPeakRate")]
        # Intelligent Go's import is a HalfHourlyTariff with no flat rate
        # fields; its unitRates list carries the day's prices, which for Go
        # collapse to exactly two distinct values (6.9p / 30.5p verified live
        # 2026-08-22) - so min and max are the off-peak and peak rates.
        rates += [_number(r.get("value")) for r in (tariff.get("unitRates") or [])
                  if isinstance(r, dict)]
        rates = sorted(r for r in rates if r is not None)
        entry = {
            "standing": _number(tariff.get("standingCharge")),
            # Cheapest and dearest published rate; a single-rate tariff puts
            # the same number in both.
            "cheap": rates[0] if rates else None,
            "peak": rates[-1] if rates else None,
        }
        key = "export" if "EXPORT" in direction else "import"
        result[key] = entry
    return result


def tariff():
    """Rates and standing charge, or None while nothing has ever been fetched.

    Environment overrides (pence): OCTOPUS_RATE_CHEAP / OCTOPUS_RATE_PEAK for
    import, OCTOPUS_RATE_EXPORT, OCTOPUS_STANDING - applied over whatever the
    API returned, so a tariff the API describes incompletely still prices.
    """
    day = datetime.now(TZ).date().isoformat()
    fresh = _tariff_cache["day"] == day and _tariff_cache["value"] is not None
    if not fresh and time.time() - _tariff_cache["last_attempt"] > TARIFF_RETRY_SECONDS:
        _tariff_cache["last_attempt"] = time.time()
        try:
            data = _graphql(TARIFF_QUERY, {"account": _env("OCTOPUS_ACCOUNT")}, token())
            _tariff_cache["value"] = _parse_tariff(data)
            _tariff_cache["day"] = day
        except OctopusError:
            pass  # keep serving the previous day's answer, retry in an hour
    value = _tariff_cache["value"]
    if value is None:
        return None
    result = {"import": dict(value.get("import") or {}),
              "export": dict(value.get("export") or {})}
    overrides = (("import", "cheap", "OCTOPUS_RATE_CHEAP"),
                 ("import", "peak", "OCTOPUS_RATE_PEAK"),
                 ("import", "standing", "OCTOPUS_STANDING"),
                 ("export", "cheap", "OCTOPUS_RATE_EXPORT"),
                 ("export", "peak", "OCTOPUS_RATE_EXPORT"))
    for section, field, env_name in overrides:
        value_text = os.environ.get(env_name)
        if value_text:
            result[section][field] = _number(value_text)
    return result


PREFS_QUERY = """
query Preferences($account: String!) {
  vehicleChargingPreferences(accountNumber: $account) {
    weekdayTargetSoc weekendTargetSoc weekdayTargetTime weekendTargetTime
  }
}
"""

# Target SOC and ready-by time change when the owner edits them in the Octopus
# app - rarely. Cached like the tariff: daily, hourly retry on failure.
# Schema verified live 2026-08-22 (75% by 05:30, both day types).
_prefs_cache = {"day": None, "value": None, "last_attempt": 0.0}


def preferences():
    """vehicleChargingPreferences as a dict, or None while never fetched."""
    day = datetime.now(TZ).date().isoformat()
    fresh = _prefs_cache["day"] == day and _prefs_cache["value"] is not None
    if not fresh and time.time() - _prefs_cache["last_attempt"] > TARIFF_RETRY_SECONDS:
        _prefs_cache["last_attempt"] = time.time()
        try:
            data = _graphql(PREFS_QUERY, {"account": _env("OCTOPUS_ACCOUNT")}, token())
            _prefs_cache["value"] = data.get("vehicleChargingPreferences") or None
            _prefs_cache["day"] = day
        except OctopusError:
            pass  # keep serving the previous answer, retry in an hour
    return _prefs_cache["value"]


# ------------------------------------------------------------------ the poll


def _poll():
    global _device_id
    account = _env("OCTOPUS_ACCOUNT")
    for attempt in (0, 1):
        device = device_id()
        try:
            return _graphql(POLL_QUERY, {"account": account, "device": device},
                            token(), tolerate=TOLERATED_CODES)
        except OctopusError as error:
            # Re-registering the car changes its UUID, so a cached id can go
            # stale while the account is perfectly healthy. Re-resolve once
            # before reporting a failure.
            if attempt == 0 and _device_id and "device" in str(error).lower():
                _device_id = None
                continue
            raise


def state():
    """The current view: cached, refreshed at most every MIN_POLL_SECONDS.

    There is deliberately no force/refresh argument. The rate limiter ratchets
    downwards when it is tripped, so the interval is not the caller's to
    override - a dashboard polling in a loop must not be able to walk into a
    manual unblock. Failures are cached too, for the same reason: an unreachable
    Octopus is retried on the same floor rather than hammered.
    """
    global _cache
    if _cache and time.time() - _cache["read_at_epoch"] < MIN_POLL_SECONDS:
        return _cache
    with _poll_lock:
        # Re-check under the lock: the thread that queued behind an in-flight
        # poll finds a fresh cache here and must not poll again.
        if _cache and time.time() - _cache["read_at_epoch"] < MIN_POLL_SECONDS:
            return _cache
        try:
            _cache = _build_state(_poll())
        except OctopusError as error:
            _cache = _failed(str(error))
        except Exception as error:  # nothing here may raise into the dashboard
            _cache = _failed(f"unexpected error: {error}")
        return _cache


def _day_prefix(moment, now):
    """"", "tomorrow " or a weekday, so a time alone is never ambiguous."""
    days = (moment.date() - now.date()).days
    if days <= 0:
        return ""
    if days == 1:
        return "tomorrow "
    return moment.strftime("%a ")


def describe(current=None):
    """One short sentence for the dashboard. States facts, recommends nothing.

    A dispatch is a plan and not a promise: slots are cancelled at the last
    minute and a dispatch does not guarantee the car draws anything. The wording
    says "planned" for that reason.
    """
    current = current or state()
    if not current["ok"]:
        return "car charge status unavailable"
    now = datetime.now(TZ)
    if current["charging_now"]:
        active = current.get("current")
        if active and active["end"]:
            return f"car charging now, until {active['end']:%H:%M}"
        return "car charging now"
    start, end = current["next_start"], current["next_end"]
    if start is None:
        return "no car charge planned"
    when = f"{_day_prefix(start, now)}{start:%H:%M}"
    if end is not None:
        when += f"-{end:%H:%M}"
    kwh = current["next_kwh"]
    if kwh:
        # abs() because Octopus reports dispatch energy as a signed delta,
        # negative for energy into the car (verified live 2026-08-22); the
        # magnitude is the readable part. :g after rounding, not :.0f - 12.0
        # prints as "12" but 8.5 stays "8.5" rather than becoming "8", which is
        # what :.0f does (it rounds half to even) and reads as a wrong number
        # rather than a rounded one.
        return f"next car charge planned {when}, {round(abs(kwh), 1):g} kWh"
    return f"next car charge planned {when}"


# ------------------------------------------------------------------- the CLI


def _show():
    current = state()
    if not current["ok"]:
        print(f"octopus: {current['error']}")
        return 1
    print(describe(current))
    print(f"  charging now   {current['charging_now']}"
          f"  (device state {current['current_state'] or 'unknown'})")
    print(f"  daytime slot   {current['daytime_dispatch']}")
    if not current["planned"]:
        print("  planned        none  (note: a slot disappears from the planned "
              "list once it starts)")
    for dispatch in current["planned"]:
        when = f"{dispatch['start']:%a %H:%M}"
        if dispatch["end"] is not None:
            when += f"-{dispatch['end']:%H:%M}"
        kwh = "" if dispatch["kwh"] is None else f"  {abs(dispatch['kwh']):.1f} kWh"
        flag = "  OUTSIDE 23:30-05:30" if dispatch["outside_window"] else ""
        print(f"  planned        {when}  {dispatch['type'] or '-'}{kwh}{flag}")
    return 0


def _devices():
    try:
        found = devices()
    except OctopusError as error:
        print(f"octopus: {error}")
        return 1
    if not found:
        print("no devices on this account")
        return 0
    for device in found:
        print(f"  {device.get('id')}  {device.get('deviceType') or '-'}"
              f"  {device.get('provider') or '-'}")
    return 0


def main():
    args = sys.argv[1:]
    command = args[0] if args else "show"
    if command == "show":
        return _show()
    if command == "devices":
        return _devices()
    print(f"usage: octopus.py [show|devices]  (unknown command {command!r})")
    return 2


if __name__ == "__main__":
    sys.exit(main())
