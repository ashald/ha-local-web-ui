"""Diagnostics for Local Web UIs."""

from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import REDACTED, async_redact_data
from homeassistant.core import HomeAssistant

from . import LocalWebUiConfigEntry
from .const import CONF_KIND, CONF_PASSWORD, CONF_URL, CONF_USERNAME, KIND_HUB

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
    options = async_redact_data(dict(entry.options), TO_REDACT)
    if CONF_URL in options:
        options[CONF_URL] = _redact_query(options[CONF_URL])
    result: dict[str, Any] = {"data": dict(entry.data), "options": options}
    if entry.data.get(CONF_KIND) == KIND_HUB:
        result["web_uis"] = len(hub.views)
        result["device_page_links"] = sorted(hub.originals)
    elif (view := hub.get_view(entry.entry_id)) is not None:
        result["view"] = {
            "url": _redact_query(view.url),
            "source": view.source,
            "mode": view.mode,
            "device_page_link": hub.link_enabled(view.view_id) if view.device_id else None,
        }
    else:
        result["view"] = None  # Its device has no local web page right now
    return result
