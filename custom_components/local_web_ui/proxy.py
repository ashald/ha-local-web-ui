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
from collections.abc import AsyncIterator, Callable, Iterable
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
# Identity and token headers that authenticating reverse proxies add (oauth2-proxy,
# Cloudflare Access, authentik, AWS ALB, Azure App Service, Google IAP, Pomerium,
# Tailscale, mod_auth_openidc...)
REQUEST_HEADER_PREFIXES_DROPPED = (
    "x-auth-request-",
    "cf-access-",
    "x-authentik-",
    "x-amzn-oidc-",
    "x-ms-token-",
    "x-ms-client-principal",
    "oidc_",
    "oidc-",
    "x-goog-iap-",
    "x-goog-authenticated-",
    "x-pomerium-",
    "tailscale-",
    "x-webauth-",
    "x-forwarded-",
    "x-remote-",
    "remote-",
)

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
        # Instructions to a reverse proxy in front of Home Assistant (nginx, Apache,
        # lighttpd): internal redirects to any file or location, caching
        "x-accel-redirect",
        "x-accel-expires",
        "x-accel-limit-rate",
        "x-accel-charset",
        "x-sendfile",
        "x-lighttpd-send-file",
        "x-litespeed-location",
        "x-litespeed-cache-control",
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
# Until the response headers arrive; bodies and streams have no overall limit
UPSTREAM_HEADERS_TIMEOUT = 60
# Conditional requests would let the browser reuse a page with stale injected data
CONDITIONAL_HEADERS = (hdrs.IF_NONE_MATCH, hdrs.IF_MODIFIED_SINCE)
# Close codes that describe a local failure and must not be sent (RFC 6455 7.4.1)
RESERVED_CLOSE_CODES = frozenset({1005, 1006, 1015})
# nosniff (set on every response) makes the browser refuse a script or stylesheet
# whose type is not one it accepts for that kind. Small device web servers often
# send none, a generic one, or even a mismatched one (an SLZB-06 serves its
# stylesheet as text/javascript), so for these extensions the extension decides.
TYPES_BY_EXTENSION = {
    ".js": "text/javascript",
    ".mjs": "text/javascript",
    ".css": "text/css",
}
# Types already fine for those extensions, kept as the site sent them: a specific
# JavaScript type is the site's choice, and the browser accepts them all as scripts
JAVASCRIPT_TYPES = frozenset(
    {
        "text/javascript",
        "application/javascript",
        "application/ecmascript",
        "application/x-ecmascript",
        "application/x-javascript",
        "text/ecmascript",
        "text/jscript",
        "text/livescript",
        "text/x-ecmascript",
        "text/x-javascript",
    }
)
# When a device sends no type at all: without one, nosniff would make the browser
# download a page instead of showing it
PAGE_EXTENSIONS = (".html", ".htm")
_HTML_START = (b"<!doctype html", b"<html")


def _type_accepted(suffix: str, content_type: str) -> bool:
    """Whether a script's or stylesheet's type is one the browser accepts (nosniff)."""
    if suffix == ".css":
        return content_type == "text/css"
    return content_type in JAVASCRIPT_TYPES


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
        response = await _handle_request(request)
    except web.HTTPException as err:
        # Never let an HTTPUnauthorized escape: HA's ban middleware would count it
        status = 404 if err.status == 401 else err.status
        response = _error(status, err.text or err.reason)
    except Exception:
        # Still sandboxed, like every other response under the prefix
        _LOGGER.exception("Unexpected error proxying %s", request.rel_url.path)
        response = _error(500, "Internal error")
    if (
        request.headers.get(hdrs.ORIGIN) == "null"
        and not response.prepared
        and hdrs.ACCESS_CONTROL_ALLOW_ORIGIN not in response.headers
    ):
        response.headers.update(CORS_HEADERS)  # Errors too, so the page can read them
    return response


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
    view = hub.effective_view(view)

    if request.headers.get("Service-Worker") == "script":
        # Registered from HA's origin, a service worker could outlive the view and
        # intercept Home Assistant itself. inject.js refuses too, but a page can get
        # around that; the browser always marks the script request.
        raise web.HTTPForbidden
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


# Isolated pages have origin "null". Echoing it with credentials allowed lets the
# page's credentialed requests (fetch with credentials: "include", XHR
# withCredentials) work too; the browser holds no site credentials for these paths
# (cookies are server side), and the actual credential is the path token.
CORS_HEADERS = {
    hdrs.ACCESS_CONTROL_ALLOW_ORIGIN: "null",
    hdrs.ACCESS_CONTROL_ALLOW_CREDENTIALS: "true",
    hdrs.VARY: "Origin",
}


def _preflight_response(request: web.Request) -> web.Response:
    headers = {
        **SECURITY_HEADERS,
        **CORS_HEADERS,
        "Content-Security-Policy": ISOLATION_CSP,
        # The token was checked: any method the page wants to send to its device
        hdrs.ACCESS_CONTROL_ALLOW_METHODS: request.headers[hdrs.ACCESS_CONTROL_REQUEST_METHOD],
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
    if request.headers.get("Sec-Fetch-Dest") in ("document", "iframe"):
        for name in CONDITIONAL_HEADERS:
            headers.pop(name, None)
    if request.body_exists and request.content_length is not None:
        # Otherwise the body goes out chunked, which small device web servers
        # (ESPAsyncWebServer, esp_http_server...) cannot read
        headers[hdrs.CONTENT_LENGTH] = str(request.content_length)
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


def _response_headers(
    result: aiohttp.ClientResponse, ctx: _Context, is_html: bool
) -> CIMultiDict[str]:
    headers: CIMultiDict[str] = CIMultiDict()
    for name, value in result.headers.items():
        lower = name.lower()
        if lower in RESPONSE_HEADERS_ALLOWED or (
            lower.startswith("x-") and lower not in RESPONSE_X_HEADERS_DROPPED
        ):
            headers.add(name, value)
    if is_html:
        # The page will embed this user's site data: never reuse a stored copy
        headers[hdrs.CACHE_CONTROL] = "no-store"
        headers.popall(hdrs.ETAG, None)
        headers.popall(hdrs.LAST_MODIFIED, None)
    elif cache := headers.get(hdrs.CACHE_CONTROL):
        # Per-user content: never let a shared cache in front of HA keep it
        directives = [
            d.strip()
            for d in cache.split(",")
            if d.strip()
            and d.strip().split("=")[0].lower() not in ("public", "private", "s-maxage")
        ]
        headers[hdrs.CACHE_CONTROL] = ", ".join(["private", *directives])
    else:
        headers[hdrs.CACHE_CONTROL] = "private"
    if set_cookies := result.headers.getall(hdrs.SET_COOKIE, ()):
        ctx.hub.async_store_cookies(ctx.session.user_id, ctx.view, set_cookies, result.url)
    if (location := result.headers.get(hdrs.LOCATION)) is not None:
        origin = ctx.view.origin
        if (upgraded := https_upgrade(location, origin)) is not None:
            # Printers, NAS and routers often move to https on the same host: follow
            # them inside the proxy, instead of sending the browser to the LAN address
            ctx.hub.async_upgrade_to_https(ctx.view, upgraded)
            origin = upgraded
        headers[hdrs.LOCATION] = rewrite_location(location, origin, ctx.prefix)
    headers.update(SECURITY_HEADERS)
    if ctx.view.mode == MODE_ISOLATED:
        headers["Content-Security-Policy"] = ISOLATION_CSP
    if ctx.cross_origin:
        # With credentials allowed, "*" is not a wildcard here: name the headers
        exposed = sorted({name.lower() for name in headers} - {"set-cookie"})
        if vary := headers.get(hdrs.VARY):
            headers[hdrs.VARY] = f"{vary}, Origin"
        headers.update({k: v for k, v in CORS_HEADERS.items() if k != hdrs.VARY})
        headers.setdefault(hdrs.VARY, "Origin")
        if exposed:
            headers[hdrs.ACCESS_CONTROL_EXPOSE_HEADERS] = ", ".join(exposed)
    return headers


def https_upgrade(location: str, origin: URL) -> URL | None:
    """The https origin of a redirect from an http site to https on the same host."""
    if origin.scheme != "http":
        return None
    try:
        url = URL(location)
    except ValueError:
        return None
    if url.scheme != "https" or not url.host or url.host.lower() != (origin.host or "").lower():
        return None
    return url.origin()


def rewrite_location(location: str, origin: URL, prefix: str) -> str:
    """Keep redirects to the site's own pages inside the proxy."""
    # A protocol-relative location takes the site's scheme so origins compare
    location_url = f"{origin.scheme}:{location}" if location.startswith("//") else location
    try:
        url = URL(location_url)
        # Compared by parts: "http://host:80" is the same site as "http://host"
        if url.is_absolute() and (url.scheme, url.host, url.port) != (
            origin.scheme,
            origin.host,
            origin.port,
        ):
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
    """Rewrite root-relative and same-site absolute links in HTML into the prefix.

    Every pattern stops at "<" and ">", so its cost stays linear in the size of
    the page whatever a device sends.
    """
    site = (
        rb"(?:"
        + re.escape(str(origin).encode())
        + rb"|"
        + re.escape(
            f"//{origin.host}{'' if origin.is_default_port() else f':{origin.port}'}".encode()
        )
        + rb")?"
    )
    link = re.compile(
        # Quoted values, or unquoted ones written right after "="
        rb"(\s(?:" + _LINK_ATTRIBUTES + rb")(?:\s*=\s*[\"']|=))" + site + rb"/(?!/)",
        re.IGNORECASE,
    )
    refresh = re.compile(
        rb"(<meta\s[^<>]*?content\s*=\s*[\"']?\s*\d+\s*[;,]\s*url\s*=\s*[\"']?)"
        + site
        + rb"/(?!/)",
        re.IGNORECASE,
    )
    srcset_item = re.compile(rb"((?:^|,)\s*)" + site + rb"/(?!/)")
    to_prefix = rb"\1" + prefix.encode().replace(b"\\", b"\\\\") + b"/"

    def rewrite(body: bytes) -> bytes:
        body = link.sub(to_prefix, body)
        body = refresh.sub(to_prefix, body)
        body = _SRCSET.sub(lambda m: m[1] + srcset_item.sub(to_prefix, m[2]), body)
        # Stylesheets inside the page: <style> blocks and style attributes
        body = _CSS_URL.sub(to_prefix, body)
        body = _META_REFERRER.sub(rb"\1x-referrer-removed", body)
        return _REFERRERPOLICY_ATTR.sub(rb"\1x-referrerpolicy-removed", body)

    return rewrite


_LINK_ATTRIBUTES = rb"src|href|action|formaction|poster|data|background|manifest"
_SRCSET = re.compile(rb"(\s(?:srcset|imagesrcset)\s*=\s*[\"'])([^\"'<>]*)", re.IGNORECASE)
# A page's own referrer policy could send its URL (with the token) to other sites
_META_REFERRER = re.compile(rb"(<meta\s[^<>]*?name\s*=\s*[\"']?)referrer", re.IGNORECASE)
_REFERRERPOLICY_ATTR = re.compile(rb"(<[a-z][^<>]*?\s)referrerpolicy(?=\s*=)", re.IGNORECASE)


# Runs before any script of a proxied page; see inject.js
INJECT_JS = (Path(__file__).parent / "inject.js").read_bytes()
if b"</script" in INJECT_JS.lower() or b"]]>" in INJECT_JS:
    raise RuntimeError("inject.js must not contain a closing script tag or CDATA end")


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


def inject_script(ctx: _Context, xhtml: bool = False) -> bytes:
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
        # Lets the page tell whether the previous page's last write made it in
        config["writes"] = ctx.hub.applied_writes(ctx.session.user_id, view.view_id)
    code = b"(" + INJECT_JS + b")(" + _js(config).encode() + b");"
    if xhtml:
        # In XML, the script's "<" and "&" would end the document
        code = b"//<![CDATA[\n" + code + b"\n//]]>"
    return b"<script>/* local_web_ui */" + code + b"</script>"


_CSS_URL = re.compile(rb"""(url\(\s*["']?|@import\s+["'])/(?!/)""", re.IGNORECASE)


def rewrite_css(body: bytes, prefix: str) -> bytes:
    """Root-relative url(...) and @import in stylesheets."""
    return _CSS_URL.sub(rb"\1" + prefix.encode() + b"/", body)


_HEAD_OPEN = re.compile(rb"<head(\s[^<>]*)?>", re.IGNORECASE)
_HTML_OPEN = re.compile(rb"<html(\s[^<>]*)?>", re.IGNORECASE)
_DOCTYPE = re.compile(rb"<!doctype[^<>]*>", re.IGNORECASE)


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
    """Endpoints the injected script uses (emulated storage and cookies).

    POST storage  {"w": write id, "p": page id, "s": sequence number,
                   "set": {key: value or null}, "clear": bool}
    GET  storage?w=<write id>  the current storage, once that write has arrived
    POST cookie   one Set-Cookie style string, from document.cookie
    """
    hub, session, view = ctx.hub, ctx.session, ctx.view
    name = raw_path[len(INTERNAL_PATH_PREFIX) + 1 :]
    if name == "storage" and view.mode != MODE_ISOLATED:
        raise web.HTTPNotFound  # Trusted pages have real localStorage
    headers = {**SECURITY_HEADERS, "Content-Security-Policy": ISOLATION_CSP}
    if ctx.cross_origin:
        headers.update(CORS_HEADERS)
    if name == "storage" and request.method == hdrs.METH_GET:
        if write_id := request.query.get("w"):
            await hub.async_wait_for_storage_write(session.user_id, view.view_id, write_id)
        headers[hdrs.CACHE_CONTROL] = "no-store"
        return web.json_response(hub.shim_storage(session.user_id, view.view_id), headers=headers)
    if request.method != hdrs.METH_POST or name not in ("storage", "cookie"):
        raise web.HTTPNotFound
    # The limit counts characters; UTF-8 takes up to 4 bytes for one
    body, complete = await read_limited(request.content, 4 * MAX_SHIM_STORAGE_BYTES)
    if not complete:
        raise web.HTTPRequestEntityTooLarge(max_size=MAX_SHIM_STORAGE_BYTES, actual_size=len(body))
    if name == "storage":
        try:
            data = json.loads(body)
        except ValueError:
            raise web.HTTPBadRequest from None
        changes = data.get("set") if isinstance(data, dict) else None
        if not isinstance(changes, dict):
            raise web.HTTPBadRequest
        page = None
        if isinstance(data.get("p"), str) and type(data.get("s")) is int:
            page = (data["p"][:32], data["s"])
        if not hub.async_apply_storage_write(
            session.user_id,
            view.view_id,
            str(data.get("w") or "")[:64],
            {str(k): None if v is None else str(v) for k, v in changes.items()},
            clear=bool(data.get("clear")),
            page=page,
        ):
            raise web.HTTPRequestEntityTooLarge(
                max_size=MAX_SHIM_STORAGE_BYTES, actual_size=len(body)
            )
    else:
        hub.async_store_cookies(
            session.user_id,
            view,
            [body.decode(errors="replace")],
            view.origin,
            from_script=True,
        )
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
    limiter = ctx.hub.request_limiter(ctx.view)
    await limiter.acquire()
    held = True
    try:
        # The time limit runs from the end of the upload, however long that takes
        # (firmware over a slow link), to the response headers
        loop = asyncio.get_running_loop()
        async with asyncio.timeout(None) as deadline:

            def start_deadline() -> None:
                deadline.reschedule(loop.time() + UPSTREAM_HEADERS_TIMEOUT)

            body = None
            if request.body_exists:
                body = _upload(request.content, start_deadline)
            else:
                start_deadline()
            result = await client.request(
                request.method,
                url,
                headers=headers,
                data=body,
                allow_redirects=False,
                timeout=UPSTREAM_TIMEOUT,
                skip_auto_headers={hdrs.CONTENT_TYPE, hdrs.USER_AGENT},
            )
        async with result:
            response, stream_head = await _respond(request, ctx, result, url)
            if stream_head is None:
                if ctx.view.mode == MODE_ISOLATED:
                    # Sent here, before Home Assistant's headers middleware would add
                    # X-Frame-Options: SAMEORIGIN, which an isolated page's own
                    # frames can never meet (its origin is opaque)
                    await response.prepare(request)
                    await response.write_eof()
                return response
            # Streams (downloads, server-sent events) can last long; let others in
            limiter.release()
            held = False
            return await _stream(request, ctx, result, response, stream_head)
    finally:
        if held:
            limiter.release()


async def _upload(content: aiohttp.StreamReader, done: Callable[[], None]) -> AsyncIterator[bytes]:
    async for chunk in content.iter_any():
        yield chunk
    done()


async def _respond(
    request: web.Request, ctx: _Context, result: aiohttp.ClientResponse, url: URL
) -> tuple[web.StreamResponse, bytes | None]:
    """The response for a device response: complete, or to stream (with its head)."""
    view, prefix = ctx.view, ctx.prefix
    content_type_header = result.headers.get(hdrs.CONTENT_TYPE, "")
    content_type = content_type_header.partition(";")[0].strip().lower()
    suffix = Path(url.path).suffix.lower()
    untyped = hdrs.CONTENT_TYPE not in result.headers
    if (fixed := TYPES_BY_EXTENSION.get(suffix)) and not _type_accepted(suffix, content_type):
        content_type = content_type_header = fixed
    elif untyped and (url.path.endswith("/") or suffix in PAGE_EXTENSIONS):
        content_type = content_type_header = "text/html"
    is_html = content_type in ("text/html", "application/xhtml+xml")
    if must_be_empty_body(request.method, result.status):
        response_headers = _response_headers(result, ctx, is_html)
        if content_type_header:
            response_headers[hdrs.CONTENT_TYPE] = content_type_header
        return web.Response(status=result.status, headers=response_headers), None

    length = result.headers.get(hdrs.CONTENT_LENGTH)
    body = b""
    # Pages and stylesheets are rewritten, so they are buffered even when their
    # length is not known up front (chunked, as small device servers often send)
    buffered = (
        is_html
        or content_type == "text/css"
        or (length is not None and length.isdigit() and int(length) <= MAX_SIMPLE_RESPONSE_SIZE)
    )
    if buffered:
        body, complete = await read_limited(result.content, MAX_SIMPLE_RESPONSE_SIZE)
        if untyped and not suffix and body.lstrip()[:14].lower().startswith(_HTML_START):
            # An untyped page at a path without an extension
            is_html = True
            content_type = content_type_header = "text/html"
    response_headers = _response_headers(result, ctx, is_html)
    if buffered:
        if is_html:
            # Also when too large to buffer: the start of the page gets the script
            script = inject_script(ctx, xhtml=content_type == "application/xhtml+xml")
            body = await _off_loop(
                ctx, lambda: inject_first(html_rewriter(view.origin, prefix)(body), script), body
            )
        if complete:
            if content_type == "text/css":
                body = await _off_loop(ctx, lambda: rewrite_css(body, prefix), body)
            response = web.Response(status=result.status, headers=response_headers, body=body)
            response.headers[hdrs.CONTENT_TYPE] = content_type_header or "application/octet-stream"
            if _should_compress(content_type) and len(body) > 256:
                response.enable_compression()
            return response, None

    # Not compressed: compressing holds data back, which stalls live streams
    response = web.StreamResponse(status=result.status, headers=response_headers)
    response.headers[hdrs.CONTENT_TYPE] = content_type_header or "application/octet-stream"
    return response, body


async def _off_loop(ctx: _Context, rewrite: Callable[[], bytes], body: bytes) -> bytes:
    """Rewrite large bodies in a worker thread, so Home Assistant keeps running."""
    if len(body) < 256 * 1024:
        return rewrite()
    return await ctx.hub.hass.async_add_executor_job(rewrite)


class _StreamCloser:
    """Ends a streamed response early: the browser sees a broken, not a complete, body."""

    def __init__(self, request: web.Request, result: aiohttp.ClientResponse) -> None:
        self._request = request
        self._result = result

    async def close(self) -> None:
        self._result.close()
        if (transport := self._request.transport) is not None:
            transport.abort()


async def _stream(
    request: web.Request,
    ctx: _Context,
    result: aiohttp.ClientResponse,
    response: web.StreamResponse,
    head: bytes,
) -> web.StreamResponse:
    """Stream large or unbounded bodies: downloads, chunked responses, SSE."""
    # Ends with the session (expiry, logout), like WebSockets
    slot = ctx.hub.sessions.track_stream(ctx.session.token)
    closer = _StreamCloser(request, result)
    slot.add(closer)
    try:
        if slot.ended:
            await closer.close()
            return response
        await response.prepare(request)
        if head:
            await response.write(head)
        async for data, _ in result.content.iter_chunks():
            await response.write(data)
    except (aiohttp.ClientError, ConnectionError) as err:
        # Cut off by the device: do not let it look like a complete download
        _LOGGER.debug("Stream ended: %s", err)
        await closer.close()
    finally:
        slot.release()
    return response


async def _proxy_websocket(
    request: web.Request,
    ctx: _Context,
    client: aiohttp.ClientSession,
    url: URL,
    headers: CIMultiDict[str],
) -> web.StreamResponse:
    if (slot := ctx.hub.sessions.reserve_websocket(ctx.session.token)) is None:
        return _error(503, "Too many open connections for this web UI")
    protocols: Iterable[str] = [
        proto.strip()
        for proto in request.headers.get(hdrs.SEC_WEBSOCKET_PROTOCOL, "").split(",")
        if proto.strip()
    ]
    ws_url = url.with_scheme("wss" if url.scheme == "https" else "ws")
    try:
        async with client.ws_connect(
            ws_url,
            headers=headers,
            protocols=protocols,
            autoclose=False,
            autoping=False,
            max_msg_size=MAX_WEBSOCKET_MESSAGE_SIZE,
            timeout=aiohttp.ClientWSTimeout(ws_close=10),
        ) as ws_client:
            slot.add(ws_client)
            ws_server = web.WebSocketResponse(
                protocols=[ws_client.protocol] if ws_client.protocol else (),
                autoclose=False,
                autoping=False,
                max_msg_size=MAX_WEBSOCKET_MESSAGE_SIZE,
            )
            ws_server.headers.update(SECURITY_HEADERS)
            await ws_server.prepare(request)
            slot.add(ws_server)
            if slot.ended:
                # The session ended while connecting (expiry, logout)
                await slot.close()
            tasks = [
                asyncio.create_task(_websocket_forward(ws_server, ws_client)),
                asyncio.create_task(_websocket_forward(ws_client, ws_server)),
            ]
            _, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in pending:
                task.cancel()
    finally:
        slot.release()
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
        # Iteration ends on close. A lost connection has a local-only code.
        code = ws_from.close_code
        if code is None or code < 1000 or code in RESERVED_CLOSE_CODES:
            code = aiohttp.WSCloseCode.GOING_AWAY
        await ws_to.close(code=code)
    except RuntimeError, ConnectionError:
        pass
