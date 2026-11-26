import asyncio
import shutil
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from app import hh_browser
from app.database import Database

RESUME = "1a2b3c4d5e6f7a8b"
VACANCY = "1200"


def test_persistent_attempt_recovery_and_reconciliation(tmp_path):
    db = Database(tmp_path)
    db.add(VACANCY, {"title": "Пример", "source": "browser"})
    db.finish_analysis(VACANCY, "approved", {}, "Письмо")
    assert db.next_browser_application()["id"] == VACANCY
    day = "2026-11-26"
    assert db.transition(VACANCY, ("approved",), "sending", day=day, limit=1) == "ok"
    restarted = Database(tmp_path)
    restarted.recover_browser_applications()
    assert restarted.vacancy(VACANCY)["status"] == "unknown"
    assert restarted.next_browser_application() is None
    assert restarted.counts(day)[1] == 1
    restarted.complete_browser_application(VACANCY, "sent", previous="unknown")
    assert restarted.vacancy(VACANCY)["status"] == "sent"
    assert restarted.counts(day)[1] == 1


@pytest.mark.parametrize("scenario", ["sent", "unknown", "questions", "captcha", "limit"])
def test_submit_requires_letter_resume_and_confirmation(scenario):
    executable = shutil.which("chromium") or shutil.which("google-chrome")
    if not executable:
        pytest.skip("Chromium для проверки HTML-формы не установлен.")
    submissions = []
    reserved = []
    letter = "Здравствуйте! Готов обсудить бюджетирование."

    async def respond(route):
        path = route.request.url.split("hh.ru", 1)[1].split("?", 1)[0]
        if path == f"/vacancy/{VACANCY}":
            if submissions and scenario == "sent":
                html = '<a data-qa="vacancy-response-link-view-response">Мой отклик</a>'
            elif scenario == "captcha":
                html = '<form action="/captcha"><input name="captcha"></form>'
            else:
                html = (
                    f'<a data-qa="vacancy-response-link-top" '
                    f'href="/applicant/vacancy_response?vacancyId={VACANCY}">Откликнуться</a>'
                )
        elif path == "/applicant/vacancy_response":
            html = f"""<form data-qa="vacancy-response-popup-form" action="/fixture-submit">
              <input type="radio" name="resumeId" value="{RESUME}">
              <textarea data-qa="vacancy-response-popup-form-letter-input" name="letter"></textarea>
              {'<textarea data-qa="employer-question"></textarea>' if scenario == "questions" else ""}
              <button data-qa="vacancy-response-popup-submit-button">Отправить</button>
            </form>"""
        elif path == "/fixture-submit":
            submissions.append(route.request.url)
            html = "<p>Отклик отправлен</p>"
        else:
            html = ""
        await route.fulfill(status=200, body=html, content_type="text/html; charset=utf-8")

    def reserve():
        reserved.append(True)
        return scenario != "limit"

    async def run():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=executable)
            page = await browser.new_page()
            await page.route("**/*", respond)
            if scenario in {"questions", "captcha"}:
                with pytest.raises(
                    hh_browser.HHQuestions if scenario == "questions" else hh_browser.HHAttention
                ):
                    await hh_browser.apply(page, VACANCY, RESUME, letter, reserve)
            else:
                assert await hh_browser.apply(page, VACANCY, RESUME, letter, reserve) == scenario
            await browser.close()

    asyncio.run(run())
    assert bool(reserved) == (scenario in {"sent", "unknown", "limit"})
    assert bool(submissions) == (scenario in {"sent", "unknown"})
    if submissions:
        from urllib.parse import parse_qs, urlsplit

        assert parse_qs(urlsplit(submissions[0]).query) == {
            "resumeId": [RESUME],
            "letter": [letter],
        }


def test_browser_worker_sends_once_and_pauses_on_unknown(tmp_path, monkeypatch):
    import hashlib
    from contextlib import asynccontextmanager
    from zoneinfo import ZoneInfo

    from app.browser_workflow import BrowserWorkflow
    from app.config import Config
    from app.vacancies import parse_vacancy

    monkeypatch.setenv("TELEGRAM_TOKEN", "12345:synthetic")
    monkeypatch.setenv("OWNER_ID", "12345")
    monkeypatch.setenv("GROQ_API_KEY", "fake")
    monkeypatch.setenv("BROWSER_ENGINE", "chromium")
    monkeypatch.setenv("CHROMIUM_PATH", "/bin/sh")
    monkeypatch.setenv("PERSISTENT_STORAGE", "true")
    monkeypatch.setenv("AUTO_SEND", "true")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    monkeypatch.setenv("TIMEZONE", ZoneInfo("Europe/Moscow").key)
    monkeypatch.setattr("app.browser_workflow.resource_warning", lambda _: None)
    config = Config.load()
    db = Database(tmp_path)
    resume_text = "Подготовка управленческой отчётности и бюджетирование."
    db.set_many(
        {
            "hh_selected_resume": {"id": RESUME, "name": "Финансовый менеджер"},
            "resume": resume_text,
            "groq_consent": True,
        }
    )
    _, details = parse_vacancy(
        f"https://hh.ru/vacancy/{VACANCY}\nДолжность: Финансовый менеджер\n"
        "Компания: Пример\nРегион: Москва\nЗарплата: 150000 RUB\n"
        "Описание: Управление бюджетом, отчётность и финансовые прогнозы на квартал."
    )
    db.add(VACANCY, details | {"source": "browser"})
    db.finish_analysis(
        VACANCY,
        "approved",
        {
            "score": 95,
            "resume_hash": hashlib.sha256(resume_text.encode()).hexdigest(),
        },
        "Письмо",
    )
    messages = []
    sends = []

    @asynccontextmanager
    async def session():
        yield "session", None

    async def resumes(page):
        return {RESUME: "Финансовый менеджер"}

    async def apply(page, vacancy_id, resume_id, letter, reserve):
        assert resume_id == RESUME
        assert reserve()
        sends.append(vacancy_id)
        return "unknown"

    async def message(owner, text):
        messages.append(text)

    async def run():
        flow = BrowserWorkflow(config, db)
        monkeypatch.setattr(flow.provider, "session", session)
        monkeypatch.setattr(hh_browser, "authorized_resumes", resumes)
        monkeypatch.setattr(hh_browser, "apply", apply)
        flow.start_search(send=True)
        await flow.tick(SimpleNamespace(send_message=message), config.defaults, "2026-11-26")
        await flow.tick(SimpleNamespace(send_message=message), config.defaults, "2026-11-26")

    asyncio.run(run())
    assert sends == [VACANCY]
    assert db.vacancy(VACANCY)["status"] == "unknown"
    assert not db.get("browser_auto_enabled")
    assert len(messages) == 1
