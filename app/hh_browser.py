import re
from urllib.parse import parse_qs, urljoin, urlsplit

from playwright.async_api import TimeoutError as BrowserTimeout

from app.services import ServiceError
from app.vacancies import HH, URL_FIELDS, vacancy_id

DESCRIPTION = '[data-qa="vacancy-description"]'
TITLE = '[data-qa="vacancy-title"]'
COMPANY = '[data-qa="vacancy-company-name"]'
APPLY = '[data-qa="vacancy-response-link-top"]'
SUBMIT = '[data-qa="vacancy-response-popup-submit-button"]'
LETTER = 'textarea[data-qa="vacancy-response-popup-form-letter-input"]'
RESPONSE = '[data-qa="vacancy-response-link-view-response"]'
RESUME_LINK = 'a[href*="/resume/"]'
RESUME_ID = re.compile(r"/resume/([a-f0-9]{12,64})/?$", re.I)


class HHAttention(ServiceError):
    pass


class HHLoginRequired(HHAttention):
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
        raise HHAttention("HH показал CAPTCHA. Операции остановлены.")
    if await page.get_by_text(
        re.compile(r"^Доступ ограничен|^Слишком много запросов|^Вы не робот\?|^Access denied", re.I)
    ).first.is_visible():
        raise HHAttention("HH ограничил доступ. Операции остановлены.")
    if not login_allowed and (
        any(part in parsed.path for part in ("/account/login", "/login", "/oauth"))
        or await page.locator('[data-qa="login-input-username"]').first.is_visible()
    ):
        raise HHLoginRequired("Требуется повторно войти в HH.")


async def open_page(page, url, *, login_allowed=False):
    if not hh_url(url):
        raise HHAttention("Неподдерживаемая ссылка HH.")
    response = await page.goto(url, wait_until="domcontentloaded", timeout=12000)
    if response and response.status in {403, 429, 503}:
        raise HHAttention(f"HH вернул HTTP {response.status}. Операции остановлены.")
    await guard(page, login_allowed=login_allowed)


async def authorized_resumes(page):
    await open_page(page, HH + "/applicant/resumes")
    if urlsplit(page.url).path.rstrip("/") != "/applicant/resumes":
        raise HHLoginRequired("HH не открыл личную страницу резюме.")
    links = page.locator(RESUME_LINK)
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
        name = (await item.inner_text()).strip()
        if hh_url(target) and match and name:
            items[match[1]] = name[:90]
    if not items:
        raise HHLoginRequired("Не удалось подтвердить вход по личному списку резюме HH.")
    return items


async def read_resume(page, identifier):
    if identifier not in await authorized_resumes(page):
        raise HHLoginRequired("Выбранное резюме отсутствует в вашем кабинете HH.")
    await open_page(page, f"{HH}/resume/{identifier}")
    sections = page.locator(
        '[data-qa="resume-block-experience"], [data-qa="resume-block-skills"], '
        '[data-qa="resume-block-keyskills"], [data-qa="resume-block-education"], '
        '[data-qa="resume-block-title"], [data-qa="resume-personal-specialization"]'
    )
    result = []
    for element in await sections.all():
        if await element.is_visible():
            result.append((await element.inner_text())[:3500])
    text = "\n".join(result).strip()
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


async def search_ids(page, url):
    await open_page(page, url)
    links = page.locator('a[data-qa="serp-item__title"]')
    if not await links.count():
        await page.wait_for_timeout(800)
        await guard(page)
    found = []
    for href in await links.evaluate_all("(links) => links.slice(0, 30).map(link => link.href)"):
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


async def read_vacancy(page, identifier):
    url = f"{HH}/vacancy/{identifier}"
    await open_page(page, url)
    if await page.locator('[data-qa="vacancy-archive"]').first.is_visible():
        return None
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
        raise HHAttention("Структура карточки HH изменилась. Операции остановлены.")
    if values["salary"]:
        amount = salary(values["salary"])
        if not amount:
            return None
        values.update(amount)
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
    if not await control.is_visible():
        raise HHQuestions("HH не показал поддерживаемую форму отклика.")
    href = await control.get_attribute("href")
    # Open the dedicated form without clicking a control that may submit directly.
    if (
        href
        and hh_url(urljoin(HH, href))
        and urlsplit(urljoin(HH, href)).path == "/applicant/vacancy_response"
    ):
        target = urljoin(HH, href)
        if parse_qs(urlsplit(target).query).get("vacancyId") != [identifier]:
            raise HHQuestions("Нельзя подтвердить принадлежность формы вакансии.")
        await open_page(page, target)
        form_url = urlsplit(page.url)
        if form_url.path != "/applicant/vacancy_response" or parse_qs(form_url.query).get(
            "vacancyId"
        ) != [identifier]:
            raise HHQuestions("HH открыл другую страницу вместо проверенной формы отклика.")
    else:
        raise HHQuestions(
            "Форма HH не позволяет гарантировать отправку выбранного резюме с письмом."
        )
    form = page.locator('[data-qa="vacancy-response-popup-form"]').filter(has=page.locator(SUBMIT))
    if not await form.count():
        form = page.locator('[role="dialog"]').filter(has=page.locator(SUBMIT))
    if await form.count() != 1:
        raise HHQuestions("Требуется ручная проверка формы или анкеты.")
    action = await form.get_attribute("action")
    if action and not hh_url(urljoin(page.url, action)):
        raise HHQuestions("Форма ведёт на сайт работодателя. Отклик требует ручной отправки.")
    if await form.locator('[data-qa*="question"], select[required]').count():
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
    if await field.count() != 1 or not await field.is_visible() or not await send.is_enabled():
        raise HHQuestions("Форма не позволяет вложить сопроводительное письмо.")
    await field.fill(letter)
    await guard(page)
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
