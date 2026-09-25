"""HTTP proxy of Local Web UIs, end to end through Home Assistant's http server.

A fake device UI (an aiohttp app on 127.0.0.1) stands behind static views. A
session is created over the websocket API as an admin, and the returned URL is
then requested without any Home Assistant authentication, like an iframe does.
"""

from __future__ import annotations

import asyncio
import base64
from collections.abc import AsyncGenerator, Awaitable, Callable
from dataclasses import dataclass
import json
from pathlib import PurePosixPath
import random
import re
import socket
import time
from typing import Any

import aiohttp
from aiohttp import hdrs, web
from aiohttp.abc import AbstractResolver, ResolveResult
from aiohttp.test_utils import TestClient, TestServer
from homeassistant.auth.const import GROUP_ID_ADMIN, GROUP_ID_USER
from homeassistant.auth.models import RefreshToken
from homeassistant.config_entries import ConfigEntryState, ConfigSubentry
from homeassistant.core import HomeAssistant
from homeassistant.helpers import aiohttp_client, device_registry as dr
from homeassistant.setup import async_setup_component
from multidict import CIMultiDict
import pytest
from pytest_homeassistant_custom_component.common import CLIENT_ID, MockConfigEntry, MockUser
from pytest_homeassistant_custom_component.typing import (
    ClientSessionGenerator,
    MockHAClientWebSocket,
    WebSocketGenerator,
)
from yarl import URL

from custom_components.local_web_ui import hub as hub_module, proxy as proxy_module
from custom_components.local_web_ui.const import (
    CONF_DISCOVERY,
    CONF_LINK_DEVICE_PAGES,
    CONF_MODE,
    CONF_PASSWORD,
    CONF_SHOW_IN_SIDEBAR,
    CONF_TRUSTED_ACK,
    CONF_URL,
    CONF_USERNAME,
    CONF_VERIFY_SSL,
    DISCOVERED_PREFIX,
    DOMAIN,
    MAX_REQUESTS_PER_SITE,
    MAX_SHIM_STORAGE_BYTES,
    MAX_SHIM_STORAGE_KEYS,
    MODE_ISOLATED,
    MODE_TRUSTED,
    NAME,
    PROXY_URL_PREFIX,
    SESSION_MAX_AGE,
    SESSION_TTL,
    STORAGE_KEY_JAR,
    SUBENTRY_TYPE_VIEW,
)
from custom_components.local_web_ui.hub import LocalWebUiHub
from custom_components.local_web_ui.proxy import ISOLATION_CSP, https_upgrade

ISO = "isoview"
TRUSTED = "trustedview"
AUTH = "authview"
DOWN = "downview"

SHIM_MARKER = b"<script>/* local_web_ui */("
BIG = random.Random(1234).randbytes(5 * 1024 * 1024 + 123)
# Larger than what the proxy buffers: links in the first 4 MiB are rewritten
BIG_PAGE_TAIL = b'<a href="/late">l</a></body></html>'
BIG_PAGE = b'<html><head></head><body><a href="/early">e</a>' + BIG + BIG_PAGE_TAIL
CSS_BODY = 'body{background:url(/img/bg.png)} @import "/more.css";'
LAST_MODIFIED = "Wed, 21 Oct 2015 07:28:00 GMT"
# Set on every response the proxy sends, streamed or not, errors included
SECURITY_HEADERS = {"X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer"}
# The upstream test app removes Content-Type from responses carrying this header
STRIP_CONTENT_TYPE = "X-Test-Strip-Content-Type"


# ---- fake device UI -----------------------------------------------------------


@dataclass
class Recorded:
    """A request that reached the fake device UI."""

    method: str
    target: str  # raw path and query, exactly as received
    headers: CIMultiDict[str]
    body: bytes


class Upstream:
    """A fake local web UI that records what reaches it."""

    def __init__(self) -> None:
        self.requests: list[Recorded] = []
        self.redirects: dict[str, str] = {}
        self.sse_release = asyncio.Event()
        self.sse_done = False
        # /hold?id=<id> answers once release(<id>) is called
        self.held: list[str] = []
        self._held_changed = asyncio.Condition()
        self._holds: dict[str, asyncio.Event] = {}
        self._released_all = False
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self._handle)
        app.on_response_prepare.append(self._strip_content_type)
        self.server = TestServer(app, host="127.0.0.1")

    @property
    def port(self) -> int:
        assert self.server.port is not None
        return self.server.port

    @property
    def origin(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    @property
    def last(self) -> Recorded:
        return self.requests[-1]

    async def wait_held(self, predicate: Callable[[list[str]], bool]) -> None:
        """Wait until the ids of the requests held so far satisfy predicate."""
        async with self._held_changed:
            await self._held_changed.wait_for(lambda: predicate(self.held))

    def release(self, hold_id: str) -> None:
        self._holds.setdefault(hold_id, asyncio.Event()).set()

    def release_all(self) -> None:
        self._released_all = True
        self.sse_release.set()
        for event in self._holds.values():
            event.set()

    def page_html(self) -> str:
        return (
            "<!DOCTYPE html>\n"
            '<html lang="en"><head><meta charset="utf-8"><title>Fake device</title>'
            '<link rel="stylesheet" href="/style.css"></head><body>'
            '<a id="root" href="/settings?x=1">root</a>'
            f'<img id="abs" src="{self.origin}/img/logo.png">'
            f"<script id=\"proto\" src='//127.0.0.1:{self.port}/app.js'></script>"
            '<form id="form" action="/submit" method="post">'
            '<button formaction="/alt">go</button></form>'
            '<video poster="/poster.jpg"></video>'
            '<a id="other" href="http://example.com/x">other</a>'
            '<a id="otherproto" href="//example.com/y">otherproto</a>'
            '<a id="rel" href="relative/page">rel</a>'
            '<a id="otherport" href="http://127.0.0.1:1/z">otherport</a>'
            "</body></html>"
        )

    @staticmethod
    async def _strip_content_type(_request: web.Request, response: web.StreamResponse) -> None:
        # Runs after aiohttp added its default Content-Type
        if response.headers.pop(STRIP_CONTENT_TYPE, None):
            response.headers.popall(hdrs.CONTENT_TYPE, None)

    async def _chunked(
        self, request: web.Request, body: bytes, content_type: str
    ) -> web.StreamResponse:
        response = web.StreamResponse()
        response.content_type = content_type
        response.enable_chunked_encoding()
        await response.prepare(request)
        for start in range(0, len(body), 65536):
            await response.write(body[start : start + 65536])
        await response.write_eof()
        return response

    async def _handle(self, request: web.Request) -> web.StreamResponse:
        body = await request.read()
        self.requests.append(
            Recorded(request.method, request.raw_path, request.headers.copy(), body)
        )
        path = request.path
        if path == "/page":
            return web.Response(text=self.page_html(), content_type="text/html")
        if path == "/page-cached":
            return web.Response(
                text="<html><head></head><body>cached</body></html>",
                content_type="text/html",
                headers={
                    hdrs.ETAG: '"page-v1"',
                    hdrs.LAST_MODIFIED: LAST_MODIFIED,
                    hdrs.CACHE_CONTROL: "public, max-age=3600",
                },
            )
        if path == "/meta-referrer":
            return web.Response(
                text=(
                    "<html><head>"
                    '<meta name="referrer" content="unsafe-url">'
                    "<META NAME='Referrer' CONTENT='origin'>"
                    '<meta content="no-referrer-when-downgrade" name=referrer>'
                    '<meta name="description" content="referrer">'
                    "</head><body>referrer</body></html>"
                ),
                content_type="text/html",
            )
        if path == "/referrer-attributes":
            return web.Response(
                text=(
                    "<html><head></head><body>"
                    '<a href="http://example.com/" referrerpolicy="unsafe-url">out</a>'
                    '<img src="http://example.com/x.png" '
                    "referrerpolicy='no-referrer-when-downgrade'>"
                    "</body></html>"
                ),
                content_type="text/html",
            )
        if path == "/style.css":
            return web.Response(text=CSS_BODY, content_type="text/css")
        if path == "/css-chunked":
            return await self._chunked(request, CSS_BODY.encode(), "text/css")
        if path.startswith("/typed/"):
            suffix = PurePosixPath(path).suffix.lower()
            text = {".css": CSS_BODY, ".js": "console.log(1);", ".mjs": "export {};"}
            response = web.Response(body=text.get(suffix, "{}").encode())
            if (content_type := request.query.get("ct")) is None:
                response.headers[STRIP_CONTENT_TYPE] = "1"
            else:
                response.headers[hdrs.CONTENT_TYPE] = content_type
            return response
        if path == "/login":
            response = web.Response(text="logged in")
            domain = "; Domain=127.0.0.1" if request.query.get("domain") else ""
            response.headers.add(hdrs.SET_COOKIE, f"sid=abc123{domain}; Path=/; HttpOnly")
            response.headers.add(hdrs.SET_COOKIE, "theme=dark; Path=/")
            return response
        if path.startswith("/redirect/"):
            return web.Response(
                status=302, headers={hdrs.LOCATION: self.redirects[path.rsplit("/", 1)[1]]}
            )
        if path == "/headers":
            return web.Response(
                text="headers",
                headers={
                    "X-Frame-Options": "DENY",
                    "Content-Security-Policy": "default-src 'none'",
                    "Content-Security-Policy-Report-Only": "default-src 'none'",
                    "Referrer-Policy": "unsafe-url",
                    "X-Content-Type-Options": "sniff",
                    hdrs.ACCESS_CONTROL_ALLOW_ORIGIN: "https://evil.example",
                    hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS: "true",
                    hdrs.ACCESS_CONTROL_EXPOSE_HEADERS: "*",
                    "Clear-Site-Data": '"*"',
                    "Strict-Transport-Security": "max-age=31536000",
                    "Service-Worker-Allowed": "/",
                    "Link": "</evil.js>; rel=preload",
                    "Permissions-Policy": "camera=*",
                    "Report-To": '{"group":"x"}',
                    "Refresh": "0; url=/elsewhere",
                    "X-XSS-Protection": "0",
                    "X-DNS-Prefetch-Control": "on",
                    "X-Permitted-Cross-Domain-Policies": "all",
                    "X-Device": "kept",
                    "Content-Language": "de",
                    "Content-Disposition": 'inline; filename="x.txt"',
                    "Retry-After": "5",
                },
            )
        if path == "/proxy-control":
            return web.Response(
                text="proxy control",
                headers={
                    "X-Accel-Redirect": "/internal/secret",
                    "X-Accel-Expires": "86400",
                    "X-Sendfile": "/etc/passwd",
                    "X-Accel-Buffering": "no",
                },
            )
        if path == "/cors":
            response = web.Response(
                text="cors",
                headers={
                    "X-Device": "kept",
                    "X-Frame-Options": "DENY",
                    "Content-Language": "de",
                    hdrs.VARY: "Accept-Encoding",
                },
            )
            response.headers.add(hdrs.SET_COOKIE, "c=1; Path=/")
            return response
        if path == "/cache":
            cache_control = request.query.get("cc")
            return web.Response(
                text="cache", headers={hdrs.CACHE_CONTROL: cache_control} if cache_control else {}
            )
        if path == "/nocontent":
            return web.Response(status=204, headers={"X-Device": "kept"})
        if path == "/cached":
            validators = {hdrs.ETAG: '"v1"', hdrs.LAST_MODIFIED: LAST_MODIFIED}
            if (
                request.headers.get(hdrs.IF_NONE_MATCH) == '"v1"'
                or request.headers.get(hdrs.IF_MODIFIED_SINCE) == LAST_MODIFIED
            ):
                return web.Response(status=304, headers=validators)
            return web.Response(text="fresh", headers=validators)
        if path == "/big":
            return web.Response(body=BIG, content_type="application/octet-stream")
        if path == "/big-html":
            return web.Response(body=BIG_PAGE, content_type="text/html")
        if path == "/big-html-chunked":
            return await self._chunked(request, BIG_PAGE, "text/html")
        if path == "/big-chunked":
            return await self._chunked(request, BIG, "application/octet-stream")
        if path == "/events":
            response = web.StreamResponse(headers={hdrs.CACHE_CONTROL: "no-cache"})
            response.content_type = "text/event-stream"
            await response.prepare(request)
            await response.write(b"data: one\n\n")
            await asyncio.wait_for(self.sse_release.wait(), 5)
            await response.write(b"data: two\n\n")
            await response.write_eof()
            self.sse_done = True
            return response
        if path == "/hold":
            hold_id = request.query["id"]
            async with self._held_changed:
                self.held.append(hold_id)
                self._held_changed.notify_all()
            if not self._released_all:
                event = self._holds.setdefault(hold_id, asyncio.Event())
                await asyncio.wait_for(event.wait(), 10)
            return web.Response(text=f"held {hold_id}")
        return web.Response(text=f"echo {request.method} {request.raw_path}")


# ---- fixtures -----------------------------------------------------------------


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


def _view(view_id: str, title: str, url: str, mode: str = MODE_ISOLATED, **extra: Any) -> Any:
    return {
        "subentry_id": view_id,
        "subentry_type": SUBENTRY_TYPE_VIEW,
        "title": title,
        "unique_id": None,
        "data": {
            CONF_URL: url,
            CONF_MODE: mode,
            CONF_TRUSTED_ACK: mode == MODE_TRUSTED,
            CONF_VERIFY_SSL: True,
            CONF_SHOW_IN_SIDEBAR: False,
            **extra,
        },
    }


@pytest.fixture
async def upstream(socket_enabled: None) -> AsyncGenerator[Upstream]:
    fake = Upstream()
    await fake.server.start_server()
    yield fake
    fake.release_all()
    await fake.server.close()


@pytest.fixture
def http_config() -> dict[str, Any]:
    return {}


@dataclass
class Env:
    hass: HomeAssistant
    entry: MockConfigEntry
    client: TestClient
    ws: MockHAClientWebSocket
    upstream: Upstream

    @property
    def hub(self) -> LocalWebUiHub:
        return self.hass.data[DOMAIN]

    async def session(
        self, view_id: str, ws: MockHAClientWebSocket | None = None
    ) -> dict[str, Any]:
        ws = ws or self.ws
        await ws.send_json_auto_id({"type": f"{DOMAIN}/session", "view_id": view_id})
        msg = await ws.receive_json()
        assert msg["success"], msg
        return msg["result"]

    async def prefix(self, view_id: str, ws: MockHAClientWebSocket | None = None) -> str:
        """Open a session and return the proxy prefix of the view."""
        result = await self.session(view_id, ws)
        return f"{PROXY_URL_PREFIX}/{view_id}/{result['token']}"

    async def page_config(self, prefix: str) -> dict[str, Any]:
        """The injected configuration of a freshly loaded page."""
        response = await self.client.get(prefix + "/page")
        assert response.status == 200
        return shim_config(await response.read())

    async def post_storage(
        self, prefix: str, payload: Any, headers: dict[str, str] | None = None
    ) -> aiohttp.ClientResponse:
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        return await self.client.post(
            prefix + "/__lwu/storage",
            data=data,
            headers={hdrs.CONTENT_TYPE: "text/plain", **(headers or {})},
        )


@pytest.fixture
async def env(
    hass: HomeAssistant,
    upstream: Upstream,
    http_config: dict[str, Any],
    hass_client_no_auth: ClientSessionGenerator,
    hass_ws_client: WebSocketGenerator,
) -> AsyncGenerator[Env]:
    assert await async_setup_component(hass, "http", {"http": http_config})
    entry = MockConfigEntry(
        domain=DOMAIN,
        title=NAME,
        options={CONF_DISCOVERY: False, CONF_LINK_DEVICE_PAGES: False},
        subentries_data=[
            _view(ISO, "Isolated site", f"{upstream.origin}/page?start=1"),
            _view(TRUSTED, "Trusted site", f"{upstream.origin}/", MODE_TRUSTED),
            _view(
                AUTH,
                "Site with login",
                f"{upstream.origin}/",
                **{CONF_USERNAME: "admin", CONF_PASSWORD: "s3cret"},
            ),
            _view(DOWN, "Unplugged", f"http://127.0.0.1:{_free_port()}/"),
        ],
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    client = await hass_client_no_auth()
    ws = await hass_ws_client(hass)
    yield Env(hass, entry, client, ws, upstream)
    upstream.release_all()
    if entry.state is ConfigEntryState.LOADED:
        # Detaches (static views) or closes (discovered views) the upstream clients
        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()


def shim_config(body: bytes) -> dict[str, Any]:
    """The configuration object passed to the injected script."""
    start = body.index(SHIM_MARKER)
    end = body.index(b"</script>", start)
    script = body[start:end]
    assert script.endswith(b");")
    return json.loads(script[script.rindex(b")(") + 2 : -2])


def _basic(username: str, password: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{password}".encode()).decode()


async def _admin_login(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, name: str
) -> tuple[MockHAClientWebSocket, RefreshToken]:
    """A websocket connection of another admin user, and the login it belongs to."""
    admin_group = await hass.auth.async_get_group(GROUP_ID_ADMIN)
    user = MockUser(name=name, groups=[admin_group]).add_to_hass(hass)
    refresh_token = await hass.auth.async_create_refresh_token(user, CLIENT_ID)
    ws = await hass_ws_client(hass, hass.auth.async_create_access_token(refresh_token))
    return ws, refresh_token


async def _admin_ws(
    hass: HomeAssistant, hass_ws_client: WebSocketGenerator, name: str
) -> MockHAClientWebSocket:
    return (await _admin_login(hass, hass_ws_client, name))[0]


def assert_security_headers(response: aiohttp.ClientResponse, sandboxed: bool = True) -> None:
    for name, value in SECURITY_HEADERS.items():
        assert response.headers.getall(name, []) == [value], name
    if sandboxed:
        assert response.headers.getall("Content-Security-Policy") == [ISOLATION_CSP]
    else:
        assert "Content-Security-Policy" not in response.headers


def assert_cors_null(response: aiohttp.ClientResponse) -> None:
    assert response.headers.getall(hdrs.ACCESS_CONTROL_ALLOW_ORIGIN) == ["null"]
    assert response.headers.getall(hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS) == ["true"]
    assert "Origin" in [v.strip() for v in response.headers[hdrs.VARY].split(",")]


# ---- routing and access -----------------------------------------------------------


async def test_session_url_opens_the_entry_page(env: Env) -> None:
    result = await env.session(ISO)
    assert result["url"] == f"{PROXY_URL_PREFIX}/{ISO}/{result['token']}/page?start=1"

    response = await env.client.get(result["url"])
    assert response.status == 200
    assert response.content_type == "text/html"
    assert env.upstream.last.method == "GET"
    assert env.upstream.last.target == "/page?start=1"


async def test_get_preserves_path_and_query(env: Env) -> None:
    prefix = await env.prefix(ISO)
    target = "/echo/a%20b/c%2Fd.json?x=1&y=two+words&list=a,b&empty=&flag"

    response = await env.client.get(URL(prefix + target, encoded=True))
    assert response.status == 200
    assert env.upstream.last.target == target
    assert await response.text() == f"echo GET {target}"


@pytest.mark.parametrize(
    "target",
    [
        "/echo?name=Living%20Room&path=%2Fconfig&q=%26%3D",
        "/echo/%C3%A9t%C3%A9/100%25?pct=%25&plus=a%2Bb",
    ],
)
async def test_get_preserves_percent_encoded_query(env: Env, target: str) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(URL(prefix + target, encoded=True))
    assert response.status == 200
    # Passed through byte for byte, not encoded a second time
    assert env.upstream.last.target == target


async def test_request_without_query_or_path(env: Env) -> None:
    prefix = await env.prefix(ISO)
    assert (await env.client.get(prefix + "/")).status == 200
    assert env.upstream.last.target == "/"
    assert (await env.client.get(prefix + "/plain")).status == 200
    assert env.upstream.last.target == "/plain"


async def test_post_body_and_method_forwarded(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.post(
        prefix + "/api/save",
        data=b'{"on": true}',
        headers={hdrs.CONTENT_TYPE: "application/json"},
    )
    assert response.status == 200
    assert env.upstream.last.method == "POST"
    assert env.upstream.last.body == b'{"on": true}'
    assert env.upstream.last.headers[hdrs.CONTENT_TYPE] == "application/json"

    response = await env.client.delete(prefix + "/api/item/1")
    assert response.status == 200
    assert env.upstream.last.method == "DELETE"


async def test_unknown_token_is_not_found(env: Env) -> None:
    await env.session(ISO)
    response = await env.client.get(f"{PROXY_URL_PREFIX}/{ISO}/not-a-real-token/page")
    assert response.status == 404
    assert env.upstream.requests == []


async def test_unknown_view_is_not_found(env: Env) -> None:
    token = (await env.session(ISO))["token"]
    response = await env.client.get(f"{PROXY_URL_PREFIX}/nosuchview/{token}/page")
    assert response.status == 404
    assert env.upstream.requests == []


async def test_token_of_another_view_is_not_found(env: Env) -> None:
    token = (await env.session(ISO))["token"]
    response = await env.client.get(f"{PROXY_URL_PREFIX}/{TRUSTED}/{token}/echo")
    assert response.status == 404
    assert env.upstream.requests == []
    # The session itself keeps working for its own view
    response = await env.client.get(f"{PROXY_URL_PREFIX}/{ISO}/{token}/echo")
    assert response.status == 200


async def test_percent_encoded_ids_are_not_found(env: Env) -> None:
    token = (await env.session(ISO))["token"]
    encoded_view = ISO.replace("i", "%69", 1)
    encoded_token = f"%{ord(token[0]):02X}{token[1:]}"
    for url in (
        f"{PROXY_URL_PREFIX}/{encoded_view}/{token}/echo",
        f"{PROXY_URL_PREFIX}/{ISO}/{encoded_token}/echo",
    ):
        response = await env.client.get(URL(url, encoded=True))
        assert response.status == 404, url
    assert env.upstream.requests == []
    assert (await env.client.get(f"{PROXY_URL_PREFIX}/{ISO}/{token}/echo")).status == 200


async def test_session_of_user_no_longer_admin_is_not_found(
    env: Env, hass_admin_user: MockUser
) -> None:
    prefix = await env.prefix(ISO)
    assert (await env.client.get(prefix + "/echo")).status == 200
    requests_before = len(env.upstream.requests)

    await env.hass.auth.async_update_user(hass_admin_user, group_ids=[GROUP_ID_USER])
    assert not hass_admin_user.is_admin
    assert (await env.client.get(prefix + "/echo")).status == 404

    # The session was revoked, not just refused: becoming admin again does not revive it
    await env.hass.auth.async_update_user(hass_admin_user, group_ids=[GROUP_ID_ADMIN])
    assert hass_admin_user.is_admin
    assert (await env.client.get(prefix + "/echo")).status == 404
    assert len(env.upstream.requests) == requests_before


async def test_session_of_deactivated_user_is_not_found(
    env: Env, hass_admin_user: MockUser
) -> None:
    prefix = await env.prefix(ISO)
    assert (await env.client.get(prefix + "/echo")).status == 200
    await env.ws.close()  # Deactivating the user would drop it anyway
    await env.hass.auth.async_update_user(hass_admin_user, is_active=False)
    assert (await env.client.get(prefix + "/echo")).status == 404


@pytest.mark.parametrize(
    "http_config", [{"use_x_forwarded_for": True, "trusted_proxies": ["127.0.0.1"]}]
)
async def test_local_only_user_from_remote_is_not_found(
    env: Env, hass_admin_user: MockUser
) -> None:
    prefix = await env.prefix(ISO)
    remote = {hdrs.X_FORWARDED_FOR: "203.0.113.9"}
    assert (await env.client.get(prefix + "/echo", headers=remote)).status == 200

    await env.hass.auth.async_update_user(hass_admin_user, local_only=True)
    assert (await env.client.get(prefix + "/echo")).status == 200  # From the local network
    requests_before = len(env.upstream.requests)
    assert (await env.client.get(prefix + "/echo", headers=remote)).status == 404
    # Revoked, not just refused for that one request
    assert (await env.client.get(prefix + "/echo")).status == 404
    assert len(env.upstream.requests) == requests_before


# Home Assistant's own websocket leaves its heartbeat timer behind when a revoked
# login closes it (also without this integration)
@pytest.mark.parametrize("expected_lingering_timers", [True])
async def test_session_ends_with_its_login(env: Env, hass_ws_client: WebSocketGenerator) -> None:
    other_ws, refresh_token = await _admin_login(env.hass, hass_ws_client, "Other admin")
    prefix = await env.prefix(ISO, other_ws)
    own_prefix = await env.prefix(ISO)
    assert (await env.client.get(prefix + "/echo")).status == 200

    env.hass.auth.async_remove_refresh_token(refresh_token)
    # Home Assistant closes that login's websocket too
    assert (await other_ws.receive()).type is aiohttp.WSMsgType.CLOSE
    assert (await env.client.get(prefix + "/echo")).status == 404
    # Sessions of other logins are not affected
    assert (await env.client.get(own_prefix + "/echo")).status == 200


async def test_removed_view_is_not_found(env: Env) -> None:
    prefix = await env.prefix(ISO)
    assert (await env.client.get(prefix + "/echo")).status == 200

    assert env.hass.config_entries.async_remove_subentry(env.entry, ISO)
    await env.hass.async_block_till_done()
    assert env.entry.state is ConfigEntryState.LOADED

    assert (await env.client.get(prefix + "/echo")).status == 404
    assert len(env.upstream.requests) == 1


async def test_sessions_survive_web_ui_changes(env: Env) -> None:
    prefix = await env.prefix(ISO)
    hub = env.hub
    env.hass.config_entries.async_add_subentry(
        env.entry,
        ConfigSubentry(
            data={CONF_URL: "http://192.168.1.2/", CONF_MODE: MODE_ISOLATED},
            subentry_type=SUBENTRY_TYPE_VIEW,
            title="Another",
            unique_id=None,
        ),
    )
    await env.hass.async_block_till_done()
    assert env.hub is hub  # Applied in place
    assert (await env.client.get(prefix + "/echo")).status == 200


async def test_sessions_survive_entry_reload(env: Env) -> None:
    prefix = await env.prefix(ISO)
    hub = env.hub
    assert await env.hass.config_entries.async_reload(env.entry.entry_id)
    await env.hass.async_block_till_done()
    assert env.hub is not hub
    assert (await env.client.get(prefix + "/echo")).status == 200


async def test_unloaded_integration_is_not_found(env: Env) -> None:
    prefix = await env.prefix(ISO)
    assert await env.hass.config_entries.async_unload(env.entry.entry_id)
    await env.hass.async_block_till_done()
    assert (await env.client.get(prefix + "/echo")).status == 404
    assert env.upstream.requests == []


async def test_expired_session_is_not_found(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            return now[0]

    monkeypatch.setattr(hub_module, "time", FakeTime)
    prefix = await env.prefix(ISO)

    # Each proxied request extends the session
    for _ in range(3):
        now[0] += SESSION_TTL - 1
        assert (await env.client.get(prefix + "/echo")).status == 200

    now[0] += SESSION_TTL + 1
    assert (await env.client.get(prefix + "/echo")).status == 404


async def test_session_has_a_maximum_age(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    now = [1000.0]

    class FakeTime:
        @staticmethod
        def monotonic() -> float:
            return now[0]

    monkeypatch.setattr(hub_module, "time", FakeTime)
    result = await env.session(ISO)
    prefix = f"{PROXY_URL_PREFIX}/{ISO}/{result['token']}"
    created = now[0]

    # Kept in use all along, as an open view does
    while now[0] + SESSION_TTL - 1 < created + SESSION_MAX_AGE - 1:
        now[0] += SESSION_TTL - 1
        assert env.hub.sessions.touch(result["token"], ISO) is not None
    now[0] = created + SESSION_MAX_AGE - 1
    assert (await env.client.get(prefix + "/echo")).status == 200

    now[0] = created + SESSION_MAX_AGE + 1
    assert (await env.client.get(prefix + "/echo")).status == 404


async def test_reload_releases_upstream_client_sessions(
    env: Env, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    created: list[aiohttp.ClientSession] = []
    create = hub_module.async_create_clientsession

    def _create(*args: Any, **kwargs: Any) -> aiohttp.ClientSession:
        session = create(*args, **kwargs)
        created.append(session)
        return session

    monkeypatch.setattr(hub_module, "async_create_clientsession", _create)
    prefix = await env.prefix(ISO)
    # The first proxied request creates the hub's upstream client session
    assert (await env.client.get(prefix + "/echo")).status == 200
    assert len(created) == 1
    assert await env.hass.config_entries.async_reload(env.entry.entry_id)
    await env.hass.async_block_till_done()
    assert "closes the Home Assistant aiohttp session" not in caplog.text
    assert created[0].closed
    # The new hub makes its own
    assert (await env.client.get(prefix + "/echo")).status == 200
    assert len(created) == 2


async def test_upstream_down_is_bad_gateway(env: Env) -> None:
    prefix = await env.prefix(DOWN)
    response = await env.client.get(prefix + "/")
    assert response.status == 502
    assert "Unplugged is not reachable" in await response.text()
    assert_security_headers(response)


class _FakeResolver(AbstractResolver):
    """Resolves names from a fixed table and records what was asked."""

    def __init__(self, addresses: dict[str, str]) -> None:
        self.addresses = addresses
        self.asked: list[str] = []

    async def resolve(
        self, host: str, port: int = 0, family: socket.AddressFamily = socket.AF_INET
    ) -> list[ResolveResult]:
        self.asked.append(host)
        return [
            {
                "hostname": host,
                "host": self.addresses[host],
                "port": port,
                "family": socket.AF_INET,
                "proto": 0,
                "flags": socket.AI_NUMERICHOST,
            }
        ]

    async def close(self) -> None:
        """Nothing to release."""


async def test_discovered_view_only_connects_to_lan_addresses(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A local name that resolves to the Home Assistant host itself
    resolver = _FakeResolver({"fake-device.local": "127.0.0.1"})
    monkeypatch.setattr(aiohttp_client, "_async_get_or_create_resolver", lambda hass: resolver)
    owner = MockConfigEntry(domain="fake_devices", title="Fake devices")
    owner.add_to_hass(env.hass)
    device = dr.async_get(env.hass).async_get_or_create(
        config_entry_id=owner.entry_id,
        identifiers={("fake_devices", "one")},
        name="Fake device",
        configuration_url=f"http://fake-device.local:{env.upstream.port}/",
    )
    env.hass.config_entries.async_update_entry(
        env.entry, options={**env.entry.options, CONF_DISCOVERY: True}
    )
    await env.hass.async_block_till_done()

    prefix = await env.prefix(f"{DISCOVERED_PREFIX}{device.id}")
    response = await env.client.get(prefix + "/echo")
    assert response.status == 502
    assert "Fake device is not reachable" in await response.text()
    assert resolver.asked == ["fake-device.local"]
    assert env.upstream.requests == []

    # Static views are configured by an admin and may point anywhere
    assert (await env.client.get(await env.prefix(ISO) + "/echo")).status == 200


# ---- response headers ---------------------------------------------------------------


async def test_isolated_security_headers(env: Env) -> None:
    prefix = await env.prefix(ISO)
    for path in ("/page", "/headers", "/big", "/big-html", "/events"):
        env.upstream.sse_release.set()
        response = await env.client.get(prefix + path)
        assert response.status == 200, path
        assert_security_headers(response)
        await response.read()


async def test_trusted_security_headers(env: Env) -> None:
    prefix = await env.prefix(TRUSTED)
    for path in ("/page", "/headers", "/big"):
        response = await env.client.get(prefix + path)
        assert response.status == 200, path
        assert_security_headers(response, sandboxed=False)
        await response.read()


async def test_error_responses_carry_security_headers(env: Env) -> None:
    token = (await env.session(ISO))["token"]
    trusted = await env.prefix(TRUSTED)
    for url in (
        f"{PROXY_URL_PREFIX}/{ISO}/bogus/echo",  # Unknown session
        f"{PROXY_URL_PREFIX}/nosuchview/{token}/echo",  # Unknown view
        await env.prefix(DOWN) + "/",  # Unreachable site
        trusted + "/__lwu/storage",  # Endpoint of isolated views only
    ):
        response = await env.client.get(url)
        assert response.status in (404, 502), url
        # Nothing under the prefix runs unsandboxed, not even an error page
        assert_security_headers(response)


async def test_upstream_security_headers_replaced(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(prefix + "/headers")
    assert response.status == 200
    headers = response.headers
    # Device data and the content headers of the allowlist pass
    assert headers["X-Device"] == "kept"
    assert headers["Content-Language"] == "de"
    assert headers["Content-Disposition"] == 'inline; filename="x.txt"'
    assert headers["Retry-After"] == "5"
    # Security headers are the proxy's own
    assert_security_headers(response)
    assert headers.get("X-Frame-Options") in (None, "SAMEORIGIN")
    # Headers acting on Home Assistant's whole origin never pass
    for name in (
        "Content-Security-Policy-Report-Only",
        "Clear-Site-Data",
        "Strict-Transport-Security",
        "Service-Worker-Allowed",
        "Link",
        "Permissions-Policy",
        "Report-To",
        "Refresh",
        "X-XSS-Protection",
        "X-DNS-Prefetch-Control",
        "X-Permitted-Cross-Domain-Policies",
        hdrs.ACCESS_CONTROL_ALLOW_ORIGIN,
        hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS,
        hdrs.ACCESS_CONTROL_EXPOSE_HEADERS,
    ):
        assert name not in headers, name


async def test_reverse_proxy_control_headers_dropped(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(prefix + "/proxy-control")
    assert response.status == 200
    for name in ("X-Accel-Redirect", "X-Accel-Expires", "X-Sendfile"):
        assert name not in response.headers, name


@pytest.mark.parametrize(
    ("sent", "expected"),
    [
        (None, "private"),
        ("public, max-age=60, s-maxage=600", "private, max-age=60"),
        ("max-age=0, must-revalidate", "private, max-age=0, must-revalidate"),
        ("no-store", "private, no-store"),
    ],
)
async def test_cache_control_made_private(env: Env, sent: str | None, expected: str) -> None:
    prefix = await env.prefix(ISO)
    query = f"?cc={sent}" if sent else ""
    response = await env.client.get(prefix + "/cache" + query)
    assert response.status == 200
    assert response.headers.getall(hdrs.CACHE_CONTROL) == [expected]


@pytest.mark.parametrize("view_id", [ISO, TRUSTED])
async def test_html_never_cached(env: Env, view_id: str) -> None:
    prefix = await env.prefix(view_id)
    response = await env.client.get(prefix + "/page-cached")
    assert response.status == 200
    # The page embeds this user's site data: a stored copy would go stale
    assert response.headers.getall(hdrs.CACHE_CONTROL) == ["no-store"]
    assert hdrs.ETAG not in response.headers
    assert hdrs.LAST_MODIFIED not in response.headers

    # Other responses keep their validators
    response = await env.client.get(prefix + "/cached")
    assert response.headers[hdrs.ETAG] == '"v1"'
    assert response.headers[hdrs.LAST_MODIFIED] == LAST_MODIFIED
    assert response.headers[hdrs.CACHE_CONTROL] == "private"


# ---- isolated mode ----------------------------------------------------------------


async def test_isolated_html_rewritten_and_shim_first(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(prefix + "/page")
    assert response.status == 200
    assert response.headers[hdrs.CONTENT_TYPE].startswith("text/html")
    body = await response.read()

    # The shim is the very first thing inside <head>, before any script of the page
    head = b'<!DOCTYPE html>\n<html lang="en"><head>'
    assert body.startswith(head + SHIM_MARKER)
    assert body.count(SHIM_MARKER) == 1
    config = shim_config(body)
    assert config["prefix"] == prefix
    assert config["viewPath"] == f"{PROXY_URL_PREFIX}/{ISO}/"
    assert config["site"] == env.upstream.origin
    assert config["isolated"] is True
    assert config["storage"] == {}
    assert config["cookies"] == {}
    assert config["writes"] == []

    page = body[body.index(b"</script>") + len(b"</script>") :]
    # Root-relative and same-site absolute URLs go through the proxy
    assert f'href="{prefix}/style.css"'.encode() in page
    assert f'href="{prefix}/settings?x=1"'.encode() in page
    assert f'src="{prefix}/img/logo.png"'.encode() in page
    assert f"src='{prefix}/app.js'".encode() in page
    assert f'action="{prefix}/submit"'.encode() in page
    assert f'formaction="{prefix}/alt"'.encode() in page
    assert f'poster="{prefix}/poster.jpg"'.encode() in page
    # Other sites and relative URLs are left alone
    assert b'href="http://example.com/x"' in page
    assert b'href="//example.com/y"' in page
    assert b'href="relative/page"' in page
    assert b'href="http://127.0.0.1:1/z"' in page
    assert env.upstream.origin.encode() not in page


async def test_meta_referrer_neutralized(env: Env) -> None:
    prefix = await env.prefix(ISO)
    body = await (await env.client.get(prefix + "/meta-referrer")).read()
    page = body[body.index(b"</script>") :]
    # The page's own policy could send its URL, with the token, to other sites
    assert b'<meta name="x-referrer-removed" content="unsafe-url">' in page
    assert b"<META NAME='x-referrer-removed' CONTENT='origin'>" in page
    assert b'<meta content="no-referrer-when-downgrade" name=x-referrer-removed>' in page
    assert not re.search(rb"name\s*=\s*[\"']?referrer", page, re.IGNORECASE)
    # Other uses of the word are left alone
    assert b'<meta name="description" content="referrer">' in page
    assert b"<body>referrer</body>" in page


async def test_referrerpolicy_attributes_neutralized(env: Env) -> None:
    prefix = await env.prefix(ISO)
    body = await (await env.client.get(prefix + "/referrer-attributes")).read()
    page = body[body.index(b"</script>") :]
    assert not re.search(
        rb"\sreferrerpolicy\s*=\s*[\"']?(?:unsafe-url|no-referrer-when-downgrade)",
        page,
        re.IGNORECASE,
    )


async def test_css_root_relative_urls_rewritten(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(prefix + "/style.css")
    assert response.status == 200
    assert response.content_type == "text/css"
    assert await response.text() == (
        f'body{{background:url({prefix}/img/bg.png)}} @import "{prefix}/more.css";'
    )


async def test_chunked_css_rewritten(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(prefix + "/css-chunked")
    assert response.status == 200
    assert response.content_type == "text/css"
    assert await response.text() == (
        f'body{{background:url({prefix}/img/bg.png)}} @import "{prefix}/more.css";'
    )


@pytest.mark.parametrize(
    ("name", "sent", "expected"),
    [
        ("app.js", None, "text/javascript"),
        ("app.js", "", "text/javascript"),
        ("app.mjs", "application/octet-stream", "text/javascript"),
        ("app.js", "text/plain; charset=utf-8", "text/javascript"),
        ("style.css", None, "text/css"),
        ("style.css", "text/plain", "text/css"),
        ("STYLE.CSS", "application/octet-stream", "text/css"),
        # A specific type is the site's choice
        ("app.js", "application/javascript", "application/javascript"),
        # Other files are not guessed at
        ("data.json", "text/plain", "text/plain"),
        ("data", None, "application/octet-stream"),
    ],
)
async def test_script_and_stylesheet_types_fixed(
    env: Env, name: str, sent: str | None, expected: str
) -> None:
    prefix = await env.prefix(ISO)
    query = "" if sent is None else "?" + URL.build(query={"ct": sent}).raw_query_string
    if sent is None:
        direct = await env.client.session.get(f"{env.upstream.origin}/typed/{name}")
        assert hdrs.CONTENT_TYPE not in direct.headers  # Really sent without one
        await direct.read()
    response = await env.client.get(URL(f"{prefix}/typed/{name}{query}", encoded=True))
    assert response.status == 200
    assert response.headers[hdrs.CONTENT_TYPE] == expected
    text = await response.text()
    if expected == "text/css":
        # Rewritten like any stylesheet
        assert f"url({prefix}/img/bg.png)" in text
        assert f'@import "{prefix}/more.css"' in text


@pytest.mark.parametrize("view_id", [ISO, TRUSTED])
@pytest.mark.parametrize("query", ["", "?domain=1"])
async def test_cookies_kept_in_server_side_jar(env: Env, view_id: str, query: str) -> None:
    prefix = await env.prefix(view_id)
    response = await env.client.get(prefix + "/login" + query)
    assert response.status == 200
    # Never set in the browser, whose cookies belong to Home Assistant's origin
    assert hdrs.SET_COOKIE not in response.headers
    assert len(env.client.session.cookie_jar) == 0

    # The browser's own cookies never reach the site; the jar's do
    response = await env.client.get(prefix + "/echo", headers={hdrs.COOKIE: "browser=1"})
    assert response.status == 200
    sent = env.upstream.last.headers.getall(hdrs.COOKIE)
    assert len(sent) == 1
    assert sorted(sent[0].split("; ")) == ["sid=abc123", "theme=dark"]

    # Scripts see the cookies that are not HttpOnly
    assert (await env.page_config(prefix))["cookies"] == {"theme": "dark"}


@pytest.mark.parametrize("view_id", [ISO, TRUSTED])
async def test_browser_cookie_never_forwarded(env: Env, view_id: str) -> None:
    prefix = await env.prefix(view_id)
    response = await env.client.get(prefix + "/echo", headers={hdrs.COOKIE: "sid=xyz; a=b"})
    assert response.status == 200
    assert hdrs.COOKIE not in env.upstream.last.headers


async def test_cookie_jar_is_per_user(env: Env, hass_ws_client: WebSocketGenerator) -> None:
    prefix = await env.prefix(ISO)
    await env.client.get(prefix + "/login")

    other_ws = await _admin_ws(env.hass, hass_ws_client, "Other admin")
    other_prefix = await env.prefix(ISO, other_ws)
    assert other_prefix != prefix
    assert (await env.client.get(other_prefix + "/echo")).status == 200
    assert hdrs.COOKIE not in env.upstream.last.headers

    assert (await env.client.get(prefix + "/echo")).status == 200
    assert "sid=abc123" in env.upstream.last.headers[hdrs.COOKIE]


async def test_cookie_jar_survives_reload(env: Env, hass_storage: dict[str, Any]) -> None:
    prefix = await env.prefix(ISO)
    await env.client.get(prefix + "/login")

    assert await env.hass.config_entries.async_reload(env.entry.entry_id)
    await env.hass.async_block_till_done()
    assert STORAGE_KEY_JAR in hass_storage

    assert (await env.client.get(prefix + "/echo")).status == 200
    assert sorted(env.upstream.last.headers[hdrs.COOKIE].split("; ")) == [
        "sid=abc123",
        "theme=dark",
    ]


# ---- trusted mode -----------------------------------------------------------------


async def test_trusted_no_csp_and_no_storage_shim(env: Env) -> None:
    prefix = await env.prefix(TRUSTED)
    response = await env.client.get(prefix + "/page")
    assert response.status == 200
    assert_security_headers(response, sandboxed=False)
    body = await response.read()
    # No emulated storage: the page has real localStorage. Cookies are server side
    # in both modes, so scripts get them handed over.
    config = shim_config(body)
    assert config["isolated"] is False
    assert "storage" not in config
    assert "writes" not in config
    assert config["cookies"] == {}
    # Links are still kept inside the proxy
    assert f'href="{prefix}/settings?x=1"'.encode() in body

    response = await env.client.get(prefix + "/headers")
    assert "Content-Security-Policy" not in response.headers


# ---- redirects --------------------------------------------------------------------


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("/login?next=/x&a=%26", "{prefix}/login?next=/x&a=%26"),
        ("/", "{prefix}/"),
        ("{origin}/setup/wifi?step=2#top", "{prefix}/setup/wifi?step=2#top"),
        ("{origin}", "{prefix}/"),
        ("//127.0.0.1:{port}/proto-relative?a=1", "{prefix}/proto-relative?a=1"),
        ("http://example.com/elsewhere", "http://example.com/elsewhere"),
        ("//example.com/elsewhere", "//example.com/elsewhere"),
        ("//127.0.0.1:1/other-port", "//127.0.0.1:1/other-port"),
        ("http://127.0.0.1:1/other-port", "http://127.0.0.1:1/other-port"),
        # The same host moving to https is followed inside the proxy
        ("https://127.0.0.1:{port}/other-scheme", "{prefix}/other-scheme"),
        ("relative/page", "relative/page"),
    ],
)
async def test_location_rewritten(env: Env, location: str, expected: str) -> None:
    prefix = await env.prefix(ISO)
    fmt = {"origin": env.upstream.origin, "port": env.upstream.port, "prefix": prefix}
    env.upstream.redirects["r"] = location.format(**fmt)

    response = await env.client.get(prefix + "/redirect/r", allow_redirects=False)
    assert response.status == 302
    assert response.headers[hdrs.LOCATION] == expected.format(**fmt)


# ---- CORS for opaque origins --------------------------------------------------------


async def test_preflight_from_opaque_origin_answered_by_proxy(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.options(
        prefix + "/api/save",
        headers={
            hdrs.ORIGIN: "null",
            hdrs.ACCESS_CONTROL_REQUEST_METHOD: "PUT",
            hdrs.ACCESS_CONTROL_REQUEST_HEADERS: "content-type, x-requested-with",
        },
    )
    assert response.status == 204
    assert_cors_null(response)
    # The requested method is allowed (the token was checked), WebDAV-style ones too
    assert response.headers[hdrs.ACCESS_CONTROL_ALLOW_METHODS] == "PUT"
    assert response.headers[hdrs.ACCESS_CONTROL_MAX_AGE] == "600"
    assert response.headers[hdrs.ACCESS_CONTROL_ALLOW_HEADERS] == "content-type, x-requested-with"
    assert_security_headers(response)
    assert env.upstream.requests == []

    # Nothing requested, nothing allowed
    response = await env.client.options(
        prefix + "/api/save",
        headers={hdrs.ORIGIN: "null", hdrs.ACCESS_CONTROL_REQUEST_METHOD: "POST"},
    )
    assert response.status == 204
    assert hdrs.ACCESS_CONTROL_ALLOW_HEADERS not in response.headers
    assert env.upstream.requests == []


async def test_preflight_needs_a_valid_session(env: Env) -> None:
    await env.session(ISO)
    response = await env.client.options(
        f"{PROXY_URL_PREFIX}/{ISO}/bogus/api/save",
        headers={hdrs.ORIGIN: "null", hdrs.ACCESS_CONTROL_REQUEST_METHOD: "POST"},
    )
    assert response.status == 404
    assert hdrs.ACCESS_CONTROL_ALLOW_METHODS not in response.headers
    assert_cors_null(response)  # So the page can tell what happened


async def test_plain_options_forwarded(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.options(prefix + "/api/save")
    assert response.status == 200
    assert env.upstream.last.method == "OPTIONS"
    assert hdrs.ACCESS_CONTROL_ALLOW_ORIGIN not in response.headers


async def test_opaque_origin_responses_allow_null_with_credentials(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(prefix + "/cors", headers={hdrs.ORIGIN: "null"})
    assert response.status == 200
    assert_cors_null(response)
    # The site's Vary is kept, with Origin added
    assert response.headers.getall(hdrs.VARY) == ["Accept-Encoding, Origin"]
    # With credentials allowed "*" is no wildcard: the actual names are listed
    assert response.headers.getall(hdrs.ACCESS_CONTROL_EXPOSE_HEADERS) == [
        "cache-control, content-language, content-security-policy, date, "
        "referrer-policy, vary, x-content-type-options, x-device"
    ]

    # Without a Vary of its own, and for streamed responses too
    for path in ("/echo", "/big"):
        response = await env.client.get(prefix + path, headers={hdrs.ORIGIN: "null"})
        assert response.status == 200
        assert_cors_null(response)
        assert response.headers.getall(hdrs.VARY) == ["Origin"]
        exposed = response.headers[hdrs.ACCESS_CONTROL_EXPOSE_HEADERS].split(", ")
        assert exposed == sorted(exposed)
        assert "date" in exposed
        assert all(name == name.lower() for name in exposed)
        await response.read()

    # Cookies go to the server-side jar, and are neither sent nor exposed
    response = await env.client.get(prefix + "/login", headers={hdrs.ORIGIN: "null"})
    assert hdrs.SET_COOKIE not in response.headers
    assert "set-cookie" not in response.headers[hdrs.ACCESS_CONTROL_EXPOSE_HEADERS]

    # Same-origin requests get no CORS headers
    for headers in ({}, {hdrs.ORIGIN: f"http://127.0.0.1:{env.client.port}"}):
        response = await env.client.get(prefix + "/cors", headers=headers)
        assert hdrs.ACCESS_CONTROL_ALLOW_ORIGIN not in response.headers
        assert hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS not in response.headers
        assert hdrs.ACCESS_CONTROL_EXPOSE_HEADERS not in response.headers
        assert response.headers.getall(hdrs.VARY) == ["Accept-Encoding"]


async def test_opaque_origin_error_responses_readable(env: Env) -> None:
    null = {hdrs.ORIGIN: "null"}
    prefix = await env.prefix(ISO)
    trusted = await env.prefix(TRUSTED)
    down = await env.prefix(DOWN)
    too_big = b'{"set": {}}' + b" " * 4 * MAX_SHIM_STORAGE_BYTES
    checks: list[tuple[int, Callable[[], Awaitable[aiohttp.ClientResponse]]]] = [
        (404, lambda: env.client.get(f"{PROXY_URL_PREFIX}/{ISO}/bogus/echo", headers=null)),
        (502, lambda: env.client.get(down + "/", headers=null)),
        (400, lambda: env.post_storage(prefix, b"not json", headers=null)),
        (413, lambda: env.post_storage(prefix, too_big, headers=null)),
        (404, lambda: env.post_storage(trusted, {"set": {}}, headers=null)),
    ]
    for status, request in checks:
        response = await request()
        assert response.status == status
        assert_cors_null(response)
        assert_security_headers(response)
    assert env.upstream.requests == []


# ---- request headers towards the site ----------------------------------------------


async def test_origin_and_referer_stripped(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(
        prefix + "/echo",
        headers={
            hdrs.ORIGIN: "null",
            hdrs.REFERER: f"http://127.0.0.1:{env.client.port}{prefix}/page",
            "X-Custom": "yes",
        },
    )
    assert response.status == 200
    headers = env.upstream.last.headers
    assert hdrs.ORIGIN not in headers
    assert hdrs.REFERER not in headers
    assert headers["X-Custom"] == "yes"
    assert headers[hdrs.HOST] == f"127.0.0.1:{env.upstream.port}"


async def test_ingress_and_forwarded_headers(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(prefix + "/echo")
    assert response.status == 200
    headers = env.upstream.last.headers
    assert headers["X-Ingress-Path"] == prefix
    assert headers.getall(hdrs.X_FORWARDED_FOR) == ["127.0.0.1"]
    assert headers.getall(hdrs.X_FORWARDED_PROTO) == ["http"]
    assert hdrs.X_FORWARDED_HOST not in headers


@pytest.mark.parametrize(
    "http_config", [{"use_x_forwarded_for": True, "trusted_proxies": ["127.0.0.1"]}]
)
async def test_forwarded_headers_behind_reverse_proxy(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(
        prefix + "/echo",
        headers={
            hdrs.X_FORWARDED_FOR: "203.0.113.9",
            hdrs.X_FORWARDED_HOST: "ha.example.com",
            hdrs.X_FORWARDED_PROTO: "https",
        },
    )
    assert response.status == 200
    headers = env.upstream.last.headers
    # What Home Assistant made of them, not what the client sent
    assert headers.getall(hdrs.X_FORWARDED_FOR) == ["203.0.113.9"]
    assert headers.getall(hdrs.X_FORWARDED_PROTO) == ["https"]
    assert hdrs.X_FORWARDED_HOST not in headers


async def test_client_identity_and_forwarding_headers_dropped(env: Env) -> None:
    prefix = await env.prefix(ISO)
    dropped = {
        hdrs.FORWARDED: "for=203.0.113.9;proto=https",
        hdrs.X_FORWARDED_HOST: "evil.example",
        "X-Real-IP": "203.0.113.9",
        "Remote-User": "admin",
        "Remote-Email": "admin@example.com",
        "Remote-Groups": "admins",
        "Remote-Name": "Admin",
        "X-Remote-User": "admin",
        "X-Forwarded-User": "admin",
        "X-Forwarded-Email": "admin@example.com",
        "X-Forwarded-Preferred-Username": "admin",
        "X-Forwarded-Groups": "admins",
        "X-Forwarded-Access-Token": "token",
        "X-Auth-Request-User": "admin",
        "X-Auth-Request-Access-Token": "token",
        "Cf-Access-Jwt-Assertion": "jwt",
        "Cf-Access-Authenticated-User-Email": "admin@example.com",
    }
    response = await env.client.get(
        prefix + "/echo",
        headers={
            **dropped,
            hdrs.X_FORWARDED_PROTO: "https",
            "X-Ingress-Path": "/elsewhere",
            "X-Custom": "kept",
        },
    )
    assert response.status == 200
    headers = env.upstream.last.headers
    for name in dropped:
        assert name not in headers, name
    # Replaced by the proxy's own values
    assert headers.getall(hdrs.X_FORWARDED_PROTO) == ["http"]
    assert headers.getall("X-Ingress-Path") == [prefix]
    assert headers["X-Custom"] == "kept"


async def test_stored_credentials_injected(env: Env) -> None:
    prefix = await env.prefix(AUTH)
    response = await env.client.get(prefix + "/echo")
    assert response.status == 200
    assert env.upstream.last.headers.getall(hdrs.AUTHORIZATION) == [_basic("admin", "s3cret")]


@pytest.mark.parametrize(
    ("view_id", "expected"),
    [(AUTH, [_basic("admin", "s3cret")]), (ISO, []), (TRUSTED, [])],
)
async def test_browser_authorization_never_forwarded(
    env: Env, view_id: str, expected: list[str]
) -> None:
    # e.g. Basic credentials of a reverse proxy in front of Home Assistant
    prefix = await env.prefix(view_id)
    own = _basic("someone", "else")
    response = await env.client.get(prefix + "/echo", headers={hdrs.AUTHORIZATION: own})
    assert response.status == 200
    assert env.upstream.last.headers.getall(hdrs.AUTHORIZATION, []) == expected


@pytest.mark.parametrize(
    ("dest", "forwarded"),
    [
        (None, True),
        ("empty", True),
        ("script", True),
        ("image", True),
        ("document", False),
        ("iframe", False),
    ],
)
async def test_conditional_headers_dropped_for_page_loads(
    env: Env, dest: str | None, forwarded: bool
) -> None:
    prefix = await env.prefix(ISO)
    headers = {hdrs.IF_NONE_MATCH: '"v1"', hdrs.IF_MODIFIED_SINCE: LAST_MODIFIED}
    if dest is not None:
        headers["Sec-Fetch-Dest"] = dest
    response = await env.client.get(prefix + "/cached", headers=headers)
    sent = env.upstream.last.headers
    if forwarded:
        assert response.status == 304
        assert sent[hdrs.IF_NONE_MATCH] == '"v1"'
        assert sent[hdrs.IF_MODIFIED_SINCE] == LAST_MODIFIED
    else:
        # A page must not be reused with stale injected data
        assert response.status == 200
        assert await response.text() == "fresh"
        assert hdrs.IF_NONE_MATCH not in sent
        assert hdrs.IF_MODIFIED_SINCE not in sent


# ---- per-site request limit -----------------------------------------------------------


async def test_concurrent_requests_per_site_limited(env: Env) -> None:
    prefix = await env.prefix(ISO)
    same_site = await env.prefix(TRUSTED)
    down = await env.prefix(DOWN)
    first = [
        asyncio.create_task(env.client.get(f"{prefix}/hold?id={i}"))
        for i in range(MAX_REQUESTS_PER_SITE)
    ]
    async with asyncio.timeout(5):
        await env.upstream.wait_held(lambda held: len(held) == MAX_REQUESTS_PER_SITE)

    # One more, through another web UI of the same site, waits for a free slot
    waiting = asyncio.create_task(env.client.get(f"{same_site}/hold?id=late"))
    await asyncio.sleep(0.3)
    assert not waiting.done()
    assert sorted(env.upstream.held) == sorted(str(i) for i in range(MAX_REQUESTS_PER_SITE))
    # Other sites are not held up
    response = await asyncio.wait_for(env.client.get(down + "/"), 5)
    assert response.status == 502

    env.upstream.release("0")
    async with asyncio.timeout(5):
        await env.upstream.wait_held(lambda held: "late" in held)
    env.upstream.release_all()
    for task in [*first, waiting]:
        response = await asyncio.wait_for(task, 5)
        assert response.status == 200
        assert (await response.text()).startswith("held ")


async def test_streamed_responses_do_not_hold_a_slot(env: Env) -> None:
    prefix = await env.prefix(ISO)
    streams = []
    for _ in range(MAX_REQUESTS_PER_SITE):
        response = await asyncio.wait_for(env.client.get(prefix + "/events"), 5)
        first = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 5)
        assert first == b"data: one\n\n"
        streams.append(response)

    # Every stream is still open, yet other requests go through
    response = await asyncio.wait_for(env.client.get(prefix + "/echo"), 5)
    assert response.status == 200
    assert not env.upstream.sse_done

    env.upstream.sse_release.set()
    for stream in streams:
        assert await asyncio.wait_for(stream.content.read(), 5) == b"data: two\n\n"


async def test_response_headers_timeout_is_bad_gateway(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(proxy_module, "UPSTREAM_HEADERS_TIMEOUT", 0.2)
    # A single slot shows whether the timed out request gave it back
    monkeypatch.setattr(hub_module, "MAX_REQUESTS_PER_SITE", 1)
    prefix = await env.prefix(ISO)

    response = await asyncio.wait_for(env.client.get(prefix + "/hold?id=silent"), 5)
    assert response.status == 502
    assert "Isolated site is not reachable" in await response.text()
    assert env.upstream.held == ["silent"]

    response = await asyncio.wait_for(env.client.get(prefix + "/echo"), 5)
    assert response.status == 200

    # Only the wait for the headers is limited: a stream may stay quiet longer
    response = await asyncio.wait_for(env.client.get(prefix + "/events"), 5)
    assert await asyncio.wait_for(response.content.readuntil(b"\n\n"), 5) == b"data: one\n\n"
    await asyncio.sleep(0.4)
    env.upstream.sse_release.set()
    assert await asyncio.wait_for(response.content.read(), 5) == b"data: two\n\n"


# ---- internal endpoints of the storage shim -------------------------------------------


async def test_storage_endpoint_persists_into_next_page(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.post_storage(
        prefix,
        {"w": "w1", "set": {"theme": "dark", "count": 3, "html": "</script><b>"}},
        headers={hdrs.ORIGIN: "null"},
    )
    assert response.status == 204
    assert_cors_null(response)
    assert env.upstream.requests == []  # Answered by the proxy itself

    body = await (await env.client.get(prefix + "/page")).read()
    assert shim_config(body)["storage"] == {
        "theme": "dark",
        "count": "3",
        "html": "</script><b>",
    }
    # The stored value could not close the injected script early
    assert body.count(b"</script>") == 2  # The shim's and the page's own


async def test_storage_writes_are_diffs(env: Env) -> None:
    prefix = await env.prefix(ISO)

    async def write(payload: dict[str, Any]) -> dict[str, str]:
        assert (await env.post_storage(prefix, payload)).status == 204
        return (await env.page_config(prefix))["storage"]

    assert await write({"w": "w1", "set": {"a": "1", "b": "2"}}) == {"a": "1", "b": "2"}
    # Other keys stay; null removes one
    assert await write({"w": "w2", "set": {"a": None, "c": "3", "missing": None}}) == {
        "b": "2",
        "c": "3",
    }
    # Clear applies first, then the changes that came after it
    assert await write({"w": "w3", "clear": True, "set": {"d": "4"}}) == {"d": "4"}
    assert await write({"w": "w4", "clear": True, "set": {}}) == {}
    # Without a write id
    assert await write({"set": {"e": "5"}}) == {"e": "5"}


async def test_storage_is_per_user(env: Env, hass_ws_client: WebSocketGenerator) -> None:
    prefix = await env.prefix(ISO)
    response = await env.post_storage(prefix, {"w": "mine", "set": {"a": "1"}})
    assert response.status == 204

    other_ws = await _admin_ws(env.hass, hass_ws_client, "Other admin")
    other_prefix = await env.prefix(ISO, other_ws)
    config = await env.page_config(other_prefix)
    assert config["storage"] == {}
    assert config["writes"] == []


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"\xff\xfe",
        b"[1, 2]",
        b"null",
        b'"text"',
        b"{}",  # No changes
        b'{"w": "x", "clear": true}',  # No changes either
        b'{"set": ["a"]}',
        b'{"set": "a=1"}',
        b'{"set": null}',
    ],
)
async def test_storage_endpoint_rejects_bad_data(env: Env, body: bytes) -> None:
    prefix = await env.prefix(ISO)
    assert (await env.post_storage(prefix, {"w": "keep", "set": {"keep": "1"}})).status == 204
    assert (await env.post_storage(prefix, body)).status == 400
    config = await env.page_config(prefix)
    assert config["storage"] == {"keep": "1"}
    assert config["writes"] == ["keep"]
    assert [r.target for r in env.upstream.requests] == ["/page"]


async def test_storage_size_limit(env: Env) -> None:
    prefix = await env.prefix(ISO)
    half = MAX_SHIM_STORAGE_BYTES // 2
    a_value = "a" * half
    b_value = "b" * (MAX_SHIM_STORAGE_BYTES - half - 2)
    # Keys and values count; exactly at the limit is fine
    assert (await env.post_storage(prefix, {"w": "w1", "set": {"a": a_value}})).status == 204
    assert (await env.post_storage(prefix, {"w": "w2", "set": {"b": b_value}})).status == 204

    # One byte over: rejected as a whole, the removal in it included
    over = {"w": "over", "set": {"a": None, "c": "c" * (half + 1)}}
    assert (await env.post_storage(prefix, over)).status == 413
    assert env.upstream.requests == []
    config = await env.page_config(prefix)
    assert config["storage"] == {"a": a_value, "b": b_value}
    # Recorded although rejected: the next page load must not wait for it
    assert config["writes"] == ["w1", "w2", "over"]

    # What a write removes makes room for what it adds
    fits = {"w": "w3", "set": {"a": None, "c": "c" * half}}
    assert (await env.post_storage(prefix, fits)).status == 204
    assert (await env.page_config(prefix))["storage"] == {"b": b_value, "c": "c" * half}


async def test_storage_key_limit(env: Env) -> None:
    prefix = await env.prefix(ISO)
    keys = {f"k{i:04}": "" for i in range(MAX_SHIM_STORAGE_KEYS)}
    assert (await env.post_storage(prefix, {"w": "w1", "set": keys})).status == 204

    response = await env.post_storage(prefix, {"w": "w2", "set": {"k0000": "x", "extra": ""}})
    assert response.status == 413
    config = await env.page_config(prefix)
    assert config["storage"] == keys
    assert config["writes"] == ["w1", "w2"]

    response = await env.post_storage(prefix, {"w": "w3", "set": {"k0000": None, "extra": ""}})
    assert response.status == 204
    assert len((await env.page_config(prefix))["storage"]) == MAX_SHIM_STORAGE_KEYS


async def test_storage_body_size_limit(env: Env) -> None:
    prefix = await env.prefix(ISO)
    # The data limit counts characters; the body may use up to 4 UTF-8 bytes each
    small = json.dumps({"w": "big", "set": {"x": "1"}}).encode()
    body = small + b" " * (4 * MAX_SHIM_STORAGE_BYTES + 1 - len(small))
    assert len(body) == 4 * MAX_SHIM_STORAGE_BYTES + 1
    assert (await env.post_storage(prefix, body)).status == 413
    config = await env.page_config(prefix)
    assert config["storage"] == {}
    assert config["writes"] == []

    # At the limit it is read
    assert (await env.post_storage(prefix, body[:-1])).status == 204
    config = await env.page_config(prefix)
    assert config["storage"] == {"x": "1"}
    assert config["writes"] == ["big"]


async def test_storage_write_ids_remembered(env: Env) -> None:
    prefix = await env.prefix(ISO)
    assert (await env.post_storage(prefix, {"w": "first", "set": {"a": "1"}})).status == 204
    assert (await env.page_config(prefix))["writes"] == ["first"]

    # Only the last ones, which is what a page load can be racing with
    ids = [f"id{i}" for i in range(40)]
    for write_id in ids:
        response = await env.post_storage(prefix, {"w": write_id, "set": {"a": write_id}})
        assert response.status == 204
    assert (await env.page_config(prefix))["writes"] == ids[-32:]

    # Long ids are cut
    assert (await env.post_storage(prefix, {"w": "x" * 100, "set": {}})).status == 204
    assert (await env.page_config(prefix))["writes"][-1] == "x" * 64


async def test_storage_read_waits_for_write(env: Env, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(hub_module, "STORAGE_WRITE_WAIT", 30)
    prefix = await env.prefix(ISO)
    null = {hdrs.ORIGIN: "null"}
    assert (await env.post_storage(prefix, {"w": "done", "set": {"a": "1"}})).status == 204

    # Already applied: answered at once
    response = await asyncio.wait_for(
        env.client.get(prefix + "/__lwu/storage?w=done", headers=null), 5
    )
    assert response.status == 200
    assert await response.json() == {"a": "1"}
    assert response.headers[hdrs.CACHE_CONTROL] == "no-store"
    assert_cors_null(response)
    assert_security_headers(response)

    # The previous page's last write arrives while the next page waits for it
    task = asyncio.create_task(env.client.get(prefix + "/__lwu/storage?w=late"))
    await asyncio.sleep(0.3)
    assert not task.done()
    assert (await env.post_storage(prefix, {"w": "late", "set": {"b": "2"}})).status == 204
    response = await asyncio.wait_for(task, 5)
    assert response.status == 200
    assert await response.json() == {"a": "1", "b": "2"}
    assert env.upstream.requests == []


async def test_storage_read_gives_up_on_unknown_write(
    env: Env, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(hub_module, "STORAGE_WRITE_WAIT", 0.3)
    prefix = await env.prefix(ISO)
    assert (await env.post_storage(prefix, {"w": "known", "set": {"a": "1"}})).status == 204

    start = time.monotonic()
    response = await asyncio.wait_for(env.client.get(prefix + "/__lwu/storage?w=lost"), 5)
    elapsed = time.monotonic() - start
    assert response.status == 200
    assert await response.json() == {"a": "1"}
    assert 0.25 <= elapsed < 3

    # A write with that id arriving later is still applied as usual
    assert (await env.post_storage(prefix, {"w": "lost", "set": {"b": "2"}})).status == 204
    response = await asyncio.wait_for(env.client.get(prefix + "/__lwu/storage?w=lost"), 5)
    assert await response.json() == {"a": "1", "b": "2"}


async def test_cookie_endpoint_adds_cookie_sent_upstream(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.post(
        prefix + "/__lwu/cookie", data=b"lang=de", headers={hdrs.ORIGIN: "null"}
    )
    assert response.status == 204
    assert_cors_null(response)
    assert env.upstream.requests == []

    assert (await env.client.get(prefix + "/echo")).status == 200
    assert env.upstream.last.headers[hdrs.COOKIE] == "lang=de"
    assert (await env.page_config(prefix))["cookies"] == {"lang": "de"}


async def test_internal_endpoints_methods(env: Env) -> None:
    prefix = await env.prefix(ISO)
    assert (await env.client.get(prefix + "/__lwu/cookie")).status == 404
    assert (await env.client.put(prefix + "/__lwu/storage", data=b'{"set": {}}')).status == 404
    assert (await env.client.post(prefix + "/__lwu/other", data=b"{}")).status == 404
    assert (await env.client.get(prefix + "/__lwu/other")).status == 404
    assert env.upstream.requests == []


async def test_internal_endpoints_in_trusted_mode(env: Env) -> None:
    prefix = await env.prefix(TRUSTED)
    # Trusted pages have real localStorage
    response = await env.post_storage(prefix, {"w": "x", "set": {"a": "1"}})
    assert response.status == 404
    assert (await env.client.get(prefix + "/__lwu/storage?w=x")).status == 404
    # Cookies are server side in both modes
    response = await env.client.post(prefix + "/__lwu/cookie", data=b"a=1")
    assert response.status == 204
    assert env.upstream.requests == []
    assert (await env.client.get(prefix + "/echo")).status == 200
    assert env.upstream.last.headers[hdrs.COOKIE] == "a=1"


# ---- bodies and streaming -----------------------------------------------------------


@pytest.mark.parametrize("path", ["/big", "/big-chunked"])
async def test_large_body_streamed_intact(env: Env, path: str) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(prefix + path)
    assert response.status == 200
    assert response.content_type == "application/octet-stream"
    assert await response.read() == BIG


@pytest.mark.parametrize("path", ["/big-html", "/big-html-chunked"])
async def test_large_html_injected_then_streamed(env: Env, path: str) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(prefix + path)
    assert response.status == 200
    assert response.content_type == "text/html"
    assert response.headers.getall("Content-Security-Policy") == [ISOLATION_CSP]
    assert response.headers.getall(hdrs.CACHE_CONTROL) == ["no-store"]
    body = await response.read()

    # The start of the page gets the script and the rewriting, the rest streams as is
    assert body.startswith(b"<html><head>" + SHIM_MARKER)
    assert shim_config(body)["prefix"] == prefix
    page = body[body.index(b"</script>") + len(b"</script>") :]
    assert page == (
        b'</head><body><a href="' + prefix.encode() + b'/early">e</a>' + BIG + BIG_PAGE_TAIL
    )


async def test_large_upload_forwarded_intact(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.post(prefix + "/upload", data=BIG)
    assert response.status == 200
    assert env.upstream.last.body == BIG


async def test_event_stream_is_incremental(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await asyncio.wait_for(env.client.get(prefix + "/events"), 5)
    assert response.status == 200
    assert response.content_type == "text/event-stream"
    assert hdrs.CONTENT_ENCODING not in response.headers

    # The first event arrives while the site is still holding the stream open
    first = await asyncio.wait_for(response.content.readuntil(b"\n\n"), 5)
    assert first == b"data: one\n\n"
    assert not env.upstream.sse_done

    env.upstream.sse_release.set()
    rest = await asyncio.wait_for(response.content.read(), 5)
    assert rest == b"data: two\n\n"
    assert env.upstream.sse_done


async def test_head_request(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.head(prefix + "/page")
    assert response.status == 200
    assert response.headers[hdrs.CONTENT_TYPE].startswith("text/html")
    assert response.headers.getall("Content-Security-Policy") == [ISOLATION_CSP]
    assert await response.read() == b""
    assert env.upstream.last.method == "HEAD"


async def test_no_content_response(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.post(prefix + "/nocontent", data=b"x")
    assert response.status == 204
    assert response.headers["X-Device"] == "kept"
    assert await response.read() == b""


async def test_not_modified_response(env: Env) -> None:
    prefix = await env.prefix(ISO)
    response = await env.client.get(prefix + "/cached")
    assert response.status == 200
    assert response.headers[hdrs.ETAG] == '"v1"'
    assert await response.text() == "fresh"

    response = await env.client.get(prefix + "/cached", headers={hdrs.IF_NONE_MATCH: '"v1"'})
    assert response.status == 304
    assert response.headers[hdrs.ETAG] == '"v1"'
    assert await response.read() == b""
    assert env.upstream.last.headers[hdrs.IF_NONE_MATCH] == '"v1"'


async def test_redirect_to_https_on_same_host_followed_in_proxy(env: Env) -> None:
    prefix = await env.prefix(ISO)
    host = URL(env.upstream.origin).host
    env.upstream.redirects["r"] = f"https://{host}:8443/login?next=home"

    response = await env.client.get(prefix + "/redirect/r", allow_redirects=False)
    assert response.status == 302
    # Stays in the proxy: the browser never leaves for the LAN address
    assert response.headers[hdrs.LOCATION] == prefix + "/login?next=home"
    view = env.hub.get_view(ISO)
    assert view is not None
    assert env.hub.effective_view(view).origin == URL(f"https://{host}:8443")

    # Later requests go to the https site (nothing listens there in this test)
    assert (await env.client.get(prefix + "/login")).status == 502
    # The http site saw only the redirect request
    assert len(env.upstream.requests) == 1


@pytest.mark.parametrize(
    ("location", "origin", "expected"),
    [
        ("https://192.168.1.5/", "http://192.168.1.5", "https://192.168.1.5"),
        ("https://192.168.1.5:8443/x", "http://192.168.1.5:8080", "https://192.168.1.5:8443"),
        ("https://other.lan/", "http://192.168.1.5", None),
        ("http://192.168.1.5:81/", "http://192.168.1.5", None),
        ("https://192.168.1.5/", "https://192.168.1.5:8443", None),
        ("/relative", "http://192.168.1.5", None),
    ],
)
def test_https_upgrade(location: str, origin: str, expected: str | None) -> None:
    result = https_upgrade(location, URL(origin))
    assert (None if result is None else str(result)) == expected
