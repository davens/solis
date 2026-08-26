"""Config flow for Solis Solarman (read-only)."""
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PORT, CONF_SCAN_INTERVAL

from .const import CONF_SERIAL, DEFAULT_PORT, DEFAULT_SCAN_INTERVAL, DOMAIN
from .solis import SolisClient

SCHEMA = vol.Schema({
    vol.Required(CONF_HOST): str,
    vol.Required(CONF_SERIAL): int,
    vol.Optional(CONF_PORT, default=DEFAULT_PORT): int,
    vol.Optional(CONF_SCAN_INTERVAL, default=DEFAULT_SCAN_INTERVAL): vol.All(
        int, vol.Range(min=5, max=300)),
})


def _probe(data):
    """Blocking connectivity check: one full sweep, then hang up."""
    client = SolisClient(
        data[CONF_HOST], data[CONF_SERIAL], data.get(CONF_PORT, DEFAULT_PORT))
    try:
        client.read_all()
    finally:
        client.close()


class SolisConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """One step: where is the logger."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            await self.async_set_unique_id(str(user_input[CONF_SERIAL]))
            self._abort_if_unique_id_configured()
            try:
                await self.hass.async_add_executor_job(_probe, user_input)
            except Exception:  # noqa: BLE001 - any failure means "not reachable"
                errors["base"] = "cannot_connect"
            else:
                return self.async_create_entry(
                    title=f"Solis inverter ({user_input[CONF_HOST]})",
                    data=user_input)
        return self.async_show_form(
            step_id="user", data_schema=SCHEMA, errors=errors)
