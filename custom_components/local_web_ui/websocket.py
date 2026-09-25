"""Websocket API for the panel. Admin only."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components import websocket_api
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import device_registry as dr
import voluptuous as vol

from .const import DEVICE_LINK_PREFIX, DOMAIN, PROXY_URL_PREFIX, SESSION_TTL
from .hub import main_device

if TYPE_CHECKING:
    from .hub import LocalWebUiHub, View


@callback
def async_register_commands(hass: HomeAssistant) -> None:
    for command in (ws_views, ws_session, ws_clear_site_data):
        websocket_api.async_register_command(hass, command)


def _hub(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg_id: int
) -> LocalWebUiHub | None:
    hub: LocalWebUiHub | None = hass.data.get(DOMAIN)
    if hub is None or not hub.active:
        connection.send_error(msg_id, "not_loaded", "Local Web UIs is not set up")
        return None
    return hub


def _view_json(hub: LocalWebUiHub, view: View) -> dict[str, Any]:
    device_link = None
    if view.device_id is not None:
        device = main_device(dr.async_get(hub.hass), view.device_id)
        device_link = {
            "enabled": hub.link_enabled(view.view_id),
            "choice": hub.visit_link(view.view_id),
            "active": bool(
                device and device.configuration_url == f"{DEVICE_LINK_PREFIX}{view.view_id}"
            ),
        }
    return {
        "view_id": view.view_id,
        "entry_id": view.view_id,
        "name": view.name,
        "subtitle": view.subtitle,
        "url": view.url,
        "mode": view.mode,
        "source": view.source,
        "device_id": view.device_id,
        "area": hub.area_name(view.device_id),
        "icon": view.icon,
        "show_in_sidebar": view.show_in_sidebar,
        "device_link": device_link,
        "linked_device_id": hub.linked_device_id(view.view_id),
    }


@websocket_api.require_admin
@websocket_api.websocket_command({vol.Required("type"): f"{DOMAIN}/views"})
@callback
def ws_views(
    hass: HomeAssistant, connection: websocket_api.ActiveConnection, msg: dict[str, Any]
) -> None:
    """List the web UIs, and how many discovered ones wait to be added."""
    if (hub := _hub(hass, connection, msg["id"])) is None:
        return
    discovered = hass.config_entries.flow.async_progress_by_handler(
        DOMAIN, match_context={"source": SOURCE_INTEGRATION_DISCOVERY}
    )
    connection.send_result(
        msg["id"],
        {
            "views": [_view_json(hub, view) for view in hub.views.values()],
            "discovered": len(discovered),
            "discovery": hub.discovery_enabled,
            "link_device_pages": hub.link_default,
            "hub_entry_id": hub.hub_entry.entry_id if hub.hub_entry else None,
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
