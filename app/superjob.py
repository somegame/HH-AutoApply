import asyncio
import secrets
import time
from urllib.parse import urlencode

import httpx

from app.job_sources import text
from app.services import ServiceError

API = "https://api.superjob.ru"


class SuperJob:
    def __init__(self, config, db, client):
        self.config = config
        self.db = db
        self.client = client
        self.lock = asyncio.Lock()
        self.pending = None

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
            }
        )

    def select(self, identifier):
        if self.lock.locked():
            raise ServiceError("Дождитесь завершения запроса SuperJob.")
        resumes = self.db.get("superjob_resumes", {})
        if not isinstance(identifier, str) or identifier not in resumes:
            raise ServiceError("Выберите резюме из аккаунта SuperJob.")
        self.db.set("superjob_resume_id", identifier)

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

    async def apply(self, row, day, limit):
        if self.lock.locked():
            raise ServiceError("SuperJob уже выполняет запрос.")
        async with self.lock:
            tokens = self.db.get("superjob_tokens", {})
            selected = self.db.get("superjob_resume_id", "")
            identifier = row["id"]
            vacancy = identifier.removeprefix("superjob:")
            if not (
                self.available
                and self.config.persistent
                and tokens
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
            if tokens["expires"] <= int(time.time()) + 30:
                try:
                    tokens = await self.token_exchange(
                        "refresh_token", {"refresh_token": tokens["refresh"]}
                    )
                except ServiceError:
                    self.db.set_many(
                        {
                            "superjob_status": "Нужно снова подключить SuperJob.",
                            "superjob_needs_login": True,
                        }
                    )
                    raise
                self.db.set("superjob_tokens", tokens)
            if self.db.vacancy(identifier)["letter"] != row["letter"]:
                raise ServiceError("Письмо изменилось. Откройте его перед отправкой.")
            reserved = self.db.transition(
                identifier, ("draft", "approved"), "sending", day=day, limit=limit
            )
            if reserved == "limit":
                raise ServiceError("Дневной лимит откликов достигнут.")
            if reserved != "ok":
                raise ServiceError("Статус изменился. Обновите отклики.")
            try:
                response = await self.client.post(
                    f"{API}/2.0/send_cv_on_vacancy/",
                    headers=self.headers(tokens["access"]),
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
                        "SuperJob отклонил отправку. Проверьте аккаунт и доступность вакансии."
                    )
                else:
                    self.db.complete_browser_application(identifier, "unknown")
            except (httpx.HTTPError, ValueError):
                self.db.complete_browser_application(identifier, "unknown")
            except asyncio.CancelledError:
                self.db.complete_browser_application(identifier, "unknown")
                raise
