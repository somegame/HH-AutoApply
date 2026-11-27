import json
import math
import re

import httpx

from app.services import ANALYSIS_SCHEMA, PROMPT, ProviderUnavailable, ServiceError, validate

BASE_URL = "https://generativelanguage.googleapis.com/v1beta/models/"
RETRY_SECONDS = re.compile(r"[0-9]{1,6}")
RETRY_DURATION = re.compile(r"[0-9]{1,6}(?:\.[0-9]{1,9})?s")
KEY_ERRORS = {
    "API_KEY_INVALID",
    "API_KEY_EXPIRED",
    "API_KEY_SERVICE_BLOCKED",
    "API_KEY_HTTP_REFERRER_BLOCKED",
    "API_KEY_IP_ADDRESS_BLOCKED",
}


def error_details(response):
    try:
        items = response.json()["error"]["details"]
    except (ValueError, TypeError, KeyError):
        return ()
    return (
        tuple(item for item in items if isinstance(item, dict)) if isinstance(items, list) else ()
    )


def retry_delay(response, details):
    header = response.headers.get("Retry-After", "").strip()
    delays = [int(header)] if RETRY_SECONDS.fullmatch(header) else []
    for item in details:
        duration = item.get("retryDelay")
        if (
            item.get("@type") == "type.googleapis.com/google.rpc.RetryInfo"
            and isinstance(duration, str)
            and RETRY_DURATION.fullmatch(duration)
        ):
            delays.append(math.ceil(float(duration[:-1])))
    return min(86400, max(30, max(delays, default=60)))


def failure(response):
    status = response.status_code
    details = error_details(response)
    if status == 429:
        return ProviderUnavailable(
            "Gemini ограничил частоту запросов (429).",
            retry_delay(response, details),
            provider="gemini",
        )
    if status in {408, 409} or status >= 500:
        return ProviderUnavailable(
            f"Gemini временно недоступен (HTTP {status}).", 60, provider="gemini"
        )
    key_error = status == 400 and any(
        item.get("@type") == "type.googleapis.com/google.rpc.ErrorInfo"
        and isinstance(item.get("reason"), str)
        and item.get("reason") in KEY_ERRORS
        for item in details
    )
    if status in {401, 403, 404} or key_error:
        return ProviderUnavailable(
            f"Gemini отклонил запрос (HTTP {status}). Проверьте ключ и модель.",
            900,
            provider="gemini",
        )
    return ServiceError(f"Gemini отклонил запрос (HTTP {status}).")


class Gemini:
    def __init__(self, client, config):
        self.client = client
        self.config = config

    async def complete(self, system, data, *, response_schema=None, max_tokens=3500):
        if not self.config.gemini_key:
            raise ServiceError("Требуется настройка GEMINI_API_KEY.")
        generation = {"responseMimeType": "application/json", "maxOutputTokens": max_tokens}
        if response_schema is not None:
            generation["responseJsonSchema"] = response_schema
        try:
            response = await self.client.post(
                f"{BASE_URL}{self.config.gemini_model}:generateContent",
                headers={"x-goog-api-key": self.config.gemini_key},
                json={
                    "systemInstruction": {"parts": [{"text": system}]},
                    "contents": [
                        {"role": "user", "parts": [{"text": json.dumps(data, ensure_ascii=False)}]}
                    ],
                    "generationConfig": generation,
                },
                timeout=18,
                follow_redirects=False,
            )
        except httpx.RequestError:
            raise ProviderUnavailable(
                "Gemini не ответил вовремя или недоступен по сети.", 60, provider="gemini"
            ) from None
        if not response.is_success:
            raise failure(response)
        try:
            candidate = response.json()["candidates"][0]
            if not isinstance(candidate, dict) or candidate.get("finishReason", "STOP") != "STOP":
                raise ValueError("incomplete candidate")
            parts = candidate["content"]["parts"]
            result = json.loads(
                "".join(
                    part["text"]
                    for part in parts
                    if isinstance(part, dict)
                    and not part.get("thought")
                    and isinstance(part.get("text"), str)
                )
            )
        except (KeyError, IndexError, TypeError, ValueError):
            raise ServiceError("Gemini не вернул полный ответ в формате JSON.") from None
        if not isinstance(result, dict):
            raise ServiceError("Gemini вернул результат вне схемы.")
        return result

    async def analyze(self, data):
        result = await self.complete(PROMPT, data, response_schema=ANALYSIS_SCHEMA)
        return validate(result, data["resume"])
