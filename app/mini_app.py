import asyncio
import hashlib
import hmac
import json
import time
from collections import OrderedDict
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from aiohttp import web

from app.config import VERSION, setting
from app.database import LABELS
from app.hh_browser import HHLoginRequired
from app.job_sources import SEARCH_SOURCES, SOURCES, JobSources, source_url, source_vacancy_id, text
from app.resume import checked
from app.scheduler import local_day
from app.services import ServiceError
from app.superjob import SuperJob
from app.vacancies import HH, URL_FIELDS, vacancy_id

STATIC = Path(__file__).with_name("mini_web")
MAX_AGE = 3600
EDITABLE = ("draft", "approved", "deferred", "questions")
HEADERS = {
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
    "Content-Security-Policy": (
        "default-src 'none'; script-src 'self' https://telegram.org; "
        "style-src 'self'; img-src 'self' data:; connect-src 'self'; frame-src 'self'; "
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


class MiniApp:
    def __init__(self, config, db, scheduler, client, bot):
        self.config = config
        self.db = db
        self.scheduler = scheduler
        self.browser = scheduler.browser
        self.sources = JobSources(config, self.browser, client)
        self.superjob = SuperJob(config, db, client)
        self.bot = bot
        self.search_lock = asyncio.Lock()
        self.catalog = OrderedDict()

    def add_routes(self, app):
        app.add_routes(
            [
                web.get("/mini", self.asset),
                web.get("/mini/{asset:app\\.js|app\\.css}", self.asset),
                web.get("/mini/api/{command:state|search|vacancy}", self.api),
                web.get("/mini/oauth/superjob", self.superjob_callback),
                web.get("/mini/oauth/finished", self.oauth_finished),
                web.post(
                    "/mini/api/{command:job|profile|connect|resume|cancel|disconnect|import|hh-mode|hh-resumes|hh-browser|superjob}",
                    self.api,
                ),
            ]
        )

    async def asset(self, request):
        file = request.match_info.get("asset", "index.html")
        mime = {
            "index.html": "text/html",
            "app.js": "application/javascript",
            "app.css": "text/css",
        }
        return web.Response(
            text=(STATIC / file).read_text(encoding="utf-8"),
            content_type=mime[file],
            headers=HEADERS,
        )

    def json(self, payload, status=200):
        return web.json_response(payload, status=status, headers=HEADERS)

    async def superjob_callback(self, request):
        success = await self.superjob.connect(request.query)
        location = "/mini/oauth/finished" + ("?connected=1" if success else "")
        raise web.HTTPSeeOther(location=location, headers=HEADERS)

    async def oauth_finished(self, request):
        status = (
            "SuperJob подключён."
            if request.query.get("connected") == "1"
            else "SuperJob не подтвердил вход."
        )
        return web.Response(
            text=f'<!doctype html><html lang="ru"><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width, initial-scale=1">'
            f'<title>SuperJob</title><link rel="stylesheet" href="/mini/app.css">'
            f'<main class="oauth-result"><h1>{status}</h1>'
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
            if command == "vacancy":
                return self.json(self.vacancy_details(request.query.get("id", "")))
            data = await request.clone(client_max_size=100_000).json()
            if not isinstance(data, dict):
                raise ValueError("Ожидались данные в виде объекта.")
            if command == "job":
                return self.json(await self.job_action(data))
            if command == "profile":
                return self.json(self.save_profile(data))
            if command == "import":
                return self.json(await self.import_job(data))
            if command == "hh-mode":
                return self.json(await self.change_hh_mode(data))
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
        except HHLoginRequired as exc:
            self.db.set("hh_needs_login", True)
            self.browser.pause("Нужно снова подключить HH.")
            return self.json({"error": str(exc)}, 400)
        except (ValueError, ServiceError) as exc:
            return self.json({"error": str(exc)}, 400)
        except OSError:
            return self.json({"error": "Хранилище недоступно. Проверьте Volume."}, 500)

    def state(self):
        prefs = self.db.settings(self.config)
        counts, today = self.db.counts(local_day(self.config))
        resumes = self.db.get("superjob_resumes", {})
        hh_resume = self.db.get("hh_selected_resume", {})
        hh_needs_login = self.db.get("hh_needs_login", False)
        hh_login_open = bool(self.browser.login_task and not self.browser.login_task.done())
        return {
            "version": VERSION,
            "defaults": {key: prefs[key] for key in ("query", "region", "salary")},
            "sources": self.directory(prefs),
            "jobs": [self.saved(row) for row in self.db.page((), 0, size=60)[:60]],
            "resume": self.db.get("resume", ""),
            "consent": self.db.get("groq_consent", False),
            "groq": bool(self.config.groq_key),
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
            "hh_send": self.browser.available and self.config.persistent,
            "hh_auto_allowed": self.browser.available
            and self.config.auto_send
            and self.config.persistent,
            "hh_running": bool(self.db.get("browser_search")),
            "hh_auto": bool(self.db.get("browser_auto_enabled")),
            "hh_notice": self.db.get("browser_notice", ""),
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
            },
        }

    def directory(self, prefs):
        modes = {
            "hh": "browser" if self.browser.available else "link",
            "trudvsem": "public",
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
                "mode": modes.get(key, "link"),
                "in_app_apply": (key == "hh" and self.browser.available)
                or (key == "superjob" and self.superjob.available),
            }
            for key, info in SOURCES.items()
        ]

    def saved(self, row):
        job = json.loads(row["details"])
        result = json.loads(row["result"] or "{}")
        site = job.get("source")
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
            "score": result.get("score"),
            "reason": row["error"] or result.get("reason") or "",
            "can_edit": row["status"] in EDITABLE and bool(row["letter"]),
            "can_send": site in {"browser", "mini_hh", "superjob"}
            and row["status"] in {"draft", "approved"}
            and bool(row["letter"]),
        }

    def vacancy_details(self, identifier):
        if not 1 <= len(identifier) <= 55:
            raise ValueError("Выберите вакансию.")
        row = self.db.vacancy(identifier)
        entry = self.catalog.get(identifier)
        if row:
            detail = json.loads(row["details"])
        elif entry and time.monotonic() - entry[0] <= 1800:
            detail = entry[1]
        else:
            raise ServiceError("Вакансия устарела. Обновите поиск.")
        return {
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

    async def search(self, query):
        prefs = self.db.settings(self.config)
        criteria = {
            key: setting(key, query.get(key, str(prefs[key])))
            for key in ("query", "region", "salary")
        }
        criteria["unknown_salary"] = criteria["salary"] == 0 or prefs["unknown_salary"]
        prefs |= criteria
        prefs["hh_title_only"] = query.get("hh_title_only", "true") == "true"
        source = query.get("source", "all")
        if source != "all" and source not in SOURCES:
            raise ValueError("Неизвестный источник.")
        if self.search_lock.locked():
            raise ServiceError("Поиск уже выполняется.")
        keys = SEARCH_SOURCES if source == "all" else (source,)
        if query.get("apply_only") == "true":
            applicable = {item["id"] for item in self.directory(prefs) if item["in_app_apply"]}
            keys = tuple(key for key in keys if key in applicable)
        async with self.search_lock:
            results = await asyncio.gather(
                *(self.sources.search(prefs, key) for key in keys), return_exceptions=True
            )
        jobs = []
        notices = []
        for key, result in zip(keys, results, strict=True):
            if isinstance(result, BaseException):
                message = (
                    str(result)
                    if isinstance(result, ServiceError)
                    else "Источник временно недоступен."
                )
                notices.append({"source": key, "message": message})
                continue
            for identifier, data in result:
                self.catalog[identifier] = (time.monotonic(), data, criteria)
                self.catalog.move_to_end(identifier)
                jobs.append(
                    {
                        "id": identifier,
                        "title": data["title"],
                        "company": data["company"],
                        "region": data["region"],
                        "salary": data["salary"],
                        "url": data["url"],
                        "source": key,
                        "saved": self.db.vacancy(identifier) is not None,
                    }
                )
        while len(self.catalog) > 150:
            self.catalog.popitem(last=False)
        return {"jobs": jobs, "notices": notices, "sources": self.directory(prefs)}

    def save_profile(self, data):
        resume, consent = data.get("resume"), data.get("consent")
        if not isinstance(resume, str) or type(consent) is not bool:
            raise ValueError("Проверьте текст резюме и согласие.")
        resume = checked(resume.strip())
        values = {"resume": resume, "groq_consent": consent}
        if resume != self.db.get("resume", ""):
            self.browser.pause("Данные резюме изменены.", keep_search=True)
            values["hh_selected_resume"] = {}
        if not consent:
            self.browser.pause("Разрешение на анализ отключено.", keep_search=True)
        self.db.set_many(values)
        return self.state()

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
        title = text(raw.get("title"))
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
        description = text(raw.get("description"), 5200)
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
            else:
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
                _, details, criteria = entry
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
