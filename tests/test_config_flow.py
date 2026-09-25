"""Config flow, options flow and "view" subentry flow of Local Web UIs."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    ConfigEntryState,
    ConfigSubentry,
)
from homeassistant.core import HomeAssistant
from homeassistant.data_entry_flow import FlowResultType, InvalidData, UnknownHandler
from homeassistant.helpers import device_registry as dr
from homeassistant.setup import async_setup_component
import pytest
from pytest_homeassistant_custom_component.common import MockConfigEntry
import voluptuous as vol

from custom_components.local_web_ui import config_flow
from custom_components.local_web_ui.const import (
    CONF_DEVICE_ID,
    CONF_DISCOVERY,
    CONF_ICON,
    CONF_LINK_DEVICE_PAGES,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SHOW_IN_SIDEBAR,
    CONF_TRUSTED_ACK,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEVICE_LINK_PREFIX,
    DISCOVERED_PREFIX,
    DOMAIN,
    MODE_ISOLATED,
    MODE_TRUSTED,
    MODES,
    NAME,
    PANEL_COMPONENT,
    PANEL_URL_PATH,
    SUBENTRY_TYPE_VIEW,
)

COMPONENT_DIR = Path(__file__).parent.parent / "custom_components" / DOMAIN
CONF_NAME = "name"

VIEW_INPUT: dict[str, Any] = {
    CONF_NAME: "Router",
    CONF_URL: "http://192.168.1.1/admin/?tab=1",
    CONF_MODE: MODE_ISOLATED,
    CONF_TRUSTED_ACK: False,
    CONF_VERIFY_SSL: True,
    CONF_SHOW_IN_SIDEBAR: False,
}


# ---- helpers -----------------------------------------------------------------


def _panels(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.get("frontend_panels", {})


def _view_panel_path(subentry_id: str) -> str:
    return f"{PANEL_URL_PATH}-{subentry_id.lower()}"


def _fields(schema: vol.Schema) -> dict[str, vol.Marker]:
    return {str(marker): marker for marker in schema.schema}


def _default(marker: vol.Marker) -> Any:
    return vol.UNDEFINED if marker.default is vol.UNDEFINED else marker.default()


def _form_value(marker: vol.Marker) -> Any:
    """What the frontend prefills: suggested_value wins over the default."""
    suggested = (marker.description or {}).get("suggested_value")
    return suggested if suggested is not None else _default(marker)


async def _start_view_flow(hass: HomeAssistant, entry: MockConfigEntry) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_VIEW), context={"source": SOURCE_USER}
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    return result


async def _add_view(
    hass: HomeAssistant, entry: MockConfigEntry, user_input: dict[str, Any]
) -> tuple[dict[str, Any], ConfigSubentry | None]:
    """Run the add-web-UI flow; return the last result and the new subentry, if any."""
    before = set(entry.subentries)
    result = await _start_view_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    new = set(entry.subentries) - before
    assert len(new) <= 1
    return result, (entry.subentries[new.pop()] if new else None)


async def _start_reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, subentry_id: str
) -> dict[str, Any]:
    result = await hass.config_entries.subentries.async_init(
        (entry.entry_id, SUBENTRY_TYPE_VIEW),
        context={"source": SOURCE_RECONFIGURE, "subentry_id": subentry_id},
    )
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    return result


async def _reconfigure(
    hass: HomeAssistant, entry: MockConfigEntry, subentry_id: str, user_input: dict[str, Any]
) -> dict[str, Any]:
    result = await _start_reconfigure(hass, entry, subentry_id)
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], user_input)
    await hass.async_block_till_done()
    return result


def _add_subentry_directly(
    hass: HomeAssistant, entry: MockConfigEntry, title: str, data: dict[str, Any]
) -> ConfigSubentry:
    subentry = ConfigSubentry(
        data=data, subentry_type=SUBENTRY_TYPE_VIEW, title=title, unique_id=None
    )
    hass.config_entries.async_add_subentry(entry, subentry)
    return subentry


# ---- fixtures ----------------------------------------------------------------


@pytest.fixture
async def http(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})


@pytest.fixture
async def entry(hass: HomeAssistant, http: None) -> MockConfigEntry:
    """A loaded Local Web UIs entry with the options the config flow creates."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=NAME,
        data={},
        options={CONF_DISCOVERY: True, CONF_LINK_DEVICE_PAGES: True},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


# ---- config flow -------------------------------------------------------------


async def test_user_flow_creates_entry_with_default_options(
    hass: HomeAssistant, http: None
) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["data_schema"] is None  # A confirmation step, nothing to fill in

    result = await hass.config_entries.flow.async_configure(result["flow_id"], {})
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == NAME
    assert result["data"] == {}
    assert result["options"] == {CONF_DISCOVERY: True, CONF_LINK_DEVICE_PAGES: True}

    entries = hass.config_entries.async_entries(DOMAIN)
    assert len(entries) == 1
    created = entries[0]
    assert created.options == {CONF_DISCOVERY: True, CONF_LINK_DEVICE_PAGES: True}
    assert created.subentries == {}
    assert created.state is ConfigEntryState.LOADED
    assert PANEL_URL_PATH in _panels(hass)


async def test_user_flow_single_instance(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "single_instance_allowed"
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


async def test_parallel_user_flows_only_create_one_entry(hass: HomeAssistant, http: None) -> None:
    first = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    second = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert first["type"] is second["type"] is FlowResultType.FORM

    result = await hass.config_entries.flow.async_configure(first["flow_id"], {})
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY

    # Home Assistant aborts the other flow once the single entry exists
    assert not hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(hass.config_entries.async_entries(DOMAIN)) == 1


def test_manifest_declares_single_config_entry() -> None:
    manifest = json.loads((COMPONENT_DIR / "manifest.json").read_text())
    assert manifest["single_config_entry"] is True
    assert manifest["config_flow"] is True


# ---- options flow ------------------------------------------------------------


async def test_options_flow_prefills_current_options(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    hass.config_entries.async_update_entry(
        entry, options={CONF_DISCOVERY: False, CONF_LINK_DEVICE_PAGES: True}
    )
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "init"
    fields = _fields(result["data_schema"])
    assert set(fields) == {CONF_DISCOVERY, CONF_LINK_DEVICE_PAGES}
    assert _default(fields[CONF_DISCOVERY]) is False
    assert _default(fields[CONF_LINK_DEVICE_PAGES]) is True


async def test_options_flow_defaults_when_options_missing(hass: HomeAssistant, http: None) -> None:
    """Entries created before options existed fall back to the defaults (both on)."""
    entry = MockConfigEntry(domain=DOMAIN, title=NAME, options={})
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    fields = _fields(result["data_schema"])
    assert _default(fields[CONF_DISCOVERY]) is True
    assert _default(fields[CONF_LINK_DEVICE_PAGES]) is True


async def test_options_flow_saves_and_reloads(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    old_hub = entry.runtime_data
    assert old_hub.discovery_enabled is True
    assert old_hub.link_default is True

    result = await hass.config_entries.options.async_init(entry.entry_id)
    result = await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DISCOVERY: False, CONF_LINK_DEVICE_PAGES: False}
    )
    await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert entry.options == {CONF_DISCOVERY: False, CONF_LINK_DEVICE_PAGES: False}
    assert entry.state is ConfigEntryState.LOADED
    # The update listener reloaded the entry: a fresh hub reads the new options
    new_hub = entry.runtime_data
    assert new_hub is not old_hub
    assert hass.data[DOMAIN] is new_hub
    assert new_hub.discovery_enabled is False
    assert new_hub.link_default is False
    assert PANEL_URL_PATH in _panels(hass)


async def test_options_link_device_pages_toggles_device_visit_link(
    hass: HomeAssistant, entry: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    """Turning link_device_pages off in options gives the device its own URL back."""
    other = MockConfigEntry(domain="fake_esphome")
    other.add_to_hass(hass)
    original = "http://192.168.1.50/"
    device = device_registry.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("fake_esphome", "kitchen")},
        name="Kitchen",
        configuration_url=original,
    )
    await hass.async_block_till_done()
    linked = f"{DEVICE_LINK_PREFIX}{DISCOVERED_PREFIX}{device.id}"
    assert device_registry.async_get(device.id).configuration_url == linked

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DISCOVERY: True, CONF_LINK_DEVICE_PAGES: False}
    )
    await hass.async_block_till_done()
    assert device_registry.async_get(device.id).configuration_url == original
    # The view is still discovered, only the Visit link is left alone
    assert f"{DISCOVERED_PREFIX}{device.id}" in entry.runtime_data.discovered_views()

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DISCOVERY: True, CONF_LINK_DEVICE_PAGES: True}
    )
    await hass.async_block_till_done()
    assert device_registry.async_get(device.id).configuration_url == linked


async def test_options_discovery_off_hides_views_and_restores_links(
    hass: HomeAssistant, entry: MockConfigEntry, device_registry: dr.DeviceRegistry
) -> None:
    other = MockConfigEntry(domain="fake_esphome")
    other.add_to_hass(hass)
    original = "http://192.168.1.51:8080/"
    device = device_registry.async_get_or_create(
        config_entry_id=other.entry_id,
        identifiers={("fake_esphome", "porch")},
        name="Porch",
        configuration_url=original,
    )
    await hass.async_block_till_done()
    assert device_registry.async_get(device.id).configuration_url.startswith(DEVICE_LINK_PREFIX)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    await hass.config_entries.options.async_configure(
        result["flow_id"], {CONF_DISCOVERY: False, CONF_LINK_DEVICE_PAGES: True}
    )
    await hass.async_block_till_done()
    assert entry.runtime_data.discovered_views() == {}
    assert device_registry.async_get(device.id).configuration_url == original


# ---- view subentry: add ------------------------------------------------------


async def test_view_user_step_form(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result = await _start_view_flow(hass, entry)
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
    assert _default(fields[CONF_MODE]) == MODE_ISOLATED
    assert _default(fields[CONF_TRUSTED_ACK]) is False
    assert _default(fields[CONF_VERIFY_SSL]) is True
    assert _default(fields[CONF_SHOW_IN_SIDEBAR]) is False
    assert _default(fields[CONF_NAME]) is vol.UNDEFINED
    assert _default(fields[CONF_URL]) is vol.UNDEFINED
    for optional in (CONF_USERNAME, CONF_PASSWORD, CONF_ICON):
        assert isinstance(fields[optional], vol.Optional)


async def test_view_user_step_creates_subentry(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    result, subentry = await _add_view(hass, entry, VIEW_INPUT)

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Router"
    assert subentry is not None
    assert subentry.subentry_type == SUBENTRY_TYPE_VIEW
    assert subentry.title == "Router"
    assert dict(subentry.data) == {
        CONF_URL: "http://192.168.1.1/admin/?tab=1",
        CONF_MODE: MODE_ISOLATED,
        CONF_TRUSTED_ACK: False,
        CONF_VERIFY_SSL: True,
        CONF_SHOW_IN_SIDEBAR: False,
    }
    # The automatic reload picked the new view up, keyed by the subentry id
    assert entry.state is ConfigEntryState.LOADED
    view = entry.runtime_data.static_views[subentry.subentry_id]
    assert view.name == "Router"
    assert view.url == "http://192.168.1.1/admin/?tab=1"
    assert view.source == "static"
    assert view.auth is None
    # Not flagged for the sidebar: no extra panel
    assert _view_panel_path(subentry.subentry_id) not in _panels(hass)


async def test_view_user_step_fills_schema_defaults(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _, subentry = await _add_view(
        hass, entry, {CONF_NAME: "Printer", CONF_URL: "https://printer.local:8443/"}
    )
    assert subentry is not None
    assert dict(subentry.data) == {
        CONF_URL: "https://printer.local:8443/",
        CONF_MODE: MODE_ISOLATED,
        CONF_TRUSTED_ACK: False,
        CONF_VERIFY_SSL: True,
        CONF_SHOW_IN_SIDEBAR: False,
    }


async def test_view_user_step_strips_and_stores_all_fields(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    result, subentry = await _add_view(
        hass,
        entry,
        {
            CONF_NAME: "  NAS  ",
            CONF_URL: "  https://nas.lan:5001/ui  ",
            CONF_MODE: MODE_TRUSTED,
            CONF_TRUSTED_ACK: True,
            CONF_VERIFY_SSL: False,
            CONF_USERNAME: "  admin  ",
            CONF_PASSWORD: "hunter2",
            CONF_SHOW_IN_SIDEBAR: False,
            CONF_ICON: "mdi:nas",
        },
    )
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "NAS"
    assert subentry is not None
    assert subentry.title == "NAS"
    assert dict(subentry.data) == {
        CONF_URL: "https://nas.lan:5001/ui",
        CONF_MODE: MODE_TRUSTED,
        CONF_TRUSTED_ACK: True,
        CONF_VERIFY_SSL: False,
        CONF_USERNAME: "admin",
        CONF_PASSWORD: "hunter2",
        CONF_SHOW_IN_SIDEBAR: False,
        CONF_ICON: "mdi:nas",
    }
    view = entry.runtime_data.static_views[subentry.subentry_id]
    assert view.mode == MODE_TRUSTED
    assert view.verify_ssl is False
    assert view.auth is not None
    assert (view.auth.login, view.auth.password) == ("admin", "hunter2")
    assert view.icon == "mdi:nas"


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
async def test_view_user_step_credentials_and_icon(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    extra: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    _, subentry = await _add_view(hass, entry, VIEW_INPUT | extra)
    assert subentry is not None
    stored = {
        key: value
        for key, value in subentry.data.items()
        if key in (CONF_USERNAME, CONF_PASSWORD, CONF_ICON)
    }
    assert stored == expected


async def test_view_user_step_ignores_device_id(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """device_id is not part of the form and cannot be injected through it."""
    result = await _start_view_flow(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], VIEW_INPUT | {CONF_DEVICE_ID: "abc"}
        )
    assert entry.subentries == {}


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
async def test_view_user_step_validation_errors(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    changes: dict[str, Any],
    errors: dict[str, str],
) -> None:
    user_input = VIEW_INPUT | changes
    result, subentry = await _add_view(hass, entry, user_input)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "user"
    assert result["errors"] == errors
    assert subentry is None
    assert entry.subentries == {}
    # What the user typed comes back, so fixing one field does not mean retyping all
    fields = _fields(result["data_schema"])
    for key, value in user_input.items():
        assert _form_value(fields[key]) == value


async def test_view_user_step_recovers_after_error(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    result = await _start_view_flow(hass, entry)
    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"], VIEW_INPUT | {CONF_URL: "not a url"}
    )
    assert result["errors"] == {CONF_URL: "invalid_url"}
    result = await hass.config_entries.subentries.async_configure(result["flow_id"], VIEW_INPUT)
    await hass.async_block_till_done()
    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert len(entry.subentries) == 1


async def test_view_user_step_trusted_with_ack(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    _, subentry = await _add_view(
        hass, entry, VIEW_INPUT | {CONF_MODE: MODE_TRUSTED, CONF_TRUSTED_ACK: True}
    )
    assert subentry is not None
    assert subentry.data[CONF_MODE] == MODE_TRUSTED
    assert subentry.data[CONF_TRUSTED_ACK] is True


async def test_view_user_step_rejects_unknown_mode(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    result = await _start_view_flow(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], VIEW_INPUT | {CONF_MODE: "bogus"}
        )
    assert entry.subentries == {}


async def test_view_user_step_requires_name_and_url_keys(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    result = await _start_view_flow(hass, entry)
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_URL: "http://192.168.1.1/"}
        )
    with pytest.raises(InvalidData):
        await hass.config_entries.subentries.async_configure(
            result["flow_id"], {CONF_NAME: "Router"}
        )


async def test_unknown_subentry_type_is_rejected(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    with pytest.raises(UnknownHandler):
        await hass.config_entries.subentries.async_init(
            (entry.entry_id, "bogus"), context={"source": SOURCE_USER}
        )


@pytest.mark.xfail(
    strict=True,
    reason="BUG: credentials in the URL are accepted, silently ignored by the proxy and stored unredacted",
)
async def test_view_url_with_credentials_is_not_stored_verbatim(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """http://user:pass@host/ must be rejected or split into the credential fields.

    Today the flow stores it as is: the proxy drops the userinfo (view origin), so
    Basic auth is never sent, and the password sits in the url field, which
    diagnostics does not redact.
    """
    result, subentry = await _add_view(
        hass, entry, VIEW_INPUT | {CONF_URL: "http://admin:s3cret@192.168.1.1/"}
    )
    if result["type"] is FlowResultType.FORM:
        assert CONF_URL in result["errors"]
        return
    assert subentry is not None
    assert "s3cret" not in subentry.data[CONF_URL]


# ---- view subentry: reconfigure ---------------------------------------------


async def test_reconfigure_form_is_prefilled(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    _, subentry = await _add_view(
        hass,
        entry,
        {
            CONF_NAME: "NAS",
            CONF_URL: "https://nas.lan:5001/",
            CONF_MODE: MODE_TRUSTED,
            CONF_TRUSTED_ACK: True,
            CONF_VERIFY_SSL: False,
            CONF_USERNAME: "admin",
            CONF_PASSWORD: "hunter2",
            CONF_SHOW_IN_SIDEBAR: True,
            CONF_ICON: "mdi:nas",
        },
    )
    assert subentry is not None

    result = await _start_reconfigure(hass, entry, subentry.subentry_id)
    assert result["errors"] == {}
    fields = _fields(result["data_schema"])
    assert _form_value(fields[CONF_NAME]) == "NAS"
    assert _form_value(fields[CONF_URL]) == "https://nas.lan:5001/"
    assert _form_value(fields[CONF_MODE]) == MODE_TRUSTED
    assert _form_value(fields[CONF_TRUSTED_ACK]) is True
    assert _form_value(fields[CONF_VERIFY_SSL]) is False
    assert _form_value(fields[CONF_USERNAME]) == "admin"
    assert _form_value(fields[CONF_SHOW_IN_SIDEBAR]) is True
    assert _form_value(fields[CONF_ICON]) == "mdi:nas"
    # The stored password is never sent back to the browser
    password = fields[CONF_PASSWORD]
    assert _default(password) is vol.UNDEFINED
    assert not (password.description or {}).get("suggested_value")
    assert "hunter2" not in repr(result["data_schema"].schema)


async def test_reconfigure_updates_subentry_and_reloads(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _, subentry = await _add_view(hass, entry, VIEW_INPUT)
    assert subentry is not None
    old_hub = entry.runtime_data

    result = await _reconfigure(
        hass,
        entry,
        subentry.subentry_id,
        VIEW_INPUT
        | {
            CONF_NAME: " Gateway ",
            CONF_URL: "https://192.168.1.2:8443/",
            CONF_VERIFY_SSL: False,
        },
    )
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"

    updated = entry.subentries[subentry.subentry_id]
    assert updated.title == "Gateway"
    assert dict(updated.data) == {
        CONF_URL: "https://192.168.1.2:8443/",
        CONF_MODE: MODE_ISOLATED,
        CONF_TRUSTED_ACK: False,
        CONF_VERIFY_SSL: False,
        CONF_SHOW_IN_SIDEBAR: False,
    }
    assert len(entry.subentries) == 1
    assert entry.runtime_data is not old_hub
    view = entry.runtime_data.static_views[subentry.subentry_id]
    assert view.name == "Gateway"
    assert view.url == "https://192.168.1.2:8443/"
    assert view.verify_ssl is False


async def test_reconfigure_unchanged_does_not_reload(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _, subentry = await _add_view(hass, entry, VIEW_INPUT)
    assert subentry is not None
    hub = entry.runtime_data
    result = await _reconfigure(hass, entry, subentry.subentry_id, VIEW_INPUT)
    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.runtime_data is hub


@pytest.mark.parametrize("password_input", [{}, {CONF_PASSWORD: ""}])
async def test_reconfigure_empty_password_keeps_stored_one(
    hass: HomeAssistant, entry: MockConfigEntry, password_input: dict[str, Any]
) -> None:
    _, subentry = await _add_view(
        hass, entry, VIEW_INPUT | {CONF_USERNAME: "admin", CONF_PASSWORD: "hunter2"}
    )
    assert subentry is not None

    await _reconfigure(
        hass,
        entry,
        subentry.subentry_id,
        VIEW_INPUT | {CONF_NAME: "Router 2", CONF_USERNAME: "admin"} | password_input,
    )
    data = entry.subentries[subentry.subentry_id].data
    assert data[CONF_USERNAME] == "admin"
    assert data[CONF_PASSWORD] == "hunter2"
    view = entry.runtime_data.static_views[subentry.subentry_id]
    assert view.auth is not None
    assert (view.auth.login, view.auth.password) == ("admin", "hunter2")


async def test_reconfigure_new_password_replaces_stored_one(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _, subentry = await _add_view(
        hass, entry, VIEW_INPUT | {CONF_USERNAME: "admin", CONF_PASSWORD: "hunter2"}
    )
    assert subentry is not None
    await _reconfigure(
        hass,
        entry,
        subentry.subentry_id,
        VIEW_INPUT | {CONF_USERNAME: "root", CONF_PASSWORD: "correct horse"},
    )
    data = entry.subentries[subentry.subentry_id].data
    assert data[CONF_USERNAME] == "root"
    assert data[CONF_PASSWORD] == "correct horse"


@pytest.mark.parametrize("username_input", [{}, {CONF_USERNAME: ""}, {CONF_USERNAME: "   "}])
async def test_reconfigure_removing_username_drops_password(
    hass: HomeAssistant, entry: MockConfigEntry, username_input: dict[str, Any]
) -> None:
    _, subentry = await _add_view(
        hass, entry, VIEW_INPUT | {CONF_USERNAME: "admin", CONF_PASSWORD: "hunter2"}
    )
    assert subentry is not None

    await _reconfigure(hass, entry, subentry.subentry_id, VIEW_INPUT | username_input)
    data = entry.subentries[subentry.subentry_id].data
    assert CONF_USERNAME not in data
    assert CONF_PASSWORD not in data
    assert entry.runtime_data.static_views[subentry.subentry_id].auth is None


async def test_reconfigure_removing_icon(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    _, subentry = await _add_view(hass, entry, VIEW_INPUT | {CONF_ICON: "mdi:router"})
    assert subentry is not None
    await _reconfigure(hass, entry, subentry.subentry_id, VIEW_INPUT)
    assert CONF_ICON not in entry.subentries[subentry.subentry_id].data


async def test_reconfigure_preserves_device_id(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    """A pinned view keeps its device link through edits (device_id is not in the form)."""
    subentry = _add_subentry_directly(
        hass,
        entry,
        "Kitchen",
        {
            CONF_URL: "http://192.168.1.50/",
            CONF_MODE: MODE_ISOLATED,
            CONF_VERIFY_SSL: False,
            CONF_SHOW_IN_SIDEBAR: False,
            CONF_DEVICE_ID: "device123",
        },
    )
    await hass.async_block_till_done()

    result = await _start_reconfigure(hass, entry, subentry.subentry_id)
    fields = _fields(result["data_schema"])
    assert CONF_DEVICE_ID not in fields
    # Subentries created by "pin" have no acknowledgement key; the form still works
    assert _form_value(fields[CONF_TRUSTED_ACK]) is False
    assert _form_value(fields[CONF_NAME]) == "Kitchen"

    result = await hass.config_entries.subentries.async_configure(
        result["flow_id"],
        {
            CONF_NAME: "Kitchen display",
            CONF_URL: "http://192.168.1.50/",
            CONF_MODE: MODE_ISOLATED,
            CONF_TRUSTED_ACK: False,
            CONF_VERIFY_SSL: False,
            CONF_SHOW_IN_SIDEBAR: False,
        },
    )
    await hass.async_block_till_done()
    assert result["reason"] == "reconfigure_successful"
    updated = entry.subentries[subentry.subentry_id]
    assert updated.title == "Kitchen display"
    assert updated.data[CONF_DEVICE_ID] == "device123"
    assert entry.runtime_data.static_views[subentry.subentry_id].device_id == "device123"


@pytest.mark.parametrize(
    ("changes", "errors"),
    [
        ({CONF_NAME: " "}, {CONF_NAME: "name_required"}),
        ({CONF_URL: "ftp://192.168.1.1/"}, {CONF_URL: "invalid_url"}),
        ({CONF_URL: "http:///x"}, {CONF_URL: "invalid_url"}),
        ({CONF_MODE: MODE_TRUSTED}, {CONF_TRUSTED_ACK: "trusted_not_acknowledged"}),
    ],
)
async def test_reconfigure_validation_errors(
    hass: HomeAssistant,
    entry: MockConfigEntry,
    changes: dict[str, Any],
    errors: dict[str, str],
) -> None:
    _, subentry = await _add_view(hass, entry, VIEW_INPUT)
    assert subentry is not None
    hub = entry.runtime_data

    result = await _reconfigure(hass, entry, subentry.subentry_id, VIEW_INPUT | changes)
    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "reconfigure"
    assert result["errors"] == errors
    # Nothing was saved and the entry was not reloaded
    unchanged = entry.subentries[subentry.subentry_id]
    assert unchanged.title == "Router"
    assert unchanged.data[CONF_URL] == VIEW_INPUT[CONF_URL]
    assert unchanged.data[CONF_MODE] == MODE_ISOLATED
    assert entry.runtime_data is hub


async def test_reconfigure_trusted_with_ack(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    _, subentry = await _add_view(hass, entry, VIEW_INPUT)
    assert subentry is not None
    await _reconfigure(
        hass,
        entry,
        subentry.subentry_id,
        VIEW_INPUT | {CONF_MODE: MODE_TRUSTED, CONF_TRUSTED_ACK: True},
    )
    data = entry.subentries[subentry.subentry_id].data
    assert data[CONF_MODE] == MODE_TRUSTED
    assert data[CONF_TRUSTED_ACK] is True
    assert entry.runtime_data.static_views[subentry.subentry_id].mode == MODE_TRUSTED


@pytest.mark.xfail(
    strict=True,
    reason="BUG: a reconfigure validation error re-shows the stored values, discarding the user's edits",
)
async def test_reconfigure_error_keeps_user_input(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    """Like the user step, the re-shown reconfigure form should carry what was typed."""
    _, subentry = await _add_view(hass, entry, VIEW_INPUT)
    assert subentry is not None

    typed = VIEW_INPUT | {CONF_NAME: "Renamed", CONF_URL: "htp://typo/"}
    result = await _reconfigure(hass, entry, subentry.subentry_id, typed)
    assert result["errors"] == {CONF_URL: "invalid_url"}
    fields = _fields(result["data_schema"])
    assert _form_value(fields[CONF_NAME]) == "Renamed"
    assert _form_value(fields[CONF_URL]) == "htp://typo/"


# ---- view subentry: sidebar panels ------------------------------------------


async def test_show_in_sidebar_registers_panel_and_removal_removes_it(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    assert set(_panels(hass)) & {PANEL_URL_PATH} == {PANEL_URL_PATH}

    _, subentry = await _add_view(
        hass,
        entry,
        VIEW_INPUT | {CONF_NAME: "Printer", CONF_SHOW_IN_SIDEBAR: True, CONF_ICON: "mdi:printer"},
    )
    assert subentry is not None
    path = _view_panel_path(subentry.subentry_id)
    panel = _panels(hass).get(path)
    assert panel is not None, "sidebar panel not registered after the automatic reload"
    assert panel.sidebar_title == "Printer"
    assert panel.sidebar_icon == "mdi:printer"
    assert panel.require_admin is True
    assert panel.config["view_id"] == subentry.subentry_id
    assert panel.config["_panel_custom"]["name"] == PANEL_COMPONENT
    # The main panel survives the reload
    assert PANEL_URL_PATH in _panels(hass)

    hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
    await hass.async_block_till_done()
    assert path not in _panels(hass)
    assert PANEL_URL_PATH in _panels(hass)
    assert entry.state is ConfigEntryState.LOADED
    assert subentry.subentry_id not in entry.runtime_data.static_views


async def test_sidebar_panel_default_icon(hass: HomeAssistant, entry: MockConfigEntry) -> None:
    _, subentry = await _add_view(hass, entry, VIEW_INPUT | {CONF_SHOW_IN_SIDEBAR: True})
    assert subentry is not None
    panel = _panels(hass)[_view_panel_path(subentry.subentry_id)]
    assert panel.sidebar_icon == "mdi:web"


async def test_sidebar_panels_for_several_views(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _, first = await _add_view(
        hass, entry, VIEW_INPUT | {CONF_NAME: "One", CONF_SHOW_IN_SIDEBAR: True}
    )
    _, second = await _add_view(
        hass, entry, VIEW_INPUT | {CONF_NAME: "Two", CONF_SHOW_IN_SIDEBAR: True}
    )
    _, third = await _add_view(hass, entry, VIEW_INPUT | {CONF_NAME: "Three"})
    assert first and second and third
    panels = _panels(hass)
    assert panels[_view_panel_path(first.subentry_id)].sidebar_title == "One"
    assert panels[_view_panel_path(second.subentry_id)].sidebar_title == "Two"
    assert _view_panel_path(third.subentry_id) not in panels

    hass.config_entries.async_remove_subentry(entry, first.subentry_id)
    await hass.async_block_till_done()
    panels = _panels(hass)
    assert _view_panel_path(first.subentry_id) not in panels
    assert _view_panel_path(second.subentry_id) in panels


async def test_reconfigure_toggles_and_renames_sidebar_panel(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _, subentry = await _add_view(hass, entry, VIEW_INPUT)
    assert subentry is not None
    path = _view_panel_path(subentry.subentry_id)
    assert path not in _panels(hass)

    await _reconfigure(hass, entry, subentry.subentry_id, VIEW_INPUT | {CONF_SHOW_IN_SIDEBAR: True})
    assert _panels(hass)[path].sidebar_title == "Router"

    await _reconfigure(
        hass,
        entry,
        subentry.subentry_id,
        VIEW_INPUT | {CONF_NAME: "Gateway", CONF_SHOW_IN_SIDEBAR: True, CONF_ICON: "mdi:router"},
    )
    assert _panels(hass)[path].sidebar_title == "Gateway"
    assert _panels(hass)[path].sidebar_icon == "mdi:router"

    await _reconfigure(
        hass, entry, subentry.subentry_id, VIEW_INPUT | {CONF_SHOW_IN_SIDEBAR: False}
    )
    assert path not in _panels(hass)
    assert PANEL_URL_PATH in _panels(hass)


async def test_unloading_entry_removes_sidebar_panels(
    hass: HomeAssistant, entry: MockConfigEntry
) -> None:
    _, subentry = await _add_view(hass, entry, VIEW_INPUT | {CONF_SHOW_IN_SIDEBAR: True})
    assert subentry is not None
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()
    assert _view_panel_path(subentry.subentry_id) not in _panels(hass)
    assert PANEL_URL_PATH not in _panels(hass)

    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert _view_panel_path(subentry.subentry_id) in _panels(hass)
    assert PANEL_URL_PATH in _panels(hass)


# ---- translations ------------------------------------------------------------


@pytest.mark.parametrize("filename", ["strings.json", "translations/en.json"])
def test_translations_cover_flows(filename: str) -> None:
    strings = json.loads((COMPONENT_DIR / filename).read_text())

    assert "single_instance_allowed" in strings["config"]["abort"]
    options_data = strings["options"]["step"]["init"]["data"]
    assert set(options_data) == {CONF_DISCOVERY, CONF_LINK_DEVICE_PAGES}

    view = strings["config_subentries"][SUBENTRY_TYPE_VIEW]
    assert view["initiate_flow"]["user"]
    assert "reconfigure_successful" in view["abort"]
    assert set(view["error"]) == {"name_required", "invalid_url", "trusted_not_acknowledged"}
    form_fields = set(_fields(config_flow._view_schema({})))
    for step in ("user", "reconfigure"):
        assert set(view["step"][step]["data"]) == form_fields
        # Descriptions only for fields that exist
        assert set(view["step"][step].get("data_description", {})) <= form_fields
    assert set(strings["selector"][CONF_MODE]["options"]) == set(MODES)


def test_strings_and_english_translation_match() -> None:
    strings = json.loads((COMPONENT_DIR / "strings.json").read_text())
    english = json.loads((COMPONENT_DIR / "translations" / "en.json").read_text())
    assert strings == english
