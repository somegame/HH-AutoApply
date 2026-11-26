import asyncio
import logging
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import quote, urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from app.services import BROWSERBASE, ServiceError, request

log = logging.getLogger(__name__)


class Browserbase:
    def __init__(self, client, config, db):
        self.client = client
        self.config = config
        self.db = db

    async def api(self, method, path, data=None):
        return await request(
            self.client,
            method,
            BROWSERBASE + path,
            headers={"X-BB-API-Key": self.config.browserbase_key},
            payload=data,
        )

    async def context(self):
        context = self.db.get("browser_context")
        if not context:
            context = await self.api("POST", "/contexts", {})
            context = context.get("id") if isinstance(context, dict) else None
            if not isinstance(context, str) or not context:
                raise ServiceError("Browserbase не вернул идентификатор контекста.")
            self.db.set("browser_context", context)
        return context

    def reserve(self, seconds):
        month = datetime.now(self.config.zone).strftime("%Y-%m")
        budget = self.db.get("browser_budget", {})
        used = int(budget.get("seconds", 0)) if budget.get("month") == month else 0
        if used + seconds > self.config.browser_month_seconds:
            raise ServiceError("Исчерпан заданный месячный бюджет Browserbase.")
        self.db.set("browser_budget", {"month": month, "seconds": used + seconds})

    async def live(self, session_id):
        response = await self.api("GET", f"/sessions/{quote(session_id, safe='')}/debug")
        try:
            value = response["debuggerFullscreenUrl"]
            parsed = urlsplit(value)
            if (
                parsed.scheme == "https"
                and parsed.hostname
                and (
                    parsed.hostname == "browserbase.com"
                    or parsed.hostname.endswith(".browserbase.com")
                )
            ):
                return value
        except (KeyError, ValueError, TypeError, AttributeError):
            pass
        raise ServiceError("Browserbase не вернул защищённую ссылку Live View.")

    async def close(self, session_id):
        try:
            await self.api(
                "POST", f"/sessions/{quote(session_id, safe='')}", {"status": "REQUEST_RELEASE"}
            )
        except ServiceError:
            log.warning(
                "Browserbase: подтверждение закрытия сессии не получено, ожидается её таймаут."
            )

    @asynccontextmanager
    async def session(self, seconds=60, interactive=False):
        if not self.config.browserbase_key:
            raise ServiceError("Укажите BROWSERBASE_API_KEY в Bothost.")
        self.reserve(seconds)
        context = await self.context()
        session = await self.api(
            "POST",
            "/sessions",
            {
                "timeout": seconds,
                "keepAlive": False,
                "proxies": False,
                "browserSettings": {
                    "context": {"id": context, "persist": interactive},
                    "recordSession": False,
                    "logSession": False,
                    "solveCaptchas": False,
                    "advancedStealth": False,
                    "verified": False,
                },
            },
        )
        identifier = session.get("id") if isinstance(session, dict) else None
        if not isinstance(identifier, str) or not identifier:
            raise ServiceError("Browserbase не вернул идентификатор сессии.")
        try:
            url = session.get("connectUrl", "")
            target = urlsplit(url)
            if (
                target.scheme != "wss"
                or not target.hostname
                or not target.hostname.endswith(".browserbase.com")
            ):
                raise ServiceError("Browserbase не вернул допустимый CDP URL.")
            async with async_playwright() as driver:
                browser = await driver.chromium.connect_over_cdp(url, timeout=12000)
                if not browser.contexts:
                    raise ServiceError("В Browserbase отсутствует браузерный контекст.")
                profile = browser.contexts[0]
                page = profile.pages[0] if profile.pages else await profile.new_page()
                page.set_default_timeout(3500)
                yield identifier, page
        except (PlaywrightError, ValueError):
            raise ServiceError(
                "Соединение с облачным браузером или страницей прервано.", 120
            ) from None
        finally:
            # Complete release before allowing another session with the same context.
            cleanup = asyncio.create_task(self.close(identifier))
            try:
                await asyncio.shield(cleanup)
            except asyncio.CancelledError:
                await cleanup
                raise
