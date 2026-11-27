import asyncio
import re
import secrets
import time
from urllib.parse import urlencode

import httpx

from app.services import ServiceError

ORIGIN = "https://career.habr.com"
AUTH = ORIGIN + "/integrations/oauth/"
APPLICATIONS = ORIGIN + "/profile/applications"
USER = ORIGIN + "/api/v1/integrations/users/me"
OAUTH_STATE = re.compile(r"[A-Za-z0-9_-]{43}")


class HabrAccount:
    def __init__(self, config, db, client):
        self.config = config
        self.db = db
        self.client = client
        self.pending = None
        self.lock = asyncio.Lock()

    @property
    def available(self):
        return bool(
            self.config.habr_client_id
            and self.config.habr_client_secret
            and self.config.login_public_url
        )

    @property
    def redirect(self):
        return self.config.login_public_url + "/mini/oauth/habr"

    def start(self):
        if not self.available:
            raise ServiceError("Укажите HABR_CLIENT_ID и HABR_CLIENT_SECRET в Bothost.")
        if self.lock.locked():
            raise ServiceError("Проверка аккаунта Хабр Карьеры уже выполняется.")
        state = secrets.token_urlsafe(32)
        self.pending = (state, time.monotonic() + 600)
        self.db.set("habr_status", "Завершите вход на Хабр Карьере и вернитесь в Telegram.")
        return (
            AUTH
            + "authorize?"
            + urlencode(
                {
                    "client_id": self.config.habr_client_id,
                    "redirect_uri": self.redirect,
                    "response_type": "code",
                    "state": state,
                }
            )
        )

    async def connect(self, query):
        expected, expires = self.pending or ("", 0)
        states = query.getall("state", [])
        if (
            not self.available
            or len(states) != 1
            or not OAUTH_STATE.fullmatch(states[0])
            or time.monotonic() >= expires
            or not secrets.compare_digest(states[0], expected)
        ):
            return False
        self.pending = None
        codes = query.getall("code", [])
        if query.getall("error", []) or len(codes) != 1 or not 1 <= len(codes[0]) <= 1024:
            self.db.set("habr_status", "Вход отменён. Можно начать заново.")
            return False
        async with self.lock:
            try:
                token_reply = await self.client.post(
                    AUTH + "token",
                    data={
                        "client_id": self.config.habr_client_id,
                        "client_secret": self.config.habr_client_secret,
                        "redirect_uri": self.redirect,
                        "grant_type": "authorization_code",
                        "code": codes[0],
                    },
                    timeout=12,
                    follow_redirects=False,
                )
                token_reply.raise_for_status()
                token = token_reply.json()["access_token"]
                if not isinstance(token, str) or not 1 <= len(token) <= 4096 or not token.isascii():
                    raise ValueError
                profile_reply = await self.client.get(
                    USER, params={"access_token": token}, timeout=12, follow_redirects=False
                )
                profile_reply.raise_for_status()
                login = profile_reply.json()["login"]
                if not isinstance(login, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,80}", login):
                    raise ValueError
            except (httpx.HTTPError, KeyError, TypeError, ValueError):
                self.db.set(
                    "habr_status",
                    "Хабр Карьера не подтвердила профиль. Проверьте активацию приложения и URL возврата.",
                )
                return False
            self.db.set_many(
                {
                    "habr_profile": {"login": login, "verified_at": int(time.time())},
                    "habr_status": "Профиль подтверждён.",
                }
            )
            return True

    def disconnect(self):
        if self.lock.locked():
            raise ServiceError("Дождитесь проверки профиля Хабр Карьеры.")
        self.pending = None
        self.db.set_many({"habr_profile": {}, "habr_status": ""})

    def state(self):
        return {
            "enabled": self.available,
            "profile": self.db.get("habr_profile", {}),
            "status": self.db.get("habr_status", ""),
            "applications_url": APPLICATIONS,
        }
