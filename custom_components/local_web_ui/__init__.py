"""Local Web UIs: open the web UIs of local devices and sites inside Home Assistant.

Views come from the device registry (any device whose configuration_url is a
local http(s) URL, e.g. ESPHome with web_server) and from web UIs added by hand.
Home Assistant proxies them, so they work wherever its frontend works.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv
from homeassistant.helpers.typing import ConfigType

from .const import DOMAIN, NAME, PANEL_COMPONENT, PANEL_URL_PATH, STATIC_URL_PATH
from .hub import LocalWebUiHub, async_restore_device_links
from .proxy import async_register_proxy
from .websocket import async_register_commands

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PANEL_FILE = Path(__file__).parent / "www" / "local-web-ui-panel.js"
DATA_MODULE_URL = f"{DOMAIN}_module_url"
# Sidebar entries of single web UIs: url path -> (title, icon, view id)
DATA_SIDEBAR = f"{DOMAIN}_sidebar"

type LocalWebUiConfigEntry = ConfigEntry[LocalWebUiHub]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Once per Home Assistant run: routes and commands cannot be unregistered.

    They look the hub up on every call, so they keep working across reloads.
    """
    async_register_proxy(hass)
    async_register_commands(hass)
    # Versioned by content, so browsers (and HA's service worker) may cache it
    digest = await hass.async_add_executor_job(
        lambda: hashlib.sha256(PANEL_FILE.read_bytes()).hexdigest()[:12]
    )
    await hass.http.async_register_static_paths(
        [StaticPathConfig(STATIC_URL_PATH, str(PANEL_FILE.parent), True)]
    )
    hass.data[DATA_MODULE_URL] = f"{STATIC_URL_PATH}/{PANEL_FILE.name}?v={digest}"
    return True


async def async_setup_entry(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> bool:
    hub = LocalWebUiHub(hass, entry)
    # Also runs when setup fails halfway
    entry.async_on_unload(hub.async_unload)
    await hub.async_setup()
    entry.runtime_data = hub
    hass.data[DOMAIN] = hub

    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title=NAME,
        sidebar_icon="mdi:web-box",
        module_url=hass.data[DATA_MODULE_URL],
        require_admin=True,
        config={},
    )
    entry.async_on_unload(lambda: frontend.async_remove_panel(hass, PANEL_URL_PATH))
    entry.async_on_unload(lambda: _async_remove_sidebar_panels(hass))
    await _async_sync_sidebar_panels(hass, hub)

    # Options and web UI (subentry) changes are applied in place: open views keep
    # working, and a pinned view is there as soon as the command returns
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> None:
    hub = entry.runtime_data
    hub.async_update_config()
    await _async_sync_sidebar_panels(hass, hub)


async def _async_sync_sidebar_panels(hass: HomeAssistant, hub: LocalWebUiHub) -> None:
    """Give each web UI marked "show in sidebar" its own entry, and only those."""
    current: dict[str, tuple[str, str, str]] = hass.data.setdefault(DATA_SIDEBAR, {})
    wanted = {
        f"{PANEL_URL_PATH}-{view.view_id.lower()}": (
            view.name,
            view.icon or "mdi:web",
            view.view_id,
        )
        for view in hub.static_views.values()
        if view.show_in_sidebar
    }
    for path, panel in list(current.items()):
        if wanted.get(path) != panel:
            # panel_custom cannot update a panel: replace it
            frontend.async_remove_panel(hass, path, warn_if_unknown=False)
            del current[path]
    for path, (title, icon, view_id) in wanted.items():
        if path in current:
            continue
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=path,
            webcomponent_name=PANEL_COMPONENT,
            sidebar_title=title,
            sidebar_icon=icon,
            module_url=hass.data[DATA_MODULE_URL],
            require_admin=True,
            config={"view_id": view_id},
        )
        current[path] = (title, icon, view_id)


@callback
def _async_remove_sidebar_panels(hass: HomeAssistant) -> None:
    for path in hass.data.pop(DATA_SIDEBAR, {}):
        frontend.async_remove_panel(hass, path, warn_if_unknown=False)


async def async_unload_entry(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> bool:
    hass.data.pop(DOMAIN, None)
    if entry.disabled_by is not None:
        # The panel is gone, so device pages must not point at it
        entry.runtime_data.async_restore_device_links()
    return True


async def async_remove_entry(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> None:
    await async_restore_device_links(hass)
    await LocalWebUiHub.async_remove_storage(hass)
