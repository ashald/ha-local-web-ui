# Local Web UIs: design

A Home Assistant custom integration, distributed through HACS, that opens the web UIs of
local devices and sites inside HA: ESPHome `web_server`, WLED, printers, routers and so on.
HA proxies the traffic itself, so the UIs work wherever HA's frontend works:
- remotely, through Nabu Casa or a reverse proxy;
- over HTTPS, with no mixed-content blocking;
- in the companion apps.

It needs no firmware changes. It is "variant 2". Variant 1, the deeper version that tunnels
over the ESPHome native API, lives in `ashald/esphome@variant-1`, and this integration is
meant to become its carrier later.

Requires Home Assistant 2026.9 or newer (tested with 2026.9.3).

## Concepts

**View.** A web UI HA can open. It has:
- an id and a name;
- a base URL (`http(s)://host[:port]`) and an entry path;
- a mode: `isolated` (default) or `trusted`;
- `verify_ssl`;
- optional basic-auth credentials;
- an optional device link;
- an optional "show in sidebar" flag.

**Entries.** Since 0.3 there are two kinds of config entry (entry version 2):
- **The hub** (`data {kind: hub}`, `unique_id` `hub`, at most one) holds the global
  options: discovery, the default for device page links, and the sidebar entry of the main
  panel (shown or not, its title and icon).
- **A web UI**, one entry per view. The view id is the entry id. Its settings are the
  entry's options, edited with the entry's *Configure* button.

Web UIs come from two sources:
- **Device web UIs** (`data {device_id}`, `unique_id` `device:<device_id>`) come from
  discovery. Any device whose `configuration_url` is `http(s)` to a local host qualifies
  (see the discovery filter below). Examples:
  - ESPHome sets this to `http://<host>:<web_server port>` whenever `web_server` is enabled;
  - WLED, SMLIGHT, many printers and routers set it too.

  The hub starts an `integration_discovery` flow for each such device, once per HA run, so
  it shows under *Discovered* with **Add** and **Ignore**; ignoring is HA's own
  `SOURCE_IGNORE` entry. There is one offer per URL: since 2026.8 a device known to several
  integrations is one device per integration, each with the same link, so only the first is
  offered. Child devices (2026.9) are ignored; they share their parent's link. Turning
  discovery off aborts pending offers. The URL follows the device's current link, so a
  device that moves to a new address keeps working.
- **Manual web UIs** (`data {}`) are added with *Add entry*. Credentials typed into the URL
  (`http://user:pass@host/`) are moved into the username and password fields.

All entries share one `LocalWebUiHub`, created in `async_setup` and kept in
`hass.data[DOMAIN]`. It is active while any entry is loaded.

**Migration from 0.2** (entry version 1, one entry with a `view` subentry per web UI):
each subentry is re-created as a web UI entry through an import flow, keeping its
settings; its per-device link choice becomes the `visit_link` option; its cookies and
storage move to the new id. Hidden discovered views become ignored entries. The old entry
becomes the hub.

**Session.** Grants one HA user's browser access to one view:
- a random 256-bit token, created over the WebSocket API by admins only;
- expires after 5 minutes of inactivity and 12 hours after creation, whichever comes first;
- kept alive by proxied requests and by panel keepalives;
- ends as soon as the HA login (refresh token) that created it is revoked, and its
  WebSockets are closed with it;
- every proxied request re-checks that the user is still an active admin and is allowed
  to log in from where the request comes from (`local_only` users).

## URL layout

```
/api/local_web_ui/<view_id>/<session_token>/<path…>
```

- **The token is in the path, not a cookie.** Iframes cannot send HA's bearer token, and
  isolated (opaque-origin) pages never send `SameSite=Strict`/`Lax` cookies (verified in
  Chromium). A path token also makes the UI's relative URLs carry the token automatically.
  Supervisor ingress cannot isolate apps for exactly this reason.
- **The route is registered directly on HA's aiohttp router**, not as a
  `HomeAssistantView`. HA attaches CORS handling to views, which claims `OPTIONS` (only
  `/api/hassio_ingress/` is exempt), and isolated pages need their preflights answered by us.
- **It stays under `/api/`**, which HA's frontend service worker never caches.
- The path must match the prefix byte for byte: percent-encoded ids are rejected.
- A bad or expired token is a 404, never a 401, which HA's ban middleware would count as a
  failed login.

## Isolation modes

| | isolated (default) | trusted |
|---|---|---|
| **Origin of the page** | opaque (`null`): CSP `sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads` on every response (errors included), plus the same iframe `sandbox`. Holds in a new tab too. | HA's origin, like Supervisor ingress. The page can act as the logged-in HA user. The form makes you confirm. |
| **Site cookies** | Server-side jar per (HA user, view), persisted in `.storage`. The browser never holds them; the proxy strips `Set-Cookie` and sends `Cookie` itself. | Same. Device cookies never land in, or read, the browser's jar for HA's origin. |
| **`document.cookie`** | Emulated, backed by the same jar (non-HttpOnly cookies only). | Same. |
| **localStorage** | Emulated and stored server side per (HA user, view); see below. | Native: shared with HA and every other trusted view. |
| **sessionStorage** | Emulated, per page (does not survive a reload). | Native. |
| **IndexedDB, Cache Storage, service workers** | Removed, so feature detection falls back (they would throw). | Native, except service workers, which are refused in both modes: one registered from HA's origin could intercept HA itself. |
| **CORS** | Requests carry `Origin: null`. The proxy answers preflights itself and adds `Access-Control-Allow-Origin: null` with `Access-Control-Allow-Credentials: true` (errors included), so credentialed `fetch`/XHR work. The browser holds no site credentials for these paths; the credential is the path token. | Not needed. |

**Emulated localStorage.** The data is inlined into each HTML page the proxy serves.
- **Writes.** The page writes back changes (not snapshots) to `POST …/__lwu/storage`:
  `{"w": write id, "p": page id, "s": sequence number, "set": {key: value or null},
  "clear": bool}`. They are debounced by 250 ms, sent at once for values over 16 KiB and
  after `pagehide`, and flushed when the page is hidden. One write is in flight at a time;
  one that must go out while another is still in flight (the page is going away) carries
  the whole data instead, and the server ignores a write that arrives after a later one of
  the same page. `keepalive` is used within the browser's 64 KiB budget; a write lost to
  the network is followed by the whole data.
- **Save, then reload.** The last write of a page can reach HA after the next page was
  rendered. So the page remembers its view and last write id in `window.name` (which
  survives navigation; the page's own name is kept), the server lists recently received
  write ids in the injected config, and a page of the same view whose copy predates that
  write fetches fresh data with a synchronous `GET …/__lwu/storage?w=<id>`, which waits up
  to 2 s for it.
- **Limits** per (user, view): 1000 keys and 1 Mi characters.

## Proxy behaviour

**Request headers**
- Hop-by-hop headers, `Host`, `Origin` (device UIs such as ESPHome's reject HA's origin as
  cross-origin) and `Referer` (which contains the token) are dropped.
- Credentials the browser holds for HA's origin never reach a device: `Cookie`,
  `Authorization` (a reverse proxy's Basic auth, for example) and the identity and token
  headers of authenticating proxies (`Remote-*`, `X-Forwarded-*`, `X-Auth-Request-*`,
  `Cf-Access-*`, authentik, AWS ALB OIDC, Azure App Service, Google IAP, Pomerium,
  Tailscale, mod_auth_openidc…) are dropped.
  Site cookies come from the server-side jar and site logins from the view's settings
  (sent as Basic auth).
- Client-sent `Forwarded`/`X-Forwarded-*`/`X-Real-IP` are dropped and replaced with HA's
  own view of the request. `X-Ingress-Path` is set to the prefix, as Supervisor ingress
  does.
- `If-None-Match`/`If-Modified-Since` are dropped for document and iframe loads, so pages
  with injected per-user data are never revalidated.
- Request bodies keep their `Content-Length`: small device web servers (ESPAsyncWebServer,
  esp_http_server) cannot read chunked uploads.

**Response headers**
- Only an allowlist of device headers reaches the browser (`Cache-Control`, `ETag`,
  `Content-Disposition`, `Content-Range`, `Vary`… and custom `X-*` data headers). The
  response comes from HA's origin, and several standard headers act on the whole origin
  (`Clear-Site-Data`, `NEL`/`Report-To`, `Strict-Transport-Security`,
  `Service-Worker-Allowed`…).
- `Cache-Control` is made `private`; HTML gets `no-store`, without `ETag`/`Last-Modified`.
- `Set-Cookie` always goes to the server-side jar.
- `Location` headers pointing at the target origin (absolute, protocol-relative or
  root-relative) are rewritten under the prefix.
- The proxy sets `X-Content-Type-Options: nosniff` and `Referrer-Policy: no-referrer` on
  every response itself, because HA's headers middleware cannot reach streamed responses.
- A missing or generic `Content-Type` for `.js`/`.mjs`/`.css` is fixed up from the
  extension, since `nosniff` would make browsers refuse them. A missing one for a
  directory, `.html` or a body that starts like HTML becomes `text/html`.
- Complete responses of isolated views are sent by the proxy itself, before HA's headers
  middleware would add `X-Frame-Options: SAMEORIGIN`, which a page's own frames can never
  meet when its origin is opaque.

**Server-side rewriting**
- HTML: root-relative and same-site absolute or protocol-relative URLs in
  `src`/`href`/`action`/`formaction`/`poster`/`data`/`background`/`manifest` attributes
  (quoted or not, `<base href="/">` included), `srcset`, `<meta http-equiv="refresh">`, and
  `url(...)`/`@import` in `<style>` blocks and `style` attributes are rewritten under the
  prefix. `<meta name="referrer">` tags and `referrerpolicy` attributes are neutralized.
- Every pattern stops at `<` and `>`, so rewriting stays linear in the size of the page
  whatever a device sends; pages over 256 KiB are rewritten in a worker thread.
- The script below is injected right after `<head>` (or `<html>`, or the doctype), so
  it runs before any script of the page. HTML over 4 MiB gets it too, in its first 4 MiB,
  and the rest is streamed.
- CSS: root-relative `url(...)` and `@import`, chunked stylesheets included.
- XHTML: the script is wrapped in CDATA.

**Runtime rewriting** (`inject.js`)
- The script keeps URLs that the page builds at runtime under the prefix: `fetch`, XHR,
  `EventSource`, `WebSocket`, `history.pushState`/`replaceState`, `window.open`, `src`/`href`/
  `action` set through `setAttribute` or properties, and link clicks and form submits.
- It handles root-relative paths, URLs built from `location.host` or `location.origin`
  (which is HA's host even when isolated), whatever their scheme (`"ws://" +
  location.host` on an HTTPS page becomes `wss:`), and absolute URLs to the device.
- It sets `no-referrer` on requests and elements where the page asks for another
  policy, and `fetch(new Request(...))` with a body keeps working when its URL changes.
- It provides the storage and cookie emulation above.

**Bodies and streaming**
- Bodies up to 4 MiB are buffered, and compressible types are compressed.
- Larger or unknown-length bodies, and SSE, are streamed without compression, which would
  hold data back.
- Streams end with their session, like WebSockets. A stream the device cuts off is cut
  off for the browser too, so a truncated download does not look complete.
- WebSockets connect upstream first, then accept the browser with the negotiated
  subprotocol, and relay frames. At most 8 per session. A side that drops without a close
  frame is relayed as close code 1001, never as the reserved 1006.

**Upstream connections**
- Manual web UIs use HA's shared aiohttp connectors (so `.local` names resolve over mDNS),
  one per `verify_ssl` value.
- Device web UIs use their own connector with a resolver that only returns LAN
  addresses (it wraps HA's resolver, so mDNS still works), so a device-supplied host name
  cannot resolve to something else.
- `DummyCookieJar`, because cookies are per user and view, as above.
- At most 6 concurrent requests per site; small device web servers have few sockets.
  Streams release their slot once the headers arrive.
- A 10 s connect timeout, 60 s from the end of the upload to the response headers (an
  upload may take as long as it needs), and no overall limit.

**Safety**
- Targets always come from view config or the device registry, never from the request.
- Everything is admin only in v0.1.
- Unexpected errors are answered with the same sandboxed error response as others.

**Discovery filter.** A discovered URL must point at one of:
- a private IPv4/IPv6 address, or `100.64.0.0/10` (Tailscale/CGNAT);
- a `.local`, `.lan`, `.home`, `.home.arpa`, `.internal` or `.localdomain` name;
- a single-label hostname.

It excludes loopback, link-local, multicast, cloud metadata addresses and names,
Supervisor's `172.30.32.0/23` network, `*.localhost`, app hostnames (`core-*`, `local-*`,
`a0d7b954-*`, `supervisor`, `homeassistant`) and Home Assistant's own URLs. IPv4-mapped
IPv6 addresses are checked as IPv4. Discovered views skip TLS verification, because LAN
devices rarely have trusted certificates. Manual web UIs are not filtered.

## Storage

Two private stores (`private=True`, `atomic_writes=True`):
- `.storage/local_web_ui`: original device links;
- `.storage/local_web_ui.jar`: site cookies and emulated localStorage per (user, view).

A removed user's data and a removed web UI's data (every user's) are dropped. Removing the
last entry deletes both stores.

## Lifecycle

- The proxy route, the static path of the panel and the WebSocket commands are registered
  once, in `async_setup`; they cannot be unregistered, and they look the hub up per call.
- Option changes are applied in place by the update listener: views are rebuilt from the
  entries, device links and linked devices re-synced, and sidebar panels added, replaced
  or removed. Open views keep working.
- The main panel stays registered while any entry is loaded, even when hidden from the
  sidebar, because it serves the device pages' "Visit" links.
- Sessions live in `hass.data`, outside the entry, so they also survive a reload.
- The panel module URL carries a hash of its content, so it can be cached for long.

## Frontend

A panel is registered with `panel_custom` at `/local-web-ui`. It is a plain web component
with no build step.
- **List view:** "Devices" (with device name and area) and "Sites" (manual), plus a
  notice when discoveries wait to be added. Actions: open, open in new tab, settings (the
  entry's page), device page, web UI device page, forget saved logins and data.
- **View page:** a toolbar with back, title, a mode badge, reload and open in new tab,
  plus the iframe (with `sandbox` when isolated).
- **Sidebar entries:** web UIs flagged "show in sidebar" get their own entry at
  `/local-web-ui-<view_id>`, using the same component in single-view mode.

**Device page "Visit" link** (optional, on by default)
- Where enabled, a device's `configuration_url` points at
  `homeassistant://local-web-ui/<view_id>`, so the device page's "Visit" button opens the
  view here.
- It is controlled at two levels:
  - a **global switch** in the hub options, `link_device_pages`;
  - a **per web UI choice** in its options, `visit_link`: `default` (follow the global
    switch), `here` or `device`.
- The original URL is remembered for linked devices only. It is refreshed whenever the
  owning integration rewrites it (for example on ESPHome reconnect), and restored when
  linking is turned off, when the web UI is disabled or removed, or when the last entry
  is removed.

**Linked devices**
- Each device web UI entry owns a device named "<name> web UI". It shares the device's
  connections (MAC address and so on), or its identifiers when it has no connections, so
  HA 2026.9 shows it in the "Linked devices" card of the device's page. Its "Visit"
  button opens the web UI here.
- The device itself is not changed, so this works alongside, or instead of, the "Visit"
  link above.

**Standalone access.** Every view is reachable without the device page:
- from the panel list;
- at its own bookmarkable route, `/local-web-ui/<view_id>`;
- through "open in new tab", which shows the proxied UI full page (still isolated through
  the CSP header);
- optionally, from its own sidebar entry.

## WebSocket API (admin only)

| command | purpose |
|---|---|
| `local_web_ui/views` | list the views, the number of pending discoveries and the global options |
| `local_web_ui/session {view_id, token?}` | create a session, or extend the caller's own; returns `{url, token, view, expires_in}` |
| `local_web_ui/clear_site_data {view_id}` | forget the caller's cookies and stored data for a view ("log out") |

Adding, ignoring and editing web UIs go through HA's own config entry flows.

## Known limitations

- **HA's security filter** rejects (400) request paths and queries that look like attacks
  (`../`, `<script>`, SQL keywords…), before the proxy sees them, and logs the path, which
  contains the session token, at WARNING. Some router diagnostic pages can trip it.
- Root-relative ES module imports (`import "/x.js"`) cannot be rewritten at runtime.
- `Storage.prototype.getItem.call(localStorage, k)` throws with the emulated storage.
- Iframes a page creates with script (`about:blank`) get their own opaque origin.

## Out of scope (designed for)

- A pluggable transport, for example the ESPHome native-API tunnel from variant 1.
- Non-admin users with per-view allow lists.
