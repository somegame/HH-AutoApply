import asyncio
import shutil
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.async_api import async_playwright

from app import hh_browser as hh


def test_apply_reveals_the_letter_and_checks_the_required_resume_in_the_known_form():
    chromium = (
        shutil.which("chromium-headless-shell")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
    )
    if not chromium:
        pytest.skip("Chromium нужен для проверки формы отклика.")
    number, resume = "1200456", "abcdef0123456789"
    letter = "Здравствуйте! Составлял бюджет и контролировал отклонения от плана."
    submitted = []

    async def serve(route):
        url = urlsplit(route.request.url)
        if url.path == f"/vacancy/{number}":
            if submitted:
                body = '<p data-qa="vacancy-response-link-view-response">Отклик отправлен</p>'
            else:
                body = (
                    '<a data-qa="vacancy-response-link-top" '
                    f'href="/applicant/vacancy_response/?vacancyId={number}">Откликнуться</a>'
                )
        elif url.path == "/applicant/vacancy_response/":
            body = f"""
            <form data-qa="vacancy-response-popup-form" action="/fixture-submit">
              <select required name="resumeId">
                <option value="">Выберите резюме</option>
                <option value="{resume}">Финансовый менеджер</option>
              </select>
              <button type="button" onclick="document.querySelector('textarea').hidden=false">
                Добавить сопроводительное письмо
              </button>
              <textarea hidden name="letter"></textarea>
              <button data-qa="vacancy-response-popup-submit-button">Отправить</button>
            </form>"""
        elif url.path == "/fixture-submit":
            submitted.append(parse_qs(url.query))
            body = "<p>Отклик отправлен</p>"
        else:
            return await route.abort()
        await route.fulfill(body=body, content_type="text/html; charset=utf-8")

    async def run():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=chromium)
            try:
                page = await browser.new_page()
                await page.route("**/*", serve)
                assert await hh.apply(page, number, resume, letter, lambda: True) == "sent"
            finally:
                await browser.close()

    asyncio.run(run())
    assert submitted == [{"resumeId": [resume], "letter": [letter]}]
