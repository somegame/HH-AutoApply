import asyncio
import logging
import time

from app.job_sources import JobSources
from app.services import ServiceError

log = logging.getLogger(__name__)
BACKGROUND_SOURCES = ("trudvsem", "superjob")


class JobHub:
    def __init__(self, config, db, browser, client):
        self.config = config
        self.db = db
        self.sources = JobSources(config, browser, client)

    def available(self, source):
        return source == "trudvsem" or (source == "superjob" and bool(self.config.superjob_key))

    def state(self):
        settings = self.db.get("hub_sources", {})
        return {
            source: self.db.get(f"hub_search_{source}", {})
            | {"available": self.available(source), "enabled": bool(settings.get(source))}
            for source in BACKGROUND_SOURCES
        }

    def configure(self, source, enabled):
        if source not in BACKGROUND_SOURCES or type(enabled) is not bool:
            raise ValueError("Выберите площадку для фонового поиска.")
        if enabled and not self.available(source):
            raise ServiceError("Для поиска SuperJob нужен ключ приложения на Bothost.")
        current = self.db.get("hub_sources", {})
        if bool(current.get(source)) == enabled:
            return
        updates = {
            "hub_sources": current | {source: enabled},
            f"hub_search_{source}": {
                "message": "Следующий поиск запланирован." if enabled else "",
                "next_at": 0,
                "revision": self.db.get(f"hub_search_{source}", {}).get("revision", 0) + 1,
            },
        }
        if enabled:
            updates["processing"] = True
        self.db.set_many(updates)

    async def tick(self, _bot):
        configured = self.db.get("hub_sources", {})
        now = int(time.time())
        sources = [
            source
            for source in BACKGROUND_SOURCES
            if configured.get(source)
            and self.available(source)
            and self.db.get(f"hub_search_{source}", {}).get("next_at", 0) <= now
        ]
        if not sources:
            return
        settings = self.db.settings(self.config)
        await asyncio.gather(*(self.scan(source, settings, now) for source in sources))

    async def scan(self, source, settings, now):
        key = f"hub_search_{source}"
        revision = self.db.get(key, {})
        delay = settings["interval"] * 60
        rows, message = [], ""
        try:
            async with asyncio.timeout(20):
                rows = await self.sources.search(settings, source)
        except (ServiceError, TimeoutError) as exc:
            delay = max(120, min(900, delay), getattr(exc, "retry", 0))
            message = str(exc) or "Площадка долго отвечает. Поиск повторится."
        except Exception as exc:
            log.warning("Фоновый поиск %s (%s)", source, type(exc).__name__)
            delay = 300
            message = "Поиск временно недоступен. Проверим площадку позже."
        if not self.db.get("hub_sources", {}).get(source) or self.db.get(key, {}) != revision:
            return
        added = sum(
            self.db.add_discovered(identifier, job)
            for identifier, job in rows
            if job.get("description", "").strip()
        )
        self.db.set(
            key,
            {
                "revision": revision.get("revision", 0),
                "last_at": now,
                "next_at": now + delay,
                "found": added,
                "message": message
                or (f"Новых вакансий: {added}." if added else "Новых вакансий пока нет."),
            },
        )
