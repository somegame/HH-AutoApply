import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from app.config import DEFAULT_GEMINI_MODEL, Config
from app.gemini import BASE_URL, Gemini
from app.services import ProviderUnavailable, ServiceError

EVIDENCE = "Сверяю бюджеты и готовлю ежемесячные отчёты."
ANALYSIS = {
    "score": 92,
    "skills_score": 96,
    "duties": ["Готовить отчёты"],
    "reason": "Опыт соответствует задачам.",
    "required": ["Бюджетирование"],
    "missing": [],
    "letter": EVIDENCE,
    "evidence": [EVIDENCE],
}


def run_analysis(handler):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            config = SimpleNamespace(
                gemini_key="synthetic-gemini", gemini_model=DEFAULT_GEMINI_MODEL
            )
            return await Gemini(client, config).analyze({"resume": EVIDENCE})

    return asyncio.run(run())


def candidate(value, *, finish_reason="STOP"):
    output = json.dumps(value, ensure_ascii=False)
    return {
        "candidates": [
            {
                "finishReason": finish_reason,
                "content": {
                    "parts": [
                        {"text": "additional thought", "thought": True},
                        {"text": output[:12]},
                        {"text": output[12:]},
                    ]
                },
            }
        ]
    }


def test_gemini_analysis_uses_header_and_json_schema():
    def handler(request):
        assert request.method == "POST"
        assert str(request.url) == BASE_URL + DEFAULT_GEMINI_MODEL + ":generateContent"
        assert request.headers["x-goog-api-key"] == "synthetic-gemini"
        payload = json.loads(request.content)
        assert payload["systemInstruction"]["parts"][0]["text"]
        assert json.loads(payload["contents"][0]["parts"][0]["text"]) == {"resume": EVIDENCE}
        generation = payload["generationConfig"]
        assert generation["responseMimeType"] == "application/json"
        assert set(generation["responseJsonSchema"]["required"]) == set(ANALYSIS)
        return httpx.Response(200, json=candidate(ANALYSIS))

    assert run_analysis(handler) == ANALYSIS


def test_gemini_does_not_accept_unsupported_evidence_or_partial_output():
    invalid = ANALYSIS | {"evidence": ["Новая неподтверждённая должность"]}
    with pytest.raises(ServiceError, match="цитатами"):
        run_analysis(lambda _: httpx.Response(200, json=candidate(invalid)))
    with pytest.raises(ServiceError, match="полный ответ"):
        run_analysis(
            lambda _: httpx.Response(200, json=candidate(ANALYSIS, finish_reason="SAFETY"))
        )


@pytest.mark.parametrize(
    ("status", "details", "retry"),
    [
        (
            429,
            [{"@type": "type.googleapis.com/google.rpc.RetryInfo", "retryDelay": "93.4s"}],
            94,
        ),
        (503, [], 60),
        (
            400,
            [{"@type": "type.googleapis.com/google.rpc.ErrorInfo", "reason": "API_KEY_INVALID"}],
            900,
        ),
    ],
)
def test_gemini_retry_classification_does_not_expose_provider_body(status, details, retry):
    response = httpx.Response(
        status,
        headers={"Retry-After": "31"},
        json={"error": {"message": "untrusted provider response", "details": details}},
    )
    with pytest.raises(ProviderUnavailable) as error:
        run_analysis(lambda _: response)
    assert error.value.retry == retry
    assert error.value.provider == "gemini"
    assert "untrusted provider response" not in str(error.value)


def test_gemini_config_is_optional_and_rejects_model_paths(monkeypatch):
    monkeypatch.setattr("app.config.load_dotenv", lambda **_: None)
    monkeypatch.setenv("TELEGRAM_TOKEN", "42:test")
    monkeypatch.setenv("OWNER_ID", "12")
    monkeypatch.delenv("GROQ_API_KEY", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "temporary-gemini")
    monkeypatch.setenv("GEMINI_MODEL", "models/gemini-3.5-flash-lite")
    monkeypatch.setenv("GEMINI_DAILY_LIMIT", "28")
    config = Config.load()
    assert config.ai_configured
    assert config.gemini_model == DEFAULT_GEMINI_MODEL
    assert config.gemini_daily_limit == 28
    assert "temporary-gemini" not in repr(config)
    monkeypatch.setenv("GEMINI_MODEL", "https://external.example")
    with pytest.raises(ValueError, match="GEMINI_MODEL"):
        Config.load()
