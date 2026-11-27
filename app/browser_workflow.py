import asyncio
import hashlib
import json
import logging
import time
from collections import Counter
from contextlib import closing, suppress
from datetime import datetime
from urllib.parse import urlsplit

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import TimeoutError as BrowserTimeout

from app import hh_browser as hh
from app.diagnostics import resource_warning
from app.local_browser import LocalBrowser
from app.login_view import SESSION_SECONDS, LoginView, waiting_for_sms
from app.services import (
    AUTO_ANALYSIS_POLICY,
    ServiceError,
    ai_allowed,
    auto_letter_supported,
    clean_text,
)
from app.vacancies import filter_reason, search_links, vacancy_id

log = logging.getLogger(__name__)
DISCOVERY_BATCH_SIZE = 3
APPLICATION_MESSAGES = {
    "analysis_pending": "Ожидает анализа.",
    "analysis_error": "Анализ завершился ошибкой.",
    "overall_below_threshold": "Общая оценка ниже текущего порога.",
    "skills_below_threshold": "Совпадение навыков ниже текущего порога.",
    "missing_requirements": "В резюме не подтверждены обязательные требования.",
    "stale_analysis": "Требуется анализ по актуальному резюме и настройкам.",
    "letter_review": "Письмо требует ручной проверки.",
    "search_filters": "Вакансия не проходит сохранённые фильтры.",
    "manual_review": "Проверьте форму или вопросы работодателя на HH.",
    "unknown_outcome": "Сверьте историю откликов на HH.",
    "manual_only": "Карточка доступна для ручного отклика.",
}


def next_query(items, offset, available):
    for index in (*range(offset, len(items)), *range(offset)):
        if available(items[index]):
            return index
    return None


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

    @property
    def account_connected(self):
        return (
            self.available
            and self.provider.state.is_file()
            and not self.db.get("hh_needs_login", False)
            and self.db.get("hh_session_verified", bool(self.db.get("hh_resume_choices", {})))
        )

    def require_browser(self):
        if not self.available:
            raise ServiceError("Укажите BROWSER_ENGINE=chromium в Bothost.")
        self.provider.check_installation()
        if warning := resource_warning(self.config.data_dir):
            raise ServiceError(warning)

    async def start_login(self, notify=None, *, in_mini=False, fresh=False, resume_mode=False):
        self.require_browser()
        if resume_mode and not self.account_connected:
            raise ServiceError("Сначала подключите аккаунт HH.")
        await self.view.start()
        if not self.view.runner:
            raise ServiceError("Настройте LOGIN_PUBLIC_URL и HTTPS-домен в Bothost.")
        if self.login_task and not self.login_task.done():
            raise ServiceError("Окно входа уже открыто. Завершите вход или отмените его.")
        if self.lock.locked():
            raise ServiceError("Браузер занят. Повторите вход чуть позже.")
        if not resume_mode:
            self.pause("Открыто окно входа в HH.")
        loop = asyncio.get_running_loop()
        ready = loop.create_future()
        self.login_task = asyncio.create_task(
            self.login(ready, notify, in_mini, fresh, resume_mode)
        )
        try:
            return await asyncio.shield(ready)
        except BaseException:
            if not ready.done():
                ready.cancel()
                await self.cancel_login()
            raise

    async def login(self, ready, notify, in_mini, fresh, resume_mode):
        async with self.lock:
            status = "failed"
            try:
                async with self.provider.session(
                    SESSION_SECONDS + 45, interactive=True, guest=fresh
                ) as (
                    context,
                    page,
                ):
                    path = "/" if resume_mode else "/account/login"
                    await hh.open_page(page, hh.HH + path, login_allowed=True)
                    url = await self.view.issue(page)
                    if in_mini:
                        route = "/mini/hh-login?mode=resume#" if resume_mode else "/mini/hh-login#"
                        url = url.replace("/hh-login#", route, 1)
                    ready.set_result(url)
                    async with asyncio.timeout(SESSION_SECONDS):
                        result = await self.wait_for_account(context, page, resume_mode=resume_mode)
                    if resume_mode:
                        identifier = next(iter(result))
                        async with self.view.lock:
                            name, text = await hh.read_owned_resume(page, identifier)
                    await self.provider.save(context)
                    if resume_mode:
                        self.bind_owned_resume(identifier, name, clean_text(text))
                    else:
                        self.remember_account(result, fresh=fresh)
                    if in_mini and len(result) == 1 and not resume_mode:
                        identifier, name = next(iter(result.items()))
                        try:
                            async with asyncio.timeout(15):
                                resume = clean_text(await hh.read_resume(page, identifier))
                                self.bind_resume(identifier, name, resume)
                        except (ServiceError, PlaywrightError, TimeoutError):
                            pass
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
            except Exception as exc:
                log.warning("Сбой подтверждения входа HH (%s)", type(exc).__name__)
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

    def remember_account(self, resumes, *, fresh=False):
        selected = self.db.get("hh_selected_resume", {})
        values = {"hh_session_verified": True, "hh_needs_login": False, "hh_resumes_status": ""}
        if resumes or fresh:
            values["hh_resume_choices"] = resumes
        if fresh or (resumes and selected and selected.get("id") not in resumes):
            values.update(hh_selected_resume={}, groq_consent=False, gemini_consent=False)
        self.db.set_many(values)

    async def wait_for_account(self, context, page, *, resume_mode=False):
        last_checked, observed_url, observed_at = None, None, 0
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
            try:
                if owned := await hh.owned_resume(page):
                    return owned
                if not resume_mode and await hh.dashboard_authenticated(page):
                    return {}
            except PlaywrightError:
                if page.is_closed():
                    raise ServiceError(
                        "Chromium закрыл вкладку HH. Откройте окно входа заново."
                    ) from None
            if resume_mode:
                if requested:
                    self.view.change_status("retry")
                continue
            path = urlsplit(page.url).path
            login_path = any(part in path for part in ("/login", "/oauth", "/auth", "/account"))
            if observed_url != page.url:
                observed_url, observed_at = page.url, time.monotonic()
            if not requested and (login_path or page.url == last_checked):
                continue
            if (
                not requested
                and path.rstrip("/") in hh.DASHBOARD_PATHS
                and time.monotonic() - observed_at < 2.5
            ):
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
        if choices or self.account_connected:
            return choices
        raise ServiceError("Сначала откройте окно входа HH в боте.")

    async def cancel_login(self):
        if self.login_task:
            self.login_task.cancel()
            await asyncio.gather(self.login_task, return_exceptions=True)
            self.login_task = None
        await self.view.revoke()

    async def refresh_resumes(self, url=None):
        self.require_browser()
        if not self.account_connected:
            raise ServiceError("Подключите HH, чтобы загрузить резюме.")
        if self.lock.locked():
            raise ServiceError("Браузер занят другой операцией.")
        identifier = hh.resume_identifier(url) if url is not None else ""
        async with self.lock:
            try:
                single = None
                warning = ""
                async with self.provider.session(35) as (_, page):
                    if identifier:
                        await hh.open_page(page, f"{hh.HH}/resume/{identifier}")
                        name, resume = await hh.read_owned_resume(page, identifier)
                    else:
                        try:
                            resumes = await hh.authorized_resumes(page)
                        except hh.HHLoginRequired:
                            await hh.open_page(page, hh.HH + "/", login_allowed=True)
                            if not await hh.dashboard_authenticated(page):
                                raise
                            raise hh.HHResumeUnavailable(
                                "HH не показал список резюме. Откройте своё резюме в браузере HH."
                            ) from None
                        if len(resumes) == 1:
                            candidate, label = next(iter(resumes.items()))
                            try:
                                async with asyncio.timeout(12):
                                    text = clean_text(await hh.read_resume(page, candidate))
                                if len(text.strip()) >= 60:
                                    single = (candidate, label, text)
                            except (hh.HHAccessBarrier, hh.HHLoginRequired):
                                raise
                            except (ServiceError, PlaywrightError, TimeoutError):
                                warning = (
                                    "Резюме найдено. Откройте его карточку, чтобы загрузить текст."
                                )
                if identifier:
                    self.bind_owned_resume(identifier, name, clean_text(resume))
                    return self.db.get("hh_resume_choices", {})
                self.remember_account(resumes)
                if single:
                    self.bind_resume(*single)
                elif warning:
                    self.db.set("hh_resumes_status", warning)
            except hh.HHAccessBarrier as exc:
                self.pause(str(exc))
                self.db.set("hh_resumes_status", str(exc))
                raise
            except hh.HHLoginRequired:
                raise
            except ServiceError as exc:
                self.db.set("hh_resumes_status", str(exc))
                raise
        return resumes

    def bind_owned_resume(self, identifier, name, resume):
        self.bind_resume(identifier, name, resume)
        self.db.set_many(
            {
                "hh_session_verified": True,
                "hh_needs_login": False,
                "hh_resume_choices": self.db.get("hh_resume_choices", {}) | {identifier: name},
                "hh_resumes_status": "",
            }
        )

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
        self.bind_resume(identifier, choices[identifier], resume)
        return choices[identifier]

    def bind_resume(self, identifier, name, resume):
        if len(resume.strip()) < 60:
            raise ServiceError("В резюме недостаточно профессионального текста для анализа.")
        unchanged = (
            self.db.get("hh_selected_resume", {}).get("id") == identifier
            and self.db.get("resume") == resume
        )
        self.db.set_many(
            {
                "resume": resume,
                "hh_selected_resume": {"id": identifier, "name": name},
                "groq_consent": unchanged and self.db.get("groq_consent", False),
                "gemini_consent": unchanged and self.db.get("gemini_consent", False),
                "browser_auto_enabled": unchanged and self.db.get("browser_auto_enabled", False),
            }
        )

    def readiness(self):
        self.require_browser()
        if self.db.get("hh_needs_login", False):
            raise ServiceError("Снова подключите HH перед отправкой откликов.")
        selected = self.db.get("hh_selected_resume", {})
        if not selected.get("id") or not self.db.get("resume"):
            raise ServiceError("Войдите в HH и выберите резюме.")
        if not ai_allowed(self.config, self.db):
            raise ServiceError("Добавьте ключ модели на Bothost и разрешите анализ в профиле.")
        return selected

    def application_status(self):
        settings = self.db.settings(self.config)
        day = datetime.now(self.config.zone).date().isoformat()
        totals, rows = self.db.hh_application_overview(day)
        resume = self.db.get("resume", "")
        digest = hashlib.sha256(resume.encode()).hexdigest()
        blocked = Counter()
        examples = []
        for row in rows[:200]:
            reason, message = self.application_block(row, resume, settings, digest)
            if reason is None:
                continue
            blocked[reason] += 1
            title = json.loads(row["details"]).get("title")
            if title and len(examples) < 3:
                examples.append(
                    {"id": row["id"], "title": title, "reason": reason, "message": message}
                )
        blocked.update(
            {
                "analysis_pending": totals["pending"] - blocked["analysis_pending"],
                "unknown_outcome": totals["unknown"] - blocked["unknown_outcome"],
            }
        )
        eligible = sum(1 for _ in self.eligible_applications(resume, settings, digest))
        enabled = bool(self.db.get("browser_auto_enabled"))
        search = bool(self.db.get("browser_search"))
        retry = self.db.get("next_browser_operation", 0)
        notice = self.db.get("browser_notice", "")
        reason, message = self.application_summary(enabled, search, eligible, totals, settings)
        if reason in {"auto_disabled", "no_candidates"} and notice:
            message = f"{message} {notice}"
        return totals | {
            "enabled": enabled,
            "ready": reason in {"ready", "no_candidates"},
            "reason": reason,
            "message": message,
            "eligible": eligible,
            "blocked": dict(blocked),
            "examples": examples,
            "daily_limit": settings["limit"],
            "retry_at": retry if reason == "ready" and retry > time.time() else 0,
            "sample_limited": len(rows) > 200,
        }

    def application_summary(self, enabled, search, eligible, totals, settings):
        if not self.available:
            return "browser_unavailable", "Включите Chromium в настройках Bothost."
        if not self.config.persistent:
            return "storage_unavailable", "Для отправки подтвердите постоянное хранилище Bothost."
        if not self.config.auto_send:
            return "auto_not_configured", "Разрешите автоотклики переменной AUTO_SEND на Bothost."
        if not self.account_connected:
            return "login_required", "Подключите аккаунт HH для отправки."
        if not self.db.get("hh_selected_resume", {}).get("id") or not self.db.get("resume"):
            return "resume_required", "Выберите резюме HH в разделе «Аккаунты»."
        if not ai_allowed(self.config, self.db):
            return "analysis_setup", "Проверьте ключ модели на Bothost и согласие в профиле."
        if not (enabled and search):
            return "auto_disabled", "Автоотклики выключены."
        if totals["used_today"] >= settings["limit"]:
            return "daily_limit", "Дневной лимит откликов достигнут."
        if not eligible:
            return "no_candidates", "Сейчас нет вакансий, готовых к автоотклику."
        return "ready", "Автоотклики включены. Бот отправит готовые письма по очереди."

    def application_block(self, row, resume, settings, digest):
        details = json.loads(row["details"])
        result = json.loads(row["result"] or "{}")
        status = row["status"]
        reason = {
            "queued": "analysis_pending",
            "error": "analysis_error",
            "unknown": "unknown_outcome",
            "questions": "manual_review",
            "deferred": "manual_only",
        }.get(status)
        message = ""
        if reason in {"analysis_error", "manual_review"}:
            message = row["error"] or ""
        if reason is None:
            reason, message = self.application_result_block(
                row, details, result, resume, settings, digest
            )
        return reason, message or APPLICATION_MESSAGES.get(reason, "")

    def application_result_block(self, row, details, result, resume, settings, digest):
        if details.get("source") != "browser":
            return "manual_only", ""
        try:
            filtered = filter_reason(details, settings)
        except (KeyError, TypeError, AttributeError):
            return "search_filters", "В карточке недостаточно сведений для проверки фильтров."
        if filtered:
            return "search_filters", filtered
        score, skills = result.get("score"), result.get("skills_score")
        if result.get("resume_hash") != digest or type(score) is not int or type(skills) is not int:
            return "stale_analysis", ""
        if score < settings["score"]:
            return (
                "overall_below_threshold",
                f"Общая оценка {score}% при пороге {settings['score']}%.",
            )
        if skills < settings["skills_score"]:
            return (
                "skills_below_threshold",
                f"Навыки {skills}% при пороге {settings['skills_score']}%.",
            )
        if result.get("missing"):
            return "missing_requirements", ""
        old_threshold = result.get("analysis_threshold")
        if row["status"] == "filtered" and (
            type(old_threshold) is not int or old_threshold > settings["score"]
        ):
            return "stale_analysis", ""
        if (
            result.get("auto_policy") != AUTO_ANALYSIS_POLICY
            or row["status"] not in {"draft", "approved"}
            or not auto_letter_supported(
                result,
                resume,
                details,
                settings["score"],
                row["letter"],
                skills_threshold=settings["skills_score"],
            )
        ):
            return "letter_review", ""
        return None, ""

    def eligible_applications(self, resume, settings, digest):
        candidates = self.db.browser_candidates(
            policy=AUTO_ANALYSIS_POLICY,
            resume_hash=digest,
            min_score=settings["score"],
            min_skills=settings["skills_score"],
        )
        with closing(candidates):
            for row in candidates:
                if self.application_block(row, resume, settings, digest)[0] is None:
                    yield row

    def start_search(self, *, send=False):
        if send:
            self.readiness()
            if not self.account_connected:
                raise ServiceError("Подключите аккаунт HH для автооткликов.")
        else:
            self.require_browser()
            if not self.account_connected:
                raise ServiceError("Подключите HH, чтобы запустить фоновый поиск.")
        if send and not (self.config.auto_send and self.config.persistent):
            raise ServiceError(
                "Для автооткликов нужны AUTO_SEND=true и подтверждённый постоянный Volume."
            )
        if send:
            digest = hashlib.sha256(self.db.get("resume", "").encode()).hexdigest()
            self.db.refresh_auto_drafts(
                AUTO_ANALYSIS_POLICY, digest, min_score=self.db.settings(self.config)["score"]
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

    def pause(self, notice, *, keep_search=False):
        self.pause_revision += 1
        values = {"browser_auto_enabled": False, "browser_notice": notice}
        if not keep_search:
            values["browser_search"] = False
        self.db.set_many(values)

    async def tick(self, bot, settings, day):
        if not (self.available and self.db.get("browser_search")) or self.lock.locked():
            return
        now = int(time.time())
        if now < self.db.get("next_browser_operation", 0):
            return
        due = now >= self.db.get("next_browser_search", 0)
        auto = (
            self.config.auto_send and self.config.persistent and self.db.get("browser_auto_enabled")
        )
        row = None
        if auto:
            resume = self.db.get("resume", "")
            digest = hashlib.sha256(resume.encode()).hexdigest()
            self.db.refresh_auto_drafts(AUTO_ANALYSIS_POLICY, digest, min_score=settings["score"])
            if self.db.counts(day)[1] < settings["limit"]:
                with closing(self.eligible_applications(resume, settings, digest)) as eligible:
                    row = next(eligible, None)
        if not (due or row):
            return
        send = row is not None and (not due or self.db.get("browser_send_turn", True))
        self.db.set_many({"next_browser_operation": now + 60, "browser_send_turn": not send})
        count = 0
        async with self.lock:
            async with self.provider.session() as (_, page):
                if send:
                    selected = self.readiness()
                    await hh.verify_resume(page, selected["id"])
                    await self.send_one(bot, page, row, selected, settings, day)
                else:
                    count, next_search = await self.discover(page, settings, now)
                    self.db.set("next_browser_search", next_search)
            if self.db.get("browser_search"):
                self.db.set_many({"browser_failures": 0, "browser_notice": ""})
        if count and now >= self.db.get("next_browser_discovery_notice", 0):
            self.db.set("next_browser_discovery_notice", now + settings["interval"] * 60)
            await bot.send_message(
                self.config.owner, "На HH появились новые вакансии. Откройте список найденных."
            )

    async def discover(self, page, settings, now):
        links = [url for _, url in search_links(settings, title_only=True)]
        signature = hashlib.sha256(json.dumps(links).encode()).hexdigest()
        scan = self.db.get("browser_scan", {})
        if scan.get("signature") != signature:
            scan = {
                "signature": signature,
                "head_due": [0] * len(links),
                "next_pages": [None] * len(links),
                "head_offset": 0,
                "page_offset": 0,
                "prefer_head": True,
                "pending": [],
            }
        revision = self.pause_revision
        if not scan["pending"]:
            fresh = next_query(scan["head_due"], scan["head_offset"], lambda value: value <= now)
            older = next_query(scan["next_pages"], scan["page_offset"], bool)
            if fresh is None and older is None:
                return 0, min(scan["head_due"])
            scan_head = fresh is not None and (older is None or scan["prefer_head"])
            index = fresh if scan_head else older
            target = links[index] if scan_head else scan["next_pages"][index]
            pending, following = await hh.search_page(page, target)
            if scan_head:
                scan["head_due"][index] = now + settings["interval"] * 60
                scan["head_offset"] = (index + 1) % len(links)
            else:
                scan["page_offset"] = (index + 1) % len(links)
            if not scan_head or not scan["next_pages"][index]:
                scan["next_pages"][index] = following
            scan["pending"] = pending
            scan["prefer_head"] = not scan_head
            self.db.set("browser_scan", scan)
        added = 0
        detail_reads = 0
        retries = scan.setdefault("detail_retries", {})
        for identifier in scan["pending"][:]:
            if revision != self.pause_revision:
                break
            row = self.db.vacancy(identifier)
            if row is None:
                if detail_reads == DISCOVERY_BATCH_SIZE:
                    break
                detail_reads += 1
            try:
                added += await self.save_discovered(page, identifier, row)
            except (BrowserTimeout, ServiceError) as exc:
                if isinstance(exc, ServiceError) and not exc.retry:
                    raise
                self.defer_search_detail(scan, identifier)
                raise
            scan["pending"].pop(0)
            retries.pop(identifier, None)
            if row is None:
                self.db.set("browser_scan", scan)
        self.db.set("browser_scan", scan)
        next_search = 0 if scan["pending"] or any(scan["next_pages"]) else min(scan["head_due"])
        return added, next_search

    def defer_search_detail(self, scan, identifier):
        attempts = scan["detail_retries"].get(identifier, 0) + 1
        scan["pending"].pop(0)
        if attempts < 3:
            scan["detail_retries"][identifier] = attempts
            scan["pending"].append(identifier)
        else:
            scan["detail_retries"].pop(identifier, None)
            log.warning("Вакансия HH %s отложена до следующего прохода поиска", identifier)
        self.db.set("browser_scan", scan)

    async def save_discovered(self, page, identifier, row):
        if row:
            details = json.loads(row["details"])
            if details.get("source") == "mini_hh" and details.get("auto_discovered"):
                details.pop("mini_filters", None)
                self.db.add_discovered(identifier, details | {"source": "browser"})
            return False
        try:
            details = await hh.read_vacancy(page, identifier)
        except hh.HHVacancyUnavailable:
            log.warning("HH не показал основные поля вакансии %s", identifier)
            return False
        if not details:
            return False
        duplicate = await hh.already_applied(page)
        if self.db.add_discovered(identifier, details | {"auto_discovered": True}):
            if duplicate:
                self.db.transition(identifier, ("queued",), "duplicate")
                return False
            return True
        return False

    async def reconcile(self, identifier):
        self.require_browser()
        row = self.db.vacancy(identifier)
        if not (row and row["status"] == "unknown" and row["sent_day"]):
            raise ServiceError("Нет отклика с неизвестным результатом для сверки.")
        if self.lock.locked():
            raise ServiceError("Браузер занят. Попробуйте позже.")
        async with self.lock:
            async with self.provider.session() as (_, page):
                selected = self.db.get("hh_selected_resume", {})
                if selected:
                    await hh.verify_resume(page, selected["id"])
                else:
                    await hh.authorized_resumes(page)
                await hh.open_page(page, f"{hh.HH}/vacancy/{identifier}")
                if vacancy_id(page.url) == identifier and await hh.already_applied(page):
                    self.db.complete_browser_application(identifier, "sent", previous="unknown")
                    return True
        return False

    async def submit_selected(self, bot, row, day):
        selected = self.readiness()
        if not self.config.persistent:
            raise ServiceError("Для отправки через HH подключите постоянное хранилище Bothost.")
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
                await hh.verify_resume(page, selected["id"])
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
        allowed_states = ("draft", "approved")
        resume_text = self.db.get("resume", "")
        resume_hash = hashlib.sha256(resume_text.encode()).hexdigest()
        if manual:
            if self.pause_revision != manual_revision:
                raise ServiceError("Отправка остановлена.")
            if (
                filter_reason(details, settings | details.get("mini_filters", {}))
                or result.get("score", -1) < settings["score"]
                or result.get("resume_hash") != resume_hash
                or not row["letter"]
            ):
                raise ServiceError("Отклик требует новой оценки по текущему резюме и фильтрам.")
        elif self.application_block(row, resume_text, settings, resume_hash)[0] is not None:
            return
        revision = self.pause_revision
        reserved = False

        def reserve():
            nonlocal reserved
            current = self.db.vacancy(identifier)
            if (
                (not manual and not self.db.get("browser_auto_enabled"))
                or self.pause_revision != revision
                or current is None
                or current["letter"] != row["letter"]
                or current["details"] != row["details"]
                or current["result"] != row["result"]
                or self.db.get("hh_selected_resume") != resume
                or hashlib.sha256(self.db.get("resume", "").encode()).hexdigest() != resume_hash
                or not ai_allowed(self.config, self.db)
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
            self.db.transition(identifier, allowed_states, "questions", error=str(exc))
            if not manual:
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
        if result == "unknown":
            self.pause("HH не подтвердил отправку. Проверьте результат перед возобновлением.")
        if not manual and result in {"sent", "unknown"}:
            message = (
                "Отклик подтверждён на HH."
                if result == "sent"
                else "Статус отправки неизвестен. Проверьте отклики на HH."
            )
            await bot.send_message(self.config.owner, f"Вакансия {identifier}. {message}")
