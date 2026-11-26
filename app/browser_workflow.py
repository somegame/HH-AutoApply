import asyncio
import hashlib
import json
import time

from app import hh_browser as hh
from app.diagnostics import resource_warning
from app.local_browser import LocalBrowser
from app.login_view import SESSION_SECONDS, LoginView
from app.services import ServiceError, clean_text
from app.vacancies import filter_reason, search_links, vacancy_id


class BrowserWorkflow:
    def __init__(self, config, db):
        self.config = config
        self.db = db
        self.provider = LocalBrowser(config)
        self.view = LoginView(config)
        self.lock = asyncio.Lock()
        self.login_task = None
        self.login_done = None

    @property
    def available(self):
        return self.config.browser_engine == "chromium"

    def require_browser(self):
        if not self.available:
            raise ServiceError("Укажите BROWSER_ENGINE=chromium в Bothost.")
        self.provider.check_installation()
        if warning := resource_warning(self.config.data_dir):
            raise ServiceError(warning)

    async def start_login(self):
        self.require_browser()
        await self.view.start()
        if not self.view.runner:
            raise ServiceError("Настройте LOGIN_PUBLIC_URL и HTTPS-домен в Bothost.")
        if self.login_task and not self.login_task.done():
            raise ServiceError("Окно входа уже открыто. Завершите вход или отмените его.")
        if self.lock.locked():
            raise ServiceError("Браузер занят. Повторите вход чуть позже.")
        self.pause("Открыто новое окно входа. После проверки резюме включите нужный режим.")
        self.db.set("hh_resume_choices", {})
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        self.login_done = asyncio.Event()
        self.login_task = asyncio.create_task(self.login(ready, self.login_done))
        try:
            return await asyncio.shield(ready)
        except BaseException:
            if not ready.done():
                ready.cancel()
                await self.cancel_login()
            raise

    async def login(self, ready, done):
        async with self.lock:
            try:
                async with self.provider.session(225, interactive=True) as (_, page):
                    await hh.open_page(page, hh.HH + "/account/login", login_allowed=True)
                    try:
                        ready.set_result(self.view.issue(page))
                        async with asyncio.timeout(SESSION_SECONDS):
                            await done.wait()
                    finally:
                        await self.view.revoke()
                    return await hh.authorized_resumes(page)
            except TimeoutError:
                error = ServiceError("Срок ссылки истёк. Откройте новое окно входа.")
            except ServiceError as exc:
                error = exc
            except asyncio.CancelledError:
                if not ready.done():
                    ready.cancel()
                raise
            except Exception:
                error = ServiceError("Операция входа не завершилась.")
            if not ready.done():
                ready.set_exception(error)
            return error

    async def finish_login(self):
        if self.login_task is None:
            raise ServiceError("Сначала откройте окно входа HH в боте.")
        self.login_done.set()
        try:
            result = await asyncio.shield(self.login_task)
        finally:
            if self.login_task and self.login_task.done():
                self.login_task = None
        if isinstance(result, ServiceError):
            raise result
        self.db.set("hh_resume_choices", result)
        return result

    async def cancel_login(self):
        if self.login_task:
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)
            self.login_task = None
        await self.view.revoke()

    async def import_resume(self, identifier):
        self.require_browser()
        if self.lock.locked():
            raise ServiceError("Браузер занят другой операцией.")
        choices = self.db.get("hh_resume_choices", {})
        if identifier not in choices or not hh.RESUME_ID.fullmatch(f"/resume/{identifier}"):
            raise ServiceError("Выберите резюме из списка после входа.")
        async with self.lock:
            async with self.provider.session() as (_, page):
                resume = clean_text(await hh.read_resume(page, identifier))
        if len(resume.strip()) < 60:
            raise ServiceError("В резюме недостаточно профессионального текста для анализа.")
        selected = {"id": identifier, "name": choices[identifier]}
        self.db.set_many(
            {
                "resume": resume,
                "hh_selected_resume": selected,
                "groq_consent": False,
                "browser_auto_enabled": False,
                "browser_search": False,
            }
        )
        return selected["name"]

    def readiness(self):
        self.require_browser()
        selected = self.db.get("hh_selected_resume", {})
        if not selected or not self.db.get("resume"):
            raise ServiceError("Войдите в HH и выберите резюме.")
        if not self.db.get("groq_consent") or not self.config.groq_key:
            raise ServiceError("Разрешите анализ в Groq и проверьте ключ GROQ_API_KEY.")
        return selected

    def start_search(self, *, send=False):
        self.readiness()
        if send and not (self.config.auto_send and self.config.persistent):
            raise ServiceError(
                "Для автооткликов нужны AUTO_SEND=true и подтверждённый постоянный Volume."
            )
        self.db.set_many(
            {
                "browser_search": True,
                "browser_auto_enabled": send,
                "processing": True,
                "next_browser_search": 0,
                "browser_notice": "",
                "browser_failures": 0,
            }
        )

    def pause(self, notice):
        self.db.set_many(
            {
                "browser_search": False,
                "browser_auto_enabled": False,
                "browser_notice": notice,
            }
        )

    async def tick(self, bot, settings, day):
        if not (self.available and self.db.get("browser_search")) or self.lock.locked():
            return
        selected = self.readiness()
        now = int(time.time())
        due = now >= self.db.get("next_browser_search", 0)
        auto = (
            self.config.auto_send and self.config.persistent and self.db.get("browser_auto_enabled")
        )
        row = self.db.next_browser_application() if auto else None
        if row and self.db.counts(day)[1] >= settings["limit"]:
            row = None
        if not (due or row):
            return
        if now < self.db.get("next_browser_operation", 0):
            return
        self.db.set("next_browser_operation", now + 60)
        if due:
            self.db.set("next_browser_search", now + settings["interval"] * 60)
        async with self.lock:
            async with self.provider.session() as (_, page):
                resumes = await hh.authorized_resumes(page)
                if selected["id"] not in resumes:
                    raise hh.HHLoginRequired("Выбранное резюме больше не доступно на HH.")
                if row:
                    await self.send_one(bot, page, row, selected, settings, day)
                    if self.db.get("browser_search"):
                        self.db.set_many({"browser_failures": 0, "browser_notice": ""})
                    if due:
                        self.db.set("next_browser_search", now + 60)
                    return
                count = await self.discover(page, settings)
                self.db.set_many({"browser_failures": 0, "browser_notice": ""})
        if count:
            await bot.send_message(self.config.owner, f"Новых вакансий к анализу: {count}.")

    async def discover(self, page, settings):
        links = list(search_links(settings))
        index = self.db.get("browser_query_offset", 0) % len(links)
        offset = self.db.get("browser_page_offset", 0)
        identifiers = await hh.search_ids(page, links[index][1])
        batch = identifiers[offset : offset + 2]
        if len(batch) < 2 and offset:
            batch = identifiers[:2]
            offset = 0
        added = 0
        for identifier in batch:
            if self.db.vacancy(identifier):
                continue
            details = await hh.read_vacancy(page, identifier)
            if details:
                if await hh.already_applied(page):
                    if self.db.add(identifier, details):
                        self.db.transition(identifier, ("queued",), "duplicate")
                elif self.db.add(identifier, details):
                    added += 1
        self.db.set_many(
            {
                "browser_query_offset": index + 1,
                "browser_page_offset": (offset + 2)
                if index + 1 == len(links) and len(batch) == 2
                else offset,
            }
        )
        return added

    async def reconcile(self, identifier):
        self.require_browser()
        row = self.db.vacancy(identifier)
        if not (row and row["status"] == "unknown" and row["sent_day"]):
            raise ServiceError("Нет отклика с неизвестным результатом для сверки.")
        if self.lock.locked():
            raise ServiceError("Браузер занят. Попробуйте позже.")
        async with self.lock:
            async with self.provider.session() as (_, page):
                await hh.authorized_resumes(page)
                await hh.open_page(page, f"{hh.HH}/vacancy/{identifier}")
                if vacancy_id(page.url) == identifier and await hh.already_applied(page):
                    self.db.complete_browser_application(identifier, "sent", previous="unknown")
                    return True
        return False

    async def send_one(self, bot, page, row, resume, settings, day):
        identifier = row["id"]
        details = json.loads(row["details"])
        result = json.loads(row["result"] or "{}")
        resume_hash = hashlib.sha256(self.db.get("resume", "").encode()).hexdigest()
        if (
            filter_reason(details, settings)
            or result.get("score", -1) < settings["score"]
            or result.get("resume_hash") != resume_hash
            or not row["letter"]
        ):
            self.db.transition(identifier, ("approved",), "deferred")
            return
        reserved = False

        def reserve():
            nonlocal reserved
            current = self.db.vacancy(identifier)
            if not self.db.get("browser_auto_enabled") or current["letter"] != row["letter"]:
                return False
            if self.db.settings(self.config) != settings:
                return False
            reserved = (
                self.db.transition(
                    identifier, ("approved",), "sending", day=day, limit=settings["limit"]
                )
                == "ok"
            )
            return reserved

        try:
            result = await hh.apply(page, identifier, resume["id"], row["letter"], reserve)
        except hh.HHQuestions as exc:
            self.db.transition(identifier, ("approved",), "questions")
            await bot.send_message(self.config.owner, f"Вакансия {identifier}. {exc}")
            return
        except BaseException:
            if reserved:
                self.db.complete_browser_application(identifier, "unknown")
                self.pause("Отправка прервана. Проверьте отклик перед возобновлением.")
            raise
        if reserved:
            self.db.complete_browser_application(identifier, result)
        elif result == "duplicate":
            self.db.transition(identifier, ("approved",), "duplicate")
        if result in {"sent", "unknown"}:
            if result == "unknown":
                self.pause("HH не подтвердил отправку. Проверьте результат перед возобновлением.")
            message = (
                "Отклик подтверждён на HH."
                if result == "sent"
                else "Статус отправки неизвестен. Проверьте отклики на HH."
            )
            await bot.send_message(self.config.owner, f"Вакансия {identifier}. {message}")
