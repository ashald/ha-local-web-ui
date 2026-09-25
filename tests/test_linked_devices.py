"""The optional "<device> web UI" linked devices."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigSubentryData
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.local_web_ui.const import (
    CONF_DEVICE_ID,
    CONF_DISCOVERY,
    CONF_LINK_DEVICE_PAGES,
    CONF_LINKED_DEVICES,
    CONF_MODE,
    CONF_URL,
    DEVICE_LINK_PREFIX,
    DOMAIN,
    MODE_ISOLATED,
    SUBENTRY_TYPE_VIEW,
)

MAC = "aa:bb:cc:dd:ee:ff"


@pytest.fixture
async def setup(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "config", {})


async def _load(
    hass: HomeAssistant,
    linked: bool,
    subentries: list[ConfigSubentryData] | None = None,
    link_pages: bool = False,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        options={
            CONF_DISCOVERY: True,
            CONF_LINK_DEVICE_PAGES: link_pages,
            CONF_LINKED_DEVICES: linked,
        },
        subentries_data=subentries,
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


def _pinned(
    device_id: str, url: str = "http://192.168.1.50/admin", title: str = "Porch admin"
) -> ConfigSubentryData:
    """A web UI added by hand for a device (as the panel's "pin" does)."""
    return ConfigSubentryData(
        data={CONF_URL: url, CONF_MODE: MODE_ISOLATED, CONF_DEVICE_ID: device_id},
        subentry_type=SUBENTRY_TYPE_VIEW,
        title=title,
        unique_id=f"device:{device_id}",
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


async def _list_linked(client: Any, device_id: str) -> list[str]:
    await client.send_json_auto_id(
        {"type": "config/device_registry/list_linked_devices", "device_id": device_id}
    )
    result = await client.receive_json()
    assert result["success"], result
    return result["result"]["linked_devices"]


@pytest.mark.usefixtures("setup")
async def test_hidden_views_get_no_linked_device(hass: HomeAssistant) -> None:
    device = _owner_device(hass)
    entry = await _load(hass, linked=True)
    hub = entry.runtime_data
    assert len(_ours(hass, entry)) == 1

    hub.async_set_hidden(f"d_{device.id}", True)
    assert _ours(hass, entry) == []

    hub.async_set_hidden(f"d_{device.id}", False)
    [linked] = _ours(hass, entry)
    assert linked.identifiers == {(DOMAIN, f"d_{device.id}")}


@pytest.mark.usefixtures("setup")
async def test_disabled_target_gets_no_linked_device(hass: HomeAssistant) -> None:
    registry = dr.async_get(hass)
    device = _owner_device(hass)
    entry = await _load(hass, linked=True)
    assert len(_ours(hass, entry)) == 1

    registry.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await hass.async_block_till_done()
    assert _ours(hass, entry) == []

    registry.async_update_device(device.id, disabled_by=None)
    await hass.async_block_till_done()
    assert len(_ours(hass, entry)) == 1


@pytest.mark.usefixtures("setup")
async def test_disabled_target_of_pinned_view_gets_no_linked_device(hass: HomeAssistant) -> None:
    """A web UI added by hand for a device stays, but its linked device goes."""
    registry = dr.async_get(hass)
    device = _owner_device(hass)
    registry.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    entry = await _load(hass, linked=True, subentries=[_pinned(device.id)])
    [view] = entry.runtime_data.static_views.values()
    assert entry.runtime_data.get_view(view.view_id) is not None
    assert _ours(hass, entry) == []

    registry.async_update_device(device.id, disabled_by=None)
    await hass.async_block_till_done()
    [linked] = _ours(hass, entry)
    assert linked.identifiers == {(DOMAIN, view.view_id)}


@pytest.mark.usefixtures("setup")
async def test_pinned_view_gets_linked_device_named_after_it(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    device = _owner_device(hass)
    entry = await _load(hass, linked=True, subentries=[_pinned(device.id)])
    [view] = entry.runtime_data.static_views.values()

    [linked] = _ours(hass, entry)
    assert linked.name == "Porch admin web UI"
    assert linked.identifiers == {(DOMAIN, view.view_id)}
    assert linked.connections == {(dr.CONNECTION_NETWORK_MAC, MAC)}
    assert linked.configuration_url == f"{DEVICE_LINK_PREFIX}{view.view_id}"
    assert linked.manufacturer == "Local Web UIs"
    assert linked.model == "Web UI"
    # No second one for the device's discovered web UI
    assert entry.runtime_data.discovered_views(include_hidden=True) == {}

    client = await hass_ws_client(hass)
    assert await _list_linked(client, device.id) == [linked.id]

    # Renaming the web UI renames its linked device, in place
    hass.config_entries.async_update_subentry(entry, entry.subentries[view.view_id], title="Porch")
    await hass.async_block_till_done()
    [renamed] = _ours(hass, entry)
    assert renamed.id == linked.id
    assert renamed.name == "Porch web UI"


@pytest.mark.usefixtures("setup")
async def test_targets_sharing_a_mac_get_separate_linked_devices(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator
) -> None:
    """One physical device, two integrations, two web UIs: two linked devices."""
    registry = dr.async_get(hass)
    first = _owner_device(hass)
    other = MockConfigEntry(domain="other_devices")
    other.add_to_hass(hass)
    second = registry.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("other_devices", "porch")},
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        name="Porch Light (other)",
        configuration_url="http://192.168.1.50:8080/",
    )
    assert first.id != second.id
    entry = await _load(hass, linked=True)

    linked = {
        next(i for d, i in device.identifiers if d == DOMAIN): device
        for device in _ours(hass, entry)
    }
    assert set(linked) == {f"d_{first.id}", f"d_{second.id}"}
    first_linked = linked[f"d_{first.id}"]
    second_linked = linked[f"d_{second.id}"]
    assert first_linked.id != second_linked.id
    assert first_linked.name == "Porch Light web UI"
    assert second_linked.name == "Porch Light (other) web UI"
    # The first one takes the shared connection; the second is matched by identifiers
    assert first_linked.connections == {(dr.CONNECTION_NETWORK_MAC, MAC)}
    assert second_linked.connections == set()
    assert second_linked.identifiers == {
        (DOMAIN, f"d_{second.id}"),
        ("other_devices", "porch"),
    }

    client = await hass_ws_client(hass)
    assert first_linked.id in await _list_linked(client, first.id)
    assert second_linked.id in await _list_linked(client, second.id)

    # Stable across re-syncs: nothing is removed and created again
    entry.runtime_data.async_update_config()
    assert {device.id for device in _ours(hass, entry)} == {first_linked.id, second_linked.id}


@pytest.mark.usefixtures("setup")
async def test_removing_the_entry_leaves_no_linked_devices(hass: HomeAssistant) -> None:
    device = _owner_device(hass)
    entry = await _load(hass, linked=True, subentries=[_pinned(device.id, url="http://nas.lan/")])
    assert len(_ours(hass, entry)) == 1

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()

    registry = dr.async_get(hass)
    assert _ours(hass, entry) == []
    assert not [d for d in registry.devices if any(domain == DOMAIN for domain, _ in d.identifiers)]
    # The target is still there, with its own URL
    assert registry.async_get(device.id).configuration_url == "http://192.168.1.50/"


@pytest.mark.usefixtures("setup")
async def test_linked_devices_are_never_visit_linked(hass: HomeAssistant) -> None:
    device = _owner_device(hass)
    entry = await _load(hass, linked=True, link_pages=True)
    hub = entry.runtime_data
    view_id = f"d_{device.id}"

    [linked] = _ours(hass, entry)
    registry = dr.async_get(hass)
    # The target's page is linked; the linked device's own link is its view, untouched
    assert registry.async_get(device.id).configuration_url == f"{DEVICE_LINK_PREFIX}{view_id}"
    assert linked.configuration_url == f"{DEVICE_LINK_PREFIX}{view_id}"
    assert set(hub.originals) == {device.id}
    assert hub.view_for_device(linked.id) is None
    assert set(hub.discovered_views(include_hidden=True)) == {view_id}

    # Even after re-syncs and changes to it
    hub.async_sync_device_links()
    hub.async_set_device_link(linked.id, True)
    registry.async_update_device(linked.id, name_by_user="Porch page")
    await hass.async_block_till_done()
    linked = registry.async_get(linked.id)
    assert linked.configuration_url == f"{DEVICE_LINK_PREFIX}{view_id}"
    assert linked.id not in hub.originals
    assert set(hub.discovered_views(include_hidden=True)) == {view_id}
