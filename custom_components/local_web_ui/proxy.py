"""Reverse proxy from Home Assistant's HTTP server to local web UIs.

Requests arrive at /api/local_web_ui/<view_id>/<session token>/<path>. The token
names a session created over the authenticated websocket API; the view decides
where the request goes. Nothing in the request can choose the upstream host.

Everything served here comes from Home Assistant's own origin, so the proxy is
strict about what it lets through in either direction:
- towards the device: no browser credentials (Cookie, Authorization) and no
  client-supplied identity or forwarding headers;
- towards the browser: only an allowlist of the device's response headers, plus
  security headers set here on every response, streamed or not.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterable
from dataclasses import dataclass
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
    MAX_SHIM_STORAGE_KEYS,
    MODE_ISOLATED,
    PROXY_URL_PREFIX,
)

try:
    from homeassistant.components.http.auth_util import async_user_not_allowed_do_auth
except ImportError:  # Home Assistant before the helper moved
    from homeassistant.components.http.auth import (  # type: ignore[attr-defined,no-redef]
        async_user_not_allowed_do_auth,
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
REQUEST_HEADERS_DROPPED = frozenset(
    name.lower()
    for name in HOP_BY_HOP
    | {
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
        # Credentials the browser holds for Home Assistant's origin (a reverse
        # proxy's Basic auth, SSO cookies) must never reach a device. Site cookies
        # live in the server-side jar; site logins come from the view's settings.
        hdrs.COOKIE,
        hdrs.AUTHORIZATION,
        # Set from Home Assistant's validated view of the request, never passed on
        hdrs.FORWARDED,
        hdrs.X_FORWARDED_FOR,
        hdrs.X_FORWARDED_HOST,
        hdrs.X_FORWARDED_PROTO,
        "X-Real-IP",
        "X-Ingress-Path",
        # Identity headers some reverse proxies add for the logged-in user
        "Remote-User",
        "Remote-Email",
        "Remote-Groups",
        "Remote-Name",
        "X-Remote-User",
        "X-Forwarded-User",
        "X-Forwarded-Email",
        "X-Forwarded-Preferred-Username",
        "X-Forwarded-Groups",
        "X-Forwarded-Access-Token",
    }
)
REQUEST_HEADER_PREFIXES_DROPPED = ("x-auth-request-", "cf-access-")

# Device response headers that may reach the browser. Everything else is dropped:
# the response comes from Home Assistant's origin, and several standard headers act
# on the whole origin (service worker scope, reporting, Clear-Site-Data, HSTS...).
RESPONSE_HEADERS_ALLOWED = frozenset(
    {
        "accept-ranges",
        "age",
        "cache-control",
        "content-disposition",
        "content-language",
        "content-range",
        "date",
        "etag",
        "expires",
        "last-modified",
        "pragma",
        "retry-after",
        "vary",
    }
)
# Custom X- headers carry device data (versions, checksums); these few are not data
RESPONSE_X_HEADERS_DROPPED = frozenset(
    {
        "x-frame-options",
        "x-content-type-options",
        "x-xss-protection",
        "x-ingress-path",
        "x-dns-prefetch-control",
        "x-permitted-cross-domain-policies",
    }
)

# Isolated views get an opaque origin: they cannot read Home Assistant's storage
# (which holds its access tokens) or script its pages. As a response header this
# also holds when a view is opened in a tab of its own. Popups may escape the
# sandbox so links to other sites work normally; a popup showing another proxied
# page is sandboxed again by this same header.
ISOLATION_CSP = (
    "sandbox allow-scripts allow-forms allow-popups "
    "allow-popups-to-escape-sandbox allow-modals allow-downloads"
)
# Set on every response, including streamed ones, which Home Assistant's own
# headers middleware cannot reach once they are prepared
SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    # The session token is in the URL
    "Referrer-Policy": "no-referrer",
}

MAX_SIMPLE_RESPONSE_SIZE = 4 * 1024 * 1024
MAX_WEBSOCKET_MESSAGE_SIZE = 4 * 1024 * 1024
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


def _error(status: int, text: str) -> web.Response:
    """Errors carry the sandbox too: nothing under the prefix runs unsandboxed."""
    return web.Response(
        status=status,
        text=text,
        headers={**SECURITY_HEADERS, "Content-Security-Policy": ISOLATION_CSP},
    )


async def _handle(request: web.Request) -> web.StreamResponse:
    try:
        return await _handle_request(request)
    except web.HTTPException as err:
        # Never let an HTTPUnauthorized escape: HA's ban middleware would count it
        status = 404 if err.status == 401 else err.status
        return _error(status, err.text or err.reason)


async def _handle_request(request: web.Request) -> web.StreamResponse:
    hass = request.app[KEY_HASS]
    hub: LocalWebUiHub | None = hass.data.get(DOMAIN)
    view_id = request.match_info["view_id"]
    token = request.match_info["token"]
    prefix = f"{PROXY_URL_PREFIX}/{view_id}/{token}"
    raw = request.rel_url.raw_path
    # Percent-encoded ids would decode to a valid session but shift the path below
    if not (raw == prefix or raw.startswith(prefix + "/")):
        raise web.HTTPNotFound
    if hub is None or (session := hub.sessions.touch(token, view_id)) is None:
        raise web.HTTPNotFound
    user = await hass.auth.async_get_user(session.user_id)
    if (
        user is None
        or not user.is_admin
        or async_user_not_allowed_do_auth(hass, user, request) is not None
    ):
        hub.sessions.revoke_user(session.user_id)
        raise web.HTTPNotFound
    if (view := hub.get_view(view_id)) is None:
        raise web.HTTPNotFound

    raw_path = raw[len(prefix) :] or "/"
    # Isolated pages have an opaque origin, so their requests to us are cross-origin
    cross_origin = request.headers.get(hdrs.ORIGIN) == "null"
    if (
        cross_origin
        and request.method == hdrs.METH_OPTIONS
        and hdrs.ACCESS_CONTROL_REQUEST_METHOD in request.headers
    ):
        return _preflight_response(request)
    ctx = _Context(hub, session, view, prefix, cross_origin)
    if raw_path.startswith("/" + INTERNAL_PATH_PREFIX):
        return await _handle_internal(request, ctx, raw_path)

    # Already percent-encoded: pass path and query through byte for byte
    query = request.rel_url.raw_query_string
    url = URL(str(view.origin) + raw_path + (f"?{query}" if query else ""), encoded=True)
    headers = _request_headers(request, ctx, url)
    client = hub.http_client(view)
    try:
        if _is_websocket(request):
            return await _proxy_websocket(request, ctx, client, url, headers)
        return await _proxy_request(request, ctx, client, url, headers)
    except (aiohttp.ClientError, OSError, TimeoutError) as err:
        _LOGGER.debug("Proxying %s for %s failed: %s", url.path, view.name, err)
        return _error(502, f"{view.name} is not reachable")


def _preflight_response(request: web.Request) -> web.Response:
    headers = {
        **SECURITY_HEADERS,
        "Content-Security-Policy": ISOLATION_CSP,
        hdrs.ACCESS_CONTROL_ALLOW_ORIGIN: "*",
        hdrs.ACCESS_CONTROL_ALLOW_METHODS: "GET, HEAD, POST, PUT, PATCH, DELETE",
        hdrs.ACCESS_CONTROL_MAX_AGE: "600",
    }
    if requested := request.headers.get(hdrs.ACCESS_CONTROL_REQUEST_HEADERS):
        headers[hdrs.ACCESS_CONTROL_ALLOW_HEADERS] = requested
    return web.Response(status=204, headers=headers)


def _request_headers(request: web.Request, ctx: _Context, url: URL) -> CIMultiDict[str]:
    headers = CIMultiDict(
        (name, value)
        for name, value in request.headers.items()
        if name.lower() not in REQUEST_HEADERS_DROPPED
        and not name.lower().startswith(REQUEST_HEADER_PREFIXES_DROPPED)
    )
    cookies = ctx.hub.cookie_jar(ctx.session.user_id, ctx.view).filter_cookies(url)
    if cookies:
        headers[hdrs.COOKIE] = "; ".join(f"{k}={m.value}" for k, m in cookies.items())
    if ctx.view.authorization is not None:
        headers[hdrs.AUTHORIZATION] = ctx.view.authorization
    # Same header Supervisor ingress uses, so UIs built for ingress can adapt links
    headers["X-Ingress-Path"] = ctx.prefix
    # Validated by Home Assistant's forwarded middleware against trusted_proxies
    if request.remote:
        headers[hdrs.X_FORWARDED_FOR] = request.remote
    headers[hdrs.X_FORWARDED_PROTO] = request.scheme
    return headers


def _response_headers(result: aiohttp.ClientResponse, ctx: _Context) -> CIMultiDict[str]:
    headers: CIMultiDict[str] = CIMultiDict()
    for name, value in result.headers.items():
        lower = name.lower()
        if lower in RESPONSE_HEADERS_ALLOWED or (
            lower.startswith("x-") and lower not in RESPONSE_X_HEADERS_DROPPED
        ):
            headers.add(name, value)
    if cache := headers.get(hdrs.CACHE_CONTROL):
        # Per-user content: never let a shared cache in front of HA keep it
        directives = [
            d
            for d in cache.split(",")
            if d.strip().split("=")[0].lower() not in ("public", "s-maxage")
        ]
        headers[hdrs.CACHE_CONTROL] = ", ".join(
            ["private", *(d.strip() for d in directives if d.strip())]
        )
    else:
        headers[hdrs.CACHE_CONTROL] = "private"
    if set_cookies := result.headers.getall(hdrs.SET_COOKIE, ()):
        ctx.hub.async_store_cookies(ctx.session.user_id, ctx.view, set_cookies, result.url)
    if (location := result.headers.get(hdrs.LOCATION)) is not None:
        headers[hdrs.LOCATION] = rewrite_location(location, ctx.view.origin, ctx.prefix)
    headers.update(SECURITY_HEADERS)
    if ctx.view.mode == MODE_ISOLATED:
        headers["Content-Security-Policy"] = ISOLATION_CSP
    if ctx.cross_origin:
        headers[hdrs.ACCESS_CONTROL_ALLOW_ORIGIN] = "*"
        headers[hdrs.ACCESS_CONTROL_EXPOSE_HEADERS] = "*"
    return headers


def rewrite_location(location: str, origin: URL, prefix: str) -> str:
    """Keep redirects to the site's own pages inside the proxy."""
    # A protocol-relative location takes the site's scheme so origins compare
    location_url = f"{origin.scheme}:{location}" if location.startswith("//") else location
    try:
        url = URL(location_url)
        if url.is_absolute() and url.origin() != origin:
            return location  # Redirect somewhere else entirely
    except ValueError:
        return location
    if url.is_absolute():
        pass  # Same site (checked above): keep it inside the proxy
    elif not location.startswith("/"):
        return location  # Relative: resolves inside the proxy as is
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
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace(chr(0x2028), "\\u2028")
        .replace(chr(0x2029), "\\u2029")
    )


def inject_script(ctx: _Context) -> bytes:
    """The script injected first into a proxied HTML page."""
    view = ctx.view
    isolated = view.mode == MODE_ISOLATED
    jar = ctx.hub.cookie_jar(ctx.session.user_id, view)
    config: dict[str, Any] = {
        "prefix": ctx.prefix,
        "viewPath": ctx.prefix.rsplit("/", 1)[0] + "/",
        "site": str(view.origin),
        "isolated": isolated,
        # Site cookies live server side in both modes; scripts see the ones that
        # are not HttpOnly, as they would in a browser
        "cookies": {m.key: m.value for m in jar if not m.get("httponly")},
    }
    if isolated:
        config["storage"] = ctx.hub.shim_storage(ctx.session.user_id, view.view_id)
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
    """Endpoints the injected script writes to (emulated storage and cookies)."""
    hub, session, view = ctx.hub, ctx.session, ctx.view
    name = raw_path[len(INTERNAL_PATH_PREFIX) + 1 :]
    if request.method != hdrs.METH_POST or name not in ("storage", "cookie"):
        raise web.HTTPNotFound
    if name == "storage" and view.mode != MODE_ISOLATED:
        raise web.HTTPNotFound  # Trusted pages have real localStorage
    body, complete = await read_limited(request.content, MAX_SHIM_STORAGE_BYTES)
    if not complete:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_SHIM_STORAGE_BYTES, actual_size=len(body))
    if name == "storage":
        try:
            data = json.loads(body)
        except ValueError:
            raise web.HTTPBadRequest from None
        if not isinstance(data, dict) or len(data) > MAX_SHIM_STORAGE_KEYS:
            raise web.HTTPBadRequest
        hub.async_set_shim_storage(
            session.user_id, view.view_id, {str(k): str(v) for k, v in data.items()}
        )
    else:
        hub.async_store_cookies(session.user_id, view, [body.decode(errors="replace")], view.origin)
    headers = {**SECURITY_HEADERS, "Content-Security-Policy": ISOLATION_CSP}
    if ctx.cross_origin:
        headers[hdrs.ACCESS_CONTROL_ALLOW_ORIGIN] = "*"
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
                    # The page now embeds this user's stored site data
                    response_headers[hdrs.CACHE_CONTROL] = "no-store"
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
    ctx: _Context,
    client: aiohttp.ClientSession,
    url: URL,
    headers: CIMultiDict[str],
) -> web.StreamResponse:
    if not ctx.hub.sessions.can_open_websocket(ctx.session.token):
        return _error(503, "Too many open connections for this web UI")
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
        ws_server.headers.update(SECURITY_HEADERS)
        await ws_server.prepare(request)
        # Closed when the session ends (expiry, logout), not only by either side
        release = ctx.hub.sessions.track_websocket(ctx.session.token, ws_server, ws_client)
        try:
            tasks = [
                asyncio.create_task(_websocket_forward(ws_server, ws_client)),
                asyncio.create_task(_websocket_forward(ws_client, ws_server)),
            ]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
        finally:
            release()
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
