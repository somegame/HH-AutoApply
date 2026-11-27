import asyncio
import hashlib
import hmac
import json
import shutil
import time
from contextlib import asynccontextmanager
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, parse_qsl, urlencode, urlsplit

import httpx
import pytest
from playwright.async_api import async_playwright

from app import hh_browser
from app.browser_workflow import BrowserWorkflow
from app.config import Config
from app.database import Database
from app.mini_app import MiniApp, verify_init_data
from app.scheduler import Scheduler
from app.services import ServiceError
from app.vacancies import URL_FIELDS

TOKEN = "12345:synthetic"
OWNER = 43210
PUBLIC_URL = "https://bot.example.test"
JOB = "28ff4d08-c88d-11f1-999a-2f7506c6dfb2"
JOB_ID = f"trudvsem:{JOB}"


def signed(now, *, owner=OWNER):
    data = {"auth_date": str(now), "user": json.dumps({"id": owner}), "query_id": "test-query"}
    key = hmac.digest(b"WebAppData", TOKEN.encode(), "sha256")
    check = "\n".join(f"{k}={v}" for k, v in sorted(data.items())).encode()
    data["hash"] = hmac.new(key, check, hashlib.sha256).hexdigest()
    return urlencode(data)


def test_mini_signature_rejects_old_forged_repeated_and_foreign_data():
    now = 100000
    good = signed(now)
    assert verify_init_data(good, TOKEN, OWNER, now=now)
    for data in (
        signed(now, owner=42),
        signed(now - 3601),
        signed(now + 31),
        good.replace("test-query", "changed"),
        good + "&query_id=duplicate",
    ):
        assert not verify_init_data(data, TOKEN, OWNER, now=now)


@pytest.fixture
def mini_config(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.load_dotenv", lambda **_: None)
    for key, value in {
        "TELEGRAM_TOKEN": TOKEN,
        "OWNER_ID": str(OWNER),
        "DATA_DIR": str(tmp_path),
        "BROWSER_ENGINE": "manual",
        "LOGIN_PUBLIC_URL": PUBLIC_URL,
        "PORT": "8000",
    }.items():
        monkeypatch.setenv(key, value)
    return replace(Config.load(), web_port=0, superjob_key="fake")


def test_stop_cancels_confirmed_send_before_reservation(mini_config, monkeypatch):
    config = replace(mini_config, auto_send=True, persistent=True, groq_key="fake")
    db = Database(config.data_dir)
    workflow = BrowserWorkflow(config, db)
    selected = {"id": "123456789abc", "name": "Резюме"}
    resume = "Финансовое планирование"
    db.set_many({"hh_selected_resume": selected, "resume": resume, "groq_consent": True})
    job = dict.fromkeys(URL_FIELDS, "")
    job.update(source="mini_hh", region="Москва", salary="от 150 000 ₽")
    job.update(currency="RUB", salary_max=150000)
    db.add("123456", job)
    db.finish_analysis(
        "123456",
        "draft",
        {"score": 100, "resume_hash": hashlib.sha256(resume.encode()).hexdigest()},
        "Сопроводительное письмо",
    )
    monkeypatch.setattr(workflow, "require_browser", lambda: None)
    monkeypatch.setattr(hh_browser, "verify_resume", AsyncMock())

    @asynccontextmanager
    async def session(_):
        yield None, None

    async def apply(_page, _identifier, _resume, _letter, reserve):
        workflow.pause("Остановлено владельцем.")
        assert not reserve()
        return "limit"

    monkeypatch.setattr(workflow.provider, "session", session)
    monkeypatch.setattr(hh_browser, "apply", apply)
    with pytest.raises(ServiceError, match="Отправка отменена"):
        asyncio.run(workflow.submit_selected(None, db.vacancy("123456"), "2026-11-26"))
    row = db.vacancy("123456")
    assert row["status"] == "draft" and row["sent_day"] is None


def source_response(req):
    if req.url.host == "opendata.trudvsem.ru":
        assert req.url.path.endswith("/region/77")
        return httpx.Response(
            200,
            json={
                "results": {
                    "vacancies": [
                        {
                            "vacancy": {
                                "id": JOB,
                                "job-name": "Финансовый менеджер",
                                "company": {"name": "Работодатель", "email": "private@example.org"},
                                "region": {"name": "Город Москва"},
                                "salary_min": "150000",
                                "salary_max": "200000",
                                "vac_url": f"https://trudvsem.ru/vacancy/card/123/{JOB}",
                                "duty": "Работа с бюджетами, финансовым моделированием и управленческой отчётностью.",
                            }
                        },
                        {
                            "vacancy": {
                                "id": JOB,
                                "vac_url": "https://unexpected.test",
                                "job-name": "Неверная ссылка",
                            }
                        },
                    ]
                }
            },
        )
    assert req.url.host == "api.superjob.ru"
    assert req.headers["X-Api-App-Id"] == "fake"
    return httpx.Response(429)


def test_search_isolated_sources_owner_actions_and_real_storage(mini_config):
    async def run():
        db = Database(mini_config.data_dir)
        async with httpx.AsyncClient(transport=httpx.MockTransport(source_response)) as sources:
            scheduler = Scheduler(mini_config, db, sources)
            view = scheduler.browser.view
            view.mini_app = MiniApp(mini_config, db, scheduler, sources, SimpleNamespace())
            await view.start()
            try:
                port = view.runner.addresses[0][1]
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    assert (await client.get("/mini")).status_code == 200
                    assert (await client.get("/mini/app.js")).status_code == 200
                    assert (await client.get("/mini/api/state")).status_code == 401
                    now = int(time.time())
                    headers = {"Authorization": "tma " + signed(now), "Origin": PUBLIC_URL}
                    r = await client.get("/mini/api/search", headers=headers)
                    assert r.status_code == 200
                    data = r.json()
                    assert [job["id"] for job in data["jobs"]] == [JOB_ID]
                    assert {note["source"] for note in data["notices"]} == {"hh", "superjob"}
                    assert "private@example.org" not in r.text
                    action = {"id": JOB_ID, "action": "prepare"}
                    origin = headers | {"Origin": "https://unexpected.test"}
                    assert (
                        await client.post("/mini/api/job", headers=origin, json=action)
                    ).status_code == 403
                    assert not db.vacancy(JOB_ID)
                    assert (
                        await client.post("/mini/api/job", headers=headers, json=action)
                    ).json()["job"]["status"] == "queued"
                    action["action"] = "reported"
                    assert (
                        await client.post("/mini/api/job", headers=headers, json=action)
                    ).status_code == 400
                    action["confirm"] = True
                    assert (
                        await client.post("/mini/api/job", headers=headers, json=action)
                    ).json()["job"]["status"] == "reported"
                    assert (
                        await client.post("/mini/api/job", headers=headers, json=action)
                    ).status_code == 400
                    assert (await client.get("/mini/api/state", headers=headers)).json()["jobs"][0][
                        "id"
                    ] == JOB_ID
                    same = await client.post(
                        "/mini/api/import",
                        headers=headers,
                        json={"url": data["jobs"][0]["url"], "title": data["jobs"][0]["title"]},
                    )
                    assert same.json()["job"]["id"] == JOB_ID
                    assert same.json()["job"]["status"] == "reported"
                    external = {
                        "url": "https://employer.example/careers/1#details",
                        "title": "Аналитик",
                    }
                    first = await client.post("/mini/api/import", headers=headers, json=external)
                    assert first.status_code == 200
                    imported = first.json()["job"]
                    assert imported["source"] == "external"
                    assert imported["status"] == "deferred"
                    external["url"] = "https://employer.example/careers/1"
                    second = await client.post("/mini/api/import", headers=headers, json=external)
                    assert second.json()["job"]["id"] == imported["id"]
                    external["url"] = "https://some-user@employer.example/careers/1"
                    assert (
                        await client.post("/mini/api/import", headers=headers, json=external)
                    ).status_code == 400
                    auto = {"mode": "auto", "confirm": False}
                    assert (
                        await client.post("/mini/api/hh-mode", headers=headers, json=auto)
                    ).status_code == 400
                    auto["confirm"] = True
                    assert (
                        await client.post("/mini/api/hh-mode", headers=headers, json=auto)
                    ).status_code == 400
                    assert not db.get("browser_auto_enabled")
            finally:
                await scheduler.shutdown()
                await view.shutdown()

    asyncio.run(run())


def test_verified_hh_account_can_search_and_refresh_resumes_in_mini_app(mini_config, monkeypatch):
    async def run():
        config = replace(mini_config, browser_engine="chromium", groq_key="")
        db = Database(config.data_dir)
        resumes = {"123456789abc": "Финансовый менеджер", "abcdef012345": "Финансовый аналитик"}
        unavailable = hh_browser.HHResumeUnavailable("HH пока не показал список резюме.")

        async with httpx.AsyncClient(transport=httpx.MockTransport(source_response)) as sources:
            scheduler = Scheduler(config, db, sources)
            browser = scheduler.browser
            view = browser.view
            bot = SimpleNamespace()
            view.mini_app = MiniApp(config, db, scheduler, sources, bot)
            browser.provider.state.write_text('{"cookies":[],"origins":[]}')
            db.set("hh_session_verified", True)
            start = AsyncMock()
            monkeypatch.setattr(scheduler, "start", start)
            monkeypatch.setattr(browser, "require_browser", lambda: None)
            monkeypatch.setattr(
                hh_browser, "authorized_resumes", AsyncMock(side_effect=[unavailable, resumes])
            )

            @asynccontextmanager
            async def session(_):
                yield None, None

            monkeypatch.setattr(browser.provider, "session", session)
            await view.start()
            try:
                port = view.runner.addresses[0][1]
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    route = "/mini/api/hh-resumes"
                    foreign = {"Authorization": "tma " + signed(int(time.time()), owner=1)}
                    assert (
                        await client.post(route, headers=foreign | {"Origin": PUBLIC_URL}, json={})
                    ).status_code == 401

                    headers = {
                        "Authorization": "tma " + signed(int(time.time())),
                        "Origin": PUBLIC_URL,
                    }
                    state = (await client.get("/mini/api/state", headers=headers)).json()
                    assert state["hh_connected"] is True
                    assert state["hh_resume_id"] == ""

                    search = await client.post(
                        "/mini/api/hh-mode", headers=headers, json={"mode": "search"}
                    )
                    assert search.status_code == 200
                    assert search.json()["hh_running"] is True
                    assert search.json()["hh_auto"] is False
                    start.assert_awaited_once_with(bot)

                    failed = await client.post(route, headers=headers, json={})
                    assert failed.status_code == 400
                    assert failed.json()["error"] == str(unavailable)
                    state = (await client.get("/mini/api/state", headers=headers)).json()
                    assert state["hh_connected"] is True
                    assert state["hh_running"] is True

                    updated = await client.post(route, headers=headers, json={})
                    assert updated.status_code == 200
                    assert updated.json()["resumes"] == resumes
                    assert updated.json()["hh_connected"] is True
                    assert updated.json()["hh_resume_id"] == ""
            finally:
                await scheduler.shutdown()
                await view.shutdown()

    asyncio.run(run())


def test_mini_app_mobile_search_and_prepare(mini_config):
    executable = shutil.which("chromium") or shutil.which("google-chrome")
    if not executable:
        pytest.skip("Chromium не установлен.")

    async def run():
        db = Database(mini_config.data_dir)
        async with httpx.AsyncClient(transport=httpx.MockTransport(source_response)) as sources:
            scheduler = Scheduler(mini_config, db, sources)
            view = scheduler.browser.view
            view.mini_app = MiniApp(mini_config, db, scheduler, sources, SimpleNamespace())
            await view.start()
            try:
                port = view.runner.addresses[0][1]
                async with (
                    httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client,
                    async_playwright() as driver,
                ):
                    browser = await driver.chromium.launch(executable_path=executable)
                    page = await browser.new_page(
                        viewport={"width": 390, "height": 844}, is_mobile=True, has_touch=True
                    )

                    async def forward(route):
                        request = route.request
                        url = urlsplit(request.url)
                        if url.hostname == "telegram.org":
                            data = json.dumps(signed(int(time.time())))
                            await route.fulfill(
                                body="window.Telegram={WebApp:{ready(){},expand(){},initData:"
                                + data
                                + "}};",
                                content_type="application/javascript",
                            )
                            return
                        result = await client.request(
                            request.method,
                            url.path + ("?" + url.query if url.query else ""),
                            headers=await request.all_headers(),
                            content=request.post_data_buffer,
                        )
                        await route.fulfill(
                            status=result.status_code,
                            headers=dict(result.headers),
                            body=result.content,
                        )

                    await page.route("**/*", forward)
                    await page.goto(PUBLIC_URL + "/mini")
                    await page.get_by_role("button", name="Найти", exact=False).click()
                    await page.get_by_role("heading", name="Финансовый менеджер").wait_for()
                    await page.get_by_role("button", name="Описание").click()
                    await page.get_by_text("Работа с бюджетами", exact=False).wait_for()
                    await page.get_by_role("button", name="Закрыть", exact=True).click()
                    await page.get_by_role("button", name="Сохранить вакансию", exact=True).click()
                    await page.get_by_text("Ожидает анализа").wait_for()
                    await (
                        page.get_by_role("navigation")
                        .get_by_role("button", name="Аккаунты")
                        .click()
                    )
                    await page.get_by_role("heading", name="Авито Работа").wait_for()
                    await page.set_viewport_size({"width": 320, "height": 680})
                    assert await page.evaluate("document.documentElement.scrollWidth") == 320
                    await browser.close()
            finally:
                await scheduler.shutdown()
                await view.shutdown()

    asyncio.run(run())


def test_superjob_authorization_and_real_status_in_the_journal(mini_config):
    config = replace(mini_config, superjob_client_id="42", persistent=True)
    db = Database(config.data_dir)
    requests = []
    token = "synthetic-access"

    def provider(request):
        path = request.url.path
        if path.endswith("/access_token/"):
            assert request.method == "POST"
            assert dict(parse_qsl(request.content.decode())) == {
                "client_id": "42",
                "client_secret": "fake",
                "redirect_uri": PUBLIC_URL + "/mini/oauth/superjob",
                "code": "synthetic-code",
            }
            return httpx.Response(
                200, json={"access_token": token, "refresh_token": "synthetic-refresh"}
            )
        assert request.headers["Authorization"] == "Bearer " + token
        assert request.headers["X-Api-App-Id"] == "fake"
        if path == "/1.0/user_cvs/":
            return httpx.Response(200, json={"objects": [{"id": 37, "profession": "Аналитик"}]})
        assert path == "/2.0/send_cv_on_vacancy/"
        payload = dict(parse_qsl(request.content.decode()))
        requests.append(payload)
        if payload["id_vacancy"] == "71":
            raise httpx.ReadTimeout("synthetic timeout")
        return httpx.Response(200, json={"result": True})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as sources:
            scheduler = Scheduler(config, db, sources)
            view = scheduler.browser.view
            view.mini_app = MiniApp(config, db, scheduler, sources, SimpleNamespace())
            await view.start()
            try:
                port = view.runner.addresses[0][1]
                headers = {"Authorization": "tma " + signed(int(time.time())), "Origin": PUBLIC_URL}
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    connect = await client.post(
                        "/mini/api/superjob", headers=headers, json={"action": "connect"}
                    )
                    query = parse_qs(urlsplit(connect.json()["url"]).query)
                    invalid = await client.get(
                        "/mini/oauth/superjob?state=invalid&code=synthetic-code"
                    )
                    assert invalid.headers["Location"] == "/mini/oauth/finished"
                    auth_query = {"state": query["state"][0], "code": "synthetic-code"}
                    callback = await client.get("/mini/oauth/superjob", params=auth_query)
                    assert callback.headers["Location"] == "/mini/oauth/finished?connected=1"
                    assert (await client.get("/mini/oauth/superjob", params=auth_query)).headers[
                        "Location"
                    ] == "/mini/oauth/finished"
                    profile = await client.get("/mini/api/state", headers=headers)
                    assert token not in profile.text
                    assert profile.json()["superjob"]["selected"] == "37"

                    for vacancy, expected in (("70", "sent"), ("71", "unknown")):
                        identifier = "superjob:" + vacancy
                        details = dict.fromkeys(URL_FIELDS, "")
                        details.update(
                            title="Аналитик",
                            source="superjob",
                            url=f"https://www.superjob.ru/vakansii/analitik-{vacancy}.html",
                        )
                        db.add(identifier, details)
                        db.finish_analysis(identifier, "draft", {"score": 90}, "Письмо аналитика")
                        action = {"id": identifier, "action": "send", "confirm": False}
                        assert (
                            await client.post("/mini/api/job", headers=headers, json=action)
                        ).status_code == 400
                        action["confirm"] = True
                        result = await client.post("/mini/api/job", headers=headers, json=action)
                        assert result.json()["job"]["status"] == expected
                        assert (
                            await client.post("/mini/api/job", headers=headers, json=action)
                        ).status_code == 400
                    assert requests == [
                        {"id_vacancy": number, "id_cv": "37", "comment": "Письмо аналитика"}
                        for number in ("70", "71")
                    ]
            finally:
                await view.shutdown()

    asyncio.run(run())
