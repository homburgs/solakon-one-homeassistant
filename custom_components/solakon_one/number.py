"""Number platform for Solakon ONE integration."""
from __future__ import annotations

import logging

from homeassistant.components.number import NumberEntity, NumberMode, NumberDeviceClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfPower
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import DOMAIN, NUMBER_DEFINITIONS, REGISTERS

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    config_entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up Solakon ONE number entities."""
    coordinator = hass.data[DOMAIN][config_entry.entry_id]["coordinator"]
    hub = hass.data[DOMAIN][config_entry.entry_id]["hub"]

    # Get device info
    device_info = await hub.async_get_device_info()

    entities = []
    for key, definition in NUMBER_DEFINITIONS.items():
        # Special handling for force_duration (virtual entity with minutes<->seconds conversion)
        if key == "force_duration":
            entities.append(
                ForceDurationNumber(
                    coordinator,
                    hub,
                    config_entry,
                    definition,
                    device_info,
                )
            )
        # Special handling for force_power (writes to both 46003 and 46005)
        elif key == "force_power":
            entities.append(
                ForcePowerNumber(
                    coordinator,
                    hub,
                    config_entry,
                    definition,
                    device_info,
                )
            )
        # Only create number entities for registers that exist and have rw flag
        elif key in REGISTERS and REGISTERS[key].get("rw", False):
            entities.append(
                SolakonNumber(
                    coordinator,
                    hub,
                    config_entry,
                    key,
                    definition,
                    device_info,
                )
            )

    if entities:
        async_add_entities(entities, True)


class SolakonNumber(CoordinatorEntity, NumberEntity):
    """Representation of a Solakon ONE number entity."""

    def __init__(
        self,
        coordinator,
        hub,
        config_entry: ConfigEntry,
        number_key: str,
        definition: dict,
        device_info: dict,
    ) -> None:
        """Initialize the number entity."""
        super().__init__(coordinator)
        self._hub = hub
        self._number_key = number_key
        self._definition = definition
        self._config_entry = config_entry
        self._device_info = device_info
        self._register_config = REGISTERS[number_key]

        # Set unique ID and entity ID
        self._attr_unique_id = f"{config_entry.entry_id}_{number_key}"
        self.entity_id = f"number.solakon_one_{number_key}"

        # Set basic attributes
        self._attr_name = definition["name"]
        self._attr_icon = definition.get("icon")

        # Set number attributes
        self._attr_native_min_value = definition.get("min", 0)
        self._attr_native_max_value = definition.get("max", 100000)
        self._attr_native_step = definition.get("step", 1)

        # Set device class and unit
        if definition.get("device_class") == "power":
            self._attr_device_class = NumberDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT
        elif definition.get("unit"):
            self._attr_native_unit_of_measurement = definition["unit"]

        # Set mode
        mode = definition.get("mode", "box")
        if mode == "box":
            self._attr_mode = NumberMode.BOX
        elif mode == "slider":
            self._attr_mode = NumberMode.SLIDER
        else:
            self._attr_mode = NumberMode.AUTO

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get("name", "Solakon ONE"),
            manufacturer=self._device_info.get("manufacturer", "Solakon"),
            model=self._device_info.get("model", "One"),
            sw_version=self._device_info.get("version"),
            serial_number=self._device_info.get("serial_number"),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data and self._number_key in self.coordinator.data:
            value = self.coordinator.data[self._number_key]

            # Value is already processed by modbus.py (scaled and converted)
            if isinstance(value, (int, float)):
                self._attr_native_value = float(value)
                _LOGGER.debug(
                    f"{self._number_key}: Read value = {self._attr_native_value}"
                )
            else:
                _LOGGER.warning(
                    f"Invalid value type for {self._number_key}: {type(value)}"
                )
                self._attr_native_value = None
        else:
            self._attr_native_value = None

        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        # Convert to int for Modbus register writing
        int_value = int(value)

        # Get register info
        address = self._register_config["address"]
        count = self._register_config.get("count", 1)
        data_type = self._register_config.get("type", "u16")
        scale = self._register_config.get("scale", 1)

        # Apply scaling for write (reverse of read scaling)
        # If scale=1 (which is the case for all our RW registers), this does nothing
        if scale != 1:
            int_value = int(value * scale)

        _LOGGER.info(
            f"Setting {self._number_key} at address {address} to {value} "
            f"(raw value: {int_value}, type: {data_type}, count: {count})"
        )

        # Write based on register count
        if count == 1:
            # Single register write (16-bit)
            # Ensure value fits in uint16 range
            if int_value < 0:
                int_value = 0
            elif int_value > 0xFFFF:
                int_value = 0xFFFF

            success = await self._hub.async_write_register(address, int_value)
        else:
            # Multi-register write (32-bit)
            # Handle signed/unsigned conversion
            if "i32" in data_type.lower() and int_value < 0:
                # Convert negative to two's complement for I32
                int_value = int_value + 0x100000000

            # Ensure value fits in uint32 range
            if int_value < 0:
                int_value = 0
            elif int_value > 0xFFFFFFFF:
                int_value = 0xFFFFFFFF

            # Split into high and low words (big-endian: high word first)
            high_word = (int_value >> 16) & 0xFFFF
            low_word = int_value & 0xFFFF
            values = [high_word, low_word]

            _LOGGER.debug(
                f"Writing 32-bit value: {int_value:#x} = [{high_word:#x}, {low_word:#x}]"
            )

            success = await self._hub.async_write_registers(address, values)

        if success:
            _LOGGER.info(f"Successfully set {self._number_key} to {value}")
            # Update the state immediately (optimistic update)
            self._attr_native_value = float(value)
            self.async_write_ha_state()
            # Request coordinator to refresh data to confirm the change
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error(f"Failed to set {self._number_key} to {value}")

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        # Entity is available if coordinator succeeded
        return self.coordinator.last_update_success


class ForceDurationNumber(CoordinatorEntity, NumberEntity):
    """Number entity for Force Mode Duration with minutes<->seconds conversion.

    This entity controls register 46002 (remote_timeout_set) but displays
    the value in minutes for better user experience, while storing it in
    seconds in the register.
    """

    def __init__(
        self,
        coordinator,
        hub,
        config_entry: ConfigEntry,
        definition: dict,
        device_info: dict,
    ) -> None:
        """Initialize the force duration number entity."""
        super().__init__(coordinator)
        self._hub = hub
        self._definition = definition
        self._config_entry = config_entry
        self._device_info = device_info

        # Set unique ID and entity ID
        self._attr_unique_id = f"{config_entry.entry_id}_force_duration"
        self.entity_id = "number.solakon_one_force_duration"

        # Set basic attributes
        self._attr_name = definition["name"]
        self._attr_icon = definition.get("icon")

        # Set number attributes (in minutes)
        self._attr_native_min_value = definition.get("min", 0)
        self._attr_native_max_value = definition.get("max", 1092)
        self._attr_native_step = definition.get("step", 1)
        self._attr_native_unit_of_measurement = "min"

        # Set mode
        self._attr_mode = NumberMode.SLIDER

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get("name", "Solakon ONE"),
            manufacturer=self._device_info.get("manufacturer", "Solakon"),
            model=self._device_info.get("model", "One"),
            sw_version=self._device_info.get("version"),
            serial_number=self._device_info.get("serial_number"),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        if self.coordinator.data and "remote_timeout_set" in self.coordinator.data:
            value_seconds = self.coordinator.data["remote_timeout_set"]

            # Convert from seconds to minutes for display
            if isinstance(value_seconds, (int, float)):
                value_minutes = float(value_seconds) / 60.0
                self._attr_native_value = round(value_minutes, 1)  # Round to 1 decimal place
                _LOGGER.debug(
                    f"force_duration: Read value = {value_seconds}s = {self._attr_native_value} min"
                )
            else:
                _LOGGER.warning(
                    f"Invalid value type for remote_timeout_set: {type(value_seconds)}"
                )
                self._attr_native_value = None
        else:
            self._attr_native_value = None

        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        # Convert from minutes to seconds for writing
        value_seconds = int(value * 60)

        # Ensure value fits in uint16 range (0-65535)
        if value_seconds < 0:
            value_seconds = 0
        elif value_seconds > 65535:
            value_seconds = 65535

        address = REGISTERS["remote_timeout_set"]["address"]

        _LOGGER.info(
            f"Setting force_duration to {value} min (raw value: {value_seconds}s) at address {address}"
        )

        # Write to register 46002
        success = await self._hub.async_write_register(address, value_seconds)

        if success:
            _LOGGER.info(f"Successfully set force_duration to {value} min ({value_seconds}s)")
            # Update the state immediately (optimistic update)
            self._attr_native_value = float(value)
            self.async_write_ha_state()
            # Request coordinator to refresh data to confirm the change
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error(f"Failed to set force_duration to {value} min")

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success


class ForcePowerNumber(CoordinatorEntity, NumberEntity):
    """Number entity for Force Mode Power.

    This entity controls both registers 46003 (remote_active_power) and
    46005 (remote_reactive_power), ensuring they always have the same
    positive value as required by the force charge/discharge operation.
    """

    def __init__(
        self,
        coordinator,
        hub,
        config_entry: ConfigEntry,
        definition: dict,
        device_info: dict,
    ) -> None:
        """Initialize the force power number entity."""
        super().__init__(coordinator)
        self._hub = hub
        self._definition = definition
        self._config_entry = config_entry
        self._device_info = device_info

        # Set unique ID and entity ID
        self._attr_unique_id = f"{config_entry.entry_id}_force_power"
        self.entity_id = "number.solakon_one_force_power"

        # Set basic attributes
        self._attr_name = definition["name"]
        self._attr_icon = definition.get("icon")

        # Set number attributes
        self._attr_native_min_value = definition.get("min", 0)
        self._attr_native_max_value = definition.get("max", 1200)
        self._attr_native_step = definition.get("step", 10)

        # Set device class and unit
        self._attr_device_class = NumberDeviceClass.POWER
        self._attr_native_unit_of_measurement = UnitOfPower.WATT

        # Set mode
        self._attr_mode = NumberMode.BOX

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._config_entry.entry_id)},
            name=self._config_entry.data.get("name", "Solakon ONE"),
            manufacturer=self._device_info.get("manufacturer", "Solakon"),
            model=self._device_info.get("model", "One"),
            sw_version=self._device_info.get("version"),
            serial_number=self._device_info.get("serial_number"),
        )

    @callback
    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        # Read from register 46003 (remote_active_power)
        if self.coordinator.data and "remote_active_power" in self.coordinator.data:
            value = self.coordinator.data["remote_active_power"]

            # Always use absolute value (positive)
            if isinstance(value, (int, float)):
                self._attr_native_value = abs(float(value))
                _LOGGER.debug(
                    f"force_power: Read value = {self._attr_native_value}W"
                )
            else:
                _LOGGER.warning(
                    f"Invalid value type for remote_active_power: {type(value)}"
                )
                self._attr_native_value = None
        else:
            self._attr_native_value = None

        self.async_write_ha_state()

    async def async_set_native_value(self, value: float) -> None:
        """Update the current value."""
        # Always use positive value
        int_value = abs(int(value))

        # Validate based on current force mode
        # (You could add validation here to check if force charge is active and limit to 1200W,
        #  or if force discharge is active and limit to 800W)

        address_46003 = REGISTERS["remote_active_power"]["address"]
        address_46005 = REGISTERS["remote_reactive_power"]["address"]

        _LOGGER.info(
            f"Setting force_power to {int_value}W (writing to both 46003 and 46005)"
        )

        # Write to both registers 46003 and 46005 (32-bit values)
        # Split into high and low words (big-endian: high word first)
        high_word = (int_value >> 16) & 0xFFFF
        low_word = int_value & 0xFFFF
        values = [high_word, low_word]

        _LOGGER.debug(
            f"Writing 32-bit value: {int_value:#x} = [{high_word:#x}, {low_word:#x}]"
        )

        # Write to both registers
        success_46003 = await self._hub.async_write_registers(address_46003, values)
        success_46005 = await self._hub.async_write_registers(address_46005, values)

        if success_46003 and success_46005:
            _LOGGER.info(f"Successfully set force_power to {int_value}W")
            # Update the state immediately (optimistic update)
            self._attr_native_value = float(int_value)
            self.async_write_ha_state()
            # Request coordinator to refresh data to confirm the change
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error(
                f"Failed to set force_power to {int_value}W "
                f"(46003: {success_46003}, 46005: {success_46005})"
            )

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return self.coordinator.last_update_success