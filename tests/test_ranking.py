import hashlib
from datetime import date

from app.ranking import duplicate_key, rank_job, sort_ranked, work_formats

TODAY = date(2026, 11, 27)
PREFS = {
    "salary": 150_000,
    "currency": "RUB",
    "preferred_format": "hybrid",
    "max_commute": 45,
}
RESUME = "Финансовый менеджер. Управлял бюджетом, готовил отчётность и строил модели в Excel."
JOB = {
    "id": "1",
    "title": "Финансовый менеджер",
    "company": "ООО Пример",
    "region": "Москва",
    "description": "Финансовое моделирование, контроль бюджета, Excel, управленческая отчётность.",
    "salary_min": 150_000,
    "salary_max": 200_000,
    "currency": "RUR",
    "format": "На месте работодателя, Гибрид",
    "published": "2026-11-26T10:33:00+03:00",
}


def factors(score):
    return {item["key"]: item for item in score["factors"]}


def test_rank_with_verified_groq_and_all_five_factors():
    commute = {"minutes": 18, "kind": "route"}
    analysis = {"score": 92, "resume_hash": hashlib.sha256(RESUME.encode()).hexdigest()}
    rated = rank_job(JOB, PREFS, RESUME, analysis, commute, today=TODAY)
    parts = factors(rated)

    assert rated["score"] == 92
    assert rated["fit"] == 92
    assert rated["fit_kind"] == "groq"
    assert rated["coverage"] == 100
    assert rated["priority"] == rated["score"]
    assert [parts[name]["score"] for name in ("salary", "commute", "format", "freshness")] == [
        93,
        84,
        100,
        96,
    ]
    assert "По маршруту 18 мин" in parts["commute"]["detail"]


def test_stale_analysis_falls_back_to_lexical_fit_and_reports_unavailable_factors():
    rated = rank_job(
        JOB | {"currency": "USD", "published": "ошибка", "format": ""},
        PREFS,
        RESUME,
        {"score": 100, "resume_hash": "previous"},
        today=TODAY,
    )
    parts = factors(rated)
    assert 0 < rated["fit"] <= 85 and rated["fit_kind"] == "estimate"
    assert rated["coverage"] == 45
    assert all(parts[key]["score"] is None for key in ("salary", "freshness", "commute", "format"))
    assert "Предварительная" in parts["fit"]["detail"]
    assert rated["priority"] < rated["score"]


def test_missing_required_experience_lowers_comparison_without_losing_groq_score():
    analysis = {
        "score": 93,
        "resume_hash": hashlib.sha256(RESUME.encode()).hexdigest(),
        "missing": ["обязателен опыт МСФО", "владение Power BI"],
    }
    rated = rank_job(JOB, PREFS, RESUME, analysis, today=TODAY)
    assert rated["fit"] == 93 and rated["missing_required"] == 2
    assert factors(rated)["fit"]["score"] < rated["fit"]
    assert "Не подтверждено" in factors(rated)["fit"]["detail"]


def test_remote_and_unknown_format_and_publication_behave_differently():
    unknown = rank_job(
        {"title": "Пример", "format": "", "published": "2027-01-10"},
        PREFS | {"preferred_format": "remote"},
        "",
        today=TODAY,
    )
    assert unknown["score"] is None and unknown["coverage"] == 0
    remote = rank_job(
        JOB | {"format": "Удалённая работа"},
        PREFS | {"preferred_format": "remote"},
        "",
        commute={"kind": "remote", "minutes": 0},
        today=TODAY,
    )
    assert factors(remote)["format"]["score"] == 100
    assert factors(remote)["commute"]["score"] == 100
    assert work_formats("Смешанный формат, удалённо или на месте работодателя") == {
        "hybrid",
        "remote",
        "office",
    }
    assert work_formats("Пятидневная неделя") == set()


def test_salary_upper_bound_is_not_scored_as_a_guaranteed_salary():
    exact = rank_job(JOB | {"salary_max": 300_000, "salary_min": 300_000}, PREFS, "", today=TODAY)
    ceiling = rank_job(JOB | {"salary_max": 300_000, "salary_min": None}, PREFS, "", today=TODAY)
    assert factors(exact)["salary"]["score"] == 100
    assert factors(ceiling)["salary"]["score"] == 70
    assert "Верхняя граница" in factors(ceiling)["salary"]["detail"]


def test_sorting_by_each_dimension_leaves_absent_information_last():
    a = JOB | {"rank": {"priority": 72, "fit": 80, "coverage": 75}, "commute": {"minutes": 12}}
    b = JOB | {
        "id": "2",
        "salary_max": 260_000,
        "published": "2026-11-27",
        "rank": {"priority": 70, "fit": 93, "coverage": 85},
        "commute": {"minutes": 50},
    }
    unknown = {"id": "none", "title": "Неизвестная вакансия"}
    jobs = [unknown, b, a]
    for order, ids in (
        ("best", ["1", "2", "none"]),
        ("commute", ["1", "2", "none"]),
        ("salary", ["2", "1", "none"]),
        ("fit", ["2", "1", "none"]),
        ("fresh", ["2", "1", "none"]),
    ):
        assert [item["id"] for item in sort_ranked(jobs, order)] == ids


def test_deduplication_merges_known_location_and_keeps_separate_sites():
    a = JOB | {"location": {"lat": 55.73514, "lon": 37.59328}}
    same = a | {"company": "«Пример»", "id": "2"}
    other = a | {"location": {"lat": 55.73514, "lon": 37.5948}}
    assert duplicate_key(a) == duplicate_key(same)
    assert duplicate_key(a) != duplicate_key(other)
    assert duplicate_key(a | {"company": "Работодатель не указан"}) is None
    assert duplicate_key(JOB) is None
    assert duplicate_key(JOB | {"address": "Петровка, 12"}) != duplicate_key(
        JOB | {"address": "Петровка, 17"}
    )
