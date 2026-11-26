import asyncio
import json
import shutil
import stat
from types import SimpleNamespace
from urllib.parse import urlsplit

import httpx
import pytest

from app.local_browser import LocalBrowser, public_login_url
from app.login_view import LoginView


@pytest.mark.parametrize(
    "value",
    [
        "http://host.test",
        "https://u:p@host.test",
        "https://host.test/path",
        "host.test/path",
        "host.test?param=x",
        "host.test:3000",
    ],
)
def test_public_login_requires_https_origin(value):
    with pytest.raises(ValueError, match="HTTPS"):
        public_login_url(value)


@pytest.mark.parametrize(
    "value", ["bot.example.test", "  BoT.example.test ", "https://bot.example.test/"]
)
def test_public_login_accepts_panel_domain_or_full_url(value):
    assert public_login_url(value) == "https://bot.example.test"


def test_local_login_frame_input_expiration_and_state(tmp_path):
    executable = (
        shutil.which("chromium-headless-shell")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
    )
    if not executable:
        pytest.skip("Chromium для проверки локального входа не установлен.")

    async def run():
        config = SimpleNamespace(
            data_dir=tmp_path,
            chromium_path=executable,
            login_public_url="https://bot.example.test",
            web_port=0,
        )
        browser = LocalBrowser(config)
        view = LoginView(config)
        await view.start()
        try:
            port = view.runner.addresses[0][1]
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                assert (await client.get("/health")).text == "ok"
                html = await client.get("/hh-login")
                assert "frame-ancestors 'none'" in html.headers["Content-Security-Policy"]
                assert (await client.get("/hh-login.js")).status_code == 200
                assert (await client.get("/hh-login/frame")).status_code == 401
                async with browser.session(interactive=True) as (context, page):
                    await page.route(
                        "**/*",
                        lambda route: route.fulfill(
                            body='<input type="password" autofocus>',
                            content_type="text/html",
                        ),
                    )
                    await page.goto("https://hh.ru/account/login")
                    link = await view.issue(page)
                    secret = urlsplit(link).fragment
                    headers = {"Authorization": "Bearer " + secret}
                    await asyncio.wait_for(view.frame_ready.wait(), 5)
                    frame = await client.get("/hh-login/frame", headers=headers)
                    assert frame.status_code == 200
                    assert frame.content[:2] == b"\xff\xd8"
                    assert frame.headers["Cache-Control"] == "no-store"
                    endpoint = "/hh-login/input"
                    [field] = json.loads(frame.headers["X-HH-Fields"])
                    entry = {"kind": "entry", "field": field["id"], "text": "synthetic-secret"}
                    assert (
                        await client.post(endpoint, headers=headers, json=entry)
                    ).status_code == 403
                    valid_headers = headers | {"Origin": "https://bot.example.test"}
                    assert (
                        await client.post(endpoint, headers=valid_headers, json=entry)
                    ).status_code == 204
                    assert await page.locator("input").input_value() == "synthetic-secret"
                    phone_context = await context.browser.new_context(
                        is_mobile=True,
                        has_touch=True,
                        viewport={"width": 390, "height": 844},
                    )
                    try:
                        phone = await phone_context.new_page()

                        async def forward(route):
                            request = route.request
                            response = await client.request(
                                request.method,
                                urlsplit(request.url).path,
                                headers=await request.all_headers(),
                                content=request.post_data_buffer,
                            )
                            await route.fulfill(
                                status=response.status_code,
                                headers=dict(response.headers),
                                body=response.content,
                            )

                        await phone.route("**/*", forward)
                        await phone.goto(link)
                        await phone.locator('#screen[src^="blob:"]').wait_for(timeout=5000)
                        await phone.get_by_label("Пароль").fill("phone-input")
                        await page.wait_for_function(
                            "() => document.querySelector('input').value === 'phone-input'"
                        )
                        assert not urlsplit(phone.url).fragment
                        await view.revoke("done")
                        await phone.get_by_text("Вход подтверждён.", exact=False).wait_for(
                            timeout=5000
                        )
                        assert not await phone.locator("#stage").is_visible()
                    finally:
                        await phone_context.close()
                    await context.add_cookies(
                        [
                            {"name": "session", "value": "sample", "domain": ".hh.ru", "path": "/"},
                            {
                                "name": "third_party",
                                "value": "omit",
                                "domain": ".example.test",
                                "path": "/",
                            },
                        ]
                    )
                    await browser.save(context)
                    await view.revoke()
                    assert (await client.get("/hh-login/frame", headers=headers)).status_code == 401
                saved = json.loads(browser.state.read_text())
                assert [item["name"] for item in saved["cookies"]] == ["session"]
                assert "synthetic-secret" not in browser.state.read_text()
                assert stat.S_IMODE(browser.state.stat().st_mode) == 0o600
                async with browser.session() as (context, _):
                    assert (await context.cookies())[0]["name"] == "session"
        finally:
            await view.shutdown()

    asyncio.run(run())


def test_failed_login_check_keeps_session_until_success(tmp_path, monkeypatch):
    from contextlib import asynccontextmanager

    from app.browser_workflow import BrowserWorkflow
    from app.database import Database

    executable = shutil.which("chromium") or shutil.which("google-chrome")
    if not executable:
        pytest.skip("Chromium для проверки локального входа не установлен.")

    async def run():
        config = SimpleNamespace(
            data_dir=tmp_path,
            chromium_path=executable,
            browser_engine="chromium",
            login_public_url="https://bot.example.test",
            web_port=0,
        )
        db = Database(tmp_path)
        workflow = BrowserWorkflow(config, db)
        monkeypatch.setattr("app.browser_workflow.resource_warning", lambda _: None)
        notified = []
        active = None
        session = workflow.provider.session

        async def serve(route):
            path = urlsplit(route.request.url).path
            if path == "/applicant/resumes":
                if "test_session=on" not in (await route.request.all_headers()).get("cookie", ""):
                    await route.fulfill(status=302, headers={"location": "/account/login"})
                else:
                    await route.fulfill(
                        body='<a href="/resume/123456789abc">Finance Manager</a>',
                        content_type="text/html",
                    )
            else:
                await route.fulfill(body="<input autofocus>", content_type="text/html")

        @asynccontextmanager
        async def local_session(*args, **kwargs):
            nonlocal active
            async with session(*args, **kwargs) as (context, page):
                await context.route("**/*", serve)
                active = page
                yield context, page

        monkeypatch.setattr(workflow.provider, "session", local_session)

        async def notify(result):
            notified.append(result)

        try:
            await workflow.start_login(notify)
            token = workflow.view.secret
            assert await workflow.finish_login() is None
            async with asyncio.timeout(8):
                while workflow.view.status != "retry":
                    await asyncio.sleep(0.05)
            assert workflow.view.secret == token
            assert workflow.view.page == active
            assert not workflow.provider.state.exists()
            await active.context.add_cookies(
                [{"name": "test_session", "value": "on", "domain": ".hh.ru", "path": "/"}]
            )
            await active.goto("https://hh.ru/")
            result = await asyncio.wait_for(workflow.login_task, 8)
            assert result == {"123456789abc": "Finance Manager"}
            assert notified == [result]
            assert workflow.view.status == "done"
            assert workflow.view.page is None
            assert workflow.provider.state.exists()
            assert db.get("hh_resume_choices") == result
        finally:
            await workflow.cancel_login()
            await workflow.view.shutdown()

    asyncio.run(run())
