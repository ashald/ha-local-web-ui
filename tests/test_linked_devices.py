"""The "<title> web UI" device every web UI entry owns."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntryState
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
from pytest_homeassistant_custom_component.typing import WebSocketGenerator

from custom_components.local_web_ui.const import (
    CONF_DEVICE_ID,
    CONF_DISCOVERY,
    CONF_KIND,
    CONF_LINK_DEVICE_PAGES,
    CONF_MODE,
    CONF_SHOW_IN_SIDEBAR,
    CONF_SHOW_PANEL,
    CONF_TRUSTED_ACK,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEVICE_LINK_PREFIX,
    DOMAIN,
    HUB_UNIQUE_ID,
    KIND_HUB,
    KIND_VIEW,
    MODE_ISOLATED,
)
from custom_components.local_web_ui.hub import device_unique_id

MAC = "aa:bb:cc:dd:ee:ff"
OWNER_DOMAIN = "fake_devices"
PORCH_URL = "http://192.168.1.50/"


@pytest.fixture
async def setup(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})
    assert await async_setup_component(hass, "config", {})


@pytest.fixture
def owner(hass: HomeAssistant) -> MockConfigEntry:
    entry = MockConfigEntry(domain=OWNER_DOMAIN)
    entry.add_to_hass(hass)
    return entry


def _owner_device(
    hass: HomeAssistant,
    owner: MockConfigEntry,
    connections: set[tuple[str, str]] | None = None,
    key: str = "porch",
    url: str = PORCH_URL,
) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={(OWNER_DOMAIN, key)},
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)} if connections is None else connections,
        name="Porch Light",
        configuration_url=url,
    )


async def _setup(hass: HomeAssistant, entry: MockConfigEntry) -> MockConfigEntry:
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def _hub_entry(**options: Any) -> MockConfigEntry:
    return MockConfigEntry(
        domain=DOMAIN,
        version=2,
        title="Local Web UIs",
        unique_id=HUB_UNIQUE_ID,
        data={CONF_KIND: KIND_HUB},
        options={
            CONF_DISCOVERY: False,
            CONF_LINK_DEVICE_PAGES: True,
            CONF_SHOW_PANEL: True,
            **options,
        },
    )


async def _device_web_ui(
    hass: HomeAssistant, device: dr.DeviceEntry, title: str = "Porch Light"
) -> MockConfigEntry:
    return await _setup(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            version=2,
            title=title,
            unique_id=device_unique_id(device.id),
            source=SOURCE_INTEGRATION_DISCOVERY,
            data={CONF_KIND: KIND_VIEW, CONF_DEVICE_ID: device.id},
            options={CONF_MODE: MODE_ISOLATED, CONF_VERIFY_SSL: False},
        ),
    )


async def _manual_web_ui(
    hass: HomeAssistant, title: str = "NAS", url: str = "http://nas.lan:5000/"
) -> MockConfigEntry:
    return await _setup(
        hass,
        MockConfigEntry(
            domain=DOMAIN,
            version=2,
            title=title,
            data={CONF_KIND: KIND_VIEW},
            options={CONF_URL: url, CONF_MODE: MODE_ISOLATED, CONF_VERIFY_SSL: True},
        ),
    )


def _ours(hass: HomeAssistant, entry: MockConfigEntry) -> list[dr.DeviceEntry]:
    return dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)


def _all_ours(hass: HomeAssistant) -> list[dr.DeviceEntry]:
    return [
        device
        for device in dr.async_get(hass).devices
        if any(domain == DOMAIN for domain, _ in device.identifiers)
    ]


async def _list_linked(client: Any, device_id: str) -> list[str]:
    await client.send_json_auto_id(
        {"type": "config/device_registry/list_linked_devices", "device_id": device_id}
    )
    result = await client.receive_json()
    assert result["success"], result
    return result["result"]["linked_devices"]


@pytest.mark.usefixtures("setup")
async def test_web_ui_device_shares_the_target_connections(
    hass: HomeAssistant, owner: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    device = _owner_device(hass, owner)
    await _setup(hass, _hub_entry())
    entry = await _device_web_ui(hass, device)

    [ours] = _ours(hass, entry)
    assert ours.name == "Porch Light web UI"
    assert ours.identifiers == {(DOMAIN, entry.entry_id)}
    assert ours.connections == {(dr.CONNECTION_NETWORK_MAC, MAC)}
    assert ours.entry_type is dr.DeviceEntryType.SERVICE
    assert ours.manufacturer == "Local Web UIs"
    assert ours.model == "Web UI"
    assert ours.configuration_url == f"{DEVICE_LINK_PREFIX}{entry.entry_id}"
    assert ours.id != device.id
    assert entry.runtime_data.linked_device_id(entry.entry_id) == ours.id

    client = await hass_ws_client(hass)
    assert await _list_linked(client, device.id) == [ours.id]


@pytest.mark.usefixtures("setup")
async def test_web_ui_device_without_connections_uses_identifiers(
    hass: HomeAssistant, owner: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    device = _owner_device(hass, owner, connections=set())
    entry = await _device_web_ui(hass, device)

    [ours] = _ours(hass, entry)
    assert ours.connections == set()
    assert ours.identifiers == {(DOMAIN, entry.entry_id), (OWNER_DOMAIN, "porch")}

    client = await hass_ws_client(hass)
    assert await _list_linked(client, device.id) == [ours.id]


@pytest.mark.usefixtures("setup")
async def test_web_ui_device_follows_the_target(
    hass: HomeAssistant, owner: MockConfigEntry
) -> None:
    """Its connections follow the target's; one device per entry all along."""
    registry = dr.async_get(hass)
    device = _owner_device(hass, owner, connections=set())
    entry = await _device_web_ui(hass, device)

    registry.async_update_device(device.id, new_connections={(dr.CONNECTION_NETWORK_MAC, MAC)})
    await hass.async_block_till_done()
    [ours] = _ours(hass, entry)
    assert ours.connections == {(dr.CONNECTION_NETWORK_MAC, MAC)}
    assert ours.identifiers == {(DOMAIN, entry.entry_id)}

    # Target gone: the entry stays, with a device of its own
    registry.async_remove_device(device.id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    [ours] = _ours(hass, entry)
    assert ours.connections == set()
    assert ours.identifiers == {(DOMAIN, entry.entry_id)}
    assert ours.name == "Porch Light web UI"


@pytest.mark.usefixtures("setup")
async def test_disabled_target_keeps_the_web_ui_device(
    hass: HomeAssistant, owner: MockConfigEntry
) -> None:
    registry = dr.async_get(hass)
    device = _owner_device(hass, owner)
    entry = await _device_web_ui(hass, device)
    [ours] = _ours(hass, entry)

    registry.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await hass.async_block_till_done()
    assert entry.runtime_data.get_view(entry.entry_id) is None
    assert [d.id for d in _ours(hass, entry)] == [ours.id]

    registry.async_update_device(device.id, disabled_by=None)
    await hass.async_block_till_done()
    assert [d.id for d in _ours(hass, entry)] == [ours.id]


@pytest.mark.usefixtures("setup")
async def test_web_ui_device_is_renamed_with_the_entry(
    hass: HomeAssistant, owner: MockConfigEntry
) -> None:
    device = _owner_device(hass, owner)
    entry = await _device_web_ui(hass, device)
    [ours] = _ours(hass, entry)

    hass.config_entries.async_update_entry(entry, title="Porch")
    await hass.async_block_till_done()
    [renamed] = _ours(hass, entry)
    assert renamed.id == ours.id
    assert renamed.name == "Porch web UI"
    assert entry.runtime_data.get_view(entry.entry_id).name == "Porch"

    # A web UI added by URL is renamed in its options
    manual = await _manual_web_ui(hass)
    [nas] = _ours(hass, manual)
    assert nas.name == "NAS web UI"
    result = await hass.config_entries.options.async_init(manual.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        {
            "name": "Storage",
            CONF_URL: "http://nas.lan:5000/",
            CONF_MODE: MODE_ISOLATED,
            CONF_TRUSTED_ACK: False,
            CONF_VERIFY_SSL: True,
            CONF_SHOW_IN_SIDEBAR: False,
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    await hass.async_block_till_done()
    assert manual.title == "Storage"
    [renamed] = _ours(hass, manual)
    assert renamed.id == nas.id
    assert renamed.name == "Storage web UI"


@pytest.mark.usefixtures("setup")
async def test_manual_web_ui_gets_a_standalone_device(
    hass: HomeAssistant, owner: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    # A device at the same address is not its target
    device = _owner_device(hass, owner, url="http://nas.lan:5000/")
    entry = await _manual_web_ui(hass)

    [ours] = _ours(hass, entry)
    assert ours.name == "NAS web UI"
    assert ours.identifiers == {(DOMAIN, entry.entry_id)}
    assert ours.connections == set()
    assert ours.configuration_url == f"{DEVICE_LINK_PREFIX}{entry.entry_id}"
    assert ours.entry_type is dr.DeviceEntryType.SERVICE

    client = await hass_ws_client(hass)
    assert await _list_linked(client, device.id) == []


@pytest.mark.usefixtures("setup")
async def test_targets_sharing_a_mac_get_separate_web_ui_devices(
    hass: HomeAssistant, owner: MockConfigEntry, hass_ws_client: WebSocketGenerator
) -> None:
    """One physical device, two integrations, two web UIs: two devices of ours."""
    registry = dr.async_get(hass)
    first = _owner_device(hass, owner)
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
    first_entry = await _device_web_ui(hass, first)
    second_entry = await _device_web_ui(hass, second, title="Porch Light (other)")

    [first_ours] = _ours(hass, first_entry)
    [second_ours] = _ours(hass, second_entry)
    assert first_ours.id != second_ours.id
    assert first_ours.name == "Porch Light web UI"
    assert second_ours.name == "Porch Light (other) web UI"

    client = await hass_ws_client(hass)
    assert first_ours.id in await _list_linked(client, first.id)
    assert second_ours.id in await _list_linked(client, second.id)

    # Stable across refreshes: nothing is removed and created again
    first_entry.runtime_data.async_refresh()
    await hass.async_block_till_done()
    assert {device.id for device in _all_ours(hass)} == {first_ours.id, second_ours.id}


@pytest.mark.usefixtures("setup")
async def test_deleting_the_entry_removes_its_device(
    hass: HomeAssistant, owner: MockConfigEntry
) -> None:
    registry = dr.async_get(hass)
    device = _owner_device(hass, owner)
    await _setup(hass, _hub_entry())
    entry = await _device_web_ui(hass, device)
    manual = await _manual_web_ui(hass)
    assert len(_all_ours(hass)) == 2

    await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert _ours(hass, entry) == []
    assert [d.name for d in _all_ours(hass)] == ["NAS web UI"]
    # The target is still there, with its own URL
    assert registry.async_get(device.id).configuration_url == PORCH_URL

    await hass.config_entries.async_remove(manual.entry_id)
    await hass.async_block_till_done()
    assert _all_ours(hass) == []


@pytest.mark.usefixtures("setup")
async def test_hub_owns_no_devices(hass: HomeAssistant, owner: MockConfigEntry) -> None:
    """The hub has no device; linked devices it kept before 0.3 are removed."""
    registry = dr.async_get(hass)
    device = _owner_device(hass, owner)
    hub_entry = _hub_entry()
    hub_entry.add_to_hass(hass)
    old = registry.async_get_or_create(
        config_entry_id=hub_entry.entry_id,
        identifiers={(DOMAIN, f"d_{device.id}")},
        connections={(dr.CONNECTION_NETWORK_MAC, MAC)},
        name="Porch Light web UI",
    )
    assert await hass.config_entries.async_setup(hub_entry.entry_id)
    await hass.async_block_till_done()

    assert registry.async_get(old.id) is None
    assert _ours(hass, hub_entry) == []

    entry = await _device_web_ui(hass, device)
    await _manual_web_ui(hass)
    assert _ours(hass, hub_entry) == []
    assert {d.name for d in _all_ours(hass)} == {"Porch Light web UI", "NAS web UI"}
    assert len(_ours(hass, entry)) == 1


@pytest.mark.usefixtures("setup")
async def test_web_ui_devices_are_never_visit_linked(
    hass: HomeAssistant, owner: MockConfigEntry
) -> None:
    registry = dr.async_get(hass)
    device = _owner_device(hass, owner)
    entry = await _device_web_ui(hass, device)
    hub_entry = await _setup(hass, _hub_entry(**{CONF_DISCOVERY: True}))
    hub = entry.runtime_data
    link = f"{DEVICE_LINK_PREFIX}{entry.entry_id}"

    [ours] = _ours(hass, entry)
    # The target's page is linked; our device's own link is the web UI, untouched
    assert registry.async_get(device.id).configuration_url == link
    assert ours.configuration_url == link
    assert set(hub.originals) == {device.id}
    assert hub.view_for_device(ours.id) is None

    # Even after refreshes and changes to it
    registry.async_update_device(ours.id, name_by_user="Porch page")
    hass.config_entries.async_update_entry(
        hub_entry, options={**hub_entry.options, CONF_LINK_DEVICE_PAGES: False}
    )
    await hass.async_block_till_done()
    assert registry.async_get(device.id).configuration_url == PORCH_URL
    ours = registry.async_get(ours.id)
    assert ours.configuration_url == link
    assert ours.name_by_user == "Porch page"
    assert hub.originals == {}
    assert set(hub.views) == {entry.entry_id}
    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []
