"""Websocket API, diagnostics and WebSocket relaying through the proxy."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
import json
import re
from types import MappingProxyType
from typing import Any

import aiohttp
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.auth.models import Credentials, RefreshToken
from homeassistant.config_entries import (
    ConfigEntryState,
    ConfigSubentry,
    ConfigSubentryDataWithId,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import area_registry as ar, device_registry as dr
from homeassistant.setup import async_setup_component
from homeassistant.util import dt as dt_util
import pytest
from pytest_homeassistant_custom_component.common import (
    CLIENT_ID,
    MockConfigEntry,
    MockUser,
    async_fire_time_changed,
)
from pytest_homeassistant_custom_component.components.diagnostics import (
    get_diagnostics_for_config_entry,
)
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    MockHAClientWebSocket,
    WebSocketGenerator,
)

from custom_components.local_web_ui import hub as hub_module
from custom_components.local_web_ui.const import (
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
    DOMAIN,
    MAX_WEBSOCKETS_PER_SESSION,
    MODE_ISOLATED,
    MODE_TRUSTED,
    SESSION_MAX_AGE,
    SESSION_TTL,
    SUBENTRY_TYPE_VIEW,
)
from custom_components.local_web_ui.hub import LocalWebUiHub, pinned_unique_id

OWNER_DOMAIN = "fake_ws_devices"
PORCH_URL = "http://192.168.1.50/"
KITCHEN_URL = "http://wled-kitchen.local/settings?page=1"
PRINTER_URL = "http://192.168.1.60:8080/"
ROUTER_URL = "http://192.168.1.1:8080/admin?tab=wifi"

ROUTER_ID = "router"
PRINTER_VIEW_ID = "printer"

ALL_COMMANDS: list[dict[str, Any]] = [
    {"type": f"{DOMAIN}/views"},
    {"type": f"{DOMAIN}/session", "view_id": ROUTER_ID},
    {"type": f"{DOMAIN}/pin", "view_id": ROUTER_ID},
    {"type": f"{DOMAIN}/set_hidden", "view_id": "d_abc", "hidden": True},
    {"type": f"{DOMAIN}/set_device_link", "device_id": "abc", "enabled": False},
    {"type": f"{DOMAIN}/clear_site_data", "view_id": ROUTER_ID},
]
COMMAND_IDS = [c["type"].split("/", 1)[1] for c in ALL_COMMANDS]

TOKEN_URL = re.compile(
    r"^/api/local_web_ui/(?P<view>[^/]+)/(?P<token>[A-Za-z0-9_-]{43})(?P<rest>/.*)$"
)

# What a browser may hold for Home Assistant's own origin: a reverse proxy's Basic
# auth and an SSO cookie. Neither may ever reach a device.
BROWSER_CREDENTIALS = {
    "Authorization": "Basic cmV2ZXJzZTpwcm94eQ==",
    "Cookie": "ha_sso=browser-secret",
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def link_for(view_id: str) -> str:
    return f"homeassistant://local-web-ui/{view_id}"


def link_state(enabled: bool, active: bool, override: bool | None = None) -> dict[str, Any]:
    """The device_link part of a view, as the panel gets it."""
    return {"enabled": enabled, "override": override, "active": active}


def view_subentry(subentry_id: str, url: str, title: str, **data: Any) -> ConfigSubentryDataWithId:
    return ConfigSubentryDataWithId(
        data={CONF_URL: url, CONF_MODE: MODE_ISOLATED, **data},
        subentry_id=subentry_id,
        subentry_type=SUBENTRY_TYPE_VIEW,
        title=title,
        unique_id=None,
    )


@pytest.fixture
async def http(hass: HomeAssistant) -> None:
    assert await async_setup_component(hass, "http", {})


@pytest.fixture
def owner(hass: HomeAssistant) -> MockConfigEntry:
    """Config entry of the integration that owns the devices (think ESPHome)."""
    entry = MockConfigEntry(domain=OWNER_DOMAIN, title="Fake devices")
    entry.add_to_hass(hass)
    return entry


def add_device(
    hass: HomeAssistant,
    owner: MockConfigEntry,
    key: str,
    url: str | None,
    name: str,
    **kwargs: Any,
) -> dr.DeviceEntry:
    return dr.async_get(hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={(OWNER_DOMAIN, key)},
        name=name,
        configuration_url=url,
        **kwargs,
    )


def url_of(hass: HomeAssistant, device_id: str) -> str | None:
    device = dr.async_get(hass).async_get(device_id)
    assert device is not None
    return device.configuration_url


async def setup_lwu(
    hass: HomeAssistant,
    options: dict[str, Any] | None = None,
    subentries: list[ConfigSubentryDataWithId] | None = None,
) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Local Web UIs",
        options=options if options is not None else {},
        subentries_data=subentries,
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    assert entry.state is ConfigEntryState.LOADED
    return entry


def hub_of(hass: HomeAssistant) -> LocalWebUiHub:
    return hass.data[DOMAIN]


async def ws_call(client: MockHAClientWebSocket, **msg: Any) -> dict[str, Any]:
    await client.send_json_auto_id(msg)
    response = await client.receive_json()
    assert response["type"] == "result"
    return response


async def ws_ok(client: MockHAClientWebSocket, **msg: Any) -> Any:
    response = await ws_call(client, **msg)
    assert response["success"], response
    return response["result"]


async def ws_error(client: MockHAClientWebSocket, **msg: Any) -> dict[str, Any]:
    response = await ws_call(client, **msg)
    assert not response["success"], response
    return response["error"]


async def list_views(client: MockHAClientWebSocket) -> dict[str, dict[str, Any]]:
    result = await ws_ok(client, type=f"{DOMAIN}/views")
    return {view["view_id"]: view for view in result["views"]}


@dataclass
class Login:
    """A Home Assistant login: a user, its refresh token and an access token."""

    user: MockUser
    refresh_token: RefreshToken
    access_token: str


async def create_admin_login(hass: HomeAssistant, name: str) -> Login:
    """A second admin user with a login of its own."""
    group = await hass.auth.async_get_group(GROUP_ID_ADMIN)
    user = MockUser(name=name, groups=[group]).add_to_hass(hass)
    credential = Credentials(
        id=f"mock-{name}-credential-id",
        auth_provider_type="homeassistant",
        auth_provider_id=None,
        data={"username": name},
        is_new=False,
    )
    user.credentials.append(credential)
    refresh_token = await hass.auth.async_create_refresh_token(
        user, CLIENT_ID, credential=credential
    )
    return Login(user, refresh_token, hass.auth.async_create_access_token(refresh_token))


async def wait_until(condition: Callable[[], bool]) -> None:
    """Let the event loop run until condition holds, for at most 5 seconds."""
    for _ in range(500):
        if condition():
            return
        await asyncio.sleep(0.01)
    pytest.fail("Condition not reached within 5 seconds")


class FakeClock:
    """Stands in for the time module inside hub.py (sessions use monotonic time)."""

    def __init__(self) -> None:
        self.now = 10_000.0

    def monotonic(self) -> float:
        return self.now


@pytest.fixture
def clock(monkeypatch: pytest.MonkeyPatch) -> FakeClock:
    fake = FakeClock()
    monkeypatch.setattr(hub_module, "time", fake)
    return fake


@dataclass
class Home:
    """Devices, an area and a config entry with two static views."""

    entry: MockConfigEntry
    porch: dr.DeviceEntry
    kitchen: dr.DeviceEntry
    printer: dr.DeviceEntry
    cloud: dr.DeviceEntry
    bare: dr.DeviceEntry


async def build_home(
    hass: HomeAssistant, owner: MockConfigEntry, options: dict[str, Any] | None = None
) -> Home:
    area = ar.async_get(hass).async_create("Porch")
    porch = add_device(hass, owner, "porch", PORCH_URL, "Porch light", suggested_area=None)
    dr.async_get(hass).async_update_device(porch.id, area_id=area.id)
    kitchen = add_device(hass, owner, "kitchen", KITCHEN_URL, "Kitchen WLED")
    printer = add_device(hass, owner, "printer", PRINTER_URL, "Printer")
    cloud = add_device(hass, owner, "cloud", "https://example.com/device", "Cloud thing")
    bare = add_device(hass, owner, "bare", None, "No UI")
    entry = await setup_lwu(
        hass,
        options=options,
        subentries=[
            view_subentry(
                ROUTER_ID,
                ROUTER_URL,
                "Router",
                **{
                    CONF_MODE: MODE_TRUSTED,
                    CONF_VERIFY_SSL: False,
                    CONF_SHOW_IN_SIDEBAR: True,
                    CONF_ICON: "mdi:router-wireless",
                    CONF_USERNAME: "rootuser",
                    CONF_PASSWORD: "hunter2",
                },
            ),
            view_subentry(
                PRINTER_VIEW_ID,
                PRINTER_URL,
                "Office printer",
                **{CONF_DEVICE_ID: printer.id},
            ),
        ],
    )
    return Home(entry, porch, kitchen, printer, cloud, bare)


@pytest.fixture
async def home(hass: HomeAssistant, http: None, owner: MockConfigEntry) -> Home:
    return await build_home(hass, owner)


@pytest.fixture
async def ws(home: Home, hass_ws_client: WebSocketGenerator) -> MockHAClientWebSocket:
    return await hass_ws_client()


# ---------------------------------------------------------------------------
# local_web_ui/views
# ---------------------------------------------------------------------------


async def test_views_lists_static_and_discovered(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    result = await ws_ok(ws, type=f"{DOMAIN}/views")
    assert result["discovery"] is True
    assert result["link_device_pages"] is True
    assert result["linked_devices"] is False
    assert result["entry_id"] == home.entry.entry_id

    views = {view["view_id"]: view for view in result["views"]}
    porch_id = f"d_{home.porch.id}"
    kitchen_id = f"d_{home.kitchen.id}"
    # The printer is pinned by a static view; the cloud device is not local
    assert set(views) == {ROUTER_ID, PRINTER_VIEW_ID, porch_id, kitchen_id}
    # Static views come first
    assert [v["view_id"] for v in result["views"]][:2] == [ROUTER_ID, PRINTER_VIEW_ID]

    assert views[ROUTER_ID] == {
        "view_id": ROUTER_ID,
        "name": "Router",
        "subtitle": "192.168.1.1",
        "url": ROUTER_URL,
        "mode": MODE_TRUSTED,
        "source": "static",
        "device_id": None,
        "area": None,
        "icon": "mdi:router-wireless",
        "show_in_sidebar": True,
        "hidden": False,
        "device_link": None,
    }
    # Credentials never reach the panel
    assert "hunter2" not in json.dumps(result)
    assert "rootuser" not in json.dumps(result)

    printer = views[PRINTER_VIEW_ID]
    assert printer["source"] == "static"
    assert printer["device_id"] == home.printer.id
    assert printer["url"] == PRINTER_URL
    assert printer["mode"] == MODE_ISOLATED
    assert printer["show_in_sidebar"] is False
    assert printer["device_link"] == link_state(enabled=True, active=True)
    assert url_of(hass, home.printer.id) == link_for(PRINTER_VIEW_ID)

    porch = views[porch_id]
    assert porch["source"] == "discovered"
    assert porch["name"] == "Porch light"
    assert porch["url"] == PORCH_URL
    assert porch["mode"] == MODE_ISOLATED
    assert porch["device_id"] == home.porch.id
    assert porch["area"] == "Porch"
    assert porch["hidden"] is False
    assert porch["icon"] is None
    assert porch["show_in_sidebar"] is False
    assert porch["subtitle"].endswith("192.168.1.50")
    assert porch["device_link"] == link_state(enabled=True, active=True)

    kitchen = views[kitchen_id]
    assert kitchen["url"] == KITCHEN_URL
    assert kitchen["area"] is None
    assert kitchen["subtitle"].endswith("wled-kitchen.local")


async def test_views_hidden_views_are_listed_and_flagged(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    porch_id = f"d_{home.porch.id}"
    await ws_ok(ws, type=f"{DOMAIN}/set_hidden", view_id=porch_id, hidden=True)

    views = await list_views(ws)
    assert views[porch_id]["hidden"] is True
    assert views[f"d_{home.kitchen.id}"]["hidden"] is False
    # Hiding also stops sending the device page here
    assert views[porch_id]["device_link"] == link_state(enabled=True, active=False)
    assert url_of(hass, home.porch.id) == PORCH_URL
    # A hidden view can still be opened (the panel shows it under "hidden")
    session = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=porch_id)
    assert session["view"]["hidden"] is True

    await ws_ok(ws, type=f"{DOMAIN}/set_hidden", view_id=porch_id, hidden=False)
    views = await list_views(ws)
    assert views[porch_id]["hidden"] is False
    assert views[porch_id]["device_link"] == link_state(enabled=True, active=True)
    assert url_of(hass, home.porch.id) == link_for(porch_id)


@pytest.mark.parametrize(
    ("options", "discovery", "link", "linked"),
    [
        ({}, True, True, False),
        ({CONF_DISCOVERY: True, CONF_LINK_DEVICE_PAGES: False}, True, False, False),
        ({CONF_DISCOVERY: False, CONF_LINK_DEVICE_PAGES: True}, False, True, False),
        ({CONF_LINKED_DEVICES: True}, True, True, True),
    ],
)
async def test_views_reports_discovery_flags(
    hass: HomeAssistant,
    http: None,
    owner: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    options: dict[str, Any],
    discovery: bool,
    link: bool,
    linked: bool,
) -> None:
    home = await build_home(hass, owner, options)
    ws = await hass_ws_client()
    result = await ws_ok(ws, type=f"{DOMAIN}/views")
    assert result["discovery"] is discovery
    assert result["link_device_pages"] is link
    assert result["linked_devices"] is linked

    views = {view["view_id"]: view for view in result["views"]}
    discovered = {vid for vid, v in views.items() if v["source"] == "discovered"}
    if discovery:
        assert discovered == {f"d_{home.porch.id}", f"d_{home.kitchen.id}"}
        assert views[f"d_{home.porch.id}"]["device_link"] == link_state(enabled=link, active=link)
    else:
        assert discovered == set()
    assert views[PRINTER_VIEW_ID]["device_link"] == link_state(enabled=link, active=link)
    expected_printer_url = link_for(PRINTER_VIEW_ID) if link else PRINTER_URL
    assert url_of(hass, home.printer.id) == expected_printer_url


async def test_views_skip_disabled_devices(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    dr.async_get(hass).async_update_device(home.kitchen.id, disabled_by=dr.DeviceEntryDisabler.USER)
    await hass.async_block_till_done()
    views = await list_views(ws)
    assert f"d_{home.kitchen.id}" not in views
    assert f"d_{home.porch.id}" in views
    error = await ws_error(ws, type=f"{DOMAIN}/session", view_id=f"d_{home.kitchen.id}")
    assert error["code"] == "not_found"


# ---------------------------------------------------------------------------
# local_web_ui/session
# ---------------------------------------------------------------------------


async def test_session_create(hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket) -> None:
    result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID)
    token = result["token"]
    assert result["url"] == f"/api/local_web_ui/{ROUTER_ID}/{token}/admin?tab=wifi"
    match = TOKEN_URL.match(result["url"])
    assert match is not None
    assert match["view"] == ROUTER_ID
    assert match["token"] == token
    assert result["expires_in"] == SESSION_TTL
    assert result["view"]["view_id"] == ROUTER_ID
    assert result["view"]["name"] == "Router"
    assert result["view"]["mode"] == MODE_TRUSTED

    # Each call without a token makes a new session
    again = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID)
    assert again["token"] != token


async def test_session_url_for_discovered_view_keeps_entry_path_and_query(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    kitchen_id = f"d_{home.kitchen.id}"
    result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=kitchen_id)
    assert result["url"] == f"/api/local_web_ui/{kitchen_id}/{result['token']}/settings?page=1"
    assert result["view"]["source"] == "discovered"

    porch_id = f"d_{home.porch.id}"
    result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=porch_id)
    assert result["url"] == f"/api/local_web_ui/{porch_id}/{result['token']}/"


async def test_session_extend_returns_same_token(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket, clock: FakeClock
) -> None:
    first = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID)
    token = first["token"]

    # Keepalives well within the TTL extend the same session...
    for _ in range(3):
        clock.now += SESSION_TTL * 0.8
        result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID, token=token)
        assert result["token"] == token
        assert result["url"] == first["url"]
        assert result["expires_in"] == SESSION_TTL
    # ...which by now has lived far longer than a single TTL
    session = hub_of(hass).sessions.touch(token, ROUTER_ID)
    assert session is not None

    # Once expired, the token is not revived: a fresh session replaces it
    clock.now += SESSION_TTL + 1
    result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID, token=token)
    assert result["token"] != token
    assert hub_of(hass).sessions.touch(token, ROUTER_ID) is None


async def test_session_has_an_absolute_lifetime(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket, clock: FakeClock
) -> None:
    """Keepalives extend a session, but never past SESSION_MAX_AGE after it was created."""
    first = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID)
    token = first["token"]
    sessions = hub_of(hass).sessions
    session = sessions.get(token)
    assert session is not None
    end = session.created + SESSION_MAX_AGE

    step = SESSION_TTL * 0.9
    while clock.now + step < end - 10:
        clock.now += step
        result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID, token=token)
        assert result["token"] == token
    # The last keepalive cannot move the end past the absolute limit
    clock.now = end - 10
    result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID, token=token)
    assert result["token"] == token
    assert session.expires == end

    clock.now = end + 1
    assert sessions.touch(token, ROUTER_ID) is None
    result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID, token=token)
    assert result["token"] != token
    assert sessions.get(token) is None


async def test_session_survives_entry_reload(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    """Open views keep working when the entry reloads."""
    first = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID)
    assert await hass.config_entries.async_reload(home.entry.entry_id)
    await hass.async_block_till_done()
    result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID, token=first["token"])
    assert result["token"] == first["token"]


async def test_session_unknown_token_creates_new_session(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID, token="made-up")
    assert result["token"] != "made-up"
    assert TOKEN_URL.match(result["url"])


async def test_session_token_of_other_view_is_not_reused(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    router = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID)
    printer = await ws_ok(
        ws, type=f"{DOMAIN}/session", view_id=PRINTER_VIEW_ID, token=router["token"]
    )
    assert printer["token"] != router["token"]
    assert printer["url"] == f"/api/local_web_ui/{PRINTER_VIEW_ID}/{printer['token']}/"
    sessions = hub_of(hass).sessions
    # The new session is bound to the printer, and the router's is left alone
    assert sessions.touch(printer["token"], PRINTER_VIEW_ID) is not None
    assert sessions.touch(printer["token"], ROUTER_ID) is None
    assert sessions.touch(router["token"], ROUTER_ID) is not None
    assert sessions.touch(router["token"], PRINTER_VIEW_ID) is None


async def test_session_token_of_other_user_is_not_reused(
    hass: HomeAssistant,
    home: Home,
    ws: MockHAClientWebSocket,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
    clock: FakeClock,
) -> None:
    mine = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID)
    sessions = hub_of(hass).sessions
    my_session = sessions.get(mine["token"])
    assert my_session is not None
    expires = my_session.expires

    other = await create_admin_login(hass, "other")
    other_ws = await hass_ws_client(access_token=other.access_token)
    clock.now += SESSION_TTL * 0.8
    theirs = await ws_ok(other_ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID, token=mine["token"])
    assert theirs["token"] != mine["token"]

    # Presenting someone else's token neither takes it over nor keeps it alive
    assert sessions.get(mine["token"]) is my_session
    assert my_session.user_id == hass_admin_user.id
    assert my_session.expires == expires
    fresh = sessions.get(theirs["token"])
    assert fresh is not None
    assert fresh.user_id == other.user.id
    assert fresh.refresh_token_id == other.refresh_token.id

    # And the other way round
    their_expires = fresh.expires
    back = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID, token=theirs["token"])
    assert back["token"] not in (theirs["token"], mine["token"])
    assert fresh.expires == their_expires

    # My session was not extended by the other user's request: it runs out on time
    clock.now += SESSION_TTL * 0.3
    assert sessions.touch(mine["token"], ROUTER_ID) is None
    assert sessions.touch(theirs["token"], ROUTER_ID) is not None


async def test_session_ends_when_its_login_is_revoked(
    hass: HomeAssistant,
    home: Home,
    ws: MockHAClientWebSocket,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """Logging out (removing the refresh token) ends the sessions made with that login."""
    other = await create_admin_login(hass, "other")
    other_ws = await hass_ws_client(access_token=other.access_token)
    theirs = [
        (await ws_ok(other_ws, type=f"{DOMAIN}/session", view_id=view_id))["token"]
        for view_id in (ROUTER_ID, PRINTER_VIEW_ID)
    ]
    # Sessions outlive the panel connection that asked for them
    await other_ws.close()
    mine = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID)
    sessions = hub_of(hass).sessions
    assert all(sessions.get(token) is not None for token in theirs)

    hass.auth.async_remove_refresh_token(other.refresh_token)
    await hass.async_block_till_done()

    assert [sessions.get(token) for token in theirs] == [None, None]
    assert sessions.touch(theirs[0], ROUTER_ID) is None
    # Other logins are not affected
    assert sessions.touch(mine["token"], ROUTER_ID) is not None
    result = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID, token=mine["token"])
    assert result["token"] == mine["token"]


@pytest.mark.parametrize("view_id", ["nope", "d_nope", ""])
async def test_session_unknown_view(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket, view_id: str
) -> None:
    error = await ws_error(ws, type=f"{DOMAIN}/session", view_id=view_id)
    assert error["code"] == "not_found"


async def test_session_for_pinned_device_discovered_id_is_not_found(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    """The printer's discovered id is superseded by its static view."""
    error = await ws_error(ws, type=f"{DOMAIN}/session", view_id=f"d_{home.printer.id}")
    assert error["code"] == "not_found"
    error = await ws_error(ws, type=f"{DOMAIN}/session", view_id=f"d_{home.cloud.id}")
    assert error["code"] == "not_found"


# ---------------------------------------------------------------------------
# local_web_ui/pin
# ---------------------------------------------------------------------------


async def test_pin_discovered_view(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    kitchen_id = f"d_{home.kitchen.id}"
    old_hub = hub_of(hass)
    result = await ws_ok(ws, type=f"{DOMAIN}/pin", view_id=kitchen_id)
    await hass.async_block_till_done()
    subentry_id = result["view_id"]

    subentry = home.entry.subentries[subentry_id]
    assert subentry.subentry_type == SUBENTRY_TYPE_VIEW
    assert subentry.title == "Kitchen WLED"
    assert subentry.unique_id == pinned_unique_id(home.kitchen.id)
    assert isinstance(subentry.data, MappingProxyType)
    assert dict(subentry.data) == {
        CONF_URL: KITCHEN_URL,
        CONF_MODE: MODE_ISOLATED,
        CONF_VERIFY_SSL: False,
        CONF_SHOW_IN_SIDEBAR: False,
        CONF_DEVICE_ID: home.kitchen.id,
    }
    # Applied in place: the entry was not reloaded
    assert home.entry.state is ConfigEntryState.LOADED
    assert hub_of(hass) is old_hub
    assert home.entry.runtime_data is old_hub

    views = await list_views(ws)
    assert kitchen_id not in views
    pinned = views[subentry_id]
    assert pinned["source"] == "static"
    assert pinned["name"] == "Kitchen WLED"
    assert pinned["url"] == KITCHEN_URL
    assert pinned["device_id"] == home.kitchen.id
    assert pinned["device_link"] == link_state(enabled=True, active=True)
    # The device's "Visit" link follows the view to its new id
    assert url_of(hass, home.kitchen.id) == link_for(subentry_id)
    # Other discovered views are untouched
    assert f"d_{home.porch.id}" in views

    # The old id is gone for sessions too, the new one works
    error = await ws_error(ws, type=f"{DOMAIN}/session", view_id=kitchen_id)
    assert error["code"] == "not_found"
    session = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=subentry_id)
    assert session["url"] == f"/api/local_web_ui/{subentry_id}/{session['token']}/settings?page=1"

    # Pinning again: the discovered view no longer exists
    error = await ws_error(ws, type=f"{DOMAIN}/pin", view_id=kitchen_id)
    assert error["code"] == "not_found"
    assert len(home.entry.subentries) == 3


async def test_pinned_view_is_listed_as_soon_as_pin_returns(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    """The panel lists views right after pinning; nothing may still be pending."""
    kitchen_id = f"d_{home.kitchen.id}"
    hub = hub_of(hass)
    # Sent back to back: the views request is handled right after the pin
    await ws.send_json_auto_id({"type": f"{DOMAIN}/pin", "view_id": kitchen_id})
    await ws.send_json_auto_id({"type": f"{DOMAIN}/views"})
    await ws.send_json_auto_id({"type": f"{DOMAIN}/session", "view_id": kitchen_id})
    pin = await ws.receive_json()
    views = await ws.receive_json()
    old_id_session = await ws.receive_json()
    assert pin["success"], pin
    assert views["success"], views
    subentry_id = pin["result"]["view_id"]

    listed = {view["view_id"]: view for view in views["result"]["views"]}
    assert kitchen_id not in listed
    assert listed[subentry_id]["source"] == "static"
    assert listed[subentry_id]["device_link"] == link_state(enabled=True, active=True)
    assert not old_id_session["success"]
    assert old_id_session["error"]["code"] == "not_found"
    assert url_of(hass, home.kitchen.id) == link_for(subentry_id)
    session = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=subentry_id)
    assert session["view"]["view_id"] == subentry_id

    await hass.async_block_till_done()
    assert hub_of(hass) is hub
    assert home.entry.runtime_data is hub
    assert set(await list_views(ws)) == set(listed)


async def test_pin_twice_adds_one_web_ui(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    kitchen_id = f"d_{home.kitchen.id}"
    # A double click: both requests arrive before either reply
    await ws.send_json_auto_id({"type": f"{DOMAIN}/pin", "view_id": kitchen_id})
    await ws.send_json_auto_id({"type": f"{DOMAIN}/pin", "view_id": kitchen_id})
    first = await ws.receive_json()
    second = await ws.receive_json()
    await hass.async_block_till_done()
    assert first["success"], first
    # The first pin replaced the discovered view at once, so there is nothing left
    # to pin (already_pinned is for a device still discovered; see the next test)
    assert not second["success"]
    assert second["error"]["code"] == "not_found", second
    pinned = [
        s
        for s in home.entry.subentries.values()
        if s.unique_id == pinned_unique_id(home.kitchen.id)
    ]
    assert [s.subentry_id for s in pinned] == [first["result"]["view_id"]]
    assert len(home.entry.subentries) == 3


async def test_pin_device_that_already_has_a_web_ui_is_already_pinned(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    """The device's web UI id is taken while its discovered view is still offered.

    Here a web UI holds the device's unique id without naming the device (as if
    added by an earlier version), so the device is still discovered.
    """
    kitchen_id = f"d_{home.kitchen.id}"
    hass.config_entries.async_add_subentry(
        home.entry,
        ConfigSubentry(
            data=MappingProxyType({CONF_URL: "http://192.168.1.99/", CONF_MODE: MODE_ISOLATED}),
            subentry_type=SUBENTRY_TYPE_VIEW,
            title="Kitchen (old)",
            unique_id=pinned_unique_id(home.kitchen.id),
        ),
    )
    await hass.async_block_till_done()
    assert kitchen_id in await list_views(ws)

    error = await ws_error(ws, type=f"{DOMAIN}/pin", view_id=kitchen_id)
    assert error["code"] == "already_pinned"
    await hass.async_block_till_done()
    assert len(home.entry.subentries) == 3
    assert [
        s.title
        for s in home.entry.subentries.values()
        if s.unique_id == pinned_unique_id(home.kitchen.id)
    ] == ["Kitchen (old)"]
    # Nothing else changed: the discovered view is still there and still linked
    views = await list_views(ws)
    assert views[kitchen_id]["source"] == "discovered"
    assert url_of(hass, home.kitchen.id) == link_for(kitchen_id)


@pytest.mark.parametrize("view_id", [ROUTER_ID, PRINTER_VIEW_ID, "nope", "d_nope", ""])
async def test_pin_static_or_unknown_view(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket, view_id: str
) -> None:
    error = await ws_error(ws, type=f"{DOMAIN}/pin", view_id=view_id)
    assert error["code"] == "not_found"
    await hass.async_block_till_done()
    assert set(home.entry.subentries) == {ROUTER_ID, PRINTER_VIEW_ID}


async def test_pin_with_discovery_off_is_not_found(
    hass: HomeAssistant,
    http: None,
    owner: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    home = await build_home(hass, owner, {CONF_DISCOVERY: False})
    ws = await hass_ws_client()
    error = await ws_error(ws, type=f"{DOMAIN}/pin", view_id=f"d_{home.porch.id}")
    assert error["code"] == "not_found"
    assert len(home.entry.subentries) == 2


# ---------------------------------------------------------------------------
# local_web_ui/set_hidden and local_web_ui/set_device_link
# ---------------------------------------------------------------------------


async def test_set_hidden_persists_without_reload(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    porch_id = f"d_{home.porch.id}"
    hub = hub_of(hass)
    assert await ws_ok(ws, type=f"{DOMAIN}/set_hidden", view_id=porch_id, hidden=True) is None
    await hass.async_block_till_done()
    assert hub_of(hass) is hub  # No reload
    assert porch_id in hub.hidden
    # Idempotent
    await ws_ok(ws, type=f"{DOMAIN}/set_hidden", view_id=porch_id, hidden=True)
    assert hub.hidden == {porch_id}
    await ws_ok(ws, type=f"{DOMAIN}/set_hidden", view_id=porch_id, hidden=False)
    await ws_ok(ws, type=f"{DOMAIN}/set_hidden", view_id=porch_id, hidden=False)
    assert hub.hidden == set()

    # Survives a reload
    await ws_ok(ws, type=f"{DOMAIN}/set_hidden", view_id=porch_id, hidden=True)
    assert await hass.config_entries.async_reload(home.entry.entry_id)
    await hass.async_block_till_done()
    views = await list_views(ws)
    assert views[porch_id]["hidden"] is True
    assert url_of(hass, home.porch.id) == PORCH_URL


@pytest.mark.parametrize("view_id", [ROUTER_ID, PRINTER_VIEW_ID, "nope", ""])
@pytest.mark.parametrize("hidden", [True, False])
async def test_set_hidden_only_accepts_discovered_views(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket, view_id: str, hidden: bool
) -> None:
    """Web UIs added by hand are removed, not hidden."""
    error = await ws_error(ws, type=f"{DOMAIN}/set_hidden", view_id=view_id, hidden=hidden)
    assert error["code"] == "not_found"
    await hass.async_block_till_done()
    assert hub_of(hass).hidden == set()
    views = await list_views(ws)
    assert views[PRINTER_VIEW_ID]["hidden"] is False
    assert views[PRINTER_VIEW_ID]["device_link"] == link_state(enabled=True, active=True)
    assert url_of(hass, home.printer.id) == link_for(PRINTER_VIEW_ID)


@pytest.mark.parametrize(
    "args",
    [
        {"view_id": "d_x"},
        {"hidden": True},
        {"view_id": "d_x", "hidden": "yes"},
        {"view_id": 5, "hidden": True},
    ],
)
async def test_set_hidden_validates_arguments(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket, args: dict[str, Any]
) -> None:
    error = await ws_error(ws, type=f"{DOMAIN}/set_hidden", **args)
    assert error["code"] == "invalid_format"
    assert hub_of(hass).hidden == set()


async def test_set_device_link_overrides_global_default(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    porch_id = f"d_{home.porch.id}"
    hub = hub_of(hass)
    assert url_of(hass, home.porch.id) == link_for(porch_id)

    await ws_ok(ws, type=f"{DOMAIN}/set_device_link", device_id=home.porch.id, enabled=False)
    assert hub_of(hass) is hub  # No reload
    assert hub.link_overrides == {home.porch.id: False}
    assert url_of(hass, home.porch.id) == PORCH_URL
    views = await list_views(ws)
    assert views[porch_id]["device_link"] == link_state(enabled=False, active=False, override=False)
    # Only that device
    assert views[f"d_{home.kitchen.id}"]["device_link"] == link_state(enabled=True, active=True)
    assert url_of(hass, home.kitchen.id) == link_for(f"d_{home.kitchen.id}")

    # Works for static views linked to a device as well
    await ws_ok(ws, type=f"{DOMAIN}/set_device_link", device_id=home.printer.id, enabled=False)
    assert url_of(hass, home.printer.id) == PRINTER_URL
    views = await list_views(ws)
    assert views[PRINTER_VIEW_ID]["device_link"] == link_state(
        enabled=False, active=False, override=False
    )

    # Explicitly on is an override too, even though it matches the global option
    await ws_ok(ws, type=f"{DOMAIN}/set_device_link", device_id=home.porch.id, enabled=True)
    assert hub.link_overrides == {home.porch.id: True, home.printer.id: False}
    assert url_of(hass, home.porch.id) == link_for(porch_id)

    # None goes back to following the global option
    await ws_ok(ws, type=f"{DOMAIN}/set_device_link", device_id=home.porch.id, enabled=None)
    await ws_ok(ws, type=f"{DOMAIN}/set_device_link", device_id=home.printer.id, enabled=None)
    assert hub.link_overrides == {}
    assert url_of(hass, home.porch.id) == link_for(porch_id)
    assert url_of(hass, home.printer.id) == link_for(PRINTER_VIEW_ID)
    views = await list_views(ws)
    assert views[porch_id]["device_link"] == link_state(enabled=True, active=True)
    assert views[PRINTER_VIEW_ID]["device_link"] == link_state(enabled=True, active=True)


async def test_set_device_link_with_global_default_off(
    hass: HomeAssistant,
    http: None,
    owner: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
) -> None:
    home = await build_home(hass, owner, {CONF_LINK_DEVICE_PAGES: False})
    ws = await hass_ws_client()
    porch_id = f"d_{home.porch.id}"
    assert url_of(hass, home.porch.id) == PORCH_URL

    await ws_ok(ws, type=f"{DOMAIN}/set_device_link", device_id=home.porch.id, enabled=True)
    assert hub_of(hass).link_overrides == {home.porch.id: True}
    assert url_of(hass, home.porch.id) == link_for(porch_id)
    views = await list_views(ws)
    assert views[porch_id]["device_link"] == link_state(enabled=True, active=True, override=True)
    assert views[f"d_{home.kitchen.id}"]["device_link"] == link_state(enabled=False, active=False)

    await ws_ok(ws, type=f"{DOMAIN}/set_device_link", device_id=home.porch.id, enabled=False)
    assert hub_of(hass).link_overrides == {home.porch.id: False}
    assert url_of(hass, home.porch.id) == PORCH_URL

    await ws_ok(ws, type=f"{DOMAIN}/set_device_link", device_id=home.porch.id, enabled=None)
    assert hub_of(hass).link_overrides == {}
    assert url_of(hass, home.porch.id) == PORCH_URL
    views = await list_views(ws)
    assert views[porch_id]["device_link"] == link_state(enabled=False, active=False)


@pytest.mark.parametrize("link_default", [True, False])
async def test_views_device_link_override_follows_set_device_link(
    hass: HomeAssistant,
    http: None,
    owner: MockConfigEntry,
    hass_ws_client: WebSocketGenerator,
    link_default: bool,
) -> None:
    home = await build_home(hass, owner, {CONF_LINK_DEVICE_PAGES: link_default})
    ws = await hass_ws_client()
    porch_id = f"d_{home.porch.id}"

    async def porch_link() -> dict[str, Any]:
        return (await list_views(ws))[porch_id]["device_link"]

    assert await porch_link() == link_state(enabled=link_default, active=link_default)
    for enabled in (True, False, None, False, True, None):
        await ws_ok(ws, type=f"{DOMAIN}/set_device_link", device_id=home.porch.id, enabled=enabled)
        effective = link_default if enabled is None else enabled
        assert await porch_link() == link_state(
            enabled=effective, active=effective, override=enabled
        )
        assert url_of(hass, home.porch.id) == (link_for(porch_id) if effective else PORCH_URL)
        # Other devices keep following the global option
        views = await list_views(ws)
        assert views[f"d_{home.kitchen.id}"]["device_link"] == link_state(
            enabled=link_default, active=link_default
        )


async def test_set_device_link_unknown_device_is_harmless(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    await ws_ok(ws, type=f"{DOMAIN}/set_device_link", device_id="no-such-device", enabled=False)
    views = await list_views(ws)
    assert views[f"d_{home.porch.id}"]["device_link"] == link_state(enabled=True, active=True)


@pytest.mark.parametrize("enabled", ["yes", 1, "null"])
async def test_set_device_link_validates_enabled(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket, enabled: Any
) -> None:
    error = await ws_error(
        ws, type=f"{DOMAIN}/set_device_link", device_id=home.porch.id, enabled=enabled
    )
    assert error["code"] == "invalid_format"
    assert hub_of(hass).link_overrides == {}


# ---------------------------------------------------------------------------
# admin only, not loaded
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("command", ALL_COMMANDS, ids=COMMAND_IDS)
async def test_commands_reject_non_admin(
    hass: HomeAssistant,
    home: Home,
    hass_ws_client: WebSocketGenerator,
    hass_read_only_access_token: str,
    command: dict[str, Any],
) -> None:
    ws = await hass_ws_client(access_token=hass_read_only_access_token)
    hub = hub_of(hass)
    error = await ws_error(ws, **command)
    assert error["code"] == "unauthorized"
    await hass.async_block_till_done()
    # Nothing changed
    assert hub_of(hass) is hub
    assert hub.hidden == set()
    assert hub.link_overrides == {}
    assert set(home.entry.subentries) == {ROUTER_ID, PRINTER_VIEW_ID}
    assert not hub.sessions._sessions


async def test_commands_reject_non_admin_user_with_own_group(
    hass: HomeAssistant,
    home: Home,
    hass_ws_client: WebSocketGenerator,
) -> None:
    """A regular (non-admin, non-read-only) user is rejected as well."""
    group = await hass.auth.async_get_group(GROUP_ID_USER)
    assert group is not None
    user = MockUser(name="plain", groups=[group]).add_to_hass(hass)
    credential = Credentials(
        id="mock-plain-credential-id",
        auth_provider_type="homeassistant",
        auth_provider_id=None,
        data={"username": "plain"},
        is_new=False,
    )
    user.credentials.append(credential)
    refresh_token = await hass.auth.async_create_refresh_token(
        user, CLIENT_ID, credential=credential
    )
    assert not user.is_admin
    ws = await hass_ws_client(access_token=hass.auth.async_create_access_token(refresh_token))
    for command in ALL_COMMANDS:
        error = await ws_error(ws, **dict(command))
        assert error["code"] == "unauthorized", command


@pytest.mark.parametrize("command", ALL_COMMANDS, ids=COMMAND_IDS)
async def test_commands_not_loaded(
    hass: HomeAssistant,
    home: Home,
    hass_ws_client: WebSocketGenerator,
    command: dict[str, Any],
) -> None:
    assert await hass.config_entries.async_unload(home.entry.entry_id)
    await hass.async_block_till_done()
    assert home.entry.state is ConfigEntryState.NOT_LOADED
    ws = await hass_ws_client()
    error = await ws_error(ws, **command)
    assert error["code"] == "not_loaded"


async def test_commands_work_again_after_reload(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    assert await hass.config_entries.async_unload(home.entry.entry_id)
    await hass.async_block_till_done()
    error = await ws_error(ws, type=f"{DOMAIN}/views")
    assert error["code"] == "not_loaded"
    assert await hass.config_entries.async_setup(home.entry.entry_id)
    await hass.async_block_till_done()
    views = await list_views(ws)
    assert ROUTER_ID in views


# ---------------------------------------------------------------------------
# diagnostics
# ---------------------------------------------------------------------------


async def test_diagnostics_redacts_credentials(
    hass: HomeAssistant,
    home: Home,
    hass_client: ClientSessionGenerator,
) -> None:
    porch_id = f"d_{home.porch.id}"
    hub = hub_of(hass)
    hub.async_set_hidden(porch_id, True)
    hub.async_set_device_link(home.kitchen.id, False)

    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, home.entry)
    dumped = json.dumps(diagnostics)
    assert "hunter2" not in dumped
    assert "rootuser" not in dumped

    static = {view["title"]: view for view in diagnostics["static_views"]}
    assert static["Router"][CONF_USERNAME] == "**REDACTED**"
    assert static["Router"][CONF_PASSWORD] == "**REDACTED**"
    # Query strings can hold tokens
    assert static["Router"][CONF_URL] == ROUTER_URL.partition("?")[0] + "?**REDACTED**"
    assert static["Router"][CONF_MODE] == MODE_TRUSTED
    assert CONF_USERNAME not in static["Office printer"]
    assert static["Office printer"][CONF_DEVICE_ID] == home.printer.id

    discovered = {view["view_id"]: view for view in diagnostics["discovered_views"]}
    assert set(discovered) == {porch_id, f"d_{home.kitchen.id}"}
    assert discovered[porch_id]["url"] == PORCH_URL
    assert diagnostics["hidden"] == [porch_id]
    assert diagnostics["link_overrides"] == {home.kitchen.id: False}
    # Only the devices whose "Visit" link currently points here: the porch is
    # hidden and the kitchen's link is off
    assert diagnostics["device_page_links"] == [home.printer.id]


async def test_diagnostics_never_contain_session_tokens_or_site_data(
    hass: HomeAssistant,
    home: Home,
    hass_client: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
    hass_admin_user: MockUser,
) -> None:
    ws = await hass_ws_client()
    session = await ws_ok(ws, type=f"{DOMAIN}/session", view_id=ROUTER_ID)
    hub = hub_of(hass)
    assert hub.async_apply_storage_write(
        hass_admin_user.id, PRINTER_VIEW_ID, "write-1", {"secret": "s3cr3t"}, False
    )
    router = hub.get_view(ROUTER_ID)
    assert router is not None
    hub.async_store_cookies(
        hass_admin_user.id, router, ["sid=c00kie-value; HttpOnly"], router.origin
    )
    diagnostics = await get_diagnostics_for_config_entry(hass, hass_client, home.entry)
    dumped = json.dumps(diagnostics)
    assert session["token"] not in dumped
    assert "s3cr3t" not in dumped
    assert "c00kie-value" not in dumped
    assert "write-1" not in dumped


# ---------------------------------------------------------------------------
# local_web_ui/clear_site_data (end to end through the proxy)
# ---------------------------------------------------------------------------
# The relay fixture and the fake device are defined with the WebSocket tests below.


def prefix_of(url: str) -> str:
    """The proxy prefix (/api/local_web_ui/<view>/<token>) of a session URL."""
    match = TOKEN_URL.match(url)
    assert match is not None
    return f"/api/local_web_ui/{match['view']}/{match['token']}"


@pytest.mark.parametrize("view_id", ["echo", "echo_trusted"])
async def test_clear_site_data_forgets_cookies_and_storage(
    relay: Relay, hass_admin_user: MockUser, view_id: str
) -> None:
    """Site cookies live server side in both modes, so both can be cleared."""
    hub = hub_of(relay.hass)
    user_id = hass_admin_user.id
    other_view = "echo_trusted" if view_id == "echo" else "echo"
    prefixes = {v: prefix_of(await relay.session_url(v)) for v in (view_id, other_view)}
    for prefix in prefixes.values():
        response = await relay.client.get(prefix + "/login")
        assert response.status == 200
        # The browser never gets the device's cookie
        assert "Set-Cookie" not in response.headers
    assert await relay.whoami(prefixes[view_id]) == "sid=device-session"
    isolated = view_id == "echo"
    if isolated:
        response = await relay.client.post(
            prefixes[view_id] + "/__lwu/storage",
            json={"w": "w1", "set": {"theme": "dark"}, "clear": False},
        )
        assert response.status == 204
        assert hub.shim_storage(user_id, view_id) == {"theme": "dark"}
    # Another user's cookies for the same web UI
    view = hub.get_view(view_id)
    assert view is not None
    hub.async_store_cookies("someone-else", view, ["sid=theirs"], view.origin)

    assert await ws_ok(relay.ws, type=f"{DOMAIN}/clear_site_data", view_id=view_id) is None

    assert await relay.whoami(prefixes[view_id]) is None
    assert hub.shim_storage(user_id, view_id) == {}
    assert hub.applied_writes(user_id, view_id) == []
    # The script injected into the next page sees no cookies either
    assert not list(hub.cookie_jar(user_id, view))
    # Only this user and this web UI
    assert await relay.whoami(prefixes[other_view]) == "sid=device-session"
    assert [m.value for m in hub.cookie_jar("someone-else", view)] == ["theirs"]
    # The session itself stays valid
    assert (await relay.client.get(prefixes[view_id] + "/whoami")).status == 200


async def test_clear_site_data_unknown_view_is_harmless(
    hass: HomeAssistant, home: Home, ws: MockHAClientWebSocket
) -> None:
    assert await ws_ok(ws, type=f"{DOMAIN}/clear_site_data", view_id="nope") is None


# ---------------------------------------------------------------------------
# WebSocket relaying through the proxy
# ---------------------------------------------------------------------------


@dataclass
class Upstream:
    """A fake device UI with a WebSocket echo endpoint."""

    server: TestServer
    connected: asyncio.Event = field(default_factory=asyncio.Event)
    closed: asyncio.Event = field(default_factory=asyncio.Event)
    requests: list[dict[str, Any]] = field(default_factory=list)
    close_codes: list[int | None] = field(default_factory=list)
    open_sockets: int = 0

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.port}/"


UPSTREAM_KEY: web.AppKey[Upstream] = web.AppKey("upstream", Upstream)


async def _ws_echo(request: web.Request) -> web.StreamResponse:
    upstream = request.app[UPSTREAM_KEY]
    ws = web.WebSocketResponse(protocols=("chat", "superchat"))
    await ws.prepare(request)
    upstream.requests.append(
        {"headers": dict(request.headers), "path_qs": request.path_qs, "protocol": ws.ws_protocol}
    )
    upstream.connected.set()
    upstream.open_sockets += 1
    try:
        while True:
            msg = await ws.receive()
            if msg.type is WSMsgType.TEXT:
                if msg.data.startswith("close:"):
                    await ws.close(code=int(msg.data[6:]), message=b"bye")
                    upstream.close_codes.append(ws.close_code)
                    break
                if msg.data == "drop":
                    assert request.transport is not None
                    request.transport.abort()
                    break
                if msg.data == "protocol?":
                    await ws.send_str(str(ws.ws_protocol))
                    continue
                await ws.send_str(msg.data)
            elif msg.type is WSMsgType.BINARY:
                await ws.send_bytes(msg.data)
            else:
                if msg.type is WSMsgType.CLOSE:
                    upstream.close_codes.append(msg.data)
                break
    finally:
        upstream.open_sockets -= 1
    upstream.closed.set()
    return ws


async def _not_websocket(request: web.Request) -> web.Response:
    return web.Response(text="no websockets here")


async def _forbidden(request: web.Request) -> web.Response:
    return web.Response(status=403, text="go away")


async def _login(request: web.Request) -> web.Response:
    response = web.Response(text="welcome")
    response.set_cookie("sid", "device-session", path="/", httponly=True)
    return response


async def _whoami(request: web.Request) -> web.Response:
    return web.json_response(
        {
            "cookie": request.headers.get("Cookie"),
            "authorization": request.headers.get("Authorization"),
        }
    )


@pytest.fixture
async def upstream(socket_enabled: None) -> AsyncGenerator[Upstream]:
    app = web.Application()
    app.router.add_get("/ws", _ws_echo)
    app.router.add_get("/deep/path/ws", _ws_echo)
    app.router.add_get("/plain", _not_websocket)
    app.router.add_get("/forbidden", _forbidden)
    app.router.add_get("/login", _login)
    app.router.add_get("/whoami", _whoami)
    server = TestServer(app, host="127.0.0.1")
    fake = Upstream(server)
    app[UPSTREAM_KEY] = fake
    await server.start_server()
    yield fake
    await server.close()


@dataclass
class Relay:
    hass: HomeAssistant
    entry: MockConfigEntry
    ws: MockHAClientWebSocket
    client: TestClient
    upstream: Upstream

    async def session_url(self, view_id: str = "echo") -> str:
        result = await ws_ok(self.ws, type=f"{DOMAIN}/session", view_id=view_id)
        return result["url"]

    async def whoami(self, prefix: str) -> str | None:
        """The Cookie header the device gets for a request through the proxy."""
        response = await self.client.get(prefix + "/whoami", headers=BROWSER_CREDENTIALS)
        assert response.status == 200
        return (await response.json())["cookie"]


@pytest.fixture
async def relay(
    hass: HomeAssistant,
    http: None,
    upstream: Upstream,
    hass_ws_client: WebSocketGenerator,
    hass_client_no_auth: ClientSessionGenerator,
) -> AsyncGenerator[Relay]:
    entry = await setup_lwu(
        hass,
        subentries=[
            view_subentry("echo", upstream.url, "Echo"),
            view_subentry(
                "echo_trusted",
                upstream.url + "deep/path/",
                "Echo trusted",
                **{CONF_MODE: MODE_TRUSTED, CONF_USERNAME: "u", CONF_PASSWORD: "p"},
            ),
        ],
    )
    ws = await hass_ws_client()
    client = await hass_client_no_auth()
    yield Relay(hass, entry, ws, client, upstream)
    if entry.state is ConfigEntryState.LOADED:
        # Closes the upstream client sessions
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


async def _receive(ws: aiohttp.ClientWebSocketResponse) -> aiohttp.WSMessage:
    return await asyncio.wait_for(ws.receive(), timeout=5)


async def test_ws_relay_echoes_text_and_binary(relay: Relay) -> None:
    url = await relay.session_url()
    async with relay.client.ws_connect(url + "ws") as ws:
        await ws.send_str("hello")
        msg = await _receive(ws)
        assert (msg.type, msg.data) == (WSMsgType.TEXT, "hello")

        payload = bytes(range(256)) * 4
        await ws.send_bytes(payload)
        msg = await _receive(ws)
        assert (msg.type, msg.data) == (WSMsgType.BINARY, payload)

        # Unicode and a larger message, in order
        await ws.send_str("héllo ✓")
        await ws.send_str("x" * 100_000)
        assert (await _receive(ws)).data == "héllo ✓"
        assert (await _receive(ws)).data == "x" * 100_000
        assert ws.protocol is None

    assert len(relay.upstream.requests) == 1
    request = relay.upstream.requests[0]
    assert request["path_qs"] == "/ws"
    assert request["protocol"] is None
    headers = {k.lower(): v for k, v in request["headers"].items()}
    # The token-bearing referer and HA's origin do not reach the device
    assert "origin" not in headers
    assert "referer" not in headers
    assert headers["x-ingress-path"] == url.rstrip("/")


async def test_ws_relay_keeps_query_and_forwards_credentials(relay: Relay) -> None:
    """A trusted view's own login is sent; the browser's credentials are not."""
    url = await relay.session_url("echo_trusted")
    assert url.endswith("/deep/path/")
    async with relay.client.ws_connect(
        url + "ws?x=1&y=two", headers={"Origin": "https://ha.example", **BROWSER_CREDENTIALS}
    ) as ws:
        await ws.send_str("ping")
        assert (await _receive(ws)).data == "ping"
    request = relay.upstream.requests[0]
    assert request["path_qs"] == "/deep/path/ws?x=1&y=two"
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert headers["authorization"] == "Basic dTpw"
    assert "origin" not in headers
    assert "cookie" not in headers
    assert "browser-secret" not in json.dumps(request["headers"])


@pytest.mark.parametrize("view_id", ["echo", "echo_trusted"])
async def test_ws_relay_sends_site_cookies_not_browser_credentials(
    relay: Relay, view_id: str
) -> None:
    """The handshake carries the site's server-side cookies instead of the browser's."""
    url = await relay.session_url(view_id)
    prefix = prefix_of(url)
    assert (await relay.client.get(prefix + "/login")).status == 200
    async with relay.client.ws_connect(prefix + "/ws", headers=BROWSER_CREDENTIALS) as ws:
        await ws.send_str("hi")
        assert (await _receive(ws)).data == "hi"
    headers = {k.lower(): v for k, v in relay.upstream.requests[0]["headers"].items()}
    assert headers["cookie"] == "sid=device-session"
    assert headers.get("authorization") == ("Basic dTpw" if view_id == "echo_trusted" else None)
    assert "browser-secret" not in json.dumps(headers)
    assert "cmV2ZXJzZTpwcm94eQ==" not in json.dumps(headers)


@pytest.mark.parametrize(
    ("offered", "expected"),
    [
        (("superchat", "chat"), "superchat"),
        (("chat",), "chat"),
        (("mqtt", "chat"), "chat"),
    ],
)
async def test_ws_relay_negotiates_subprotocol(
    relay: Relay, offered: tuple[str, ...], expected: str
) -> None:
    url = await relay.session_url()
    async with relay.client.ws_connect(url + "ws", protocols=offered) as ws:
        # The browser gets what the device chose
        assert ws.protocol == expected
        await ws.send_str("protocol?")
        assert (await _receive(ws)).data == expected
    assert relay.upstream.requests[0]["protocol"] == expected
    offered_upstream = relay.upstream.requests[0]["headers"].get("Sec-WebSocket-Protocol", "")
    assert [p.strip() for p in offered_upstream.split(",")] == list(offered)


async def test_ws_relay_unsupported_subprotocol(relay: Relay) -> None:
    url = await relay.session_url()
    async with relay.client.ws_connect(url + "ws", protocols=("mqtt",)) as ws:
        assert ws.protocol is None
        await ws.send_str("still works")
        assert (await _receive(ws)).data == "still works"


async def test_ws_relay_browser_close_closes_upstream(relay: Relay) -> None:
    url = await relay.session_url()
    ws = await relay.client.ws_connect(url + "ws")
    await ws.send_str("hi")
    assert (await _receive(ws)).data == "hi"
    assert not relay.upstream.closed.is_set()

    await asyncio.wait_for(ws.close(code=4000, message=b"leaving"), timeout=5)
    assert ws.closed
    await asyncio.wait_for(relay.upstream.closed.wait(), timeout=5)
    assert relay.upstream.close_codes == [4000]


async def test_ws_relay_upstream_close_closes_browser(relay: Relay) -> None:
    url = await relay.session_url()
    ws = await relay.client.ws_connect(url + "ws")
    await ws.send_str("close:4001")
    msg = await _receive(ws)
    assert msg.type is WSMsgType.CLOSE
    assert msg.data == 4001
    assert ws.closed
    assert ws.close_code == 4001
    await asyncio.wait_for(relay.upstream.closed.wait(), timeout=5)


async def test_ws_relay_upstream_normal_close(relay: Relay) -> None:
    url = await relay.session_url()
    ws = await relay.client.ws_connect(url + "ws")
    await ws.send_str("close:1000")
    msg = await _receive(ws)
    assert msg.type is WSMsgType.CLOSE
    assert ws.close_code == 1000
    await asyncio.wait_for(relay.upstream.closed.wait(), timeout=5)


async def test_ws_relay_upstream_drops_connection(relay: Relay) -> None:
    """The device dropping off the network (no close frame) ends the browser's socket."""
    url = await relay.session_url()
    ws = await relay.client.ws_connect(url + "ws")
    await ws.send_str("hi")
    assert (await _receive(ws)).data == "hi"
    await ws.send_str("drop")
    msg = await _receive(ws)
    assert msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR)
    await asyncio.wait_for(ws.close(), timeout=5)
    assert ws.closed


async def test_ws_relay_browser_drops_connection(relay: Relay) -> None:
    """The browser going away (no close frame) closes the device's socket."""
    url = await relay.session_url()
    ws = await relay.client.ws_connect(url + "ws")
    await ws.send_str("hi")
    assert (await _receive(ws)).data == "hi"
    ws._conn.transport.abort()
    await asyncio.wait_for(relay.upstream.closed.wait(), timeout=5)


# RFC 6455 7.4.1: these only describe what happened locally and MUST NOT be
# sent in a Close frame. Browsers fail the connection when they receive one.
RESERVED_CLOSE_CODES = {1005, 1006, 1015}


@pytest.mark.parametrize("dropped_by", ["upstream", "browser"])
async def test_ws_relay_abnormal_closure_is_relayed_as_going_away(
    relay: Relay, dropped_by: str
) -> None:
    url = await relay.session_url()
    ws = await relay.client.ws_connect(url + "ws")
    await ws.send_str("hi")
    assert (await _receive(ws)).data == "hi"
    if dropped_by == "upstream":
        await ws.send_str("drop")
        msg = await _receive(ws)
        # A CLOSE message (rather than CLOSED/ERROR) means a Close frame arrived
        assert msg.type is WSMsgType.CLOSE
        assert msg.data not in RESERVED_CLOSE_CODES
        assert msg.data == aiohttp.WSCloseCode.GOING_AWAY
    else:
        ws._conn.transport.abort()
        await asyncio.wait_for(relay.upstream.closed.wait(), timeout=5)
        # The upstream records codes of Close frames it actually received
        assert not set(relay.upstream.close_codes) & RESERVED_CLOSE_CODES
        assert relay.upstream.close_codes == [aiohttp.WSCloseCode.GOING_AWAY]


async def test_ws_relay_forwards_ping_and_pong(relay: Relay) -> None:
    url = await relay.session_url()
    async with relay.client.ws_connect(url + "ws", autoping=False) as ws:
        await ws.ping(b"are you there")
        msg = await _receive(ws)
        assert (msg.type, msg.data) == (WSMsgType.PONG, b"are you there")
        await ws.send_str("still here")
        assert (await _receive(ws)).data == "still here"


@pytest.mark.parametrize("path", ["plain", "forbidden", "missing"])
async def test_ws_relay_upstream_refuses_upgrade(relay: Relay, path: str) -> None:
    url = await relay.session_url()
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await relay.client.ws_connect(url + path)
    assert err.value.status == 502

    # The same, looked at as a plain HTTP exchange: no 101, no accepted socket
    response = await relay.client.get(
        url + path,
        headers={
            "Connection": "Upgrade",
            "Upgrade": "websocket",
            "Sec-WebSocket-Version": "13",
            "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ==",
        },
    )
    assert response.status == 502
    assert "Echo" in await response.text()
    assert not relay.upstream.connected.is_set()


async def test_ws_relay_upstream_unreachable(relay: Relay) -> None:
    await relay.upstream.server.close()
    url = await relay.session_url()
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await relay.client.ws_connect(url + "ws")
    assert err.value.status == 502


async def test_ws_relay_requires_valid_session(relay: Relay) -> None:
    url = await relay.session_url()
    match = TOKEN_URL.match(url)
    assert match is not None
    bad = f"/api/local_web_ui/echo/{'A' * 43}/ws"
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await relay.client.ws_connect(bad)
    assert err.value.status == 404
    # A token only opens the view it was made for
    other = f"/api/local_web_ui/echo_trusted/{match['token']}/ws"
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await relay.client.ws_connect(other)
    assert err.value.status == 404
    assert not relay.upstream.connected.is_set()


# ---------------------------------------------------------------------------
# WebSockets end with their session
# ---------------------------------------------------------------------------


async def _assert_closed_by_proxy(ws: aiohttp.ClientWebSocketResponse, upstream: Upstream) -> None:
    """The proxy closed both the browser's and the device's socket."""
    msg = await _receive(ws)
    assert msg.type is WSMsgType.CLOSE, msg
    assert msg.data not in RESERVED_CLOSE_CODES
    await asyncio.wait_for(upstream.closed.wait(), timeout=5)
    await wait_until(lambda: upstream.open_sockets == 0)


async def test_ws_relay_closed_when_login_is_revoked(
    relay: Relay, hass_ws_client: WebSocketGenerator
) -> None:
    hass = relay.hass
    other = await create_admin_login(hass, "other")
    other_ws = await hass_ws_client(access_token=other.access_token)
    theirs = (await ws_ok(other_ws, type=f"{DOMAIN}/session", view_id="echo"))["url"]
    await other_ws.close()
    mine = await relay.session_url()

    ws = await relay.client.ws_connect(theirs + "ws")
    await ws.send_str("hi")
    assert (await _receive(ws)).data == "hi"

    hass.auth.async_remove_refresh_token(other.refresh_token)
    await _assert_closed_by_proxy(ws, relay.upstream)
    await ws.close()

    # The session is gone for plain requests and new WebSockets alike
    assert (await relay.client.get(prefix_of(theirs) + "/whoami")).status == 404
    with pytest.raises(aiohttp.WSServerHandshakeError) as err:
        await relay.client.ws_connect(theirs + "ws")
    assert err.value.status == 404
    # Sessions of other logins keep working
    async with relay.client.ws_connect(mine + "ws") as ws:
        await ws.send_str("still here")
        assert (await _receive(ws)).data == "still here"


@pytest.mark.parametrize(
    "noticed_by",
    [
        "proxy_request",
        "keepalive",
        "new_session",
        "timer",
    ],
)
async def test_ws_relay_closed_when_session_expires(
    relay: Relay, clock: FakeClock, noticed_by: str
) -> None:
    hass = relay.hass
    result = await ws_ok(relay.ws, type=f"{DOMAIN}/session", view_id="echo")
    url, token = result["url"], result["token"]
    ws = await relay.client.ws_connect(url + "ws")
    try:
        await ws.send_str("hi")
        assert (await _receive(ws)).data == "hi"

        # An open WebSocket does not keep its session alive on its own
        clock.now += SESSION_TTL + 1
        if noticed_by == "proxy_request":
            assert (await relay.client.get(prefix_of(url) + "/whoami")).status == 404
        elif noticed_by == "keepalive":
            again = await ws_ok(relay.ws, type=f"{DOMAIN}/session", view_id="echo", token=token)
            assert again["token"] != token
        elif noticed_by == "new_session":
            await relay.session_url("echo_trusted")
        else:
            async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=31))
            await hass.async_block_till_done()
        await _assert_closed_by_proxy(ws, relay.upstream)
        assert hub_of(hass).sessions.get(token) is None
    finally:
        await ws.close()


async def test_ws_relay_session_socket_limit(relay: Relay) -> None:
    url = await relay.session_url()
    token = prefix_of(url).rsplit("/", 1)[1]
    sessions = hub_of(relay.hass).sessions
    sockets = [await relay.client.ws_connect(url + "ws") for _ in range(MAX_WEBSOCKETS_PER_SESSION)]
    try:
        for index, ws in enumerate(sockets):
            await ws.send_str(f"socket {index}")
            assert (await _receive(ws)).data == f"socket {index}"

        with pytest.raises(aiohttp.WSServerHandshakeError) as err:
            await relay.client.ws_connect(url + "ws")
        assert err.value.status == 503
        # Refused before connecting to the device
        assert len(relay.upstream.requests) == MAX_WEBSOCKETS_PER_SESSION

        # The limit is per session
        async with relay.client.ws_connect(await relay.session_url() + "ws") as other:
            await other.send_str("other session")
            assert (await _receive(other)).data == "other session"

        # Closing one makes room for another
        await sockets.pop().close()
        session = sessions.get(token)
        assert session is not None
        await wait_until(lambda: len(session.websockets) < MAX_WEBSOCKETS_PER_SESSION)
        sockets.append(await relay.client.ws_connect(url + "ws"))
        await sockets[-1].send_str("replacement")
        assert (await _receive(sockets[-1])).data == "replacement"
    finally:
        for ws in sockets:
            await ws.close()


async def test_ws_relay_session_socket_limit_holds_for_simultaneous_opens(
    relay: Relay,
) -> None:
    url = await relay.session_url()
    attempts = MAX_WEBSOCKETS_PER_SESSION + 4
    results = await asyncio.gather(
        *(relay.client.ws_connect(url + "ws") for _ in range(attempts)),
        return_exceptions=True,
    )
    opened = [r for r in results if isinstance(r, aiohttp.ClientWebSocketResponse)]
    refused = [r for r in results if isinstance(r, aiohttp.WSServerHandshakeError)]
    try:
        assert len(opened) + len(refused) == attempts
        assert {err.status for err in refused} <= {503}
        assert len(opened) == MAX_WEBSOCKETS_PER_SESSION
    finally:
        for ws in opened:
            await ws.close()
