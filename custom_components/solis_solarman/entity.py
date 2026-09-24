"""Shared base entity for Solis Solarman."""
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN
from .coordinator import SolisCoordinator


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

    @property
    def available(self) -> bool:
        """Stay available across a momentary loss of the logger.

        The hold is uniform across every key, and its length is set by what
        the instantaneous power sensors tolerate rather than by what the
        monotonic daily counters would: a held counter contributes a zero
        delta to statistics and is harmless, while a held power is a
        measurement that never happened. The forecast coordinator has no hold
        - Open-Meteo being down is already non-fatal.
        """
        if super().available:
            return True
        return (isinstance(self.coordinator, SolisCoordinator)
                and self.coordinator.holding_last_good)
