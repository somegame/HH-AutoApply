import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest

from app import hh_browser as hh
from app.browser_workflow import BrowserWorkflow
from app.database import Database
from app.local_browser import LocalBrowser

OWN = "abcdef0123456789"
PUBLIC = "012345abcdef0123"
EXPERIENCE = "Бюджетирование, создание отчётности, планирование движения денежных средств."
PAGE = (
    "<h1>Финансовый менеджер</h1>{edit}"
    "<section><h2>Опыт работы</h2><p>" + EXPERIENCE + "</p></section>"
    "<section><h2>Контакты</h2><p>private@example.test</p></section>"
)


@pytest.fixture
def browser_config(tmp_path):
    executable = (
        shutil.which("chromium-headless-shell")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
    )
    if not executable:
        pytest.skip("Chromium нужен для проверки загрузки резюме.")
    return SimpleNamespace(
        data_dir=tmp_path,
        browser_engine="chromium",
        chromium_path=executable,
        login_public_url="https://bot.example.test",
        web_port=0,
    )


def test_resume_link_is_strictly_scoped_to_hh():
    assert hh.resume_identifier(f"https://hh.ru/resume/{OWN}?hhtmFrom=resume_list") == OWN
    for link in (
        f"https://hh.ru.evil.test/resume/{OWN}",
        f"https://hh.ru@evil.test/resume/{OWN}",
        f"https://hh.ru:2443/resume/{OWN}",
        f"http://hh.ru/resume/{OWN}",
        "https://hh.ru/applicant/resumes/",
    ):
        with pytest.raises(hh.HHResumeUnavailable):
            hh.resume_identifier(link)


def test_official_resume_route_accepts_a_private_profile_redirect(browser_config, monkeypatch):
    async def run():
        async with LocalBrowser(browser_config).session(15, guest=True) as (_, page):

            async def serve(route):
                assert urlsplit(route.request.url).path == "/applicant/profile/"
                await route.fulfill(
                    body=f'<a href="/resume/{OWN}">Финансовый менеджер</a>',
                    content_type="text/html; charset=utf-8",
                )

            await page.route("**/*", serve)
            navigate = hh.open_page

            async def redirected(current, url):
                assert url == "https://hh.ru/applicant/resumes/"
                await navigate(current, "https://hh.ru/applicant/profile/")

            monkeypatch.setattr(hh, "open_page", redirected)
            assert await hh.authorized_resumes(page) == {OWN: "Финансовый менеджер"}

    asyncio.run(run())


def test_resume_paste_and_navigation_import_only_the_owners_card(browser_config, monkeypatch):
    async def run():
        db = Database(browser_config.data_dir)
        workflow = BrowserWorkflow(browser_config, db)
        db.set_many({"hh_session_verified": True, "browser_search": True})
        workflow.provider.state.write_text(
            json.dumps({"cookies": [], "origins": []}), encoding="utf-8"
        )
        monkeypatch.setattr("app.browser_workflow.resource_warning", lambda _: None)
        launch = workflow.provider.session
        paths = []

        async def serve(route):
            path = urlsplit(route.request.url).path
            paths.append(path)
            if path == "/":
                html = "<h1>Личный кабинет</h1>"
            elif path == f"/resume/{OWN}":
                html = PAGE.format(edit="<button>Редактировать</button>")
            elif path == f"/resume/{PUBLIC}":
                html = PAGE.format(edit="")
            else:
                pytest.fail(f"Unexpected path: {path}")
            await route.fulfill(body=html, content_type="text/html; charset=utf-8")

        @asynccontextmanager
        async def session(*args, **kwargs):
            async with launch(*args, **kwargs) as (context, page):
                await context.route("**/*", serve)
                yield context, page

        monkeypatch.setattr(workflow.provider, "session", session)
        try:
            with pytest.raises(hh.HHResumeUnavailable, match="принадлежит"):
                await workflow.refresh_resumes(f"https://hh.ru/resume/{PUBLIC}")
            assert workflow.account_connected and not db.get("hh_selected_resume")
            choices = await workflow.refresh_resumes(f"https://hh.ru/resume/{OWN}?share=true")
            assert choices == {OWN: "Финансовый менеджер"}
            assert EXPERIENCE in db.get("resume")
            assert "private@example.test" not in db.get("resume")
            db.set_many({"groq_consent": True, "browser_auto_enabled": True})
            link = await workflow.start_login(in_mini=True, resume_mode=True)
            assert parse_qs(urlsplit(link).query) == {"mode": ["resume"]}
            await asyncio.sleep(1)
            assert not workflow.login_task.done()
            await workflow.view.page.goto(f"https://hh.ru/resume/{OWN}")
            assert await asyncio.wait_for(workflow.login_task, 10) == choices
            assert paths == [f"/resume/{PUBLIC}", f"/resume/{OWN}", "/", f"/resume/{OWN}"]
            assert db.get("browser_search") and db.get("browser_auto_enabled")
            assert db.get("groq_consent")
        finally:
            await workflow.cancel_login()
            await workflow.view.shutdown()

    asyncio.run(run())
