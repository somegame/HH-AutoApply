import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from app import hh_browser
from app.browser_workflow import BrowserWorkflow
from app.config import Config
from app.database import Database
from app.scheduler import Scheduler, local_day
from app.services import (
    AUTO_ANALYSIS_POLICY,
    ServiceError,
    auto_letter_supported,
    grounded_auto_letter,
)
from app.vacancies import parse_vacancy

VACANCY_ID = "1200"
RESUME_ID = "1a2b3c4d5e6f7a8b"
TITLE = "Финансовый менеджер"
COMPANY = "Пример"
EVIDENCE = "Управление годовым бюджетом и контроль расходов"
RESUME = f"{EVIDENCE}. Подготовка управленческой отчётности."
LETTER = f"Здравствуйте! Меня интересует вакансия «{TITLE}» в компании «{COMPANY}». {EVIDENCE}."
ANALYSIS = {
    "score": 95,
    "skills_score": 92,
    "duties": ["Планировать бюджет и следить за расходами."],
    "reason": "Профиль соответствует требованиям по бюджетированию.",
    "required": ["Управление бюджетом"],
    "missing": [],
    "letter": LETTER,
    "evidence": [EVIDENCE],
}


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.load_dotenv", lambda **_: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "12345:synthetic")
    monkeypatch.setenv("OWNER_ID", "12345")
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    monkeypatch.setenv("BROWSER_ENGINE", "chromium")
    monkeypatch.setenv("PERSISTENT_STORAGE", "true")
    monkeypatch.setenv("AUTO_SEND", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setattr("app.browser_workflow.resource_warning", lambda _: None)
    config = Config.load()
    config.defaults.update(query=TITLE, region="Москва", score=80, salary=150000, currency="RUB")
    return config


@pytest.fixture
def vacancy():
    _, details = parse_vacancy(
        f"https://hh.ru/vacancy/{VACANCY_ID}\nДолжность: {TITLE}\nКомпания: {COMPANY}\n"
        "Регион: Москва\nЗарплата: 150000 RUB\n"
        "Описание: Управление годовым бюджетом компании, контроль расходов и отчётность."
    )
    return details | {"source": "browser"}


def test_auto_letter_accepts_exact_resume_evidence(vacancy):
    letter = grounded_auto_letter(ANALYSIS, RESUME, vacancy, 80)
    assert all(phrase in letter for phrase in (TITLE, COMPANY, EVIDENCE))
    assert auto_letter_supported(ANALYSIS | {"letter": letter}, RESUME, vacancy, 80, letter)


@pytest.mark.parametrize(
    "changes",
    [
        {"score": 79},
        {"skills_score": 79},
        {"skills_score": None},
        {"missing": ["Опыт работы в отрасли"]},
        {"evidence": []},
        {"evidence": ["Подготовка управленческой отчётности"]},
        {"evidence": [EVIDENCE.lower()]},
        {"letter": LETTER.replace(TITLE, "специалиста")},
        {"letter": LETTER.replace(COMPANY, "другой компании")},
        {"letter": LETTER.replace(EVIDENCE, "Занимаюсь бюджетом и издержками")},
        {"letter": LETTER + " Пишите на candidate@example.test."},
        {"letter": LETTER + " Звоните на +7 (555) 123-45-67."},
        {"letter": LETTER + " Мой Telegram @example_candidate."},
    ],
    ids=[
        "score",
        "skills-score",
        "unverified-skills",
        "missing-requirement",
        "no-evidence",
        "unrelated-quote",
        "changed-case",
        "missing-title",
        "missing-company",
        "paraphrased-quote",
        "email",
        "phone",
        "social-handle",
    ],
)
def test_auto_letter_builder_rejects_unsupported_model_result(vacancy, changes):
    assert not grounded_auto_letter(ANALYSIS | changes, RESUME, vacancy, 80)


def test_skill_threshold_is_independent_of_general_match(vacancy):
    assert grounded_auto_letter(ANALYSIS, RESUME, vacancy, 95, skills_threshold=92)
    assert not grounded_auto_letter(ANALYSIS, RESUME, vacancy, 95, skills_threshold=93)
    old_result = {key: value for key, value in ANALYSIS.items() if key != "skills_score"}
    assert not grounded_auto_letter(old_result, RESUME, vacancy, 80, skills_threshold=0)


def test_auto_letter_rejects_edited_draft(vacancy):
    letter = grounded_auto_letter(ANALYSIS, RESUME, vacancy, 80)
    result = ANALYSIS | {"letter": letter}
    assert not auto_letter_supported(result, RESUME, vacancy, 80, letter + " Спасибо!")


def test_auto_letter_rejects_unsupported_claim_even_when_recorded_as_generated(vacancy):
    letter = grounded_auto_letter(ANALYSIS, RESUME, vacancy, 80)
    injected = letter + " Руководил внедрением SAP."
    result = ANALYSIS | {"letter": injected}
    assert not auto_letter_supported(result, RESUME, vacancy, 80, injected)


def test_new_groq_draft_is_the_only_automatic_submission(config, vacancy, monkeypatch):
    db = Database(config.data_dir)
    resume = RESUME + "\nЭлектронная почта: candidate@example.test"
    resume_hash = hashlib.sha256(resume.encode()).hexdigest()
    letter = grounded_auto_letter(ANALYSIS, RESUME, vacancy, 80)
    db.set_many(
        {
            "hh_selected_resume": {"id": RESUME_ID, "name": TITLE},
            "hh_session_verified": True,
            "resume": resume,
            "groq_consent": True,
        }
    )
    db.add("1100", vacancy)
    db.finish_analysis("1100", "approved", ANALYSIS | {"resume_hash": resume_hash}, LETTER)
    with db.connection() as connection:
        connection.execute("UPDATE vacancies SET found_at=1 WHERE id='1100'")
    db.add("1101", vacancy)
    db.finish_analysis(
        "1101",
        "approved",
        ANALYSIS
        | {"resume_hash": resume_hash, "auto_policy": AUTO_ANALYSIS_POLICY, "letter": letter},
        letter + " Спасибо!",
    )
    db.add(VACANCY_ID, vacancy)
    submissions = []
    day = local_day(config)
    groq_result = ANALYSIS | {"letter": LETTER + " Руководил внедрением SAP."}

    def groq_response(request):
        assert request.url.path == "/openai/v1/chat/completions"
        payload = json.loads(request.content)
        candidate = json.loads(payload["messages"][1]["content"])
        assert EVIDENCE in candidate["resume"]
        assert candidate["vacancy"]["company"] == COMPANY
        assert "duties" in payload["messages"][0]["content"]
        assert "skills_score" in payload["messages"][0]["content"]
        assert b"candidate@example.test" not in request.content
        assert b"hh.ru/vacancy/" not in request.content
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(groq_result)}}]}
        )

    page = object()

    @asynccontextmanager
    async def session():
        yield None, page

    async def verify_resume(actual_page, identifier):
        assert actual_page is page
        assert identifier == RESUME_ID

    async def apply(actual_page, identifier, resume_id, submitted_letter, reserve):
        assert actual_page is page
        assert (identifier, resume_id, submitted_letter) == (VACANCY_ID, RESUME_ID, letter)
        assert reserve()
        assert db.vacancy(identifier)["status"] == "sending"
        assert db.counts(day)[1] == 1
        submissions.append(identifier)
        return "sent"

    async def message(*_args, **_kwargs):
        pass

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(groq_response)) as client:
            scheduler = Scheduler(config, db, client)
            scheduler.browser.provider.state.write_text('{"cookies":[],"origins":[]}')
            bot = SimpleNamespace(send_message=message)
            monkeypatch.setattr(scheduler.browser.provider, "check_installation", lambda: None)
            monkeypatch.setattr(scheduler.browser.provider, "session", session)
            monkeypatch.setattr(hh_browser, "verify_resume", verify_resume)
            monkeypatch.setattr(hh_browser, "apply", apply)

            await scheduler.tick(bot)
            draft = db.vacancy(VACANCY_ID)
            result = json.loads(draft["result"])
            assert draft["status"] == "draft"
            assert result["auto_policy"] == AUTO_ANALYSIS_POLICY
            assert result["skills_score"] == 92
            assert result["duties"] == ANALYSIS["duties"]
            assert result["resume_hash"] == resume_hash
            assert draft["letter"] == result["letter"] == letter
            assert "SAP" not in draft["letter"]
            assert not submissions

            scheduler.browser.start_search(send=True)
            db.set("next_browser_search", 2**40)
            await scheduler.tick(bot)
            db.set("next_browser_operation", 0)
            await scheduler.tick(bot)

    asyncio.run(run())
    assert submissions == [VACANCY_ID]
    assert db.vacancy(VACANCY_ID)["status"] == "sent"
    assert db.vacancy("1100")["status"] == db.vacancy("1101")["status"] == "approved"


def test_application_diagnostics_counts_only_confirmations_and_explains_blocks(config, vacancy):
    db = Database(config.data_dir)
    day = local_day(config)
    flow = BrowserWorkflow(config, db)
    db.set_many(
        {
            "resume": RESUME,
            "groq_consent": True,
            "hh_selected_resume": {"id": RESUME_ID, "name": TITLE},
            "hh_session_verified": True,
            "browser_auto_enabled": True,
            "browser_search": True,
        }
    )
    flow.provider.state.write_text('{"cookies":[],"origins":[]}')
    digest = hashlib.sha256(RESUME.encode()).hexdigest()
    letter = grounded_auto_letter(ANALYSIS, RESUME, vacancy, 80)
    result = ANALYSIS | {
        "resume_hash": digest,
        "analysis_policy": AUTO_ANALYSIS_POLICY,
        "analysis_threshold": 80,
        "auto_policy": AUTO_ANALYSIS_POLICY,
        "letter": letter,
    }
    for identifier, status, data, result_change in (
        ("1", "draft", vacancy, {}),
        ("2", "draft", vacancy, {"skills_score": 72}),
        ("3", "draft", vacancy, {"missing": ["Опыт работы в отрасли"]}),
        ("4", "draft", vacancy | {"region": "Казань"}, {}),
        ("5", "draft", vacancy, {"resume_hash": "outdated"}),
        ("6", "draft", vacancy, {}),
        ("7", "draft", vacancy, {}),
    ):
        db.add(identifier, data)
        db.finish_analysis(identifier, status, result | result_change, letter)
    db.add("8", vacancy)
    assert db.transition("6", ("draft",), "sending", day=day, limit=100) == "ok"
    db.complete_browser_application("6", "sent")
    assert db.transition("7", ("draft",), "sending", day=day, limit=100) == "ok"
    db.complete_browser_application("7", "unknown")

    status = flow.application_status()
    assert status["eligible"] == 1
    assert status["pending"] == 1
    assert status["confirmed_today"] == status["confirmed_total"] == 1
    assert status["attempted_today"] == 2
    assert status["unknown"] == 1
    assert status["blocked"] == {
        "analysis_pending": 1,
        "unknown_outcome": 1,
        "stale_analysis": 1,
        "search_filters": 1,
        "missing_requirements": 1,
        "skills_below_threshold": 1,
    }
    assert {row["status"] for row in status["recent"]} == {"sent", "unknown"}


def test_lowered_overall_threshold_requeues_recent_card_while_auto_is_enabled(
    config, vacancy, monkeypatch
):
    db = Database(config.data_dir)
    flow = BrowserWorkflow(config, db)
    monkeypatch.setattr(flow, "require_browser", lambda: None)
    flow.provider.state.write_text('{"cookies":[],"origins":[]}')
    db.set_many(
        {
            "resume": RESUME,
            "groq_consent": True,
            "hh_selected_resume": {"id": RESUME_ID, "name": TITLE},
            "hh_session_verified": True,
            "settings": {"score": 96},
        }
    )
    digest = hashlib.sha256(RESUME.encode()).hexdigest()
    result = ANALYSIS | {
        "resume_hash": digest,
        "analysis_policy": AUTO_ANALYSIS_POLICY,
        "analysis_threshold": 96,
        "letter": "",
    }
    for identifier in ("1", "2"):
        db.add(identifier, vacancy)
    db.finish_analysis("1", "filtered", result, None)
    db.finish_analysis("2", "filtered", result | {"filter_reason": "Работодатель исключён."}, None)
    flow.start_search(send=True)
    assert db.vacancy("1")["status"] == "filtered"
    db.set_many({"settings": {"score": 80}, "next_browser_search": 2**40})
    asyncio.run(flow.tick(None, db.settings(config), local_day(config)))
    assert db.vacancy("1")["status"] == "queued"
    assert db.vacancy("2")["status"] == "filtered"


@pytest.mark.parametrize(
    ("source", "changes"),
    [("browser", {"missing": ["Опыт работы в отрасли"]}), ("mini_hh", {})],
    ids=["missing-requirement", "manually-added-vacancy"],
)
def test_scheduler_preserves_nonautomatic_choices_for_manual_review(
    config, vacancy, source, changes
):
    db = Database(config.data_dir)
    db.set_many({"resume": RESUME, "groq_consent": True, "browser_auto_enabled": True})
    db.add(VACANCY_ID, vacancy | {"source": source})
    analysis = ANALYSIS | changes

    def groq_response(_request):
        return httpx.Response(
            200, json={"choices": [{"message": {"content": json.dumps(analysis)}}]}
        )

    async def message(*_args, **_kwargs):
        pass

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(groq_response)) as client:
            await Scheduler(config, db, client).tick(SimpleNamespace(send_message=message))

    asyncio.run(run())
    row = db.vacancy(VACANCY_ID)
    result = json.loads(row["result"])
    assert row["status"] == "draft"
    assert row["letter"] == analysis["letter"]
    assert "auto_policy" not in result
    assert result["resume_hash"] == hashlib.sha256(RESUME.encode()).hexdigest()


def test_temporary_browser_errors_do_not_stop_discovery_or_auto_mode(config, monkeypatch):
    db = Database(config.data_dir)
    db.set_many({"browser_search": True, "browser_auto_enabled": True})
    messages = []

    async def send(*args, **kwargs):
        messages.append(args)

    async def failing_browser(*args):
        raise ServiceError("Chromium временно недоступен.", 120)

    async def run():
        async with httpx.AsyncClient() as client:
            scheduler = Scheduler(config, db, client)
            monkeypatch.setattr(scheduler.browser, "tick", failing_browser)
            for i in range(4):
                await scheduler.tick_browser(SimpleNamespace(send_message=send))
                assert db.get("browser_failures") == i + 1

    asyncio.run(run())
    assert db.get("browser_search") is True
    assert db.get("browser_auto_enabled") is True
    assert len(messages) == 1


def test_groq_worker_finishes_while_browser_waits(config, vacancy, monkeypatch):
    db = Database(config.data_dir)
    db.set_many({"resume": RESUME, "groq_consent": True, "browser_search": True})
    db.add(VACANCY_ID, vacancy | {"source": "mini_hh"})
    monkeypatch.setattr("app.scheduler.resource_warning", lambda _directory: None)

    async def run():
        browser_started = asyncio.Event()
        browser_waiting = asyncio.Event()
        analyzed = asyncio.Event()

        async def browser(*args):
            browser_started.set()
            await browser_waiting.wait()

        async def send(*args, **kwargs):
            analyzed.set()

        def response(_request):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(ANALYSIS)}}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            scheduler = Scheduler(config, db, client)
            monkeypatch.setattr(scheduler.browser, "tick", browser)
            await scheduler.start(SimpleNamespace(send_message=send))
            try:
                await asyncio.wait_for(browser_started.wait(), timeout=1)
                await asyncio.wait_for(analyzed.wait(), timeout=1)
            finally:
                await scheduler.shutdown()

    asyncio.run(run())
    assert db.vacancy(VACANCY_ID)["status"] == "draft"


def test_restart_refreshes_old_browser_drafts_with_search_running(config, vacancy):
    db = Database(config.data_dir)
    db.set_many({"resume": RESUME, "groq_consent": True, "browser_search": True})
    db.add(VACANCY_ID, vacancy)
    old_result = {
        key: value for key, value in ANALYSIS.items() if key not in {"skills_score", "duties"}
    }
    old_result.update(analysis_policy="hh-evidence-v2", auto_policy="hh-evidence-v2")
    db.finish_analysis(VACANCY_ID, "draft", old_result, old_result["letter"])

    async def run():
        async with httpx.AsyncClient() as client:
            scheduler = Scheduler(config, db, client)
            await scheduler.start(None)
            await scheduler.shutdown()

    asyncio.run(run())
    assert db.vacancy(VACANCY_ID)["status"] == "queued"
    assert not db.get("browser_auto_enabled")


@pytest.mark.parametrize(
    ("status", "source", "letter", "expected"),
    [
        ("draft", "browser", LETTER, "queued"),
        ("approved", "browser", LETTER, "queued"),
        ("approved", "browser", LETTER + " Спасибо!", "approved"),
        ("sent", "browser", LETTER, "sent"),
        ("draft", "mini_hh", LETTER, "draft"),
    ],
)
def test_refresh_requeues_recent_unchanged_browser_drafts(
    tmp_path, vacancy, status, source, letter, expected
):
    db = Database(tmp_path)
    db.add(VACANCY_ID, vacancy | {"source": source})
    db.finish_analysis(VACANCY_ID, status, ANALYSIS, letter)
    db.refresh_auto_drafts(AUTO_ANALYSIS_POLICY, hashlib.sha256(RESUME.encode()).hexdigest())
    row = db.vacancy(VACANCY_ID)
    assert row["status"] == expected
    assert row["letter"] == letter
