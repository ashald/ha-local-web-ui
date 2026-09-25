"""Views, sessions, per-user site state and device page links."""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
from functools import partial
import logging
import secrets
import time
from typing import Any

import aiohttp
from homeassistant.auth import EVENT_USER_REMOVED
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import aiohttp_client, area_registry as ar, device_registry as dr
from homeassistant.helpers.aiohttp_client import async_create_clientsession
from homeassistant.helpers.event import async_track_time_interval
from homeassistant.helpers.network import NoURLAvailableError, get_url
from homeassistant.helpers.storage import Store
from homeassistant.loader import async_get_loaded_integration
from yarl import URL

from .const import (
    CONF_DEVICE_ID,
    CONF_DISCOVERY,
    CONF_ICON,
    CONF_LINK_DEVICE_PAGES,
    CONF_LINKED_DEVICES,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SHOW_IN_SIDEBAR,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DEFAULT_DISCOVERY,
    DEFAULT_LINK_DEVICE_PAGES,
    DEFAULT_LINKED_DEVICES,
    DEVICE_LINK_PREFIX,
    DISCOVERED_PREFIX,
    DOMAIN,
    MAX_COOKIE_BYTES,
    MAX_COOKIES_PER_SITE,
    MAX_REQUESTS_PER_SITE,
    MAX_SHIM_STORAGE_BYTES,
    MAX_SHIM_STORAGE_KEYS,
    MAX_WEBSOCKETS_PER_SESSION,
    MODE_ISOLATED,
    MODE_TRUSTED,
    NAME,
    SESSION_MAX_AGE,
    SESSION_TTL,
    STORAGE_KEY,
    STORAGE_KEY_JAR,
    STORAGE_VERSION,
    STORAGE_WRITE_WAIT,
    SUBENTRY_TYPE_VIEW,
)
from .discovery import LanOnlyResolver, is_local_ui_url, parse_http_url

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


def pinned_unique_id(device_id: str) -> str:
    """Unique id of the web UI subentry configured for a device."""
    return f"device:{device_id}"


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
    refresh_token_id: str | None
    created: float
    expires: float
    websockets: set[Callable[[], Coroutine[Any, Any, Any]]] = field(default_factory=set)
    # Other long-lived responses (server-sent events, downloads, camera streams)
    streams: set[Callable[[], Coroutine[Any, Any, Any]]] = field(default_factory=set)


class SessionManager:
    """Sessions outlive config entry reloads, so open views keep working.

    A session ends after SESSION_TTL without use, SESSION_MAX_AGE after creation,
    or as soon as the Home Assistant login (refresh token) that created it is
    revoked, whichever comes first. Its WebSockets are closed with it.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self._hass = hass
        self._sessions: dict[str, Session] = {}
        self._revoke_unsubs: dict[str, CALLBACK_TYPE] = {}

    def create(self, view_id: str, user_id: str, refresh_token_id: str | None) -> Session:
        self.expire()
        now = time.monotonic()
        session = Session(
            secrets.token_urlsafe(32),
            view_id,
            user_id,
            refresh_token_id,
            now,
            now + SESSION_TTL,
        )
        self._sessions[session.token] = session
        if refresh_token_id is not None and refresh_token_id not in self._revoke_unsubs:
            self._revoke_unsubs[refresh_token_id] = (
                self._hass.auth.async_register_revoke_token_callback(
                    refresh_token_id, partial(self._revoked, refresh_token_id)
                )
            )
        return session

    def get(self, token: str) -> Session | None:
        return self._sessions.get(token)

    def touch(self, token: str, view_id: str) -> Session | None:
        """Return the live session for this view and extend it."""
        session = self._sessions.get(token)
        now = time.monotonic()
        if session is None:
            return None
        if session.expires < now or now - session.created > SESSION_MAX_AGE:
            self._end(session)
            return None
        if not secrets.compare_digest(session.view_id, view_id):
            return None
        session.expires = min(now + SESSION_TTL, session.created + SESSION_MAX_AGE)
        return session

    def end_all(self) -> None:
        """End every session, closing its WebSockets (integration disabled or removed)."""
        for session in list(self._sessions.values()):
            self._end(session)

    def revoke_user(self, user_id: str) -> None:
        for session in [s for s in self._sessions.values() if s.user_id == user_id]:
            self._end(session)

    @callback
    def _revoked(self, refresh_token_id: str) -> None:
        self._revoke_unsubs.pop(refresh_token_id, None)
        for session in [
            s for s in self._sessions.values() if s.refresh_token_id == refresh_token_id
        ]:
            self._end(session)

    def expire(self) -> None:
        now = time.monotonic()
        for session in [
            s
            for s in self._sessions.values()
            if s.expires < now or now - s.created > SESSION_MAX_AGE
        ]:
            self._end(session)

    def _end(self, session: Session) -> None:
        self._sessions.pop(session.token, None)
        for close in [*session.websockets, *session.streams]:
            self._hass.async_create_task(close())
        session.websockets.clear()
        session.streams.clear()

    @callback
    def reserve_websocket(self, token: str) -> SessionSlot | None:
        """Claim one of the session's WebSockets before connecting; None if all taken.

        The claim is made before any await, so sockets opened at the same time
        cannot get past the limit. Release it with slot.release().
        """
        session = self._sessions.get(token)
        if session is None or len(session.websockets) >= MAX_WEBSOCKETS_PER_SESSION:
            return None
        slot = SessionSlot(session.websockets)
        session.websockets.add(slot.close)
        return slot

    @callback
    def track_stream(self, token: str) -> SessionSlot:
        """Close a streamed response when its session ends."""
        if (session := self._sessions.get(token)) is None:
            slot = SessionSlot(set())
            slot.ended = True
            return slot
        slot = SessionSlot(session.streams)
        session.streams.add(slot.close)
        return slot


class SessionSlot:
    """A WebSocket or stream of a session: closed when the session ends."""

    def __init__(self, owner: set[Callable[[], Coroutine[Any, Any, Any]]]) -> None:
        self._owner = owner
        self._closables: list[Any] = []
        self.ended = False

    def add(self, closable: Any) -> None:
        """Anything with an async close()."""
        self._closables.append(closable)

    async def close(self) -> None:
        self.ended = True
        for closable in self._closables:
            await closable.close()

    def release(self) -> None:
        self._owner.discard(self.close)


@callback
def async_get_sessions(hass: HomeAssistant) -> SessionManager:
    if (sessions := hass.data.get(DATA_SESSIONS)) is None:
        sessions = hass.data[DATA_SESSIONS] = SessionManager(hass)
    return sessions


@dataclass(slots=True)
class _DiscoveredIndex:
    """Discovered views, one per URL, and which view each device belongs to."""

    views: dict[str, View] = field(default_factory=dict)
    by_device: dict[str, str] = field(default_factory=dict)


class LocalWebUiHub:
    """Runtime state of the config entry."""

    def __init__(self, hass: HomeAssistant, entry: ConfigEntry) -> None:
        self.hass = hass
        self.entry = entry
        self.sessions = async_get_sessions(hass)
        # Private: they hold device session cookies and tokens sites keep in storage
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._jar_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY_JAR, private=True, atomic_writes=True
        )
        # device_id -> configuration_url the owning integration set, while we link it
        self.originals: dict[str, str] = {}
        self.hidden: set[str] = set()
        # Per-device choice for the device page link; absent means the global option
        self.link_overrides: dict[str, bool] = {}
        self._cookies: dict[str, list[dict[str, str]]] = {}
        self._shim_storage: dict[str, dict[str, str]] = {}
        self._jars: dict[str, aiohttp.CookieJar] = {}
        # Storage writes applied recently, so the next page load can wait for one
        self._applied_writes: dict[str, deque[str]] = {}
        self._write_waiters: dict[tuple[str, str], asyncio.Event] = {}
        # (state key, page id) -> sequence number of the page's last applied write
        self._page_writes: OrderedDict[tuple[str, str], int] = OrderedDict()
        self._http: dict[str | bool, aiohttp.ClientSession] = {}
        self._limiters: dict[str, asyncio.Semaphore] = {}
        self._static: dict[str, View] = {}
        self._index: _DiscoveredIndex | None = None
        # device_id -> (what the view was computed from, the view)
        self._device_views: dict[str, tuple[tuple[Any, ...], View | None]] = {}
        self._unsubs: list[Callable[[], None]] = []
        self._own_hosts: frozenset[tuple[str, int]] = frozenset()
        self._loaded = False  # Never save over storage that was not loaded
        # Our own registry writes fire registry events too; those need no handling
        self._writing = 0

    # ---- lifecycle ---------------------------------------------------------

    @property
    def discovery_enabled(self) -> bool:
        return self.entry.options.get(CONF_DISCOVERY, DEFAULT_DISCOVERY)

    @property
    def link_default(self) -> bool:
        return self.entry.options.get(CONF_LINK_DEVICE_PAGES, DEFAULT_LINK_DEVICE_PAGES)

    @property
    def linked_devices_enabled(self) -> bool:
        return self.entry.options.get(CONF_LINKED_DEVICES, DEFAULT_LINKED_DEVICES)

    async def async_setup(self) -> None:
        data = await self._store.async_load() or {}
        self.originals = dict(data.get("originals", {}))
        self.hidden = set(data.get("hidden", []))
        self.link_overrides = {str(k): bool(v) for k, v in data.get("link_overrides", {}).items()}
        jar = await self._jar_store.async_load() or {}
        self._cookies = dict(jar.get("cookies", {}))
        self._shim_storage = dict(jar.get("storage", {}))
        self._loaded = True
        # Users removed while the integration was not running
        users = {user.id for user in await self.hass.auth.async_get_users()}
        for user_id in {key.partition("|")[0] for key in (*self._cookies, *self._shim_storage)}:
            if user_id not in users:
                self._async_forget_user(user_id)
        self._own_hosts = self._compute_own_hosts()
        self._unsubs.append(
            self.hass.bus.async_listen(
                dr.EVENT_DEVICE_REGISTRY_UPDATED, self._async_device_registry_updated
            )
        )
        self._unsubs.append(
            self.hass.bus.async_listen(EVENT_USER_REMOVED, self._async_user_removed)
        )
        self._unsubs.append(
            async_track_time_interval(
                # A callback: it runs in the event loop, like everything that uses sessions
                self.hass,
                callback(lambda _now: self.sessions.expire()),
                timedelta(seconds=30),
            )
        )
        self.async_update_config()

    @callback
    def async_update_config(self) -> None:
        """Apply the entry's current options and web UIs (subentries) in place."""
        previous = self._static
        self._static = self._load_static_views()
        self._index = None
        for view_id, old in previous.items():
            # A web UI moved to another site must not hand it the old site's data
            if (view := self._static.get(view_id)) is None:
                self._async_forget_view(view_id)
            elif view.origin != old.origin:
                self._async_forget_site_data(view_id)
        self.async_sync_device_links()
        self.async_sync_linked_devices()
        if trusted := [v.name for v in self._static.values() if v.mode == MODE_TRUSTED]:
            _LOGGER.warning(
                "These web UIs run in trusted mode and can act as the logged-in admin "
                "in Home Assistant: %s",
                ", ".join(sorted(trusted)),
            )

    @callback
    def async_restore_device_links(self) -> None:
        """Give every device we linked its original URL back (entry disabled)."""
        # Stop following the registry first, or the restored links get linked again
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        registry = dr.async_get(self.hass)
        for device_id, original in list(self.originals.items()):
            device = main_device(registry, device_id)
            if device and (device.configuration_url or "").startswith(DEVICE_LINK_PREFIX):
                self._write(registry.async_update_device, device_id, configuration_url=original)
        self.originals.clear()
        self._async_schedule_save()

    @callback
    def _write(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Change the device registry without handling the events it fires."""
        self._writing += 1
        try:
            return method(*args, **kwargs)
        finally:
            self._writing -= 1

    async def async_unload(self) -> None:
        for unsub in self._unsubs:
            unsub()
        self._unsubs.clear()
        for key, client in self._http.items():
            if key == "discovered":
                await client.close()  # Our own connector
            else:
                client.detach()  # Shares Home Assistant's connector; only let go of it
        self._http.clear()
        for event in self._write_waiters.values():
            event.set()
        if self._loaded:
            await self._async_save_now()
            # A request still in flight must not write storage back after this
            # (the integration may have been removed)
            self._loaded = False

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
        if device.config_entry_id and (
            config_entry := self.hass.config_entries.async_get_entry(device.config_entry_id)
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

    def _discovered_index(self) -> _DiscoveredIndex:
        """Built from the device registry on demand; dropped when it changes."""
        if self._index is not None:
            return self._index
        index = _DiscoveredIndex()
        if self.discovery_enabled:
            pinned = {v.device_id for v in self._static.values() if v.device_id}
            # One view per URL: since 2026.8 a device known to several integrations
            # is one device per integration, each with the same link
            by_url = {v.url: v.view_id for v in self._static.values()}
            for device in iter_devices(dr.async_get(self.hass)):
                if device.disabled or device.id in pinned:
                    continue
                if (view := self._device_view(device)) is None:
                    continue
                if (kept := by_url.get(view.url)) is not None:
                    index.by_device[device.id] = kept
                    continue
                index.views[view.view_id] = view
                index.by_device[device.id] = by_url[view.url] = view.view_id
        self._index = index
        return index

    def _device_view(self, device: dr.DeviceEntry) -> View | None:
        """The device's discovered view; remembered, as parsing URLs adds up."""
        current = device.configuration_url
        key = (
            current,
            self.originals.get(device.id)
            if current and current.startswith(DEVICE_LINK_PREFIX)
            else None,
            device.name,
            device.name_by_user,
            device.config_entry_id,
        )
        if (cached := self._device_views.get(device.id)) is not None and cached[0] == key:
            return cached[1]
        url = self.device_ui_url(device)
        view = None if url is None else self._discovered_view(device, url)
        self._device_views[device.id] = (key, view)
        return view

    def discovered_views(self, include_hidden: bool = False) -> dict[str, View]:
        return {
            view_id: view
            for view_id, view in self._discovered_index().views.items()
            if include_hidden or view_id not in self.hidden
        }

    def get_view(self, view_id: str) -> View | None:
        if (view := self._static.get(view_id)) is not None:
            return view
        return self._discovered_index().views.get(view_id)

    def view_for_device(self, device_id: str) -> View | None:
        for view in self._static.values():
            if view.device_id == device_id:
                return view
        if (view_id := self._discovered_index().by_device.get(device_id)) is None:
            return None
        return self.get_view(view_id)

    def area_name(self, device_id: str | None) -> str | None:
        if device_id is None:
            return None
        device = main_device(dr.async_get(self.hass), device_id)
        if device is None or device.area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(device.area_id)
        return area.name if area else None

    # ---- device page links ---------------------------------------------------

    def link_enabled(self, device_id: str) -> bool:
        return self.link_overrides.get(device_id, self.link_default)

    @callback
    def async_sync_device_links(self) -> None:
        for device in iter_devices(dr.async_get(self.hass)):
            self._async_sync_device_link(device)

    @callback
    def _async_sync_device_link(self, device: dr.DeviceEntry) -> None:
        """Point the device's "Visit" link at its view, or give it back."""
        if device.config_entry_id == self.entry.entry_id:
            return  # Our own linked devices always point here
        registry = dr.async_get(self.hass)
        current = device.configuration_url
        ours = bool(current and current.startswith(DEVICE_LINK_PREFIX))
        view = self.view_for_device(device.id)
        link = (
            view is not None
            # Hiding a device's UI also stops sending its page here
            and view.view_id not in self.hidden
            and self.link_enabled(device.id)
        )
        if not current:
            # The integration removed its URL; nothing left to link to
            if self.originals.pop(device.id, None) is not None:
                self._async_schedule_save()
            return
        if not ours:
            # The owning integration set (or reset) its own URL: that is the original
            if not link:
                if self.originals.pop(device.id, None) is not None:
                    self._async_schedule_save()
                return
            if self.originals.get(device.id) != current:
                self.originals[device.id] = current
                self._async_schedule_save()
        if link:
            assert view is not None
            target = f"{DEVICE_LINK_PREFIX}{view.view_id}"
            if current != target:
                self._write(registry.async_update_device, device.id, configuration_url=target)
        else:
            original = self.originals.pop(device.id, None)
            self._async_schedule_save()
            self._write(registry.async_update_device, device.id, configuration_url=original)

    @callback
    def _async_device_registry_updated(
        self, event: Event[dr.EventDeviceRegistryUpdatedData]
    ) -> None:
        if self._writing:
            return
        device_id = event.data["device_id"]
        action = event.data["action"]
        if action == "update" and not (
            {"configuration_url", "disabled_by", "name", "name_by_user", "connections"}
            & set(event.data.get("changes", {}))
        ):
            return
        registry = dr.async_get(self.hass)
        device = None if action == "remove" else main_device(registry, device_id)
        if device is not None and device.config_entry_id == self.entry.entry_id:
            return  # One of our linked devices
        if device is None:
            self._device_views.pop(device_id, None)
        if device is None and (
            device_id in self.originals
            or device_id in self.link_overrides
            or f"{DISCOVERED_PREFIX}{device_id}" in self.hidden
            or any(k.endswith(f"|{DISCOVERED_PREFIX}{device_id}") for k in self._shim_storage)
            or any(k.endswith(f"|{DISCOVERED_PREFIX}{device_id}") for k in self._cookies)
        ):
            # Removed, or turned into a child device (2026.9), which has no link of
            # its own: forget what was kept for it
            self.originals.pop(device_id, None)
            self.link_overrides.pop(device_id, None)
            self._async_forget_view(f"{DISCOVERED_PREFIX}{device_id}")
        old = self._index
        if (
            old is not None
            and device_id not in old.by_device
            and (device is None or device.disabled or self.device_ui_url(device) is None)
            and not any(view.device_id == device_id for view in self._static.values())
        ):
            return  # Had and has no web UI: nothing depends on it
        self._index = None
        new = self._discovered_index()
        if old is None:
            self.async_sync_device_links()
        else:
            for view_id, view in old.views.items():
                moved = new.views.get(view_id)
                if moved is not None and moved.origin != view.origin:
                    # A device's stored site data must not follow it to another site
                    self._async_forget_site_data(view_id)
            # Devices sharing a URL follow when the one owning its view changes
            for other in {device_id} | {
                d
                for d in old.by_device.keys() | new.by_device.keys()
                if old.by_device.get(d) != new.by_device.get(d)
            }:
                if (changed := main_device(registry, other)) is not None:
                    self._async_sync_device_link(changed)
        self.async_sync_linked_devices()

    @callback
    def async_set_hidden(self, view_id: str, hidden: bool) -> None:
        (self.hidden.add if hidden else self.hidden.discard)(view_id)
        self._async_schedule_save()
        self.async_sync_device_links()
        self.async_sync_linked_devices()

    @callback
    def async_set_device_link(self, device_id: str, enabled: bool | None) -> None:
        """Choose the device page link for one device; None follows the global option."""
        if enabled is None:
            self.link_overrides.pop(device_id, None)
        else:
            self.link_overrides[device_id] = enabled
        self._async_schedule_save()
        if device := main_device(dr.async_get(self.hass), device_id):
            self._async_sync_device_link(device)

    # ---- linked devices --------------------------------------------------------

    @callback
    def async_sync_linked_devices(self) -> None:
        """Keep one "<name> web UI" device next to each device that has a web UI.

        It shares the device's connections (or identifiers), which makes it show
        up in the "Linked devices" card of that device's page. Its "Visit" link
        opens the web UI here. Nothing of the device itself is changed.
        """
        registry = dr.async_get(self.hass)
        wanted: dict[str, tuple[View, dr.DeviceEntry]] = {}
        if self.linked_devices_enabled:
            for view in [*self._static.values(), *self._discovered_index().views.values()]:
                if view.device_id is None or view.view_id in self.hidden:
                    continue
                target = main_device(registry, view.device_id)
                if target is None or target.disabled:
                    continue
                wanted[view.view_id] = (view, target)
        # Connections are unique within a config entry: two devices of one physical
        # device (one per integration) would otherwise merge into one linked device
        claimed: set[tuple[str, str]] = set()
        specs: dict[str, tuple[set[tuple[str, str]], set[tuple[str, str]], str]] = {}
        for view_id, (view, target) in wanted.items():
            connections = set(target.connections) - claimed
            claimed |= connections
            identifiers = {(DOMAIN, view_id)}
            if not connections:
                identifiers |= set(target.identifiers)
            specs[view_id] = (identifiers, connections, f"{view.name} web UI")
        # Remove everything that does not match first: creating a device whose
        # connection an outdated one still holds would merge the two
        existing: dict[str, dr.DeviceEntry] = {}
        for device in dr.async_entries_for_config_entry(registry, self.entry.entry_id):
            view_id = next((i for d, i in device.identifiers if d == DOMAIN), None)
            spec = specs.get(view_id) if view_id is not None else None
            if (
                spec is None
                or view_id in existing
                or device.identifiers != spec[0]
                or device.connections != spec[1]
            ):
                self._write(registry.async_remove_device, device.id)
            else:
                existing[view_id] = device
        for view_id, (identifiers, connections, name) in specs.items():
            url = f"{DEVICE_LINK_PREFIX}{view_id}"
            if (device := existing.get(view_id)) is None:
                self._write(
                    registry.async_get_or_create,
                    config_entry_id=self.entry.entry_id,
                    identifiers=identifiers,
                    connections=connections,
                    name=name,
                    manufacturer=NAME,
                    model="Web UI",
                    entry_type=dr.DeviceEntryType.SERVICE,
                    configuration_url=url,
                )
            elif device.name != name or device.configuration_url != url:
                self._write(
                    registry.async_update_device, device.id, name=name, configuration_url=url
                )

    # ---- per-user site state -------------------------------------------------

    @staticmethod
    def _state_key(user_id: str, view_id: str) -> str:
        return f"{user_id}|{view_id}"

    def cookie_jar(self, user_id: str, view: View) -> aiohttp.CookieJar:
        """Cookies the site set for this user, kept server side."""
        key = self._state_key(user_id, view.view_id)
        if (jar := self._jars.get(key)) is None:
            # unsafe=True: device UIs are usually addressed by IP
            jar = self._jars[key] = aiohttp.CookieJar(unsafe=True, quote_cookie=False)
            for saved in self._cookies.get(key, []):
                jar.update_cookies(_parse_cookie(saved["cookie"]), URL(saved["url"]))
        return jar

    @callback
    def async_store_cookies(
        self,
        user_id: str,
        view: View,
        set_cookies: list[str],
        url: URL,
        from_script: bool = False,
    ) -> None:
        """Keep cookies a site set (Set-Cookie or document.cookie) for this user."""
        jar = self.cookie_jar(user_id, view)
        for set_cookie in set_cookies:
            if len(set_cookie) > MAX_COOKIE_BYTES:
                continue
            cookie = _parse_cookie(set_cookie)
            if not cookie:
                continue
            if from_script:
                # As in browsers: scripts cannot set HttpOnly cookies or replace them
                http_only = {morsel.key for morsel in jar if morsel.get("httponly")}
                if set(cookie) & http_only:
                    continue
                for morsel in cookie.values():
                    morsel["httponly"] = ""
            existing = {morsel.key for morsel in jar}
            if len(existing) >= MAX_COOKIES_PER_SITE and not set(cookie) <= existing:
                _LOGGER.debug("%s set too many cookies; ignoring more", view.name)
                continue
            jar.update_cookies(cookie, url)
        key = self._state_key(user_id, view.view_id)
        self._cookies[key] = [
            {"cookie": morsel.OutputString(), "url": str(view.origin)} for morsel in jar
        ]
        self._async_schedule_save()

    def shim_storage(self, user_id: str, view_id: str) -> dict[str, str]:
        return self._shim_storage.get(self._state_key(user_id, view_id), {})

    def applied_writes(self, user_id: str, view_id: str) -> list[str]:
        return list(self._applied_writes.get(self._state_key(user_id, view_id), ()))

    @callback
    def async_apply_storage_write(
        self,
        user_id: str,
        view_id: str,
        write_id: str,
        changes: dict[str, str | None],
        *,
        clear: bool,
        page: tuple[str, int] | None = None,
    ) -> bool:
        """Apply a page's localStorage changes; False if over the size limits.

        page is the page's own id and the write's sequence number: a write that
        arrives after a later one of the same page is ignored.
        """
        key = self._state_key(user_id, view_id)
        applied = True
        if page is not None and (last := self._page_writes.get((key, page[0]))) is not None:
            applied = page[1] > last
        if applied:
            data = {} if clear else dict(self._shim_storage.get(key, {}))
            for item, value in changes.items():
                if value is None:
                    data.pop(item, None)
                else:
                    data[item] = value
            size = sum(len(k) + len(v) for k, v in data.items())
            if len(data) > MAX_SHIM_STORAGE_KEYS or size > MAX_SHIM_STORAGE_BYTES:
                applied = False
            else:
                self._shim_storage[key] = data
                self._async_schedule_save()
                if page is not None:
                    self._page_writes[(key, page[0])] = page[1]
                    self._page_writes.move_to_end((key, page[0]))
                    while len(self._page_writes) > 256:
                        self._page_writes.popitem(last=False)
            within_limits = applied
        else:
            within_limits = True  # Outdated, not too large
        if write_id:
            # Recorded even when not applied: a page load waiting for it must not
            # wait for a write that will never come
            self._applied_writes.setdefault(key, deque(maxlen=32)).append(write_id)
            if (waiter := self._write_waiters.pop((key, write_id), None)) is not None:
                waiter.set()
        return within_limits

    async def async_wait_for_storage_write(self, user_id: str, view_id: str, write_id: str) -> None:
        """Wait until a page's last write arrived (it may race the next page load)."""
        key = self._state_key(user_id, view_id)
        if write_id in self._applied_writes.get(key, ()):
            return
        waiter = self._write_waiters.setdefault((key, write_id), asyncio.Event())
        try:
            async with asyncio.timeout(STORAGE_WRITE_WAIT):
                await waiter.wait()
        except TimeoutError:
            pass
        finally:
            # Also when the request is cancelled; others may still wait on it
            if self._write_waiters.get((key, write_id)) is waiter and not waiter.is_set():
                self._write_waiters.pop((key, write_id), None)
                waiter.set()

    @callback
    def async_clear_site_data(self, user_id: str, view_id: str) -> None:
        """Forget the cookies and stored data a site keeps for this user (log out)."""
        key = self._state_key(user_id, view_id)
        self._jars.pop(key, None)
        self._cookies.pop(key, None)
        self._shim_storage.pop(key, None)
        self._applied_writes.pop(key, None)
        self._async_schedule_save()

    @callback
    def _async_forget_view(self, view_id: str) -> None:
        """Everything kept for a web UI that is gone."""
        self.hidden.discard(view_id)
        self._async_forget_site_data(view_id)

    @callback
    def _async_forget_site_data(self, view_id: str) -> None:
        """Every user's cookies and stored data for a web UI (gone or moved)."""
        suffix = f"|{view_id}"
        for store in (self._jars, self._cookies, self._shim_storage, self._applied_writes):
            for key in [k for k in store if k.endswith(suffix)]:
                del store[key]
        self._async_schedule_save()

    @callback
    def _async_user_removed(self, event: Event[dict[str, Any]]) -> None:
        user_id = event.data["user_id"]
        self.sessions.revoke_user(user_id)
        self._async_forget_user(user_id)

    @callback
    def _async_forget_user(self, user_id: str) -> None:
        prefix = f"{user_id}|"
        for store in (self._jars, self._cookies, self._shim_storage, self._applied_writes):
            for key in [k for k in store if k.startswith(prefix)]:
                del store[key]
        self._async_schedule_save()

    # ---- upstream HTTP -------------------------------------------------------

    def http_client(self, view: View) -> aiohttp.ClientSession:
        """HTTP client for a view's upstream requests."""
        if view.source == "discovered":
            # URLs chosen by devices and integrations: only connect to LAN addresses
            key: str | bool = "discovered"
        else:
            key = view.verify_ssl
        if (client := self._http.get(key)) is not None:
            return client
        if key == "discovered":
            try:
                # Home Assistant's shared resolver, which also resolves .local over mDNS
                inner = aiohttp_client._async_get_or_create_resolver(self.hass)
            except Exception:  # noqa: BLE001 - private helper; fall back to the default
                inner = aiohttp.ThreadedResolver()
            client = aiohttp.ClientSession(
                connector=aiohttp.TCPConnector(ssl=False, resolver=LanOnlyResolver(inner)),
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        else:
            # HA's shared connector resolves .local names over mDNS
            client = async_create_clientsession(
                self.hass,
                verify_ssl=view.verify_ssl,
                auto_cleanup=False,
                cookie_jar=aiohttp.DummyCookieJar(),
            )
        self._http[key] = client
        return client

    def request_limiter(self, view: View) -> asyncio.Semaphore:
        """Caps concurrent requests to one site, which may be a small device."""
        key = str(view.origin)
        if (limiter := self._limiters.get(key)) is None:
            limiter = self._limiters[key] = asyncio.Semaphore(MAX_REQUESTS_PER_SITE)
        return limiter

    # ---- persistence ---------------------------------------------------------

    def _data(self) -> dict[str, Any]:
        return {
            "originals": self.originals,
            "hidden": sorted(self.hidden),
            "link_overrides": self.link_overrides,
        }

    def _jar_data(self) -> dict[str, Any]:
        return {"cookies": self._cookies, "storage": self._shim_storage}

    @callback
    def _async_schedule_save(self) -> None:
        if not self._loaded:
            return
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


def main_device(registry: dr.DeviceRegistry, device_id: str) -> dr.DeviceEntry | None:
    """A device by id, ignoring child devices (HA 2026+), which have no own URL."""
    device = registry.async_get(device_id)
    return device if isinstance(device, dr.DeviceEntry) else None


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
