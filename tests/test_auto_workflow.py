import asyncio
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import httpx
import pytest

from app import hh_browser
from app.config import Config
from app.database import Database
from app.scheduler import Scheduler, local_day
from app.services import AUTO_ANALYSIS_POLICY, auto_letter_supported, grounded_auto_letter
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
