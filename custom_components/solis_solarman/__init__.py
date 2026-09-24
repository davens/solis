"""Solis Solarman (read-only) integration."""
from datetime import timedelta
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_HOLD_SECONDS,
    CONF_SERIAL,
    DEFAULT_HOLD_SECONDS,
    DEFAULT_PORT,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
)
from .coordinator import SolisCoordinator
from .forecast import fetch_forecast
from .solis import SolisClient

_LOGGER = logging.getLogger(__name__)
PLATFORMS = [Platform.BINARY_SENSOR, Platform.SENSOR]


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    client = SolisClient(
        entry.data[CONF_HOST], entry.data[CONF_SERIAL],
        entry.data.get(CONF_PORT, DEFAULT_PORT))

    async def _update():
        try:
            return await hass.async_add_executor_job(client.read_all)
        except Exception as exc:  # noqa: BLE001 - pysolarmanv5 raises broadly
            raise UpdateFailed(str(exc)) from exc

    coordinator = SolisCoordinator(
        hass, name=DOMAIN, update_method=_update,
        update_interval=timedelta(
            seconds=entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)),
        hold_seconds=entry.data.get(CONF_HOLD_SECONDS, DEFAULT_HOLD_SECONDS))
    await coordinator.async_config_entry_first_refresh()

    async def _update_forecast():
        try:
            return await hass.async_add_executor_job(
                fetch_forecast, hass.config.latitude, hass.config.longitude)
        except Exception as exc:  # noqa: BLE001 - network errors of every shape
            raise UpdateFailed(str(exc)) from exc

    forecast_coordinator = DataUpdateCoordinator(
        hass, _LOGGER, name=f"{DOMAIN}_forecast",
        update_interval=timedelta(minutes=30),
        update_method=_update_forecast)
    # Plain refresh, not first_refresh: Open-Meteo being down must not take
    # the inverter sensors down with it - the forecast just reads unavailable.
    await forecast_coordinator.async_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = {
        "coordinator": coordinator, "client": client,
        "forecast_coordinator": forecast_coordinator}
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if ok:
        data = hass.data[DOMAIN].pop(entry.entry_id)
        await hass.async_add_executor_job(data["client"].close)
    return ok
