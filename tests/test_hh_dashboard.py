import asyncio
import json
import shutil
from contextlib import asynccontextmanager
from types import SimpleNamespace
from urllib.parse import urlsplit

import pytest

from app import hh_browser as hh
from app.browser_workflow import BrowserWorkflow
from app.database import Database
from app.local_browser import LocalBrowser
from app.services import ServiceError

DASHBOARD = """
<main>
  <h1>Поиск работы в Москве</h1>
  <section data-qa="applicant-dashboard" aria-label="Кабинет соискателя">
    <a href="/applicant/resumes"><div>Просмотры</div><strong>172</strong></a>
    <a href="/applicant/negotiations"><div>Отклики</div><strong>190</strong></a>
    <a href="/applicant/autosearch"><div>Автопоиски</div><strong>0</strong></a>
  </section>
  <a href="/applicant/resumes">Мои резюме</a>
</main>
"""


@pytest.fixture
def hh_config(tmp_path):
    executable = (
        shutil.which("chromium-headless-shell")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
    )
    if not executable:
        pytest.skip("Chromium нужен для проверки личного кабинета HH.")
    return SimpleNamespace(
        browser_engine="chromium",
        chromium_path=executable,
        data_dir=tmp_path,
        login_public_url="https://bot.example.test",
        web_port=0,
    )


def test_dashboard_proof_excludes_partial_public_and_login_pages(hh_config):
    async def run():
        async with LocalBrowser(hh_config).session(interactive=True, guest=True) as (_, page):
            await page.route("**/*", lambda route: route.fulfill(body="<title>Тест HH</title>"))
            await page.goto("https://hh.ru/")
            await page.set_content(DASHBOARD)
            assert await hh.dashboard_authenticated(page)

            await page.set_content(DASHBOARD.replace("Автопоиски", "Мои вакансии"))
            assert not await hh.dashboard_authenticated(page)

            await page.set_content(DASHBOARD.replace("<strong>190</strong>", "<strong>—</strong>"))
            assert not await hh.dashboard_authenticated(page)

            await page.set_content(
                DASHBOARD + '<form><input data-qa="login-input-username" type="email"></form>'
            )
            assert not await hh.dashboard_authenticated(page)

            for url in ("https://hh.ru/vacancy/123", "https://hh.example/"):
                await page.goto(url)
                await page.set_content(DASHBOARD)
                assert not await hh.dashboard_authenticated(page)

    asyncio.run(run())


def test_dashboard_login_saves_hh_session_without_loading_resume_list(hh_config, monkeypatch):
    async def run():
        db = Database(hh_config.data_dir)
        db.set("hh_needs_login", True)
        workflow = BrowserWorkflow(hh_config, db)
        monkeypatch.setattr("app.browser_workflow.resource_warning", lambda _: None)
        provider_session = workflow.provider.session
        resume_visits = []

        async def serve(route):
            path = urlsplit(route.request.url).path
            headers = await route.request.all_headers()
            if path == "/applicant/resumes":
                resume_visits.append(path)
                return await route.fulfill(
                    status=503, body="<h1>Service unavailable</h1>", content_type="text/html"
                )
            if path == "/" and "test_session=verified" in headers.get("cookie", ""):
                html = DASHBOARD
            else:
                html = '<input data-qa="login-input-username" type="email">'
            return await route.fulfill(body=html, content_type="text/html; charset=utf-8")

        @asynccontextmanager
        async def session(*args, **kwargs):
            async with provider_session(*args, **kwargs) as (context, page):
                await context.route("**/*", serve)
                yield context, page

        monkeypatch.setattr(workflow.provider, "session", session)
        try:
            await workflow.start_login(in_mini=True)
            page = workflow.view.page
            await page.context.add_cookies(
                [
                    {"name": "test_session", "value": "verified", "domain": ".hh.ru", "path": "/"},
                    {"name": "other", "value": "discard", "domain": "example.test", "path": "/"},
                ]
            )
            await page.goto("https://hh.ru/")
            assert await asyncio.wait_for(workflow.login_task, 8) == {}
            assert workflow.view.status == "done"
            assert db.get("hh_session_verified") is True
            assert db.get("hh_needs_login") is False
            assert not db.get("hh_selected_resume")
            assert not resume_visits
            state = json.loads(workflow.provider.state.read_text())
            assert {cookie["name"] for cookie in state["cookies"]} == {"test_session"}

            restarted = BrowserWorkflow(hh_config, Database(hh_config.data_dir))
            assert restarted.db.get("hh_session_verified") is True
            async with restarted.provider.session() as (_, restored):
                await restored.route("**/*", serve)
                await restored.goto("https://hh.ru/")
                assert await hh.dashboard_authenticated(restored)

            with pytest.raises(hh.HHAccessBarrier, match="503"):
                await workflow.refresh_resumes()
            assert resume_visits == ["/applicant/resumes"]
            assert workflow.account_connected
            assert db.get("hh_needs_login") is False
            assert "503" in db.get("hh_resumes_status")
            assert json.loads(workflow.provider.state.read_text()) == state
        finally:
            await workflow.cancel_login()
            await workflow.view.shutdown()

    asyncio.run(run())


def test_verified_hh_account_can_start_search_without_resume_or_send_consent(tmp_path, monkeypatch):
    db = Database(tmp_path)
    workflow = BrowserWorkflow(SimpleNamespace(data_dir=tmp_path, browser_engine="chromium"), db)
    monkeypatch.setattr(workflow, "require_browser", lambda: None)
    workflow.provider.state.write_text('{"cookies":[],"origins":[]}')
    db.set("hh_session_verified", True)

    workflow.start_search()

    assert db.get("browser_search") is True
    assert db.get("browser_auto_enabled") is False
    with pytest.raises(ServiceError, match="резюме"):
        workflow.start_search(send=True)
