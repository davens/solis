"""Sensors for Solis Solarman (read-only)."""
from dataclasses import dataclass
from collections.abc import Callable

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import (
    PERCENTAGE,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
)
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN
from .entity import SolisEntity


@dataclass(frozen=True, kw_only=True)
class SolisSensorDescription(SensorEntityDescription):
    value_fn: Callable[[dict], object] | None = None


def _power(key, name, **kwargs):
    return SolisSensorDescription(
        key=key, name=name, native_unit_of_measurement=UnitOfPower.WATT,
        device_class=SensorDeviceClass.POWER,
        state_class=SensorStateClass.MEASUREMENT, **kwargs)


def _energy(key, name, **kwargs):
    return SolisSensorDescription(
        key=key, name=name, native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY,
        state_class=SensorStateClass.TOTAL_INCREASING, **kwargs)


SENSORS: tuple[SolisSensorDescription, ...] = (
    SolisSensorDescription(
        key="battery_soc", name="Battery", native_unit_of_measurement=PERCENTAGE,
        device_class=SensorDeviceClass.BATTERY,
        state_class=SensorStateClass.MEASUREMENT),
    SolisSensorDescription(
        key="battery_soh", name="Battery health",
        native_unit_of_measurement=PERCENTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC),
    # Signed: positive charging, negative discharging.
    _power("battery_power", "Battery power"),
    SolisSensorDescription(
        key="battery_voltage", name="Battery voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC),
    SolisSensorDescription(
        key="battery_current", name="Battery current",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        device_class=SensorDeviceClass.CURRENT,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC),
    _power("pv_power", "Solar power"),
    _power("pv1_power", "Solar power SW string",
           entity_category=EntityCategory.DIAGNOSTIC),
    _power("pv2_power", "Solar power SE string",
           entity_category=EntityCategory.DIAGNOSTIC),
    _power("house_load", "House load"),
    # Signed: positive importing, negative exporting.
    _power("grid_power", "Grid power"),
    SolisSensorDescription(
        key="grid_voltage", name="Grid voltage",
        native_unit_of_measurement=UnitOfElectricPotential.VOLT,
        device_class=SensorDeviceClass.VOLTAGE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC),
    _energy("solar_today", "Solar today"),
    _energy("grid_import_today", "Grid import today"),
    _energy("grid_export_today", "Grid export today"),
    _energy("house_today", "House consumption today"),
    SolisSensorDescription(
        key="solar_yesterday", name="Solar yesterday",
        native_unit_of_measurement=UnitOfEnergy.KILO_WATT_HOUR,
        device_class=SensorDeviceClass.ENERGY),
    SolisSensorDescription(
        key="charge_current", name="Battery charge current limit",
        native_unit_of_measurement=UnitOfElectricCurrent.AMPERE,
        state_class=SensorStateClass.MEASUREMENT,
        entity_category=EntityCategory.DIAGNOSTIC),
    SolisSensorDescription(
        key="charge_window", name="Charge window",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: d["charge_window"] or "unset"),
    SolisSensorDescription(
        key="mode", name="Work mode",
        entity_category=EntityCategory.DIAGNOSTIC,
        value_fn=lambda d: ", ".join(d["mode_bits"]) or "none"),
)


async def async_setup_entry(hass, entry, async_add_entities):
    coordinator = hass.data[DOMAIN][entry.entry_id]["coordinator"]
    async_add_entities(
        SolisSensor(coordinator, entry, desc) for desc in SENSORS)


class SolisSensor(SolisEntity, SensorEntity):
    def __init__(self, coordinator, entry, description):
        super().__init__(coordinator, entry, description.key)
        self.entity_description = description

    @property
    def native_value(self):
        data = self.coordinator.data
        if data is None:
            return None
        if self.entity_description.value_fn:
            return self.entity_description.value_fn(data)
        return data.get(self._key)
