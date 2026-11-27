import asyncio
import time
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qsl

import httpx
import pytest

from app.config import Config
from app.database import Database
from app.job_sources import superjob_row
from app.scheduler import Scheduler, local_day

QUOTE = "Готовил годовые бюджеты и ежемесячно сверял плановые и фактические расходы."
VACANCY = {
    "id": 123,
    "profession": "Финансовый менеджер",
    "firm_name": "Тестовая компания",
    "town": {"title": "Москва"},
    "payment_from": 180000,
    "currency": "rub",
    "candidat": "Ведение бюджета и подготовка финансовой отчётности.",
    "link": "https://www.superjob.ru/vakansii/manager-123.html",
}


@pytest.mark.parametrize(
    ("scenario", "status", "posts"),
    [
        ("accept", "sent", 1),
        ("existing", "duplicate", 0),
        ("changed_resume", "approved", 0),
        ("unknown", "unknown", 1),
    ],
)
def test_superjob_auto_is_independent_grounded_and_idempotent(
    tmp_path, monkeypatch, scenario, status, posts
):
    monkeypatch.setattr("app.config.load_dotenv", lambda **_: None)
    for key, value in {
        "TELEGRAM_TOKEN": "123:fixture",
        "OWNER_ID": "5",
        "LOGIN_PUBLIC_URL": "https://bot.example.test",
        "DATA_DIR": str(tmp_path),
        "PERSISTENT_STORAGE": "true",
        "AUTO_SEND": "true",
        "GROQ_API_KEY": "fixture",
        "SUPERJOB_CLIENT_ID": "42",
        "SUPERJOB_API_KEY": "fixture",
    }.items():
        monkeypatch.setenv(key, value)
    config = Config.load()
    db = Database(tmp_path)
    db.set_many(
        {
            "superjob_tokens": {"access": "fixture-auth", "expires": int(time.time()) + 600},
            "superjob_resume_id": "77",
            "groq_consent": True,
            "browser_notice": "HH вернул HTTP 403.",
            "browser_auto_enabled": False,
        }
    )
    db.add("1", {"source": "browser"})
    identifier, vacancy = superjob_row(VACANCY)
    db.add_discovered(identifier, vacancy)
    seen_posts, resume_reads = [], []

    def transport(request):
        path = request.url.path
        assert request.url.host == "api.superjob.ru"
        assert request.headers["Authorization"] == "Bearer fixture-auth"
        if path == "/1.0/user_cvs/":
            return httpx.Response(200, json={"objects": [{"id": 77, "profession": "Финансист"}]})
        if path == "/2.0/resumes/77/":
            resume_reads.append(path)
            duties = QUOTE
            if len(resume_reads) > 1 and scenario == "changed_resume":
                duties += " Составлял другой набор отчётов."
            return httpx.Response(
                200,
                json={
                    "id": 77,
                    "profession": "Финансист",
                    "phone": "+79990000000",
                    "work_history": [{"work": duties}],
                },
            )
        if path == "/2.0/vacancies/123/":
            return httpx.Response(
                200, json=VACANCY | {"already_sent_on_vacancy": scenario == "existing"}
            )
        assert path == "/2.0/send_cv_on_vacancy/"
        seen_posts.append(dict(parse_qsl(request.content.decode())))
        if scenario == "unknown":
            raise httpx.ReadTimeout("fixture")
        return httpx.Response(200, json={"result": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(transport)) as client:
            scheduler = Scheduler(config, db, client)
            await scheduler.superjob.enable_auto()
            assert "+79990000000" not in scheduler.superjob.snapshot()
            assert QUOTE in scheduler.superjob.snapshot()
            candidate = {
                "score": 92,
                "skills_score": 94,
                "missing": [],
                "required": ["Бюджетирование"],
                "duties": ["Вести бюджет."],
                "reason": "Подтверждено опытом.",
                "letter": f"Откликаюсь на должность «Финансовый менеджер» в «Тестовая компания». {QUOTE}",
                "evidence": [QUOTE],
                "analysis_provider": "groq",
            }
            scheduler.analyze = AsyncMock(return_value=candidate)
            await scheduler.tick_analysis(SimpleNamespace())
            assert db.vacancy(identifier)["status"] == "approved"
            day = local_day(config)
            await scheduler.superjob.auto_tick(day)
            assert db.vacancy(identifier)["status"] == status
            db.set("superjob_auto_next", 0)
            await scheduler.superjob.auto_tick(day)
            assert len(seen_posts) == posts
            assert db.vacancy("1")["status"] == "queued"
            assert "403" in db.get("browser_notice")
            if seen_posts:
                assert seen_posts[0]["id_cv"] == "77"
                assert seen_posts[0]["id_vacancy"] == "123"
                assert QUOTE in seen_posts[0]["comment"]
            assert bool(db.get("superjob_auto_enabled")) == (scenario in {"accept", "existing"})

    asyncio.run(run())
