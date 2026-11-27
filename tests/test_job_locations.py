import asyncio
import html
import json
import shutil
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest
from playwright.async_api import async_playwright

from app import hh_browser as hh
from app import job_sources

CITY = "Москва"
ADDRESS = "Москва, Тверская улица, 17"


def test_trudvsem_declared_coordinates_and_creation_date():
    row = {
        "id": "cb7acb48-73b4-11ec-b224-57fc951f3846",
        "job-name": "Финансовый менеджер",
        "region": {"name": CITY},
        "company": {"name": "Работодатель"},
        "vac_url": "https://trudvsem.ru/vacancy/card/123/cb7acb48-73b4-11ec-b224-57fc951f3846",
        "schedule": "Дистанционная работа",
        "creation-date": "2026-11-25",
        "addresses": {"address": [{"location": ADDRESS, "lat": "55.755", "lng": "37.612"}]},
    }
    _, details = job_sources.trudvsem_row({"vacancy": row})
    assert details["published"] == "2026-11-25"
    assert details["format"] == "Удалённо"
    assert details["location"] == {"lat": 55.755, "lon": 37.612, "address": ADDRESS}


@pytest.mark.parametrize(
    "entries",
    [
        [{"location": CITY, "lat": "0", "lng": "0"}],
        [{"location": CITY, "lat": "91", "lng": "32"}],
        [
            {"location": ADDRESS, "lat": "55.7", "lng": "37.6"},
            {"location": "Москва, Пушкинская улица, 20", "lat": "55.8", "lng": "37.8"},
        ],
    ],
)
def test_trudvsem_skips_centroids_invalid_points_and_multiple_workplaces(entries):
    assert job_sources.trudvsem_office({"address": entries}) == {"address": "", "location": None}


def test_superjob_formats_address_and_verified_publication_timestamp():
    row = {
        "id": 123,
        "profession": "Аналитик",
        "link": "https://www.superjob.ru/vakansii/analitik-123.html",
        "firm_name": "Работодатель",
        "town": {"title": CITY, "latitude": 55.755, "longitude": 37.617},
        "address": ADDRESS,
        "place_of_work": {"id": 1, "title": "на территории работодателя"},
        "date_published": 1795564800,
    }
    _, office = job_sources.superjob_row(row)
    assert office["published"] == "2026-11-25"
    assert office["format"] == "На месте работодателя"
    assert office["address"] == ADDRESS
    assert office["location"] is None
    _, remote = job_sources.superjob_row(row | {"place_of_work": {"id": 2}})
    assert remote["format"] == "Удалённо"
    assert remote["address"] == ""


def test_hh_metadata_uses_vacancy_pin_instead_of_metro_or_wrong_vacancy():
    chromium = (
        shutil.which("chromium-headless-shell")
        or shutil.which("chromium")
        or shutil.which("google-chrome")
    )
    if not chromium:
        pytest.skip("Chromium нужен для проверки публичной карточки HH.")

    async def run():
        async with async_playwright() as driver:
            browser = await driver.chromium.launch(executable_path=chromium)
            try:
                page = await browser.new_page()
                marker = {"lat": 55.753544, "lng": 37.621202}
                state = {
                    "vacancyView": {
                        "vacancyFull": {
                            "vacancy": {
                                "vacancyId": 12345,
                                "address": {
                                    "marker": marker,
                                    "displayName": "Москва, Красная площадь",
                                    "metroStations": {"lat": 55.752123, "lng": 37.610388},
                                },
                            }
                        }
                    }
                }
                exported = html.escape(json.dumps(state, ensure_ascii=False))
                await page.set_content(
                    '<div>Вакансия опубликована <time datetime="2026-11-25T13:40:22Z">25 ноября</time></div>'
                    f'<template id="HH-Lux-InitialState">{exported}</template>'
                )
                result = await hh.office_metadata(page, "12345")
                assert result["location"] == {
                    "lat": marker["lat"],
                    "lon": marker["lng"],
                    "address": "Москва, Красная площадь",
                }
                assert result["published"] == "2026-11-25"
                assert (await hh.office_metadata(page, "54321"))["location"] is None
            finally:
                await browser.close()

    asyncio.run(run())


def test_related_hh_queries_share_one_bounded_guest_session(monkeypatch):
    async def run():
        checked = []
        sessions = []
        details = []

        @asynccontextmanager
        async def session(duration, *, guest):
            sessions.append((duration, guest))
            yield None, "page"

        async def search(page, query):
            assert page == "page"
            checked.append(query)
            return {
                "Финансовый менеджер": ["1", "2", "3", "4", "5", "6", "10"],
                "Аналитик": ["1", "7", "9"],
                "Аудитор": ["4", "8", "11"],
            }[query]

        async def read(page, identifier):
            assert page == "page"
            details.append(identifier)
            return {"id": identifier}

        monkeypatch.setattr(job_sources, "source_url", lambda source, query, settings: query)
        monkeypatch.setattr(job_sources, "filter_reason", lambda vacancy, settings: "")
        monkeypatch.setattr(hh, "search_ids", search)
        monkeypatch.setattr(hh, "read_vacancy", read)
        browser = SimpleNamespace(
            require_browser=lambda: None,
            access_barrier=lambda: {},
            lock=asyncio.Lock(),
            provider=SimpleNamespace(session=session),
        )
        services = job_sources.JobSources(None, browser, None)
        settings = {
            "query": "Финансовый менеджер",
            "search_queries": ["Финансовый менеджер", "Аналитик", "Аудитор", "Разработчик"],
        }
        rows = await services.search_hh(settings)
        assert checked == ["Финансовый менеджер", "Аналитик", "Аудитор"]
        assert sessions == [(45, True)]
        assert details == ["1", "4", "2", "7", "8", "3"]
        assert [key for key, _ in rows] == details

    asyncio.run(run())
