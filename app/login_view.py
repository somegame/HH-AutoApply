import asyncio
import base64
import json
import re
import secrets
import time
from pathlib import Path

from aiohttp import web
from playwright.async_api import Error as PlaywrightError

from app.hh_browser import guard
from app.services import ServiceError

STATIC = Path(__file__).with_name("login_web")
SESSION_SECONDS = 480
WIDTH, HEIGHT = 430, 820
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
FIELDS = """
() => [...document.querySelectorAll('input')].flatMap(input => {
  const kinds = ['text', 'tel', 'email', 'password', 'number', 'search'];
  if (!kinds.includes(input.type) || input.disabled || input.readOnly) return [];
  const box = input.getBoundingClientRect();
  const x = Math.max(box.x, 0), y = Math.max(box.y, 0);
  const w = Math.min(box.right, innerWidth) - x, h = Math.min(box.bottom, innerHeight) - y;
  if (w < 14 || h < 14 || document.elementFromPoint(x + w / 2, y + h / 2) !== input) return [];
  input.dataset.hhLoginField ||= crypto.randomUUID().replaceAll('-', '');
  const code = input.autocomplete === 'one-time-code';
  return [{ id: input.dataset.hhLoginField, type: input.type, mode: input.inputMode, code, x, y, w, h }];
}).slice(0, 20)
"""


OTP_COPY = re.compile(
    r"код из (?:смс|sms)|введите (?:проверочный )?код|код подтверждения|verification code",
    re.I,
)
OTP_INPUT = (
    'input[autocomplete="one-time-code"], input[name="otp"], '
    'input[name="verificationCode"], input[data-qa*="otp" i], '
    '[data-qa*="otp" i] input, input[data-qa*="verification-code" i], '
    'input[data-qa*="pin-input" i], [data-qa*="pin-input" i] input'
)
EDITABLE = 'input:not([type="hidden"]):not([disabled]):not([readonly])'


async def waiting_for_sms(page):
    return (
        await page.locator(OTP_INPUT).first.is_visible()
        or await page.get_by_text(OTP_COPY, exact=False).first.is_visible()
    )


class LoginView:
    def __init__(self, config):
        self.config = config
        self.runner = None
        self.page = None
        self.secret = None
        self.expires = 0
        self.lock = asyncio.Lock()
        self.cdp = None
        self.frame_ready = asyncio.Event()
        self.image = None
        self.revision = 0
        self.check = asyncio.Event()
        self.status = "login"

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

    async def issue(self, page):
        if not self.runner or not self.config.login_public_url:
            raise ServiceError("Настройте HTTPS-адрес LOGIN_PUBLIC_URL в Bothost.")
        self.page = page
        self.image = None
        self.revision = 0
        self.check.clear()
        self.status = "login"
        self.cdp = await page.context.new_cdp_session(page)
        self.cdp.on("Page.screencastFrame", self.on_frame)
        try:
            await self.cdp.send(
                "Page.startScreencast",
                {
                    "format": "jpeg",
                    "quality": 63,
                    "maxWidth": WIDTH,
                    "maxHeight": HEIGHT,
                },
            )
        except (PlaywrightError, asyncio.CancelledError):
            await self.revoke()
            raise
        self.secret = secrets.token_urlsafe(32)
        self.expires = time.monotonic() + SESSION_SECONDS
        return self.config.login_public_url + "/hh-login#" + self.secret

    async def on_frame(self, event):
        cdp = self.cdp
        if not cdp:
            return
        self.image = base64.b64decode(event["data"])
        self.revision += 1
        self.frame_ready.set()
        try:
            await cdp.send("Page.screencastFrameAck", {"sessionId": event["sessionId"]})
        except PlaywrightError:
            pass

    def change_status(self, status):
        if self.status != status:
            self.status = status
            self.revision += 1
            self.frame_ready.set()

    async def revoke(self, status=None):
        if status:
            self.change_status(status)
            self.expires = time.monotonic() + 120
        else:
            self.secret = None
        async with self.lock:
            self.page = None
            cdp, self.cdp = self.cdp, None
            self.image = None
            self.frame_ready.set()
            if cdp:
                try:
                    await cdp.detach()
                except PlaywrightError:
                    pass

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
        if (origin and origin != self.config.login_public_url) or (
            request.method == "POST" and origin != self.config.login_public_url
        ):
            raise web.HTTPForbidden(headers=HEADERS)
        token = request.headers.get("Authorization", "")
        if (
            not self.secret
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
        if request.query.get("after") == str(self.revision):
            self.frame_ready.clear()
            try:
                async with asyncio.timeout(2):
                    await self.frame_ready.wait()
            except TimeoutError:
                pass
        self.authorize(request)
        headers = HEADERS | {"X-HH-Status": self.status, "X-HH-Revision": str(self.revision)}
        if (
            not self.page
            or self.status == "blocked"
            or not self.image
            or (request.query.get("after") == str(self.revision))
        ):
            return web.Response(status=204, headers=headers)
        page, image = self.page, self.image
        try:
            await guard(page, login_allowed=True)
            fields = await page.evaluate(FIELDS)
            sms = await waiting_for_sms(page)
        except (ServiceError, PlaywrightError):
            return web.Response(status=204, headers=headers)
        # A completed session must not publish a frame that was already in flight.
        if page is not self.page or self.status in {"done", "failed", "blocked"}:
            return web.Response(status=204, headers=HEADERS | {"X-HH-Status": self.status})
        headers["X-HH-Fields"] = json.dumps(fields, separators=(",", ":"))
        headers["X-HH-Sms"] = "1" if sms else "0"
        return web.Response(body=image, content_type="image/jpeg", headers=headers)

    async def input(self, request):
        self.authorize(request)
        try:
            payload = await request.json()
        except (ValueError, UnicodeError):
            raise web.HTTPBadRequest(headers=HEADERS) from None
        if not isinstance(payload, dict):
            raise web.HTTPBadRequest(headers=HEADERS)
        async with self.lock:
            self.authorize(request)
            if not self.page or self.status == "blocked":
                raise web.HTTPConflict(headers=HEADERS)
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
                or not (0 <= x < WIDTH and 0 <= y < HEIGHT)
            ):
                raise web.HTTPBadRequest(headers=HEADERS)
            await page.mouse.click(x, y)
        elif kind in {"entry", "enter"}:
            field = event.get("field", "")
            if not isinstance(field, str) or not re.fullmatch(r"[a-f0-9]{32}", field):
                raise web.HTTPBadRequest(headers=HEADERS)
            value = event.get("text", "")
            if (
                not isinstance(value, str)
                or len(value) > 300
                or any(ord(char) < 32 for char in value)
            ):
                raise web.HTTPBadRequest(headers=HEADERS)
            locator = page.locator(f'input[data-hh-login-field="{field}"]')
            if await locator.count() != 1 or not await locator.is_editable():
                raise web.HTTPUnprocessableEntity(headers=HEADERS)
            await locator.fill(value, timeout=1500)
            if kind == "enter":
                await locator.press("Enter", timeout=1500)
        elif kind == "sms":
            await self.enter_sms(page, event.get("text"))
        elif kind == "scroll":
            delta = event.get("delta")
            if type(delta) not in {int, float} or not -820 <= delta <= 820:
                raise web.HTTPBadRequest(headers=HEADERS)
            await page.mouse.move(WIDTH / 2, HEIGHT / 2)
            await page.mouse.wheel(0, delta)
        elif kind == "check":
            if not await waiting_for_sms(page):
                self.check.set()
        else:
            raise web.HTTPBadRequest(headers=HEADERS)

    @staticmethod
    async def enter_sms(page, value):
        if not isinstance(value, str) or not re.fullmatch(r"[0-9]{4,8}", value):
            raise web.HTTPBadRequest(headers=HEADERS)
        if not await waiting_for_sms(page):
            raise web.HTTPConflict(headers=HEADERS)
        candidates = page.locator(OTP_INPUT + ', input[name="code"]').and_(page.locator(EDITABLE))
        fields = [field for field in await candidates.all() if await field.is_visible()]
        if not fields:
            active = candidates.and_(page.locator("input:focus"))
            if await active.count() == 1 and await active.is_editable():
                await page.keyboard.press("Control+a")
                await page.keyboard.insert_text(value)
                return
        if not fields:
            focused = page.locator(
                EDITABLE + ':focus:is([inputmode="numeric"], [inputmode="decimal"], '
                '[type="tel"], [type="number"], [maxlength="4"], [maxlength="5"], '
                '[maxlength="6"], [maxlength="7"], [maxlength="8"], [maxlength="1"])'
            )
            if await focused.count() == 1 and await focused.is_editable():
                if not await focused.is_visible():
                    await page.keyboard.press("Control+a")
                    await page.keyboard.insert_text(value)
                    return
                fields = [focused]
        if not fields:
            # Some OTP widgets draw their own boxes over a single numeric input.
            numeric = page.locator(
                EDITABLE + ':is([inputmode="numeric"], [type="tel"], [type="number"], '
                '[maxlength="4"], [maxlength="5"], [maxlength="6"], [maxlength="7"], [maxlength="8"])'
            )
            fields = [field for field in await numeric.all() if await field.is_visible()]
        if len(fields) == 1 and await fields[0].get_attribute("maxlength") != "1":
            width = await fields[0].get_attribute("maxlength")
            if width and width.isascii() and width.isdigit() and int(width) != len(value):
                raise web.HTTPUnprocessableEntity(headers=HEADERS)
            await fields[0].fill(value, timeout=1500)
        elif len(fields) == len(value) and all(
            [await field.get_attribute("maxlength") == "1" for field in fields]
        ):
            for field, digit in zip(fields, value, strict=True):
                await field.fill(digit, timeout=1500)
        else:
            raise web.HTTPUnprocessableEntity(headers=HEADERS)
