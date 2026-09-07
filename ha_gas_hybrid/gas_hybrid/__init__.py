"""One gas statistic built from billed Octopus data where it exists, Home Mini where it does not.

Octopus's "previous accumulative consumption" is the billed half-hourly meter data. The
integration publishes it as correctly-dated hourly external statistics, but a day only lands
about 18-42 hours after it starts, so today's gas is invisible. The Octopus Home Mini reports
the same meter within minutes but is not the billed record.

This builds `gas_hybrid:consumption_kwh` (and `gas_hybrid:cost_gbp`) hour by hour: for any local
day the legacy series covers, the legacy hours are used verbatim; for any day it does not, the
Home Mini's lifetime meter total supplies the hours instead. Re-importing an hour overwrites it,
so when legacy data for a day finally lands it silently replaces whatever the Mini had estimated.

Point the Energy dashboard's gas source at the hybrid statistics, not at either input.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import re
from typing import Any

import voluptuous as vol

from homeassistant.components.recorder import get_instance
from homeassistant.components.recorder.statistics import (
    async_add_external_statistics,
    get_last_statistics,
    list_statistic_ids,
    statistics_during_period,
)
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.typing import ConfigType
from homeassistant.util import dt as dt_util

_LOGGER = logging.getLogger(__name__)

DOMAIN = "gas_hybrid"

STAT_KWH = f"{DOMAIN}:consumption_kwh"
STAT_COST = f"{DOMAIN}:cost_gbp"

CONF_WINDOW_DAYS = "window_days"
CONF_INTERVAL_MINUTES = "interval_minutes"

DEFAULT_WINDOW_DAYS = 8
DEFAULT_INTERVAL_MINUTES = 30

RE_LEGACY_KWH = re.compile(
    r"^octopus_energy:gas_.+_previous_accumulative_consumption_kwh$"
)
RE_LEGACY_COST = re.compile(r"^octopus_energy:gas_.+_previous_accumulative_cost$")
RE_LIVE_KWH = re.compile(
    r"^sensor\.octopus_energy_gas_.+_current_total_consumption_kwh$"
)

CONFIG_SCHEMA = vol.Schema(
    {
        DOMAIN: vol.Any(
            vol.Schema(
                {
                    vol.Optional(CONF_WINDOW_DAYS, default=DEFAULT_WINDOW_DAYS): vol.All(
                        int, vol.Range(min=2, max=60)
                    ),
                    vol.Optional(
                        CONF_INTERVAL_MINUTES, default=DEFAULT_INTERVAL_MINUTES
                    ): vol.All(int, vol.Range(min=5, max=720)),
                }
            ),
            None,
        )
    },
    extra=vol.ALLOW_EXTRA,
)

SERVICE_MERGE_SCHEMA = vol.Schema({vol.Optional("full", default=False): cv.boolean})

# has_mean was replaced by mean_type; keep working either side of that change.
try:
    from homeassistant.components.recorder.models import StatisticMeanType

    _MEAN_META: dict[str, Any] = {"mean_type": StatisticMeanType.NONE}
except ImportError:  # pragma: no cover - older cores
    _MEAN_META = {"has_mean": False}


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Set up the periodic merge and its manual service."""
    conf = config.get(DOMAIN) or {}
    merger = GasHybrid(
        hass,
        window_days=conf.get(CONF_WINDOW_DAYS, DEFAULT_WINDOW_DAYS),
    )

    async def _service(call: ServiceCall) -> None:
        await merger.async_merge(full=call.data.get("full", False))

    hass.services.async_register(DOMAIN, "merge", _service, schema=SERVICE_MERGE_SCHEMA)

    async def _interval(_now: dt.datetime) -> None:
        await merger.async_merge()

    async_track_time_interval(
        hass,
        _interval,
        dt.timedelta(minutes=conf.get(CONF_INTERVAL_MINUTES, DEFAULT_INTERVAL_MINUTES)),
    )

    async def _started(_event: Any) -> None:
        await merger.async_merge()

    hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _started)
    return True


class GasHybrid:
    """Rebuilds the hybrid gas statistics over a rolling window."""

    def __init__(self, hass: HomeAssistant, window_days: int) -> None:
        self.hass = hass
        self.window_days = window_days
        self._lock = asyncio.Lock()

    async def async_merge(self, full: bool = False) -> None:
        """Rebuild the hybrid statistics, logging rather than raising on failure."""
        async with self._lock:
            try:
                await self._merge(full)
            except Exception:  # noqa: BLE001 - a timer callback must not bubble
                _LOGGER.exception("Gas hybrid merge failed")

    async def _recorder(self, func: Any, *args: Any) -> Any:
        return await get_instance(self.hass).async_add_executor_job(func, *args)

    async def _discover(self) -> dict[str, str] | None:
        """Find the legacy statistics and the live meter entity."""
        ids = await self._recorder(list_statistic_ids, self.hass, None, None)
        found: dict[str, str] = {}
        for row in ids:
            sid = row["statistic_id"]
            if RE_LEGACY_KWH.match(sid):
                found["legacy_kwh"] = sid
            elif RE_LEGACY_COST.match(sid):
                found["legacy_cost"] = sid

        for state in self.hass.states.async_all("sensor"):
            if RE_LIVE_KWH.match(state.entity_id):
                found["live_kwh"] = state.entity_id
                base = state.entity_id[: -len("_current_total_consumption_kwh")]
                found["rate"] = f"{base}_current_rate"
                found["standing_charge"] = f"{base}_current_standing_charge"
                break

        missing = {"legacy_kwh", "live_kwh"} - found.keys()
        if missing:
            _LOGGER.warning("Gas hybrid cannot run yet, missing: %s", sorted(missing))
            return None
        return found

    async def _fetch(
        self, start: dt.datetime, end: dt.datetime | None, ids: set[str]
    ) -> dict[str, list[dict[str, Any]]]:
        return await self._recorder(
            statistics_during_period,
            self.hass,
            start,
            end,
            ids,
            "hour",
            None,
            {"change", "sum"},
        )

    def _float_state(self, entity_id: str | None) -> float | None:
        if not entity_id:
            return None
        state = self.hass.states.get(entity_id)
        if state is None or state.state in ("unknown", "unavailable", ""):
            return None
        try:
            return float(state.state)
        except ValueError:
            return None

    async def _window_start(self, ids: dict[str, str], full: bool) -> dt.datetime | None:
        """Local midnight to rebuild from: the whole legacy history on a first or full run."""
        existing = await self._recorder(
            get_last_statistics, self.hass, 1, STAT_KWH, True, {"sum"}
        )
        if not full and existing.get(STAT_KWH):
            local_midnight = dt_util.start_of_local_day(
                dt_util.now() - dt.timedelta(days=self.window_days)
            )
            return dt_util.as_utc(local_midnight)

        earliest = await self._fetch(
            dt_util.utc_from_timestamp(0), None, {ids["legacy_kwh"]}
        )
        rows = earliest.get(ids["legacy_kwh"]) or []
        if not rows:
            _LOGGER.warning("Gas hybrid found no legacy statistics to build from")
            return None
        return dt_util.utc_from_timestamp(rows[0]["start"])

    async def _baseline(self, stat_id: str, start: dt.datetime) -> float:
        """The hybrid's own cumulative sum immediately before the rebuild window."""
        prior = await self._fetch(start - dt.timedelta(days=30), start, {stat_id})
        rows = prior.get(stat_id) or []
        for row in reversed(rows):
            if row.get("sum") is not None:
                return float(row["sum"])
        return 0.0

    async def _merge(self, full: bool) -> None:
        ids = await self._discover()
        if ids is None:
            return

        start = await self._window_start(ids, full)
        if start is None:
            return

        now = dt_util.utcnow()
        end = now.replace(minute=0, second=0, microsecond=0) + dt.timedelta(hours=1)

        wanted = {ids["legacy_kwh"], ids["live_kwh"]}
        if "legacy_cost" in ids:
            wanted.add(ids["legacy_cost"])
        series = await self._fetch(start, end, wanted)

        legacy_kwh = _changes(series.get(ids["legacy_kwh"]))
        legacy_cost = _changes(series.get(ids.get("legacy_cost", "")))
        live_kwh = _changes(series.get(ids["live_kwh"]))

        # Which local days does the legacy series fully cover?
        legacy_day_rows: dict[dt.date, int] = {}
        for ts in legacy_kwh:
            day = dt_util.as_local(dt_util.utc_from_timestamp(ts)).date()
            legacy_day_rows[day] = legacy_day_rows.get(day, 0) + 1
        today = dt_util.now().date()

        def day_is_legacy(day: dt.date) -> bool:
            """Any billed row for a finished day wins.

            Octopus publishes a day in one go, but it publishes only the half-hours the meter
            actually reported: days exist with three hours of data and no more ever arriving.
            Requiring a full 24 would zero those days permanently, so presence, not count, is
            the test. A day that does land in pieces is corrected on the next rebuild.
            """
            return day < today and legacy_day_rows.get(day, 0) > 0

        rate = self._float_state(ids.get("rate"))
        standing_charge = self._float_state(ids.get("standing_charge"))
        if rate is None:
            _LOGGER.warning(
                "Gas hybrid has no current rate; live days will carry no cost"
            )

        sum_kwh = await self._baseline(STAT_KWH, start)
        sum_cost = await self._baseline(STAT_COST, start)

        rows_kwh: list[dict[str, Any]] = []
        rows_cost: list[dict[str, Any]] = []
        legacy_hours = live_hours = 0

        hour = start
        while hour < end:
            ts = hour.timestamp()
            local = dt_util.as_local(hour)
            day = local.date()

            if day_is_legacy(day):
                kwh = legacy_kwh.get(ts, 0.0)
                cost = legacy_cost.get(ts, 0.0)
                legacy_hours += 1
            else:
                kwh = live_kwh.get(ts, 0.0)
                cost = kwh * rate if rate is not None else 0.0
                # The billed cost includes the day's standing charge; match it.
                if local.hour == 0 and standing_charge is not None and rate is not None:
                    cost += standing_charge
                live_hours += 1

            # Both inputs re-base their cumulative sum from time to time - the Octopus series has
            # done so five times - and an hour's change then reads as the whole meter running
            # backwards. Gas cannot be un-burnt, so a negative change is always that artefact.
            kwh = max(kwh, 0.0)
            cost = max(cost, 0.0)

            sum_kwh += kwh
            sum_cost += cost
            rows_kwh.append({"start": hour, "sum": sum_kwh})
            rows_cost.append({"start": hour, "sum": sum_cost})
            hour += dt.timedelta(hours=1)

        if not rows_kwh:
            return

        async_add_external_statistics(
            self.hass,
            {
                **_MEAN_META,
                "has_sum": True,
                "name": "Gas consumption (hybrid)",
                "source": DOMAIN,
                "statistic_id": STAT_KWH,
                "unit_class": "energy",
                "unit_of_measurement": "kWh",
            },
            rows_kwh,
        )
        async_add_external_statistics(
            self.hass,
            {
                **_MEAN_META,
                "has_sum": True,
                "name": "Gas cost (hybrid)",
                "source": DOMAIN,
                "statistic_id": STAT_COST,
                "unit_class": None,
                "unit_of_measurement": "GBP",
            },
            rows_cost,
        )
        _LOGGER.info(
            "Gas hybrid rebuilt %s -> %s: %d legacy hours, %d live hours, total %.3f kWh",
            dt_util.as_local(start).isoformat(timespec="hours"),
            dt_util.as_local(end).isoformat(timespec="hours"),
            legacy_hours,
            live_hours,
            sum_kwh,
        )


def _changes(rows: list[dict[str, Any]] | None) -> dict[float, float]:
    """Map hour-start timestamp to that hour's consumption/cost change."""
    if not rows:
        return {}
    out: dict[float, float] = {}
    for row in rows:
        change = row.get("change")
        if change is not None:
            out[float(row["start"])] = float(change)
    return out
