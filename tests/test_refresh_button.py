"""Tests for the manual refresh button."""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

import sys
import os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from homeassistant.exceptions import HomeAssistantError

from custom_components.lxp_modbus import button as button_module
from custom_components.lxp_modbus.button import (
    LxpRefreshButton,
    ModbusBridgeButton,
    async_setup_entry,
)
from custom_components.lxp_modbus.classes.modbus_client import LxpModbusApiClient
from custom_components.lxp_modbus.const import DOMAIN, CONF_READ_ONLY, CONF_ENTITY_PREFIX
from custom_components.lxp_modbus.entity_descriptions.button_types import BUTTON_TYPES

ENTRY_ID = "test_entry_id"


@pytest.fixture
def entry():
    cfg = MagicMock()
    cfg.entry_id = ENTRY_ID
    cfg.title = "Test Inverter"
    cfg.data = {}
    return cfg


@pytest.fixture
def api_client():
    """Client whose success timestamp advances only when a poll really connects."""
    client = MagicMock()
    client.last_successful_connection = 1000.0
    client.request_hold_refresh = MagicMock()
    return client


@pytest.fixture
def coordinator(api_client):
    coord = MagicMock()
    coord.data = {"hold": {}, "input": {}}
    coord.last_update_success = True

    async def successful_refresh():
        api_client.last_successful_connection += 30.0

    coord.async_refresh = AsyncMock(side_effect=successful_refresh)
    coord.async_request_refresh = AsyncMock()
    coord.hass = MagicMock()
    coord.hass.data = {DOMAIN: {ENTRY_ID: {"write_lock": asyncio.Lock()}}}
    return coord


def make_button(coordinator, entry, api_client):
    btn = LxpRefreshButton(coordinator, entry, "lxp", api_client)
    # Not attached to a running hass in these unit tests.
    btn.async_write_ha_state = MagicMock()
    return btn


class TestRefreshButtonIdentity:
    def test_unique_id_and_name_follow_integration_convention(self, coordinator, entry, api_client):
        btn = make_button(coordinator, entry, api_client)

        assert btn.unique_id == "lxp_refresh_data"
        assert btn.name == "lxp Refresh Data"
        assert btn.icon == "mdi:refresh"

    def test_attaches_to_the_main_inverter_device(self, coordinator, entry, api_client):
        btn = make_button(coordinator, entry, api_client)

        assert btn.device_info["identifiers"] == {(DOMAIN, ENTRY_ID)}

    def test_does_not_poll(self, coordinator, entry, api_client):
        btn = make_button(coordinator, entry, api_client)

        assert btn.should_poll is False

    def test_stays_available_when_the_inverter_is_unreachable(self, coordinator, entry, api_client):
        """A manual retry is most useful exactly while polling is failing."""
        coordinator.last_update_success = False
        btn = make_button(coordinator, entry, api_client)

        assert btn.available is True


class TestRefreshButtonPress:
    async def test_press_polls_without_the_debouncer(self, coordinator, entry, api_client):
        btn = make_button(coordinator, entry, api_client)

        await btn.async_press()

        coordinator.async_refresh.assert_awaited_once()
        # The debounced path could silently defer the poll, so it must not be used.
        coordinator.async_request_refresh.assert_not_awaited()

    async def test_settings_are_left_for_the_next_scheduled_poll(self, coordinator, entry, api_client):
        """The press reads live registers only; hold is requested afterwards."""
        order = []
        api_client.request_hold_refresh.side_effect = lambda: order.append("hold")

        async def refresh():
            order.append("poll")
            api_client.last_successful_connection += 1

        coordinator.async_refresh.side_effect = refresh
        btn = make_button(coordinator, entry, api_client)

        await btn.async_press()

        assert order == ["poll", "hold"]

    async def test_attributes_report_progress_and_success(self, coordinator, entry, api_client):
        btn = make_button(coordinator, entry, api_client)
        seen_during_poll = {}

        async def refresh():
            seen_during_poll.update(btn.extra_state_attributes)
            api_client.last_successful_connection += 1

        coordinator.async_refresh.side_effect = refresh
        assert btn.extra_state_attributes == {
            "refreshing": False, "last_result": None, "last_refresh": None,
        }

        await btn.async_press()

        assert seen_during_poll["refreshing"] is True
        attrs = btn.extra_state_attributes
        assert attrs["refreshing"] is False
        assert attrs["last_result"] == "ok"
        assert attrs["last_refresh"] is not None
        # Written once when the refresh starts and once when it ends.
        assert btn.async_write_ha_state.call_count == 2

    async def test_press_raises_when_the_dongle_was_not_reached(self, coordinator, entry, api_client):
        """A failed poll answered from cache must not look like a successful refresh."""
        coordinator.async_refresh.side_effect = None  # timestamp never advances
        btn = make_button(coordinator, entry, api_client)

        with pytest.raises(HomeAssistantError):
            await btn.async_press()

        attrs = btn.extra_state_attributes
        assert attrs["refreshing"] is False
        assert attrs["last_result"] == "failed"
        assert attrs["last_refresh"] is None
        # Settings are still queued for the next poll after a failure.
        api_client.request_hold_refresh.assert_called_once()

    async def test_press_during_a_running_refresh_is_ignored(self, coordinator, entry, api_client):
        release = asyncio.Event()

        async def slow_refresh():
            await release.wait()
            api_client.last_successful_connection += 1

        coordinator.async_refresh.side_effect = slow_refresh
        btn = make_button(coordinator, entry, api_client)

        first = asyncio.create_task(btn.async_press())
        await asyncio.sleep(0)
        await btn.async_press()  # returns immediately
        release.set()
        await first

        coordinator.async_refresh.assert_awaited_once()

    async def test_press_inside_cooldown_is_ignored(self, coordinator, entry, api_client):
        btn = make_button(coordinator, entry, api_client)

        await btn.async_press()
        await btn.async_press()

        coordinator.async_refresh.assert_awaited_once()

    async def test_press_after_cooldown_polls_again(self, coordinator, entry, api_client):
        btn = make_button(coordinator, entry, api_client)
        await btn.async_press()

        btn._last_refresh_finished -= button_module.MANUAL_REFRESH_COOLDOWN + 1
        await btn.async_press()

        assert coordinator.async_refresh.await_count == 2


class TestButtonPlatformSetup:
    def _hass(self, coordinator, api_client, read_only):
        hass = MagicMock()
        hass.data = {
            DOMAIN: {
                ENTRY_ID: {
                    "coordinator": coordinator,
                    "api_client": api_client,
                    "settings": {CONF_READ_ONLY: read_only, CONF_ENTITY_PREFIX: "lxp"},
                    "write_lock": asyncio.Lock(),
                    "main_device_id": "device-1",
                }
            }
        }
        coordinator.hass = hass
        return hass

    async def test_read_only_mode_creates_only_the_refresh_button(self, coordinator, entry, api_client):
        hass = self._hass(coordinator, api_client, read_only=True)
        added = []

        await async_setup_entry(hass, entry, added.extend)

        assert len(added) == 1
        assert isinstance(added[0], LxpRefreshButton)

    async def test_normal_mode_adds_the_write_buttons_too(self, coordinator, entry, api_client):
        hass = self._hass(coordinator, api_client, read_only=False)
        added = []

        await async_setup_entry(hass, entry, added.extend)

        assert sum(isinstance(e, LxpRefreshButton) for e in added) == 1
        assert sum(isinstance(e, ModbusBridgeButton) for e in added) == len(BUTTON_TYPES)


class TestClientSuccessTimestamp:
    def test_starts_empty(self):
        client = LxpModbusApiClient(
            "192.0.2.1", 8000, "BA0000000A", "0000000001", asyncio.Lock()
        )

        assert client.last_successful_connection is None
