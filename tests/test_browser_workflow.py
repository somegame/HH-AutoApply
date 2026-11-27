import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright

from app import hh_browser
from app.browser_workflow import BrowserWorkflow
from app.database import Database
from app.services import ServiceError
from app.vacancies import search_links

RESUME = "1a2b3c4d5e6f7a8b"
VACANCY = "1200"
SEARCH_SETTINGS = {
    "query": "Финансовый менеджер",
    "region": "Москва",
    "salary": 150_000,
    "currency": "RUB",
    "interval": 5,
    "use_related": False,
    "unknown_salary": False,
}


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


@pytest.mark.parametrize(
    "scenario", ["sent", "unknown", "questions", "captcha", "limit", "inline", "wrong_id"]
)
def test_submit_requires_letter_resume_and_confirmation(scenario):
    executable = shutil.which("chromium") or shutil.which("google-chrome")
    if not executable:
        pytest.skip("Chromium для проверки HTML-формы не установлен.")
    submissions = []
    reserved = []
    letter = "Здравствуйте! Готов обсудить бюджетирование."

    def fixture_form(vacancy_field=""):
        return f"""<form data-qa="vacancy-response-popup-form" action="/fixture-submit">
          {vacancy_field}
          <input type="radio" name="resumeId" value="{RESUME}">
          <textarea data-qa="vacancy-response-popup-form-letter-input" name="letter"></textarea>
          {'<textarea data-qa="employer-question"></textarea>' if scenario == "questions" else ""}
          <button data-qa="vacancy-response-popup-submit-button">Отправить</button>
        </form>"""

    async def respond(route):
        path = route.request.url.split("hh.ru", 1)[1].split("?", 1)[0]
        if path == f"/vacancy/{VACANCY}":
            if submissions and scenario in {"sent", "inline"}:
                html = '<a data-qa="vacancy-response-link-view-response">Мой отклик</a>'
            elif scenario == "captcha":
                html = '<form action="/captcha"><input name="captcha"></form>'
            elif scenario in {"inline", "wrong_id"}:
                identifier = VACANCY if scenario == "inline" else "990"
                html = fixture_form(f'<input type="hidden" name="vacancyId" value="{identifier}">')
            else:
                html = (
                    f'<a data-qa="vacancy-response-link-top" '
                    f'href="/applicant/vacancy_response?vacancyId={VACANCY}">Откликнуться</a>'
                )
        elif path == "/applicant/vacancy_response":
            html = fixture_form()
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
            if scenario in {"questions", "captcha", "wrong_id"}:
                with pytest.raises(
                    hh_browser.HHAttention if scenario == "captcha" else hh_browser.HHQuestions
                ):
                    await hh_browser.apply(page, VACANCY, RESUME, letter, reserve)
            else:
                expected = "sent" if scenario == "inline" else scenario
                assert await hh_browser.apply(page, VACANCY, RESUME, letter, reserve) == expected
            await browser.close()

    asyncio.run(run())
    assert bool(reserved) == (scenario in {"sent", "unknown", "limit", "inline"})
    assert bool(submissions) == (scenario in {"sent", "unknown", "inline"})
    if submissions:
        expected_fields = {
            "resumeId": [RESUME],
            "letter": [letter],
        }
        if scenario == "inline":
            expected_fields["vacancyId"] = [VACANCY]
        assert parse_qs(urlsplit(submissions[0]).query) == expected_fields


def test_background_pagination_takes_all_ids_and_only_valid_next_links():
    executable = shutil.which("chromium") or shutil.which("google-chrome")
    if not executable:
        pytest.skip("Chromium для проверки страниц поиска не установлен.")

    async def respond(route):
        title = parse_qs(urlsplit(route.request.url).query)["text"][0]
        href = {
            "valid": "/search/vacancy?text=valid&page=1",
            "external": "https://example.com/search/vacancy?text=external&page=1",
            "bad_query": "/search/vacancy?text=other&page=1",
            "backward": "/search/vacancy?text=backward&page=0",
        }[title]
        links = "".join(
            f'<a data-qa="serp-item__title" href="/vacancy/{i}">Вакансия</a>'
            for i in range(100, 135)
        )
        next_link = f'<a data-qa="pager-next" href="{href}">Дальше</a>'
        await route.fulfill(body=links + next_link, content_type="text/html; charset=utf-8")

    async def run():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=executable)
            page = await browser.new_page()
            await page.route("**/*", respond)
            for title in ("valid", "external", "bad_query", "backward"):
                url = f"https://hh.ru/search/vacancy?text={title}"
                identifiers, following = await hh_browser.search_page(page, url)
                assert identifiers == [str(i) for i in range(100, 135)]
                assert following == (url + "&page=1" if title == "valid" else None)
            await browser.close()

    asyncio.run(run())


def test_discovery_checkpoints_pages_across_restart_and_picks_up_new_vacancies(
    tmp_path, monkeypatch
):
    db = Database(tmp_path)
    config = SimpleNamespace(data_dir=tmp_path)
    settings = SEARCH_SETTINGS
    first = next(search_links(settings, title_only=True))[1]
    older = first + "&page=1"
    pages = []
    viewed = []

    async def search_page(page, url):
        pages.append(url)
        if pages == [first]:
            return ["100", "101", "102", "103", "104"], older
        if url == older:
            return ["104", "107", "105"], None
        return ["106", "100"], None

    async def read(page, identifier):
        viewed.append(identifier)
        if identifier == "107":
            raise hh_browser.HHVacancyUnavailable("HH не показал основные поля этой вакансии.")
        return {"source": "browser", "title": "Бухгалтер"}

    async def duplicate(page):
        return viewed[-1] == "104"

    monkeypatch.setattr(hh_browser, "search_page", search_page)
    monkeypatch.setattr(hh_browser, "read_vacancy", read)
    monkeypatch.setattr(hh_browser, "already_applied", duplicate)

    async def run():
        before = BrowserWorkflow(config, db)
        assert await before.discover(None, settings, 100) == (3, 0)
        after = BrowserWorkflow(config, Database(tmp_path))
        assert await after.discover(None, settings, 160) == (1, 0)
        assert await after.discover(None, settings, 220) == (1, 400)
        assert await after.discover(None, settings, 280) == (0, 400)
        assert await after.discover(None, settings, 400) == (1, 700)

    asyncio.run(run())
    assert pages == [first, older, first]
    assert viewed == ["100", "101", "102", "103", "104", "107", "105", "106"]
    assert db.vacancy("107") is None
    assert db.vacancy("104")["status"] == "duplicate"
    assert json.loads(db.vacancy("106")["details"])["auto_discovered"] is True


def test_slow_card_retries_without_blocking_other_cards_in_page(tmp_path, monkeypatch):
    db = Database(tmp_path)
    config = SimpleNamespace(data_dir=tmp_path)
    viewed = []

    async def search_page(page, url):
        return ["100", "101", "102"], None

    async def read(page, identifier):
        viewed.append(identifier)
        if identifier == "100":
            raise ServiceError("Вакансия временно недоступна.", 120)
        return {"source": "browser", "title": "Бухгалтер"}

    async def duplicate(page):
        return False

    monkeypatch.setattr(hh_browser, "search_page", search_page)
    monkeypatch.setattr(hh_browser, "read_vacancy", read)
    monkeypatch.setattr(hh_browser, "already_applied", duplicate)

    async def run():
        flow = BrowserWorkflow(config, db)
        with pytest.raises(ServiceError):
            await flow.discover(None, SEARCH_SETTINGS, 100)
        assert db.get("browser_scan")["pending"] == ["101", "102", "100"]
        restarted = BrowserWorkflow(config, Database(tmp_path))
        for now in (220, 340):
            with pytest.raises(ServiceError):
                await restarted.discover(None, SEARCH_SETTINGS, now)
        assert db.get("browser_scan")["pending"] == []
        assert await restarted.discover(None, SEARCH_SETTINGS, 350) == (0, 400)

    asyncio.run(run())
    assert viewed == ["100", "101", "102", "100", "100"]
    assert db.vacancy("101")["status"] == db.vacancy("102")["status"] == "queued"


def test_browser_alternates_sending_with_due_discovery(tmp_path, monkeypatch):
    config = SimpleNamespace(
        data_dir=tmp_path, browser_engine="chromium", auto_send=True, persistent=True
    )
    db = Database(tmp_path)
    db.set_many({"browser_search": True, "browser_auto_enabled": True})
    settings = {"score": 80, "skills_score": 85, "limit": 20}
    actions = []
    flow = BrowserWorkflow(config, db)

    @asynccontextmanager
    async def session():
        yield None, None

    def next_application(**query):
        assert query["min_skills"] == 85
        yield {"id": VACANCY}

    async def verify(page, identifier):
        assert identifier == RESUME

    async def send(*args):
        actions.append("send")

    async def discover(*args):
        actions.append("discover")
        return 0, 0

    monkeypatch.setattr(db, "browser_candidates", next_application)
    monkeypatch.setattr(flow, "application_block", lambda *_: (None, ""))
    monkeypatch.setattr(flow.provider, "session", session)
    monkeypatch.setattr(flow, "readiness", lambda: {"id": RESUME})
    monkeypatch.setattr(flow, "send_one", send)
    monkeypatch.setattr(flow, "discover", discover)
    monkeypatch.setattr(hh_browser, "verify_resume", verify)

    async def run():
        for _ in range(4):
            db.set("next_browser_operation", 0)
            await flow.tick(None, settings, "2026-11-26")

    asyncio.run(run())
    assert actions == ["send", "discover", "send", "discover"]


@pytest.mark.parametrize("raise_threshold", [False, True])
def test_browser_worker_sends_once_and_pauses_on_unknown(tmp_path, monkeypatch, raise_threshold):
    import hashlib
    from zoneinfo import ZoneInfo

    from app.config import Config
    from app.services import AUTO_ANALYSIS_POLICY, grounded_auto_letter
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
    evidence = "Подготовка управленческой отчётности и бюджетирование"
    resume_text = f"{evidence}."
    db.set_many(
        {
            "hh_selected_resume": {"id": RESUME, "name": "Финансовый менеджер"},
            "hh_session_verified": True,
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
    result = {
        "score": 95,
        "skills_score": 95,
        "duties": ["Управлять бюджетом и готовить финансовые прогнозы."],
        "reason": "Есть подтверждённый опыт.",
        "required": ["Бюджетирование"],
        "missing": [],
        "letter": f"Рассматриваю вакансию «Финансовый менеджер» в компании «Пример». {evidence}.",
        "evidence": [evidence],
        "analysis_policy": AUTO_ANALYSIS_POLICY,
        "auto_policy": AUTO_ANALYSIS_POLICY,
        "resume_hash": hashlib.sha256(resume_text.encode()).hexdigest(),
    }
    letter = grounded_auto_letter(result, resume_text, details, config.defaults["score"])
    db.finish_analysis(VACANCY, "approved", result | {"letter": letter}, letter)
    messages = []
    sends = []

    @asynccontextmanager
    async def session():
        yield "session", None

    async def verify(page, identifier):
        assert identifier == RESUME

    async def apply(page, vacancy_id, resume_id, application_letter, reserve):
        assert resume_id == RESUME
        assert application_letter == letter
        if raise_threshold:
            db.set("settings", {"skills_score": 96})
            assert not reserve()
            return "limit"
        assert reserve()
        sends.append(vacancy_id)
        return "unknown"

    async def message(owner, text):
        messages.append(text)

    async def run():
        flow = BrowserWorkflow(config, db)
        flow.provider.state.write_text('{"cookies":[],"origins":[]}')
        monkeypatch.setattr(flow.provider, "session", session)
        monkeypatch.setattr(hh_browser, "verify_resume", verify)
        monkeypatch.setattr(hh_browser, "apply", apply)
        flow.start_search(send=True)
        await flow.tick(SimpleNamespace(send_message=message), config.defaults, "2026-11-26")
        await flow.tick(SimpleNamespace(send_message=message), config.defaults, "2026-11-26")

    asyncio.run(run())
    if raise_threshold:
        assert sends == messages == []
        assert db.vacancy(VACANCY)["status"] == "approved"
        assert db.get("browser_auto_enabled")
    else:
        assert sends == [VACANCY]
        assert db.vacancy(VACANCY)["status"] == "unknown"
        assert not db.get("browser_auto_enabled")
        assert len(messages) == 1
