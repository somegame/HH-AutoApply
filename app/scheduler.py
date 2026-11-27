import asyncio
import hashlib
import json
import logging
import sqlite3
import time
from contextlib import suppress
from datetime import datetime

from app.browser_workflow import BrowserWorkflow
from app.diagnostics import resource_warning
from app.hh_browser import HHLoginRequired
from app.services import (
    AUTO_ANALYSIS_POLICY,
    Groq,
    GroqUnavailable,
    ServiceError,
    cache_key,
    grounded_auto_letter,
    prepared_input,
)
from app.vacancies import filter_reason

log = logging.getLogger(__name__)


def local_day(config):
    return datetime.now(config.zone).date().isoformat()


class Scheduler:
    def __init__(self, config, db, client):
        self.config = config
        self.db = db
        self.groq = Groq(client, config)
        self.browser = BrowserWorkflow(config, db)
        self.task = None
        self.warning = None

    async def start(self, bot):
        if self.task is None or self.task.done():
            if (
                self.config.groq_key
                and self.db.get("browser_search")
                and self.db.get("groq_consent")
            ):
                resume = self.db.get("resume", "")
                if resume:
                    self.db.refresh_auto_drafts(
                        AUTO_ANALYSIS_POLICY, hashlib.sha256(resume.encode()).hexdigest()
                    )
            self.task = asyncio.create_task(self.loop(bot))

    async def shutdown(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def loop(self, bot):
        async with asyncio.TaskGroup() as workers:
            for handler in (self.tick_reminders, self.tick_browser, self.tick_analysis):
                workers.create_task(self.run_worker(bot, handler))

    async def run_worker(self, bot, handler):
        while True:
            try:
                warning = resource_warning(self.config.data_dir)
                previous, self.warning = self.warning, warning
                if warning and warning != previous:
                    await bot.send_message(self.config.owner, "Предупреждение. " + warning)
                if not warning:
                    await handler(bot)
            except Exception as exc:
                log.warning("Ошибка фонового цикла %s (%s)", handler.__name__, type(exc).__name__)
                with suppress(OSError, sqlite3.Error):
                    self.db.set("scheduler_error", type(exc).__name__)
            await asyncio.sleep(5)

    async def tick(self, bot):
        await asyncio.gather(
            self.tick_reminders(bot), self.tick_browser(bot), self.tick_analysis(bot)
        )

    async def tick_reminders(self, bot):
        now = int(time.time())
        if self.db.get("reminders", False) and now >= self.db.get("next_reminder", 0):
            from app.telegram_ui import search_keyboard

            settings = self.db.settings(self.config)
            self.db.set("next_reminder", now + settings["interval"] * 60)
            await bot.send_message(
                self.config.owner,
                "Откройте поиск HH или проверьте сохранённый автопоиск.",
                reply_markup=search_keyboard(settings),
            )

    async def tick_browser(self, bot):
        if not (self.browser.available and self.db.get("browser_search")):
            return
        try:
            await self.browser.tick(bot, self.db.settings(self.config), local_day(self.config))
        except ServiceError as exc:
            notice = str(exc)
            if isinstance(exc, HHLoginRequired):
                self.db.set("hh_needs_login", True)
            if not exc.retry:
                self.browser.pause(notice)
                await bot.send_message(self.config.owner, notice)
                return
            if not self.db.get("browser_search"):
                await bot.send_message(self.config.owner, self.db.get("browser_notice") or notice)
                return
            previous = self.db.get("browser_notice")
            failures = self.db.get("browser_failures", 0) + 1
            delay = max(exc.retry, min(900, 60 * 2 ** min(failures - 1, 4)))
            self.db.set_many(
                {
                    "next_browser_operation": int(time.time()) + delay,
                    "browser_failures": failures,
                    "browser_notice": notice,
                }
            )
            if notice != previous:
                await bot.send_message(
                    self.config.owner, notice + " Поиск повторится автоматически."
                )

    def analysis_status(self):
        budget = self.db.get("groq_usage", {})
        used = budget.get("count", 0) if budget.get("day") == local_day(self.config) else 0
        notice = self.db.get("groq_notice", "")
        retry_at = self.db.get("next_groq", 0) if notice else 0
        return {
            "processing": self.db.get("processing", True),
            "groq_used": used,
            "groq_limit": self.config.groq_daily_limit,
            "groq_retry_at": retry_at,
            "groq_notice": notice,
        }

    async def tick_analysis(self, bot):
        row = self.db.pending()
        if not (row and self.db.get("processing", True)):
            return
        vacancy = json.loads(row["details"])
        settings = self.db.settings(self.config) | vacancy.pop("mini_filters", {})
        reason = filter_reason(vacancy, settings)
        source = vacancy.get("source")
        discovered = bool(vacancy.get("auto_discovered")) or source == "browser"
        if reason and not (discovered and vacancy.get("description")):
            self.db.finish_analysis(
                row["id"], "filtered", {"reason": reason}, None, expected_details=row["details"]
            )
            return
        resume = self.db.get("resume", "")
        if not resume or not self.db.get("groq_consent", False) or not self.config.groq_key:
            return
        try:
            result = await self.analyze(resume, vacancy, settings, int(time.time()))
        except GroqUnavailable as exc:
            failures = self.db.get("groq_failures", 0) + 1
            delay = max(exc.retry, min(900, 60 * 2 ** min(failures - 1, 4)))
            self.db.set_many(
                {
                    "next_groq": int(time.time()) + delay,
                    "groq_failures": failures,
                    "groq_notice": str(exc),
                }
            )
            return
        except ServiceError as exc:
            if self.db.get("groq_notice"):
                self.db.set_many({"groq_notice": "", "groq_failures": 0})
            attempt = row["attempts"] + 1
            delay = max(exc.retry, 60 * 2 ** (attempt - 1)) if attempt < 3 else 0
            self.db.failure(row["id"], attempt, str(exc), delay, expected_details=row["details"])
            if not delay and not discovered:
                await bot.send_message(self.config.owner, f"Вакансия {row['id']}. {exc}")
            return
        if result is None:
            return
        if self.db.get("resume", "") != resume or not self.db.get("groq_consent", False):
            return
        qualifies = (
            not reason and result["score"] >= settings["score"] and bool(result["letter"].strip())
        )
        safe_letter = (
            grounded_auto_letter(result, resume, vacancy, settings["score"], skills_threshold=0)
            if source == "browser" and qualifies
            else ""
        )
        if safe_letter:
            result = result | {"letter": safe_letter}
        status = "draft" if qualifies else "filtered"
        auto_queued = (
            safe_letter
            and result["skills_score"] >= settings["skills_score"]
            and self.db.get("browser_auto_enabled")
        )
        if qualifies and (settings["mode"] == "manual" or auto_queued):
            status = "approved"
        result = result | {"resume_hash": hashlib.sha256(resume.encode()).hexdigest()}
        if reason:
            result["filter_reason"] = reason
        if source == "browser":
            result = result | {"analysis_policy": AUTO_ANALYSIS_POLICY}
            if safe_letter:
                result["auto_policy"] = AUTO_ANALYSIS_POLICY
        updated = self.db.finish_analysis(
            row["id"],
            status,
            result,
            result["letter"] if qualifies else None,
            expected_details=row["details"],
        )
        if not updated or discovered:
            return
        from app.telegram_ui import button, markup

        summary = "Письмо готово." if qualifies else "Вакансия не прошла условия."
        await bot.send_message(
            self.config.owner,
            f"{vacancy['title']} · {result['score']}%. {summary}",
            reply_markup=markup([[button("Открыть карточку", f"v:{row['id']}")]]),
        )

    async def analyze(self, resume, vacancy, settings, now):
        data = prepared_input(resume, vacancy, settings)
        key = cache_key(self.config.model, data)
        cached = self.db.cached(key)
        if cached is not None:
            return cached
        if now < self.db.get("next_groq", 0):
            return None
        day = local_day(self.config)
        budget = self.db.get("groq_usage", {})
        used = budget.get("count", 0) if budget.get("day") == day else 0
        if used >= self.config.groq_daily_limit:
            return None
        self.db.set_many({"groq_usage": {"day": day, "count": used + 1}, "next_groq": now + 12})
        result = await self.groq.analyze(data)
        self.db.cache(key, result)
        if self.db.get("groq_notice"):
            self.db.set_many({"groq_notice": "", "groq_failures": 0})
        return result
