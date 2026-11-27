import hashlib
import re
from urllib.parse import urlsplit

from app.job_sources import SOURCES, source_vacancy_id, text
from app.vacancies import HH, URL_FIELDS, vacancy_id

URL = re.compile(r"https://[^\s<>\"«»]+", re.IGNORECASE)
MIN_DESCRIPTION = 60


def can_add_description(row, details):
    return (
        details.get("source") == "hh"
        and not details.get("description")
        and row["status"] in {"queued", "deferred"}
        and not (row["result"] or row["letter"] or row["sent_day"])
    )


def checked_url(url):
    if (
        not isinstance(url, str)
        or not 0 < len(url) <= 600
        or any(char.isspace() or ord(char) < 32 for char in url)
        or "\\" in url
    ):
        raise ValueError("Укажите HTTPS-ссылку на вакансию.")
    parsed = urlsplit(url)
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or "." not in parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or parsed.port not in {None, 443}
    ):
        raise ValueError("Укажите обычную HTTPS-ссылку без логина и пароля.")
    return parsed._replace(fragment="")


def shared_hh_jobs(body, linked_urls):
    urls = [match.group().rstrip(".,;!?)…") for match in URL.finditer(body)]
    jobs = {}
    for url in (*urls, *linked_urls):
        try:
            identifier = vacancy_id(checked_url(url).geturl())
        except ValueError:
            continue
        jobs[identifier] = {"url": f"{HH}/vacancy/{identifier}"}
    # A forwarded digest can contain several links. Its text cannot be assigned to one vacancy.
    if len(jobs) == 1:
        lines = [line.strip() for line in URL.sub("", body).splitlines() if line.strip()]
        if lines:
            job = next(iter(jobs.values()))
            if len(lines[0]) <= 180:
                job["title"] = lines[0]
            description = "\n".join(lines[1:])
            if len(text(description, 5200)) >= MIN_DESCRIPTION:
                job["description"] = description
    return list(jobs.values())


def save_job(db, raw):
    parsed = checked_url(raw.get("url"))
    url = parsed.geturl()
    site = parsed.hostname.removeprefix("www.")
    origin = next(
        (
            key
            for key, (_, href) in SOURCES.items()
            if site == urlsplit(href).hostname.removeprefix("www.")
        ),
        "external",
    )
    identifier = source_vacancy_id(origin, parsed.path)
    description = text(raw.get("description"), 5200)
    title = text(raw.get("title")) or (text(description.splitlines()[0]) if description else "")
    try:
        hh_id = vacancy_id(url)
    except ValueError:
        pass
    else:
        identifier, origin, url = hh_id, "hh", f"{HH}/vacancy/{hh_id}"
        title = title or f"Вакансия HH №{hh_id}"
    if not title:
        raise ValueError("Укажите должность.")
    if not identifier:
        identifier = "external:" + hashlib.sha256(url.encode()).hexdigest()[:32]
    details = dict.fromkeys(URL_FIELDS, "")
    details.update(
        title=title,
        company=text(raw.get("company")) or ("Компания не указана" if origin == "hh" else site),
        url=url,
        description=description,
        source=origin,
        mini_filters={"region": "любой", "unknown_salary": True},
    )
    if not db.add(identifier, details, deferred=not description) and description:
        db.add_vacancy_description(identifier, details)
    return db.vacancy(identifier)
