import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest

from app.config import Config
from app.database import Database
from app.scheduler import Scheduler
from app.services import AUTO_ANALYSIS_POLICY
from app.vacancies import parse_vacancy

GROQ_HOST = "api.groq.com"
GEMINI_HOST = "generativelanguage.googleapis.com"
RESUME_FACT = "Готовил управленческие отчёты и контролировал исполнение бюджета."
RESUME = f"Финансовый менеджер. {RESUME_FACT}"
RESULT = {
    "score": 92,
    "skills_score": 96,
    "duties": ["Готовить ежемесячные отчёты."],
    "reason": "Есть опыт управления бюджетом.",
    "required": ["Бюджетирование"],
    "missing": [],
    "letter": f"Откликаюсь на вакансию «Финансовый менеджер» в компании «Пример». {RESUME_FACT}",
    "evidence": [RESUME_FACT],
}


@pytest.fixture
def fallback(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.load_dotenv", lambda **_: None)
    for name, value in {
        "BROWSER_ENGINE": "manual",
        "OWNER_ID": "12345",
        "TELEGRAM_TOKEN": "12345:synthetic",
        "GROQ_API_KEY": "test-groq",
        "GEMINI_API_KEY": "test-gemini",
        "DATA_DIR": str(tmp_path),
    }.items():
        monkeypatch.setenv(name, value)
    config = Config.load()
    db = Database(config.data_dir)
    db.set_many({"resume": RESUME, "groq_consent": True})
    return config, db


def add_vacancy(
    db, identifier, description="Готовить ежемесячные отчёты по утверждённому бюджету."
):
    _, details = parse_vacancy(
        f"https://hh.ru/vacancy/{identifier}\n"
        "Должность: Финансовый менеджер\nКомпания: Пример\n"
        "Регион: Москва\nЗарплата: 150000-220000 RUB\nОписание:\n"
        f"{description} Сверять расходы по подразделениям."
    )
    db.add(identifier, details | {"source": "browser"})


def completed(request):
    text = json.dumps(RESULT, ensure_ascii=False)
    if request.url.host == GROQ_HOST:
        return httpx.Response(200, json={"choices": [{"message": {"content": text}}]})
    assert request.url.host == GEMINI_HOST
    return httpx.Response(
        200,
        json={"candidates": [{"finishReason": "STOP", "content": {"parts": [{"text": text}]}}]},
    )


def test_rate_limit_falls_back_for_same_vacancy_and_groq_recovers(fallback, monkeypatch):
    config, db = fallback
    db.set("gemini_consent", True)
    add_vacancy(db, "1250")
    clock = 1_800_000_000
    monkeypatch.setattr("app.scheduler.time", SimpleNamespace(time=lambda: clock))
    calls = []

    def response(request):
        calls.append(request.url.host)
        if len(calls) == 1:
            assert request.url.host == GROQ_HOST
            return httpx.Response(429, headers={"Retry-After": "120"})
        return completed(request)

    async def run():
        nonlocal clock
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            scheduler = Scheduler(config, db, client)
            await scheduler.tick_analysis(None)
            assert calls == [GROQ_HOST, GEMINI_HOST]
            ready = db.vacancy("1250")
            analyzed = json.loads(ready["result"])
            assert ready["status"] == "draft"
            assert ready["attempts"] == 0
            assert analyzed["analysis_provider"] == "gemini"
            assert analyzed["auto_policy"] == AUTO_ANALYSIS_POLICY
            assert RESUME_FACT in ready["letter"]
            assert "Финансовый менеджер" in ready["letter"]
            assert scheduler.analysis_status()["provider"] == "gemini"
            assert scheduler.provider_status("groq")["retry_at"] == clock + 120
            assert scheduler.provider_status("groq")["used"] == 1
            assert scheduler.provider_status("gemini")["used"] == 1
            add_vacancy(db, "1251", "Проверять еженедельные планы затрат по текущим проектам.")
            clock += 121
            restarted = Scheduler(config, Database(config.data_dir), client)
            await restarted.tick_analysis(None)
            assert calls == [GROQ_HOST, GEMINI_HOST, GROQ_HOST]
            assert json.loads(db.vacancy("1251")["result"])["analysis_provider"] == "groq"
            assert restarted.analysis_status()["provider"] == "groq"
            assert restarted.provider_status("groq")["notice"] == ""

    asyncio.run(run())


def test_existing_groq_consent_never_shares_data_with_gemini(fallback):
    config, db = fallback
    add_vacancy(db, "1250")
    calls = []

    def response(request):
        calls.append(request.url.host)
        assert request.url.host == GROQ_HOST
        return httpx.Response(429)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            scheduler = Scheduler(config, db, client)
            await scheduler.tick_analysis(None)
            await scheduler.tick_analysis(None)
            assert not scheduler.provider_status("gemini")["allowed"]
            assert scheduler.provider_status("gemini")["used"] == 0

    asyncio.run(run())
    row = db.vacancy("1250")
    assert calls == [GROQ_HOST]
    assert row["status"] == "queued" and row["attempts"] == 0
    assert row["letter"] is None


def test_daily_limits_pause_without_losing_rows_and_reset_automatically(fallback, monkeypatch):
    config, db = fallback
    config = replace(config, groq_daily_limit=1, gemini_daily_limit=1)
    day = "2026-11-27"
    monkeypatch.setattr("app.scheduler.local_day", lambda _: day)
    add_vacancy(db, "1250")
    db.set_many(
        {
            "gemini_consent": True,
            "groq_usage": {"day": day, "count": 1},
            "gemini_usage": {"day": day, "count": 1},
        }
    )
    calls = []

    def response(request):
        calls.append(request.url.host)
        return completed(request)

    async def run():
        nonlocal day
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            scheduler = Scheduler(config, db, client)
            await scheduler.tick_analysis(None)
            assert calls == []
            row = db.vacancy("1250")
            assert row["status"] == "queued" and row["attempts"] == 0
            day = "2026-11-28"
            await scheduler.tick_analysis(None)
            assert calls == [GROQ_HOST]
            assert db.get("groq_usage") == {"day": day, "count": 1}
            assert scheduler.provider_status("gemini")["used"] == 0
            assert db.vacancy("1250")["status"] == "draft"

    asyncio.run(run())
