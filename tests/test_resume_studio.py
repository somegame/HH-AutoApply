import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.resume_studio import build_resume, has_resume_evidence
from app.services import ServiceError

CONFIG = SimpleNamespace(model="openai/gpt-oss-120b", groq_key="synthetic")
TARGET = "Финансовый менеджер"
RESUME = (
    "Дата рождения: 1999-01-02\n"
    "Контактные данные: candidate@example.test\n"
    "Опыт работы\n"
    "ООО Вектор\n"
    "2020-2023\n"
    "Готовил бюджет подразделения и управленческую отчётность для руководства.\n"
    "\nКлючевые навыки\n"
    "Excel и Power Query\n"
    "\nОбразование\n"
    "Бакалавриат по экономике."
)
FACTS = "Сократил время подготовки ежемесячного отчёта с пяти до двух рабочих дней."


def reply(result):
    return httpx.Response(200, json={"choices": [{"message": {"content": json.dumps(result)}}]})


def test_resume_studio_uses_only_full_confirmed_facts_and_protects_contacts():
    def response(request):
        assert request.url.path == "/openai/v1/chat/completions"
        assert request.headers["Authorization"] == "Bearer synthetic"
        payload = json.loads(request.content)
        assert payload["model"] == CONFIG.model
        assert b"candidate@example.test" not in request.content
        assert b"1999-01-02" not in request.content
        assert b"trace_id" not in request.content
        assert b"do-not-copy-this" not in request.content
        data = json.loads(payload["messages"][1]["content"])
        assert data["target"] == TARGET and data["mode"] == "improve"
        assert data["vacancy_sample"] == [{"title": TARGET, "required": ["SAP FI"]}]
        entries = data["evidence"]
        assert entries[0]["quote"].startswith("ООО Вектор\n2020–2023\nГотовил бюджет")
        assert entries[-1]["quote"] == FACTS
        return reply(
            {
                "sections": {"achievements": [entries[-1]["id"]], "skills": [entries[1]["id"]]},
                "suggestions": ["Уточните, сколько подразделений входило в бюджет."],
                "questions": ["Скольким руководителям вы представляли отчёты?"],
            }
        )

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            return await build_resume(
                client,
                CONFIG,
                resume=RESUME,
                target=TARGET,
                facts=FACTS,
                vacancy_sample=[
                    {
                        "title": TARGET,
                        "required": ["SAP FI", "manager@example.test"],
                        "trace_id": "do-not-copy-this",
                    }
                ],
                mode="improve",
            )

    result = asyncio.run(run())
    assert result["text"].startswith("Целевая должность · " + TARGET)
    assert "ООО Вектор\n2020–2023\nГотовил бюджет" in result["text"]
    assert "Достижения и проекты\n• " + FACTS in result["text"]
    assert "Образование и сертификаты\n• Бакалавриат по экономике." in result["text"]
    assert "SAP FI" not in result["text"]
    assert result["suggestions"] and result["questions"]


@pytest.mark.parametrize("facts", ["", "Бюджетирование и отчётность."])
def test_quick_questions_when_there_is_not_yet_enough_input(facts):
    assert not has_resume_evidence("", facts)

    def unexpected_request(_request):
        pytest.fail("Insufficient facts must not consume Groq requests")

    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(unexpected_request)) as client:
            return await build_resume(
                client,
                CONFIG,
                resume="",
                target=TARGET,
                facts=facts,
                vacancy_sample=[],
                mode="build",
            )

    result = asyncio.run(run())
    assert result["questions"] and result["suggestions"] == []
    assert bool(result["text"]) == bool(facts)
    if facts:
        assert facts in result["text"]


def test_builds_without_existing_resume_and_rejects_imaginary_evidence():
    facts = "Участвовала в анализе затрат и самостоятельно собирала отчёт для отдела продаж."
    assert has_resume_evidence("", facts)

    async def run():
        def response(request):
            data = json.loads(json.loads(request.content)["messages"][1]["content"])
            assert data["mode"] == "build"
            assert data["evidence"][0]["quote"] == facts
            return reply({"sections": {"experience": [2]}, "suggestions": [], "questions": []})

        async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
            return await build_resume(
                client,
                CONFIG,
                resume="",
                target=TARGET,
                facts=facts,
                vacancy_sample=[],
                mode="build",
            )

    with pytest.raises(ServiceError, match="некорректную структуру"):
        asyncio.run(run())
