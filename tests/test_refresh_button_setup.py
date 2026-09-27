"""End-to-end check that the refresh button loads through a real config entry."""

from unittest.mock import AsyncMock, patch

import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry

from homeassistant.helpers import entity_registry as er

from custom_components.lxp_modbus.const import DOMAIN

INPUT = {reg: 0 for reg in range(0, 120)}
HOLD = {reg: 0 for reg in range(0, 120)}


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    yield


def _entry(read_only: bool) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        title="lxp",
        data={
            "host": "192.0.2.10",
            "port": 8000,
            "dongle_serial": "BA00000000",
            "inverter_serial": "0000000000",
            "poll_interval": 30,
            "entity_prefix": "lxp",
            "rated_power": 5000,
            "read_only": read_only,
            "register_block_size": 125,
            "connection_retries": 3,
            "enable_device_grouping": True,
            "battery_entities": "none",
            "battery_voltage_class": 48,
        },
    )


@pytest.mark.parametrize("read_only", [True, False])
async def test_refresh_button_is_created_and_polls(hass, read_only):
    entry = _entry(read_only)
    entry.add_to_hass(hass)

    calls = {"n": 0}

    async def fake_get_data(self):
        calls["n"] += 1
        self._last_successful_connection = 1_000.0 + calls["n"]
        self._last_good_input_regs.update(INPUT)
        self._last_good_hold_regs.update(HOLD)
        return self._snapshot()

    with patch(
        "custom_components.lxp_modbus.classes.modbus_client.LxpModbusApiClient.async_get_data",
        fake_get_data,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

        registry = er.async_get(hass)
        entity_id = registry.async_get_entity_id("button", DOMAIN, "lxp_refresh_data")
        assert entity_id == "button.lxp_refresh_data"

        buttons = [e for e in er.async_entries_for_config_entry(registry, entry.entry_id)
                   if e.domain == "button"]
        if read_only:
            assert [e.unique_id for e in buttons] == ["lxp_refresh_data"]
        else:
            assert len(buttons) > 1

        before = calls["n"]
        await hass.services.async_call(
            "button", "press", {"entity_id": entity_id}, blocking=True
        )
        assert calls["n"] == before + 1
        assert hass.states.get(entity_id).state not in ("unknown", "unavailable")

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
