import asyncio
import json
from dataclasses import replace
from datetime import datetime
from io import BytesIO
from types import SimpleNamespace
from zipfile import ZipFile

import httpx
import pytest

import bot
from app.config import Config, setting
from app.database import Database
from app.resume import read_upload
from app.scheduler import Scheduler, local_day
from app.services import Groq, ServiceError, clean_text, validate
from app.telegram_ui import TelegramUI, allowed
from app.vacancies import filter_reason, parse_vacancy, search_links, vacancy_id

PROFESSIONAL = "Управленческая отчётность, бюджетирование, прогнозирование и планирование."
CARD = (
    "https://moscow.hh.ru/vacancy/1200?from=search\n"
    "Должность: Финансовый менеджер\nКомпания: Пример\nРегион: Москва\n"
    "Зарплата: 150000-220000 RUB\nОписание:\n" + PROFESSIONAL
)
RESULT = {
    "score": 90,
    "skills_score": 85,
    "duties": ["Готовить управленческие отчёты.", "Планировать бюджет."],
    "reason": "Есть подтверждённый навык.",
    "required": ["Бюджетирование"],
    "missing": [],
    "letter": "Здравствуйте! Могу помочь с бюджетированием.",
    "evidence": ["бюджетирование"],
}


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setattr("app.config.load_dotenv", lambda **_: None)
    monkeypatch.setenv("BROWSER_ENGINE", "manual")
    monkeypatch.setenv("OWNER_ID", "12345")
    monkeypatch.setenv("TELEGRAM_TOKEN", "12345:synthetic")
    monkeypatch.setenv("GROQ_API_KEY", "test-value")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    return Config.load()


def test_config(config, monkeypatch):
    assert config.zone.key == "Europe/Moscow"
    assert config.defaults["score"] == 80
    with pytest.raises(ValueError, match="1 до 100"):
        setting("limit", "101")
    monkeypatch.setenv("BROWSER_ENGINE", "browserbase")
    monkeypatch.setenv("PORT", "0")
    assert Config.load().browser_engine == "manual"
    monkeypatch.delenv("OWNER_ID")
    with pytest.raises(ValueError, match="OWNER_ID"):
        Config.load()


def test_search_and_filters(config):
    identifier, details = parse_vacancy(CARD)
    assert identifier == "1200"
    assert details["url"] == "https://hh.ru/vacancy/1200"
    assert len(list(search_links(config.defaults))) == 1
    assert len(list(search_links(config.defaults | {"use_related": True}))) == 11
    assert not filter_reason(details, config.defaults)
    assert filter_reason(details, config.defaults | {"salary": 230000})
    assert filter_reason(details, config.defaults | {"employers": "пример"})
    assert filter_reason(details, config.defaults | {"required": "ABC"})
    assert filter_reason(details | {"region": ""}, config.defaults)
    with pytest.raises(ValueError):
        vacancy_id("https://hh.ru.example/vacancy/1200")


def test_migration_and_restart(config):
    db = Database(config.data_dir)
    db.set("reminders", True)
    identifier, details = parse_vacancy(CARD)
    assert db.add(identifier, details)
    assert not db.add(identifier, details)
    rebuilt = Database(config.data_dir)
    assert rebuilt.get("reminders")
    assert rebuilt.pending()["id"] == identifier
    with db.connection() as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        connection.execute("PRAGMA user_version=2")
    with pytest.raises(RuntimeError, match="новее"):
        Database(config.data_dir)


def test_quota_and_transition(config):
    db = Database(config.data_dir)
    _, details = parse_vacancy(CARD)
    for identifier in ("1", "2"):
        db.add(identifier, details)
        db.finish_analysis(identifier, "approved", RESULT, RESULT["letter"])
    day = local_day(config)
    assert day == datetime.now(config.zone).date().isoformat()
    assert db.transition("1", ("approved",), "reported", day=day, limit=1) == "ok"
    assert db.transition("1", ("approved",), "reported", day=day, limit=2) == "stale"
    assert db.transition("2", ("approved",), "reported", day=day, limit=1) == "limit"
    assert db.vacancy("2")["status"] == "approved"
    assert db.counts(day)[1] == 1
    assert db.counts("1900-01-01")[1] == 0


@pytest.mark.parametrize(
    "uid,chat,expected",
    [
        (12345, "private", True),
        (98765, "private", False),
        (12345, "group", False),
        (None, "private", False),
    ],
)
def test_owner_guard(config, uid, chat, expected):
    update = SimpleNamespace(
        effective_user=SimpleNamespace(id=uid) if uid else None,
        effective_chat=SimpleNamespace(type=chat, id=uid),
    )
    assert allowed(update, config.owner) == expected
    if not expected:
        asyncio.run(TelegramUI(config, None, None, None).handle(update, None))


def test_schema_and_redaction():
    assert validate(RESULT, PROFESSIONAL) == RESULT
    for bad in (
        {"score": 100},
        RESULT | {"score": True},
        RESULT | {"skills_score": False},
        RESULT | {"skills_score": 101},
        RESULT | {"duties": [""]},
        RESULT | {"evidence": ["ложь"]},
    ):
        with pytest.raises(ServiceError):
            validate(bad, PROFESSIONAL)
    result = validate(RESULT | {"duties": ["Писать на name@example.org."]}, PROFESSIONAL)
    assert result["duties"] == []
    assert "example.org" not in clean_text("name@example.org")
    assert "555" not in clean_text("+7 (555) 123-45-67")


def test_resume_workers():
    assert asyncio.run(read_upload("x.txt", PROFESSIONAL.encode())) == PROFESSIONAL
    archive = BytesIO()
    with ZipFile(archive, "w") as doc:
        doc.writestr(
            "word/document.xml",
            (
                '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                f"<w:body><w:p><w:r><w:t>{PROFESSIONAL}</w:t></w:r></w:p></w:body></w:document>"
            ),
        )
    assert asyncio.run(read_upload("x.docx", archive.getvalue())) == PROFESSIONAL
    with pytest.raises(ValueError, match="текстовый слой"):
        asyncio.run(read_upload("x.pdf", b"invalid"))


def test_clients_and_scheduler(config):
    db = Database(config.data_dir)
    vacancy, details = parse_vacancy(CARD)
    db.add(vacancy, details)
    db.set("resume", PROFESSIONAL)
    calls = []
    messages = []

    def response(req):
        calls.append(req.url.path)
        if req.url.path.endswith("/models"):
            return httpx.Response(200, json={"data": [{"id": config.model}]})
        assert b"hh.ru" not in req.content
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(RESULT)}}]})

    async def send(*args, **kwargs):
        messages.append(args)

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            await Groq(client, config).check()
            scheduler = Scheduler(config, db, client)
            telegram = SimpleNamespace(send_message=send)
            await scheduler.tick(telegram)
            assert db.pending()
            db.set("groq_consent", True)
            db.set("processing", False)
            await scheduler.tick(telegram)
            assert db.pending()
            db.set("processing", True)
            await scheduler.tick(telegram)
            db.add("1300", details)
            await scheduler.tick(telegram)
            assert db.vacancy("1300")["status"] == "draft"
            assert len(bot.build(config, db, client).handlers[0]) == 2

    asyncio.run(run())
    assert calls.count("/openai/v1/chat/completions") == 1
    assert len(messages) == 2
    assert db.vacancy(vacancy)["status"] == "draft"


def test_groq_backoff_is_persisted(config):
    db = Database(config.data_dir)
    vacancy, details = parse_vacancy(CARD)
    db.add(vacancy, details)
    db.set("resume", PROFESSIONAL)
    db.set("groq_consent", True)

    calls = []

    async def run():
        def response(req):
            calls.append(req.url.path)
            return httpx.Response(429, headers={"Retry-After": "120"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            await Scheduler(config, db, client).tick(None)
            restarted = Scheduler(config, db, client)
            await restarted.tick(None)
            assert restarted.analysis_status()["groq_retry_at"] == db.get("next_groq")

    asyncio.run(run())
    assert len(calls) == 1
    assert db.pending()["id"] == vacancy
    assert db.vacancy(vacancy)["attempts"] == 0
    assert "429" in db.get("groq_notice")


def test_incomplete_groq_result_retries_without_blocking_other_jobs(config):
    db = Database(config.data_dir)
    _, details = parse_vacancy(CARD)
    db.add("1200", details)
    db.add("1300", details | {"title": "Финансовый аналитик"})
    db.set_many({"resume": PROFESSIONAL, "groq_consent": True})

    def response(request):
        payload = json.loads(request.content)
        data = json.loads(payload["messages"][1]["content"])
        result = RESULT if data["vacancy"]["title"] == "Финансовый аналитик" else {"score": 99}
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})

    async def send(*args, **kwargs):
        pass

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            scheduler = Scheduler(config, db, client)
            telegram = SimpleNamespace(send_message=send)
            await scheduler.tick(telegram)
            assert db.vacancy("1200")["attempts"] == 1
            assert db.pending()["id"] == "1300"
            db.set("next_groq", 0)
            await scheduler.tick(telegram)

    asyncio.run(run())
    assert db.vacancy("1200")["status"] == "queued"
    assert db.vacancy("1300")["status"] == "draft"


def test_discovered_job_retains_groq_duties_when_excluded_by_filters(config):
    db = Database(config.data_dir)
    identifier, details = parse_vacancy(CARD)
    db.add(identifier, details | {"source": "mini_hh", "auto_discovered": True})
    db.set_many(
        {
            "resume": PROFESSIONAL,
            "groq_consent": True,
            "settings": {"salary": 300000},
        }
    )

    async def run():
        def response(_request):
            return httpx.Response(
                200, json={"choices": [{"message": {"content": json.dumps(RESULT)}}]}
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            await Scheduler(config, db, client).tick(None)

    asyncio.run(run())
    row = db.vacancy(identifier)
    result = json.loads(row["result"])
    assert row["status"] == "filtered"
    assert result["duties"] == RESULT["duties"]
    assert result["skills_score"] == RESULT["skills_score"]
    assert result["filter_reason"] and not row["letter"]


def test_groq_queue_resumes_automatically_the_next_day(config, monkeypatch):
    config = replace(config, groq_daily_limit=1)
    db = Database(config.data_dir)
    _, details = parse_vacancy(CARD)
    db.add("1200", details | {"auto_discovered": True})
    db.add("1300", details | {"title": "Финансовый аналитик", "auto_discovered": True})
    db.set_many({"resume": PROFESSIONAL, "groq_consent": True})
    monkeypatch.setattr("app.scheduler.local_day", lambda _config: "2026-11-27")

    def response(_request):
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(RESULT)}}]})

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            scheduler = Scheduler(config, db, client)
            await scheduler.tick(None)
            db.set("next_groq", 0)
            await scheduler.tick(None)
            assert db.vacancy("1300")["status"] == "queued"
            monkeypatch.setattr("app.scheduler.local_day", lambda _config: "2026-11-28")
            await scheduler.tick(None)

    asyncio.run(run())
    assert db.vacancy("1300")["status"] == "draft"


def test_telegram_polling_owner_flow(config):
    from telegram.request import BaseRequest

    db = Database(config.data_dir)
    outputs = []
    pending_updates = [
        {
            "update_id": index,
            "message": {
                "message_id": index,
                "date": 1710000000,
                "text": text,
                "chat": {"id": owner, "type": "private"},
                "from": {"id": owner, "is_bot": False, "first_name": "Тест"},
            },
        }
        for index, owner, text in [
            (1, 98765, "/run"),
            (2, 12345, "/start"),
            (3, 12345, "/run"),
            (4, 12345, "/stop"),
        ]
    ]

    class TelegramTransport(BaseRequest):
        @property
        def read_timeout(self):
            return 1.0

        async def initialize(self):
            pass

        async def shutdown(self):
            pass

        async def do_request(self, url, method, request_data=None, **kwargs):
            api = url.rsplit("/", 1)[-1]
            parameters = request_data.parameters if request_data else {}
            if api == "getMe":
                result = {"id": 12345, "is_bot": True, "first_name": "Тест", "username": "test_bot"}
            elif api == "getUpdates":
                await asyncio.sleep(0.01)
                result, pending_updates[:] = pending_updates[:], []
            elif api == "sendMessage":
                outputs.append(parameters)
                result = {
                    "message_id": 1,
                    "date": 1710000000,
                    "text": parameters["text"],
                    "chat": {"id": parameters["chat_id"], "type": "private"},
                }
            else:
                result = True
            return 200, json.dumps({"ok": True, "result": result}).encode()

    async def run():
        async with httpx.AsyncClient() as client:
            app = bot.build(config, db, client, telegram_request=TelegramTransport())
            async with app:
                await app.post_init(app)
                await app.updater.start_polling()
                await app.start()
                try:
                    async with asyncio.timeout(4):
                        while len(outputs) < 3:
                            await asyncio.sleep(0.01)
                finally:
                    await app.updater.stop()
                    await app.stop()
                    await app.post_stop(app)

    asyncio.run(run())
    assert [item["chat_id"] for item in outputs] == [12345] * 3
    assert outputs[-1]["text"] == "Напоминания и анализ остановлены."
    assert not Database(config.data_dir).get("processing")


def test_shared_hh_vacancy_reaches_ai_and_preserves_confirmed_status(config):
    from telegram import Message

    config = replace(config, login_public_url="https://bot.example.test")
    db = Database(config.data_dir)
    db.set_many({"resume": PROFESSIONAL, "groq_consent": True, "browser_auto_enabled": True})
    requests = []
    replies = []

    def provider(request):
        requests.append(request.url.path)
        assert request.url.host == "api.groq.com"
        assert b"hh.ru" not in request.content
        return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(RESULT)}}]})

    async def reply(text, **options):
        replies.append((text, options["reply_markup"]))

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(provider)) as client:
            scheduler = Scheduler(config, db, client)
            ui = TelegramUI(config, db, SimpleNamespace(start=scheduler.tick_analysis), client)
            telegram = SimpleNamespace(send_message=reply_notice)

            async def share(text, *, owner=config.owner, entities=()):
                msg = Message.de_json(
                    {
                        "message_id": 1,
                        "date": 1710000000,
                        "text": text,
                        "entities": list(entities),
                        "chat": {"id": owner, "type": "private"},
                        "from": {"id": owner, "is_bot": False, "first_name": "Пример"},
                    },
                    None,
                )
                update = SimpleNamespace(
                    message=msg,
                    effective_message=SimpleNamespace(reply_text=reply),
                    effective_user=msg.from_user,
                    effective_chat=msg.chat,
                    callback_query=None,
                    get_bot=lambda: telegram,
                )
                await ui.handle(update, None)
                return update

            url = "https://moscow.hh.ru/vacancy/01200?from=share"
            await share(url, owner=98765)
            assert not db.vacancy("1200")
            await share("https://hh.ru.example.test/vacancy/1200")
            await share("https://hh.ru:8080/vacancy/1200")
            assert not db.vacancy("1200")
            update = await share(url)
            assert db.vacancy("1200")["status"] == "deferred"
            assert not requests
            keys = [key for row in replies[-1][1].inline_keyboard for key in row]
            assert any(key.callback_data == "inbox:1200" for key in keys)
            assert any(
                key.web_app and key.web_app.url == "https://bot.example.test/mini?tab=jobs"
                for key in keys
            )
            await ui.route(update, "inbox:1200")
            await share("Текст слишком короткий")
            assert not requests
            await share(PROFESSIONAL)
            draft = db.vacancy("1200")
            assert draft["status"] == "draft"
            assert draft["letter"] == RESULT["letter"]
            details = json.loads(draft["details"])
            assert details["url"] == "https://hh.ru/vacancy/1200"
            assert details["source"] == "hh"
            assert not details.get("auto_discovered")
            db.transition("1200", ("draft",), "reported")
            await share(url)
            assert db.vacancy("1200")["status"] == "reported"
            assert db.vacancy("1200")["letter"] == draft["letter"]

            db.set("processing", False)
            await share(
                "Бухгалтер\n" + PROFESSIONAL,
                entities=[
                    {
                        "type": "text_link",
                        "offset": 0,
                        "length": 9,
                        "url": "https://hh.ru/vacancy/1300",
                    }
                ],
            )
            assert db.vacancy("1300")["status"] == "queued"
            assert not db.get("processing")
            assert json.loads(db.vacancy("1300")["details"])["description"] == PROFESSIONAL
            await share(f"{PROFESSIONAL}\nhttps://hh.ru/vacancy/1400\nhttps://hh.ru/vacancy/1500")
            assert all(db.vacancy(job)["status"] == "deferred" for job in ("1400", "1500"))

    async def reply_notice(*args, **kwargs):
        pass

    asyncio.run(run())
    assert requests == ["/openai/v1/chat/completions"]


def test_pdf_text_layer():
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(400, 200)

    def name(text):
        return NameObject("/" + text)

    page[name("Resources")] = DictionaryObject(
        {
            name("Font"): DictionaryObject(
                {
                    name("F1"): DictionaryObject(
                        {
                            name("Type"): name("Font"),
                            name("Subtype"): name("Type1"),
                            name("BaseFont"): name("Helvetica"),
                        }
                    )
                }
            ),
        }
    )
    content = DecodedStreamObject()
    text = "Finance manager skilled in reporting budgeting and long term cash flow forecasting"
    content.set_data(f"BT /F1 10 Tf 20 90 Td ({text}) Tj ET".encode())
    page.replace_contents(content)
    stream = BytesIO()
    writer.write(stream)
    assert asyncio.run(read_upload("example.pdf", stream.getvalue())) == text


def test_iphone_login_continues_and_settings_are_grouped(config):
    db = Database(config.data_dir)
    ui = TelegramUI(config, db, None, None)
    messages = []

    async def reply(text, **kwargs):
        messages.append((text, kwargs["reply_markup"].inline_keyboard))

    update = SimpleNamespace(effective_message=SimpleNamespace(reply_text=reply))

    async def run():
        await ui.route(update, "login")
        text, keys = messages[-1]
        assert "закройте браузер" in text
        assert "finish" in {item.callback_data for row in keys for item in row}
        await ui.route(update, "finish")
        assert "личный кабинет" in messages[-1][0]
        assert [row[0].callback_data for row in messages[-1][1]] == ["search", "hh_auto", "start"]
        assert not db.get("resume")
        assert not db.get("groq_consent")
        await ui.route(update, "search")
        assert "Новые вакансии будет присылать HH" in messages[-1][0]
        await ui.route(update, "hh_auto")
        assert "подписку" in messages[-1][0]
        assert messages[-1][1][0][0].url == "https://hh.ru/"

        await ui.route(update, "settings")
        assert all(row[0].callback_data.startswith("settings:") for row in messages[-1][1][:-1])
        await ui.route(update, "settings:search")
        assert "Москва" in messages[-1][0]
        await ui.route(update, "set:region")
        await ui.text(update, "Казань")
        assert "Казань" in messages[-1][0]
        assert db.settings(config)["region"] == "Казань"

    asyncio.run(run())
