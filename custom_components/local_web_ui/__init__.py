"""Local Web UIs: open the web UIs of local devices and sites inside Home Assistant.

A hub entry holds the global options. Each web UI is an entry of its own: a
device's web page, offered through discovery (Add or Ignore), or a URL added by
hand. Home Assistant proxies them, so they work wherever its frontend works.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import Any

from homeassistant.components import frontend
from homeassistant.components.http import StaticPathConfig
from homeassistant.config_entries import (
    SOURCE_IGNORE,
    SOURCE_IMPORT,
    ConfigEntry,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import config_validation as cv, device_registry as dr
from homeassistant.helpers.storage import Store
from homeassistant.helpers.typing import ConfigType

from .const import (
    CONF_DEVICE_ID,
    CONF_KIND,
    CONF_LINKED_DEVICES,
    CONF_PANEL_ICON,
    CONF_PANEL_TITLE,
    CONF_PREVIOUS_VIEW_ID,
    CONF_SHOW_PANEL,
    CONF_VISIT_LINK,
    DEFAULT_PANEL_ICON,
    DEFAULT_SHOW_PANEL,
    DISCOVERED_PREFIX,
    DOMAIN,
    KIND_HUB,
    NAME,
    PANEL_COMPONENT,
    PANEL_URL_PATH,
    STATIC_URL_PATH,
    STORAGE_KEY,
    STORAGE_VERSION,
    SUBENTRY_TYPE_VIEW,
    VISIT_DEVICE,
    VISIT_HERE,
)
from .hub import LocalWebUiHub, async_restore_device_links, device_unique_id
from .proxy import async_register_proxy
from .websocket import async_register_commands

_LOGGER = logging.getLogger(__name__)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)

PANEL_FILE = Path(__file__).parent / "www" / "local-web-ui-panel.js"
DATA_MODULE_URL = f"{DOMAIN}_module_url"
# Registered panels: url path -> what they were registered with
DATA_PANELS = f"{DOMAIN}_panels"

type LocalWebUiConfigEntry = ConfigEntry[LocalWebUiHub]


async def async_setup(hass: HomeAssistant, config: ConfigType) -> bool:
    """Once per Home Assistant run: routes and commands cannot be unregistered.

    They look the hub up on every call, so they keep working across reloads.
    """
    hub = LocalWebUiHub(hass)
    hub.on_views_changed = lambda: _async_sync_panels(hass)
    await hub.async_load()
    hass.data[DOMAIN] = hub
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
    hub: LocalWebUiHub = hass.data[DOMAIN]
    entry.runtime_data = hub
    await hub.async_add_entry(entry)
    _async_sync_panels(hass)
    # Option changes (and renames) are applied in place: open views keep working
    entry.async_on_unload(entry.add_update_listener(_async_entry_updated))
    return True


async def _async_entry_updated(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> None:
    entry.runtime_data.async_refresh()
    _async_sync_panels(hass)


async def async_unload_entry(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> bool:
    await entry.runtime_data.async_remove_entry(entry, gone=entry.disabled_by is not None)
    _async_sync_panels(hass)
    return True


async def async_remove_entry(hass: HomeAssistant, entry: LocalWebUiConfigEntry) -> None:
    hub: LocalWebUiHub = hass.data[DOMAIN]
    if entry.source == SOURCE_IGNORE:
        return
    last = not [
        e
        for e in hass.config_entries.async_entries(DOMAIN, include_ignore=False)
        if e.entry_id != entry.entry_id
    ]
    if last:
        # Give every device its link back and forget everything
        hub.sessions.end_view(entry.entry_id)
        await async_restore_device_links(hass)
        await hub.async_remove_storage()
    elif entry.data.get(CONF_KIND) != KIND_HUB:
        hub.async_view_removed(entry)


# ---- panels ---------------------------------------------------------------------


def _panel(hass: HomeAssistant, title: str, icon: str, config: dict[str, Any]) -> dict[str, Any]:
    return {
        "component_name": "custom",
        "sidebar_title": title,
        "sidebar_icon": icon,
        "require_admin": True,
        "config": {
            **config,
            "_panel_custom": {
                "name": PANEL_COMPONENT,
                "module_url": hass.data[DATA_MODULE_URL],
                "embed_iframe": False,
                "trust_external": False,
            },
        },
    }


@callback
def _async_sync_panels(hass: HomeAssistant) -> None:
    """The main panel while any entry is loaded, and the sidebar entries of web UIs.

    The main panel also serves the web UIs' own pages (device "Visit" links), so
    it stays registered even when hidden from the sidebar.
    """
    hub: LocalWebUiHub = hass.data[DOMAIN]
    wanted: dict[str, tuple[dict[str, Any], bool]] = {}
    if hub.active:
        options = hub.hub_entry.options if hub.hub_entry else {}
        wanted[PANEL_URL_PATH] = (
            _panel(
                hass,
                options.get(CONF_PANEL_TITLE) or NAME,
                options.get(CONF_PANEL_ICON) or DEFAULT_PANEL_ICON,
                {},
            ),
            hub.hub_entry is not None and options.get(CONF_SHOW_PANEL, DEFAULT_SHOW_PANEL),
        )
        for view in hub.views.values():
            if view.show_in_sidebar:
                wanted[f"{PANEL_URL_PATH}-{view.view_id.lower()}"] = (
                    _panel(hass, view.name, view.icon or "mdi:web", {"view_id": view.view_id}),
                    True,
                )
    current: dict[str, tuple[dict[str, Any], bool]] = hass.data.setdefault(DATA_PANELS, {})
    for path in [p for p in current if p not in wanted]:
        frontend.async_remove_panel(hass, path, warn_if_unknown=False)
        del current[path]
    for path, (panel, show) in wanted.items():
        if current.get(path) == (panel, show):
            continue
        frontend.async_register_built_in_panel(
            hass,
            frontend_url_path=path,
            show_in_sidebar=show,
            update=path in current,
            **panel,
        )
        current[path] = (panel, show)


# ---- migration --------------------------------------------------------------------


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """0.3: the one entry becomes the hub, and each web UI an entry of its own."""
    if entry.version > 2:
        return False
    if entry.version == 1:
        stored = await Store[dict[str, Any]](hass, STORAGE_VERSION, STORAGE_KEY).async_load() or {}
        overrides: dict[str, bool] = stored.get("link_overrides", {})
        migrated: set[str] = set()
        for subentry in list(entry.subentries.values()):
            if subentry.subentry_type != SUBENTRY_TYPE_VIEW:
                continue
            options = dict(subentry.data)
            device_id = options.pop(CONF_DEVICE_ID, None)
            if device_id is not None:
                migrated.add(device_id)
            if device_id in overrides:
                options[CONF_VISIT_LINK] = VISIT_HERE if overrides[device_id] else VISIT_DEVICE
            _async_start_flow(
                hass,
                {"source": SOURCE_IMPORT},
                {
                    "title": subentry.title,
                    CONF_DEVICE_ID: device_id,
                    CONF_PREVIOUS_VIEW_ID: subentry.subentry_id,
                    "options": options,
                },
            )
            hass.config_entries.async_remove_subentry(entry, subentry.subentry_id)
        # Hidden device web UIs become ignored discoveries
        registry = dr.async_get(hass)
        for view_id in stored.get("hidden", []):
            if view_id.startswith(DISCOVERED_PREFIX):
                device_id = view_id[len(DISCOVERED_PREFIX) :]
                if device_id in migrated:
                    continue  # It has a web UI of its own, which wins
                device = registry.async_get(device_id)
                title = getattr(device, "name_by_user", None) or getattr(device, "name", None)
                _async_start_flow(
                    hass,
                    {"source": SOURCE_IGNORE},
                    {"unique_id": device_unique_id(device_id), "title": title or device_id},
                )
        options = {k: v for k, v in entry.options.items() if k != CONF_LINKED_DEVICES}
        hass.config_entries.async_update_entry(
            entry,
            data={CONF_KIND: KIND_HUB},
            options=options,
            unique_id="hub",
            version=2,
        )
        _LOGGER.info("Local Web UIs: web UIs moved to entries of their own")
    return True


@callback
def _async_start_flow(hass: HomeAssistant, context: dict[str, Any], data: dict[str, Any]) -> None:
    hass.async_create_task(
        hass.config_entries.flow.async_init(DOMAIN, context=context, data=data),
        eager_start=False,
    )
