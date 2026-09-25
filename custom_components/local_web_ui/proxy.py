"""Reverse proxy from Home Assistant's HTTP server to local web UIs.

Requests arrive at /api/local_web_ui/<view_id>/<session token>/<path>. The token
names a session created over the authenticated websocket API; the view decides
where the request goes. Nothing in the request can choose the upstream host.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from http.cookies import SimpleCookie
import json
import logging
from pathlib import Path
import re
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import ClientTimeout, hdrs, web
from aiohttp.helpers import must_be_empty_body
from homeassistant.components.http import KEY_HASS
from homeassistant.core import HomeAssistant, callback
from multidict import CIMultiDict
from yarl import URL

from .const import (
    DOMAIN,
    INTERNAL_PATH_PREFIX,
    MAX_SHIM_STORAGE_BYTES,
    MODE_ISOLATED,
    PROXY_URL_PREFIX,
)

if TYPE_CHECKING:
    from .hub import LocalWebUiHub, Session, View

_LOGGER = logging.getLogger(__name__)

HOP_BY_HOP = {
    hdrs.CONNECTION,
    hdrs.KEEP_ALIVE,
    hdrs.PROXY_AUTHENTICATE,
    hdrs.PROXY_AUTHORIZATION,
    hdrs.TE,
    hdrs.TRAILER,
    hdrs.TRANSFER_ENCODING,
    hdrs.UPGRADE,
}
REQUEST_HEADERS_FILTER = HOP_BY_HOP | {
    hdrs.HOST,
    # HA already authenticated the user. Browsers send HA's origin (or "null"),
    # which device UIs such as ESPHome's web_server reject as cross-origin.
    hdrs.ORIGIN,
    hdrs.REFERER,  # Contains the session token
    hdrs.ACCEPT_ENCODING,  # aiohttp negotiates what it can decode
    hdrs.CONTENT_LENGTH,
    hdrs.SEC_WEBSOCKET_EXTENSIONS,
    hdrs.SEC_WEBSOCKET_PROTOCOL,
    hdrs.SEC_WEBSOCKET_VERSION,
    hdrs.SEC_WEBSOCKET_KEY,
    hdrs.COOKIE,  # Re-added per mode below
}
RESPONSE_HEADERS_FILTER = HOP_BY_HOP | {
    hdrs.CONTENT_LENGTH,
    hdrs.CONTENT_TYPE,
    hdrs.CONTENT_ENCODING,
    # The UI is framed by HA; HA's own policy applies
    "X-Frame-Options",
    "Content-Security-Policy",
    "Content-Security-Policy-Report-Only",
    hdrs.ACCESS_CONTROL_ALLOW_ORIGIN,
    hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS,
    hdrs.SET_COOKIE,
    hdrs.LOCATION,
    "Referrer-Policy",
    "Clear-Site-Data",  # Would clear Home Assistant's own storage in trusted mode
}

# Isolated views get an opaque origin: they cannot read Home Assistant's storage
# (which holds its access tokens) or script its pages. As a response header this
# also holds when a view is opened in a tab of its own.
# Popups may escape the sandbox so links to other sites work normally; a popup
# showing another proxied page is sandboxed again by this same header.
ISOLATION_CSP = (
    "sandbox allow-scripts allow-forms allow-popups "
    "allow-popups-to-escape-sandbox allow-modals allow-downloads"
)
# Never let a device page pick a policy that leaks the session token to other sites
REFERRER_POLICY = "strict-origin-when-cross-origin"

MAX_SIMPLE_RESPONSE_SIZE = 4 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE_SIZE = 16 * 1024 * 1024
UPSTREAM_TIMEOUT = ClientTimeout(total=None, sock_connect=10)


@dataclass(slots=True)
class _Context:
    """What a proxied request is for."""

    hub: LocalWebUiHub
    session: Session
    view: View
    prefix: str  # /api/local_web_ui/<view_id>/<token>
    cross_origin: bool  # Sent by an isolated (opaque origin) page


@callback
def async_register_proxy(hass: HomeAssistant) -> None:
    """Route every method under the proxy prefix to the proxy.

    Not a HomeAssistantView: those get HA's CORS handling attached, which claims
    OPTIONS for itself (only /api/hassio_ingress/ is exempt). Isolated views have
    an opaque origin, so their preflights must reach us.
    """
    resource = hass.http.app.router.add_resource(
        PROXY_URL_PREFIX + "/{view_id}/{token}/{path:.*}", name=f"{DOMAIN}:proxy"
    )
    resource.add_route(hdrs.METH_ANY, _handle)


async def _handle(request: web.Request) -> web.StreamResponse:
    hass = request.app[KEY_HASS]
    hub: LocalWebUiHub | None = hass.data.get(DOMAIN)
    view_id = request.match_info["view_id"]
    token = request.match_info["token"]
    if hub is None or (session := hub.sessions.touch(token, view_id)) is None:
        raise web.HTTPNotFound
    user = await hass.auth.async_get_user(session.user_id)
    if user is None or not user.is_active or not user.is_admin:
        hub.sessions.revoke_user(session.user_id)
        raise web.HTTPNotFound
    if (view := hub.get_view(view_id)) is None:
        raise web.HTTPNotFound

    prefix = f"{PROXY_URL_PREFIX}/{view_id}/{token}"
    raw_path = request.rel_url.raw_path[len(prefix) :] or "/"
    # Isolated pages have an opaque origin, so their requests to us are cross-origin
    cross_origin = request.headers.get(hdrs.ORIGIN) == "null"
    if (
        cross_origin
        and request.method == hdrs.METH_OPTIONS
        and (hdrs.ACCESS_CONTROL_REQUEST_METHOD in request.headers)
    ):
        return _preflight_response(request)
    ctx = _Context(hub, session, view, prefix, cross_origin)
    if raw_path.startswith("/" + INTERNAL_PATH_PREFIX):
        return await _handle_internal(request, ctx, raw_path)

    url = view.origin.with_path(raw_path, encoded=True).with_query(request.rel_url.raw_query_string)
    if not request.rel_url.raw_query_string:
        url = url.with_query(None)
    headers = _request_headers(request, ctx, url)
    client = hub.http_client(view.verify_ssl)
    try:
        if _is_websocket(request):
            return await _proxy_websocket(request, client, url, headers)
        return await _proxy_request(request, ctx, client, url, headers)
    except (aiohttp.ClientError, TimeoutError) as err:
        _LOGGER.debug("Proxying %s for %s failed: %s", url.path, view.name, err)
        raise web.HTTPBadGateway(text=f"{view.name} is not reachable") from None


def _preflight_response(request: web.Request) -> web.Response:
    headers = {
        hdrs.ACCESS_CONTROL_ALLOW_ORIGIN: "*",
        hdrs.ACCESS_CONTROL_ALLOW_METHODS: "GET, HEAD, POST, PUT, PATCH, DELETE",
        hdrs.ACCESS_CONTROL_MAX_AGE: "600",
    }
    if requested := request.headers.get(hdrs.ACCESS_CONTROL_REQUEST_HEADERS):
        headers[hdrs.ACCESS_CONTROL_ALLOW_HEADERS] = requested
    return web.Response(status=204, headers=headers)


def _request_headers(request: web.Request, ctx: _Context, url: URL) -> CIMultiDict[str]:
    hub, session, view, prefix = ctx.hub, ctx.session, ctx.view, ctx.prefix
    headers = CIMultiDict(
        (name, value)
        for name, value in request.headers.items()
        if name not in REQUEST_HEADERS_FILTER
    )
    if view.mode == MODE_ISOLATED:
        # The browser holds no cookies for an opaque origin; the jar stands in
        cookies = hub.cookie_jar(session.user_id, view).filter_cookies(url)
        if cookies:
            headers[hdrs.COOKIE] = "; ".join(f"{k}={m.value}" for k, m in cookies.items())
    elif cookie := request.headers.get(hdrs.COOKIE):
        headers[hdrs.COOKIE] = cookie
    if view.authorization is not None and hdrs.AUTHORIZATION not in headers:
        headers[hdrs.AUTHORIZATION] = view.authorization
    # Same header Supervisor ingress uses, so UIs built for ingress can adapt links
    headers["X-Ingress-Path"] = prefix
    if request.transport and (peername := request.transport.get_extra_info("peername")):
        forwarded = request.headers.get(hdrs.X_FORWARDED_FOR)
        headers[hdrs.X_FORWARDED_FOR] = (
            f"{forwarded}, {peername[0]}" if forwarded else str(peername[0])
        )
    headers[hdrs.X_FORWARDED_HOST] = request.headers.get(hdrs.X_FORWARDED_HOST, request.host)
    headers[hdrs.X_FORWARDED_PROTO] = request.headers.get(hdrs.X_FORWARDED_PROTO, request.scheme)
    return headers


def _response_headers(result: aiohttp.ClientResponse, ctx: _Context) -> CIMultiDict[str]:
    hub, session, view, prefix = ctx.hub, ctx.session, ctx.view, ctx.prefix
    headers = CIMultiDict(
        (name, value)
        for name, value in result.headers.items()
        if name not in RESPONSE_HEADERS_FILTER
    )
    set_cookies = result.headers.getall(hdrs.SET_COOKIE, ())
    if set_cookies and view.mode == MODE_ISOLATED:
        jar = hub.cookie_jar(session.user_id, view)
        for set_cookie in set_cookies:
            cookie: SimpleCookie = SimpleCookie()
            try:
                cookie.load(set_cookie)
            except Exception:  # noqa: BLE001 - malformed cookie from a device
                continue
            jar.update_cookies(cookie, result.url)
        hub.async_cookies_changed(session.user_id, view)
    else:
        view_path = prefix.rsplit("/", 1)[0] + "/"
        for set_cookie in set_cookies:
            headers.add(hdrs.SET_COOKIE, scope_cookie(set_cookie, view_path))
    if (location := result.headers.get(hdrs.LOCATION)) is not None:
        headers[hdrs.LOCATION] = rewrite_location(location, view.origin, prefix)
    if view.mode == MODE_ISOLATED:
        headers["Content-Security-Policy"] = ISOLATION_CSP
    headers["Referrer-Policy"] = REFERRER_POLICY
    if ctx.cross_origin:
        headers[hdrs.ACCESS_CONTROL_ALLOW_ORIGIN] = "*"
        headers[hdrs.ACCESS_CONTROL_EXPOSE_HEADERS] = "*"
    return headers


def scope_cookie(set_cookie: str, path: str) -> str:
    """Keep a site cookie under its view's prefix, never on HA's own paths."""
    parts = [
        part
        for part in set_cookie.split(";")
        if part.strip().split("=", 1)[0].strip().lower() not in ("path", "domain")
    ]
    parts.append(f" Path={path}")
    return ";".join(parts)


def rewrite_location(location: str, origin: URL, prefix: str) -> str:
    """Keep redirects to the site's own pages inside the proxy."""
    try:
        url = URL(location)
    except ValueError:
        return location
    if url.is_absolute():
        if url.origin() != origin:
            return location  # Redirect somewhere else entirely
    elif location.startswith("//") or not location.startswith("/"):
        return location  # Protocol-relative elsewhere, or relative: fine as is
    rest = url.raw_path + (f"?{url.raw_query_string}" if url.raw_query_string else "")
    if url.raw_fragment:
        rest += f"#{url.raw_fragment}"
    return prefix + rest


def html_rewriter(origin: URL, prefix: str) -> Callable[[bytes], bytes]:
    """Rewrite root-relative and same-site absolute links in HTML into the prefix."""
    site = re.escape(str(origin).encode())
    host_port = re.escape(
        f"//{origin.host}{'' if origin.is_default_port() else f':{origin.port}'}".encode()
    )
    pattern = re.compile(
        rb"(\s(?:src|href|action|formaction|poster)\s*=\s*[\"'])(?:"
        + site
        + rb"|"
        + host_port
        + rb")?/(?!/)",
        re.IGNORECASE,
    )
    replacement = rb"\1" + prefix.encode().replace(b"\\", b"\\\\") + b"/"
    return lambda body: pattern.sub(replacement, body)


# Runs before any script of a proxied page; see inject.js
INJECT_JS = (Path(__file__).parent / "inject.js").read_bytes()
if b"</script" in INJECT_JS.lower():
    raise RuntimeError("inject.js must not contain a closing script tag")


def _js(value: Any) -> str:
    """JSON that is safe to embed in a <script> element."""
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(chr(0x2028), "\\u2028")
        .replace(chr(0x2029), "\\u2029")
    )


def inject_script(ctx: _Context) -> bytes:
    """The script injected first into a proxied HTML page."""
    view = ctx.view
    isolated = view.mode == MODE_ISOLATED
    config: dict[str, Any] = {
        "prefix": ctx.prefix,
        "viewPath": ctx.prefix.rsplit("/", 1)[0] + "/",
        "site": str(view.origin),
        "isolated": isolated,
    }
    if isolated:
        jar = ctx.hub.cookie_jar(ctx.session.user_id, view)
        config["storage"] = ctx.hub.shim_storage(ctx.session.user_id, view.view_id)
        # Scripts only ever see cookies that are not HttpOnly
        config["cookies"] = {m.key: m.value for m in jar if not m.get("httponly")}
    return (
        b"<script>/* local_web_ui */(" + INJECT_JS + b")(" + _js(config).encode() + b");</script>"
    )


_CSS_URL = re.compile(rb"""(url\(\s*["']?|@import\s+["'])/(?!/)""", re.IGNORECASE)


def rewrite_css(body: bytes, prefix: str) -> bytes:
    """Root-relative url(...) and @import in stylesheets."""
    return _CSS_URL.sub(rb"\1" + prefix.encode() + b"/", body)


_HEAD_OPEN = re.compile(rb"<head(\s[^>]*)?>", re.IGNORECASE)
_HTML_OPEN = re.compile(rb"<html(\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE = re.compile(rb"<!doctype[^>]*>", re.IGNORECASE)


def inject_first(body: bytes, snippet: bytes) -> bytes:
    """Insert snippet so it runs before any script of the page."""
    for pattern in (_HEAD_OPEN, _HTML_OPEN, _DOCTYPE):
        if match := pattern.search(body):
            return body[: match.end()] + snippet + body[match.end() :]
    return snippet + body


async def read_limited(stream: aiohttp.StreamReader, limit: int) -> tuple[bytes, bool]:
    """Read until EOF or past limit; return the data and whether EOF was reached.

    StreamReader.read(n) may return fewer than n bytes before EOF, so it cannot
    be used to cap a body on its own.
    """
    chunks: list[bytes] = []
    total = 0
    while total <= limit:
        chunk = await stream.read(limit + 1 - total)
        if not chunk:
            return b"".join(chunks), True
        chunks.append(chunk)
        total += len(chunk)
    return b"".join(chunks), False


async def _handle_internal(request: web.Request, ctx: _Context, raw_path: str) -> web.Response:
    """Endpoints the storage shim writes to."""
    hub, session, view = ctx.hub, ctx.session, ctx.view
    headers = {hdrs.ACCESS_CONTROL_ALLOW_ORIGIN: "*"} if ctx.cross_origin else {}
    if request.method != hdrs.METH_POST or view.mode != MODE_ISOLATED:
        raise web.HTTPNotFound
    name = raw_path[len(INTERNAL_PATH_PREFIX) + 1 :]
    body, complete = await read_limited(request.content, MAX_SHIM_STORAGE_BYTES)
    if not complete:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_SHIM_STORAGE_BYTES, actual_size=len(body))
    if name == "storage":
        try:
            data = json.loads(body)
        except ValueError:
            raise web.HTTPBadRequest from None
        if not isinstance(data, dict):
            raise web.HTTPBadRequest
        hub.async_set_shim_storage(
            session.user_id, view.view_id, {str(k): str(v) for k, v in data.items()}
        )
    elif name == "cookie":
        cookie: SimpleCookie = SimpleCookie()
        try:
            cookie.load(body.decode())
        except Exception:  # noqa: BLE001 - whatever the page wrote
            raise web.HTTPBadRequest from None
        hub.cookie_jar(session.user_id, view).update_cookies(cookie, view.origin)
        hub.async_cookies_changed(session.user_id, view)
    else:
        raise web.HTTPNotFound
    return web.Response(status=204, headers=headers)


def _is_websocket(request: web.Request) -> bool:
    return (
        "upgrade" in request.headers.get(hdrs.CONNECTION, "").lower()
        and request.headers.get(hdrs.UPGRADE, "").lower() == "websocket"
    )


def _should_compress(content_type: str) -> bool:
    if content_type == "text/event-stream":
        return False  # Compression buffers, which would stall live events
    return content_type.startswith("text/") or content_type in (
        "application/javascript",
        "application/json",
        "application/xml",
        "image/svg+xml",
    )


async def _proxy_request(
    request: web.Request,
    ctx: _Context,
    client: aiohttp.ClientSession,
    url: URL,
    headers: CIMultiDict[str],
) -> web.StreamResponse:
    view, prefix = ctx.view, ctx.prefix
    async with client.request(
        request.method,
        url,
        headers=headers,
        data=request.content if request.body_exists else None,
        allow_redirects=False,
        timeout=UPSTREAM_TIMEOUT,
        skip_auto_headers={hdrs.CONTENT_TYPE, hdrs.USER_AGENT},
    ) as result:
        response_headers = _response_headers(result, ctx)
        content_type = (
            result.headers.get(hdrs.CONTENT_TYPE, "application/octet-stream")
            .partition(";")[0]
            .strip()
            .lower()
        )
        if must_be_empty_body(request.method, result.status):
            if hdrs.CONTENT_TYPE in result.headers:
                response_headers[hdrs.CONTENT_TYPE] = result.headers[hdrs.CONTENT_TYPE]
            return web.Response(status=result.status, headers=response_headers)

        length = result.headers.get(hdrs.CONTENT_LENGTH)
        is_html = content_type in ("text/html", "application/xhtml+xml")
        if is_html or (length is not None and int(length) <= MAX_SIMPLE_RESPONSE_SIZE):
            body, complete = await read_limited(result.content, MAX_SIMPLE_RESPONSE_SIZE)
            if complete:
                if is_html:
                    body = html_rewriter(view.origin, prefix)(body)
                    body = inject_first(body, inject_script(ctx))
                elif content_type == "text/css":
                    body = rewrite_css(body, prefix)
                response = web.Response(status=result.status, headers=response_headers, body=body)
                response.headers[hdrs.CONTENT_TYPE] = result.headers.get(
                    hdrs.CONTENT_TYPE, content_type
                )
                if _should_compress(content_type) and len(body) > 256:
                    response.enable_compression()
                return response
            # Too large to rewrite: stream what we read, then the rest
            return await _stream(request, result, response_headers, content_type, body)

        return await _stream(request, result, response_headers, content_type, b"")


async def _stream(
    request: web.Request,
    result: aiohttp.ClientResponse,
    headers: CIMultiDict[str],
    content_type: str,
    head: bytes,
) -> web.StreamResponse:
    """Stream large or unbounded bodies: downloads, chunked responses, SSE."""
    response = web.StreamResponse(status=result.status, headers=headers)
    response.headers[hdrs.CONTENT_TYPE] = result.headers.get(hdrs.CONTENT_TYPE, content_type)
    if _should_compress(content_type):
        response.enable_compression()
    await response.prepare(request)
    try:
        if head:
            await response.write(head)
        async for data, _ in result.content.iter_chunks():
            await response.write(data)
    except (aiohttp.ClientError, ConnectionError) as err:
        _LOGGER.debug("Stream ended: %s", err)
    return response


async def _proxy_websocket(
    request: web.Request,
    client: aiohttp.ClientSession,
    url: URL,
    headers: CIMultiDict[str],
) -> web.WebSocketResponse:
    protocols: Iterable[str] = [
        proto.strip()
        for proto in request.headers.get(hdrs.SEC_WEBSOCKET_PROTOCOL, "").split(",")
        if proto.strip()
    ]
    ws_url = url.with_scheme("wss" if url.scheme == "https" else "ws")
    async with client.ws_connect(
        ws_url,
        headers=headers,
        protocols=protocols,
        autoclose=False,
        autoping=False,
        max_msg_size=MAX_WEBSOCKET_MESSAGE_SIZE,
        timeout=aiohttp.ClientWSTimeout(ws_close=10),
    ) as ws_client:
        ws_server = web.WebSocketResponse(
            protocols=[ws_client.protocol] if ws_client.protocol else (),
            autoclose=False,
            autoping=False,
            max_msg_size=MAX_WEBSOCKET_MESSAGE_SIZE,
        )
        await ws_server.prepare(request)
        tasks = [
            asyncio.create_task(_websocket_forward(ws_server, ws_client)),
            asyncio.create_task(_websocket_forward(ws_client, ws_server)),
        ]
        _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in pending:
            task.cancel()
    await ws_server.close()
    return ws_server


async def _websocket_forward(
    ws_from: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
    ws_to: web.WebSocketResponse | aiohttp.ClientWebSocketResponse,
) -> None:
    try:
        async for msg in ws_from:
            if msg.type is aiohttp.WSMsgType.TEXT:
                await ws_to.send_str(msg.data)
            elif msg.type is aiohttp.WSMsgType.BINARY:
                await ws_to.send_bytes(msg.data)
            elif msg.type is aiohttp.WSMsgType.PING:
                await ws_to.ping(msg.data)
            elif msg.type is aiohttp.WSMsgType.PONG:
                await ws_to.pong(msg.data)
        # Iteration ends on close
        await ws_to.close(code=ws_from.close_code or aiohttp.WSCloseCode.OK)
    except (RuntimeError, ConnectionError):
        pass
