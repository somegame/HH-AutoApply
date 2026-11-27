import asyncio
import hashlib
import json
import secrets
import time
from contextlib import closing
from urllib.parse import urlencode

import httpx

from app.job_sources import superjob_row, text
from app.resume import checked
from app.services import (
    AUTO_ANALYSIS_POLICY,
    ServiceError,
    ai_allowed,
    auto_letter_supported,
)
from app.vacancies import filter_reason

API = "https://api.superjob.ru"


class SuperJob:
    def __init__(self, config, db, client):
        self.config = config
        self.db = db
        self.client = client
        self.lock = asyncio.Lock()
        self.pending = None
        self.auto_revision = 0

    @property
    def available(self):
        identifier = self.config.superjob_client_id
        return (
            bool(self.config.superjob_key and self.config.login_public_url)
            and identifier.isascii()
            and identifier.isdecimal()
        )

    @property
    def redirect(self):
        return self.config.login_public_url + "/mini/oauth/superjob"

    def start(self):
        if not self.available:
            raise ServiceError(
                "Добавьте SUPERJOB_CLIENT_ID и ключ приложения в настройках хостинга."
            )
        if self.lock.locked():
            raise ServiceError("SuperJob уже выполняет запрос.")
        state = secrets.token_urlsafe(32)
        self.pending = (state, time.monotonic() + 600)
        self.db.set("superjob_status", "Авторизация открыта. Завершите вход на SuperJob.")
        query = urlencode(
            {
                "client_id": self.config.superjob_client_id,
                "redirect_uri": self.redirect,
                "state": state,
            }
        )
        return "https://www.superjob.ru/authorize/?" + query

    async def connect(self, query):
        candidate, expires = self.pending or ("", 0)
        state = query.get("state", "")
        if (
            not self.available
            or len(query.getall("state", [])) != 1
            or len(state) != 43
            or not state.isascii()
            or time.monotonic() >= expires
            or not secrets.compare_digest(state, candidate)
        ):
            return False
        self.pending = None
        if len(query.getall("code", [])) != 1 or not 1 <= len(query.get("code", "")) <= 1024:
            self.db.set("superjob_status", "Авторизация SuperJob отменена. Можно начать заново.")
            return False
        async with self.lock:
            try:
                tokens = await self.token_exchange(
                    "access_token", {"code": query["code"], "redirect_uri": self.redirect}
                )
                resumes = await self.resumes(tokens["access"])
            except ServiceError as exc:
                self.db.set("superjob_status", str(exc))
                return False
            self.db.set_many(
                {
                    "superjob_tokens": tokens,
                    "superjob_resumes": resumes,
                    "superjob_resume_id": next(iter(resumes)) if len(resumes) == 1 else "",
                    "superjob_status": "Вход подтверждён." if resumes else "В аккаунте нет резюме.",
                    "superjob_needs_login": False,
                    "superjob_auto_enabled": False,
                    "superjob_auto_resume": {},
                    "superjob_auto_notice": "",
                }
            )
            return True

    def disconnect(self):
        if self.lock.locked():
            raise ServiceError("Дождитесь завершения запроса SuperJob.")
        self.pending = None
        self.db.set_many(
            {
                "superjob_tokens": {},
                "superjob_resumes": {},
                "superjob_resume_id": "",
                "superjob_status": "",
                "superjob_needs_login": False,
                "superjob_auto_enabled": False,
                "superjob_auto_resume": {},
                "superjob_auto_notice": "",
            }
        )

    def select(self, identifier):
        if self.lock.locked():
            raise ServiceError("Дождитесь завершения запроса SuperJob.")
        resumes = self.db.get("superjob_resumes", {})
        if not isinstance(identifier, str) or identifier not in resumes:
            raise ServiceError("Выберите резюме из аккаунта SuperJob.")
        if self.db.get("superjob_resume_id") != identifier:
            self.db.set_many(
                {
                    "superjob_resume_id": identifier,
                    "superjob_auto_enabled": False,
                    "superjob_auto_resume": {},
                    "superjob_auto_notice": "Для нового резюме подтвердите автоотклики заново.",
                }
            )

    async def token_exchange(self, method, fields):
        params = {
            "client_id": self.config.superjob_client_id,
            "client_secret": self.config.superjob_key,
        } | fields
        url = f"{API}/2.0/oauth2/{method}/"
        try:
            if method == "access_token":
                response = await self.client.post(url, data=params, timeout=12)
            else:
                response = await self.client.get(url, params=params, timeout=12)
            data = response.json() if response.is_success else {}
            access, refresh = data.get("access_token"), data.get("refresh_token")
            if not all(
                isinstance(value, str) and 0 < len(value) <= 4096 for value in (access, refresh)
            ):
                raise ValueError
            duration = max(60, min(7 * 86400, int(data.get("expires_in", 3600))))
            return {"access": access, "refresh": refresh, "expires": int(time.time()) + duration}
        except (httpx.HTTPError, AttributeError, TypeError, ValueError):
            raise ServiceError(
                "SuperJob не подтвердил авторизацию. Подключите аккаунт снова."
            ) from None

    def headers(self, access):
        return {"X-Api-App-Id": self.config.superjob_key, "Authorization": "Bearer " + access}

    async def resumes(self, access):
        try:
            response = await self.client.get(
                f"{API}/1.0/user_cvs/", headers=self.headers(access), timeout=12
            )
            data = response.json() if response.is_success else {}
            rows = data.get("objects")
            if not isinstance(rows, list):
                raise ValueError
            return {
                str(row["id"]): text(row.get("profession"), 90) or "Резюме SuperJob"
                for row in rows[:50]
                if isinstance(row, dict) and type(row.get("id")) is int and row["id"] > 0
            }
        except (httpx.HTTPError, AttributeError, TypeError, ValueError):
            raise ServiceError("SuperJob не вернул список ваших резюме.") from None

    def pause_auto(self, message):
        self.auto_revision += 1
        self.db.set_many({"superjob_auto_enabled": False, "superjob_auto_notice": message})

    def snapshot(self):
        data = self.db.get("superjob_auto_resume", {})
        if data.get("id") != self.db.get("superjob_resume_id") or self.db.get(
            "superjob_needs_login"
        ):
            return ""
        return data.get("text", "")

    @property
    def auto_allowed(self):
        return bool(self.available and self.config.persistent and self.config.auto_send)

    async def access(self):
        tokens = self.db.get("superjob_tokens", {})
        if not tokens or self.db.get("superjob_needs_login"):
            raise ServiceError("Сначала подключите аккаунт SuperJob.")
        if tokens["expires"] <= int(time.time()) + 30:
            try:
                tokens = await self.token_exchange(
                    "refresh_token", {"refresh_token": tokens["refresh"]}
                )
            except ServiceError:
                message = "Нужно снова подключить SuperJob."
                self.db.set_many({"superjob_status": message, "superjob_needs_login": True})
                self.pause_auto(message)
                raise
            self.db.set("superjob_tokens", tokens)
        return tokens["access"]

    async def verified_get(self, path, access):
        try:
            reply = await self.client.get(f"{API}{path}", headers=self.headers(access), timeout=12)
            if reply.status_code == 401:
                self.db.set("superjob_needs_login", True)
            if reply.status_code == 429:
                retry = reply.headers.get("Retry-After", "")
                seconds = (
                    int(retry) if retry.isascii() and retry.isdigit() and len(retry) <= 6 else 120
                )
                raise ServiceError("SuperJob ограничил частоту запросов.", max(120, seconds))
            if reply.status_code >= 500:
                raise ServiceError("SuperJob временно недоступен.", 120)
            reply.raise_for_status()
            data = reply.json()
            if not isinstance(data, dict):
                raise ValueError
            return data
        except httpx.RequestError:
            raise ServiceError("SuperJob не ответил вовремя.", 120) from None
        except (httpx.HTTPStatusError, ValueError):
            raise ServiceError(
                "SuperJob не подтвердил доступ к данным. Проверьте аккаунт."
            ) from None

    async def professional_resume(self, access, identifier):
        if not identifier.isascii() or not identifier.isdecimal():
            raise ServiceError("Выберите резюме из аккаунта SuperJob.")
        row = await self.verified_get(f"/2.0/resumes/{identifier}/", access)
        if str(row.get("id")) != identifier:
            raise ServiceError("SuperJob вернул другое резюме.")
        parts = [
            text(row.get(key), 2500)
            for key in ("profession", "achievements", "additional_information")
        ]
        history = row.get("work_history")
        if isinstance(history, list):
            for item in history[:15]:
                if isinstance(item, dict):
                    parts.extend(
                        text(item.get(key), 2400) for key in ("profession", "name", "work")
                    )
        try:
            return checked("\n".join(part for part in parts if part)[:9000])
        except ValueError:
            raise ServiceError(
                "Дополните опыт в выбранном резюме SuperJob и попробуйте снова."
            ) from None

    async def enable_auto(self):
        if not self.auto_allowed:
            raise ServiceError(
                "Автоотклики требуют AUTO_SEND=true и постоянного хранилища Bothost."
            )
        if not ai_allowed(self.config, self.db):
            raise ServiceError("Разрешите анализ резюме в Groq или Gemini.")
        if self.lock.locked():
            raise ServiceError("Дождитесь завершения запроса SuperJob.")
        async with self.lock:
            revision = self.auto_revision
            selected = self.db.get("superjob_resume_id", "")
            access = await self.access()
            if selected not in await self.resumes(access):
                raise ServiceError("Выберите своё резюме в аккаунте SuperJob.")
            resume = await self.professional_resume(access, selected)
            if self.auto_revision != revision or not ai_allowed(self.config, self.db):
                raise ServiceError("Включение автооткликов отменено.")
            self.db.set_many(
                {
                    "superjob_auto_resume": {"id": selected, "text": resume},
                    "superjob_auto_enabled": True,
                    "superjob_auto_notice": "Резюме проверено. Автоотклики включены.",
                    "superjob_auto_next": 0,
                }
            )

    def eligible(self, row, settings, resume):
        details = json.loads(row["details"])
        result = json.loads(row["result"] or "{}")
        return (
            details.get("auto_discovered") is True
            and details.get("source") == "superjob"
            and result.get("auto_policy") == AUTO_ANALYSIS_POLICY
            and result.get("resume_hash") == hashlib.sha256(resume.encode()).hexdigest()
            and not filter_reason(details, settings)
            and auto_letter_supported(
                result,
                resume,
                details,
                settings["score"],
                row["letter"],
                skills_threshold=settings["skills_score"],
            )
        )

    async def auto_tick(self, day):
        if not self.db.get("superjob_auto_enabled") or self.lock.locked():
            return
        if not (
            self.auto_allowed and ai_allowed(self.config, self.db) and (resume := self.snapshot())
        ):
            self.pause_auto("Проверьте аккаунт, резюме и разрешение на анализ SuperJob.")
            return
        now = int(time.time())
        if now < self.db.get("superjob_auto_next", 0):
            return
        settings = self.db.settings(self.config)
        if self.db.counts(day)[1] >= settings["limit"]:
            return
        digest = hashlib.sha256(resume.encode()).hexdigest()
        self.db.refresh_auto_drafts(
            AUTO_ANALYSIS_POLICY, digest, min_score=settings["score"], source="superjob"
        )
        with closing(
            self.db.browser_candidates(
                policy=AUTO_ANALYSIS_POLICY,
                resume_hash=digest,
                min_score=settings["score"],
                min_skills=settings["skills_score"],
                source="superjob",
            )
        ) as candidates:
            row = next((row for row in candidates if self.eligible(row, settings, resume)), None)
        if row is None:
            return
        self.db.set("superjob_auto_next", now + 90)
        try:
            await self.apply(row, day, settings["limit"], automatic=True)
        except ServiceError as exc:
            if exc.retry:
                self.db.set_many(
                    {
                        "superjob_auto_next": now + max(90, exc.retry),
                        "superjob_auto_notice": str(exc),
                    }
                )
            else:
                self.pause_auto(str(exc))

    async def verify_auto_candidate(self, row, access, selected, vacancy):
        settings = self.db.settings(self.config)
        resume = self.snapshot()
        if (
            not self.db.get("superjob_auto_enabled")
            or not resume
            or not self.eligible(row, settings, resume)
        ):
            return None
        if await self.professional_resume(access, selected) != resume:
            self.db.set("superjob_auto_resume", {})
            raise ServiceError("Резюме изменилось на SuperJob. Подтвердите автоотклики заново.")
        live = await self.verified_get(f"/2.0/vacancies/{vacancy}/", access)
        if type(live.get("id")) is not int or str(live["id"]) != vacancy:
            raise ServiceError("SuperJob вернул другую вакансию. Проверьте карточку.")
        if live.get("already_sent_on_vacancy") is True:
            self.db.transition(row["id"], ("draft", "approved"), "duplicate")
            return None
        if live.get("already_sent_on_vacancy") is not False:
            raise ServiceError("SuperJob не подтвердил отсутствие предыдущего отклика.")
        current = superjob_row(live)
        original = json.loads(row["details"])
        if (
            not current
            or not all(
                current[1].get(key) == original.get(key)
                for key in ("title", "company", "description")
            )
            or filter_reason(current[1], settings)
        ):
            self.db.transition(
                row["id"],
                ("draft", "approved"),
                "deferred",
                error="Вакансия изменилась на SuperJob. Проверьте её перед откликом.",
            )
            return None
        if not (
            self.db.get("superjob_auto_enabled")
            and self.snapshot() == resume
            and ai_allowed(self.config, self.db)
            and self.db.settings(self.config) == settings
        ):
            return None
        return settings

    async def apply(self, row, day, limit, *, automatic=False):
        if self.lock.locked():
            raise ServiceError("SuperJob уже выполняет запрос.")
        async with self.lock:
            selected = self.db.get("superjob_resume_id", "")
            identifier = row["id"]
            vacancy = identifier.removeprefix("superjob:")
            if not (
                self.available
                and self.config.persistent
                and not self.db.get("superjob_needs_login")
                and selected
                and identifier.startswith("superjob:")
                and vacancy.isascii()
                and vacancy.isdecimal()
                and row["status"] in {"draft", "approved"}
                and row["letter"]
            ):
                raise ServiceError(
                    "Для отклика подключите SuperJob, выберите резюме и подготовьте письмо."
                )
            access = await self.access()
            if automatic:
                settings = await self.verify_auto_candidate(row, access, selected, vacancy)
                if settings is None:
                    return
                limit = settings["limit"]
            current = self.db.vacancy(identifier)
            if current is None or any(
                current[key] != row[key] for key in ("letter", "details", "result")
            ):
                raise ServiceError("Письмо изменилось. Откройте его перед отправкой.")
            reserved = self.db.transition(
                identifier, ("draft", "approved"), "sending", day=day, limit=limit
            )
            if reserved == "limit":
                if automatic:
                    return
                raise ServiceError("Дневной лимит откликов достигнут.")
            if reserved != "ok":
                raise ServiceError("Статус изменился. Обновите отклики.")
            try:
                response = await self.client.post(
                    f"{API}/2.0/send_cv_on_vacancy/",
                    headers=self.headers(access),
                    data={"id_vacancy": vacancy, "id_cv": selected, "comment": row["letter"]},
                    timeout=16,
                )
                result = response.json()
                if (
                    response.is_success
                    and isinstance(result, dict)
                    and result.get("result") is True
                ):
                    self.db.complete_browser_application(identifier, "sent")
                    if automatic:
                        self.db.set(
                            "superjob_auto_notice", "Последний автоотклик подтверждён SuperJob."
                        )
                elif (
                    response.status_code in {400, 401, 403, 404, 422, 429}
                    and isinstance(result, dict)
                    and isinstance(result.get("error"), dict)
                ):
                    self.db.transition(identifier, ("sending",), "draft")
                    if response.status_code == 401:
                        self.db.set_many(
                            {
                                "superjob_status": "Нужно снова подключить SuperJob.",
                                "superjob_needs_login": True,
                            }
                        )
                    raise ServiceError(
                        "SuperJob отклонил отправку. Проверьте аккаунт и доступность вакансии.",
                        120 if response.status_code == 429 else 0,
                    )
                else:
                    self.db.complete_browser_application(identifier, "unknown")
                    self.pause_auto("SuperJob не подтвердил отправку. Проверьте историю откликов.")
            except (httpx.HTTPError, ValueError):
                self.db.complete_browser_application(identifier, "unknown")
                self.pause_auto("Результат отправки неизвестен. Проверьте историю SuperJob.")
            except asyncio.CancelledError:
                self.db.complete_browser_application(identifier, "unknown")
                self.pause_auto("Отправка прервана. Проверьте историю SuperJob.")
                raise
