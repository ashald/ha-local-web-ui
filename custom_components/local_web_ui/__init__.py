"""Local Web UIs: open the web UIs of local devices and sites inside Home Assistant.

Views come from the device registry (any device whose configuration_url is a
local http(s) URL, e.g. ESPHome with web_server) and from web UIs added by hand.
Home Assistant proxies them, so they work wherever its frontend works.
"""

from __future__ import annotations

from pathlib import Path

from homeassistant.components import frontend, panel_custom
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import config_validation as cv
from homeassistant.loader import async_get_integration

from .const import DOMAIN, NAME, PANEL_COMPONENT, PANEL_URL_PATH, STATIC_URL_PATH
from .hub import LocalWebUiHub, async_restore_device_links
from .proxy import async_register_proxy
from .websocket import async_register_commands

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

DATA_HTTP_REGISTERED = f"{DOMAIN}_http_registered"
DATA_PANELS = f"{DOMAIN}_panels"

type LocalWebUiConfigEntry = ConfigEntry[LocalWebUiHub]


async def async_setup_entry(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> bool:
    if not hass.data.get(DATA_HTTP_REGISTERED):
        # Routes, static paths and commands cannot be unregistered, so they are
        # set up once and look the hub up on every call
        async_register_proxy(hass)
        await hass.http.async_register_static_paths(
            [StaticPathConfig(STATIC_URL_PATH, str(Path(__file__).parent / "www"), False)]
        )
        async_register_commands(hass)
        hass.data[DATA_HTTP_REGISTERED] = True

    hub = LocalWebUiHub(hass, entry)
    await hub.async_setup()
    entry.runtime_data = hub
    hass.data[DOMAIN] = hub

    version = (await async_get_integration(hass, DOMAIN)).version
    module_url = f"{STATIC_URL_PATH}/local-web-ui-panel.js?v={version}"
    panels = [PANEL_URL_PATH]
    await panel_custom.async_register_panel(
        hass,
        frontend_url_path=PANEL_URL_PATH,
        webcomponent_name=PANEL_COMPONENT,
        sidebar_title=NAME,
        sidebar_icon="mdi:web-box",
        module_url=module_url,
        require_admin=True,
        config={},
    )
    for view in hub.static_views.values():
        if not view.show_in_sidebar:
            continue
        path = f"{PANEL_URL_PATH}-{view.view_id.lower()}"
        await panel_custom.async_register_panel(
            hass,
            frontend_url_path=path,
            webcomponent_name=PANEL_COMPONENT,
            sidebar_title=view.name,
            sidebar_icon=view.icon or "mdi:web",
            module_url=module_url,
            require_admin=True,
            config={"view_id": view.view_id},
        )
        panels.append(path)
    hass.data[DATA_PANELS] = panels

    # Options and web UI (subentry) changes both land here
    entry.async_on_unload(entry.add_update_listener(_async_reload))
    return True


async def _async_reload(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> None:
    await hass.config_entries.async_reload(entry.entry_id)


async def async_unload_entry(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> bool:
    for path in hass.data.pop(DATA_PANELS, []):
        frontend.async_remove_panel(hass, path, warn_if_unknown=False)
    await entry.runtime_data.async_unload()
    hass.data.pop(DOMAIN, None)
    if entry.disabled_by is not None:
        # The panel is gone, so device pages must not point at it
        await async_restore_device_links(hass)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> None:
    await async_restore_device_links(hass)
    await LocalWebUiHub.async_remove_storage(hass)
