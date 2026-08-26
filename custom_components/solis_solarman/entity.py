"""Shared base entity for Solis Solarman."""
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN


class SolisEntity(CoordinatorEntity):
    """One device: the inverter behind the logger."""

    _attr_has_entity_name = True

    def __init__(self, coordinator, entry, key):
        super().__init__(coordinator)
        self._key = key
        self._attr_unique_id = f"{entry.unique_id or entry.entry_id}_{key}"
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, entry.unique_id or entry.entry_id)},
            name="Solis inverter",
            manufacturer="Ginlong Solis",
            model="S6 hybrid via Solarman logger",
        )
