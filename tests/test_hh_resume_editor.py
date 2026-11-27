import asyncio
import shutil
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from playwright.async_api import async_playwright

from app import hh_browser as hh
from app import hh_resume_editor as editor
from app.browser_workflow import BrowserWorkflow
from app.database import Database
from app.services import ServiceError

RESUME = "123456789abc"
DUTIES = "Составлял бюджет подразделения и ежемесячно сравнивал план и факт."


def test_proposals_are_bound_to_the_original_place_of_work():
    current = {
        "skills": ["Excel"],
        "jobs": [
            {"company": "Компания А", "role": "Финансовый менеджер", "duties": DUTIES},
            {"company": "Компания Б", "role": "Экономист", "duties": "Рассчитывал себестоимость."},
        ],
    }
    result = editor.proposals(
        {
            "skills": [
                {"job": 0, "name": "Бюджетирование", "quote": "Составлял бюджет подразделения"},
                {"job": 1, "name": "Управленческая отчётность", "quote": DUTIES},
                {"job": 0, "name": "Excel", "quote": DUTIES},
            ],
            "duties": [
                {"job": 0, "text": "Готовил бюджет подразделения.", "quote": DUTIES},
                {"job": 0, "text": "Управлял бюджетом в 800 млн ₽.", "quote": DUTIES},
                {"job": 1, "text": "Контролировал план и факт.", "quote": DUTIES},
            ],
        },
        current,
    )
    assert [item["name"] for item in result["skills"]] == ["Бюджетирование"]
    assert [(item["company"], item["text"]) for item in result["duties"]] == [
        ("Компания А", "• Готовил бюджет подразделения.")
    ]


def test_prefill_checks_selected_resume_and_original_work_description():
    chrome = shutil.which("chromium") or shutil.which("google-chrome")
    if not chrome:
        pytest.skip("Нужен Chromium для проверки формы HH")

    async def run():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=chrome)
            try:
                page = await browser.new_page()
                html = f"""
                    <meta charset="utf-8">
                    <h1>Финансовый менеджер</h1><button>Редактировать</button>
                    <section data-qa="resume-block-experience">
                      <div data-qa="resume-block-experience-item">
                        <p data-qa="resume-block-experience-position">Менеджер</p>
                        <p data-qa="resume-block-experience-employer">Тестовая компания</p>
                        <p data-qa="resume-block-experience-description">{DUTIES}</p>
                      </div>
                    </section>
                    <section data-qa="resume-block-skills">
                       <span data-qa="bloko-tag__text">Excel</span>
                    </section>
                    <form>
                      <textarea name="description">{DUTIES}</textarea>
                      <button id="save" type="button" onclick="this.dataset.pressed='yes'">Сохранить</button>
                    </form>
                    <input placeholder="Добавить навык">
                """
                await page.route(
                    "https://hh.ru/**",
                    lambda route: route.fulfill(status=200, body=html, content_type="text/html"),
                )
                current = await editor.resume_sections(page, RESUME)
                assert current["jobs"][0]["company"] == "Тестовая компания"
                assert current["skills"] == ["Excel"]
                field = page.locator("textarea")
                await field.focus()
                proposal = {
                    "resume_id": RESUME,
                    "jobs": current["jobs"],
                    "duties": [
                        current["jobs"][0] | {"text": "• Готовил бюджет и сравнивал план и факт."}
                    ],
                    "skills": [{"name": "Бюджетирование"}],
                }
                with pytest.raises(ServiceError, match="именно выбранного"):
                    await editor.fill_change(
                        page, proposal | {"resume_id": "abcdef987654"}, "duties", 0
                    )
                await field.fill("Описание другой должности")
                with pytest.raises(ServiceError, match="отличается"):
                    await editor.fill_change(page, proposal, "duties", 0)
                await field.fill(DUTIES)
                await editor.fill_change(page, proposal, "duties", 0)
                assert await field.input_value() == proposal["duties"][0]["text"]
                skill = page.locator("input")
                await skill.focus()
                await editor.fill_change(page, proposal, "skills", 0)
                assert await skill.input_value() == "Бюджетирование"
                assert await page.locator("#save").get_attribute("data-pressed") is None
            finally:
                await browser.close()

    asyncio.run(run())


def test_403_stops_autoapply_and_check_only_reopens_the_affected_page(tmp_path, monkeypatch):
    async def run():
        config = SimpleNamespace(data_dir=tmp_path, browser_engine="chromium")
        db = Database(tmp_path)
        workflow = BrowserWorkflow(config, db)
        workflow.provider.state.touch()
        db.set_many(
            {"hh_session_verified": True, "browser_auto_enabled": True, "browser_search": True}
        )
        monkeypatch.setattr(workflow, "require_browser", lambda: None)
        url = "https://hh.ru/applicant/vacancy_response?vacancyId=101"
        error = hh.HHAccessBarrier("HTTP 403", status=403, url=url)
        workflow.record_access_barrier(error)
        assert not db.get("browser_auto_enabled")
        assert workflow.access_barrier()["step"] == "application"
        with pytest.raises(ServiceError, match="Проверить доступ HH"):
            workflow.start_search(send=True)

        @asynccontextmanager
        async def session(_seconds):
            yield None, SimpleNamespace(url=url)

        monkeypatch.setattr(workflow.provider, "session", session)
        probe = AsyncMock(side_effect=[error, SimpleNamespace(status=200)])
        monkeypatch.setattr(hh, "open_page", probe)
        assert not await workflow.check_access()
        assert workflow.access_barrier()["status"] == 403
        assert await workflow.check_access()
        assert probe.await_args.args[1] == url
        assert not workflow.access_barrier()
        assert not db.get("browser_auto_enabled")

    asyncio.run(run())
