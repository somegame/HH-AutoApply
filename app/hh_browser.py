import asyncio
import json
import re
from contextlib import suppress
from datetime import datetime
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.async_api import TimeoutError as BrowserTimeout

from app.commute import point
from app.services import ServiceError, clean_text
from app.vacancies import HH, URL_FIELDS, vacancy_id

DESCRIPTION = '[data-qa="vacancy-description"]'
TITLE = '[data-qa="vacancy-title"]'
COMPANY = '[data-qa="vacancy-company-name"]'
APPLY = '[data-qa="vacancy-response-link-top"]'
SUBMIT = '[data-qa="vacancy-response-popup-submit-button"]'
LETTER = 'textarea[data-qa="vacancy-response-popup-form-letter-input"], textarea[name="letter"]'
LETTER_TOGGLE = 'button[type="button"][data-qa="vacancy-response-popup-add-letter"]'
RESPONSE = '[data-qa="vacancy-response-link-view-response"]'
RESUME_LINK = 'a[href*="/resume/"]'
RESUME_ID = re.compile(r"/(?:applicant/resumes?/|resume/)([a-f0-9]{12,64})/?$", re.I)
RESUME_TITLE = '[data-qa="resume-block-title"], [data-qa="resume-title"], h1'
RESUME_EDIT = (
    '[data-qa="resume-edit"], [data-qa="resume-edit-button"], a[href^="/applicant/resume/edit"]'
)
RESUMES_URL = f"{HH}/applicant/resumes/"
RESUME_LIST_PATHS = {"/applicant/resumes", "/applicant/resume", "/applicant/profile"}
RESUME_SECTIONS = (
    '[data-qa="resume-block-experience"], [data-qa="resume-block-skills"], '
    '[data-qa="resume-block-keyskills"], [data-qa="resume-block-education"], '
    '[data-qa="resume-block-title"], [data-qa="resume-personal-specialization"], '
    '[data-qa="resume-block-skills-content"], [data-qa="resume-block-experience-content"], '
    '[data-qa="resume-block-languages"], [data-qa="resume-block-about"]'
)
RESUME_HEADINGS = re.compile(
    r"^(Опыт работы|Ключевые навыки|Навыки|Образование|Языки|О себе)$", re.I
)
DASHBOARD_PATHS = {"", "/applicant/profile"}
DASHBOARD_METRICS = ("Просмотры", "Отклики", "Автопоиски")
LOGIN_INPUT = '[data-qa="login-input-username"]'


class HHAttention(ServiceError):
    pass


class HHAccessBarrier(HHAttention):
    """HH rejected access or requires a CAPTCHA."""

    def __init__(self, message, *, url="", status=None):
        super().__init__(message)
        self.url = url
        self.status = status


class HHLoginRequired(HHAttention):
    pass


class HHResumeUnavailable(HHAttention):
    pass


class HHVacancyUnavailable(ServiceError):
    pass


class HHQuestions(ServiceError):
    pass


def hh_url(url):
    parsed = urlsplit(url)
    return (
        parsed.scheme == "https"
        and parsed.port in {None, 443}
        and (
            parsed.hostname == "hh.ru"
            or bool(parsed.hostname and parsed.hostname.endswith(".hh.ru"))
        )
        and not (parsed.username or parsed.password)
    )


async def guard(page, *, login_allowed=False):
    url = page.url
    if not hh_url(url):
        raise HHAttention("Открыта страница за пределами HH. Операция остановлена.")
    parsed = urlsplit(url)
    if (
        "captcha" in parsed.path
        or await page.locator(
            'form[action*="captcha"], input[name="captcha"], [data-qa="captcha-input"]'
        ).first.is_visible()
    ):
        raise HHAccessBarrier("HH показал CAPTCHA. Операции остановлены.")
    if await page.get_by_text(
        re.compile(r"^Доступ ограничен|^Слишком много запросов|^Вы не робот\?|^Access denied", re.I)
    ).first.is_visible():
        raise HHAccessBarrier("HH ограничил доступ. Операции остановлены.")
    if not login_allowed and (
        any(part in parsed.path for part in ("/account/login", "/login", "/oauth"))
        or await page.locator(LOGIN_INPUT).first.is_visible()
    ):
        raise HHLoginRequired("Требуется повторно войти в HH.")


async def open_page(page, url, *, login_allowed=False):
    if not hh_url(url):
        raise HHAttention("Неподдерживаемая ссылка HH.")
    response = await page.goto(url, wait_until="domcontentloaded", timeout=12000)
    if response and response.status in {403, 429, 503}:
        raise HHAccessBarrier(
            f"HH вернул HTTP {response.status}. Операции остановлены.",
            url=url,
            status=response.status,
        )
    try:
        await guard(page, login_allowed=login_allowed)
    except HHAccessBarrier as exc:
        exc.url = url
        raise
    if response and response.status >= 500:
        raise ServiceError(f"HH временно вернул HTTP {response.status}.", 120)
    return response


async def dashboard_authenticated(page):
    if not hh_url(page.url) or urlsplit(page.url).path.rstrip("/") not in DASHBOARD_PATHS:
        return False
    if await page.locator(LOGIN_INPUT).first.is_visible():
        return False
    for label in DASHBOARD_METRICS:
        metric = re.compile(rf"^{label}\s+[0-9][0-9 \u00a0\u202f]*\+?$")
        visible_value = False
        candidates = page.get_by_text(re.compile(rf"^{label}(?:\s+[0-9][0-9 \u00a0\u202f]*\+?)?$"))
        for index in range(min(await candidates.count(), 6)):
            card = candidates.nth(index)
            if not await card.is_visible():
                continue
            for _ in range(3):
                if metric.fullmatch((await card.inner_text(timeout=1000)).strip()):
                    visible_value = True
                    break
                card = card.locator("..")
            if visible_value:
                break
        if not visible_value:
            return False
    return True


async def authorized_resumes(page):
    await open_page(page, RESUMES_URL)
    owned = await owned_resume(page)
    if owned:
        return owned
    if urlsplit(page.url).path.rstrip("/") not in RESUME_LIST_PATHS:
        raise HHResumeUnavailable("HH не открыл личную страницу резюме.")
    links = page.locator(f'{RESUME_LINK}:visible, a[href*="/applicant/resumes/"]:visible')
    try:
        await links.first.wait_for(state="visible", timeout=5000)
    except BrowserTimeout:
        await guard(page)
    items = {}
    for item in await links.all():
        if not await item.is_visible():
            continue
        href = await item.get_attribute("href")
        target = urljoin(HH, href or "")
        match = RESUME_ID.fullmatch(urlsplit(target).path)
        name = (
            (await item.inner_text()).strip() or await item.get_attribute("aria-label") or ""
        ).strip()
        if hh_url(target) and match and name:
            items[match[1]] = name[:90]
    if not items:
        raise HHResumeUnavailable("HH пока не показал список резюме.")
    return items


def resume_identifier(value):
    if not isinstance(value, str) or not 0 < len(value) <= 1500:
        raise HHResumeUnavailable("Вставьте ссылку на своё резюме HH.")
    try:
        url = value.strip()
        match = RESUME_ID.fullmatch(urlsplit(url).path)
        if match and hh_url(url):
            return match[1]
    except ValueError:
        pass
    raise HHResumeUnavailable("Нужна ссылка на карточку резюме на hh.ru.")


async def owned_resume(page):
    if not hh_url(page.url):
        return {}
    match = RESUME_ID.fullmatch(urlsplit(page.url).path)
    if not match:
        return {}
    await guard(page)
    edit = page.get_by_role("button", name=re.compile(r"^Редактировать(?: резюме)?$", re.I))
    edit_link = page.get_by_role("link", name=re.compile(r"^Редактировать(?: резюме)?$", re.I))
    for control in (page.locator(RESUME_EDIT), edit, edit_link):
        if await control.first.is_visible():
            for title in await page.locator(RESUME_TITLE).all():
                if await title.is_visible() and (name := (await title.inner_text()).strip()):
                    return {match[1]: name[:90]}
    return {}


async def verify_resume(page, identifier):
    if not RESUME_ID.fullmatch(f"/resume/{identifier}"):
        raise HHResumeUnavailable("Некорректный идентификатор резюме HH.")
    if identifier not in await owned_resume(page):
        await open_page(page, f"{HH}/resume/{identifier}")
    if identifier in await owned_resume(page):
        return
    if identifier not in await authorized_resumes(page):
        raise HHResumeUnavailable("Выбранное резюме отсутствует в вашем кабинете HH.")
    await open_page(page, f"{HH}/resume/{identifier}")


async def read_resume(page, identifier):
    await verify_resume(page, identifier)
    if resume_identifier(page.url) != identifier:
        raise HHResumeUnavailable("HH открыл другую страницу вместо выбранного резюме.")
    return await professional_resume_text(page)


async def read_owned_resume(page, identifier):
    owned = await owned_resume(page)
    if identifier not in owned:
        raise HHResumeUnavailable(
            "Не удалось подтвердить, что это резюме принадлежит подключённому аккаунту."
        )
    resume = await professional_resume_text(page)
    if identifier not in await owned_resume(page):
        raise HHResumeUnavailable("Карточка резюме изменилась во время чтения. Откройте её снова.")
    return owned[identifier], resume


async def professional_resume_text(page):
    result = []
    for element in await page.locator(RESUME_SECTIONS).all():
        if await element.is_visible():
            result.append((await element.inner_text())[:3500])
    if len("\n".join(result).strip()) < 60:
        for heading in await page.get_by_role("heading", name=RESUME_HEADINGS).all():
            if not await heading.is_visible():
                continue
            semantic = heading.locator("xpath=ancestor::section[1]")
            parent = heading.locator("..")
            blocks = (semantic,) if await semantic.count() else (parent, parent.locator(".."))
            for block in blocks:
                if (
                    await block.get_by_role("heading", level=1).count()
                    or await block.get_by_role(
                        "heading",
                        name=re.compile(r"^(Контакты|Личные данные|Контактная информация)$", re.I),
                    ).count()
                ):
                    continue
                body = (await block.inner_text()).strip()
                if 26 <= len(body) <= 3500 and body != (await heading.inner_text()).strip():
                    result.append(body)
                    break
    text = "\n".join(dict.fromkeys(result)).strip()
    if len(text) < 60:
        raise ServiceError("Не удалось прочитать профессиональные разделы резюме HH.")
    return text[:9000]


def salary(text):
    text = text.casefold().replace("\u202f", " ").replace("\xa0", " ")
    currencies = {"RUB": r"₽|руб\b|rub\b", "USD": r"\$|usd\b", "EUR": r"€|eur\b"}
    currency = next((key for key, pattern in currencies.items() if re.search(pattern, text)), None)
    numbers = [
        int(item.replace(" ", ""))
        for item in re.findall(r"(?<!\d)(?:\d{1,3}(?: \d{3})+|\d{1,9})(?!\d)", text)[:2]
    ]
    if not currency or not numbers or not 0 < min(numbers):
        return {}
    return {"salary_min": min(numbers), "salary_max": max(numbers), "currency": currency}


async def visible_text(page, selector, limit=180):
    node = page.locator(selector).first
    if not await node.is_visible():
        return ""
    return (await node.inner_text()).strip()[:limit]


def publication_date(value):
    if not isinstance(value, str):
        return ""
    try:
        return datetime.fromisoformat(value.strip()).date().isoformat()
    except ValueError:
        return ""


def office_address(value, *, with_point=False):
    if not isinstance(value, str):
        return ""
    address = " ".join(clean_text(value[:280]).split())[:240]
    if "[скрыто]" in address or not any(char.isalpha() for char in address):
        return ""
    if not with_point and not re.search(
        r"(?<![\w-])\d{1,5}(?:[а-яa-z]\d{0,4}|[/-]\d{1,5})?(?=$|[\s,;.)])",
        address,
        flags=re.I,
    ):
        return ""
    return address


def office_location(lat, lon, address):
    coords = point({"lat": lat, "lon": lon})
    if coords and (coords["lat"] or coords["lon"]):
        return coords | {"address": address}
    return None


def posting_metadata(payloads):
    for raw in payloads:
        try:
            decoded = json.loads(raw)
        except (ValueError, TypeError):
            continue
        graph = decoded.get("@graph") if isinstance(decoded, dict) else None
        candidates = graph if isinstance(graph, list) else decoded
        if not isinstance(candidates, list):
            candidates = [candidates]
        for item in candidates[:12]:
            if not isinstance(item, dict):
                continue
            kind = item.get("@type")
            if kind == "JobPosting" or isinstance(kind, list) and "JobPosting" in kind:
                return item
    return {}


async def office_metadata(page, identifier):
    scripts = await page.locator('script[type="application/ld+json"]').evaluate_all(
        "(nodes) => nodes.slice(0, 8).map(node => node.textContent.slice(0, 120000))"
    )
    posting = posting_metadata(scripts)
    published = publication_date(posting.get("datePosted"))
    if not published:
        dates = page.get_by_text(re.compile(r"^Вакансия опубликована")).locator("time[datetime]")
        if await dates.count() == 1:
            published = publication_date(await dates.first.get_attribute("datetime"))

    places = posting.get("jobLocation")
    if isinstance(places, list):
        if len(places) > 1:
            return {"published": published, "address": "", "location": None}
        places = places[0] if places else None
    place = places if isinstance(places, dict) else {}
    postal = place.get("address")
    address = ""
    if isinstance(postal, dict):
        street = office_address(postal.get("streetAddress"))
        if street:
            city = postal.get("addressLocality")
            address = office_address(f"{city}, {street}") if isinstance(city, str) else street
    if not address:
        address = office_address(
            await visible_text(page, '[data-qa="vacancy-view-raw-address"]', 240)
        )

    geo = place.get("geo")
    location = (
        office_location(geo.get("latitude"), geo.get("longitude"), address)
        if isinstance(geo, dict)
        else None
    )
    if not location:
        marker = await page.locator("template#HH-Lux-InitialState").evaluate_all(
            """(nodes, id) => {
                const text = nodes[0]?.content?.textContent;
                if (!text || text.length > 750000) return null;
                try {
                    const vacancy = JSON.parse(text).vacancyView?.vacancyFull?.vacancy;
                    if (String(vacancy?.vacancyId) !== id) return null;
                    const office = vacancy.address;
                    return office?.marker ? {
                        lat: office.marker.lat,
                        lon: office.marker.lng,
                        address: office.displayName,
                    } : null;
                } catch {
                    return null;
                }
            }""",
            identifier,
        )
        if marker:
            pin_address = office_address(marker.get("address"), with_point=True) or address
            location = office_location(marker.get("lat"), marker.get("lon"), pin_address)
            if location:
                address = pin_address
    return {"published": published, "address": address, "location": location}


async def search_results(page, url, *, limit=None):
    response = await open_page(page, url)
    if response and response.status in {404, 410} and "page" in parse_qs(urlsplit(url).query):
        return []
    links = page.locator('a[data-qa="serp-item__title"]')
    if not await links.count():
        await page.wait_for_timeout(800)
        await guard(page)
    found = []
    hrefs = await links.evaluate_all("(links) => links.map(link => link.href)")
    for href in hrefs[:limit]:
        try:
            found.append(vacancy_id(href))
        except ValueError:
            continue
    if (
        not found
        and not await page.get_by_text(
            re.compile(r"ничего не найдено|нет подходящих вакансий|не нашли вакансий", re.I)
        ).first.is_visible()
    ):
        raise HHAttention("HH не показал знакомый список вакансий. Поиск приостановлен.")
    return list(dict.fromkeys(found))


async def search_ids(page, url):
    return await search_results(page, url, limit=30)


async def search_page(page, url):
    identifiers = await search_results(page, url)
    if not identifiers:
        return identifiers, None
    next_link = page.locator(
        'a[data-qa="pager-next"][href]:visible, [data-qa="pager-next"] a[href]:visible, '
        'a[rel="next"][href]:visible'
    ).first
    if not await next_link.count():
        return identifiers, None
    href = await next_link.get_attribute("href")
    if not href:
        return identifiers, None
    try:
        target = urljoin(page.url, href)
        parsed = urlsplit(target)
        current = parse_qs(urlsplit(url).query)
        following = parse_qs(parsed.query)
        if (
            hh_url(target)
            and parsed.path.rstrip("/") == "/search/vacancy"
            and following.get("text") == current.get("text")
            and int(following.get("page", ["0"])[0]) == int(current.get("page", ["0"])[0]) + 1
        ):
            return identifiers, target
    except ValueError:
        pass
    return identifiers, None


async def read_vacancy(page, identifier):
    url = f"{HH}/vacancy/{identifier}"
    response = await open_page(page, url)
    if (response and response.status in {404, 410}) or await page.locator(
        '[data-qa="vacancy-archive"]'
    ).first.is_visible():
        return None
    with suppress(BrowserTimeout):
        await asyncio.gather(
            *(
                page.locator(selector).first.wait_for(state="visible", timeout=3000)
                for selector in (TITLE, COMPANY, DESCRIPTION)
            )
        )
    await guard(page)
    values = dict.fromkeys(URL_FIELDS, "")
    selectors = {
        "title": TITLE,
        "company": COMPANY,
        "region": '[data-qa="vacancy-view-location"], [data-qa="vacancy-view-raw-address"]',
        "salary": '[data-qa="vacancy-salary-compensation-type-net"], [data-qa="vacancy-salary-compensation-type-gross"], [data-qa="vacancy-salary"]',
        "experience": '[data-qa="vacancy-experience"]',
        "schedule": '[data-qa="work-schedule-by-days-text"]',
        "format": '[data-qa="work-formats-text"]',
        "description": DESCRIPTION,
    }
    for key, selector in selectors.items():
        values[key] = await visible_text(page, selector, 9000 if key == "description" else 180)
    values.update(url=url, source="browser")
    if not values["title"] or not values["company"] or not values["description"]:
        raise HHVacancyUnavailable("HH не показал основные поля этой вакансии.")
    if values["salary"]:
        amount = salary(values["salary"])
        if not amount:
            return None
        values.update(amount)
    values.update(await office_metadata(page, identifier))
    return values


async def already_applied(page):
    return (
        await page.locator(RESPONSE).first.is_visible()
        or await page.get_by_text(
            re.compile(r"^(Вы уже откликнулись|Вы откликнулись|Отклик отправлен)[.!]?$", re.I)
        ).first.is_visible()
    )


async def apply(page, identifier, resume_id, letter, reserve):
    await open_page(page, f"{HH}/vacancy/{identifier}")
    if await already_applied(page):
        return "duplicate"
    control = page.locator(APPLY).first
    href = await control.get_attribute("href") if await control.is_visible() else None
    dedicated = (
        href
        and hh_url(urljoin(HH, href))
        and urlsplit(urljoin(HH, href)).path.rstrip("/") == "/applicant/vacancy_response"
    )
    # An unverified site button can submit directly. Inspect only the dedicated
    # link or a form already displayed on the known vacancy.
    if dedicated:
        target = urljoin(HH, href)
        if parse_qs(urlsplit(target).query).get("vacancyId") != [identifier]:
            raise HHQuestions("Нельзя подтвердить принадлежность формы вакансии.")
        await open_page(page, target)
        form_url = urlsplit(page.url)
        if form_url.path.rstrip("/") != "/applicant/vacancy_response" or parse_qs(
            form_url.query
        ).get("vacancyId") != [identifier]:
            raise HHQuestions("HH открыл другую страницу вместо проверенной формы отклика.")
    else:
        form_url = urlsplit(page.url)
        if form_url.path.rstrip("/") != f"/vacancy/{identifier}":
            raise HHQuestions("HH не показал поддерживаемую форму отклика.")
    form = page.locator('[data-qa="vacancy-response-popup-form"]:visible').filter(
        has=page.locator(SUBMIT)
    )
    if not await form.count():
        form = page.locator('[role="dialog"]:visible').filter(has=page.locator(SUBMIT))
    if await form.count() != 1:
        raise HHQuestions("Требуется ручная проверка формы или анкеты.")
    action = await form.get_attribute("action")
    if action and not hh_url(urljoin(page.url, action)):
        raise HHQuestions("Форма ведёт на сайт работодателя. Отклик требует ручной отправки.")
    if not dedicated:
        vacancy_field = form.locator('input[type="hidden"][name="vacancyId"]')
        if await vacancy_field.count() != 1 or await vacancy_field.input_value() != identifier:
            raise HHQuestions("Нельзя подтвердить принадлежность формы вакансии.")
    if await form.locator(
        '[data-qa*="question"], select[required]:not([name="resumeId"]), '
        'input[required]:not([name="resumeId"]):not([type="hidden"])'
    ).count():
        raise HHQuestions("Работодатель просит ответить на вопросы.")
    choice = form.locator(f'input[type="radio"][value="{resume_id}"]')
    selected = form.locator(f'input[type="hidden"][name="resumeId"][value="{resume_id}"]')
    dropdown = form.locator('select[name="resumeId"]').filter(
        has=page.locator(f'option[value="{resume_id}"]')
    )
    if await choice.count() == 1:
        await choice.check()
    elif await dropdown.count() == 1:
        await dropdown.select_option(resume_id)
    elif await selected.count() != 1:
        raise HHQuestions("В форме не удалось подтвердить выбранное резюме HH.")
    field = form.locator(LETTER)
    send = form.locator(SUBMIT)
    if not await field.first.is_visible():
        toggle = form.locator(LETTER_TOGGLE).or_(
            form.get_by_role(
                "button", name=re.compile(r"^Добавить сопроводительное письмо$", re.I)
            ).and_(form.locator('button[type="button"]'))
        )
        if await toggle.count() == 1 and await toggle.is_visible():
            await toggle.click()
    if await field.count() != 1 or not await field.is_visible() or not await send.is_enabled():
        raise HHQuestions("Форма не позволяет вложить сопроводительное письмо.")
    await field.fill(letter)
    await guard(page)
    if (
        page.url != form_url.geturl()
        or await field.input_value() != letter
        or (not dedicated and await vacancy_field.input_value() != identifier)
    ):
        raise HHQuestions("HH изменил форму или текст письма перед отправкой.")
    if not reserve():
        return "limit"
    await send.click(timeout=3500)
    try:
        await page.get_by_text(
            re.compile(r"^(Отклик отправлен|Вы откликнулись)[.!]?$", re.I)
        ).first.wait_for(state="visible", timeout=2500)
    except BrowserTimeout:
        pass
    await open_page(page, f"{HH}/vacancy/{identifier}")
    if urlsplit(page.url).path.rstrip("/") != f"/vacancy/{identifier}":
        return "unknown"
    return "sent" if await already_applied(page) else "unknown"
