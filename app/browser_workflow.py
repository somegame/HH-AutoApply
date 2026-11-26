import asyncio
import hashlib
import json
import time
from contextlib import suppress
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError

from app import hh_browser as hh
from app.diagnostics import resource_warning
from app.local_browser import LocalBrowser
from app.login_view import SESSION_SECONDS, LoginView, waiting_for_sms
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
        self.pause_revision = 0

    @property
    def available(self):
        return self.config.browser_engine == "chromium"

    def require_browser(self):
        if not self.available:
            raise ServiceError("Укажите BROWSER_ENGINE=chromium в Bothost.")
        self.provider.check_installation()
        if warning := resource_warning(self.config.data_dir):
            raise ServiceError(warning)

    async def start_login(self, notify=None):
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
        self.login_task = asyncio.create_task(self.login(ready, notify))
        try:
            return await asyncio.shield(ready)
        except BaseException:
            if not ready.done():
                ready.cancel()
                await self.cancel_login()
            raise

    async def login(self, ready, notify):
        async with self.lock:
            status = "failed"
            try:
                async with self.provider.session(SESSION_SECONDS + 45, interactive=True) as (
                    context,
                    page,
                ):
                    await hh.open_page(page, hh.HH + "/account/login", login_allowed=True)
                    ready.set_result(await self.view.issue(page))
                    async with asyncio.timeout(SESSION_SECONDS):
                        result = await self.wait_for_account(context, page)
                    await self.provider.save(context)
                self.db.set("hh_resume_choices", result)
                status = "done"
            except TimeoutError:
                result = ServiceError("Срок ссылки истёк. Откройте новое окно входа.")
            except hh.HHAttention as exc:
                result = exc
                status = "blocked"
            except ServiceError as exc:
                result = exc
            except asyncio.CancelledError:
                if not ready.done():
                    ready.cancel()
                await self.view.revoke()
                raise
            except Exception:
                result = ServiceError("Операция входа не завершилась.")
            await self.view.revoke(status if ready.done() and not ready.cancelled() else None)
            if not ready.done():
                ready.set_exception(result)
        if notify and not ready.cancelled() and ready.exception() is None:
            try:
                await notify(result)
            except Exception:
                # The selection remains in the database for a later Telegram request.
                pass
        return result

    async def wait_for_account(self, context, page):
        last_checked = None
        while True:
            try:
                await asyncio.wait_for(self.view.check.wait(), 0.8)
                requested = True
            except TimeoutError:
                requested = False
            self.view.check.clear()
            await hh.guard(page, login_allowed=True)
            if await waiting_for_sms(page):
                continue
            path = urlsplit(page.url).path
            login_path = any(part in path for part in ("/login", "/oauth", "/auth", "/account"))
            if not requested and (login_path or page.url == last_checked):
                continue
            last_checked = page.url
            self.view.change_status("checking")
            probe = None
            try:
                probe = await context.new_page()
                return await hh.authorized_resumes(probe)
            except hh.HHAccessBarrier:
                raise
            except (hh.HHAttention, PlaywrightError):
                if page.is_closed():
                    raise ServiceError(
                        "Chromium закрыл вкладку HH. Откройте новое окно входа."
                    ) from None
                self.view.change_status("retry")
            finally:
                if probe is not None:
                    with suppress(PlaywrightError):
                        await probe.close()
                with suppress(PlaywrightError):
                    await page.bring_to_front()

    async def finish_login(self):
        if self.login_task and not self.login_task.done():
            self.view.check.set()
            return None
        if self.login_task:
            result = await self.login_task
            if isinstance(result, ServiceError):
                raise result
            return result
        choices = self.db.get("hh_resume_choices", {})
        if choices:
            return choices
        raise ServiceError("Сначала откройте окно входа HH в боте.")

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
        self.pause_revision += 1
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

    async def submit_selected(self, bot, row, day):
        selected = self.readiness()
        if not (self.config.auto_send and self.config.persistent):
            raise ServiceError("Для отправки через Chromium нужны AUTO_SEND и постоянный Volume.")
        details = json.loads(row["details"])
        if (
            details.get("source") not in {"browser", "mini_hh"}
            or row["status"] not in {"draft", "approved"}
            or not row["letter"]
        ):
            raise ServiceError("У этой вакансии нет готового отклика для отправки через HH.")
        if self.lock.locked():
            raise ServiceError("Браузер занят другой операцией.")
        revision = self.pause_revision
        async with self.lock:
            async with self.provider.session(45) as (_, page):
                resumes = await hh.authorized_resumes(page)
                if selected["id"] not in resumes:
                    raise hh.HHLoginRequired("Выбранное резюме недоступно. Проверьте вход.")
                await self.send_one(
                    bot,
                    page,
                    row,
                    selected,
                    self.db.settings(self.config),
                    day,
                    manual_revision=revision,
                )

    async def send_one(self, bot, page, row, resume, settings, day, *, manual_revision=None):
        identifier = row["id"]
        details = json.loads(row["details"])
        result = json.loads(row["result"] or "{}")
        manual = manual_revision is not None
        allowed_states = ("draft", "approved") if manual else ("approved",)
        effective_settings = settings | details.get("mini_filters", {})
        resume_hash = hashlib.sha256(self.db.get("resume", "").encode()).hexdigest()
        if (
            filter_reason(details, effective_settings)
            or result.get("score", -1) < settings["score"]
            or result.get("resume_hash") != resume_hash
            or not row["letter"]
        ):
            self.db.transition(identifier, allowed_states, "deferred")
            return
        if manual and self.pause_revision != manual_revision:
            raise ServiceError("Отправка остановлена.")
        reserved = False

        def reserve():
            nonlocal reserved
            current = self.db.vacancy(identifier)
            if (
                (not manual and not self.db.get("browser_auto_enabled"))
                or (manual and self.pause_revision != manual_revision)
                or current["letter"] != row["letter"]
                or self.db.get("hh_selected_resume") != resume
                or hashlib.sha256(self.db.get("resume", "").encode()).hexdigest() != resume_hash
                or not self.db.get("groq_consent")
            ):
                return False
            if self.db.settings(self.config) != settings:
                return False
            reserved = (
                self.db.transition(
                    identifier, allowed_states, "sending", day=day, limit=settings["limit"]
                )
                == "ok"
            )
            return reserved

        try:
            result = await hh.apply(page, identifier, resume["id"], row["letter"], reserve)
        except hh.HHQuestions as exc:
            self.db.transition(identifier, allowed_states, "questions")
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
            self.db.transition(identifier, allowed_states, "duplicate")
        elif manual and result == "limit":
            raise ServiceError("Отправка отменена. Проверьте статус отклика и дневной лимит.")
        if result in {"sent", "unknown"}:
            if result == "unknown":
                self.pause("HH не подтвердил отправку. Проверьте результат перед возобновлением.")
            message = (
                "Отклик подтверждён на HH."
                if result == "sent"
                else "Статус отправки неизвестен. Проверьте отклики на HH."
            )
            await bot.send_message(self.config.owner, f"Вакансия {identifier}. {message}")
