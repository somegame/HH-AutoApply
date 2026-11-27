"""Explainable vacancy comparison. These scores never control application sending."""

import hashlib
import math
import re
from collections import Counter
from datetime import date, datetime

WEIGHTS = {"fit": 45, "salary": 20, "commute": 20, "format": 10, "freshness": 5}
LABELS = {
    "fit": "Подходит по резюме",
    "salary": "Зарплата",
    "commute": "Дорога",
    "format": "Формат работы",
    "freshness": "Свежесть",
}
FORMAT_LABELS = {"remote": "удалённо", "hybrid": "гибрид", "office": "офис"}
FORMATS = {
    "remote": re.compile(r"\b(?:удален\w*|дистанцион\w*|remote)\b"),
    "hybrid": re.compile(r"\b(?:гибрид\w*|смешанн\w*|hybrid)\b"),
    "office": re.compile(r"\b(?:офис\w*|очно|на месте работодателя|on.?site|office)\b"),
}
WORDS = re.compile(r"[a-zа-я0-9]+(?:[+#]+\w*)?")
STOPWORDS = frozenset(
    """без более быть вам вас ваш ваша ваши весь все всего года даже для если еще
    или иметь как какая какие какой когда кто либо лишь лучший меня могут можно
    наша наше наши наш находимся надо нами нас нам нее них ничего очень пока после
    при про ранее ряд сам самая свое свои свой свои так там тех того тоже три уже
    хотя часто чем через что это этой этот эту любой работа работать рабочий
    рабочая работодатель компания команда вакансия должность кандидат соискатель
    предлагаем условия обязанности требования необходимо необходим будет требуется
    опыт знание навык навыки умение уровень высокая высокий возможность проект
    предоставляем график оформление рублей зарплата заработная плата трудовой
    полный день специалист сотрудник сфера стать работать ищем группа рынке
    the and are for with from have this that will you your our about into
    work experience company must role team years full part job vacancy candidate""".split()
)
RUSSIAN_ENDINGS = (
    "иями",
    "ями",
    "ами",
    "ого",
    "его",
    "ому",
    "ему",
    "ыми",
    "ими",
    "ая",
    "яя",
    "ое",
    "ее",
    "ый",
    "ий",
    "ой",
    "ым",
    "им",
    "ых",
    "их",
    "ую",
    "юю",
    "ые",
    "ие",
    "ом",
    "ем",
    "ов",
    "ев",
    "ам",
    "ям",
    "ах",
    "ях",
    "ов",
    "ия",
    "ию",
    "ии",
    "ы",
    "и",
    "а",
    "я",
    "у",
    "ю",
    "е",
)
TEMP_ROLES = frozenset({"работодатель не указан", "компания не указана", "скрытый работодатель"})
ORDERS = frozenset({"best", "salary", "commute", "fit", "fresh"})
REVIEW_STATES = {None, "queued", "draft", "approved", "deferred"}


def normalized(text):
    return " ".join(str(text or "").casefold().replace("ё", "е").split())


def work_formats(text):
    """Recognize explicit work modes only. Description text may merely mention them."""
    value = normalized(text)
    return {name for name, pattern in FORMATS.items() if pattern.search(value)}


def valid_number(value, low, high):
    return type(value) in {int, float} and low <= value <= high


def _terms(text):
    words = []
    for word in WORDS.findall(normalized(text)[:12000]):
        if len(word) < 2 or word.isdecimal() or word in STOPWORDS:
            continue
        if re.fullmatch(r"[а-я]+", word):
            for suffix in RUSSIAN_ENDINGS:
                if len(word) - len(suffix) >= 4 and word.endswith(suffix):
                    word = word[: -len(suffix)]
                    break
        if word not in STOPWORDS:
            words.append(word)
    return words


def _fit(job, resume, analysis):
    if not isinstance(resume, str) or not resume.strip():
        return None, "none", "Добавьте резюме для оценки.", 0
    if (
        isinstance(analysis, dict)
        and analysis.get("resume_hash") == hashlib.sha256(resume.encode()).hexdigest()
    ):
        score = analysis.get("score")
        if type(score) is int and 0 <= score <= 100:
            missing = analysis.get("missing")
            provider = "gemini" if analysis.get("analysis_provider") == "gemini" else "groq"
            name = "Gemini" if provider == "gemini" else "Groq"
            detail = f"Оценка {name} по актуальному резюме."
            missing_count = len(missing) if isinstance(missing, list) else 0
            if missing_count:
                detail += f" Не подтверждено обязательных требований: {missing_count}."
            return score, provider, detail, missing_count
    resume_terms = set(_terms(resume))
    title_terms = set(_terms(job.get("title")))
    description_terms = {
        word for word, _ in Counter(_terms(job.get("description"))).most_common(18)
    } - title_terms
    if not resume_terms or not (title_terms or description_terms):
        return None, "none", "Недостаточно текста для оценки.", 0
    matched = (title_terms | description_terms) & resume_terms
    groups = [(title_terms, 60), (description_terms, 40)]
    points = sum(
        weight * len(group & resume_terms) / len(group) for group, weight in groups if group
    )
    weight = sum(weight for group, weight in groups if group)
    return (
        round(85 * points / weight),
        "estimate",
        f"Предварительная оценка по словам в резюме. Совпадений: {len(matched)}.",
        0,
    )


def _currency(value):
    code = str(value or "").strip().upper()
    return "RUB" if code == "RUR" else code


def _salary(job, prefs):
    low, high = job.get("salary_min"), job.get("salary_max")
    low = low if valid_number(low, 1, 10**9) else None
    high = high if valid_number(high, 1, 10**9) else None
    if (low is None and high is None) or (low and high and low > high):
        return None, "Размер зарплаты неизвестен."
    currency = _currency(job.get("currency"))
    target_currency = _currency(prefs.get("currency", "RUB"))
    if not currency or currency != target_currency:
        return None, "Зарплата в другой или неизвестной валюте."
    goal = prefs.get("salary_goal") or prefs.get("salary")
    if not valid_number(goal, 1, 10**9):
        return None, "Укажите желаемую зарплату для сравнения."
    reference = (low + high) / 2 if low and high else low or high
    score = min(100 if low else 70, round(80 * reference / goal))
    amount = f"{round(reference):,}".replace(",", " ")
    target = f"{round(goal):,}".replace(",", " ")
    meaning = "Середина диапазона" if low and high and low != high else "Заявленная сумма"
    if not low:
        meaning = "Верхняя граница"
    return score, f"{meaning} {amount} {currency} при цели {target} {currency}."


def _commute(commute, prefs):
    if not isinstance(commute, dict):
        return None, "Нет данных для расчёта дороги."
    minutes = commute.get("minutes")
    kind = commute.get("kind")
    if kind == "remote":
        return 100, "Удалённая работа. Ежедневная поездка не нужна."
    if not valid_number(minutes, 0, 1440):
        return None, "Нет данных для расчёта дороги."
    target = prefs.get("max_commute")
    target = target if valid_number(target, 1, 360) else 45
    score = max(0, round(100 - 40 * minutes / target))
    prefix = "По маршруту" if kind in {"route", "routed"} else "Ориентировочно"
    return score, f"{prefix} {round(minutes)} мин в одну сторону. Цель до {round(target)} мин."


def _format(job, prefs):
    reported = work_formats(job.get("format"))
    preferred = prefs.get("preferred_format") or prefs.get("format")
    wanted = (
        {preferred}
        if isinstance(preferred, str) and preferred in FORMATS
        else work_formats(preferred)
    )
    options = ", ".join(label for mode, label in FORMAT_LABELS.items() if mode in reported)
    if not wanted:
        return None, f"Указано: {options}." if options else "Формат не указан.", False
    if not reported:
        return None, "Формат не указан.", True
    score = 100 if wanted & reported else 0
    return score, f"Указано: {options}.", True


def _publication(value):
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if not isinstance(value, str) or not value:
        return None
    try:
        return datetime.fromisoformat(value).date()
    except ValueError:
        return None


def _freshness(job, today):
    published = _publication(job.get("published"))
    if not published or published > today:
        return None, "Дата публикации неизвестна."
    age = (today - published).days
    return max(
        0, 100 - age * 4
    ), f"Опубликовано {age} дн. назад." if age else "Опубликовано сегодня."


def rank_job(job, prefs, resume, analysis=None, commute=None, *, today=None):
    """Compare known factors. Return data coverage separately from the average.

    Fit, salary, road, work format and age weigh 45/20/20/10/5. Salary is
    inactive without a goal, format is inactive without a preference, and
    unknown factors contribute nothing to score or known coverage. The priority
    used for ordering discounts the comparable score when little is known.
    """
    fit, fit_kind, fit_detail, missing_required = _fit(job, resume, analysis)
    fit_for_ranking = min(fit, max(0, 70 - 8 * (missing_required - 1))) if missing_required else fit
    salary, salary_detail = _salary(job, prefs)
    road, road_detail = _commute(commute, prefs)
    mode, mode_detail, mode_active = _format(job, prefs)
    fresh, fresh_detail = _freshness(job, today or date.today())
    values = {
        "fit": (fit_for_ranking, fit_detail, True),
        "salary": (
            salary,
            salary_detail,
            valid_number(prefs.get("salary_goal") or prefs.get("salary"), 1, 10**9),
        ),
        "commute": (road, road_detail, True),
        "format": (mode, mode_detail, mode_active),
        "freshness": (fresh, fresh_detail, True),
    }
    total_weight = sum(WEIGHTS[key] for key, (_, _, active) in values.items() if active)
    known_weight = sum(
        WEIGHTS[key] for key, (score, _, active) in values.items() if active and score is not None
    )
    points = sum(
        WEIGHTS[key] * score
        for key, (score, _, active) in values.items()
        if active and score is not None
    )
    score = round(points / known_weight) if known_weight else None
    coverage = round(100 * known_weight / total_weight)
    priority = round(score * (0.6 + 0.4 * coverage / 100), 2) if score is not None else None
    return {
        "score": score,
        "fit": fit,
        "fit_kind": fit_kind,
        "missing_required": missing_required,
        "coverage": coverage,
        "priority": priority,
        "factors": [
            {"key": key, "label": LABELS[key], "score": score, "detail": detail}
            for key, (score, detail, _) in values.items()
        ],
    }


def sort_ranked(jobs, order):
    order = order if order in ORDERS else "best"

    def descending(value):
        return -value if valid_number(value, 0, 10**9) else math.inf

    def sort_key(job):
        rank = job.get("rank") or {}
        commute = job.get("commute") or {}
        publication = _publication(job.get("published"))
        minutes = commute.get("minutes")
        modes = {
            "best": descending(rank.get("priority", rank.get("score"))),
            "salary": descending(
                job.get("salary_sort", job.get("salary_max") or job.get("salary_min"))
            ),
            "commute": minutes if valid_number(minutes, 0, 1440) else math.inf,
            "fit": descending(rank.get("fit")),
            "fresh": descending(publication.toordinal() if publication else None),
        }
        return (
            modes[order],
            modes["best"],
            descending(rank.get("coverage")),
            normalized(job.get("title")),
            str(job.get("id", "")),
        )

    return sorted(jobs, key=sort_key)


def mark_top(jobs):
    """Mark up to three options worth reviewing, including in the saved queue."""
    top = 0
    for job in sort_ranked(jobs, "best"):
        rank = job["rank"]
        job["top"] = (
            top < 3
            and job.get("status") in REVIEW_STATES
            and (rank["score"] or 0) >= 75
            and rank["coverage"] >= 60
            and not rank["missing_required"]
        )
        top += job["top"]
    return top


def duplicate_key(job):
    """Keep positions with unknown employers or distinct addresses apart."""
    title = normalized(job.get("title"))
    company = normalized(job.get("company"))
    region = normalized(job.get("region"))
    company = re.sub(r"^(?:ооо|ао|пао|ип)\s+", "", company).strip(' «»"')
    if not title or not company or company in TEMP_ROLES:
        return None
    location = job.get("location") or {}
    if isinstance(location, dict):
        lat, lon = location.get("lat"), location.get("lon")
        if valid_number(lat, -90, 90) and valid_number(lon, -180, 180) and (lat or lon):
            return title, company, (round(lat, 5), round(lon, 5))
        address = normalized(location.get("address") or job.get("address"))
    else:
        address = normalized(job.get("address") or location)
    if address and any(char.isdigit() for char in address):
        return title, company, (region, address)
    description = normalized(job.get("description"))
    if work_formats(job.get("format")) == {"remote"} and len(description) >= 60:
        signature = hashlib.sha256(description.encode()).hexdigest()
        return title, company, ("remote", signature)
    return None
