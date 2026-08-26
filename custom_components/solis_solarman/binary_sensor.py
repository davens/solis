"""Binary sensors for Solis Solarman (read-only)."""
from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
)
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .entity import SolisEntity

BINARY = (
    # (key, name, device_class, entity_category)
    ("battery_charging", "Battery charging",
     BinarySensorDeviceClass.BATTERY_CHARGING, None),
    ("timed_charging", "Timed charging",
     None, EntityCategory.DIAGNOSTIC),
)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        SolisBinarySensor(coordinator, entry, *spec) for spec in BINARY)


class SolisBinarySensor(SolisEntity, BinarySensorEntity):
    def __init__(self, coordinator, entry, key, name, device_class, category):
        super().__init__(coordinator, entry, key)
        self._attr_name = name
        self._attr_device_class = device_class
        self._attr_entity_category = category

    @property
    def is_on(self):
        data = self.coordinator.data
        return None if data is None else bool(data.get(self._key))
