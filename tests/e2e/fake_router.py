"""A stand-in admin UI of a LAN device, for end-to-end tests.

Behind HTTP Basic auth (admin/secret) with a cookie login on top, root-relative
links and redirects, and a page script using localStorage and document.cookie.

Usage: python fake_router.py [port]
"""

import base64
import secrets
import sys

from aiohttp import web

USER, PASSWORD = "admin", "secret"
SESSIONS: set[str] = set()

LOGIN = """<!doctype html><html><head><title>Router login</title></head><body>
<h1>Router</h1><form method="post" action="/login"><input name="user" value="admin">
<button id="login">Log in</button></form></body></html>"""

DASHBOARD = """<!doctype html><html><head><meta charset="utf-8"><title>Router</title>
<link rel="stylesheet" href="/static/style.css"></head><body>
<h1>Router dashboard</h1>
<p id="status">logged in</p>
<p>Visits in this browser (localStorage): <b id="visits">?</b></p>
<p>Theme cookie set by script: <b id="theme">?</b></p>
<p>Server saw: <b id="server">?</b></p>
<a id="settings" href="/settings">Settings</a>
<script src="/static/app.js"></script></body></html>"""

APP_JS = """
const n = Number(localStorage.getItem('visits') || 0) + 1;
localStorage.setItem('visits', String(n));
document.getElementById('visits').textContent = n;
document.cookie = 'theme=dark; path=/';
document.getElementById('theme').textContent = document.cookie.includes('theme=dark') ? 'dark' : 'missing';
fetch('/api/whoami').then(r => r.json()).then(j =>
  document.getElementById('server').textContent = JSON.stringify(j));
"""


@web.middleware
async def basic_auth(request: web.Request, handler):
    header = request.headers.get("Authorization", "")
    expected = "Basic " + base64.b64encode(f"{USER}:{PASSWORD}".encode()).decode()
    if header != expected:
        return web.Response(status=401, headers={"WWW-Authenticate": 'Basic realm="router"'})
    return await handler(request)


def logged_in(request: web.Request) -> bool:
    return request.cookies.get("sid") in SESSIONS


async def root(request):
    raise web.HTTPFound("/dashboard" if logged_in(request) else "/login")


async def login_form(request):
    return web.Response(text=LOGIN, content_type="text/html")


async def login(request):
    sid = secrets.token_hex(8)
    SESSIONS.add(sid)
    resp = web.HTTPFound("/dashboard")
    resp.set_cookie("sid", sid, httponly=True, path="/")
    raise resp


async def dashboard(request):
    if not logged_in(request):
        raise web.HTTPFound("/login")
    return web.Response(text=DASHBOARD, content_type="text/html")


async def app_js(request):
    return web.Response(text=APP_JS, content_type="application/javascript")


async def style(request):
    return web.Response(
        text="body{font-family:sans-serif;background:#223;color:#eee}", content_type="text/css"
    )


async def whoami(request):
    return web.json_response({"session": logged_in(request), "theme": request.cookies.get("theme")})


def make_app() -> web.Application:
    app = web.Application(middlewares=[basic_auth])
    app.add_routes(
        [
            web.get("/", root),
            web.get("/login", login_form),
            web.post("/login", login),
            web.get("/dashboard", dashboard),
            web.get("/static/app.js", app_js),
            web.get("/static/style.css", style),
            web.get("/api/whoami", whoami),
        ]
    )
    return app


if __name__ == "__main__":
    web.run_app(make_app(), host="0.0.0.0", port=int(sys.argv[1]) if len(sys.argv) > 1 else 8081)
