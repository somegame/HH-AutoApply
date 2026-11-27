import asyncio
import base64
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


def test_habr_oauth_and_manual_vacancy_work_without_hh_access(mini_config):
    async def run():
        config = replace(
            mini_config,
            habr_client_id="fixture-app",
            habr_client_secret="fixture-app-secret",
        )
        db = Database(config.data_dir)
        db.set("browser_notice", "HH вернул HTTP 403. Операции остановлены.")
        used = []

        def upstream(request):
            used.append(request.url.path)
            assert request.url.host == "career.habr.com"
            if request.url.path == "/integrations/oauth/token":
                assert request.method == "POST"
                assert dict(parse_qsl(request.content.decode())) == {
                    "client_id": "fixture-app",
                    "client_secret": "fixture-app-secret",
                    "redirect_uri": PUBLIC_URL + "/mini/oauth/habr",
                    "grant_type": "authorization_code",
                    "code": "fixture-code",
                }
                return httpx.Response(200, json={"access_token": "fixture-token"})
            assert request.url.path == "/api/v1/integrations/users/me"
            assert dict(request.url.params) == {"access_token": "fixture-token"}
            return httpx.Response(
                200, json={"login": "fixture-owner", "email": "owner@example.test"}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as source:
            scheduler = Scheduler(config, db, source)
            view = scheduler.browser.view
            view.mini_app = MiniApp(config, db, scheduler, source, SimpleNamespace())
            await view.start()
            try:
                port = view.runner.addresses[0][1]
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    headers = {
                        "Origin": PUBLIC_URL,
                        "Authorization": "tma " + signed(int(time.time())),
                    }
                    action = {"action": "connect"}
                    response = await client.post(
                        "/mini/api/habr", headers={"Origin": PUBLIC_URL}, json=action
                    )
                    assert response.status_code == 401
                    opened = await client.post("/mini/api/habr", headers=headers, json=action)
                    oauth = urlsplit(opened.json()["url"])
                    params = parse_qs(oauth.query)
                    assert oauth.hostname == "career.habr.com"
                    assert oauth.path == "/integrations/oauth/authorize"
                    assert params["response_type"] == ["code"]
                    assert "client_secret" not in params
                    callback = "/mini/oauth/habr"
                    state = params["state"][0]
                    invalid = await client.get(
                        callback, params=[("state", state), ("state", state)]
                    )
                    assert invalid.headers["Location"] == "/mini/oauth/finished?provider=habr"
                    assert not used
                    response = await client.get(
                        callback, params={"state": state, "code": "fixture-code"}
                    )
                    assert response.headers["Location"].endswith("provider=habr&connected=1")
                    await client.get(callback, params={"state": state, "code": "fixture-code"})
                    assert len(used) == 2
                    snapshot = await client.get("/mini/api/state", headers=headers)
                    data = snapshot.json()
                    assert data["habr"]["profile"]["login"] == "fixture-owner"
                    assert "fixture-token" not in snapshot.text
                    assert "owner@example.test" not in snapshot.text
                    assert db.get("habr_profile") == data["habr"]["profile"]
                    assert data["hh_access_restricted"] and not data["hh_send"]
                    assert not next(site for site in data["sources"] if site["id"] == "habr")[
                        "in_app_apply"
                    ]
                    assert next(site for site in data["sources"] if site["id"] == "trudvsem")[
                        "account_url"
                    ].endswith("/auth/login")

                    url = "/mini/api/import"
                    vacancy = {
                        "url": "https://career.habr.com/vacancies/201?ref=test",
                        "title": "Аналитик",
                    }
                    first = await client.post(url, headers=headers, json=vacancy)
                    assert first.json()["job"]["status"] == "deferred"
                    edited = {
                        "url": "https://career.habr.com/vacancies/201?ref=another",
                        "description": "Аналитик финансовых данных\nПодготовка отчётов и план-факт анализ.",
                    }
                    upgraded = await client.post(url, headers=headers, json=edited)
                    assert upgraded.json()["job"]["id"] == "habr:201"
                    assert upgraded.json()["job"]["status"] == "queued"
                    assert (
                        "план-факт" in json.loads(db.vacancy("habr:201")["details"])["description"]
                    )
                    assert len(used) == 2
            finally:
                await scheduler.shutdown()
                await view.shutdown()

    asyncio.run(run())


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


def test_progressive_search_survives_a_slow_source_and_persists_jobs(mini_config, monkeypatch):
    async def run():
        db = Database(mini_config.data_dir)
        slow_hh = asyncio.Event()
        first_source_saved = asyncio.Event()
        resume = "Оценка финансового состояния и анализ расходов компании."
        db.set("resume", resume)

        async def search_source(_, source):
            if source == "hh":
                await slow_hh.wait()
                raise ServiceError("Браузер HH занят.")
            if source == "superjob":
                return []
            return [
                (
                    JOB_ID,
                    dict.fromkeys(URL_FIELDS, "")
                    | {
                        "title": "Финансовый аналитик",
                        "url": f"https://trudvsem.ru/vacancy/card/{JOB}",
                        "description": "Составлять отчёт о доходах и расходах.",
                    },
                )
            ]

        async def start_worker(_):
            first_source_saved.set()

        async with httpx.AsyncClient() as upstream:
            scheduler = Scheduler(mini_config, db, upstream)
            monkeypatch.setattr(scheduler, "start", start_worker)
            view = scheduler.browser.view
            view.mini_app = MiniApp(mini_config, db, scheduler, upstream, SimpleNamespace())
            monkeypatch.setattr(view.mini_app, "search_source", search_source)
            await view.start()
            try:
                port = view.runner.addresses[0][1]
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    headers = {
                        "Authorization": "tma " + signed(int(time.time())),
                        "Origin": PUBLIC_URL,
                    }
                    started = await client.post("/mini/api/search-start", headers=headers, json={})
                    assert started.status_code == 202
                    identifier = started.json()["search_id"]
                    url = "/mini/api/search-status"
                    params = {"search_id": identifier}
                    assert (await client.get(url, params=params)).status_code == 401
                    await asyncio.wait_for(first_source_saved.wait(), timeout=2)
                    run = view.mini_app.searches[identifier]
                    assert not run.task.done()
                    partial = (await client.get(url, params=params, headers=headers)).json()
                    assert not partial["done"] and "hh" in partial["pending_sources"]
                    assert partial["analysis_pending"] == 1
                    assert partial["jobs"][0]["analysis_status"] == "queued"
                    assert json.loads(db.vacancy(JOB_ID)["details"])["auto_discovered"] is True
                    again = await client.post("/mini/api/search-start", headers=headers, json={})
                    assert again.json()["search_id"] == identifier
                    other = await client.post(
                        "/mini/api/search-start", headers=headers, json={"query": "Экономист"}
                    )
                    assert other.status_code == 409

                    db.finish_analysis(
                        JOB_ID,
                        "draft",
                        {
                            "score": 91,
                            "skills_score": 84,
                            "duties": ["Составлять отчёт о доходах и расходах."],
                            "resume_hash": hashlib.sha256(resume.encode()).hexdigest(),
                        },
                        "Письмо",
                    )
                    slow_hh.set()
                    await asyncio.wait_for(run.task, timeout=2)
                    result = (await client.get(url, params=params, headers=headers)).json()
                    assert result["done"] and result["analysis_pending"] == 0
                    assert result["notices"] == [{"source": "hh", "message": "Браузер HH занят."}]
                    assert result["jobs"][0]["skills_score"] == 84
                    assert result["jobs"][0]["duties"] == ["Составлять отчёт о доходах и расходах."]
                    assert result["jobs"][0]["analysis_status"] == "complete"
                    assert (
                        await client.get(url, params={"search_id": "gone"}, headers=headers)
                    ).status_code == 410

                    settings = "/mini/api/auto-settings"
                    for invalid in (False, 0, -1, 101, "92"):
                        denied = await client.post(
                            settings, headers=headers, json={"skills_score": invalid}
                        )
                        assert denied.status_code == 400
                    updated = await client.post(
                        settings, headers=headers, json={"skills_score": 92}
                    )
                    assert updated.json()["auto_settings"]["skills_score"] == 92
                    assert not db.get("browser_auto_enabled")
                    assert (
                        Database(mini_config.data_dir).settings(mini_config)["skills_score"] == 92
                    )
            finally:
                await view.shutdown()

    asyncio.run(run())


def test_background_discovery_rechecks_mini_results_and_keeps_edited_letters(mini_config):
    db = Database(mini_config.data_dir)
    details = {"source": "mini_hh", "mini_filters": {"salary": 0}}
    background = {"source": "browser"}
    assert db.add_discovered("11", details)
    original = db.vacancy("11")["details"]
    assert db.finish_analysis("11", "filtered", {"letter": "Прежний результат"}, None)
    assert db.add_discovered("11", background)
    assert not db.finish_analysis(
        "11", "draft", {"letter": "Устаревший черновик"}, "", expected_details=original
    )
    row = db.vacancy("11")
    assert row["status"] == "queued"
    assert json.loads(row["details"]) == background | {"auto_discovered": True}
    assert not db.add_discovered("11", details)

    assert db.add_discovered("12", details)
    assert db.finish_analysis("12", "draft", {"letter": "Исходное письмо"}, "Текст владельца")
    assert not db.add_discovered("12", background)
    assert db.vacancy("12")["letter"] == "Текст владельца"


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

    def mapped_source(request):
        response = source_response(request)
        if not response.is_success:
            return response
        payload = response.json()
        rows = payload["results"]["vacancies"]
        first = rows[0]["vacancy"]
        first["addresses"] = {
            "address": [{"lat": 55.75, "lng": 37.62, "location": "Москва, улица Тестовая, 1"}]
        }
        second = first | {
            "id": "01234567-c88d-11f1-999a-2f7506c6dfb2",
            "job-name": "Финансовый аналитик",
            "addresses": {
                "address": [{"lat": 55.755, "lng": 37.625, "location": "Москва, улица Тестовая, 2"}]
            },
        }
        return httpx.Response(200, json={"results": {"vacancies": [rows[0], {"vacancy": second}]}})

    async def run():
        db = Database(mini_config.data_dir)
        resume = "Готовил управленческую отчётность, строил финансовые модели и проверял бюджеты."
        db.set_many(
            {
                "resume": resume,
                "hh_session_verified": True,
                "hh_selected_resume": {"id": "123456789abc", "name": "Финансовый менеджер"},
            }
        )
        config = replace(mini_config, browser_engine="chromium", gemini_key="synthetic")
        async with httpx.AsyncClient(transport=httpx.MockTransport(mapped_source)) as sources:
            scheduler = Scheduler(config, db, sources)
            scheduler.browser.provider.state.write_text("{}")
            view = scheduler.browser.view
            view.mini_app = MiniApp(config, db, scheduler, sources, SimpleNamespace())
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
                    tile_requests = []
                    search_down = False
                    draft = {
                        "text": "Финансовый менеджер\n\nОпыт работы\n" + resume,
                        "mode": "improve",
                        "target": "Финансовый менеджер",
                        "suggestions": ["Уточните инструменты для отчётности."],
                        "questions": ["Сколько бюджетов вы вели?"],
                        "sample_count": 2,
                        "provider": "gemini",
                    }

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
                        if search_down and url.path == "/mini/api/search-status":
                            await route.fulfill(status=504, body="Gateway Timeout")
                            return
                        if url.path == "/mini/api/resume-studio" and request.method == "POST":
                            assert request.post_data_json == {
                                "action": "generate",
                                "mode": "improve",
                                "target": "Финансовый менеджер",
                                "facts": "Вёл бюджет проекта.",
                            }
                            db.set("resume_studio", draft)
                            await route.fulfill(
                                json={
                                    "resume_studio": draft,
                                    "resume_studio_status": {"status": "ready", "message": ""},
                                }
                            )
                            return
                        if url.hostname == "tile.openstreetmap.org":
                            tile_requests.append(url.path)
                            await route.fulfill(
                                body=base64.b64decode(
                                    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+pU4sAAAAASUVORK5CYII="
                                ),
                                content_type="image/png",
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
                    await page.locator("#source").select_option("trudvsem")
                    await page.get_by_role("button", name="Найти", exact=False).click()
                    await page.get_by_role("heading", name="Финансовый менеджер").wait_for()
                    matches = page.locator("#results")
                    card = matches.locator('[data-job-id="' + JOB_ID + '"]')
                    await card.get_by_text("Ожидает анализа", exact=True).wait_for()
                    search_down = True
                    await page.get_by_role("button", name="Обновить", exact=True).click()
                    await page.get_by_text(
                        "Bothost не дождался ответа бота.", exact=False
                    ).wait_for()
                    assert await card.is_visible()
                    db.finish_analysis(
                        JOB_ID,
                        "draft",
                        {
                            "score": 98,
                            "skills_score": 95,
                            "duties": [
                                "Готовить отчёты о доходах и расходах.",
                                "Проверять и обновлять бюджеты.",
                                "Строить финансовые прогнозы.",
                            ],
                            "resume_hash": hashlib.sha256(resume.encode()).hexdigest(),
                            "analysis_provider": "gemini",
                        },
                        "Письмо работодателю",
                    )
                    search_down = False
                    await page.get_by_role("button", name="Обновить результаты").click()
                    await card.get_by_text("Оценка Gemini", exact=True).wait_for()
                    await card.get_by_text("95%", exact=True).wait_for()
                    await card.get_by_text("Лучший вариант", exact=False).wait_for()
                    await card.get_by_text("Готовить отчёты о доходах и расходах.").wait_for()
                    await card.get_by_text("Ещё обязанности").click()
                    assert await card.get_by_text("Строить финансовые прогнозы.").is_visible()
                    assert not tile_requests
                    await page.get_by_role("button", name="Карта", exact=True).click()
                    await page.locator("#job-map .office-pin").last.wait_for()
                    assert await page.locator("#job-map .office-pin").count() == 2
                    await page.locator("#job-map .leaflet-tile").first.wait_for()
                    assert tile_requests
                    await page.get_by_role("button", name="Список", exact=True).click()
                    for _ in range(2):
                        await matches.get_by_role("button", name="К сравнению").first.click()
                    await page.get_by_role("button", name="Сравнить", exact=True).click()
                    assert await page.locator("#compare-dialog .compare-card").count() == 2
                    await page.locator("#compare-close").click()
                    await card.get_by_role("button", name="Описание").click()
                    await page.get_by_text("Работа с бюджетами", exact=False).wait_for()
                    await page.get_by_role("button", name="Закрыть", exact=True).click()
                    await card.get_by_role("button", name="К откликам", exact=True).click()
                    await page.get_by_role("heading", name="Отклики", exact=True).wait_for()
                    db.add(
                        "53",
                        dict.fromkeys(URL_FIELDS, "")
                        | {
                            "source": "browser",
                            "title": "Финансовый контролёр",
                            "url": "https://hh.ru/vacancy/53",
                        },
                    )
                    db.transition("53", ("queued",), "unknown")
                    await page.get_by_role("button", name="Обновить", exact=True).click()
                    await page.get_by_text("Требуют сверки на HH · 1.").wait_for()
                    await page.get_by_text("Причины и недавние попытки").click()
                    assert await page.get_by_text(
                        "Результат отправки неизвестен", exact=False
                    ).first.is_visible()
                    assert await page.locator("#hh-activity-confirmed").text_content() == "0"
                    await page.get_by_role("button", name="К проверке отправки").click()
                    assert await page.evaluate(
                        "document.activeElement.hasAttribute('data-hh-reconcile')"
                    )
                    await (
                        page.get_by_role("navigation")
                        .get_by_role("button", name="Аккаунты")
                        .click()
                    )
                    await page.get_by_role("heading", name="Авито Работа").wait_for()
                    await page.get_by_role("heading", name="Автоотклики HH").wait_for()
                    assert await page.get_by_text(
                        "Сохранение резюме, входа в HH", exact=False
                    ).is_visible()
                    await page.get_by_label("Совпадение навыков от, %").fill("91")
                    await page.get_by_role("button", name="Сохранить порог").click()
                    await page.get_by_text("Порог навыков сохранён · 91%.").wait_for()
                    assert db.settings(config)["skills_score"] == 91
                    assert not db.get("browser_auto_enabled")
                    await page.get_by_role("button", name="Редактор резюме").click()
                    await page.get_by_role("button", name="Настроить доступ Gemini").click()
                    await page.get_by_role("heading", name="Анализ Gemini", exact=True).wait_for()
                    await page.get_by_role("button", name="Разрешить Gemini").click()
                    await page.get_by_text("Обработка в Gemini разрешена.", exact=True).wait_for()
                    assert db.get("gemini_consent") is True
                    assert not db.get("groq_consent")
                    await page.get_by_role("button", name="Редактор резюме").click()
                    await page.get_by_label("Ваши факты и достижения").fill("Вёл бюджет проекта.")
                    await page.get_by_role("button", name="Подготовить черновик").click()
                    await page.get_by_text("Уточните инструменты для отчётности.").wait_for()
                    assert await page.get_by_label("Текст черновика").input_value() == draft["text"]
                    assert await page.get_by_text("Gemini · Вакансий учтено: 2").is_visible()
                    assert not await page.get_by_role(
                        "button", name="Использовать для анализа"
                    ).is_visible()
                    assert db.get("resume") == resume
                    await page.get_by_role("button", name="Отозвать разрешение Gemini").click()
                    await page.get_by_text("Разрешение для Gemini отозвано.", exact=True).wait_for()
                    assert not db.get("gemini_consent")
                    db.set("browser_notice", "HH вернул HTTP 403. Операции остановлены.")
                    await page.get_by_role("button", name="Обновить", exact=True).click()
                    await page.get_by_role("button", name="Проверить доступ HH").wait_for()
                    assert not await page.get_by_role("button", name="Включить автоотклики").count()
                    await page.get_by_role("button", name="Редактор резюме").click()
                    assert await page.get_by_role(
                        "heading", name="Навыки и опыт на HH"
                    ).is_visible()
                    assert await page.get_by_role("button", name="Открыть резюме в HH").is_enabled()
                    await page.get_by_role("button", name="Добавить вакансию по ссылке").click()
                    await page.get_by_label("Ссылка на вакансию", exact=True).fill(
                        "https://career.habr.com/vacancies/201"
                    )
                    await page.get_by_label("Описание для анализа и письма").fill(
                        "Финансовый аналитик\nПодготовка управленческих отчётов."
                    )
                    await page.get_by_role("button", name="Сохранить в журнал").click()
                    imported = page.locator('article[data-job-id="habr:201"]')
                    await imported.wait_for()
                    assert await imported.get_by_role(
                        "button", name="Открыть на площадке"
                    ).is_enabled()
                    assert await imported.get_by_role(
                        "button", name="Отметить свой отклик"
                    ).is_enabled()
                    await page.locator("#journal-source").select_option("hh")
                    assert not await imported.count()
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


def test_discovery_preferences_rank_and_explicit_route(mini_config, monkeypatch):
    from app.scheduler import local_day

    config = replace(mini_config, ors_key="synthetic-routing-key")
    resume = "Бюджетирование, финансовый анализ, подготовка отчётов, прогнозирование расходов."
    origin = {"lat": 55.75, "lon": 37.62}
    near = {"lat": 55.754, "lon": 37.625, "address": "Москва, улица Тестовая, 1"}
    external_requests = []
    searched = []

    def external(request):
        external_requests.append(request)
        assert request.url.host == "api.heigit.org"
        assert request.headers["Authorization"] == "synthetic-routing-key"
        assert request.url.path.endswith("/driving-car/json")
        assert json.loads(request.content)["coordinates"] == [
            [origin["lon"], origin["lat"]],
            [near["lon"], near["lat"]],
        ]
        return httpx.Response(
            200, json={"routes": [{"summary": {"duration": 540, "distance": 4500}}]}
        )

    def vacancy(title, location):
        job = dict.fromkeys(URL_FIELDS, "")
        job.update(
            title=title,
            company="Пример",
            region="Москва",
            monthly=True,
            salary="200 000 ₽",
            salary_min=200000,
            salary_max=200000,
            currency="RUB",
            address="Москва, улица Тестовая, 1" if location == near else "",
            format="офис",
            published=local_day(config),
            location=location,
            url="https://hh.ru/vacancy/1",
            source="browser",
            description="Работа с бюджетированием, финансовым анализом и расходами.",
        )
        return job

    async def run():
        db = Database(config.data_dir)
        db.set("resume", resume)
        close = vacancy("Финансовый аналитик", near)
        db.add("1", close)
        db.finish_analysis(
            "1",
            "draft",
            {"score": 97, "resume_hash": hashlib.sha256(resume.encode()).hexdigest()},
            "Текст письма",
        )

        async def sources(settings, source):
            searched.append((source, settings["query"], len(settings["search_queries"])))
            if source == "hh":
                return [
                    ("1", close),
                    ("2", vacancy("Финансовый контролёр", {"lat": 57.1, "lon": 37.62})),
                ]
            if source == "trudvsem":
                return [("trudvsem:1", close), ("trudvsem:2", vacancy("Экономист", None))]
            return []

        async with httpx.AsyncClient(transport=httpx.MockTransport(external)) as upstream:
            scheduler = Scheduler(config, db, upstream)
            view = scheduler.browser.view
            view.mini_app = MiniApp(config, db, scheduler, upstream, SimpleNamespace())
            monkeypatch.setattr(view.mini_app.sources, "search", sources)
            await view.start()
            try:
                port = view.runner.addresses[0][1]
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    prefs = "/mini/api/preferences"
                    assert (
                        await client.post(prefs, headers={"Origin": PUBLIC_URL}, json=origin)
                    ).status_code == 401
                    headers = {
                        "Authorization": "tma " + signed(int(time.time())),
                        "Origin": PUBLIC_URL,
                    }
                    invalid = await client.post(
                        prefs, headers=headers, json={"home": {"lat": 180, "lon": 0}}
                    )
                    assert invalid.status_code == 400
                    saved = await client.post(
                        prefs,
                        headers=headers,
                        json={
                            "home": origin | {"address": "Личный адрес"},
                            "travel_mode": "car",
                            "preferred_format": "office",
                        },
                    )
                    assert saved.json()["preferences"]["home"] == origin
                    assert "Личный адрес" not in saved.text
                    url = "/mini/api/search?include_related=true&sort=commute&max_commute=50&include_unknown_commute=false"
                    response = await client.get(url, headers=headers)
                    assert response.status_code == 200
                    result = response.json()
                    assert [job["id"] for job in result["jobs"]] == ["1"]
                    job = result["jobs"][0]
                    assert job["rank"]["fit"] == 97
                    assert job["commute"]["kind"] == "estimate"
                    assert job["top"] is True and result["stats"]["duplicates"] == 1
                    assert (
                        len(
                            {
                                query
                                for key, query, count in searched
                                if key == "trudvsem" and count == 3
                            }
                        )
                        == 3
                    )
                    assert not external_requests

                    routed = await client.post("/mini/api/route", headers=headers, json={"id": "1"})
                    assert routed.json()["commute"]["kind"] == "routed"
                    assert routed.json()["commute"]["minutes"] == 9
                    assert len(external_requests) == 1
                    repeated = await client.get(url, headers=headers)
                    assert repeated.json()["jobs"][0]["commute"]["minutes"] == 9
                    assert len(external_requests) == 1
                    assert (
                        await client.post(prefs, headers=headers, json={"home": None})
                    ).status_code == 200
                    assert db.get("vacancy_discovery")["home"] is None
            finally:
                await view.shutdown()

    asyncio.run(run())
