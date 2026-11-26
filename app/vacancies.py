import re
from urllib.parse import urlencode, urlsplit

HH = "https://hh.ru"
URL_FIELDS = {
    "title": "Должность",
    "company": "Компания",
    "region": "Регион",
    "salary": "Зарплата",
    "format": "Формат",
    "schedule": "График",
    "experience": "Опыт",
    "published": "Опубликована",
    "description": "Описание",
}
TEMPLATE = """Ссылка на вакансию
Должность:
Компания:
Регион:
Зарплата:
Формат:
График:
Опыт:
Опубликована:
Описание:"""


def terms(text):
    return [s.strip() for s in text.split(";") if s.strip()]


def vacancy_id(url):
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    match = re.fullmatch(r"/vacancy/([0-9]{1,18})/?", parsed.path)
    if (
        parsed.scheme != "https"
        or not (host == "hh.ru" or host.endswith(".hh.ru"))
        or parsed.username
        or parsed.password
        or not match
    ):
        raise ValueError("Нужна ссылка https://hh.ru/vacancy/…")
    return str(int(match[1]))


def parse_vacancy(text):
    if len(text) > 18000:
        raise ValueError("Вставьте до 18 000 символов, включая требования.")
    link, _, body = text.strip().partition("\n")
    identifier = vacancy_id(link.strip())
    values = {key: "" for key in URL_FIELDS}
    keys = {label.casefold(): key for key, label in URL_FIELDS.items()}
    description = []
    in_description = False
    for line in body.splitlines():
        label, sep, value = line.partition(":")
        key = keys.get(label.strip().casefold()) if sep else None
        if in_description:
            description.append(line)
        elif key == "description":
            in_description = True
            description.append(value.strip())
        elif key:
            if len(value.strip()) > 180:
                raise ValueError(f"Поле «{label}» слишком длинное.")
            values[key] = value.strip()
        elif line.strip():
            raise ValueError("Начните описание строкой «Описание:».")
    values["description"] = "\n".join(description).strip()
    if not values["title"] or not values["company"] or len(values["description"]) < 60:
        raise ValueError("Укажите должность, компанию и описание от 60 символов.")
    salary = values["salary"]
    if salary:
        match = re.fullmatch(
            r"(\d{1,9})(?:\s*[-–]\s*(\d{1,9}))?\s+([A-Za-z]{3})", salary.replace("\u00a0", " ")
        )
        if not match:
            raise ValueError("Зарплата в формате 150000-200000 RUB или 150000 RUB.")
        floor, ceiling = int(match[1]), int(match[2] or match[1])
        if not 0 < floor <= ceiling:
            raise ValueError("Проверьте границы зарплаты.")
        values.update(salary_min=floor, salary_max=ceiling, currency=match[3].upper())
    values["url"] = f"{HH}/vacancy/{identifier}"
    return identifier, values


def search_links(settings, *, order="publication_time", title_only=False):
    queries = [settings["query"]]
    if settings["use_related"]:
        queries.extend(terms(settings["related"]))
    for query in dict.fromkeys(queries):
        params = {
            "text": query,
            "currency_code": {"RUB": "RUR"}.get(settings["currency"], settings["currency"]),
            "salary": settings["salary"],
            "order_by": order,
        }
        if settings["region"].casefold() in {"москва", "moscow", "1"}:
            params["area"] = 1
        if title_only:
            params["search_field"] = "name"
        if not settings["unknown_salary"]:
            params["only_with_salary"] = "true"
        yield query, f"{HH}/search/vacancy?{urlencode(params)}"


def filter_reason(vacancy, settings):
    text = " ".join(vacancy[key] for key in ("title", "company", "description")).casefold()
    for key in ("region", "format", "schedule", "experience"):
        expected = settings[key].casefold()
        actual = vacancy[key].casefold()
        if expected != "любой" and expected not in actual:
            return f"{URL_FIELDS[key]} — сведения отсутствуют или отличаются от фильтра."
    if vacancy["company"].casefold() in {x.casefold() for x in terms(settings["employers"])}:
        return "Работодатель исключён."
    if any(word.casefold() in text for word in terms(settings["excluded"])):
        return "Найдено исключаемое слово."
    if any(word.casefold() not in text for word in terms(settings["required"])):
        return "Отсутствует обязательное слово."
    if not vacancy["salary"]:
        return None if settings["unknown_salary"] else "Зарплата не указана."
    if vacancy["currency"] != settings["currency"] or vacancy["salary_max"] < settings["salary"]:
        return "Валюта или верхняя граница зарплаты не соответствует фильтру."
    return None
