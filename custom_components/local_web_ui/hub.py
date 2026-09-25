"""Views, sessions, per-user site state and device page links."""

from __future__ import annotations

import base64
from collections.abc import Callable
from dataclasses import dataclass
import logging
import secrets
import time
from typing import Any

import aiohttp
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_loaded_integration
from yarl import URL

from .const import (
    CONF_DEVICE_ID,
    CONF_DISCOVERY,
    CONF_ICON,
    CONF_LINK_DEVICE_PAGES,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SHOW_IN_SIDEBAR,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_DISCOVERY,
    DEFAULT_LINK_DEVICE_PAGES,
    DEVICE_LINK_PREFIX,
    DISCOVERED_PREFIX,
    DOMAIN,
    MODE_ISOLATED,
    SESSION_TTL,
    STORAGE_KEY,
    STORAGE_KEY_JAR,
    STORAGE_VERSION,
    SUBENTRY_TYPE_VIEW,
)
from .discovery import is_local_ui_url, parse_http_url

_LOGGER = logging.getLogger(__name__)

DATA_SESSIONS = f"{DOMAIN}_sessions"


@dataclass(frozen=True, slots=True)
class View:
    """A web UI that Home Assistant can open."""

    view_id: str
    name: str
    origin: URL  # scheme://host[:port]
    entry: str  # raw path and query of the page to open first
    mode: str
    verify_ssl: bool
    authorization: str | None  # Authorization header value for stored credentials
    source: str  # "static" or "discovered"
    device_id: str | None
    show_in_sidebar: bool
    icon: str | None
    subtitle: str

    @property
    def url(self) -> str:
        """Full URL of the entry page, for display."""
        return str(self.origin) + self.entry


def basic_authorization(username: str, password: str) -> str:
    """HTTP Basic credentials (RFC 7617, UTF-8)."""
    token = base64.b64encode(f"{username}:{password}".encode()).decode("ascii")
    return f"Basic {token}"


def view_from_url(url: URL) -> tuple[URL, str]:
    """Split an absolute URL into origin and raw entry path (with query)."""
    origin = url.origin()
    entry = url.raw_path or "/"
    if url.raw_query_string:
        entry += "?" + url.raw_query_string
    return origin, entry


@dataclass(slots=True)
class Session:
    """Lets one Home Assistant user's browser open one view."""

    token: str
    view_id: str
    user_id: str
    expires: float


class SessionManager:
    """Sessions outlive config entry reloads, so open views keep working."""

    def __init__(self) -> None:
        self._sessions: dict[str, Session] = {}

    def create(self, view_id: str, user_id: str) -> Session:
        self._expire()
        session = Session(
            secrets.token_urlsafe(32), view_id, user_id, time.monotonic() + SESSION_TTL
        )
        self._sessions[session.token] = session
        return session

    def touch(self, token: str, view_id: str) -> Session | None:
        """Return the live session for this view and extend it."""
        session = self._sessions.get(token)
        now = time.monotonic()
        if session is None or session.expires < now:
            self._sessions.pop(token, None)
            return None
        if not secrets.compare_digest(session.view_id, view_id):
            return None
        session.expires = now + SESSION_TTL
        return session

    def revoke_user(self, user_id: str) -> None:
        for token in [t for t, s in self._sessions.items() if s.user_id == user_id]:
            del self._sessions[token]

    def _expire(self) -> None:
        now = time.monotonic()
        for token in [t for t, s in self._sessions.items() if s.expires < now]:
            del self._sessions[token]


@callback
def async_get_sessions(hass: HomeAssistant) -> SessionManager:
    if (sessions := hass.data.get(DATA_SESSIONS)) is None:
        sessions = hass.data[DATA_SESSIONS] = SessionManager()
    return sessions


class LocalWebUiHub:
    """Runtime state of the config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.sessions = async_get_sessions(hass)
        self._store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
        self._jar_store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY_JAR)
        # device_id -> configuration_url the owning integration set, before we linked it
        self.originals: dict[str, str] = {}
        self.hidden: set[str] = set()
        # Devices whose page link behaviour is flipped relative to the global option
        self.link_exceptions: set[str] = set()
        self._cookies: dict[str, list[dict[str, str]]] = {}
        self._shim_storage: dict[str, dict[str, str]] = {}
        self._jars: dict[str, aiohttp.CookieJar] = {}
        self._http: dict[bool, aiohttp.ClientSession] = {}
        self._static: dict[str, View] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._own_hosts: frozenset[tuple[str, int]] = frozenset()

    # ---- lifecycle ---------------------------------------------------------

    @property
    def discovery_enabled(self) -> bool:
        return self.entry.options.get(CONF_DISCOVERY, DEFAULT_DISCOVERY)

    @property
    def link_default(self) -> bool:
        return self.entry.options.get(CONF_LINK_DEVICE_PAGES, DEFAULT_LINK_DEVICE_PAGES)

    async def async_setup(self) -> None:
        data = await self._store.async_load() or {}
        self.originals = dict(data.get("originals", {}))
        self.hidden = set(data.get("hidden", []))
        self.link_exceptions = set(data.get("link_exceptions", []))
        jar = await self._jar_store.async_load() or {}
        self._cookies = dict(jar.get("cookies", {}))
        self._shim_storage = dict(jar.get("storage", {}))
        self._own_hosts = self._compute_own_hosts()
        self._static = self._load_static_views()
        self._unsubs.append(
            self.hass.bus.async_listen(
                dr.EVENT_DEVICE_REGISTRY_UPDATED, self._async_device_registry_updated
            )
        )
        self.async_sync_device_links()

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for client in self._http.values():
            await client.close()
        self._http.clear()
        await self._async_save_now()

    def _compute_own_hosts(self) -> frozenset[tuple[str, int]]:
        hosts: set[tuple[str, int]] = set()
        for kwargs in ({"prefer_external": False}, {"prefer_external": True}):
            try:
                url = URL(get_url(self.hass, allow_cloud=False, **kwargs))
            except NoURLAvailableError:
                continue
            if url.host:
                hosts.add((url.host.lower(), url.port or 0))
        return frozenset(hosts)

    # ---- views -------------------------------------------------------------

    def _load_static_views(self) -> dict[str, View]:
        views: dict[str, View] = {}
        for subentry in self.entry.subentries.values():
            if subentry.subentry_type != SUBENTRY_TYPE_VIEW:
                continue
            data = subentry.data
            url = parse_http_url(data.get(CONF_URL))
            if url is None:
                _LOGGER.warning("Ignoring web UI %s with invalid URL", subentry.title)
                continue
            origin, entry_path = view_from_url(url)
            authorization = None
            if data.get(CONF_USERNAME):
                authorization = basic_authorization(
                    data[CONF_USERNAME], data.get(CONF_PASSWORD) or ""
                )
            views[subentry.subentry_id] = View(
                view_id=subentry.subentry_id,
                name=subentry.title,
                origin=origin,
                entry=entry_path,
                mode=data.get(CONF_MODE, MODE_ISOLATED),
                verify_ssl=data.get(CONF_VERIFY_SSL, True),
                authorization=authorization,
                source="static",
                device_id=data.get(CONF_DEVICE_ID),
                show_in_sidebar=data.get(CONF_SHOW_IN_SIDEBAR, False),
                icon=data.get(CONF_ICON),
                subtitle=url.host or "",
            )
        return views

    @property
    def static_views(self) -> dict[str, View]:
        return self._static

    def device_ui_url(self, device: dr.DeviceEntry) -> URL | None:
        """The device's own web UI URL, looking through our link if we set one."""
        current = device.configuration_url
        if current and current.startswith(DEVICE_LINK_PREFIX):
            current = self.originals.get(device.id)
        url = parse_http_url(current)
        if url is None or not is_local_ui_url(url, self._own_hosts):
            return None
        return url

    def _discovered_view(self, device: dr.DeviceEntry, url: URL) -> View:
        origin, entry_path = view_from_url(url)
        subtitle = url.host or ""
        if device.primary_config_entry and (
            config_entry := self.hass.config_entries.async_get_entry(device.primary_config_entry)
        ):
            try:
                integration = async_get_loaded_integration(self.hass, config_entry.domain)
                subtitle = f"{integration.name} · {subtitle}"
            except Exception:  # noqa: BLE001 - integration not loaded
                pass
        return View(
            view_id=f"{DISCOVERED_PREFIX}{device.id}",
            name=device.name_by_user or device.name or subtitle,
            origin=origin,
            entry=entry_path,
            mode=MODE_ISOLATED,
            verify_ssl=False,  # Device UIs on the LAN rarely have trusted certificates
            authorization=None,
            source="discovered",
            device_id=device.id,
            show_in_sidebar=False,
            icon=None,
            subtitle=subtitle,
        )

    def discovered_views(self, include_hidden: bool = False) -> dict[str, View]:
        if not self.discovery_enabled:
            return {}
        pinned = {v.device_id for v in self._static.values() if v.device_id}
        views: dict[str, View] = {}
        for device in iter_devices(dr.async_get(self.hass)):
            if device.disabled or device.id in pinned:
                continue
            if (url := self.device_ui_url(device)) is None:
                continue
            view = self._discovered_view(device, url)
            if include_hidden or view.view_id not in self.hidden:
                views[view.view_id] = view
        return views

    def get_view(self, view_id: str) -> View | None:
        if (view := self._static.get(view_id)) is not None:
            return view
        if not view_id.startswith(DISCOVERED_PREFIX) or not self.discovery_enabled:
            return None
        device = dr.async_get(self.hass).async_get(view_id[len(DISCOVERED_PREFIX) :])
        if device is None or device.disabled:
            return None
        if any(v.device_id == device.id for v in self._static.values()):
            return None
        url = self.device_ui_url(device)
        return None if url is None else self._discovered_view(device, url)

    def view_for_device(self, device_id: str) -> View | None:
        for view in self._static.values():
            if view.device_id == device_id:
                return view
        return self.get_view(f"{DISCOVERED_PREFIX}{device_id}")

    def area_name(self, device_id: str | None) -> str | None:
        if device_id is None:
            return None
        device = dr.async_get(self.hass).async_get(device_id)
        if device is None or device.area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(device.area_id)
        return area.name if area else None

    # ---- device page links ---------------------------------------------------

    def link_enabled(self, device_id: str) -> bool:
        return self.link_default != (device_id in self.link_exceptions)

    @callback
    def async_sync_device_links(self) -> None:
        for device in iter_devices(dr.async_get(self.hass)):
            self._async_sync_device_link(device)

    @callback
    def _async_sync_device_link(self, device: dr.DeviceEntry) -> None:
        """Point the device's "Visit" link at its view, or give it back."""
        registry = dr.async_get(self.hass)
        current = device.configuration_url
        ours = bool(current and current.startswith(DEVICE_LINK_PREFIX))
        if current and not ours:
            # The owning integration set (or reset) its own URL: that is the original now
            if self.originals.get(device.id) != current:
                self.originals[device.id] = current
                self._async_schedule_save()
        elif not current and device.id in self.originals:
            # The integration removed its URL; nothing left to link to
            del self.originals[device.id]
            self._async_schedule_save()
            return

        view = self.view_for_device(device.id)
        if view is not None and view.view_id in self.hidden:
            view = None  # Hiding a device's UI also stops sending its page here
        if view is not None and self.link_enabled(device.id):
            target = f"{DEVICE_LINK_PREFIX}{view.view_id}"
            if current != target:
                registry.async_update_device(device.id, configuration_url=target)
        elif ours:
            registry.async_update_device(device.id, configuration_url=self.originals.get(device.id))

    @callback
    def _async_device_registry_updated(
        self, event: Event[dr.EventDeviceRegistryUpdatedData]
    ) -> None:
        if event.data["action"] == "remove":
            if self.originals.pop(event.data["device_id"], None) is not None:
                self._async_schedule_save()
            return
        if event.data["action"] == "update" and not (
            {"configuration_url", "disabled_by", "name", "name_by_user"}
            & set(event.data.get("changes", {}))
        ):
            return
        if device := dr.async_get(self.hass).async_get(event.data["device_id"]):
            self._async_sync_device_link(device)

    @callback
    def async_set_hidden(self, view_id: str, hidden: bool) -> None:
        (self.hidden.add if hidden else self.hidden.discard)(view_id)
        self._async_schedule_save()
        if view_id.startswith(DISCOVERED_PREFIX) and (
            device := dr.async_get(self.hass).async_get(view_id[len(DISCOVERED_PREFIX) :])
        ):
            self._async_sync_device_link(device)

    @callback
    def async_set_device_link(self, device_id: str, enabled: bool) -> None:
        if enabled == self.link_default:
            self.link_exceptions.discard(device_id)
        else:
            self.link_exceptions.add(device_id)
        self._async_schedule_save()
        if device := dr.async_get(self.hass).async_get(device_id):
            self._async_sync_device_link(device)

    # ---- per-user site state -------------------------------------------------

    @staticmethod
    def _state_key(user_id: str, view_id: str) -> str:
        return f"{user_id}|{view_id}"

    def cookie_jar(self, user_id: str, view: View) -> aiohttp.CookieJar:
        """Cookies the site set for this user in isolated mode, kept server side."""
        key = self._state_key(user_id, view.view_id)
        if (jar := self._jars.get(key)) is None:
            # unsafe=True: device UIs are usually addressed by IP
            jar = self._jars[key] = aiohttp.CookieJar(unsafe=True, quote_cookie=False)
            for saved in self._cookies.get(key, []):
                jar.update_cookies(_parse_cookie(saved["cookie"]), URL(saved["url"]))
        return jar

    @callback
    def async_cookies_changed(self, user_id: str, view: View) -> None:
        key = self._state_key(user_id, view.view_id)
        jar = self._jars[key]
        self._cookies[key] = [
            {"cookie": morsel.OutputString(), "url": str(view.origin)} for morsel in jar
        ]
        self._async_schedule_save()

    def shim_storage(self, user_id: str, view_id: str) -> dict[str, str]:
        return self._shim_storage.get(self._state_key(user_id, view_id), {})

    @callback
    def async_set_shim_storage(self, user_id: str, view_id: str, data: dict[str, str]) -> None:
        self._shim_storage[self._state_key(user_id, view_id)] = data
        self._async_schedule_save()

    @callback
    def async_clear_site_data(self, user_id: str, view_id: str) -> None:
        """Forget the cookies and stored data a site keeps for this user (log out)."""
        key = self._state_key(user_id, view_id)
        self._jars.pop(key, None)
        self._cookies.pop(key, None)
        self._shim_storage.pop(key, None)
        self._async_schedule_save()

    # ---- upstream HTTP -------------------------------------------------------

    def http_client(self, verify_ssl: bool) -> aiohttp.ClientSession:
        if (client := self._http.get(verify_ssl)) is None:
            # HA's shared connector resolves .local names over mDNS
            client = self._http[verify_ssl] = async_create_clientsession(
                self.hass,
                verify_ssl=verify_ssl,
                auto_cleanup=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        return client

    # ---- persistence ---------------------------------------------------------

    def _data(self) -> dict[str, Any]:
        return {
            "originals": self.originals,
            "hidden": sorted(self.hidden),
            "link_exceptions": sorted(self.link_exceptions),
        }

    def _jar_data(self) -> dict[str, Any]:
        return {"cookies": self._cookies, "storage": self._shim_storage}

    @callback
    def _async_schedule_save(self) -> None:
        self._store.async_delay_save(self._data, 1)
        self._jar_store.async_delay_save(self._jar_data, 10)

    async def _async_save_now(self) -> None:
        await self._store.async_save(self._data())
        await self._jar_store.async_save(self._jar_data())

    @staticmethod
    async def async_remove_storage(hass: HomeAssistant) -> None:
        """Forget originals, cookies and stored site data (integration removed)."""
        await Store(hass, STORAGE_VERSION, STORAGE_KEY).async_remove()
        await Store(hass, STORAGE_VERSION, STORAGE_KEY_JAR).async_remove()


def iter_devices(registry: dr.DeviceRegistry) -> list[dr.DeviceEntry]:
    """All device entries, on current and older Home Assistant releases.

    Since 2026.x iterating registry.devices yields entries (using it as a mapping
    is deprecated); before, it was a mapping whose iteration yields device ids.
    """
    devices: list[dr.DeviceEntry] = []
    for item in registry.devices:
        if isinstance(item, str):
            if (device := registry.async_get(item)) is not None:
                devices.append(device)
        else:
            devices.append(item)
    return devices


def _parse_cookie(set_cookie: str) -> Any:
    from http.cookies import SimpleCookie  # noqa: PLC0415

    cookie: SimpleCookie = SimpleCookie()
    try:
        cookie.load(set_cookie)
    except Exception:  # noqa: BLE001 - malformed cookie from a device
        return SimpleCookie()
    return cookie


async def async_restore_device_links(hass: HomeAssistant) -> None:
    """Give every device we linked its original URL back (on removal/disable)."""
    store: Store[dict[str, Any]] = Store(hass, STORAGE_VERSION, STORAGE_KEY)
    originals = (await store.async_load() or {}).get("originals", {})
    registry = dr.async_get(hass)
    for device in iter_devices(registry):
        if device.configuration_url and device.configuration_url.startswith(DEVICE_LINK_PREFIX):
            registry.async_update_device(device.id, configuration_url=originals.get(device.id))
