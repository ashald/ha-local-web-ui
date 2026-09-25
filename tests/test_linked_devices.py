"""The optional "<device> web UI" linked devices."""

from __future__ import annotations

from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.local_web_ui.const import (
    CONF_DISCOVERY,
    CONF_LINK_DEVICE_PAGES,
    CONF_LINKED_DEVICES,
    DEVICE_LINK_PREFIX,
    DOMAIN,
)

MAC = "aa:bb:cc:dd:ee:ff"


@pytest.fixture
async def setup(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "config", {})


async def _load(hass: HomeAssistant, linked: bool) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DISCOVERY: True,
            CONF_LINK_DEVICE_PAGES: False,
            CONF_LINKED_DEVICES: linked,
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


def _owner_device(
    hass: HomeAssistant, connections: set[tuple[str, str]] | None = None
) -> dr.DeviceEntry:
    owner = MockConfigEntry(domain="fake_devices")
    owner.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("fake_devices", "porch")},
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)} if connections is None else connections,
        name="Porch Light",
        configuration_url="http://192.168.1.50/",
    )


def _ours(hass: HomeAssistant, entry: MockConfigEntry) -> list[dr.DeviceEntry]:
    return dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)


@pytest.mark.usefixtures("setup")
async def test_linked_device_shares_connections(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    device = _owner_device(hass)
    entry = await _load(hass, linked=True)

    [linked] = _ours(hass, entry)
    view_id = f"d_{device.id}"
    assert linked.name == "Porch Light web UI"
    assert linked.configuration_url == f"{DEVICE_LINK_PREFIX}{view_id}"
    assert linked.connections == {(dr.CONNECTION_NETWORK_MAC, MAC)}
    assert linked.identifiers == {(DOMAIN, view_id)}
    assert linked.entry_type is dr.DeviceEntryType.SERVICE
    # The device itself is left alone
    device = dr.async_get(hass).async_get(device.id)
    assert device.configuration_url == "http://192.168.1.50/"
    assert device.config_entry_id != entry.entry_id

    # Home Assistant shows it in the device page's "Linked devices" card
    client = await hass_ws_client(hass)
    await client.send_json_auto_id(
        {"type": "config/device_registry/list_linked_devices", "device_id": device.id}
    )
    result = await client.receive_json()
    assert result["success"], result
    assert result["result"]["linked_devices"] == [linked.id]


@pytest.mark.usefixtures("setup")
async def test_linked_device_without_connections_uses_identifiers(hass: HomeAssistant) -> None:
    device = _owner_device(hass, connections=set())
    entry = await _load(hass, linked=True)

    [linked] = _ours(hass, entry)
    assert linked.identifiers == {(DOMAIN, f"d_{device.id}"), ("fake_devices", "porch")}
    assert linked.connections == set()


@pytest.mark.usefixtures("setup")
async def test_linked_devices_follow_the_option_and_the_device(hass: HomeAssistant) -> None:
    device = _owner_device(hass)
    entry = await _load(hass, linked=False)
    assert _ours(hass, entry) == []

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_LINKED_DEVICES: True}
    )
    await hass.async_block_till_done()
    assert len(_ours(hass, entry)) == 1

    # Renamed with the device
    dr.async_get(hass).async_update_device(device.id, name_by_user="Front Porch")
    await hass.async_block_till_done()
    [linked] = _ours(hass, entry)
    assert linked.name == "Front Porch web UI"

    # Gone with the device's web UI
    dr.async_get(hass).async_update_device(device.id, configuration_url=None)
    await hass.async_block_till_done()
    assert _ours(hass, entry) == []

    dr.async_get(hass).async_update_device(device.id, configuration_url="http://192.168.1.50/")
    await hass.async_block_till_done()
    assert len(_ours(hass, entry)) == 1

    hass.config_entries.async_update_entry(
        entry, options={**entry.options, CONF_LINKED_DEVICES: False}
    )
    await hass.async_block_till_done()
    assert _ours(hass, entry) == []
