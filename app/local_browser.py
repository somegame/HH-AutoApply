import asyncio
import json
import os
import re
import tempfile
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import async_playwright

from app.hh_browser import hh_url
from app.services import ServiceError


def hh_domain(domain):
    domain = domain.lstrip(".").casefold()
    return domain == "hh.ru" or domain.endswith(".hh.ru")


class LocalBrowser:
    def __init__(self, config):
        self.config = config
        self.state = config.data_dir / "hh_storage_state.json"

    def check_installation(self):
        executable = Path(self.config.chromium_path)
        if not executable.is_file() or not os.access(executable, os.X_OK):
            raise ServiceError(
                "Chromium Headless Shell отсутствует. Пересоберите бот из Dockerfile."
            )

    async def save(self, context):
        state = await context.storage_state()
        state["cookies"] = [cookie for cookie in state["cookies"] if hh_domain(cookie["domain"])]
        state["origins"] = [origin for origin in state["origins"] if hh_url(origin["origin"])]
        try:
            fd, filename = tempfile.mkstemp(
                prefix="hh-state-", suffix=".tmp", dir=self.state.parent
            )
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as handle:
                    json.dump(state, handle, ensure_ascii=False)
                os.replace(filename, self.state)
            finally:
                Path(filename).unlink(missing_ok=True)
        except OSError:
            raise ServiceError("Не удалось сохранить сессию HH. Проверьте DATA_DIR.") from None

    @asynccontextmanager
    async def session(self, seconds=60, interactive=False):
        self.check_installation()
        if not interactive and not self.state.is_file():
            raise ServiceError("Сессия HH отсутствует. Войдите из Telegram ещё раз.")
        state = str(self.state) if self.state.is_file() else None
        async with async_playwright() as driver:
            try:
                browser = await driver.chromium.launch(
                    executable_path=self.config.chromium_path,
                    headless=True,
                    timeout=15000,
                    args=["--disable-dev-shm-usage"],
                )
            except PlaywrightError:
                raise ServiceError(
                    "Chromium не запустился. Проверьте логи Bothost и лимиты контейнера.", 120
                ) from None
            try:
                async with asyncio.timeout(seconds):
                    context = await browser.new_context(
                        viewport={"width": 430, "height": 820}
                        if interactive
                        else {"width": 1280, "height": 800},
                        storage_state=state,
                        accept_downloads=False,
                        locale="ru-RU",
                    )
                    page = await context.new_page()
                    page.set_default_timeout(3500)
                    yield context, page
                    if not interactive and any(
                        hh_domain(cookie["domain"]) for cookie in await context.cookies()
                    ):
                        await self.save(context)
            except PlaywrightError:
                raise ServiceError(
                    "Связь с Chromium прервана либо состояние HH повреждено.", 120
                ) from None
            except TimeoutError:
                if interactive:
                    raise
                raise ServiceError("Chromium не завершил операцию вовремя.", 120) from None
            finally:
                await browser.close()


def public_login_url(value):
    value = value.strip()
    if not value:
        return ""
    label = r"[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?"
    if re.fullmatch(rf"(?:{label}\.)+{label}", value):
        value = f"https://{value}"
    try:
        parsed = urlsplit(value)
        hostname = parsed.hostname
        if (
            parsed.scheme == "https"
            and hostname
            and parsed.path in {"", "/"}
            and not (parsed.username or parsed.password or parsed.query or parsed.fragment)
            and parsed.port in {None, 443}
            and "." in hostname
            and not any(char.isspace() for char in value)
        ):
            return f"https://{hostname}"
    except ValueError:
        pass
    raise ValueError(
        "LOGIN_PUBLIC_URL должен содержать домен бота или HTTPS-адрес без пути и параметров."
    )
