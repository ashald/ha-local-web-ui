"""Diagnostics for Local Web UIs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from . import LocalWebUiConfigEntry
from .const import CONF_PASSWORD, CONF_URL, CONF_USERNAME

TO_REDACT = {CONF_PASSWORD, CONF_USERNAME}


def _redact_query(url: str | None) -> str | None:
    """Query strings of web UI URLs can hold tokens."""
    if not url or "?" not in url:
        return url
    return url.partition("?")[0] + "?" + REDACTED


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: LocalWebUiConfigEntry
) -> dict[str, Any]:
    hub = entry.runtime_data
    return {
        "options": dict(entry.options),
        "static_views": [
            async_redact_data(dict(s.data), TO_REDACT)
            | {"title": s.title, CONF_URL: _redact_query(s.data.get(CONF_URL))}
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
