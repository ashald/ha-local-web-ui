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

## Concepts

**View.** A web UI HA can open. It has:
- an id and a name;
- a base URL (`http(s)://host[:port]`) and an entry path;
- a mode: `isolated` (default) or `trusted`;
- `verify_ssl`;
- optional basic-auth credentials;
- an optional device link;
- an optional "show in sidebar" flag.

Views come from two sources:
- **Static views** are config subentries (type `view`) of the single config entry. You add
  and edit them in the UI under Settings → Devices & services → Local Web UIs → Add view.
  The view id is the subentry id.
- **Discovered views** are derived live from the device registry. Any device whose
  `configuration_url` is `http(s)` to a local host qualifies. Local means a private,
  loopback, link-local or ULA IP address, a `.local`, `.lan` or `.home.arpa` name, or a
  single-label hostname. Examples:
  - ESPHome sets this to `http://<host>:<web_server port>` whenever `web_server` is enabled;
  - WLED, SMLIGHT, many printers and routers set it too.

  The view id is `d_<device_id>`, and the view is named after the device. Discovery can be
  turned off in options. A discovered view can be **pinned**, which creates a static
  subentry pre-filled from it and linked to the device, so you can rename it or change its
  mode, and the discovered duplicate disappears. It can also be **hidden**.

**Session.** Grants one HA user's browser access to one view:
- a random 256-bit token, created over the WebSocket API by admins only;
- expires after 5 minutes of inactivity;
- kept alive by proxied requests and by panel keepalives.

## URL layout

```
/api/local_web_ui/<view_id>/<session_token>/<path…>
```

- **The token is in the path, not a cookie.** Iframes cannot send HA's bearer token, and
  isolated (opaque-origin) pages never send `SameSite=Strict`/`Lax` cookies (verified in
  Chromium). A path token also makes the UI's relative URLs carry the token automatically.
  Supervisor ingress cannot isolate apps for exactly this reason.
- **The view id comes before the token.** That gives trusted-mode cookies a stable scope,
  `Path=/api/local_web_ui/<view_id>/`, so the site's sessions survive new HA sessions.
- **The route is registered directly on HA's aiohttp router**, not as a
  `HomeAssistantView`. HA attaches CORS handling to views, which claims `OPTIONS` (only
  `/api/hassio_ingress/` is exempt), and isolated pages need their preflights answered by us.

## Isolation modes

| | isolated (default) | trusted |
|---|---|---|
| **Origin of the page** | opaque (`null`): CSP `sandbox allow-scripts allow-forms allow-popups allow-popups-to-escape-sandbox allow-modals allow-downloads` on every response, plus the same iframe `sandbox`. Holds in a new tab too. | HA's origin, like Supervisor ingress. The page can act as the logged-in HA user. The UI warns before enabling. |
| **Site cookies** | Server-side jar per (HA user, view), persisted in `.storage`. The browser never holds them; the proxy strips `Set-Cookie` and injects `Cookie`. | Passed to the browser with `Path` rewritten to `/api/local_web_ui/<view_id>/` and `Domain` removed. |
| **localStorage / sessionStorage** | Shim injected first in `<head>`. Its data is preloaded from and written back to server-side storage per (HA user, view), through `POST …/__lwu/storage`. | Native. Shared with HA and every other trusted view. |
| **`document.cookie` from JavaScript** | Not available (throws). A shim is possible later. | Native. |
| **CORS** | Requests carry `Origin: null`. The proxy answers preflights itself and adds `Access-Control-Allow-Origin: *`, which is safe because the credential is the path token. | Not needed. |

## Proxy behaviour

Inherited from the variant 1 PoC, which is tested with HTTP, 1 MiB downloads, SSE and WebSockets.

**Request headers**
- Hop-by-hop headers, `Origin` and `Referer` (which contains the token) are dropped.
- `Host` comes from the target.
- `X-Forwarded-*` and `X-Ingress-Path` are set.
- `Authorization` from the browser is forwarded. Otherwise the view's stored credentials are
  injected as Basic auth.

**Response headers**
- `X-Frame-Options` and the device's own CSP are dropped. In isolated mode our sandbox CSP
  is added.
- `Location` headers pointing at the target origin or at a root-relative path are rewritten
  under the prefix.

**HTML rewriting**
- Root-relative `src`, `href` and `action` attributes are rewritten under the prefix, as are
  absolute URLs to the target origin.
- In isolated mode the storage shim is injected.

**Bodies and streaming**
- Bodies up to 4 MiB are buffered, and compressible types are compressed.
- Larger or unknown-length bodies, and SSE (uncompressed), are streamed.
- WebSockets are relayed frame by frame.

**Upstream connections**
- One aiohttp `ClientSession` per `verify_ssl` value.
- `DummyCookieJar`, because cookies are handled per user and view as above.
- `limit_per_host=6`, a 10 s connect timeout, and no total timeout.

**Safety**
- Targets always come from view config, never from the request. There is no open proxy and
  no SSRF.
- Everything is admin-only in v0.1.

## Frontend

A panel is registered with `panel_custom` at `/local-web-ui`. It is a plain web component
with no build step.
- **List view:** "Devices" (discovered, with device name and area) and "Sites" (static).
  Actions: open, open in new tab, pin, hide.
- **View page:** a toolbar with menu, back, title, a mode badge, reload and open in new tab,
  plus the iframe (with `sandbox` when isolated).
- **Sidebar entries:** static views flagged "show in sidebar" get their own entry at
  `/local-web-ui-<view_id>`, using the same component in single-view mode.

**Device page link** (optional)
- Where enabled, a device's `configuration_url` points at
  `homeassistant://local-web-ui/<view_id>`, so the device page's "Visit" button opens the
  view here.
- It is controlled at two levels:
  - a **global switch** in the integration options, `link_device_pages`, default on;
  - **per-device exceptions** that flip the global default for one device. You set them from
    the panel's view menu ("Use for device page link"), and they are stored in options.
- The original URL is remembered in `.storage`. It is refreshed whenever the owning
  integration rewrites it (for example on ESPHome reconnect), and restored when linking is
  turned off for that device, globally, or when the integration is removed.

**Standalone access.** Every view is reachable without the device page:
- from the panel list;
- at its own bookmarkable route, `/local-web-ui/<view_id>`;
- through "open in new tab", which shows the proxied UI full page (still isolated through
  the CSP header);
- optionally, from its own sidebar entry.

## WebSocket API (admin only)

| command | purpose |
|---|---|
| `local_web_ui/views` | list the visible views |
| `local_web_ui/session {view_id, token?}` | create or extend a session; returns `{url, token, name, mode, expires_in}` |
| `local_web_ui/pin {view_id}` | turn a discovered view into a static subentry |
| `local_web_ui/hide {view_id}` / `local_web_ui/unhide {view_id}` | hide or unhide a discovered view |

## Out of scope for v0.1 (designed for)

- A pluggable transport, for example the ESPHome native-API tunnel from variant 1.
- Non-admin users with per-view allow lists.
- A `document.cookie` shim.
- CSS `url(/…)` and JavaScript-built absolute URLs. UIs that build `location.origin + '/…'`
  need trusted mode plus firmware that honours `X-Ingress-Path`, or rewrite rules.
