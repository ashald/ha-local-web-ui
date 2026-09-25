"""Websocket API for the panel. Admin only."""

from __future__ import annotations

from types import MappingProxyType
from typing import TYPE_CHECKING, Any

from homeassistant.components import websocket_api
from homeassistant.config_entries import ConfigSubentry
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .const import (
    CONF_DEVICE_ID,
    CONF_MODE,
    CONF_SHOW_IN_SIDEBAR,
    CONF_URL,
    CONF_VERIFY_SSL,
    DEVICE_LINK_PREFIX,
    DISCOVERED_PREFIX,
    DOMAIN,
    PROXY_URL_PREFIX,
    SESSION_TTL,
    SUBENTRY_TYPE_VIEW,
)
from .hub import main_device, pinned_unique_id

if TYPE_CHECKING:
    from .hub import LocalWebUiHub, View


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    for command in (
        ws_views,
        ws_session,
        ws_pin,
        ws_set_hidden,
        ws_set_device_link,
        ws_clear_site_data,
    ):
        websocket_api.async_register_command(hass, command)


def _hub(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg_id: int
) -> LocalWebUiHub | None:
    hub: LocalWebUiHub | None = hass.data.get(DOMAIN)
    if hub is None:
        connection.send_error(msg_id, "not_loaded", "Local Web UIs is not set up")
    return hub


def _view_json(hub: LocalWebUiHub, view: View) -> dict[str, Any]:
    device_link = None
    if view.device_id is not None:
        device = main_device(dr.async_get(hub.hass), view.device_id)
        device_link = {
            "enabled": hub.link_enabled(view.device_id),
            "override": hub.link_overrides.get(view.device_id),
            "active": bool(
                device and device.configuration_url == f"{DEVICE_LINK_PREFIX}{view.view_id}"
            ),
        }
    return {
        "view_id": view.view_id,
        "name": view.name,
        "subtitle": view.subtitle,
        "url": view.url,
        "mode": view.mode,
        "source": view.source,
        "device_id": view.device_id,
        "area": hub.area_name(view.device_id),
        "icon": view.icon,
        "show_in_sidebar": view.show_in_sidebar,
        "hidden": view.view_id in hub.hidden,
        "device_link": device_link,
    }


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/views"})
@callback
def ws_views(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """List static views and discovered device views (hidden ones flagged)."""
    if (hub := _hub(hass, connection, msg["id"])) is None:
        return
    views = list(hub.static_views.values()) + list(
        hub.discovered_views(include_hidden=True).values()
    )
    connection.send_result(
        msg["id"],
        {
            "views": [_view_json(hub, view) for view in views],
            "discovery": hub.discovery_enabled,
            "link_device_pages": hub.link_default,
            "linked_devices": hub.linked_devices_enabled,
            "entry_id": hub.entry.entry_id,
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/session",
        vol.Required("view_id"): str,
        # Extend this session instead of creating a new one, when still valid
        vol.Optional("token"): str,
    }
)
@callback
def ws_session(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Create or keep alive the session a view's iframe loads through."""
    if (hub := _hub(hass, connection, msg["id"])) is None:
        return
    if (view := hub.get_view(msg["view_id"])) is None:
        connection.send_error(msg["id"], "not_found", "This web UI no longer exists")
        return
    user_id = connection.user.id
    session = None
    if (token := msg.get("token")) is not None:
        existing = hub.sessions.get(token)
        if existing is not None and existing.user_id == user_id:
            session = hub.sessions.touch(token, view.view_id)
    if session is None:
        # Bound to this login: logging out ends the session
        session = hub.sessions.create(view.view_id, user_id, connection.refresh_token_id)
    connection.send_result(
        msg["id"],
        {
            "token": session.token,
            "url": f"{PROXY_URL_PREFIX}/{view.view_id}/{session.token}{view.entry}",
            "view": _view_json(hub, view),
            "expires_in": SESSION_TTL,
        },
    )


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/pin", vol.Required("view_id"): str}
)
@callback
def ws_pin(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Turn a discovered view into a configurable one."""
    if (hub := _hub(hass, connection, msg["id"])) is None:
        return
    view = hub.get_view(msg["view_id"])
    if view is None or view.source != "discovered":
        connection.send_error(msg["id"], "not_found", "No such discovered web UI")
        return
    assert view.device_id is not None  # Discovered views belong to a device
    subentry = ConfigSubentry(
        data=MappingProxyType(
            {
                CONF_URL: view.url,
                CONF_MODE: view.mode,
                CONF_VERIFY_SSL: view.verify_ssl,
                CONF_SHOW_IN_SIDEBAR: False,
                CONF_DEVICE_ID: view.device_id,
            }
        ),
        subentry_type=SUBENTRY_TYPE_VIEW,
        title=view.name,
        # One configured web UI per device, however often this is clicked
        unique_id=pinned_unique_id(view.device_id),
    )
    try:
        hass.config_entries.async_add_subentry(hub.entry, subentry)
    except AbortFlow:
        connection.send_error(msg["id"], "already_pinned", "This device already has a web UI")
        return
    # The update listener also does this, later; the reply must see the new view
    hub.async_update_config()
    connection.send_result(msg["id"], {"view_id": subentry.subentry_id})


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_hidden",
        vol.Required("view_id"): str,
        vol.Required("hidden"): bool,
    }
)
@callback
def ws_set_hidden(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Hide or unhide a discovered view."""
    if (hub := _hub(hass, connection, msg["id"])) is None:
        return
    if not msg["view_id"].startswith(DISCOVERED_PREFIX):
        connection.send_error(msg["id"], "not_found", "Only discovered web UIs can be hidden")
        return
    hub.async_set_hidden(msg["view_id"], msg["hidden"])
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {
        vol.Required("type"): f"{DOMAIN}/set_device_link",
        vol.Required("device_id"): str,
        # None: follow the global option again
        vol.Required("enabled"): vol.Any(bool, None),
    }
)
@callback
def ws_set_device_link(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Override the global "link device pages" option for one device."""
    if (hub := _hub(hass, connection, msg["id"])) is None:
        return
    hub.async_set_device_link(msg["device_id"], msg["enabled"])
    connection.send_result(msg["id"])


@websocket_api.require_admin
@websocket_api.websocket_command(
    {vol.Required("type"): f"{DOMAIN}/clear_site_data", vol.Required("view_id"): str}
)
@callback
def ws_clear_site_data(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """Forget the calling user's saved cookies and storage for a web UI."""
    if (hub := _hub(hass, connection, msg["id"])) is None:
        return
    hub.async_clear_site_data(connection.user.id, msg["view_id"])
    connection.send_result(msg["id"])
