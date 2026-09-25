"""Config flow, options flows, migration and panels of Local Web UIs (0.3 entries)."""

from __future__ import annotations

import hashlib
import json
import logging
from pathlib import Path
import re
from types import MappingProxyType
from typing import Any
from unittest.mock import patch

from homeassistant.components import websocket_api
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_IMPORT,
    SOURCE_INTEGRATION_DISCOVERY,
    SOURCE_USER,
    ConfigEntry,
    ConfigEntryDisabler,
    ConfigEntryState,
    ConfigSubentryData,
)
from homeassistant.const import EVENT_PANELS_UPDATED
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    MockUser,
    async_capture_events,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    WebSocketGenerator,
)
import voluptuous as vol

from custom_components.local_web_ui import async_migrate_entry, config_flow
from custom_components.local_web_ui.const import (
    CONF_DEVICE_ID,
    CONF_DISCOVERY,
    CONF_ICON,
    CONF_KIND,
    CONF_LINK_DEVICE_PAGES,
    CONF_LINKED_DEVICES,
    CONF_MODE,
    CONF_PANEL_ICON,
    CONF_PANEL_TITLE,
    CONF_PASSWORD,
    CONF_PREVIOUS_VIEW_ID,
    CONF_SHOW_IN_SIDEBAR,
    CONF_SHOW_PANEL,
    CONF_TRUSTED_ACK,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    CONF_VISIT_LINK,
    DEFAULT_PANEL_ICON,
    DEVICE_LINK_PREFIX,
    DOMAIN,
    HUB_UNIQUE_ID,
    KIND_HUB,
    KIND_VIEW,
    MODE_ISOLATED,
    MODE_TRUSTED,
    MODES,
    NAME,
    PANEL_COMPONENT,
    PANEL_URL_PATH,
    PROXY_URL_PREFIX,
    STATIC_URL_PATH,
    STORAGE_KEY,
    STORAGE_KEY_JAR,
    SUBENTRY_TYPE_VIEW,
    VISIT_CHOICES,
    VISIT_DEFAULT,
    VISIT_DEVICE,
    VISIT_HERE,
)
from custom_components.local_web_ui.hub import (
    SOURCE_DEVICE,
    SOURCE_MANUAL,
    LocalWebUiHub,
    basic_authorization,
    device_unique_id,
)

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / DOMAIN
PANEL_FILE = COMPONENT_DIR / "www" / "local-web-ui-panel.js"
CONF_NAME = "name"

HUB_OPTIONS: dict[str, bool] = {
    CONF_DISCOVERY: True,
    CONF_LINK_DEVICE_PAGES: True,
    CONF_SHOW_PANEL: True,
}

VIEW_INPUT: dict[str, Any] = {
    CONF_NAME: "Router",
    CONF_URL: "http://192.168.1.1/admin/?tab=1",
    CONF_MODE: MODE_ISOLATED,
    CONF_TRUSTED_ACK: False,
    CONF_VERIFY_SSL: True,
    CONF_SHOW_IN_SIDEBAR: False,
}

# What the options form of a device's web UI sends when nothing is changed
DEVICE_OPTIONS_INPUT: dict[str, Any] = {
    CONF_MODE: MODE_ISOLATED,
    CONF_TRUSTED_ACK: False,
    CONF_VISIT_LINK: VISIT_DEFAULT,
    CONF_VERIFY_SSL: False,
    CONF_SHOW_IN_SIDEBAR: False,
}

DEVICE_URL = "http://192.168.1.50/"

# hassfest (script/hassfest/translations.py): translations must not contain URLs
RE_URL = re.compile(
    r"(((ftp|ftps|scp|http|https|mqtt|mqtts|socket|socks5):\/\/|www\.)"
    r"[a-z0-9]+([\-\.]{1}[a-z0-9]+)*\.[a-z]{2,5}(:[0-9]{1,5})?(\/.*)?)",
    re.IGNORECASE,
)


# ---- helpers -----------------------------------------------------------------


def _panels(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.get("frontend_panels", {})


def _view_panel_path(view_id: str) -> str:
    return f"{PANEL_URL_PATH}-{view_id.lower()}"


def _fields(schema: vol.Schema | None) -> dict[str, vol.Marker]:
    return {} if schema is None else {str(marker): marker for marker in schema.schema}


def _default(marker: vol.Marker) -> Any:
    return vol.UNDEFINED if marker.default is vol.UNDEFINED else marker.default()


def _suggested(marker: vol.Marker) -> Any:
    return (marker.description or {}).get("suggested_value")


def _form_value(marker: vol.Marker) -> Any:
    """What the frontend prefills: suggested_value wins over the default."""
    suggested = _suggested(marker)
    return suggested if suggested is not None else _default(marker)


def _entries(hass: HomeAssistant, include_ignore: bool = False) -> list[ConfigEntry]:
    return hass.config_entries.async_entries(DOMAIN, include_ignore=include_ignore)


def _view_entries(hass: HomeAssistant) -> list[ConfigEntry]:
    return [e for e in _entries(hass) if e.data.get(CONF_KIND) == KIND_VIEW]


def _hub(hass: HomeAssistant) -> LocalWebUiHub:
    return hass.data[DOMAIN]


def _discovery_flows(hass: HomeAssistant) -> list[dict[str, Any]]:
    return hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, match_context={"source": SOURCE_INTEGRATION_DISCOVERY}
    )


async def _create_hub(hass: HomeAssistant) -> ConfigEntry:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hub"
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    return result["result"]


async def _start_web_ui_flow(hass: HomeAssistant) -> dict[str, Any]:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "web_ui"
    return result


async def _add_web_ui(
    hass: HomeAssistant, user_input: dict[str, Any]
) -> tuple[dict[str, Any], ConfigEntry | None]:
    """Run the manual add flow; return the last result and the new entry, if any."""
    before = {e.entry_id for e in _entries(hass)}
    result = await _start_web_ui_flow(hass)
    result = await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    new = [e for e in _entries(hass) if e.entry_id not in before]
    assert len(new) <= 1
    return result, (new[0] if new else None)


async def _add_router(hass: HomeAssistant, **changes: Any) -> ConfigEntry:
    result, entry = await _add_web_ui(hass, VIEW_INPUT | changes)
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    assert entry is not None
    return entry


async def _start_options(hass: HomeAssistant, entry: ConfigEntry, step_id: str) -> dict[str, Any]:
    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == step_id
    return result


async def _set_options(
    hass: HomeAssistant, entry: ConfigEntry, user_input: dict[str, Any]
) -> dict[str, Any]:
    step_id = "hub" if entry.data.get(CONF_KIND) == KIND_HUB else "web_ui"
    result = await _start_options(hass, entry, step_id)
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result


def _device_with_web_ui(
    hass: HomeAssistant,
    name: str = "Porch",
    url: str | None = DEVICE_URL,
    mac: str = "aa:bb:cc:dd:ee:01",
) -> dr.DeviceEntry:
    """A device of another integration whose "Visit" link is a local web UI."""
    owner = MockConfigEntry(domain="fake_esphome")
    owner.add_to_hass(hass)
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("fake_esphome", name.lower())},
        connections={(dr.CONNECTION_NETWORK_MAC, mac)},
        name=name,
        configuration_url=url,
    )


async def _add_device_web_ui(hass: HomeAssistant, device: dr.DeviceEntry) -> ConfigEntry:
    """Accept the discovery flow of a device."""
    unique_id = device_unique_id(device.id)
    flow = next(f for f in _discovery_flows(hass) if f["context"]["unique_id"] == unique_id)
    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY, result
    return result["result"]


async def _porch(hass: HomeAssistant) -> tuple[dr.DeviceEntry, ConfigEntry]:
    """A device and its web UI entry, added through discovery."""
    device = _device_with_web_ui(hass)
    await hass.async_block_till_done()
    return device, await _add_device_web_ui(hass, device)


def _store_site_data(hass: HomeAssistant, user_id: str, view_id: str, value: str) -> None:
    """What a site leaves behind for a user: a cookie and a localStorage item."""
    hub = _hub(hass)
    view = hub.views[view_id]
    hub.async_store_cookies(user_id, view, [f"sid={value}; Path=/"], view.origin)
    assert hub.async_apply_storage_write(user_id, view_id, "w1", {"token": value}, clear=False)


def _cookies(hass: HomeAssistant, user_id: str, view_id: str) -> dict[str, str]:
    hub = _hub(hass)
    return {m.key: m.value for m in hub.cookie_jar(user_id, hub.views[view_id])}


def _device_url(hass: HomeAssistant, device: dr.DeviceEntry) -> str | None:
    current = dr.async_get(hass).async_get(device.id)
    assert current is not None
    return current.configuration_url


def _own_devices(hass: HomeAssistant, entry: ConfigEntry) -> list[dr.DeviceEntry]:
    return dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture
async def http(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})


@pytest.fixture
async def hub_entry(hass: HomeAssistant, http: None) -> ConfigEntry:
    """The hub, created through the config flow and loaded."""
    entry = await _create_hub(hass)
    assert entry.state is ConfigEntryState.LOADED
    return entry


# ---- config flow: the hub --------------------------------------------------


async def test_user_flow_creates_hub(hass: HomeAssistant, http: None) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "hub"
    assert not _fields(result["data_schema"])
    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == NAME
    assert entry.version == 2
    assert entry.unique_id == HUB_UNIQUE_ID
    assert dict(entry.data) == {CONF_KIND: KIND_HUB}
    assert dict(entry.options) == HUB_OPTIONS
    assert entry.state is ConfigEntryState.LOADED
    hub = _hub(hass)
    assert entry.runtime_data is hub
    assert hub.hub_entry is entry
    assert hub.active
    assert hub.views == {}
    # The hub owns no devices
    assert _own_devices(hass, entry) == []


async def test_second_user_flow_offers_web_ui_step(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    for _ in range(2):
        result = await _start_web_ui_flow(hass)
        hass.config_entries.flow.async_abort(result["flow_id"])
    assert [e.entry_id for e in _entries(hass)] == [hub_entry.entry_id]


async def test_parallel_user_flows_create_one_hub(hass: HomeAssistant, http: None) -> None:
    first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert first["step_id"] == "hub"
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"
    await hass.config_entries.flow.async_configure(first["flow_id"], {})
    await hass.async_block_till_done()
    assert len(_entries(hass)) == 1
    # From now on "Add entry" adds web UIs
    await _start_web_ui_flow(hass)


async def test_user_flow_brings_back_a_deleted_hub(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    router = await _add_router(hass)
    assert await hass.config_entries.async_remove(hub_entry.entry_id)
    await hass.async_block_till_done()
    assert _hub(hass).hub_entry is None
    new_hub = await _create_hub(hass)
    assert _hub(hass).hub_entry is new_hub
    assert router.entry_id in _hub(hass).views


# ---- config flow: a web UI added by URL -------------------------------------------


async def test_web_ui_step_form(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    result = await _start_web_ui_flow(hass)
    assert result["errors"] == {}
    fields = _fields(result["data_schema"])
    assert list(fields) == [
        CONF_NAME,
        CONF_URL,
        CONF_MODE,
        CONF_TRUSTED_ACK,
        CONF_VERIFY_SSL,
        CONF_USERNAME,
        CONF_PASSWORD,
        CONF_SHOW_IN_SIDEBAR,
        CONF_ICON,
    ]
    defaults = {k: _default(m) for k, m in fields.items()}
    assert {k: v for k, v in defaults.items() if v is not vol.UNDEFINED} == {
        CONF_MODE: MODE_ISOLATED,
        CONF_TRUSTED_ACK: False,
        CONF_VERIFY_SSL: True,
        CONF_SHOW_IN_SIDEBAR: False,
    }
    assert isinstance(fields[CONF_NAME], vol.Required)
    assert isinstance(fields[CONF_URL], vol.Required)
    assert isinstance(fields[CONF_USERNAME], vol.Optional)
    assert isinstance(fields[CONF_PASSWORD], vol.Optional)


async def test_web_ui_step_creates_entry(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    result, entry = await _add_web_ui(
        hass,
        VIEW_INPUT
        | {
            CONF_NAME: "  Router  ",
            CONF_URL: "  http://192.168.1.1/admin/?tab=1  ",
            CONF_USERNAME: " admin ",
            CONF_PASSWORD: "hunter2",
            CONF_ICON: "mdi:router",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry is not None
    assert entry.title == "Router"
    assert entry.source == SOURCE_USER
    assert entry.unique_id is None
    assert dict(entry.data) == {CONF_KIND: KIND_VIEW}
    assert dict(entry.options) == {
        CONF_URL: "http://192.168.1.1/admin/?tab=1",
        CONF_MODE: MODE_ISOLATED,
        CONF_TRUSTED_ACK: False,
        CONF_VERIFY_SSL: True,
        CONF_SHOW_IN_SIDEBAR: False,
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "hunter2",
        CONF_ICON: "mdi:router",
    }
    assert entry.state is ConfigEntryState.LOADED
    assert entry.runtime_data is _hub(hass)

    # The view id is the entry id
    view = _hub(hass).views[entry.entry_id]
    assert view.view_id == entry.entry_id
    assert view.name == "Router"
    assert view.url == "http://192.168.1.1/admin/?tab=1"
    assert view.source == SOURCE_MANUAL
    assert view.device_id is None
    assert view.verify_ssl is True
    assert view.authorization == basic_authorization("admin", "hunter2")
    assert view.icon == "mdi:router"

    # Its own device, linked to nothing
    [device] = _own_devices(hass, entry)
    assert device.name == "Router web UI"
    assert device.identifiers == {(DOMAIN, entry.entry_id)}
    assert device.connections == set()
    assert device.configuration_url == f"{DEVICE_LINK_PREFIX}{entry.entry_id}"


@pytest.mark.parametrize(
    ("extra", "expected"),
    [
        # A password without a username is useless for Basic auth and is not kept
        ({CONF_PASSWORD: "secret"}, {}),
        ({CONF_USERNAME: "   ", CONF_PASSWORD: "secret"}, {}),
        ({CONF_USERNAME: "", CONF_PASSWORD: ""}, {}),
        # A username without a password is kept on its own
        ({CONF_USERNAME: "admin"}, {CONF_USERNAME: "admin"}),
        ({CONF_USERNAME: "admin", CONF_PASSWORD: ""}, {CONF_USERNAME: "admin"}),
        # Empty icon is not stored
        ({CONF_ICON: ""}, {}),
    ],
)
async def test_web_ui_step_credentials_and_icon(
    hass: HomeAssistant,
    hub_entry: ConfigEntry,
    extra: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    entry = await _add_router(hass, **extra)
    stored = {
        key: value
        for key, value in entry.options.items()
        if key in (CONF_USERNAME, CONF_PASSWORD, CONF_ICON)
    }
    assert stored == expected


@pytest.mark.parametrize(
    ("changes", "expected"),
    [
        (
            {CONF_URL: "http://admin:s3cret@192.168.1.1/"},
            {CONF_USERNAME: "admin", CONF_PASSWORD: "s3cret"},
        ),
        # Typed fields win over what is in the URL
        (
            {
                CONF_URL: "http://admin:s3cret@192.168.1.1/",
                CONF_USERNAME: "root",
                CONF_PASSWORD: "typed",
            },
            {CONF_USERNAME: "root", CONF_PASSWORD: "typed"},
        ),
        (
            {CONF_URL: "http://admin@192.168.1.1/", CONF_PASSWORD: "typed"},
            {CONF_USERNAME: "admin", CONF_PASSWORD: "typed"},
        ),
    ],
)
async def test_web_ui_url_with_credentials_is_not_stored_verbatim(
    hass: HomeAssistant,
    hub_entry: ConfigEntry,
    changes: dict[str, Any],
    expected: dict[str, str],
) -> None:
    """http://user:pass@host/ is split into the credential fields.

    The proxy only sends Basic auth from those fields, and only they are redacted in
    diagnostics, so the credentials must not stay in the URL.
    """
    entry = await _add_router(hass, **changes)
    assert entry.options[CONF_URL] == "http://192.168.1.1/"
    assert {k: entry.options.get(k) for k in expected} == expected
    assert _hub(hass).views[entry.entry_id].url == "http://192.168.1.1/"


@pytest.mark.parametrize(
    ("changes", "errors"),
    [
        ({CONF_NAME: ""}, {CONF_NAME: "name_required"}),
        ({CONF_NAME: "   "}, {CONF_NAME: "name_required"}),
        ({CONF_URL: ""}, {CONF_URL: "invalid_url"}),
        ({CONF_URL: "ftp://192.168.1.1/"}, {CONF_URL: "invalid_url"}),
        ({CONF_URL: "javascript:alert(1)"}, {CONF_URL: "invalid_url"}),
        ({CONF_URL: "192.168.1.1"}, {CONF_URL: "invalid_url"}),
        ({CONF_URL: "//192.168.1.1/"}, {CONF_URL: "invalid_url"}),
        ({CONF_URL: "http://"}, {CONF_URL: "invalid_url"}),
        ({CONF_URL: "http:///admin"}, {CONF_URL: "invalid_url"}),
        ({CONF_URL: "http://host:99999/"}, {CONF_URL: "invalid_url"}),
        ({CONF_URL: "http://[::1"}, {CONF_URL: "invalid_url"}),
        ({CONF_MODE: MODE_TRUSTED}, {CONF_TRUSTED_ACK: "trusted_not_acknowledged"}),
        (
            {CONF_NAME: "", CONF_URL: "nope", CONF_MODE: MODE_TRUSTED},
            {
                CONF_NAME: "name_required",
                CONF_URL: "invalid_url",
                CONF_TRUSTED_ACK: "trusted_not_acknowledged",
            },
        ),
    ],
)
async def test_web_ui_step_validation_errors(
    hass: HomeAssistant,
    hub_entry: ConfigEntry,
    changes: dict[str, Any],
    errors: dict[str, str],
) -> None:
    user_input = VIEW_INPUT | {CONF_USERNAME: "admin", CONF_PASSWORD: "secret"} | changes
    result, entry = await _add_web_ui(hass, user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "web_ui"
    assert result["errors"] == errors
    assert entry is None
    assert _view_entries(hass) == []
    # What the user typed comes back, so fixing one field does not mean retyping all;
    # but the password is never sent back to the browser
    fields = _fields(result["data_schema"])
    for key, value in user_input.items():
        if key != CONF_PASSWORD:
            assert _form_value(fields[key]) == value, key
    assert _suggested(fields[CONF_PASSWORD]) is None


async def test_web_ui_step_recovers_after_error(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    result = await _start_web_ui_flow(hass)
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"], VIEW_INPUT | {CONF_URL: "not a url"}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    result = await hass.config_entries.flow.async_configure(result["flow_id"], VIEW_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(_view_entries(hass)) == 1


async def test_web_ui_step_trusted_with_ack(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    entry = await _add_router(hass, **{CONF_MODE: MODE_TRUSTED, CONF_TRUSTED_ACK: True})
    assert entry.options[CONF_MODE] == MODE_TRUSTED
    assert entry.options[CONF_TRUSTED_ACK] is True
    assert _hub(hass).views[entry.entry_id].mode == MODE_TRUSTED


@pytest.mark.parametrize(
    "user_input",
    [
        VIEW_INPUT | {CONF_MODE: "bogus"},
        # Not part of the form: a web UI added by URL cannot claim a device
        VIEW_INPUT | {CONF_DEVICE_ID: "abc"},
        VIEW_INPUT | {CONF_VISIT_LINK: VISIT_HERE},
        {CONF_URL: "http://192.168.1.1/"},
        {CONF_NAME: "Router"},
    ],
)
async def test_web_ui_step_rejects_invalid_data(
    hass: HomeAssistant, hub_entry: ConfigEntry, user_input: dict[str, Any]
) -> None:
    result = await _start_web_ui_flow(hass)
    with pytest.raises(InvalidData):
        await hass.config_entries.flow.async_configure(result["flow_id"], user_input)
    assert _view_entries(hass) == []


async def test_same_url_can_be_added_twice(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    """Web UIs added by URL have no unique id: two can open the same site."""
    first = await _add_router(hass)
    second = await _add_router(hass, **{CONF_NAME: "Router again"})
    assert first.entry_id != second.entry_id
    assert set(_hub(hass).views) == {first.entry_id, second.entry_id}


# ---- config flow: discovered devices -----------------------------------------------


async def test_discovery_confirm_flow(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    device = _device_with_web_ui(hass)
    await hass.async_block_till_done()
    [flow] = _discovery_flows(hass)
    assert flow["step_id"] == "discovery_confirm"
    assert flow["context"]["unique_id"] == device_unique_id(device.id)
    assert flow["context"]["title_placeholders"] == {"name": "Porch"}

    # Showing the form again: no fields, the device described
    result = await hass.config_entries.flow.async_configure(flow["flow_id"])
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "discovery_confirm"
    assert not _fields(result["data_schema"])
    placeholders = result["description_placeholders"]
    assert placeholders["name"] == "Porch"
    assert placeholders["url"] == DEVICE_URL
    assert "192.168.1.50" in placeholders["subtitle"]

    result = await hass.config_entries.flow.async_configure(flow["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == "Porch"
    assert entry.source == SOURCE_INTEGRATION_DISCOVERY
    assert entry.unique_id == device_unique_id(device.id)
    assert dict(entry.data) == {CONF_KIND: KIND_VIEW, CONF_DEVICE_ID: device.id}
    assert dict(entry.options) == {CONF_MODE: MODE_ISOLATED, CONF_VERIFY_SSL: False}
    assert entry.state is ConfigEntryState.LOADED

    view = _hub(hass).views[entry.entry_id]
    assert view.source == SOURCE_DEVICE
    assert view.device_id == device.id
    assert view.url == DEVICE_URL
    assert view.verify_ssl is False
    assert _discovery_flows(hass) == []
    # Its own device shares the device's connections ("Linked devices")
    [own] = _own_devices(hass, entry)
    assert own.name == "Porch web UI"
    assert own.connections == device.connections


async def test_discovery_of_configured_device_aborts(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    device, _ = await _porch(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_INTEGRATION_DISCOVERY},
        data={CONF_DEVICE_ID: device.id, "name": "Porch", CONF_URL: DEVICE_URL, "subtitle": ""},
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(_view_entries(hass)) == 1


async def test_discovered_web_ui_url_follows_device(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    device, entry = await _porch(hass)
    dr.async_get(hass).async_update_device(device.id, configuration_url="http://192.168.1.51/x")
    await hass.async_block_till_done()
    assert _hub(hass).views[entry.entry_id].url == "http://192.168.1.51/x"
    assert CONF_URL not in entry.options


# ---- config flow: import (migration from 0.2) ----------------------------------------


async def test_import_manual_web_ui(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    options = {
        CONF_URL: "http://192.168.1.1/",
        CONF_MODE: MODE_ISOLATED,
        CONF_VERIFY_SSL: True,
        CONF_SHOW_IN_SIDEBAR: True,
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "pw",
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_IMPORT},
        data={"title": "Router", CONF_PREVIOUS_VIEW_ID: "old1", "options": options},
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.title == "Router"
    assert entry.source == SOURCE_IMPORT
    assert entry.unique_id is None
    assert dict(entry.data) == {CONF_KIND: KIND_VIEW, CONF_PREVIOUS_VIEW_ID: "old1"}
    assert dict(entry.options) == options
    assert _hub(hass).views[entry.entry_id].url == "http://192.168.1.1/"


async def test_import_device_web_ui(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    """A device's web UI wins over its discovery flow and is imported once."""
    device = _device_with_web_ui(hass)
    await hass.async_block_till_done()
    assert len(_discovery_flows(hass)) == 1
    data = {
        "title": "Porch custom",
        CONF_DEVICE_ID: device.id,
        CONF_PREVIOUS_VIEW_ID: "old2",
        "options": {CONF_URL: DEVICE_URL, CONF_MODE: MODE_ISOLATED, CONF_VERIFY_SSL: False},
    }
    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=data
    )
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    entry = result["result"]
    assert entry.unique_id == device_unique_id(device.id)
    assert dict(entry.data) == {
        CONF_KIND: KIND_VIEW,
        CONF_DEVICE_ID: device.id,
        CONF_PREVIOUS_VIEW_ID: "old2",
    }
    # Follows the device's own link
    assert dict(entry.options) == {CONF_MODE: MODE_ISOLATED, CONF_VERIFY_SSL: False}
    assert _discovery_flows(hass) == []

    result = await hass.config_entries.flow.async_init(
        DOMAIN, context={"source": SOURCE_IMPORT}, data=data
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert len(_view_entries(hass)) == 1


# ---- options flow: the hub -----------------------------------------------------


async def test_hub_options_form(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    result = await _start_options(hass, hub_entry, "hub")
    assert result["description_placeholders"] == {"panel_url": f"/{PANEL_URL_PATH}"}
    fields = _fields(result["data_schema"])
    assert list(fields) == [
        CONF_DISCOVERY,
        CONF_LINK_DEVICE_PAGES,
        CONF_SHOW_PANEL,
        CONF_PANEL_TITLE,
        CONF_PANEL_ICON,
    ]
    assert {k: _form_value(m) for k, m in fields.items()} == {
        **HUB_OPTIONS,
        CONF_PANEL_TITLE: vol.UNDEFINED,
        CONF_PANEL_ICON: DEFAULT_PANEL_ICON,
    }


async def test_hub_options_form_prefills_current_options(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    current = {
        CONF_DISCOVERY: False,
        CONF_LINK_DEVICE_PAGES: False,
        CONF_SHOW_PANEL: False,
        CONF_PANEL_TITLE: "Devices",
        CONF_PANEL_ICON: "mdi:lan",
    }
    hass.config_entries.async_update_entry(hub_entry, options=current)
    await hass.async_block_till_done()
    result = await _start_options(hass, hub_entry, "hub")
    fields = _fields(result["data_schema"])
    assert {k: _form_value(m) for k, m in fields.items()} == current


async def test_hub_options_defaults_when_options_missing(hass: HomeAssistant, http: None) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN, version=2, title=NAME, unique_id=HUB_UNIQUE_ID, data={CONF_KIND: KIND_HUB}
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    result = await _start_options(hass, entry, "hub")
    fields = _fields(result["data_schema"])
    assert {k: _default(fields[k]) for k in HUB_OPTIONS} == HUB_OPTIONS
    assert _hub(hass).discovery_enabled
    assert _hub(hass).link_default
    assert _panels(hass)[PANEL_URL_PATH].show_in_sidebar is True


async def test_hub_options_saved_and_applied_in_place(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    router = await _add_router(hass)
    hub = _hub(hass)
    new_options = {
        CONF_DISCOVERY: False,
        CONF_LINK_DEVICE_PAGES: False,
        CONF_SHOW_PANEL: True,
        CONF_PANEL_TITLE: "Devices",
        CONF_PANEL_ICON: "mdi:lan",
    }
    with (
        patch.object(
            hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
        ) as reload,
        patch.object(
            hass.config_entries, "async_unload", wraps=hass.config_entries.async_unload
        ) as unload,
    ):
        result = await _set_options(hass, hub_entry, new_options)
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert dict(hub_entry.options) == new_options
    reload.assert_not_called()
    unload.assert_not_called()
    assert hub_entry.state is ConfigEntryState.LOADED
    assert _hub(hass) is hub
    assert not hub.discovery_enabled
    assert not hub.link_default
    assert router.entry_id in hub.views
    panel = _panels(hass)[PANEL_URL_PATH]
    assert (panel.sidebar_title, panel.sidebar_icon) == ("Devices", "mdi:lan")


async def test_hub_options_empty_title_and_icon_are_not_stored(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    await _set_options(
        hass, hub_entry, HUB_OPTIONS | {CONF_PANEL_TITLE: "Devices", CONF_PANEL_ICON: "mdi:lan"}
    )
    await _set_options(hass, hub_entry, HUB_OPTIONS | {CONF_PANEL_TITLE: "", CONF_PANEL_ICON: ""})
    assert dict(hub_entry.options) == HUB_OPTIONS
    panel = _panels(hass)[PANEL_URL_PATH]
    assert (panel.sidebar_title, panel.sidebar_icon) == (NAME, DEFAULT_PANEL_ICON)


# ---- options flow: web UIs -----------------------------------------------------------


async def test_manual_web_ui_options_form(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    entry = await _add_router(
        hass, **{CONF_USERNAME: "admin", CONF_PASSWORD: "hunter2", CONF_ICON: "mdi:router"}
    )
    result = await _start_options(hass, entry, "web_ui")
    assert result["errors"] == {}
    assert result["description_placeholders"] == {
        "panel_url": f"/{PANEL_URL_PATH}/{entry.entry_id}"
    }
    fields = _fields(result["data_schema"])
    assert list(fields) == [
        CONF_NAME,
        CONF_URL,
        CONF_MODE,
        CONF_TRUSTED_ACK,
        CONF_VERIFY_SSL,
        CONF_USERNAME,
        CONF_PASSWORD,
        CONF_SHOW_IN_SIDEBAR,
        CONF_ICON,
    ]
    assert {k: _form_value(m) for k, m in fields.items()} == {
        CONF_NAME: "Router",
        CONF_URL: VIEW_INPUT[CONF_URL],
        CONF_MODE: MODE_ISOLATED,
        CONF_TRUSTED_ACK: False,
        CONF_VERIFY_SSL: True,
        CONF_USERNAME: "admin",
        # The stored password is never sent to the browser
        CONF_PASSWORD: vol.UNDEFINED,
        CONF_SHOW_IN_SIDEBAR: False,
        CONF_ICON: "mdi:router",
    }


async def test_device_web_ui_options_form(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    _, entry = await _porch(hass)
    result = await _start_options(hass, entry, "web_ui")
    fields = _fields(result["data_schema"])
    # No name (Home Assistant's own rename) and no URL (the device's own link)
    assert list(fields) == [
        CONF_MODE,
        CONF_TRUSTED_ACK,
        CONF_VISIT_LINK,
        CONF_VERIFY_SSL,
        CONF_USERNAME,
        CONF_PASSWORD,
        CONF_SHOW_IN_SIDEBAR,
        CONF_ICON,
    ]
    assert {k: _form_value(m) for k, m in fields.items()} == {
        CONF_MODE: MODE_ISOLATED,
        CONF_TRUSTED_ACK: False,
        CONF_VISIT_LINK: VISIT_DEFAULT,
        CONF_VERIFY_SSL: False,
        CONF_USERNAME: vol.UNDEFINED,
        CONF_PASSWORD: vol.UNDEFINED,
        CONF_SHOW_IN_SIDEBAR: False,
        CONF_ICON: vol.UNDEFINED,
    }
    # Name and URL cannot be sent either
    for extra in ({CONF_NAME: "x"}, {CONF_URL: "http://192.168.1.9/"}):
        with pytest.raises(InvalidData):
            await hass.config_entries.options.async_configure(
                result["flow_id"], DEVICE_OPTIONS_INPUT | extra
            )


@pytest.mark.parametrize(
    ("link_device_pages", "visit_link", "stored", "linked"),
    [
        (True, VISIT_DEFAULT, None, True),
        (False, VISIT_DEFAULT, None, False),
        (True, VISIT_DEVICE, VISIT_DEVICE, False),
        (False, VISIT_DEVICE, VISIT_DEVICE, False),
        (True, VISIT_HERE, VISIT_HERE, True),
        (False, VISIT_HERE, VISIT_HERE, True),
    ],
)
async def test_device_web_ui_visit_link(
    hass: HomeAssistant,
    hub_entry: ConfigEntry,
    link_device_pages: bool,
    visit_link: str,
    stored: str | None,
    linked: bool,
) -> None:
    await _set_options(hass, hub_entry, HUB_OPTIONS | {CONF_LINK_DEVICE_PAGES: link_device_pages})
    device, entry = await _porch(hass)

    result = await _set_options(hass, entry, DEVICE_OPTIONS_INPUT | {CONF_VISIT_LINK: visit_link})
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options.get(CONF_VISIT_LINK) == stored
    expected = f"{DEVICE_LINK_PREFIX}{entry.entry_id}" if linked else DEVICE_URL
    assert _device_url(hass, device) == expected
    # The form shows the stored choice
    result = await _start_options(hass, entry, "web_ui")
    assert _form_value(_fields(result["data_schema"])[CONF_VISIT_LINK]) == visit_link

    # Back to the default: nothing stored, the hub's option applies again
    await hass.config_entries.options.async_configure(result["flow_id"], DEVICE_OPTIONS_INPUT)
    await hass.async_block_till_done()
    assert CONF_VISIT_LINK not in entry.options
    expected = f"{DEVICE_LINK_PREFIX}{entry.entry_id}" if link_device_pages else DEVICE_URL
    assert _device_url(hass, device) == expected


async def test_device_web_ui_options_saved(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    device, entry = await _porch(hass)
    await _set_options(
        hass,
        entry,
        {
            CONF_MODE: MODE_TRUSTED,
            CONF_TRUSTED_ACK: True,
            CONF_VISIT_LINK: VISIT_DEFAULT,
            CONF_VERIFY_SSL: True,
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "pw",
            CONF_SHOW_IN_SIDEBAR: True,
            CONF_ICON: "mdi:lightbulb",
        },
    )
    assert dict(entry.options) == {
        CONF_MODE: MODE_TRUSTED,
        CONF_TRUSTED_ACK: True,
        CONF_VERIFY_SSL: True,
        CONF_SHOW_IN_SIDEBAR: True,
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "pw",
        CONF_ICON: "mdi:lightbulb",
    }
    assert dict(entry.data) == {CONF_KIND: KIND_VIEW, CONF_DEVICE_ID: device.id}
    assert entry.title == "Porch"
    view = _hub(hass).views[entry.entry_id]
    assert (view.mode, view.verify_ssl, view.url) == (MODE_TRUSTED, True, DEVICE_URL)
    assert view.authorization == basic_authorization("admin", "pw")


async def test_web_ui_options_applied_in_place(
    hass: HomeAssistant, hub_entry: ConfigEntry, hass_admin_user: MockUser
) -> None:
    """Open views keep working: no reload, sessions and site data stay."""
    entry = await _add_router(hass)
    hub = _hub(hass)
    session = hub.sessions.create(entry.entry_id, hass_admin_user.id, None)
    _store_site_data(hass, hass_admin_user.id, entry.entry_id, "router")
    with patch.object(
        hass.config_entries, "async_reload", wraps=hass.config_entries.async_reload
    ) as reload:
        await _set_options(
            hass,
            entry,
            VIEW_INPUT
            | {
                CONF_URL: "http://192.168.1.1/status",
                CONF_MODE: MODE_TRUSTED,
                CONF_TRUSTED_ACK: True,
            },
        )
    reload.assert_not_called()
    assert entry.state is ConfigEntryState.LOADED
    view = hub.views[entry.entry_id]
    assert (view.url, view.mode) == ("http://192.168.1.1/status", MODE_TRUSTED)
    assert hub.sessions.touch(session.token, entry.entry_id) is session
    assert hub.shim_storage(hass_admin_user.id, entry.entry_id) == {"token": "router"}
    assert _cookies(hass, hass_admin_user.id, entry.entry_id) == {"sid": "router"}


async def test_renaming_manual_web_ui(
    hass: HomeAssistant, hub_entry: ConfigEntry, hass_admin_user: MockUser
) -> None:
    entry = await _add_router(hass, **{CONF_SHOW_IN_SIDEBAR: True})
    _store_site_data(hass, hass_admin_user.id, entry.entry_id, "router")
    result = await _set_options(
        hass, entry, VIEW_INPUT | {CONF_NAME: "  Gateway ", CONF_SHOW_IN_SIDEBAR: True}
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.title == "Gateway"
    assert CONF_NAME not in entry.options
    assert entry.state is ConfigEntryState.LOADED
    assert _hub(hass).views[entry.entry_id].name == "Gateway"
    assert [d.name for d in _own_devices(hass, entry)] == ["Gateway web UI"]
    assert _panels(hass)[_view_panel_path(entry.entry_id)].sidebar_title == "Gateway"
    # Same site: logins stay
    assert _hub(hass).shim_storage(hass_admin_user.id, entry.entry_id) == {"token": "router"}
    # The form shows the new name
    result = await _start_options(hass, entry, "web_ui")
    assert _form_value(_fields(result["data_schema"])[CONF_NAME]) == "Gateway"


async def test_renaming_device_web_ui_entry(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    """Device web UIs are renamed with Home Assistant's own rename of the entry."""
    _, entry = await _porch(hass)
    await _set_options(hass, entry, DEVICE_OPTIONS_INPUT | {CONF_SHOW_IN_SIDEBAR: True})
    hass.config_entries.async_update_entry(entry, title="Front door")
    await hass.async_block_till_done()
    assert _hub(hass).views[entry.entry_id].name == "Front door"
    assert [d.name for d in _own_devices(hass, entry)] == ["Front door web UI"]
    assert _panels(hass)[_view_panel_path(entry.entry_id)].sidebar_title == "Front door"


async def test_moving_web_ui_to_another_site_drops_old_site_data(
    hass: HomeAssistant, hub_entry: ConfigEntry, hass_admin_user: MockUser
) -> None:
    entry = await _add_router(hass)
    user_id = hass_admin_user.id
    _store_site_data(hass, user_id, entry.entry_id, "router-secret")
    await _set_options(hass, entry, VIEW_INPUT | {CONF_URL: "http://192.168.1.9/"})
    view = _hub(hass).views[entry.entry_id]
    assert str(view.origin) == "http://192.168.1.9"
    # A browser would never give one site's cookies and storage to another
    assert _hub(hass).shim_storage(user_id, entry.entry_id) == {}
    assert _cookies(hass, user_id, entry.entry_id) == {}


@pytest.mark.parametrize("password_input", [{}, {CONF_PASSWORD: ""}])
async def test_options_empty_password_keeps_stored_one(
    hass: HomeAssistant, hub_entry: ConfigEntry, password_input: dict[str, Any]
) -> None:
    entry = await _add_router(hass, **{CONF_USERNAME: "admin", CONF_PASSWORD: "hunter2"})
    await _set_options(hass, entry, VIEW_INPUT | {CONF_USERNAME: "admin"} | password_input)
    assert entry.options[CONF_USERNAME] == "admin"
    assert entry.options[CONF_PASSWORD] == "hunter2"
    assert _hub(hass).views[entry.entry_id].authorization == basic_authorization("admin", "hunter2")


async def test_options_new_password_replaces_stored_one(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    entry = await _add_router(hass, **{CONF_USERNAME: "admin", CONF_PASSWORD: "hunter2"})
    await _set_options(hass, entry, VIEW_INPUT | {CONF_USERNAME: "root", CONF_PASSWORD: "new"})
    assert (entry.options[CONF_USERNAME], entry.options[CONF_PASSWORD]) == ("root", "new")


async def test_options_credentials_in_url_replace_stored_ones(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    entry = await _add_router(hass, **{CONF_USERNAME: "admin", CONF_PASSWORD: "hunter2"})
    await _set_options(hass, entry, VIEW_INPUT | {CONF_URL: "http://root:fromurl@192.168.1.1/"})
    assert entry.options[CONF_URL] == "http://192.168.1.1/"
    assert (entry.options[CONF_USERNAME], entry.options[CONF_PASSWORD]) == ("root", "fromurl")


async def test_device_options_empty_password_keeps_stored_one(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    _, entry = await _porch(hass)
    await _set_options(
        hass, entry, DEVICE_OPTIONS_INPUT | {CONF_USERNAME: "admin", CONF_PASSWORD: "pw"}
    )
    await _set_options(hass, entry, DEVICE_OPTIONS_INPUT | {CONF_USERNAME: "admin"})
    assert entry.options[CONF_PASSWORD] == "pw"


@pytest.mark.parametrize("username_input", [{}, {CONF_USERNAME: ""}, {CONF_USERNAME: "   "}])
async def test_options_removing_username_drops_password(
    hass: HomeAssistant, hub_entry: ConfigEntry, username_input: dict[str, Any]
) -> None:
    entry = await _add_router(hass, **{CONF_USERNAME: "admin", CONF_PASSWORD: "hunter2"})
    await _set_options(hass, entry, VIEW_INPUT | username_input)
    assert CONF_USERNAME not in entry.options
    assert CONF_PASSWORD not in entry.options
    assert _hub(hass).views[entry.entry_id].authorization is None


async def test_options_removing_icon(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    entry = await _add_router(hass, **{CONF_ICON: "mdi:router"})
    await _set_options(hass, entry, VIEW_INPUT | {CONF_ICON: ""})
    assert CONF_ICON not in entry.options


@pytest.mark.parametrize(
    ("changes", "errors"),
    [
        ({CONF_NAME: " "}, {CONF_NAME: "name_required"}),
        ({CONF_URL: "ftp://192.168.1.1/"}, {CONF_URL: "invalid_url"}),
        ({CONF_MODE: MODE_TRUSTED}, {CONF_TRUSTED_ACK: "trusted_not_acknowledged"}),
    ],
)
async def test_manual_web_ui_options_validation_errors(
    hass: HomeAssistant,
    hub_entry: ConfigEntry,
    changes: dict[str, Any],
    errors: dict[str, str],
) -> None:
    entry = await _add_router(hass, **{CONF_USERNAME: "admin", CONF_PASSWORD: "hunter2"})
    before = dict(entry.options)
    result = await _start_options(hass, entry, "web_ui")
    user_input = (
        VIEW_INPUT | {CONF_USERNAME: "root", CONF_ICON: "mdi:new", CONF_PASSWORD: "typed"} | changes
    )
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "web_ui"
    assert result["errors"] == errors
    assert dict(entry.options) == before
    assert entry.title == "Router"
    # What was typed comes back over the stored values, but never a password
    fields = _fields(result["data_schema"])
    for key, value in user_input.items():
        if key != CONF_PASSWORD:
            assert _form_value(fields[key]) == value, key
    assert _suggested(fields[CONF_PASSWORD]) is None

    # Fixed: saved
    result = await hass.config_entries.options.async_configure(result["flow_id"], VIEW_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY


async def test_device_web_ui_options_trusted_needs_ack(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    _, entry = await _porch(hass)
    result = await _start_options(hass, entry, "web_ui")
    user_input = DEVICE_OPTIONS_INPUT | {CONF_MODE: MODE_TRUSTED, CONF_VISIT_LINK: VISIT_DEVICE}
    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input)
    assert result["errors"] == {CONF_TRUSTED_ACK: "trusted_not_acknowledged"}
    fields = _fields(result["data_schema"])
    assert _form_value(fields[CONF_MODE]) == MODE_TRUSTED
    assert _form_value(fields[CONF_VISIT_LINK]) == VISIT_DEVICE
    assert entry.options[CONF_MODE] == MODE_ISOLATED


# ---- migration from 0.2 (version 1: one entry, web UIs as subentries) ---------------


def _v1_entry(
    options: dict[str, Any] | None = None,
    subentries: list[ConfigSubentryData] | None = None,
) -> MockConfigEntry:
    if options is None:
        options = {CONF_DISCOVERY: True, CONF_LINK_DEVICE_PAGES: False, CONF_LINKED_DEVICES: True}
    return MockConfigEntry(
        domain=DOMAIN,
        version=1,
        title=NAME,
        data={},
        options=options,
        subentries_data=subentries or [],
    )


def _v1_view(
    subentry_id: str, title: str, data: dict[str, Any], unique_id: str | None = None
) -> ConfigSubentryData:
    return ConfigSubentryData(
        data=MappingProxyType(data),
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_TYPE_VIEW,
        title=title,
        unique_id=unique_id,
    )


def _v1_device_view(subentry_id: str, title: str, device_id: str) -> ConfigSubentryData:
    return _v1_view(
        subentry_id,
        title,
        {
            CONF_URL: DEVICE_URL,
            CONF_MODE: MODE_ISOLATED,
            CONF_VERIFY_SSL: False,
            CONF_SHOW_IN_SIDEBAR: False,
            CONF_DEVICE_ID: device_id,
        },
        unique_id=f"device:{device_id}",
    )


def _v1_storage(
    hass_storage: dict[str, Any],
    hidden: list[str] | None = None,
    link_overrides: dict[str, bool] | None = None,
) -> None:
    hass_storage[STORAGE_KEY] = {
        "version": 1,
        "key": STORAGE_KEY,
        "data": {"originals": {}, "hidden": hidden or [], "link_overrides": link_overrides or {}},
    }


async def _migrate(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    if entry.entry_id not in {e.entry_id for e in _entries(hass)}:
        entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    await hass.async_block_till_done()


async def test_migration_from_v1(
    hass: HomeAssistant,
    http: None,
    hass_storage: dict[str, Any],
    hass_admin_user: MockUser,
) -> None:
    user_id = hass_admin_user.id
    registry = dr.async_get(hass)
    dev = _device_with_web_ui(hass)
    hidden = _device_with_web_ui(hass, "Pod", "http://192.168.1.60/", "aa:bb:cc:dd:ee:02")
    # Hidden: a discovered device, and one no longer in the registry
    _v1_storage(hass_storage, hidden=[f"d_{hidden.id}", "d_gone"], link_overrides={dev.id: False})
    hass_storage[STORAGE_KEY_JAR] = {
        "version": 1,
        "key": STORAGE_KEY_JAR,
        "data": {
            "cookies": {},
            "storage": {
                f"{user_id}|sub1": {"a": "1"},
                f"{user_id}|d_{dev.id}": {"b": "2"},
            },
        },
    }
    router_data = {
        CONF_URL: "http://192.168.1.1/",
        CONF_MODE: MODE_ISOLATED,
        CONF_TRUSTED_ACK: False,
        CONF_VERIFY_SSL: True,
        CONF_SHOW_IN_SIDEBAR: True,
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "pw",
        CONF_ICON: "mdi:router",
    }
    old = _v1_entry(
        subentries=[
            _v1_view("sub1", "Router", router_data),
            _v1_device_view("sub2", "Porch custom", dev.id),
        ]
    )
    old.add_to_hass(hass)
    # A linked device the old entry kept for itself
    old_own = registry.async_get_or_create(
        config_entry_id=old.entry_id, identifiers={(DOMAIN, "sub1")}, name="Router"
    )
    await _migrate(hass, old)

    # The old entry is the hub
    assert old.version == 2
    assert old.unique_id == HUB_UNIQUE_ID
    assert dict(old.data) == {CONF_KIND: KIND_HUB}
    assert dict(old.options) == {CONF_DISCOVERY: True, CONF_LINK_DEVICE_PAGES: False}
    assert not old.subentries
    assert old.state is ConfigEntryState.LOADED
    assert registry.async_get(old_own.id) is None
    hub = _hub(hass)
    assert hub.hub_entry is old

    views = {e.title: e for e in _view_entries(hass)}
    assert set(views) == {"Router", "Porch custom"}
    router, porch = views["Router"], views["Porch custom"]
    assert router.source == SOURCE_IMPORT
    assert router.unique_id is None
    assert dict(router.data) == {CONF_KIND: KIND_VIEW, CONF_PREVIOUS_VIEW_ID: "sub1"}
    assert dict(router.options) == router_data
    assert porch.source == SOURCE_IMPORT
    assert porch.unique_id == device_unique_id(dev.id)
    assert dict(porch.data) == {
        CONF_KIND: KIND_VIEW,
        CONF_DEVICE_ID: dev.id,
        CONF_PREVIOUS_VIEW_ID: "sub2",
    }
    assert dict(porch.options) == {
        CONF_MODE: MODE_ISOLATED,
        CONF_VERIFY_SSL: False,
        CONF_SHOW_IN_SIDEBAR: False,
        CONF_VISIT_LINK: VISIT_DEVICE,
    }
    assert set(hub.views) == {router.entry_id, porch.entry_id}
    assert all(e.state is ConfigEntryState.LOADED for e in (router, porch))
    assert _device_url(hass, dev) == DEVICE_URL
    assert _view_panel_path(router.entry_id) in _panels(hass)

    # Hidden device web UIs are ignored discoveries
    ignored = {e.unique_id: e for e in _entries(hass, True) if e.source == SOURCE_IGNORE}
    assert set(ignored) == {device_unique_id(hidden.id), device_unique_id("gone")}
    assert ignored[device_unique_id(hidden.id)].title == "Pod"
    assert ignored[device_unique_id("gone")].title == "gone"
    assert _discovery_flows(hass) == []

    # Site data follows the web UIs to their new ids
    assert hub.shim_storage(user_id, router.entry_id) == {"a": "1"}
    assert hub.shim_storage(user_id, porch.entry_id) == {"b": "2"}
    assert hub.shim_storage(user_id, "sub1") == {}
    assert hub.shim_storage(user_id, f"d_{dev.id}") == {}


async def test_migration_moves_site_data_of_old_subentry_id(
    hass: HomeAssistant,
    http: None,
    hass_storage: dict[str, Any],
    hass_admin_user: MockUser,
) -> None:
    """Site data of a device's web UI kept under its subentry id moves too."""
    user_id = hass_admin_user.id
    dev = _device_with_web_ui(hass)
    hass_storage[STORAGE_KEY_JAR] = {
        "version": 1,
        "key": STORAGE_KEY_JAR,
        "data": {"cookies": {}, "storage": {f"{user_id}|sub2": {"x": "1"}}},
    }
    await _migrate(hass, _v1_entry(subentries=[_v1_device_view("sub2", "Porch", dev.id)]))
    [porch] = _view_entries(hass)
    assert _hub(hass).shim_storage(user_id, porch.entry_id) == {"x": "1"}


@pytest.mark.parametrize(
    ("override", "expected"),
    [(None, None), (True, VISIT_HERE), (False, VISIT_DEVICE)],
)
async def test_migration_link_overrides(
    hass: HomeAssistant,
    http: None,
    hass_storage: dict[str, Any],
    override: bool | None,
    expected: str | None,
) -> None:
    dev = _device_with_web_ui(hass)
    _v1_storage(hass_storage, link_overrides={} if override is None else {dev.id: override})
    await _migrate(
        hass,
        _v1_entry(
            options={CONF_DISCOVERY: True, CONF_LINK_DEVICE_PAGES: True},
            subentries=[_v1_device_view("sub2", "Porch", dev.id)],
        ),
    )
    [porch] = _view_entries(hass)
    assert porch.options.get(CONF_VISIT_LINK) == expected
    linked = f"{DEVICE_LINK_PREFIX}{porch.entry_id}"
    assert _device_url(hass, dev) == (DEVICE_URL if expected == VISIT_DEVICE else linked)


async def test_migration_without_subentries(
    hass: HomeAssistant, http: None, hass_storage: dict[str, Any]
) -> None:
    old = _v1_entry()
    await _migrate(hass, old)
    assert old.version == 2
    assert old.unique_id == HUB_UNIQUE_ID
    assert dict(old.data) == {CONF_KIND: KIND_HUB}
    assert dict(old.options) == {CONF_DISCOVERY: True, CONF_LINK_DEVICE_PAGES: False}
    assert [e.entry_id for e in _entries(hass, True)] == [old.entry_id]
    assert _hub(hass).hub_entry is old
    assert _hub(hass).views == {}
    assert _discovery_flows(hass) == []
    assert _panels(hass)[PANEL_URL_PATH].show_in_sidebar is True


async def test_migration_offers_devices_not_yet_added(hass: HomeAssistant, http: None) -> None:
    """A device that 0.2 only listed (neither pinned nor hidden) is offered as discovered."""
    dev = _device_with_web_ui(hass)
    await _migrate(hass, _v1_entry())
    [flow] = _discovery_flows(hass)
    assert flow["context"]["unique_id"] == device_unique_id(dev.id)


async def test_migration_hidden_device_with_web_ui_of_its_own(
    hass: HomeAssistant, http: None, hass_storage: dict[str, Any]
) -> None:
    """A device hidden in 0.2 but also pinned as a web UI keeps its web UI."""
    dev = _device_with_web_ui(hass)
    _v1_storage(hass_storage, hidden=[f"d_{dev.id}"])
    await _migrate(hass, _v1_entry(subentries=[_v1_device_view("sub2", "Porch", dev.id)]))
    matching = [e for e in _entries(hass, True) if e.unique_id == device_unique_id(dev.id)]
    assert [e.source for e in matching] == [SOURCE_IMPORT]
    assert matching[0].entry_id in _hub(hass).views


async def test_migration_is_idempotent(
    hass: HomeAssistant, http: None, hass_storage: dict[str, Any]
) -> None:
    dev = _device_with_web_ui(hass)
    hidden = _device_with_web_ui(hass, "Pod", "http://192.168.1.60/", "aa:bb:cc:dd:ee:02")
    _v1_storage(hass_storage, hidden=[f"d_{hidden.id}"])
    old = _v1_entry(
        subentries=[
            _v1_view("sub1", "Router", {CONF_URL: "http://192.168.1.1/", CONF_MODE: MODE_ISOLATED}),
            _v1_device_view("sub2", "Porch", dev.id),
        ]
    )
    await _migrate(hass, old)

    def snapshot() -> list[tuple[Any, ...]]:
        return sorted(
            (
                e.entry_id,
                e.title,
                e.source,
                e.unique_id or "",
                sorted(e.data.items()),
                sorted(e.options.items()),
                e.version,
            )
            for e in _entries(hass, True)
        )

    before = snapshot()
    assert len(before) == 4

    # Migrating again changes nothing
    assert await async_migrate_entry(hass, old)
    await hass.async_block_till_done()
    assert snapshot() == before

    # Nor do reloads and restarts of the entries
    assert await hass.config_entries.async_reload(old.entry_id)
    for entry in _entries(hass):
        assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    for entry in _entries(hass):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert snapshot() == before
    assert all(e.state is ConfigEntryState.LOADED for e in _entries(hass))
    assert _discovery_flows(hass) == []
    assert len(_hub(hass).views) == 2


async def test_migration_refuses_newer_versions(hass: HomeAssistant, http: None) -> None:
    entry = MockConfigEntry(domain=DOMAIN, version=3, data={CONF_KIND: KIND_HUB})
    entry.add_to_hass(hass)
    assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.MIGRATION_ERROR


# ---- panels ----------------------------------------------------------------------


async def test_main_panel(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    panel = _panels(hass)[PANEL_URL_PATH]
    assert panel.component_name == "custom"
    assert panel.sidebar_title == NAME
    assert panel.sidebar_icon == DEFAULT_PANEL_ICON
    assert panel.show_in_sidebar is True
    assert panel.require_admin is True
    assert panel.config["_panel_custom"]["name"] == PANEL_COMPONENT
    assert panel.config["_panel_custom"]["embed_iframe"] is False
    assert "view_id" not in panel.config


async def test_main_panel_hidden_by_show_panel(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    await _set_options(hass, hub_entry, HUB_OPTIONS | {CONF_SHOW_PANEL: False})
    # Still registered: device "Visit" links open web UIs through it
    assert _panels(hass)[PANEL_URL_PATH].show_in_sidebar is False
    await _set_options(hass, hub_entry, HUB_OPTIONS)
    assert _panels(hass)[PANEL_URL_PATH].show_in_sidebar is True


async def test_main_panel_hidden_without_hub(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    await _set_options(
        hass, hub_entry, HUB_OPTIONS | {CONF_PANEL_TITLE: "Devices", CONF_PANEL_ICON: "mdi:lan"}
    )
    router = await _add_router(hass)
    assert await hass.config_entries.async_remove(hub_entry.entry_id)
    await hass.async_block_till_done()
    assert _hub(hass).active
    assert router.entry_id in _hub(hass).views
    panel = _panels(hass)[PANEL_URL_PATH]
    assert panel.show_in_sidebar is False
    assert (panel.sidebar_title, panel.sidebar_icon) == (NAME, DEFAULT_PANEL_ICON)


async def test_main_panel_hidden_with_hub_disabled(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    await _add_router(hass)
    await hass.config_entries.async_set_disabled_by(hub_entry.entry_id, ConfigEntryDisabler.USER)
    await hass.async_block_till_done()
    assert _panels(hass)[PANEL_URL_PATH].show_in_sidebar is False
    await hass.config_entries.async_set_disabled_by(hub_entry.entry_id, None)
    await hass.async_block_till_done()
    assert _panels(hass)[PANEL_URL_PATH].show_in_sidebar is True


async def test_web_ui_sidebar_panel(hass: HomeAssistant, hub_entry: ConfigEntry) -> None:
    entry = await _add_router(hass, **{CONF_SHOW_IN_SIDEBAR: True})
    path = _view_panel_path(entry.entry_id)
    panel = _panels(hass)[path]
    assert panel.component_name == "custom"
    assert (panel.sidebar_title, panel.sidebar_icon) == ("Router", "mdi:web")
    assert panel.show_in_sidebar is True
    assert panel.require_admin is True
    assert panel.config["view_id"] == entry.entry_id
    assert panel.config["_panel_custom"]["name"] == PANEL_COMPONENT

    # Renamed and given an icon
    await _set_options(
        hass,
        entry,
        VIEW_INPUT | {CONF_NAME: "Gateway", CONF_SHOW_IN_SIDEBAR: True, CONF_ICON: "mdi:lan"},
    )
    panel = _panels(hass)[path]
    assert (panel.sidebar_title, panel.sidebar_icon) == ("Gateway", "mdi:lan")

    # Taken out of the sidebar
    await _set_options(hass, entry, VIEW_INPUT | {CONF_NAME: "Gateway"})
    assert path not in _panels(hass)
    assert PANEL_URL_PATH in _panels(hass)

    # Back, then the web UI is deleted
    await _set_options(hass, entry, VIEW_INPUT | {CONF_SHOW_IN_SIDEBAR: True})
    assert path in _panels(hass)
    assert await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert path not in _panels(hass)
    assert PANEL_URL_PATH in _panels(hass)


async def test_web_ui_sidebar_panels_for_several_web_uis(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    first = await _add_router(hass, **{CONF_SHOW_IN_SIDEBAR: True})
    second = await _add_router(
        hass, **{CONF_NAME: "Printer", CONF_URL: "http://192.168.1.9/", CONF_SHOW_IN_SIDEBAR: True}
    )
    hidden = await _add_router(hass, **{CONF_NAME: "NAS", CONF_URL: "http://192.168.1.10/"})
    assert _panels(hass)[_view_panel_path(first.entry_id)].sidebar_title == "Router"
    assert _panels(hass)[_view_panel_path(second.entry_id)].sidebar_title == "Printer"
    assert _view_panel_path(hidden.entry_id) not in _panels(hass)


async def test_device_web_ui_sidebar_panel_follows_device(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    device, entry = await _porch(hass)
    await _set_options(hass, entry, DEVICE_OPTIONS_INPUT | {CONF_SHOW_IN_SIDEBAR: True})
    path = _view_panel_path(entry.entry_id)
    assert _panels(hass)[path].sidebar_title == "Porch"
    # The device is disabled: no web UI, no sidebar entry; back when enabled
    registry = dr.async_get(hass)
    registry.async_update_device(device.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await hass.async_block_till_done()
    assert entry.entry_id not in _hub(hass).views
    assert path not in _panels(hass)
    registry.async_update_device(device.id, disabled_by=None)
    await hass.async_block_till_done()
    assert path in _panels(hass)


async def test_panels_updated_only_when_they_change(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    entry = await _add_router(hass, **{CONF_SHOW_IN_SIDEBAR: True})
    main_panel = _panels(hass)[PANEL_URL_PATH]
    view_panel = _panels(hass)[_view_panel_path(entry.entry_id)]
    events = async_capture_events(hass, EVENT_PANELS_UPDATED)
    # Nothing a panel shows
    await _set_options(
        hass, entry, VIEW_INPUT | {CONF_SHOW_IN_SIDEBAR: True, CONF_VERIFY_SSL: False}
    )
    await _set_options(hass, hub_entry, HUB_OPTIONS | {CONF_DISCOVERY: False})
    assert events == []
    assert _panels(hass)[PANEL_URL_PATH] is main_panel
    assert _panels(hass)[_view_panel_path(entry.entry_id)] is view_panel


async def test_all_panels_removed_when_last_entry_unloads(
    hass: HomeAssistant, hub_entry: ConfigEntry
) -> None:
    entry = await _add_router(hass, **{CONF_SHOW_IN_SIDEBAR: True})
    path = _view_panel_path(entry.entry_id)

    assert await hass.config_entries.async_unload(hub_entry.entry_id)
    await hass.async_block_till_done()
    # The web UI is still loaded: its panels stay, the main one hidden
    assert _panels(hass)[PANEL_URL_PATH].show_in_sidebar is False
    assert path in _panels(hass)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert PANEL_URL_PATH not in _panels(hass)
    assert path not in _panels(hass)
    assert not _hub(hass).active

    for loaded in (entry, hub_entry):
        assert await hass.config_entries.async_setup(loaded.entry_id)
    await hass.async_block_till_done()
    assert path in _panels(hass)
    assert _panels(hass)[PANEL_URL_PATH].show_in_sidebar is True


async def test_panel_module_url_is_versioned_by_content(
    hass: HomeAssistant, hub_entry: ConfigEntry, hass_client: ClientSessionGenerator
) -> None:
    module_url = _panels(hass)[PANEL_URL_PATH].config["_panel_custom"]["module_url"]
    assert re.fullmatch(
        re.escape(f"{STATIC_URL_PATH}/{PANEL_FILE.name}") + r"\?v=[0-9a-f]{12}", module_url
    )
    # The version is the start of the file's SHA-256
    content = await hass.async_add_executor_job(PANEL_FILE.read_bytes)
    assert module_url.endswith(f"?v={hashlib.sha256(content).hexdigest()[:12]}")

    # Sidebar entries of single web UIs load the same module
    entry = await _add_router(hass, **{CONF_SHOW_IN_SIDEBAR: True})
    panel = _panels(hass)[_view_panel_path(entry.entry_id)]
    assert panel.config["_panel_custom"]["module_url"] == module_url

    # Served with long-lived cache headers; the version changes with the file
    client = await hass_client()
    response = await client.get(module_url)
    assert response.status == 200
    assert await response.read() == content
    assert "max-age" in response.headers["Cache-Control"]


# ---- lifecycle ------------------------------------------------------------------


async def test_routes_and_commands_registered_once(
    hass: HomeAssistant,
    hub_entry: ConfigEntry,
    hass_client: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Routes cannot be unregistered: entry unload/setup cycles must not add them again."""

    def our_routes() -> list[tuple[str, str]]:
        return sorted(
            (route.method, route.resource.canonical)
            for route in hass.http.app.router.routes()
            if route.resource is not None
            and route.resource.canonical.startswith((PROXY_URL_PREFIX, STATIC_URL_PATH))
        )

    router = await _add_router(hass)
    routes = our_routes()
    assert [r for r in routes if r[1].startswith(PROXY_URL_PREFIX)] == [
        ("*", f"{PROXY_URL_PREFIX}/{{view_id}}/{{token}}/{{path}}")
    ]
    assert any(r[1].startswith(STATIC_URL_PATH) for r in routes)
    route_count = len(hass.http.app.router.routes())
    module_url = _panels(hass)[PANEL_URL_PATH].config["_panel_custom"]["module_url"]
    hub = _hub(hass)

    with (
        patch.object(
            websocket_api, "async_register_command", wraps=websocket_api.async_register_command
        ) as register_command,
        patch.object(
            hass.http,
            "async_register_static_paths",
            wraps=hass.http.async_register_static_paths,
        ) as register_static_paths,
    ):
        for _ in range(2):
            for entry in (hub_entry, router):
                assert await hass.config_entries.async_unload(entry.entry_id)
            await hass.async_block_till_done()
            assert PANEL_URL_PATH not in _panels(hass)
            for entry in (router, hub_entry):
                assert await hass.config_entries.async_setup(entry.entry_id)
            await hass.async_block_till_done()
            assert hub_entry.state is ConfigEntryState.LOADED
            assert router.state is ConfigEntryState.LOADED

        assert await hass.config_entries.async_reload(hub_entry.entry_id)
        assert await hass.config_entries.async_reload(router.entry_id)
        await hass.async_block_till_done()

        # All removed, and set up again through the config flow
        for entry in (router, hub_entry):
            await hass.config_entries.async_remove(entry.entry_id)
        await hass.async_block_till_done()
        assert PANEL_URL_PATH not in _panels(hass)
        new_hub = await _create_hub(hass)
        new_router = await _add_router(hass)

    assert new_hub.state is ConfigEntryState.LOADED
    register_command.assert_not_called()
    register_static_paths.assert_not_called()
    assert our_routes() == routes
    assert len(hass.http.app.router.routes()) == route_count
    assert _panels(hass)[PANEL_URL_PATH].config["_panel_custom"]["module_url"] == module_url
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]
    # One hub object for the whole run
    assert _hub(hass) is hub
    assert new_hub.runtime_data is hub
    assert new_router.runtime_data is hub

    # The commands and the proxy use the current entries
    ws = await hass_ws_client(hass)
    await ws.send_json_auto_id({"type": f"{DOMAIN}/views"})
    msg = await ws.receive_json()
    assert msg["success"], msg
    assert msg["result"]["hub_entry_id"] == new_hub.entry_id
    assert [v["view_id"] for v in msg["result"]["views"]] == [new_router.entry_id]
    client = await hass_client()
    response = await client.get(f"{PROXY_URL_PREFIX}/nope/nope/")
    assert response.status == 404


# ---- translations ------------------------------------------------------------


def _strings(filename: str) -> dict[str, Any]:
    return json.loads((COMPONENT_DIR / filename).read_text())


def _all_strings(node: Any, path: str = "") -> list[tuple[str, Any]]:
    if isinstance(node, dict):
        return [
            item for key, value in node.items() for item in _all_strings(value, f"{path}.{key}")
        ]
    return [(path, node)]


def _web_ui_fields(manual: bool) -> set[str]:
    return set(_fields(config_flow._view_schema(manual=manual, device=not manual)))


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_translations_cover_config_flow(filename: str) -> None:
    config = _strings(filename)["config"]
    assert "{name}" in config["flow_title"]
    assert set(config["step"]) == {"hub", "web_ui", "discovery_confirm"}
    for step in config["step"].values():
        assert step["title"]
        assert step["description"]
    # Placeholders used are ones the flow provides
    placeholders = re.findall(r"{(\w+)}", json.dumps(config["step"]["discovery_confirm"]))
    assert set(placeholders) <= {"name", "url", "subtitle"}
    web_ui = config["step"]["web_ui"]
    assert _web_ui_fields(manual=True) <= set(web_ui["data"])
    assert set(web_ui.get("data_description", {})) <= set(web_ui["data"])
    assert {"name_required", "invalid_url", "trusted_not_acknowledged"} <= set(config["error"])


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_translations_cover_config_flow_aborts(filename: str) -> None:
    # Every reason the config flow can abort with
    reached = {"already_configured", "already_in_progress"}
    assert reached <= set(_strings(filename)["config"]["abort"])


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_translations_cover_options_flows(filename: str) -> None:
    options = _strings(filename)["options"]
    assert set(options["step"]) == {"hub", "web_ui"}

    hub = options["step"]["hub"]
    hub_fields = {*HUB_OPTIONS, CONF_PANEL_TITLE, CONF_PANEL_ICON}
    assert set(hub["data"]) == hub_fields
    assert set(hub["data_description"]) <= hub_fields
    assert "{panel_url}" in hub["description"]

    web_ui = options["step"]["web_ui"]
    form_fields = _web_ui_fields(manual=True) | _web_ui_fields(manual=False)
    assert set(web_ui["data"]) == form_fields
    assert set(web_ui.get("data_description", {})) <= form_fields
    assert CONF_VISIT_LINK in web_ui["data_description"]
    assert set(options["error"]) == {"name_required", "invalid_url", "trusted_not_acknowledged"}


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_translations_cover_selectors(filename: str) -> None:
    selectors = _strings(filename)["selector"]
    assert set(selectors[CONF_MODE]["options"]) == set(MODES)
    assert set(selectors[CONF_VISIT_LINK]["options"]) == set(VISIT_CHOICES)


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_translations_follow_hassfest_rules(filename: str) -> None:
    for path, value in _all_strings(_strings(filename)):
        assert isinstance(value, str), path
        assert value, path
        assert value == value.strip(), path
        assert not RE_URL.search(value), f"{path}: no URLs; use a description placeholder"
        assert not re.search(r"'{\w+}'", value), path
        assert "<" not in value, path


def test_strings_and_english_translation_match() -> None:
    assert _strings("strings.json") == _strings("translations/en.json")
