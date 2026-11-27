import asyncio
import hashlib
import hmac
import json
import logging
import sqlite3
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit
from uuid import uuid4

from aiohttp import web

from app import hh_resume_editor
from app.config import VERSION, setting
from app.database import LABELS
from app.discovery import VacancyDiscovery, commute_limit, preferred_format
from app.habr import HabrAccount
from app.hh_browser import HHAccessBarrier, HHLoginRequired
from app.job_sources import (
    ACCOUNT_URLS,
    SEARCH_SOURCES,
    SOURCES,
    source_url,
    source_vacancy_id,
    text,
)
from app.ranking import ORDERS, duplicate_key, mark_top, normalized, sort_ranked
from app.resume import checked
from app.resume_studio import build_resume, has_resume_evidence
from app.scheduler import local_day
from app.services import ServiceError, ai_allowed, clean_text
from app.vacancies import HH, URL_FIELDS, terms, vacancy_id

STATIC = Path(__file__).with_name("mini_web")
MAX_AGE = 3600
SEARCH_TTL = 1800
EDITABLE = ("draft", "approved", "deferred", "questions")
log = logging.getLogger(__name__)
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' https://telegram.org; "
        "style-src 'self'; img-src 'self' data: https://tile.openstreetmap.org; "
        "connect-src 'self'; frame-src 'self'; "
        "frame-ancestors 'self' https://web.telegram.org; base-uri 'none'; form-action 'none'"
    ),
}


def verify_init_data(data, token, owner, *, now=None):
    if not isinstance(data, str) or not 0 < len(data) <= 8192:
        return False
    now = int(time.time()) if now is None else now
    try:
        pairs = parse_qsl(data, keep_blank_values=True, strict_parsing=True, max_num_fields=16)
        values = dict(pairs)
        if len(pairs) != len(values):
            return False
        digest = values.pop("hash")
        if len(digest) != 64 or not digest.isascii():
            return False
        age = now - int(values["auth_date"])
        if not -30 <= age <= MAX_AGE:
            return False
        check = "\n".join(f"{k}={v}" for k, v in sorted(values.items()))
        key = hmac.digest(b"WebAppData", token.encode(), "sha256")
        expected = hmac.new(key, check.encode(), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(digest, expected):
            return False
        user = json.loads(values["user"])
        return isinstance(user, dict) and type(user.get("id")) is int and user["id"] == owner
    except (KeyError, ValueError, TypeError, OverflowError):
        return False


@dataclass
class Search:
    prefs: dict
    criteria: dict
    overrides: dict
    order: str
    include_unknown: bool
    sources: tuple
    created: float = field(default_factory=time.monotonic, compare=False)
    results: dict = field(default_factory=dict, compare=False)
    task: asyncio.Task | None = field(default=None, compare=False)


class MiniApp:
    def __init__(self, config, db, scheduler, client, bot):
        self.config = config
        self.db = db
        self.client = client
        self.scheduler = scheduler
        self.browser = scheduler.browser
        self.hub = scheduler.hub
        self.sources = self.hub.sources
        self.superjob = scheduler.superjob
        self.habr = HabrAccount(config, db, client)
        self.discovery = VacancyDiscovery(config, db, client)
        self.bot = bot
        self.search_lock = asyncio.Lock()
        self.searches = OrderedDict()
        self.catalog = OrderedDict()
        self.resume_task = None
        self.resume_error = ""
        self.hh_editor_task = None
        self.hh_editor_error = ""

    def add_routes(self, app):
        app.on_shutdown.append(self.shutdown)
        app.add_routes(
            [
                web.get("/mini", self.asset),
                web.get("/mini/{asset:(?:app|discovery|leaflet)\\.(?:js|css)}", self.asset),
                web.get(
                    "/mini/api/{command:state|search|search-status|vacancy|resume-studio|hh-editor}",
                    self.api,
                ),
                web.get("/mini/oauth/superjob", self.superjob_callback),
                web.get("/mini/oauth/habr", self.habr_callback),
                web.get("/mini/oauth/finished", self.oauth_finished),
                web.post(
                    "/mini/api/{command:job|profile|connect|resume|cancel|disconnect|import|"
                    "hh-mode|hh-resumes|hh-browser|superjob|preferences|locate|route|"
                    "search-start|auto-settings|resume-studio|ai-consent|hh-editor|hh-check|habr|hub}",
                    self.api,
                ),
            ]
        )

    async def shutdown(self, _app):
        tasks = [run.task for run in self.searches.values() if run.task and not run.task.done()]
        if self.resume_task and not self.resume_task.done():
            tasks.append(self.resume_task)
        if self.hh_editor_task and not self.hh_editor_task.done():
            tasks.append(self.hh_editor_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    async def asset(self, request):
        file = request.match_info.get("asset", "index.html")
        mime = {".html": "text/html", ".js": "application/javascript", ".css": "text/css"}
        path = STATIC / file
        if not path.is_file():
            raise web.HTTPNotFound()
        headers = HEADERS
        if file == "index.html":
            headers = HEADERS | {"Referrer-Policy": "strict-origin-when-cross-origin"}
        return web.Response(
            text=path.read_text(encoding="utf-8"),
            content_type=mime[path.suffix],
            headers=headers,
        )

    def json(self, payload, status=200):
        return web.json_response(payload, status=status, headers=HEADERS)

    async def superjob_callback(self, request):
        success = await self.superjob.connect(request.query)
        location = "/mini/oauth/finished" + ("?connected=1" if success else "")
        raise web.HTTPSeeOther(location=location, headers=HEADERS)

    async def habr_callback(self, request):
        success = await self.habr.connect(request.query)
        location = "/mini/oauth/finished?provider=habr"
        if success:
            location += "&connected=1"
        raise web.HTTPSeeOther(location=location, headers=HEADERS)

    async def oauth_finished(self, request):
        name = "Хабр Карьера" if request.query.get("provider") == "habr" else "SuperJob"
        status = (
            "Профиль подтверждён."
            if request.query.get("connected") == "1"
            else "Вход не подтверждён."
        )
        return web.Response(
            text=f'<!doctype html><html lang="ru"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>{name}</title><link rel="stylesheet" href="/mini/app.css">'
            f'<main class="oauth-result"><h1>{name}</h1><p>{status}</p>'
            f"<p>Вернитесь в Telegram и откройте раздел «Аккаунты».</p></main></html>",
            content_type="text/html",
            headers=HEADERS,
        )

    async def api(self, request):
        origin = request.headers.get("Origin")
        if (origin and origin != self.config.login_public_url) or (
            request.method == "POST" and origin != self.config.login_public_url
        ):
            return self.json({"error": "Запрос отклонён."}, 403)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("tma ") or not verify_init_data(
            auth[4:], self.config.token, self.config.owner
        ):
            return self.json({"error": "Откройте приложение заново из личного чата с ботом."}, 401)
        command = request.match_info["command"]
        try:
            if command == "state":
                return self.json(self.state())
            if command == "search":
                return self.json(await self.search(request.query))
            if command == "search-status":
                identifier = request.query.get("search_id", "")
                run = self.searches.get(identifier)
                if not run or time.monotonic() - run.created > SEARCH_TTL:
                    return self.json({"error": "Этот поиск устарел. Нажмите «Найти» ещё раз."}, 410)
                return self.json(self.search_payload(run))
            if command == "vacancy":
                return self.json(self.vacancy_details(request.query.get("id", "")))
            if command == "resume-studio" and request.method == "GET":
                return self.json(self.resume_studio_state())
            if command == "hh-editor" and request.method == "GET":
                return self.json(self.hh_editor_state())
            data = await request.clone(client_max_size=100_000).json()
            if not isinstance(data, dict):
                raise ValueError("Ожидались данные в виде объекта.")
            if command == "search-start":
                return self.start_search(data)
            if command == "auto-settings":
                if type(data.get("skills_score")) is not int:
                    raise ValueError("Введите целый процент совпадения по навыкам.")
                score = setting("skills_score", str(data["skills_score"]))
                self.db.set("settings", self.db.get("settings", {}) | {"skills_score": score})
                return self.json(self.state())
            if command == "job":
                return self.json(await self.job_action(data))
            if command == "profile":
                return self.json(self.save_profile(data))
            if command == "resume-studio":
                return self.resume_studio_action(data)
            if command == "ai-consent":
                consent = data.get("gemini")
                if type(consent) is not bool:
                    raise ValueError("Укажите согласие на Gemini.")
                if consent and not self.config.gemini_key:
                    raise ValueError("Добавьте GEMINI_API_KEY в Bothost.")
                self.db.set("gemini_consent", consent)
                if not ai_allowed(self.config, self.db):
                    self.browser.pause("Разрешение на анализ отключено.", keep_search=True)
                return self.json(self.state())
            if command == "preferences":
                return self.json({"preferences": self.discovery.configure(data)})
            if command == "locate":
                point = await self.discovery.locate(data.get("address"))
                if not point:
                    raise ServiceError(
                        "Карты не нашли адрес. Проверьте запрос или введите координаты."
                    )
                return self.json({"point": point})
            if command == "route":
                identifier = data.get("id")
                job, analysis = self.vacancy_record(identifier)
                opts = self.comparison_options(identifier)
                return self.json(
                    await self.discovery.refine(job, opts, self.db.get("resume", ""), analysis)
                )
            if command == "import":
                return self.json(await self.import_job(data))
            if command == "hh-mode":
                return self.json(await self.change_hh_mode(data))
            if command == "hh-check":
                checked = (
                    await self.browser.check_resume_access()
                    if data.get("scope") == "resume"
                    else await self.browser.check_access()
                )
                return self.json(self.state() | {"hh_access_restored": checked})
            if command == "hh-editor":
                return await self.hh_editor_action(data)
            if command == "hub":
                self.hub.configure(data.get("source"), data.get("enabled"))
                if data["enabled"]:
                    await self.scheduler.start(self.bot)
                return self.json(self.state())
            if command == "habr":
                if data.get("action") == "connect":
                    return self.json({"url": self.habr.start()})
                if data.get("action") == "disconnect" and data.get("confirm") is True:
                    self.habr.disconnect()
                    return self.json(self.state())
                raise ValueError("Выберите действие для Хабр Карьеры.")
            if command == "hh-resumes":
                if "url" in data:
                    await self.browser.refresh_resumes(data["url"])
                else:
                    await self.browser.refresh_resumes()
                return self.json(self.state())
            if command == "hh-browser":
                return self.json({"url": await self.connect_hh(resume_mode=True)})
            if command == "superjob":
                action = data.get("action")
                if action == "connect":
                    return self.json({"url": self.superjob.start()})
                if action == "disconnect" and data.get("confirm") is True:
                    self.superjob.disconnect()
                elif action == "resume":
                    self.superjob.select(data.get("id"))
                elif action == "auto" and data.get("enabled") is False:
                    self.superjob.pause_auto("Автоотклики на SuperJob остановлены.")
                elif (
                    action == "auto" and data.get("enabled") is True and data.get("confirm") is True
                ):
                    await self.superjob.enable_auto()
                    self.hub.configure("superjob", True)
                    self.db.set("processing", True)
                    await self.scheduler.start(self.bot)
                else:
                    raise ValueError("Выберите действие для SuperJob.")
                return self.json(self.state())
            if command == "connect":
                return self.json({"url": await self.connect_hh(fresh=data.get("fresh") is True)})
            if command == "cancel":
                await self.browser.cancel_login()
                self.db.set("mini_hh_progress", "")
                return self.json(self.state())
            if command == "disconnect":
                if data.get("confirm") is not True:
                    raise ValueError("Подтвердите отключение HH.")
                await self.browser.cancel_login()
                if self.browser.lock.locked():
                    raise ServiceError("Браузер занят другой операцией.")
                async with self.browser.lock:
                    self.browser.pause("HH отключён владельцем.")
                    self.browser.provider.state.unlink(missing_ok=True)
                    self.db.set_many(
                        {
                            "mini_hh_progress": "",
                            "hh_resume_choices": {},
                            "hh_selected_resume": {},
                            "hh_session_verified": False,
                            "hh_resumes_status": "",
                        }
                    )
                return self.json(self.state())
            identifier = data.get("id")
            if not isinstance(identifier, str):
                raise ValueError("Выберите резюме HH.")
            await self.browser.import_resume(identifier)
            return self.json(self.state())
        except json.JSONDecodeError:
            return self.json({"error": "Некорректные данные."}, 400)
        except HHAccessBarrier as exc:
            self.browser.record_access_barrier(exc)
            return self.json({"error": str(exc)}, 403)
        except HHLoginRequired as exc:
            self.db.set("hh_needs_login", True)
            self.browser.pause("Нужно снова подключить HH.")
            return self.json({"error": str(exc)}, 400)
        except (ValueError, ServiceError) as exc:
            return self.json({"error": str(exc)}, 400)
        except (OSError, sqlite3.Error):
            log.warning("Не удалось прочитать или сохранить данные Mini App")
            return self.json({"error": "Бот не смог открыть хранилище на Bothost."}, 503)
        except Exception as exc:
            log.warning("Ошибка Mini App в %s (%s)", command, type(exc).__name__)
            return self.json({"error": "Ошибка приложения. Повторите действие."}, 500)

    def state(self):
        prefs = self.db.settings(self.config)
        counts, today = self.db.counts(local_day(self.config))
        resumes = self.db.get("superjob_resumes", {})
        hh_resume = self.db.get("hh_selected_resume", {})
        hh_needs_login = self.db.get("hh_needs_login", False)
        hh_barrier = self.browser.access_barrier()
        resume_barrier = self.browser.resume_access_barrier()
        hh_login_open = bool(self.browser.login_task and not self.browser.login_task.done())
        options = self.discovery.preferences()
        resume = self.db.get("resume", "")
        jobs = [
            self.saved(row, prefs | options, resume) for row in self.db.page((), 0, size=60)[:60]
        ]
        mark_top(jobs)
        return {
            "version": VERSION,
            "preferences": options,
            "defaults": {key: prefs[key] for key in ("query", "region", "salary", "use_related")},
            "sources": self.directory(prefs),
            "jobs": jobs,
            "resume": resume,
            "consent": self.db.get("groq_consent", False),
            **self.resume_studio_state(),
            **self.hh_editor_state(),
            "groq": bool(self.config.groq_key),
            "gemini": bool(self.config.gemini_key),
            "gemini_consent": self.db.get("gemini_consent", False),
            "ai_available": ai_allowed(self.config, self.db),
            "resumes": self.db.get("hh_resume_choices", {}),
            "hh_resume": hh_resume.get("name", ""),
            "hh_resume_id": hh_resume.get("id", ""),
            "hh_resume_url": f"{HH}/resume/{hh_resume['id']}" if hh_resume.get("id") else "",
            "hh_has_session": self.browser.provider.state.is_file() and not hh_needs_login,
            "hh_connected": self.browser.account_connected,
            "hh_resumes_status": self.db.get("hh_resumes_status", ""),
            "hh_login_open": hh_login_open,
            "hh_progress": "Срок входа истёк. Подключите HH заново."
            if hh_needs_login and not hh_login_open
            else self.db.get("mini_hh_progress", ""),
            "persistent": self.config.persistent,
            "storage_persistent": self.config.persistent,
            "auto_settings": {
                "skills_score": prefs["skills_score"],
                "score": prefs["score"],
            },
            "analysis_queue": {"pending": counts.get("queued", 0)}
            | self.scheduler.analysis_status(),
            "hh_send": self.browser.available
            and self.config.persistent
            and not (hh_barrier or resume_barrier),
            "hh_auto_allowed": self.browser.available
            and self.config.auto_send
            and self.config.persistent,
            "hh_running": bool(self.db.get("browser_search")),
            "hh_auto": bool(self.db.get("browser_auto_enabled")),
            "hh_notice": self.db.get("browser_notice", ""),
            "hh_scan_pending": len(self.db.get("browser_scan", {}).get("pending", [])),
            "hh_application": self.browser.application_status(),
            "hh_access_restricted": bool(hh_barrier),
            "hh_resume_access_restricted": bool(resume_barrier),
            "hh_can_check_resume": resume_barrier.get("status") == 403
            and resume_barrier.get("step") == "resume",
            "hub": self.hub.state(),
            "habr": self.habr.state(),
            "daily_limit": prefs["limit"],
            "summary": {
                "total": sum(counts.values()),
                "sent": counts.get("sent", 0),
                "today": today,
            },
            "superjob": {
                "enabled": self.superjob.available,
                "connected": bool(self.db.get("superjob_tokens"))
                and not self.db.get("superjob_needs_login"),
                "resumes": resumes,
                "selected": self.db.get("superjob_resume_id", ""),
                "status": self.db.get("superjob_status", ""),
                "send": self.superjob.available and self.config.persistent,
                "auto_allowed": self.superjob.auto_allowed,
                "auto_enabled": self.db.get("superjob_auto_enabled", False),
                "auto_resume": bool(self.superjob.snapshot()),
                "auto_notice": self.db.get("superjob_auto_notice", ""),
            },
        }

    def directory(self, prefs):
        modes = {
            "hh": "browser" if self.browser.available else "link",
            "trudvsem": "public",
            "habr": "habr",
            "superjob": (
                "account"
                if self.superjob.available
                else "key"
                if self.config.superjob_key
                else "needs_key"
            ),
        }
        return [
            {
                "id": key,
                "name": info[0],
                "url": source_url(key, prefs["query"], prefs),
                "account_url": ACCOUNT_URLS[key],
                "mode": modes.get(key, "link"),
                "in_app_search": (key == "hh" and self.browser.available)
                or self.hub.available(key),
                "in_app_apply": self.config.persistent
                and (
                    (key == "hh" and self.browser.available and not self.browser.access_barrier())
                    or (key == "superjob" and self.superjob.available)
                ),
            }
            for key, info in SOURCES.items()
        ]

    def saved(self, row, opts=None, resume=None):
        job = json.loads(row["details"])
        result = self.current_analysis(row)
        site = job.get("source")
        if opts is None:
            opts = self.db.settings(self.config) | self.discovery.preferences()
        if resume is None:
            resume = self.db.get("resume", "")
        resume = self.scheduler.resume_for(job, resume)
        return {
            "id": row["id"],
            "title": job["title"],
            "company": job["company"],
            "region": job["region"],
            "salary": job["salary"],
            "url": job["url"],
            "source": site if site in (*SOURCES, "external") else "hh",
            "status": row["status"],
            "label": LABELS[row["status"]],
            "letter": row["letter"] or "",
            **self.analysis_fields(row, result, resume),
            "can_edit": row["status"] in EDITABLE and bool(row["letter"]),
            "can_send": site in {"browser", "mini_hh", "superjob"}
            and row["status"] in {"draft", "approved"}
            and bool(row["letter"]),
            **self.discovery.enriched(job, opts, resume, result),
        }

    def vacancy_record(self, identifier):
        if not isinstance(identifier, str) or not 1 <= len(identifier) <= 55:
            raise ValueError("Выберите вакансию.")
        row = self.db.vacancy(identifier)
        entry = self.catalog.get(identifier)
        live = entry[1] if entry and time.monotonic() - entry[0] <= 1800 else None
        if row:
            detail = json.loads(row["details"])
            if live:
                detail |= {
                    key: live[key]
                    for key in ("location", "address", "format", "published")
                    if live.get(key)
                }
            return detail, self.current_analysis(row)
        if live:
            return live, None
        raise ServiceError("Вакансия устарела. Обновите поиск.")

    def comparison_options(self, identifier):
        prefs = self.db.settings(self.config) | self.discovery.preferences()
        entry = self.catalog.get(identifier)
        if entry and time.monotonic() - entry[0] <= 1800:
            prefs |= entry[3]
        return prefs

    def vacancy_details(self, identifier):
        detail, analysis = self.vacancy_record(identifier)
        opts = self.comparison_options(identifier)
        fields = {
            key: detail.get(key, "")
            for key in (
                "title",
                "company",
                "region",
                "salary",
                "description",
                "experience",
                "format",
            )
        }
        resume = self.scheduler.resume_for(detail)
        return (
            fields
            | self.discovery.enriched(detail, opts, resume, analysis)
            | self.analysis_fields(self.db.vacancy(identifier), analysis, resume)
        )

    def search_options(self, query):
        prefs = self.db.settings(self.config)
        criteria = {}
        for key in ("query", "region", "salary"):
            value = query.get(key, prefs[key])
            if not isinstance(value, str) and not (key == "salary" and type(value) is int):
                raise ValueError("Проверьте должность, регион и минимальную зарплату.")
            criteria[key] = setting(key, str(value))

        def enabled(key, default):
            return query.get(key, default) in (True, "true")

        criteria["unknown_salary"] = criteria["salary"] == 0 or prefs["unknown_salary"]
        prefs |= criteria
        prefs["hh_title_only"] = enabled("hh_title_only", True)
        variants = [prefs["query"]]
        seen_queries = {prefs["query"].casefold()}
        if enabled("include_related", prefs["use_related"]):
            for candidate in terms(prefs["related"]):
                if candidate.casefold() not in seen_queries:
                    variants.append(candidate)
                    seen_queries.add(candidate.casefold())
                if len(variants) == 3:
                    break
        prefs["search_queries"] = variants
        overrides = {"salary": criteria["salary"]}
        if "preferred_format" in query:
            overrides["preferred_format"] = preferred_format(query["preferred_format"])
        if "max_commute" in query:
            overrides["max_commute"] = commute_limit(query["max_commute"])
        order = query.get("sort", "best")
        if not isinstance(order, str) or order not in ORDERS:
            raise ValueError("Выберите сортировку.")
        include_unknown = enabled("include_unknown_commute", True)
        source = query.get("source", "all")
        if not isinstance(source, str) or (source != "all" and source not in SOURCES):
            raise ValueError("Неизвестный источник.")
        keys = SEARCH_SOURCES if source == "all" else (source,)
        if enabled("apply_only", False):
            applicable = {item["id"] for item in self.directory(prefs) if item["in_app_apply"]}
            keys = tuple(key for key in keys if key in applicable)
        return Search(prefs, criteria, overrides, order, include_unknown, keys)

    def start_search(self, query):
        run = self.search_options(query)
        for identifier, existing in reversed(self.searches.items()):
            if existing.task and not existing.task.done():
                if run == existing:
                    return self.json({"search_id": identifier}, 202)
                return self.json(
                    {"error": "Предыдущий поиск ещё идёт. Повторите через несколько секунд."}, 409
                )
        if self.search_lock.locked():
            return self.json({"error": "Поиск уже выполняется."}, 409)
        identifier = uuid4().hex
        self.searches[identifier] = run
        while len(self.searches) > 4:
            self.searches.popitem(last=False)
        run.task = asyncio.create_task(self.collect_search(run, enqueue=True))
        return self.json({"search_id": identifier}, 202)

    async def search(self, query):
        run = self.search_options(query)
        if self.search_lock.locked():
            raise ServiceError("Поиск уже выполняется.")
        await self.collect_search(run, enqueue=False)
        return self.search_payload(run)

    async def collect_search(self, run, *, enqueue):
        async def fetch(key):
            try:
                async with asyncio.timeout(70 if key == "hh" else 20):
                    results = await self.search_source(run.prefs, key)
                for identifier, data in results:
                    self.catalog[identifier] = (time.monotonic(), data, run.criteria, run.overrides)
                    self.catalog.move_to_end(identifier)
                    if enqueue and data.get("description", "").strip():
                        source = "mini_hh" if key == "hh" else key
                        self.db.add_discovered(
                            identifier, data | {"source": source, "mini_filters": run.criteria}
                        )
                while len(self.catalog) > 150:
                    self.catalog.popitem(last=False)
                if enqueue and results:
                    self.db.set("processing", True)
                    await self.scheduler.start(self.bot)
                run.results[key] = results
            except TimeoutError:
                run.results[key] = "Площадка долго отвечает. Её можно проверить ещё раз."
            except ServiceError as exc:
                run.results[key] = str(exc)
            except (OSError, sqlite3.Error):
                log.warning("Не удалось сохранить результаты поиска для %s", key)
                run.results[key] = "Бот не смог сохранить результат на Bothost."
            except Exception as exc:
                log.warning("Ошибка поиска %s (%s)", key, type(exc).__name__)
                run.results[key] = "Площадка временно недоступна."

        async with self.search_lock:
            await asyncio.gather(*(fetch(key) for key in run.sources))

    @staticmethod
    def current_analysis(row):
        if row is None or row["status"] == "queued":
            return None
        return json.loads(row["result"] or "{}")

    @staticmethod
    def analysis_fields(row, result, resume):
        result = result or {}
        # Scores must correspond to the resume currently used by the owner.
        verified = (
            bool(resume)
            and result.get("resume_hash") == hashlib.sha256(resume.encode()).hexdigest()
            and type(result.get("score")) is int
            and row is not None
            and row["status"] != "queued"
        )
        status = row["status"] if row else None
        if verified:
            state = "complete"
        elif status == "queued":
            state = "retry" if row["error"] else "queued"
        else:
            state = status if status in {"filtered", "error"} else "unavailable"
        return {
            "analysis_status": state,
            "score": result["score"] if verified else None,
            "analysis_provider": result.get("analysis_provider", "groq") if verified else None,
            "skills_score": result.get("skills_score") if verified else None,
            "duties": result.get("duties", []) if verified else [],
            "reason": (row["error"] if row else None)
            or result.get("filter_reason")
            or (result.get("reason") if verified else None)
            or "",
        }

    def search_payload(self, run):
        options = run.prefs | self.discovery.preferences() | run.overrides
        jobs = []
        notices = []
        seen = set()
        resume = self.db.get("resume", "")
        rows = self.db.vacancies(
            {
                identifier
                for results in run.results.values()
                if isinstance(results, list)
                for identifier, _ in results
            }
        )
        for key in run.sources:
            result = run.results.get(key)
            if result is None:
                continue
            if isinstance(result, str):
                notices.append({"source": key, "message": result})
                continue
            for identifier, data in result:
                if identifier in seen:
                    continue
                seen.add(identifier)
                row = rows.get(identifier)
                analysis = self.current_analysis(row)
                matching_resume = self.scheduler.resume_for(data, resume)
                meta = self.discovery.enriched(data, options, matching_resume, analysis)
                travel = meta["commute"]
                limit = options["max_commute"]
                if limit and (
                    (not travel and not run.include_unknown)
                    or (travel and travel["max_minutes"] > limit)
                ):
                    continue
                job = (
                    data
                    | meta
                    | self.analysis_fields(row, analysis, matching_resume)
                    | {
                        "id": identifier,
                        "source": key,
                        "saved": row is not None,
                        "status": row["status"] if row else None,
                    }
                )
                jobs.append(job)
        unique = []
        duplicate_keys = set()
        for job in sort_ranked(jobs, "best"):
            duplicate = duplicate_key(job)
            if duplicate and duplicate in duplicate_keys:
                continue
            if duplicate:
                duplicate_keys.add(duplicate)
            unique.append(job)
        top = mark_top(unique)
        for job in unique:
            job.pop("description", None)
        pending = sum(job["status"] == "queued" for job in unique)
        return {
            "jobs": sort_ranked(unique, run.order),
            "notices": notices,
            "sources": self.directory(run.prefs),
            "done": len(run.results) == len(run.sources),
            "pending_sources": [key for key in run.sources if key not in run.results],
            "analysis_pending": pending,
            "analysis_queue": {"pending": pending} | self.scheduler.analysis_status(),
            "analysis_running": bool(
                (resume or ("superjob" in run.sources and self.superjob.snapshot()))
                and ai_allowed(self.config, self.db)
                and self.db.get("processing", True)
            ),
            "stats": {
                "salary_known": sum(bool(job["salary_max"]) for job in unique),
                "commute_known": sum(job["commute"] is not None for job in unique),
                "mapped": sum(bool(job["location"]) for job in unique),
                "duplicates": len(jobs) - len(unique),
                "top": top,
            },
        }

    async def search_source(self, prefs, source):
        variants = prefs["search_queries"]
        if source == "hh" or len(variants) == 1:
            return await self.sources.search(prefs, source)
        batches = await asyncio.gather(
            *(self.sources.search(prefs | {"query": value}, source) for value in variants),
            return_exceptions=True,
        )
        healthy = [result for result in batches if not isinstance(result, BaseException)]
        if not healthy:
            raise batches[0]
        return [job for batch in healthy for job in batch]

    def save_profile(self, data):
        resume, consent = data.get("resume"), data.get("consent")
        if not isinstance(resume, str) or type(consent) is not bool:
            raise ValueError("Проверьте текст резюме и согласие.")
        resume = checked(resume.strip())
        values = {"resume": resume, "groq_consent": consent}
        if resume != self.db.get("resume", ""):
            self.browser.pause("Данные резюме изменены.", keep_search=True)
            values["hh_selected_resume"] = {}
        if not consent and not (self.config.gemini_key and self.db.get("gemini_consent")):
            self.browser.pause("Разрешение на анализ отключено.", keep_search=True)
        self.db.set_many(values)
        return self.state()

    def resume_studio_state(self):
        stored = self.db.get("resume_studio")
        draft = (
            {key: value for key, value in stored.items() if key != "profile_hash"}
            if stored
            else None
        )
        status = "ready" if draft else "idle"
        if self.resume_task and not self.resume_task.done():
            status = "processing"
        elif self.resume_error:
            status = "error"
        return {
            "resume_studio": draft,
            "resume_studio_status": {"status": status, "message": self.resume_error},
        }

    def hh_editor_state(self):
        selected = self.db.get("hh_selected_resume", {}).get("id")
        plan = self.db.get("hh_editor", {})
        if not selected or plan.get("resume_id") != selected:
            plan = {}
        return {
            "hh_editor": plan or None,
            "hh_editor_pending": bool(self.hh_editor_task and not self.hh_editor_task.done()),
            "hh_editor_error": self.hh_editor_error,
        }

    async def hh_editor_action(self, data):
        command = data.get("action")
        identifier = self.browser.editor_resume()
        if command == "prepare":
            if not ai_allowed(self.config, self.db):
                raise ServiceError("Разрешите Groq или Gemini обработать опыт из резюме.")
            if not self.hh_editor_task or self.hh_editor_task.done():
                self.hh_editor_error = ""
                self.hh_editor_task = asyncio.create_task(self.prepare_hh_editor(identifier))
            return self.json(self.hh_editor_state(), 202)
        plan = self.hh_editor_state()["hh_editor"]
        if not plan or data.get("revision") != plan["revision"]:
            raise ServiceError("Подготовьте актуальные изменения для выбранного резюме HH.")
        if command == "open":
            return self.json({"url": await self.browser.open_editor(identifier)})
        if command == "fill" and data.get("kind") in ("skills", "duties"):
            await self.browser.edit_resume_fields(plan, data["kind"], data.get("index"))
            return self.json({"filled": True})
        if command == "verify" and data.get("saved") is True:
            plan["verified"] = await self.browser.verify_editor_changes(plan)
            self.db.set("hh_editor", plan)
            return self.json(self.hh_editor_state())
        raise ValueError("Выберите действие с изменениями резюме.")

    async def prepare_hh_editor(self, identifier):
        try:
            snapshot = await self.browser.read_editor_sections(identifier)
            if not snapshot["jobs"]:
                raise ServiceError(
                    "HH не показал заполненные обязанности по местам работы. "
                    "Проверьте раздел «Опыт работы» в самом HH."
                )
            target = self.db.settings(self.config)["query"]
            source = {
                "target": clean_text(target),
                "skills": [clean_text(skill) for skill in snapshot["skills"]],
                "jobs": [job | {"job": index} for index, job in enumerate(snapshot["jobs"])],
                "vacancies": self.resume_vacancy_sample(target),
            }

            async def complete(provider):
                return await provider.complete(
                    hh_resume_editor.PROMPT,
                    source,
                    response_schema=hh_resume_editor.SCHEMA,
                    max_tokens=4200,
                )

            async with asyncio.timeout(60):
                async with self.scheduler.ai_lock:
                    delay = self.scheduler.ai_request_delay(int(time.time()))
                    if 0 < delay <= 12:
                        await asyncio.sleep(delay)
                    response = await self.scheduler.try_ai(complete, int(time.time()))
            if not response:
                raise ServiceError("Модели временно недоступны. Повторите анализ после паузы.")
            if self.db.get("hh_selected_resume", {}).get("id") != identifier:
                raise ServiceError("Выбранное резюме изменилось. Запустите анализ заново.")
            provider, result = response
            plan = hh_resume_editor.proposals(result, snapshot)
            self.db.set(
                "hh_editor",
                plan
                | {
                    "name": snapshot["name"],
                    "jobs": snapshot["jobs"],
                    "resume_id": identifier,
                    "revision": uuid4().hex,
                    "provider": provider,
                },
            )
        except HHAccessBarrier as exc:
            self.browser.record_access_barrier(exc)
            self.hh_editor_error = "HH ограничил доступ к резюме. Откройте карточку на hh.ru."
        except HHLoginRequired as exc:
            self.db.set("hh_needs_login", True)
            self.browser.pause("Нужно снова подключить HH.")
            self.hh_editor_error = str(exc)
        except (ServiceError, TimeoutError) as exc:
            self.hh_editor_error = str(exc) or "Подготовка изменений заняла слишком много времени."
        except Exception as exc:
            log.warning("Сбой подготовки правок HH (%s)", type(exc).__name__)
            self.hh_editor_error = "Не удалось подготовить изменения резюме."

    def resume_studio_action(self, data):
        action = data.get("action")
        resume = self.db.get("resume", "")
        digest = hashlib.sha256(resume.encode()).hexdigest()
        if action == "adopt" and data.get("confirm") is True:
            if self.browser.account_connected or self.db.get("hh_selected_resume"):
                raise ServiceError(
                    "Сначала обновите резюме на HH, затем загрузите его заново в приложении."
                )
            draft = self.db.get("resume_studio", {})
            if draft.get("profile_hash") != digest or not draft.get("text"):
                raise ServiceError("Подготовьте свежий черновик для текущего профиля.")
            return self.json(
                self.save_profile(
                    {"resume": draft["text"], "consent": self.db.get("groq_consent", False)}
                )
            )
        if action != "generate":
            raise ValueError("Выберите действие с резюме.")
        if self.resume_task and not self.resume_task.done():
            return self.json(self.resume_studio_state(), 202)
        mode, target, facts = data.get("mode"), data.get("target"), data.get("facts", "")
        if mode not in ("improve", "build"):
            raise ValueError("Выберите режим редактирования резюме.")
        if (
            not isinstance(target, str)
            or not 2 <= len(target.strip()) <= 120
            or len(target.splitlines()) != 1
            or not isinstance(facts, str)
            or len(facts) > 8000
        ):
            raise ValueError("Укажите должность одной строкой и до 8 000 символов опыта.")
        target = clean_text(target).strip()
        if sum(char.isalpha() for char in target) < 2 or "[скрыто]" in target:
            raise ValueError("Укажите название специальности без контактов.")
        use_provider = has_resume_evidence(resume, facts)
        if use_provider:
            if not self.config.ai_configured:
                raise ServiceError("Добавьте ключ Groq или Gemini в Bothost.")
            if data.get("consent") is True and self.config.groq_key:
                self.db.set("groq_consent", True)
            if not ai_allowed(self.config, self.db):
                raise ServiceError(
                    "Разрешите Groq или Gemini обработать профессиональные сведения."
                )
        self.resume_error = ""
        self.resume_task = asyncio.create_task(
            self.generate_resume(resume, digest, target, facts, mode, use_provider)
        )
        return self.json(self.resume_studio_state(), 202)

    def resume_vacancy_sample(self, target):
        words = {word[:6] for word in normalized(target).split() if len(word) > 2}
        matching = []
        for row in self.db.page((), 0, size=100)[:100]:
            vacancy = json.loads(row["details"])
            title = vacancy.get("title", "")
            overlap = words & {word[:6] for word in normalized(title).split()}
            if overlap:
                matching.append((len(overlap), vacancy, row))
        sample, seen = [], set()
        for _, vacancy, row in sorted(matching, key=lambda item: item[0], reverse=True):
            key = duplicate_key(vacancy) or row["id"]
            if key in seen:
                continue
            seen.add(key)
            result = self.current_analysis(row) or {}
            sample.append(
                {
                    "title": clean_text(vacancy["title"])[:120],
                    "required": result.get("required", []),
                    "keywords": clean_text(vacancy.get("description", ""))[:420],
                }
            )
            if len(sample) >= 8:
                break
        return sample

    async def generate_resume(self, resume, digest, target, facts, mode, use_provider):
        try:
            sample = self.resume_vacancy_sample(target)
            inputs = {
                "resume": resume,
                "target": target,
                "facts": facts,
                "mode": mode,
                "vacancy_sample": sample,
            }
            provider = ""

            async def complete(prompt, payload, **options):
                nonlocal provider
                completed = await self.scheduler.try_ai(
                    lambda service: service.complete(prompt, payload, **options), int(time.time())
                )
                if completed is None:
                    raise ServiceError(
                        "Лимит доступных моделей исчерпан или они временно недоступны."
                    )
                provider, response = completed
                return response

            if use_provider:
                async with asyncio.timeout(60):
                    async with self.scheduler.ai_lock:
                        delay = self.scheduler.ai_request_delay(int(time.time()))
                        if delay and delay <= 12:
                            await asyncio.sleep(delay)
                        if (
                            not ai_allowed(self.config, self.db)
                            or self.db.get("resume", "") != resume
                        ):
                            raise ServiceError("Профиль изменился. Запустите подготовку заново.")
                        result = await build_resume(
                            self.client, self.config, complete=complete, **inputs
                        )
            else:
                result = await build_resume(self.client, self.config, **inputs)
            if self.db.get("resume", "") != resume or (
                use_provider and not ai_allowed(self.config, self.db)
            ):
                raise ServiceError("Профиль изменился. Запустите подготовку заново.")
            self.db.set(
                "resume_studio",
                result
                | {
                    "profile_hash": digest,
                    "target": target,
                    "mode": mode,
                    "created": int(time.time()),
                    "sample_count": len(sample) if use_provider else 0,
                    "provider": provider,
                },
            )
        except (ValueError, ServiceError) as exc:
            self.resume_error = str(exc)
        except TimeoutError:
            self.resume_error = "Модель долго отвечает. Попробуйте подготовить черновик ещё раз."
        except Exception as exc:
            log.warning("Не удалось подготовить резюме (%s)", type(exc).__name__)
            self.resume_error = "Не удалось подготовить черновик. Повторите попытку."

    async def connect_hh(self, *, fresh=False, resume_mode=False):
        if not self.browser.available:
            raise ServiceError("На сервере не включён Chromium. Можно открыть HH на телефоне.")

        async def notify(result):
            if isinstance(result, ServiceError):
                if resume_mode:
                    self.db.set("hh_resumes_status", str(result))
                else:
                    self.db.set("mini_hh_progress", str(result))
            else:
                selected = self.db.get("hh_selected_resume", {})
                self.db.set(
                    "mini_hh_progress",
                    "Резюме привязано к профилю."
                    if selected
                    else "Вход подтверждён. Можно включить поиск."
                    if not result
                    else "Вход подтверждён. Выберите резюме.",
                )

        url = await self.browser.start_login(
            notify, in_mini=True, fresh=fresh, resume_mode=resume_mode
        )
        if not resume_mode:
            self.db.set("mini_hh_progress", "Окно входа открыто.")
        return url

    async def change_hh_mode(self, data):
        mode = data.get("mode")
        if mode == "stop":
            self.browser.pause("Фоновый поиск остановлен владельцем.")
        elif mode == "search" or (mode == "auto" and data.get("confirm") is True):
            self.browser.start_search(send=mode == "auto")
            await self.scheduler.start(self.bot)
        else:
            raise ValueError("Выберите режим и подтвердите автоотклики, если хотите их включить.")
        return self.state()

    async def import_job(self, raw):
        description = text(raw.get("description"), 5200)
        title = text(raw.get("title")) or (text(description.splitlines()[0]) if description else "")
        url = raw.get("url")
        if (
            not title
            or not isinstance(url, str)
            or not 0 < len(url) <= 600
            or any(char.isspace() or ord(char) < 32 for char in url)
            or "\\" in url
        ):
            raise ValueError("Укажите должность и HTTPS-ссылку на вакансию.")
        parsed = urlsplit(url)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or "." not in parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.port not in {None, 443}
        ):
            raise ValueError("Укажите обычную HTTPS-ссылку без логина и пароля.")
        url = parsed._replace(fragment="").geturl()
        site = parsed.hostname.removeprefix("www.")
        origin = next(
            (
                key
                for key, (_, href) in SOURCES.items()
                if site == urlsplit(href).hostname.removeprefix("www.")
            ),
            "external",
        )
        identifier = source_vacancy_id(origin, parsed.path)
        if not identifier:
            identifier = "external:" + hashlib.sha256(url.encode()).hexdigest()[:32]
        try:
            identifier = vacancy_id(url)
            origin, url = "hh", f"{HH}/vacancy/{identifier}"
        except ValueError:
            pass
        details = dict.fromkeys(URL_FIELDS, "")
        details.update(
            title=title,
            company=text(raw.get("company")) or site,
            url=url,
            description=description,
            source=origin,
            mini_filters={"region": "любой", "unknown_salary": True},
        )
        if self.db.add(identifier, details):
            if not description:
                self.db.transition(identifier, ("queued",), "deferred")
        elif description:
            self.db.add_vacancy_description(identifier, details)
        if description and self.db.vacancy(identifier)["status"] == "queued":
            self.db.set("processing", True)
            await self.scheduler.start(self.bot)
        return {"job": self.saved(self.db.vacancy(identifier))}

    async def job_action(self, data):
        identifier, action = data.get("id"), data.get("action")
        if not isinstance(identifier, str) or not 0 < len(identifier) <= 55:
            raise ValueError("Выберите вакансию.")
        row = self.db.vacancy(identifier)
        if action == "prepare":
            if not row:
                entry = self.catalog.get(identifier)
                if not entry or time.monotonic() - entry[0] > 1800:
                    raise ServiceError("Результаты устарели. Выполните поиск ещё раз.")
                _, details, criteria, _ = entry
                if details["source"] == "browser":
                    details = details | {"source": "mini_hh"}
                self.db.add(identifier, details | {"mini_filters": criteria})
            self.db.set("processing", True)
            await self.scheduler.start(self.bot)
        elif action == "reported" and row and data.get("confirm") is True:
            status = self.db.transition(
                identifier,
                ("queued", "draft", "approved", "deferred", "filtered", "questions", "error"),
                "reported",
                day=local_day(self.config),
                limit=self.db.settings(self.config)["limit"],
            )
            if status == "limit":
                raise ServiceError("Дневной лимит откликов достигнут.")
            if status != "ok":
                raise ServiceError("Этот статус уже изменился. Обновите список.")
        elif action == "send" and row and data.get("confirm") is True:
            day = local_day(self.config)
            if json.loads(row["details"]).get("source") == "superjob":
                await self.superjob.apply(row, day, self.db.settings(self.config)["limit"])
            else:
                await self.browser.submit_selected(self.bot, row, day)
        elif action == "edit" and row and row["status"] in EDITABLE and row["letter"]:
            letter = data.get("letter")
            if not isinstance(letter, str) or not 1 <= len(letter.strip()) <= 1200:
                raise ValueError("Введите от 1 до 1200 символов.")
            status = self.db.transition(
                identifier, (row["status"],), row["status"], letter=letter.strip()
            )
            if status != "ok":
                raise ServiceError("Этот отклик уже изменился. Обновите список.")
        elif action == "reconcile" and row and identifier.isascii() and identifier.isdigit():
            if not await self.browser.reconcile(identifier):
                raise ServiceError("HH пока не подтвердил отправку. Проверьте историю на сайте.")
        else:
            raise ValueError("Действие недоступно.")
        return {"job": self.saved(self.db.vacancy(identifier))}
