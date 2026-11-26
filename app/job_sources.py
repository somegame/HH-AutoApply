import asyncio
import re
from urllib.parse import urlencode, urlsplit
from uuid import UUID

import httpx

from app import hh_browser as hh
from app.services import ServiceError, clean_text
from app.vacancies import URL_FIELDS, filter_reason, search_links

SOURCES = {
    "hh": ("HeadHunter", "https://hh.ru/search/vacancy"),
    "trudvsem": ("Работа России", "https://trudvsem.ru/vacancy/search"),
    "superjob": ("SuperJob", "https://www.superjob.ru/vakansii/"),
    "habr": ("Хабр Карьера", "https://career.habr.com/vacancies"),
    "avito": ("Авито Работа", "https://www.avito.ru/rossiya/vakansii"),
    "rabota": ("Работа.ру", "https://www.rabota.ru/vacancy/"),
}
SEARCH_SOURCES = ("hh", "trudvsem", "superjob")


def source_url(source, query, settings):
    if source == "hh":
        return next(
            search_links(
                settings | {"query": query, "use_related": False},
                order="relevance",
                title_only=settings.get("hh_title_only", True),
            )
        )[1]
    parameter = {"superjob": "keywords", "habr": "q", "avito": "q", "rabota": "query"}.get(source)
    base = SOURCES[source][1]
    return f"{base}?{urlencode({parameter: query})}" if parameter and query else base


def text(value, size=180):
    return clean_text(value).strip()[:size] if isinstance(value, str) else ""


def price(value):
    if type(value) is int and 0 < value < 10**9:
        return value
    if isinstance(value, str) and re.fullmatch(r"[0-9]{1,9}", value):
        return int(value)
    return 0


def job_url(value, domain):
    try:
        url = urlsplit(value)
        if (
            url.scheme == "https"
            and (url.hostname or "").removeprefix("www.") == domain
            and (not url.username and not url.password and url.port in {None, 443})
        ):
            return value
    except (ValueError, TypeError, AttributeError):
        pass
    return ""


def source_vacancy_id(source, path):
    if source == "trudvsem" and path.startswith("/vacancy/card/"):
        try:
            return f"trudvsem:{UUID(path.rstrip('/').rsplit('/', 1)[-1])}"
        except ValueError:
            return ""
    if source == "superjob" and path.startswith("/vakansii/"):
        match = re.search(r"-([0-9]{1,18})\.html/?$", path)
        if match:
            return f"superjob:{int(match[1])}"
    return ""


def details(
    source, external_id, *, url, title, company, region, low, high, currency="RUB", **fields
):
    data = dict.fromkeys(URL_FIELDS, "")
    data.update(fields)
    data.update(
        url=url,
        title=text(title),
        company=text(company) or "Работодатель не указан",
        region=text(region),
        source=source,
    )
    low, high = price(low), price(high)
    if low or high:
        value_low, value_high = sorted((low or high, high or low))
        data.update(
            salary=f"{value_low:,}–{value_high:,} {currency}".replace(",", " "),
            salary_min=value_low,
            salary_max=value_high,
            currency=currency,
        )
    if data["title"] and url:
        return f"{source}:{external_id}", data
    return None


def trudvsem_row(item):
    if not isinstance(item, dict) or not isinstance(item.get("vacancy"), dict):
        return None
    row = item["vacancy"]
    try:
        identifier = str(UUID(row["id"]))
    except (ValueError, KeyError, AttributeError, TypeError):
        return None
    region = row.get("region")
    company = row.get("company")
    requirements = row.get("requirement")
    education = text(requirements.get("education")) if isinstance(requirements, dict) else ""
    description = "\n".join(
        filter(
            None,
            (
                text(row.get("duty"), 1600),
                text(row.get("requirements"), 1000),
                f"Образование — {education}" if education else "",
            ),
        )
    )
    return details(
        "trudvsem",
        identifier,
        url=job_url(row.get("vac_url"), "trudvsem.ru"),
        title=row.get("job-name"),
        company=company.get("name") if isinstance(company, dict) else "",
        region=region.get("name") if isinstance(region, dict) else "",
        low=row.get("salary_min"),
        high=row.get("salary_max"),
        schedule=text(row.get("schedule")),
        description=description,
    )


def superjob_row(row):
    if not isinstance(row, dict) or type(row.get("id")) is not int or row["id"] <= 0:
        return None
    town = row.get("town")
    currency = {"rub": "RUB", "rur": "RUB", "usd": "USD", "eur": "EUR"}.get(
        str(row.get("currency")).lower(), "OTHER"
    )
    return details(
        "superjob",
        row["id"],
        url=job_url(row.get("link"), "superjob.ru"),
        title=row.get("profession"),
        company=row.get("firm_name"),
        region=town.get("title") if isinstance(town, dict) else "",
        low=row.get("payment_from"),
        high=row.get("payment_to"),
        currency=currency,
        description=text(row.get("candidat"), 2400),
    )


class JobSources:
    def __init__(self, config, browser, client):
        self.config = config
        self.browser = browser
        self.client = client

    async def search(self, settings, source):
        if source == "hh":
            return await self.search_hh(settings)
        try:
            if source == "trudvsem":
                scope = "/region/77" if settings["region"].casefold() == "москва" else ""
                response = await self.client.get(
                    "https://opendata.trudvsem.ru/api/v1/vacancies" + scope,
                    params={"text": settings["query"], "limit": 30},
                    timeout=9,
                )
                payload = response.json() if response.is_success else {}
                data = (
                    payload.get("results", {}).get("vacancies")
                    if isinstance(payload, dict)
                    else None
                )
                parse = trudvsem_row
            elif source == "superjob":
                if not self.config.superjob_key:
                    raise ServiceError("Нужен ключ приложения SuperJob. Поиск на сайте доступен.")
                response = await self.client.get(
                    "https://api.superjob.ru/2.0/vacancies/",
                    headers={"X-Api-App-Id": self.config.superjob_key},
                    params={
                        "keyword": settings["query"],
                        "count": 25,
                        **({"town": 4} if settings["region"].casefold() == "москва" else {}),
                    },
                    timeout=9,
                )
                payload = response.json() if response.is_success else {}
                data = payload.get("objects") if isinstance(payload, dict) else None
                parse = superjob_row
            else:
                raise ServiceError("Поиск доступен на сайте источника.")
            if not isinstance(data, list):
                raise ServiceError(f"Источник не вернул вакансии (HTTP {response.status_code}).")
        except ServiceError:
            raise
        except (httpx.HTTPError, OSError, ValueError, AttributeError, asyncio.TimeoutError) as exc:
            raise ServiceError("Источник временно недоступен.") from exc
        return [
            item
            for raw in data[:30]
            if (item := parse(raw)) and not filter_reason(item[1], settings)
        ][:15]

    async def search_hh(self, settings):
        self.browser.require_browser()
        if self.browser.lock.locked():
            raise ServiceError("Браузер HH занят. Можно открыть поиск на сайте.")
        url = source_url("hh", settings["query"], settings)
        async with self.browser.lock:
            async with self.browser.provider.session(35, guest=True) as (_, page):
                ids = await hh.search_ids(page, url)
                results = []
                for identifier in ids[:4]:
                    vacancy = await hh.read_vacancy(page, identifier)
                    if vacancy and not filter_reason(vacancy, settings):
                        results.append((identifier, vacancy))
                return results
