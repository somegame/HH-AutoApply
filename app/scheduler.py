import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime

from app.browser_workflow import BrowserWorkflow
from app.diagnostics import resource_warning
from app.services import Groq, ServiceError, cache_key, prepared_input
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
            self.task = asyncio.create_task(self.loop(bot))

    async def shutdown(self):
        if self.task:
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
            self.task = None

    async def loop(self, bot):
        while True:
            try:
                warning = resource_warning(self.config.data_dir)
                if warning and warning != self.warning:
                    self.warning = warning
                    await bot.send_message(self.config.owner, "Предупреждение. " + warning)
                self.warning = warning
                if not warning:
                    await self.tick(bot)
            except Exception as exc:
                log.warning("Ошибка фонового цикла (%s)", type(exc).__name__)
                self.db.set("scheduler_error", type(exc).__name__)
            await asyncio.sleep(5)

    async def tick(self, bot):
        now = int(time.time())
        settings = self.db.settings(self.config)
        if self.db.get("reminders", False) and now >= self.db.get("next_reminder", 0):
            from app.telegram_ui import search_keyboard

            self.db.set("next_reminder", now + settings["interval"] * 60)
            await bot.send_message(
                self.config.owner,
                "Откройте поиск HH или проверьте сохранённый автопоиск.",
                reply_markup=search_keyboard(settings),
            )
        if self.browser.available and self.db.get("browser_search"):
            try:
                await self.browser.tick(bot, settings, local_day(self.config))
            except ServiceError as exc:
                notice = str(exc)
                if exc.retry:
                    self.db.set("next_browser_operation", now + exc.retry)
                    failures = self.db.get("browser_failures", 0) + 1
                    self.db.set("browser_failures", failures)
                    if failures >= 3:
                        self.browser.pause("После трёх сбоев браузер остановлен. " + notice)
                        await bot.send_message(self.config.owner, self.db.get("browser_notice"))
                    elif notice != self.db.get("browser_notice") and self.db.get("browser_search"):
                        self.db.set("browser_notice", notice)
                        await bot.send_message(self.config.owner, notice)
                    elif not self.db.get("browser_search"):
                        await bot.send_message(
                            self.config.owner, self.db.get("browser_notice") or notice
                        )
                else:
                    self.browser.pause(notice)
                    await bot.send_message(self.config.owner, notice)
        row = self.db.pending()
        if not (row and self.db.get("processing", True)):
            return
        vacancy = json.loads(row["details"])
        settings |= vacancy.pop("mini_filters", {})
        reason = filter_reason(vacancy, settings)
        if reason:
            self.db.finish_analysis(row["id"], "filtered", {"reason": reason}, None)
            return
        resume = self.db.get("resume", "")
        if not resume or not self.db.get("groq_consent", False) or not self.config.groq_key:
            return
        try:
            result = await self.analyze(resume, vacancy, settings, now)
        except ServiceError as exc:
            attempt = row["attempts"] + 1
            delay = max(exc.retry, 60 * 2 ** (attempt - 1)) if exc.retry and attempt < 3 else 0
            if exc.retry:
                self.db.set("next_groq", now + max(exc.retry, delay))
            self.db.failure(row["id"], attempt, str(exc), delay)
            if not delay:
                await bot.send_message(self.config.owner, f"Вакансия {row['id']}. {exc}")
            return
        if result is None:
            return
        qualifies = result["score"] >= settings["score"] and bool(result["letter"].strip())
        status = "draft" if qualifies else "filtered"
        if qualifies and (
            settings["mode"] == "manual"
            or (vacancy.get("source") == "browser" and self.db.get("browser_auto_enabled"))
        ):
            status = "approved"
        if vacancy.get("source") in {"browser", "mini_hh"}:
            result = result | {"resume_hash": hashlib.sha256(resume.encode()).hexdigest()}
        self.db.finish_analysis(row["id"], status, result, result["letter"] if qualifies else None)
        from app.telegram_ui import button, markup

        await bot.send_message(
            self.config.owner,
            f"{vacancy['title']} · {result['score']}%. Анализ завершён.",
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
        if used >= 50:
            return None
        self.db.set("groq_usage", {"day": day, "count": used + 1})
        self.db.set("next_groq", now + 12)
        result = await self.groq.analyze(data)
        self.db.cache(key, result)
        return result
