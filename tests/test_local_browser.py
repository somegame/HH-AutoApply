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
    "value", ["http://host.test", "https://u:p@host.test", "https://host.test/path"]
)
def test_public_login_requires_https_origin(value):
    with pytest.raises(ValueError, match="HTTPS"):
        public_login_url(value)
    assert public_login_url("https://bot.example.test/") == "https://bot.example.test"


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
                    link = view.issue(page)
                    secret = urlsplit(link).fragment
                    headers = {"Authorization": "Bearer " + secret}
                    frame = await client.get("/hh-login/frame", headers=headers)
                    assert frame.status_code == 200
                    assert frame.content[:2] == b"\xff\xd8"
                    assert frame.headers["Cache-Control"] == "no-store"
                    endpoint = "/hh-login/input"
                    entry = {"kind": "entry", "text": "synthetic-secret"}
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
                        await phone.locator("#value").fill("phone-input")
                        await phone.get_by_role("button", name="Передать в поле").click()
                        await phone.get_by_text("Действие передано в браузер HH.").wait_for(
                            timeout=5000
                        )
                        assert await page.locator("input").input_value() == "phone-input"
                        assert not urlsplit(phone.url).fragment
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
