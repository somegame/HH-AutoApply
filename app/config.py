import os
import re
from dataclasses import dataclass
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv

from app.local_browser import public_login_url

VERSION = "6.6.0"
DEFAULT_GROQ_DAILY_LIMIT = 50
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_GEMINI_DAILY_LIMIT = 50
GEMINI_MODEL_ID = re.compile(r"[a-zA-Z0-9][a-zA-Z0-9._-]{0,99}")
RELATED = (
    "Финансовый аналитик;Финансовый контролёр;Экономист;Менеджер по финансам;"
    "Специалист по бюджетированию;Специалист по управленческой отчётности;"
    "FP&A Analyst;Finance Manager;Financial Analyst;Financial Controller"
)

# Labels are also used by the settings keyboard.
FIELDS = {
    "query": "Основная должность",
    "related": "Смежные запросы",
    "use_related": "Включить смежные запросы",
    "region": "Регион",
    "salary": "Минимальная зарплата",
    "currency": "Валюта",
    "format": "Формат работы",
    "schedule": "График",
    "experience": "Опыт",
    "score": "Минимальный общий балл анализа",
    "skills_score": "Совпадение по навыкам для автоотклика",
    "limit": "Суточный лимит отметок",
    "interval": "Интервал, мин",
    "employers": "Исключаемые работодатели",
    "excluded": "Исключаемые слова",
    "required": "Обязательные слова",
    "unknown_salary": "Включить вакансии без зарплаты",
    "tone": "Тон письма",
    "length": "Длина письма",
}
NUMBERS = {
    "salary": (0, 100_000_000),
    "score": (0, 100),
    "skills_score": (1, 100),
    "limit": (1, 100),
    "interval": (5, 1440),
}
BOOLEANS = {"use_related", "unknown_salary"}
OPTIONAL = {"related", "employers", "excluded", "required"}


def setting(key, value):
    value = value.strip()
    if key in NUMBERS:
        low, high = NUMBERS[key]
        if not value.isascii() or not value.isdigit() or len(value) > 9:
            raise ValueError(f"Введите целое число от {low} до {high}.")
        number = int(value)
        if not low <= number <= high:
            raise ValueError(f"Введите целое число от {low} до {high}.")
        return number
    if key in BOOLEANS:
        if value.casefold() not in {"да", "нет", "true", "false"}:
            raise ValueError("Введите «да» или «нет».")
        return value.casefold() in {"да", "true"}
    if key in OPTIONAL and value == "-":
        return ""
    if not value or len(value) > 400 or "\n" in value:
        raise ValueError("Введите до 400 символов одной строкой.")
    if key == "currency":
        value = value.upper()
        if len(value) != 3 or not value.isascii() or not value.isalpha():
            raise ValueError("Введите трёхбуквенный код валюты, например RUB.")
    if key == "length" and value not in {"краткое", "среднее"}:
        raise ValueError("Введите «краткое» или «среднее».")
    if key == "related" and (
        len(value.split(";")) > 12 or any(len(part.strip()) > 90 for part in value.split(";"))
    ):
        raise ValueError("До 12 запросов по 90 символов через точку с запятой.")
    return value


def ai_daily_limit(env, name, default):
    value = env.get(name, str(default)).strip()
    if not value.isascii() or not value.isdigit() or len(value) > 4 or not 1 <= int(value) <= 1000:
        raise ValueError(f"{name} должен быть целым числом от 1 до 1000.")
    return int(value)


@dataclass(frozen=True, repr=False)
class Config:
    token: str
    owner: int
    groq_key: str
    model: str
    data_dir: Path
    persistent: bool
    zone: ZoneInfo
    defaults: dict
    log_level: str
    browser_engine: str
    chromium_path: str
    login_public_url: str
    web_port: int
    auto_send: bool
    superjob_key: str = ""
    superjob_client_id: str = ""
    ors_key: str = ""
    groq_daily_limit: int = DEFAULT_GROQ_DAILY_LIMIT
    gemini_key: str = ""
    gemini_model: str = DEFAULT_GEMINI_MODEL
    gemini_daily_limit: int = DEFAULT_GEMINI_DAILY_LIMIT
    habr_client_id: str = ""
    habr_client_secret: str = ""

    @property
    def ai_configured(self):
        return bool(self.groq_key or self.gemini_key)

    @classmethod
    def load(cls):
        load_dotenv(override=False)
        env = os.environ
        token = next(
            (
                env[k].strip()
                for k in ("TELEGRAM_TOKEN", "TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
                if env.get(k, "").strip()
            ),
            "",
        )
        owner = env.get("OWNER_ID", "").strip()
        if not token or ":" not in token:
            raise ValueError("Укажите действительный TELEGRAM_TOKEN.")
        if len(owner) > 19 or not owner.isascii() or not owner.isdigit() or int(owner) < 1:
            raise ValueError("Укажите числовой Telegram ID в OWNER_ID.")
        try:
            zone = ZoneInfo(env.get("TIMEZONE", "Europe/Moscow"))
        except (ValueError, ZoneInfoNotFoundError):
            raise ValueError("Укажите действительный часовой пояс TIMEZONE.") from None
        defaults = dict(
            related=RELATED,
            use_related=False,
            format="любой",
            schedule="любой",
            experience="любой",
            employers="",
            excluded="",
            required="",
            unknown_salary=False,
            mode="confirm",
            tone="деловой, естественный",
            length="краткое",
        )
        for key, name, fallback in (
            ("query", "SEARCH_QUERY", "Финансовый менеджер"),
            ("region", "SEARCH_REGION", "Москва"),
            ("salary", "MIN_SALARY", "150000"),
            ("currency", "CURRENCY", "RUB"),
            ("score", "MIN_SCORE", "80"),
            ("skills_score", "MIN_SKILL_SCORE", "80"),
            ("limit", "DAILY_LIMIT", "100"),
            ("interval", "SEARCH_INTERVAL_MINUTES", "30"),
        ):
            try:
                defaults[key] = setting(key, env.get(name, fallback))
            except ValueError as exc:
                raise ValueError(f"{name}. {exc}") from None
        engine = env.get("BROWSER_ENGINE", "manual").strip().lower()
        if engine == "browserbase":
            engine = "manual"
        if engine not in {"manual", "chromium"}:
            raise ValueError("BROWSER_ENGINE должен быть manual или chromium.")
        login_url = public_login_url(env.get("LOGIN_PUBLIC_URL", "").strip())
        port = 8000
        if login_url:
            try:
                port = int(env.get("PORT", "").strip() or "8000")
            except ValueError:
                raise ValueError("PORT должен быть целым числом.") from None
            if not 1 <= port <= 65535:
                raise ValueError("PORT должен быть от 1 до 65535.")
        groq_daily_limit = ai_daily_limit(env, "GROQ_DAILY_LIMIT", DEFAULT_GROQ_DAILY_LIMIT)
        gemini_daily_limit = ai_daily_limit(env, "GEMINI_DAILY_LIMIT", DEFAULT_GEMINI_DAILY_LIMIT)
        gemini_model = (env.get("GEMINI_MODEL", "").strip() or DEFAULT_GEMINI_MODEL).removeprefix(
            "models/"
        )
        if not GEMINI_MODEL_ID.fullmatch(gemini_model):
            raise ValueError("GEMINI_MODEL должен содержать идентификатор модели.")
        return cls(
            token,
            int(owner),
            env.get("GROQ_API_KEY", "").strip(),
            env.get("GROQ_MODEL", "").strip() or "openai/gpt-oss-120b",
            Path(env.get("DATA_DIR", "./data")),
            env.get("PERSISTENT_STORAGE") == "true",
            zone,
            defaults,
            env.get("LOG_LEVEL", "INFO").upper(),
            engine,
            env.get("CHROMIUM_PATH", "/usr/bin/chromium-headless-shell"),
            login_url,
            port,
            env.get("AUTO_SEND", "false").strip().lower() in {"true", "1", "yes"},
            env.get("SUPERJOB_API_KEY", "").strip(),
            env.get("SUPERJOB_CLIENT_ID", "").strip(),
            env.get("ORS_API_KEY", "").strip(),
            groq_daily_limit,
            env.get("GEMINI_API_KEY", "").strip(),
            gemini_model,
            gemini_daily_limit,
            env.get("HABR_CLIENT_ID", "").strip(),
            env.get("HABR_CLIENT_SECRET", "").strip(),
        )
