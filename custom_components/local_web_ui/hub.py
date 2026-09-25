"""Views, sessions, per-user site state and device page links."""

from __future__ import annotations

import asyncio
import base64
from collections import OrderedDict, deque
from collections.abc import Callable, Coroutine
from dataclasses import dataclass, field, replace
from datetime import timedelta
from functools import partial
import logging
import secrets
import time
from typing import Any

import aiohttp
from homeassistant.auth import EVENT_USER_REMOVED
from homeassistant.config_entries import SOURCE_INTEGRATION_DISCOVERY, ConfigEntry
from homeassistant.core import CALLBACK_TYPE, Event, HomeAssistant, callback
from homeassistant.helpers import (
    aiohttp_client,
    area_registry as ar,
    device_registry as dr,
    discovery_flow,
)
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
    CONF_KIND,
    CONF_LINK_DEVICE_PAGES,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_PREVIOUS_VIEW_ID,
    CONF_SHOW_IN_SIDEBAR,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    CONF_VISIT_LINK,
    DEFAULT_DISCOVERY,
    DEFAULT_LINK_DEVICE_PAGES,
    DEVICE_LINK_PREFIX,
    DISCOVERED_PREFIX,
    DOMAIN,
    KIND_HUB,
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
    VISIT_DEFAULT,
    VISIT_HERE,
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
    source: str  # SOURCE_DEVICE or SOURCE_MANUAL
    device_id: str | None
    show_in_sidebar: bool
    icon: str | None
    subtitle: str

    @property
    def url(self) -> str:
        """Full URL of the entry page, for display."""
        return str(self.origin) + self.entry


SOURCE_DEVICE = "device"  # A device's own web page, followed when it changes
SOURCE_MANUAL = "manual"  # A URL added by hand


def device_unique_id(device_id: str) -> str:
    """Unique id of the web UI entry of a device."""
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

    def end_view(self, view_id: str) -> None:
        """End every session of one web UI (it was disabled or removed)."""
        for session in [s for s in self._sessions.values() if s.view_id == view_id]:
            self._end(session)

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


class LocalWebUiHub:
    """Runtime state of the integration: web UIs, sessions, site data, device links.

    One for the whole integration. Config entries feed it: the hub entry its global
    options, and each web UI entry one view. It follows the device registry while
    any entry is loaded.
    """

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self.sessions = async_get_sessions(hass)
        # Private: they hold device session cookies and tokens sites keep in storage
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY, private=True, atomic_writes=True
        )
        self._jar_store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, STORAGE_KEY_JAR, private=True, atomic_writes=True
        )
        self.hub_entry: ConfigEntry | None = None
        self.view_entries: dict[str, ConfigEntry] = {}
        # device_id -> configuration_url the owning integration set, while we link it
        self.originals: dict[str, str] = {}
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
        # view_id -> (configured origin, https origin the site redirected to)
        self._upgrades: dict[str, tuple[URL, URL]] = {}
        self._views: dict[str, View] = {}
        # device_id -> (what its candidate view was computed from, the view)
        self._device_views: dict[str, tuple[tuple[Any, ...], View | None]] = {}
        # Devices offered as discovered web UIs during this run
        self._offered: set[str] = set()
        self._trusted: frozenset[str] = frozenset()
        self._unsubs: list[Callable[[], None]] = []
        self._own_hosts: frozenset[tuple[str, int]] = frozenset()
        self._loaded = False  # Never save over storage that was not loaded
        # Our own registry writes fire registry events too; those need no handling
        self._writing = 0
        # Called whenever views may have changed, so their sidebar panels follow
        self.on_views_changed: Callable[[], None] | None = None

    # ---- options ---------------------------------------------------------------

    def _hub_option(self, key: str, default: Any) -> Any:
        return default if self.hub_entry is None else self.hub_entry.options.get(key, default)

    @property
    def discovery_enabled(self) -> bool:
        return self.hub_entry is not None and self._hub_option(CONF_DISCOVERY, DEFAULT_DISCOVERY)

    @property
    def link_default(self) -> bool:
        return self._hub_option(CONF_LINK_DEVICE_PAGES, DEFAULT_LINK_DEVICE_PAGES)

    def visit_link(self, view_id: str) -> str:
        entry = self.view_entries.get(view_id)
        return VISIT_DEFAULT if entry is None else entry.options.get(CONF_VISIT_LINK, VISIT_DEFAULT)

    def link_enabled(self, view_id: str) -> bool:
        """Whether the device's own "Visit" button opens this web UI here."""
        choice = self.visit_link(view_id)
        return self.link_default if choice == VISIT_DEFAULT else choice == VISIT_HERE

    # ---- lifecycle ---------------------------------------------------------

    @property
    def active(self) -> bool:
        return self.hub_entry is not None or bool(self.view_entries)

    async def async_load(self) -> None:
        """Load stored state; once per run, before any entry is set up."""
        data = await self._store.async_load() or {}
        self.originals = dict(data.get("originals", {}))
        jar = await self._jar_store.async_load() or {}
        self._cookies = dict(jar.get("cookies", {}))
        self._shim_storage = dict(jar.get("storage", {}))
        self._loaded = True
        # Users removed while the integration was not running
        users = {user.id for user in await self.hass.auth.async_get_users()}
        for user_id in {key.partition("|")[0] for key in (*self._cookies, *self._shim_storage)}:
            if user_id not in users:
                self._async_forget_user(user_id)

    @callback
    def _async_start(self) -> None:
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

    async def _async_stop(self) -> None:
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

    async def async_add_entry(self, entry: ConfigEntry) -> None:
        """A hub or web UI entry was set up."""
        if not self.active:
            self._async_start()
        if entry.data.get(CONF_KIND) == KIND_HUB:
            self.hub_entry = entry
            # Before 0.3 the one entry kept linked devices of its own
            registry = dr.async_get(self.hass)
            for device in dr.async_entries_for_config_entry(registry, entry.entry_id):
                self._write(registry.async_remove_device, device.id)
        else:
            self.view_entries[entry.entry_id] = entry
            for previous in (
                entry.data.get(CONF_PREVIOUS_VIEW_ID),
                f"{DISCOVERED_PREFIX}{entry.data[CONF_DEVICE_ID]}"
                if entry.data.get(CONF_DEVICE_ID)
                else None,
            ):
                if previous:
                    self._async_move_site_data(previous, entry.entry_id)
        self.async_refresh()

    async def async_remove_entry(self, entry: ConfigEntry, gone: bool) -> None:
        """A hub or web UI entry was unloaded; gone: disabled or removed for good."""
        if entry.data.get(CONF_KIND) == KIND_HUB:
            if self.hub_entry is entry:
                self.hub_entry = None
        elif self.view_entries.pop(entry.entry_id, None) is not None and gone:
            # Device pages must not point at it, and open views must stop working
            self.sessions.end_view(entry.entry_id)
            if (device_id := entry.data.get(CONF_DEVICE_ID)) and (
                device := main_device(dr.async_get(self.hass), device_id)
            ):
                self._async_restore_device_link(device)
        self.async_refresh()
        if not self.active:
            await self._async_stop()

    @callback
    def async_view_removed(self, entry: ConfigEntry) -> None:
        """A web UI entry was deleted: end it everywhere and forget its data."""
        self.sessions.end_view(entry.entry_id)
        self._async_forget_site_data(entry.entry_id)
        if (device_id := entry.data.get(CONF_DEVICE_ID)) and (
            device := main_device(dr.async_get(self.hass), device_id)
        ):
            self._async_restore_device_link(device)
            self._offered.discard(device_id)
            if self.active:
                self.async_discover(only=device)  # Offer it again

    @callback
    def _write(self, method: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Change the device registry without handling the events it fires."""
        self._writing += 1
        try:
            return method(*args, **kwargs)
        finally:
            self._writing -= 1

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

    @callback
    def async_refresh(self) -> None:
        """Bring views, device links, web UI devices and discovery up to date."""
        previous = self._views
        self._views = self._build_views()
        for view_id, old in previous.items():
            # A web UI moved to another site must not hand it the old site's data
            if (view := self._views.get(view_id)) is not None and view.origin != old.origin:
                self._async_forget_site_data(view_id)
        self.async_sync_device_links()
        for entry in self.view_entries.values():
            self._async_sync_entry_device(entry)
        self.async_discover()
        trusted = frozenset(v.name for v in self._views.values() if v.mode == MODE_TRUSTED)
        if trusted - self._trusted:
            _LOGGER.warning(
                "These web UIs run in trusted mode and can act as the logged-in admin "
                "in Home Assistant: %s",
                ", ".join(sorted(trusted)),
            )
        self._trusted = trusted
        if self.on_views_changed is not None:
            self.on_views_changed()

    # ---- views -------------------------------------------------------------

    def _build_views(self) -> dict[str, View]:
        registry = dr.async_get(self.hass)
        views: dict[str, View] = {}
        for entry in self.view_entries.values():
            options = entry.options
            device_id = entry.data.get(CONF_DEVICE_ID)
            subtitle = ""
            if device_id:
                # The device's own link, followed when its integration changes it
                device = main_device(registry, device_id)
                if device is None or device.disabled or (url := self.device_ui_url(device)) is None:
                    continue
                if candidate := self._device_view(device):
                    subtitle = candidate.subtitle
            elif (url := parse_http_url(options.get(CONF_URL))) is None:
                _LOGGER.warning("Ignoring web UI %s with invalid URL", entry.title)
                continue
            origin, entry_path = view_from_url(url)
            authorization = None
            if options.get(CONF_USERNAME):
                authorization = basic_authorization(
                    options[CONF_USERNAME], options.get(CONF_PASSWORD) or ""
                )
            views[entry.entry_id] = View(
                view_id=entry.entry_id,
                name=entry.title,
                origin=origin,
                entry=entry_path,
                mode=options.get(CONF_MODE, MODE_ISOLATED),
                # Device UIs on the LAN rarely have trusted certificates
                verify_ssl=options.get(CONF_VERIFY_SSL, not device_id),
                authorization=authorization,
                source=SOURCE_DEVICE if device_id else SOURCE_MANUAL,
                device_id=device_id,
                show_in_sidebar=options.get(CONF_SHOW_IN_SIDEBAR, False),
                icon=options.get(CONF_ICON),
                subtitle=subtitle or url.host or "",
            )
        return views

    @property
    def views(self) -> dict[str, View]:
        return self._views

    def get_view(self, view_id: str) -> View | None:
        return self._views.get(view_id)

    def view_for_device(self, device_id: str) -> View | None:
        return next((v for v in self._views.values() if v.device_id == device_id), None)

    def device_ui_url(self, device: dr.DeviceEntry) -> URL | None:
        """The device's own web UI URL, looking through our link if we set one."""
        current = device.configuration_url
        if current and current.startswith(DEVICE_LINK_PREFIX):
            current = self.originals.get(device.id)
        url = parse_http_url(current)
        if url is None or not is_local_ui_url(url, self._own_hosts):
            return None
        return url

    def _candidate_view(self, device: dr.DeviceEntry, url: URL) -> View:
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
            verify_ssl=False,
            authorization=None,
            source=SOURCE_DEVICE,
            device_id=device.id,
            show_in_sidebar=False,
            icon=None,
            subtitle=subtitle,
        )

    def _device_view(self, device: dr.DeviceEntry) -> View | None:
        """What a device's web UI would be; remembered, as parsing URLs adds up."""
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
        view = None if url is None else self._candidate_view(device, url)
        self._device_views[device.id] = (key, view)
        return view

    def area_name(self, device_id: str | None) -> str | None:
        if device_id is None:
            return None
        device = main_device(dr.async_get(self.hass), device_id)
        if device is None or device.area_id is None:
            return None
        area = ar.async_get(self.hass).async_get_area(device.area_id)
        return area.name if area else None

    def _own_device(self, device: dr.DeviceEntry) -> bool:
        return device.config_entry_id in self.view_entries or (
            self.hub_entry is not None and device.config_entry_id == self.hub_entry.entry_id
        )

    # ---- discovery -------------------------------------------------------------

    @callback
    def async_discover(self, only: dr.DeviceEntry | None = None) -> None:
        """Offer devices with a local web page as discovered web UIs (Add or Ignore)."""
        if not self.discovery_enabled:
            self._offered.clear()
            for flow in self.hass.config_entries.flow.async_progress_by_handler(
                DOMAIN, match_context={"source": SOURCE_INTEGRATION_DISCOVERY}
            ):
                self.hass.config_entries.flow.async_abort(flow["flow_id"])
            return
        known = {
            entry.unique_id
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if entry.unique_id
        }
        # One offer per URL: since 2026.8 a device known to several integrations is
        # one device per integration, each with the same link
        urls = {view.url for view in self._views.values()}
        devices = [only] if only is not None else iter_devices(dr.async_get(self.hass))
        for device in devices:
            if device.disabled or self._own_device(device):
                continue
            if (candidate := self._device_view(device)) is None or candidate.url in urls:
                continue
            urls.add(candidate.url)
            if device_unique_id(device.id) in known or device.id in self._offered:
                continue
            self._offered.add(device.id)
            discovery_flow.async_create_flow(
                self.hass,
                DOMAIN,
                context={"source": SOURCE_INTEGRATION_DISCOVERY},
                data={
                    CONF_DEVICE_ID: device.id,
                    "name": candidate.name,
                    CONF_URL: candidate.url,
                    "subtitle": candidate.subtitle,
                },
            )

    # ---- device page links ---------------------------------------------------

    @callback
    def async_sync_device_links(self) -> None:
        registry = dr.async_get(self.hass)
        for device_id in {
            *self.originals,
            *(view.device_id for view in self._views.values() if view.device_id),
        }:
            if device := main_device(registry, device_id):
                self._async_sync_device_link(device)

    @callback
    def _async_sync_device_link(self, device: dr.DeviceEntry) -> None:
        """Point the device's "Visit" link at its web UI here, or give it back."""
        if self._own_device(device):
            return  # Our web UI devices always point here
        registry = dr.async_get(self.hass)
        current = device.configuration_url
        ours = bool(current and current.startswith(DEVICE_LINK_PREFIX))
        view = self.view_for_device(device.id)
        link = view is not None and self.link_enabled(view.view_id)
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
            self._async_restore_device_link(device)

    @callback
    def _async_restore_device_link(self, device: dr.DeviceEntry) -> None:
        original = self.originals.pop(device.id, None)
        self._async_schedule_save()
        if (device.configuration_url or "").startswith(DEVICE_LINK_PREFIX):
            registry = dr.async_get(self.hass)
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
        if device is not None and self._own_device(device):
            return  # One of our web UI devices
        if device is None:
            # Removed, or turned into a child device (2026.9): no link of its own
            self._device_views.pop(device_id, None)
            if self.originals.pop(device_id, None) is not None:
                self._async_schedule_save()
        if any(e.data.get(CONF_DEVICE_ID) == device_id for e in self.view_entries.values()):
            self.async_refresh()  # A web UI's device: its URL, name or state changed
        elif device is not None:
            self._async_sync_device_link(device)
            self.async_discover(only=device)

    # ---- web UI devices --------------------------------------------------------

    def linked_device_id(self, view_id: str) -> str | None:
        """The "<name> web UI" device of a web UI entry."""
        registry = dr.async_get(self.hass)
        device = registry.async_get_device_by_identifier((DOMAIN, view_id), view_id)
        return device.id if device else None

    @callback
    def _async_sync_entry_device(self, entry: ConfigEntry) -> None:
        """Give a web UI entry its "<name> web UI" device.

        When the web UI belongs to a device, it shares that device's connections
        (or identifiers), so it shows up in the "Linked devices" card of that
        device's page. Its "Visit" link opens the web UI here.
        """
        registry = dr.async_get(self.hass)
        target = None
        if device_id := entry.data.get(CONF_DEVICE_ID):
            target = main_device(registry, device_id)
        connections = set(target.connections) if target else set()
        identifiers = {(DOMAIN, entry.entry_id)}
        if target is not None and not connections:
            identifiers |= set(target.identifiers)
        name = f"{entry.title} web UI"
        url = f"{DEVICE_LINK_PREFIX}{entry.entry_id}"
        device = registry.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
        if device is not None and (
            device.identifiers != identifiers or device.connections != connections
        ):
            self._write(registry.async_remove_device, device.id)
            device = None
        if device is None:
            self._write(
                registry.async_get_or_create,
                config_entry_id=entry.entry_id,
                identifiers=identifiers,
                connections=connections,
                name=name,
                manufacturer=NAME,
                model="Web UI",
                entry_type=dr.DeviceEntryType.SERVICE,
                configuration_url=url,
            )
        elif device.name != name or device.configuration_url != url:
            self._write(registry.async_update_device, device.id, name=name, configuration_url=url)

    def effective_view(self, view: View) -> View:
        """The view, on https if its site redirected there (http to https, same host)."""
        if (upgrade := self._upgrades.get(view.view_id)) is not None and upgrade[0] == view.origin:
            return replace(view, origin=upgrade[1])
        return view

    @callback
    def async_upgrade_to_https(self, view: View, origin: URL) -> None:
        """Remember that a site moved its pages from http to https on the same host."""
        self._upgrades[view.view_id] = (view.origin, origin)

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
        self._async_forget_site_data(view_id)

    @callback
    def _async_move_site_data(self, old: str, new: str) -> None:
        """Keep users' logins and data when a web UI gets a new id (0.3 entries)."""
        for store in (self._cookies, self._shim_storage):
            for key in [k for k in store if k.endswith(f"|{old}")]:
                new_key = key[: -len(old)] + new
                value = store.pop(key)
                store.setdefault(new_key, value)
                self._jars.pop(new_key, None)
        self._async_schedule_save()

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
        if view.source == SOURCE_DEVICE:
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

    async def async_remove_storage(self) -> None:
        """Forget originals, cookies and stored site data (last entry removed).

        Removed through our own stores, so that saves still pending are cancelled.
        """
        self.originals.clear()
        self._cookies.clear()
        self._shim_storage.clear()
        self._jars.clear()
        self._offered.clear()
        await self._store.async_remove()
        await self._jar_store.async_remove()


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
