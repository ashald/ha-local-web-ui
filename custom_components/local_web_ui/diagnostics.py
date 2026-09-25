"""Diagnostics for Local Web UIs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.core import HomeAssistant

from . import LocalWebUiConfigEntry
from .const import CONF_PASSWORD, CONF_USERNAME

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LocalWebUiConfigEntry
) -> dict[str, Any]:
    hub = entry.runtime_data
    return {
        "options": dict(entry.options),
        "static_views": [
            async_redact_data(dict(s.data), TO_REDACT) | {"title": s.title}
            for s in entry.subentries.values()
        ],
        "discovered_views": [
            {"view_id": v.view_id, "name": v.name, "url": v.url, "device_id": v.device_id}
            for v in hub.discovered_views(include_hidden=True).values()
        ],
        "hidden": sorted(hub.hidden),
        "link_overrides": hub.link_overrides,
        "device_page_links": sorted(hub.originals),
    }
