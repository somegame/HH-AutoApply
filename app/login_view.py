import asyncio
import secrets
import time
from pathlib import Path

from aiohttp import web
from playwright.async_api import Error as PlaywrightError

from app.hh_browser import guard
from app.services import ServiceError

STATIC = Path(__file__).with_name("login_web")
SESSION_SECONDS = 180
HEADERS = {
    "Cache-Control": "no-store",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self'; style-src 'self'; img-src blob:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'"
    ),
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
}


class LoginView:
    def __init__(self, config):
        self.config = config
        self.runner = None
        self.page = None
        self.secret = None
        self.expires = 0
        self.lock = asyncio.Lock()

    async def start(self):
        if self.runner or not self.config.login_public_url:
            return
        app = web.Application(client_max_size=2048)
        app.add_routes(
            [
                web.get("/", self.health),
                web.get("/health", self.health),
                web.get("/hh-login", self.index),
                web.get("/hh-login.js", self.script),
                web.get("/hh-login.css", self.style),
                web.get("/hh-login/frame", self.frame),
                web.post("/hh-login/input", self.input),
            ]
        )
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        try:
            await web.TCPSite(runner, "0.0.0.0", self.config.web_port).start()
        except OSError:
            await runner.cleanup()
            raise ServiceError(
                "Не удалось открыть порт страницы входа. Проверьте PORT на Bothost."
            ) from None
        self.runner = runner

    async def shutdown(self):
        await self.revoke()
        if self.runner:
            await self.runner.cleanup()
            self.runner = None

    def issue(self, page):
        if not self.runner or not self.config.login_public_url:
            raise ServiceError("Настройте HTTPS-адрес LOGIN_PUBLIC_URL в Bothost.")
        self.page = page
        self.secret = secrets.token_urlsafe(32)
        self.expires = time.monotonic() + SESSION_SECONDS
        return self.config.login_public_url + "/hh-login#" + self.secret

    async def revoke(self):
        self.secret = None
        async with self.lock:
            self.page = None

    async def health(self, _):
        return web.Response(text="ok", headers=HEADERS)

    async def asset(self, name, mime):
        return web.Response(body=(STATIC / name).read_bytes(), content_type=mime, headers=HEADERS)

    async def index(self, _):
        return await self.asset("index.html", "text/html")

    async def script(self, _):
        return await self.asset("login.js", "application/javascript")

    async def style(self, _):
        return await self.asset("login.css", "text/css")

    def authorize(self, request):
        origin = request.headers.get("Origin")
        if origin and origin != self.config.login_public_url:
            raise web.HTTPForbidden(headers=HEADERS)
        if request.method == "POST" and origin != self.config.login_public_url:
            raise web.HTTPForbidden(headers=HEADERS)
        token = request.headers.get("Authorization", "")
        if (
            not self.secret
            or not self.page
            or time.monotonic() >= self.expires
            or not (
                token.startswith("Bearer ")
                and token.isascii()
                and secrets.compare_digest(token[7:], self.secret)
            )
        ):
            raise web.HTTPUnauthorized(headers=HEADERS)

    async def frame(self, request):
        self.authorize(request)
        if self.lock.locked():
            raise web.HTTPTooManyRequests(headers=HEADERS)
        async with self.lock:
            self.authorize(request)
            try:
                await guard(self.page, login_allowed=True)
                frame = await self.page.screenshot(type="jpeg", quality=65, timeout=2300)
            except (ServiceError, PlaywrightError):
                raise web.HTTPConflict(headers=HEADERS) from None
        return web.Response(body=frame, content_type="image/jpeg", headers=HEADERS)

    async def input(self, request):
        self.authorize(request)
        if self.lock.locked():
            raise web.HTTPTooManyRequests(headers=HEADERS)
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(headers=HEADERS) from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(headers=HEADERS)
        async with self.lock:
            self.authorize(request)
            try:
                await guard(self.page, login_allowed=True)
                await self.interact(payload)
            except (ServiceError, PlaywrightError):
                raise web.HTTPConflict(headers=HEADERS) from None
        return web.Response(status=204, headers=HEADERS)

    async def interact(self, event):
        kind = event.get("kind")
        page = self.page
        if kind == "click":
            x, y = event.get("x"), event.get("y")
            if (
                type(x) not in {int, float}
                or type(y) not in {int, float}
                or not (0 <= x < 430 and 0 <= y < 820)
            ):
                raise web.HTTPBadRequest(headers=HEADERS)
            await page.mouse.click(x, y)
        elif kind == "entry":
            value = event.get("text")
            if (
                not isinstance(value, str)
                or not 0 < len(value) <= 300
                or any(ord(char) < 32 for char in value)
            ):
                raise web.HTTPBadRequest(headers=HEADERS)
            editable = await page.locator(
                "input:focus:not([type=button]):not([type=submit]):not([readonly]):not([disabled])"
            ).count()
            if editable != 1:
                raise web.HTTPUnprocessableEntity(headers=HEADERS)
            await page.keyboard.press("Control+a")
            await page.keyboard.insert_text(value)
        elif kind == "scroll" and event.get("direction") in {"up", "down"}:
            await page.mouse.wheel(0, -470 if event["direction"] == "up" else 470)
        else:
            raise web.HTTPBadRequest(headers=HEADERS)
