import asyncio
import json
import secrets
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest

from app.config import BOOLEANS, FIELDS, OPTIONAL, setting
from app.database import ACTIVE, HISTORY, LABELS
from app.diagnostics import report, resource_warning
from app.resume import MAX_FILE, checked, read_upload
from app.scheduler import local_day
from app.services import ServiceError
from app.vacancies import HH, TEMPLATE, parse_vacancy, search_links, terms, vacancy_id

PREVIEW = LinkPreviewOptions(is_disabled=True)
HH_RESUMES = HH + "/applicant/resumes"
HH_APPLICATIONS = HH + "/applicant/negotiations"
SETTINGS_SECTIONS = {
    "search": (
        "🔎 Поиск вакансий",
        ("query", "region", "salary", "format", "schedule", "experience"),
    ),
    "filters": (
        "🎯 Фильтры",
        ("score", "currency", "unknown_salary", "employers", "excluded", "required"),
    ),
    "related": ("🧭 Смежные должности", ("use_related", "related")),
    "letters": ("✍️ Письма и отклики", ("tone", "length", "limit")),
    "reminders": ("🔔 Напоминания", ("interval",)),
}
COMMANDS = {
    "start": "Главное меню",
    "help": "Справка",
    "run": "Поисковые ссылки и напоминания",
    "stop": "Остановить напоминания и анализ",
    "status": "Текущее состояние",
    "login": "Вход и проверка аккаунта на HH",
    "mobile": "Войти с iPhone",
    "finish": "Подтвердить, что вы вошли в HH",
    "groq": "Диагностика Groq",
    "browser": "Диагностика локального браузера",
    "diagnose": "Полная диагностика",
    "drafts": "Очередь откликов",
    "history": "История",
    "settings": "Настройки",
    "resume": "Загрузить или посмотреть резюме",
    "stats": "Статистика",
    "mode": "Режим подготовки откликов",
    "errors": "Последние ошибки",
    "add": "Добавить выбранную вакансию",
}
PAGES = {"found": (), "drafts": ACTIVE, "history": HISTORY}


def allowed(update, owner):
    return bool(
        update.effective_user
        and update.effective_user.id == owner
        and update.effective_chat
        and update.effective_chat.type == "private"
        and update.effective_chat.id == owner
    )


def button(label, callback):
    return InlineKeyboardButton(label, callback_data=callback)


def link(label, url):
    return InlineKeyboardButton(label, url=url)


def markup(rows):
    return InlineKeyboardMarkup(rows)


def menu(browser=False):
    if browser:
        return markup(
            [
                [button("🔑 Войти в HH через браузер", "cloud_login")],
                [button("▶️ Браузерный поиск и автоотклики", "cloud_menu")],
                [
                    button("📨 Черновики и очередь", "drafts"),
                    link("📨 Отклики HH", HH_APPLICATIONS),
                ],
                [button("⚙️ Настройки", "settings"), button("⋯ Ещё", "more")],
            ]
        )
    return markup(
        [
            [button("🔎 Подобрать вакансии", "search")],
            [button("⚡ Автоотклики HH", "hh_auto"), link("📨 Отклики HH", HH_APPLICATIONS)],
            [button("🔑 Проверить вход", "login"), button("✍️ Анализ вручную", "manual")],
            [button("⚙️ Настройки", "settings"), button("⋯ Ещё", "more")],
        ]
    )


def setting_section(key):
    return next(section for section, (_, keys) in SETTINGS_SECTIONS.items() if key in keys)


def setting_value(value):
    if isinstance(value, bool):
        return "Да" if value else "Нет"
    return str(value) if value != "" else "Не задано"


def search_keyboard(settings):
    rows = [[link(query, url)] for query, url in search_links(settings)]
    rows.append(
        [link("Как включить автопоиск HH", "https://feedback.hh.ru/knowledge-base/article/0048")]
    )
    rows.append([button("➕ Вакансия", "add"), button("🏠 Меню", "start")])
    return markup(rows)


class TelegramUI:
    def __init__(self, config, db, scheduler, client):
        self.config = config
        self.db = db
        self.scheduler = scheduler
        self.client = client
        self.input = None
        self.diagnostic_task = None
        self.auto_prompt = None

    async def answer(self, update, text, keyboard=None):
        return await update.effective_message.reply_text(
            text,
            reply_markup=keyboard,
            link_preview_options=PREVIEW,
            protect_content=True,
        )

    async def handle(self, update, context):
        if not allowed(update, self.config.owner):
            return
        try:
            if update.callback_query:
                try:
                    await update.callback_query.answer()
                except BadRequest:
                    pass
                action = update.callback_query.data
            elif update.message.document:
                await self.document(update)
                return
            elif update.message.text.startswith("/"):
                action = update.message.text.partition(" ")[0].split("@")[0][1:]
            else:
                await self.text(update)
                return
            if not isinstance(action, str):
                return
            await self.route(update, action)
        except (ValueError, TimeoutError) as exc:
            message = (
                "Операция заняла слишком много времени."
                if isinstance(exc, TimeoutError)
                else str(exc)
            )
            await self.answer(update, message, markup([[button("Меню", "start")]]))

    async def route(self, update, action):
        kind, _, value = action.partition(":")
        self.input = None
        if self.config.browser_engine == "chromium":
            kind = {
                "login": "cloud_login",
                "mobile": "cloud_login",
                "finish": "cloud_finish",
                "run": "cloud_menu",
            }.get(kind, kind)
        settings = self.db.settings(self.config)
        if kind == "start":
            title = "HH AutoApply · помощник соискателя"
            if not self.config.persistent:
                title += "\nПредупреждение. Постоянство DATA_DIR не подтверждено. Данные могут исчезнуть при обновлении."
            await self.answer(update, title, menu(self.config.browser_engine == "chromium"))
        elif kind.startswith("cloud_"):
            await self.browser_action(update, kind, value)
        elif kind == "more":
            await self.answer(
                update,
                "Дополнительные разделы",
                markup(
                    [
                        [button("📋 Статус", "status"), button("📊 Статистика", "stats")],
                        [button("📚 Все вакансии", "found"), button("🕘 История", "history")],
                        [button("🩺 Диагностика", "diagnose"), button("❓ Справка", "help")],
                        [button("🏠 Меню", "start")],
                    ]
                ),
            )
        elif kind == "search":
            running = self.db.get("reminders", False)
            controls = (
                button("⏸ Остановить анализ и напоминания", "stop")
                if running
                else button("▶️ Включить анализ и напоминания", "run")
            )
            keyboard = search_keyboard(settings)
            await self.answer(
                update,
                "Откройте подходящий запрос и сохраните автопоиск на HH. "
                "Новые вакансии будет присылать HH. Для этого достаточно аккаунта HH.",
                markup(
                    [
                        *keyboard.inline_keyboard[:-1],
                        [button("⚡ Автоотклики HH", "hh_auto")],
                        [controls],
                        [button("🏠 Меню", "start")],
                    ]
                ),
            )
        elif kind == "hh_auto":
            await self.answer(
                update,
                "Откройте главную HH и найдите «Автоматические отклики» → «Настройки». "
                "Проверьте, какое резюме и какие параметры выбраны в HH. "
                "Если там уже есть кнопка «Отключить», возможность, вероятно, включена. "
                "Если HH предлагает подписку, проверьте её условия перед подтверждением. "
                "Результаты смотрите в «Откликах HH».",
                markup(
                    [
                        [link("⚡ Открыть HH", HH + "/")],
                        [link("📨 Отклики HH", HH_APPLICATIONS)],
                        [link("ℹ️ Условия HH Pro", HH + "/article/hhpro_offer")],
                        [button("🏠 Меню", "start")],
                    ]
                ),
            )
        elif kind == "manual":
            await self.answer(
                update,
                "Здесь можно подготовить письмо к выбранной вакансии. "
                "Для анализа Groq понадобится текст резюме. "
                "Подборки и автоотклики HH работают отдельно.",
                markup(
                    [
                        [button("➕ Добавить вакансию", "add"), button("✍️ Черновики", "drafts")],
                        [button("📄 Резюме для анализа", "resume")],
                        [button("🏠 Меню", "start")],
                    ]
                ),
            )
        elif kind == "help":
            await self.answer(
                update,
                "«Подобрать вакансии» открывает автопоиск HH. «Автоотклики HH» ведёт к настройкам HH. "
                "«Анализ вручную» нужен, если вы хотите подготовить письмо к отдельной вакансии.",
                menu(),
            )
        elif kind == "run":
            self.db.set_many(
                {
                    "processing": True,
                    "reminders": True,
                    "next_reminder": int(time.time()) + settings["interval"] * 60,
                }
            )
            await self.scheduler.start(update.get_bot())
            await self.answer(
                update,
                "Выберите поиск. Для других регионов, формата, стажа "
                "и графика уточните фильтры на HH. Бот напомнит о поиске.",
                search_keyboard(settings),
            )
        elif kind == "stop":
            self.db.set_many({"reminders": False, "processing": False})
            self.scheduler.browser.pause("")
            self.auto_prompt = None
            await self.scheduler.shutdown()
            await self.scheduler.browser.cancel_login()
            await self.answer(update, "Напоминания и анализ остановлены.", menu())
        elif kind in PAGES:
            await self.page(update, kind, int(value or 0))
        elif kind in {"status", "stats", "errors"}:
            await self.status(update, kind, settings)
        elif kind in {"login", "mobile"}:
            await self.answer(
                update,
                "Откройте «Мои резюме на HH». Видите своё резюме или страницу управления "
                "резюме? Вход выполнен в этом браузере. Если HH просит войти, завершите вход "
                "на сайте. Затем закройте браузер кнопкой × и нажмите «Продолжить». "
                "Бот не видит сессию HH.",
                markup(
                    [
                        [link("🔑 Войти в HH", HH + "/account/login")],
                        [link("📄 Мои резюме на HH", HH_RESUMES)],
                        [button("➡️ Продолжить", "finish")],
                        [button("🏠 Меню", "start")],
                    ]
                ),
            )
        elif kind == "finish":
            await self.answer(
                update,
                "Если HH показал личный кабинет, выберите, что включить на самом HH. "
                "Ваше резюме уже хранится там.",
                markup(
                    [
                        [button("🔎 Подобрать вакансии", "search")],
                        [button("⚡ Автоотклики HH", "hh_auto")],
                        [button("🏠 Меню", "start")],
                    ]
                ),
            )
        elif kind in {"groq", "browser", "diagnose"}:
            await self.diagnostics(update, kind)
        elif kind in {"settings", "set", "mode", "choose"}:
            await self.settings(update, kind, value, settings)
        elif kind in {"resume", "upload", "view", "consent"}:
            await self.resume(update, kind)
        elif kind == "add":
            self.input = ("vacancy", None)
            await self.answer(
                update,
                "Заполните шаблон одним сообщением. Поля до описания "
                "берутся с карточки HH. Описание можно добавить отдельным TXT "
                "со всем шаблоном, если оно длиннее лимита Telegram.",
            )
            await self.answer(update, TEMPLATE)
        elif kind in {
            "v",
            "approve",
            "edit",
            "reject",
            "defer",
            "unknown",
            "questions",
            "reported",
            "confirm",
            "exclude",
            "retry",
            "reconcile",
        }:
            await self.vacancy(update, kind, value, settings)

    @staticmethod
    def resume_choices(resumes):
        return markup(
            [
                [button(name, f"cloud_resume:{identifier}")]
                for identifier, name in list(resumes.items())[:20]
                if len(identifier) <= 50
            ]
        )

    async def browser_action(self, update, kind, value):
        browser = self.scheduler.browser
        try:
            if not browser.available:
                raise ServiceError("Браузерный режим выключен. Включите BROWSER_ENGINE=chromium.")
            if kind == "cloud_login":
                await self.answer(update, "Открываю браузер HH. Это может занять несколько секунд.")

                async def notify(result):
                    if isinstance(result, ServiceError):
                        await update.get_bot().send_message(self.config.owner, str(result))
                    else:
                        await update.get_bot().send_message(
                            self.config.owner,
                            "✅ Вход в HH подтверждён. Выберите резюме.",
                            reply_markup=self.resume_choices(result),
                        )

                url = await browser.start_login(notify)
                await self.answer(
                    update,
                    "Войдите в открывшемся окне. Бот проверит вход и пришлёт сюда список резюме. "
                    "Ссылка действует восемь минут.",
                    markup(
                        [[link("🔑 Открыть HH", url)], [button("✖️ Закрыть сессию", "cloud_cancel")]]
                    ),
                )
            elif kind == "cloud_finish":
                resumes = await browser.finish_login()
                if resumes is None:
                    await self.answer(update, "Проверяю вход. Окно HH пока остаётся открытым.")
                else:
                    await self.answer(
                        update,
                        "✅ Вход в HH подтверждён. Выберите резюме.",
                        self.resume_choices(resumes),
                    )
            elif kind == "cloud_cancel":
                await browser.cancel_login()
                await self.answer(update, "Окно входа закрыто.", menu(True))
            elif kind == "cloud_resume":
                name = await browser.import_resume(value)
                await self.answer(
                    update,
                    f"Выбрано резюме «{name}». Для оценки вакансий и подготовки сопроводительных "
                    "Groq получит профессиональный текст резюме. Разрешить обработку?",
                    markup(
                        [
                            [button("✅ Разрешить Groq", "cloud_consent")],
                            [button("🏠 Меню", "start")],
                        ]
                    ),
                )
            elif kind == "cloud_consent":
                if not self.db.get("hh_selected_resume") or not self.db.get("resume"):
                    raise ServiceError("Сначала выберите резюме HH.")
                self.db.set("groq_consent", True)
                await self.browser_action(update, "cloud_menu", "")
            elif kind == "cloud_menu":
                selected = self.db.get("hh_selected_resume", {})
                current = (
                    "Включены автоотклики"
                    if self.db.get("browser_auto_enabled")
                    else "Включён поиск"
                    if self.db.get("browser_search")
                    else "Остановлено"
                )
                notice = self.db.get("browser_notice", "")
                if notice:
                    current += f". {notice}"
                await self.answer(
                    update,
                    f"{current}\nРезюме: {selected.get('name', 'выберите после входа в HH')}",
                    markup(
                        [
                            [button("🔎 Найти вакансии и подготовить письма", "cloud_search")],
                            [button("⚡ Настроить автоотклики", "cloud_auto")],
                            [button("⏸ Остановить", "stop")],
                            [button("🔑 Войти в HH", "cloud_login"), button("🏠 Меню", "start")],
                        ]
                    ),
                )
            elif kind == "cloud_auto":
                browser.readiness()
                if not (self.config.auto_send and self.config.persistent):
                    raise ServiceError(
                        "Для автооткликов нужны AUTO_SEND=true и подтверждённый постоянный Volume."
                    )
                token = secrets.token_hex(5)
                self.auto_prompt = (token, time.monotonic() + 180)
                cap = self.db.settings(self.config)["limit"]
                await self.answer(
                    update,
                    f"Разрешить отправлять работодателям выбранное резюме и сгенерированное письмо "
                    f"без подтверждения каждого отклика? Лимит: {cap} в сутки.",
                    markup(
                        [
                            [button("✅ Включить автоотклики", f"cloud_send:{token}")],
                            [button("‹ Назад", "cloud_menu")],
                        ]
                    ),
                )
            elif kind in {"cloud_search", "cloud_send"}:
                if kind == "cloud_send" and (
                    not self.auto_prompt
                    or self.auto_prompt[0] != value
                    or self.auto_prompt[1] < time.monotonic()
                ):
                    raise ServiceError(
                        "Подтверждение истекло. Снова откройте настройки автооткликов."
                    )
                self.auto_prompt = None
                browser.start_search(send=kind == "cloud_send")
                await self.scheduler.start(update.get_bot())
                await self.answer(
                    update,
                    "Цикл запущен. Подходящие вакансии появятся в очереди.",
                    markup([[button("📨 Очередь", "drafts"), button("⏸ Остановить", "stop")]]),
                )
        except ServiceError as exc:
            await self.answer(update, str(exc), menu(browser.available))

    async def page(self, update, kind, page):
        page = max(0, min(10000, page))
        entries = self.db.page(PAGES[kind], page)
        rows = []
        info = []
        for row in entries[:5]:
            title = json.loads(row["details"])["title"]
            rows.append([button(title[:90], f"v:{row['id']}")])
            info.append(f"{title} · {LABELS[row['status']]}")
        navigation = []
        if page:
            navigation.append(button("‹ Назад", f"{kind}:{page - 1}"))
        if len(entries) > 5:
            navigation.append(button("Далее ›", f"{kind}:{page + 1}"))
        if navigation:
            rows.append(navigation)
        rows.append([button("➕ Вакансия", "add"), button("🏠 Меню", "start")])
        await self.answer(
            update,
            "\n".join(info)
            or (
                "Черновиков пока нет. Фактические отклики смотрите на HH."
                if kind == "drafts"
                else "Здесь пока пусто."
            ),
            markup(rows),
        )

    async def status(self, update, kind, settings):
        counts, used = self.db.counts(local_day(self.config))
        summary = [f"{LABELS[key]} — {value}" for key, value in sorted(counts.items())]
        if kind == "errors":
            summary = [f"{row['id']} · {row['error']}" for row in self.db.errors()]
            if error := self.db.get("scheduler_error"):
                summary.append(f"Последняя ошибка фонового цикла · {error}")
        elif kind == "status":
            next_due = self.db.get("next_reminder", 0)
            reminder = "выключены"
            if self.db.get("reminders", False):
                reminder = datetime.fromtimestamp(next_due, self.config.zone).strftime(
                    "до %d.%m %H:%M"
                )
            summary.extend(
                [
                    f"Напоминания — {reminder}",
                    f"Анализ — {'включён' if self.db.get('processing', True) else 'остановлен'}",
                    f"Согласие на Groq — {'дано' if self.db.get('groq_consent') else 'требуется'}",
                    f"Резюме — {'загружено' if self.db.get('resume') else 'требуется'}",
                ]
            )
        if kind != "errors":
            summary.append(f"Отмечено владельцем сегодня · {used}/{settings['limit']}")
        if not self.config.persistent and kind == "status":
            summary.append("Постоянство DATA_DIR не подтверждено. История может исчезнуть.")
        await self.answer(update, "\n".join(summary) or "Записей нет.", menu())

    async def diagnostics(self, update, kind):
        if self.diagnostic_task and not self.diagnostic_task.done():
            await self.answer(update, "Диагностика уже выполняется.")
            return
        await self.answer(update, "Проверяю настройки и доступные сервисы.")
        self.diagnostic_task = asyncio.create_task(self.run_diagnostics(update, kind))

    async def run_diagnostics(self, update, kind):
        lines = []
        try:
            if kind == "diagnose":
                lines.append(report(self.config, self.db))
                counts, used = self.db.counts(local_day(self.config))
                lines.append(f"В очереди анализа · {counts.get('queued', 0)}")
                lines.append(
                    f"Ручные отметки за день · {used}/{self.db.settings(self.config)['limit']}"
                )
                lines.append(
                    f"Резюме · {'загружено' if self.db.get('resume') else 'требуется настройка'}"
                )
                lines.append(
                    f"Планировщик · {'работает' if self.scheduler.task and not self.scheduler.task.done() else 'остановлен'}"
                )
                lines.extend(f"{row['id']} · {row['error']}" for row in self.db.errors())
            if kind in {"diagnose", "groq"}:
                try:
                    await self.scheduler.groq.check()
                    lines.append(
                        f"Groq — список моделей доступен, {self.config.model} присутствует."
                    )
                except ServiceError as exc:
                    lines.append(f"Groq — {exc}")
            if kind in {"diagnose", "browser"}:
                browser = self.scheduler.browser
                if browser.available:
                    try:
                        browser.require_browser()
                        lines.append("Chromium — исполняемый файл доступен.")
                    except ServiceError as exc:
                        lines.append(str(exc))
                    lines.append(
                        "Страница входа — "
                        + (
                            "локальный порт открыт. Внешний HTTPS проверяется с телефона."
                            if browser.view.runner
                            else "нужны HTTPS-домен и LOGIN_PUBLIC_URL."
                        )
                    )
                    if browser.provider.state.is_file():
                        lines.append(
                            "Состояние HH сохранено. Срок входа проверяется при обращении к HH."
                        )
                else:
                    lines.append("Локальный браузер выключен. Включите BROWSER_ENGINE=chromium.")
            await self.answer(update, "\n".join(lines), menu())
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("Ошибка диагностики (%s)", type(exc).__name__)

    async def settings(self, update, kind, value, settings):
        if kind == "set" and value in FIELDS:
            self.input = ("setting", value)
            hint = ""
            if value in OPTIONAL:
                hint = " Для списков используйте точку с запятой, для очистки — «-»."
            elif value in BOOLEANS:
                hint = " Ответьте «да» или «нет»."
            await self.answer(
                update,
                f"{FIELDS[value]}\nСейчас: {setting_value(settings[value])}\n"
                f"Отправьте новое значение.{hint}",
                markup([[button("‹ Назад", f"settings:{setting_section(value)}")]]),
            )
            return
        if kind == "choose" and value in {"manual", "confirm"}:
            self.save_setting("mode", value)
        if kind in {"mode", "choose"}:
            label = (
                "с подтверждением черновика"
                if self.db.settings(self.config)["mode"] == "confirm"
                else "ручной"
            )
            await self.answer(
                update,
                f"Режим {label}. Отклики отправляются только на HH владельцем.",
                markup(
                    [
                        [
                            button("Ручной", "choose:manual"),
                            button("Подтверждать черновики", "choose:confirm"),
                        ],
                        [button("Настройки", "settings"), button("Меню", "start")],
                    ]
                ),
            )
            return
        if value in SETTINGS_SECTIONS:
            title, keys = SETTINGS_SECTIONS[value]
            rows = [[button(FIELDS[key], f"set:{key}")] for key in keys]
            if value == "letters":
                rows.append([button("📨 Режим откликов", "mode")])
            rows.append([button("‹ Все настройки", "settings")])
            summary = "\n".join(f"{FIELDS[key]} — {setting_value(settings[key])}" for key in keys)
            await self.answer(update, f"{title}\n\n{summary}", markup(rows))
            return
        rows = [[button(title, f"settings:{key}")] for key, (title, _) in SETTINGS_SECTIONS.items()]
        rows.append([button("🏠 Меню", "start")])
        await self.answer(update, "⚙️ Что хотите настроить?", markup(rows))

    def save_setting(self, key, value):
        self.db.set("settings", self.db.get("settings", {}) | {key: value})
        if key == "interval":
            self.db.set("next_reminder", int(time.time()) + value * 60)

    async def resume(self, update, kind):
        if kind == "upload":
            self.input = ("resume", None)
            await self.answer(
                update,
                "Отправьте текст или документ TXT, DOCX, PDF до 2 МБ. "
                "Оставьте только профессиональный опыт и навыки. "
                "Уберите ФИО, контакты, даты рождения, адреса и секреты.",
            )
            return
        if kind == "view":
            text = self.db.get("resume", "")
            for offset in range(0, len(text), 3500):
                await self.answer(update, text[offset : offset + 3500])
            if text:
                return
        if kind == "consent":
            self.db.set("groq_consent", True)
        loaded = bool(self.db.get("resume", ""))
        consent = self.db.get("groq_consent", False)
        await self.answer(
            update,
            f"Резюме {'загружено' if loaded else 'не загружено'}. "
            f"Передача профессионального текста и вакансии в Groq "
            f"{'разрешена' if consent else 'требует согласия'}.",
            markup(
                [
                    [button("Загрузить / заменить", "upload"), button("Посмотреть", "view")],
                    *([] if consent else [[button("Разрешаю анализ в Groq", "consent")]]),
                    [button("Меню", "start")],
                ]
            ),
        )

    async def document(self, update):
        if not self.input or self.input[0] not in {"resume", "vacancy"}:
            await self.answer(update, "Выберите, что хотите загрузить.", menu())
            return
        document = update.message.document
        if not document.file_size or document.file_size > MAX_FILE:
            raise ValueError("Максимальный размер документа — 2 МБ.")
        if warning := resource_warning(self.config.data_dir):
            raise ValueError(warning)
        await self.answer(update, "Читаю документ.")
        remote = await document.get_file()
        data = await remote.download_as_bytearray()
        if self.input[0] == "vacancy":
            if not (document.file_name or "").lower().endswith(".txt") or len(data) > 72000:
                raise ValueError("Для карточки нужен TXT до 72 КБ.")
            try:
                text = data.decode("utf-8-sig")
            except UnicodeError:
                raise ValueError("Сохраните карточку в UTF-8.") from None
            await self.text(update, text)
            return
        text = await read_upload(document.file_name or "", data)
        await self.save_resume(update, text)

    async def save_resume(self, update, text):
        self.db.set_many(
            {
                "resume": checked(text),
                "groq_consent": False,
                "hh_selected_resume": {},
                "browser_auto_enabled": False,
                "browser_search": False,
            }
        )
        self.input = None
        await self.resume(update, "resume")

    async def text(self, update, text=None):
        text = text if text is not None else update.message.text
        if (
            (self.input is None or self.input[0] == "vacancy")
            and text.startswith("https://")
            and "\n" not in text
        ):
            identifier = vacancy_id(text.strip())
            if self.db.vacancy(identifier):
                self.input = None
                await self.card(update, identifier)
            else:
                self.input = ("vacancy", identifier)
                await self.answer(
                    update,
                    "Заполните карточку. Вставьте только сведения, которыми вправе делиться.",
                )
                await self.answer(
                    update, TEMPLATE.replace("Ссылка на вакансию", f"{HH}/vacancy/{identifier}")
                )
            return
        if self.input is None:
            await self.answer(update, "Выберите действие в меню.", menu())
            return
        action, value = self.input
        if action == "resume":
            await self.save_resume(update, text)
            return
        if action == "setting":
            updated = setting(value, text)
            self.save_setting(value, updated)
            self.input = None
            await self.settings(
                update, "settings", setting_section(value), self.db.settings(self.config)
            )
        elif action == "edit":
            if not 1 <= len(text.strip()) <= 1200:
                raise ValueError("Введите от 1 до 1200 символов.")
            row = self.db.vacancy(value)
            if row and row["status"] in {"draft", "approved", "deferred", "questions"}:
                self.db.transition(value, (row["status"],), row["status"], letter=text.strip())
            self.input = None
            await self.card(update, value)
        elif action == "vacancy":
            if value and not text.startswith("https://"):
                text = f"{HH}/vacancy/{value}\n{text}"
            identifier, details = parse_vacancy(text)
            inserted = self.db.add(identifier, details, review=True)
            self.input = None
            if inserted and self.db.get("processing", True):
                await self.scheduler.start(update.get_bot())
            await self.card(update, identifier)

    async def vacancy(self, update, kind, identifier, settings):
        row = self.db.vacancy(identifier)
        if not row:
            await self.answer(update, "Карточка не найдена.", menu())
            return
        status = row["status"]
        if (
            kind == "edit"
            and row["letter"]
            and status in {"draft", "approved", "deferred", "questions"}
        ):
            self.input = ("edit", identifier)
            await self.answer(update, "Отправьте новый текст письма до 1200 символов.")
            return
        if kind == "reconcile":
            try:
                found = await self.scheduler.browser.reconcile(identifier)
            except ServiceError as exc:
                await self.answer(update, str(exc))
                return
            if not found:
                await self.answer(
                    update, "HH пока не показывает отправку. Повторный отклик не отправлен."
                )
            await self.card(update, identifier)
            return
        if kind in {"confirm", "reported"} and status == "unknown" and row["sent_day"]:
            await self.card(update, identifier)
            return
        if kind == "confirm" and status in {"approved", "unknown", "questions"}:
            await self.answer(
                update,
                "Отмечайте отправку только после проверки вакансии в разделе «Отклики» на HH.",
                markup(
                    [
                        [link("Проверить отклики в HH", HH + "/applicant/negotiations")],
                        [
                            button("Вижу отправленный отклик", f"reported:{identifier}"),
                            button("Назад", f"v:{identifier}"),
                        ],
                    ]
                ),
            )
            return
        if kind == "reported":
            result = self.db.transition(
                identifier,
                ("approved", "unknown", "questions"),
                "reported",
                day=local_day(self.config),
                limit=settings["limit"],
            )
            if result == "limit":
                await self.answer(
                    update, "Сегодня достигнут лимит отметок. Статус карточки сохранён."
                )
                return
        elif kind == "exclude" and status not in {"reported", "unknown", "questions"}:
            company = json.loads(row["details"])["company"]
            names = list(dict.fromkeys([*terms(settings["employers"]), company]))
            self.save_setting("employers", "; ".join(names))
            self.db.transition(identifier, ("draft", "approved", "deferred", "queued"), "rejected")
        elif kind == "retry" and status == "error":
            self.db.add(identifier, json.loads(row["details"]), review=True)
        elif kind in {"approve", "reject", "defer", "questions", "unknown"}:
            transitions = {
                "approve": (("draft", "deferred"), "approved"),
                "reject": (("draft", "approved", "deferred", "queued"), "rejected"),
                "defer": (("draft", "approved"), "deferred"),
                "questions": (("approved",), "questions"),
                "unknown": (("approved", "questions"), "unknown"),
            }
            before, after = transitions[kind]
            self.db.transition(identifier, before, after)
        await self.card(update, identifier)

    async def card(self, update, identifier):
        row = self.db.vacancy(identifier)
        details = json.loads(row["details"])
        result = json.loads(row["result"] or "{}")
        status = row["status"]
        lines = [
            details["title"],
            f"{details['company']} · {details['region'] or 'регион не указан'}",
            details["salary"] or "Зарплата не указана",
            LABELS[status],
        ]
        if "score" in result:
            lines.append(f"Оценка Groq · {result['score']}%")
        if result.get("reason"):
            lines.append(result["reason"])
        for field, label in (("required", "Требования"), ("missing", "Пробелы")):
            if result.get(field):
                lines.append(f"{label}\n" + "\n".join(result[field])[:650])
        if row["letter"]:
            lines.append("Письмо\n" + row["letter"])
        if row["error"]:
            lines.append(row["error"])
        rows = [[link("Открыть на HH.ru", details["url"])]]
        if status in {"draft", "deferred", "approved", "questions"} and row["letter"]:
            rows.append([button("Редактировать письмо", f"edit:{identifier}")])
        if status in {"draft", "deferred"} and row["letter"]:
            rows.append([button("Подтвердить черновик", f"approve:{identifier}")])
        if status in {"draft", "approved", "deferred", "queued"}:
            rows.append(
                [
                    button("Отклонить", f"reject:{identifier}"),
                    button("Исключить работодателя", f"exclude:{identifier}"),
                ]
            )
        if status in {"draft", "approved"}:
            rows.append([button("Отложить", f"defer:{identifier}")])
        if status == "unknown" and row["sent_day"]:
            rows.append([button("Проверить результат на HH", f"reconcile:{identifier}")])
        elif status in {"approved", "unknown", "questions"}:
            rows.append([button("Отметить отправку", f"confirm:{identifier}")])
        if status == "approved":
            rows.append(
                [
                    button("Требуются ответы", f"questions:{identifier}"),
                    button("Результат неизвестен", f"unknown:{identifier}"),
                ]
            )
        if status == "error":
            rows.append([button("Повторить анализ", f"retry:{identifier}")])
        rows.append([button("Очередь", "drafts"), button("Меню", "start")])
        await self.answer(update, "\n\n".join(lines), markup(rows))
