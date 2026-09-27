"""Button platform for the LuxPower Modbus integration."""
import asyncio
import logging
import time as time_lib

from homeassistant.components.button import ButtonEntity
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import (
    DOMAIN,
    CONF_ENTITY_PREFIX,
    DEFAULT_ENTITY_PREFIX,
    CONF_READ_ONLY,
    DEFAULT_READ_ONLY,
)
from .entity import ModbusBridgeEntity
from .entity_descriptions.button_types import BUTTON_TYPES

_LOGGER = logging.getLogger(__name__)

# Presses arriving this soon after a finished manual refresh are ignored. The data
# is at most this old anyway, and the Wi-Fi dongle only serves one TCP client at a
# time, so an automation or a double-tap must not queue back-to-back full polls.
MANUAL_REFRESH_COOLDOWN = 5  # seconds


async def async_setup_entry(hass, entry, async_add_entities):
    """Set up button entities from a config entry."""
    entry_data = hass.data[DOMAIN][entry.entry_id]
    coordinator = entry_data["coordinator"]
    settings = entry_data["settings"]
    entity_prefix = settings.get(CONF_ENTITY_PREFIX, DEFAULT_ENTITY_PREFIX)
    api_client = entry_data["api_client"]
    # Same source __init__ uses to decide which platforms to load.
    is_read_only = settings.get(CONF_READ_ONLY, DEFAULT_READ_ONLY)

    # The refresh button only reads from the inverter, so it is created in
    # read-only mode as well. Everything in BUTTON_TYPES writes a register.
    entities = [LxpRefreshButton(coordinator, entry, entity_prefix, api_client)]

    if not is_read_only:
        entities.extend(
            ModbusBridgeButton(coordinator, entry, desc, entity_prefix, api_client)
            for desc in BUTTON_TYPES
        )

    async_add_entities(entities)


class ModbusBridgeButton(ModbusBridgeEntity, ButtonEntity):
    """Represents a button entity that writes a value to a register."""

    def __init__(self, coordinator: DataUpdateCoordinator, entry, desc: dict, entity_prefix: str, api_client):
        """Initialize the button entity."""
        super().__init__(coordinator, entry, desc, entity_prefix, api_client)

        # Store the function that determines the value to write when pressed
        self._press = desc["press"]
        self._attr_icon = desc.get("icon")

    async def async_press(self) -> None:
        """Handle the button press action."""
        # The press function may need the current register value, so it composes under
        # the shared write lock and the coordinator is refreshed afterwards.
        await self._async_write_register(lambda current: self._press(current))


class LxpRefreshButton(ButtonEntity):
    """Poll the inverter immediately instead of waiting for the next interval.

    Deliberately not a CoordinatorEntity: the button has no state derived from the
    coordinator, so listening would only cost a state write on every poll, and it
    must stay available while the inverter is unreachable, which is exactly when
    a manual retry is wanted.
    """

    _attr_icon = "mdi:refresh"
    _attr_should_poll = False

    def __init__(self, coordinator: DataUpdateCoordinator, entry, entity_prefix: str, api_client):
        """Initialize the refresh button."""
        self._coordinator = coordinator
        self._api_client = api_client
        self._entry = entry
        self._refresh_lock = asyncio.Lock()
        self._last_refresh_finished = 0.0

        # Naming follows the rest of the integration (prefix in the name, no
        # has_entity_name), so the entity_id becomes button.<prefix>_refresh_data.
        self._attr_name = f"{entity_prefix} Refresh Data"
        self._attr_unique_id = f"{entity_prefix}_refresh_data"
        # The parent inverter device is registered in async_setup_entry before any
        # platform loads, so the identifier alone is enough to attach to it.
        self._attr_device_info = DeviceInfo(identifiers={(DOMAIN, entry.entry_id)})

    async def async_press(self) -> None:
        """Run a full poll now, including the settings (hold) registers."""
        if self._refresh_lock.locked():
            _LOGGER.debug("Manual refresh already running, ignoring extra press")
            return

        since_last = time_lib.monotonic() - self._last_refresh_finished
        if since_last < MANUAL_REFRESH_COOLDOWN:
            _LOGGER.debug("Manual refresh finished %.1fs ago, ignoring press", since_last)
            return

        previous_success = self._api_client.last_successful_connection

        async with self._refresh_lock:
            try:
                # Settings registers are normally re-read only every Nth poll. A
                # manual refresh should show the inverter's real settings too, so
                # use the same hook the write path uses to force them in.
                self._api_client.request_hold_refresh()

                _LOGGER.debug("Manual refresh requested for %s", self._entry.title)
                # async_refresh bypasses the request debouncer, so this press is
                # guaranteed to poll instead of being deferred by a cooldown, and
                # the coordinator reschedules its next automatic poll from now.
                await self._coordinator.async_refresh()
            finally:
                self._last_refresh_finished = time_lib.monotonic()

        # A failed poll can still return cached data for a couple of cycles, which
        # leaves last_update_success True. Only a new successful connection proves
        # the dongle was actually reached.
        if self._api_client.last_successful_connection == previous_success:
            raise HomeAssistantError(
                "Could not reach the LuxPower dongle. Values shown are the last "
                "successfully read data; check the dongle connection and logs."
            )
