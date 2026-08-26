"""Energy platform: the calibrated ensemble forecast on the Energy dashboard.

Home Assistant discovers this by module name - any integration module called
energy.py exposing async_get_solar_forecast is registered as a solar-forecast
provider. The dashboard calls it on every energy/solar_forecast websocket
request (initial load, date change, the hourly refresh), so this must be a
cheap cache read: the forecast coordinator already holds wh_hours.
"""
from homeassistant.core import HomeAssistant

from .const import DOMAIN


async def async_get_solar_forecast(
    hass: HomeAssistant, config_entry_id: str
) -> dict[str, dict[str, float | int]] | None:
    data = hass.data.get(DOMAIN, {}).get(config_entry_id)
    if not data:
        return None
    forecast = data["forecast_coordinator"].data
    if not forecast or not forecast.get("wh_hours"):
        return None
    return {"wh_hours": forecast["wh_hours"]}
