import asyncio
import time
from collections import OrderedDict
from datetime import datetime

from app.commute import Commutes, commute_estimate, point
from app.ranking import rank_job, work_formats
from app.services import ServiceError

PREFERENCES = {
    "home": None,
    "travel_mode": "transit",
    "preferred_format": "any",
    "max_commute": 0,
}
TRAVEL_MODES = {"transit", "car", "walk", "cycle"}
FORMATS = {"any", "remote", "hybrid", "office"}


def commute_limit(value):
    if type(value) is int and 0 <= value <= 180:
        return value
    if isinstance(value, str) and value.isascii() and value.isdecimal() and len(value) <= 3:
        limit = int(value)
        if limit <= 180:
            return limit
    raise ValueError("Укажите от 0 до 180 минут в одну сторону.")


def preferred_format(value):
    if not isinstance(value, str) or value not in FORMATS:
        raise ValueError("Выберите формат работы.")
    return value


class VacancyDiscovery:
    def __init__(self, config, db, client):
        self.config = config
        self.db = db
        self.provider = Commutes(config, client)
        self.offices = OrderedDict()
        self.routes = OrderedDict()
        self.provider_lock = asyncio.Lock()

    def preferences(self):
        return (
            PREFERENCES
            | self.db.get("vacancy_discovery", {})
            | {
                "route_provider": self.provider.available,
            }
        )

    def configure(self, data):
        current = self.preferences()
        current.pop("route_provider")
        if "home" in data:
            raw = data["home"]
            home = point(raw) if raw is not None else None
            if raw is not None and not home:
                raise ValueError("Проверьте широту и долготу точки отправления.")
            current["home"] = {key: round(home[key], 5) for key in ("lat", "lon")} if home else None
            self.routes.clear()
        if "travel_mode" in data:
            value = data["travel_mode"]
            if not isinstance(value, str) or value not in TRAVEL_MODES:
                raise ValueError("Выберите способ передвижения.")
            current["travel_mode"] = value
        if "preferred_format" in data:
            current["preferred_format"] = preferred_format(data["preferred_format"])
        if "max_commute" in data:
            current["max_commute"] = commute_limit(data["max_commute"])
        self.db.set("vacancy_discovery", current)
        return self.preferences()

    async def locate(self, address):
        if not isinstance(address, str) or not 4 <= len(address.strip()) <= 230 or "\n" in address:
            raise ValueError("Введите город и адрес одной строкой.")
        if self.provider_lock.locked():
            raise ServiceError("Запрос к картам уже выполняется.")
        async with self.provider_lock:
            return await self.provider.locate(address.strip())

    def office(self, job):
        location = point(job.get("location"))
        if location:
            return location
        key = self.office_key(job)
        return self.offices.get(key)

    @staticmethod
    def office_key(job):
        address = job.get("address")
        return (job.get("region", ""), address) if isinstance(address, str) and address else None

    @staticmethod
    def route_key(home, office, mode):
        if not home or not office:
            return None
        return (home["lat"], home["lon"], office["lat"], office["lon"], mode)

    @staticmethod
    def remember(cache, key, value):
        cache[key] = value
        cache.move_to_end(key)
        while len(cache) > 150:
            cache.popitem(last=False)

    def commute(self, job, preferences, office):
        home, mode = preferences["home"], preferences["travel_mode"]
        remote = work_formats(job.get("format", "")) == {"remote"}
        cached = self.routes.get(self.route_key(home, office, mode))
        if not remote and cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        return commute_estimate(home, office, mode, remote=remote)

    def enriched(self, job, opts, resume, analysis=None):
        location = self.office(job)
        commute = self.commute(job, opts, location)
        return {
            "format": job.get("format", ""),
            "published": job.get("published", ""),
            "salary_max": job.get("salary_max"),
            "salary_min": job.get("salary_min"),
            "currency": job.get("currency"),
            "salary_sort": (job.get("salary_max") or job.get("salary_min"))
            if job.get("currency") == opts.get("currency")
            else None,
            "location": location,
            "address": job.get("address") or (location or {}).get("address", ""),
            "commute": commute,
            "rank": rank_job(
                job, opts, resume, analysis, commute, today=datetime.now(self.config.zone).date()
            ),
        }

    async def refine(self, job, opts, resume, analysis=None):
        home, mode = opts["home"], opts["travel_mode"]
        if work_formats(job.get("format", "")) == {"remote"}:
            return self.enriched(job, opts, resume, analysis)
        if self.provider_lock.locked():
            raise ServiceError("Запрос к картам уже выполняется.")
        async with self.provider_lock:
            office = self.office(job)
            office_key = self.office_key(job)
            if not office and office_key and self.provider.available:
                region, address = office_key
                search_address = (
                    f"{region}, {address}"
                    if region and region.casefold() not in address.casefold()
                    else address
                )
                office = await self.provider.locate(search_address, precise=True)
                if office:
                    self.remember(self.offices, office_key, office)
            if not office:
                raise ServiceError("Нет точной точки офиса для расчёта.")
            key = self.route_key(home, office, mode)
            cached = self.routes.get(key)
            if (
                home
                and self.provider.available
                and mode != "transit"
                and (not cached or time.monotonic() - cached[0] >= 3600)
            ):
                route = await self.provider.route(home, office, mode)
                if route:
                    self.remember(self.routes, key, (time.monotonic(), route))
        return self.enriched(job, opts, resume, analysis)
