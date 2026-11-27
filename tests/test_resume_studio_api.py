import asyncio
import time
from dataclasses import replace
from types import SimpleNamespace

import httpx
import pytest
from test_mini_app import PUBLIC_URL, mini_config, signed  # noqa: F401

from app.database import Database
from app.mini_app import MiniApp
from app.scheduler import Scheduler, local_day

ORIGINAL = "Ежемесячно готовил бюджет и сравнивал плановые и фактические расходы."
DRAFT = "Финансовый менеджер\n\nОпыт работы\n" + ORIGINAL
ROUTE = "/mini/api/resume-studio"


@pytest.mark.parametrize("fallback", [False, True])
def test_resume_studio_preserves_original_and_requires_consent(
    mini_config,  # noqa: F811
    monkeypatch,
    fallback,
):
    async def run():
        config = replace(
            mini_config, groq_key="synthetic", gemini_key="synthetic" if fallback else ""
        )
        db = Database(config.data_dir)
        db.set("resume", ORIGINAL)
        db.add(
            "1",
            {
                "title": "Финансовый менеджер",
                "company": "Тестовая компания",
                "region": "Москва",
                "salary": "",
                "url": "https://hh.ru/vacancy/1",
                "description": "Составлять бюджеты. Письма на example@example.org",
                "source": "browser",
            },
        )
        entered, release = asyncio.Event(), asyncio.Event()

        async def generate(_client, _config, **inputs):
            assert inputs["resume"] == ORIGINAL
            assert inputs["mode"] == "improve"
            assert inputs["facts"] == "Контролировал расходы"
            sample = inputs["vacancy_sample"]
            assert len(sample) == 1
            assert sample[0]["title"] == "Финансовый менеджер"
            assert "example@example.org" not in repr(sample)
            assert "Тестовая компания" not in repr(sample)
            await inputs["complete"]("Тестовая инструкция", {"sample": sample})
            entered.set()
            await release.wait()
            return {"text": DRAFT, "questions": [], "suggestions": ["Добавьте масштаб работы."]}

        monkeypatch.setattr("app.mini_app.build_resume", generate)

        def upstream_result(request):
            if request.url.host == "api.groq.com":
                if fallback:
                    return httpx.Response(429)
                return httpx.Response(200, json={"choices": [{"message": {"content": "{}"}}]})
            assert fallback and request.url.host == "generativelanguage.googleapis.com"
            return httpx.Response(
                200, json={"candidates": [{"content": {"parts": [{"text": "{}"}]}}]}
            )

        transport = httpx.MockTransport(upstream_result)
        async with httpx.AsyncClient(transport=transport) as upstream:
            scheduler = Scheduler(config, db, upstream)
            view = scheduler.browser.view
            view.mini_app = MiniApp(config, db, scheduler, upstream, SimpleNamespace())
            await view.start()
            try:
                port = view.runner.addresses[0][1]
                async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as client:
                    headers = {
                        "Authorization": "tma " + signed(int(time.time())),
                        "Origin": PUBLIC_URL,
                    }
                    request = {
                        "action": "generate",
                        "mode": "improve",
                        "target": "Финансовый менеджер",
                        "facts": "Контролировал расходы",
                    }
                    assert (await client.get(ROUTE)).status_code == 401
                    response = await client.post(ROUTE, headers=headers, json=request)
                    assert response.status_code == 400
                    assert db.get("groq_usage") is None
                    if fallback:
                        accepted = await client.post(
                            "/mini/api/ai-consent", headers=headers, json={"gemini": True}
                        )
                        assert accepted.json()["gemini_consent"] is True
                    request["consent"] = True
                    response = await client.post(ROUTE, headers=headers, json=request)
                    assert response.status_code == 202
                    assert response.json()["resume_studio_status"]["status"] == "processing"
                    await asyncio.wait_for(entered.wait(), 2)
                    assert db.get("groq_usage") == {"day": local_day(config), "count": 1}
                    assert db.get("resume") == ORIGINAL
                    release.set()
                    await asyncio.wait_for(view.mini_app.resume_task, 2)
                    response = await client.get(ROUTE, headers=headers)
                    payload = response.json()
                    assert payload["resume_studio_status"]["status"] == "ready"
                    assert payload["resume_studio"]["text"] == DRAFT
                    assert payload["resume_studio"]["sample_count"] == 1
                    assert payload["resume_studio"]["provider"] == (
                        "gemini" if fallback else "groq"
                    )
                    assert "profile_hash" not in payload["resume_studio"]
                    assert Database(config.data_dir).get("resume_studio")["text"] == DRAFT
                    assert db.get("resume") == ORIGINAL
                    adopt = {"action": "adopt", "confirm": True}
                    db.set("hh_selected_resume", {"id": "123456789abc"})
                    blocked = await client.post(ROUTE, headers=headers, json=adopt)
                    assert blocked.status_code == 400
                    assert db.get("resume") == ORIGINAL
                    db.set("hh_selected_resume", {})
                    db.set("resume", ORIGINAL + " Профиль обновлён.")
                    assert (
                        await client.post(ROUTE, headers=headers, json=adopt)
                    ).status_code == 400
                    db.set("resume", ORIGINAL)
                    response = await client.post(ROUTE, headers=headers, json=adopt)
                    assert response.status_code == 200
                    assert response.json()["resume"] == DRAFT
            finally:
                await view.shutdown()

    asyncio.run(run())
