import asyncio
import json
import time
from datetime import datetime

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, LinkPreviewOptions
from telegram.error import BadRequest

from app.config import FIELDS, OPTIONAL, setting
from app.database import ACTIVE, HISTORY, LABELS
from app.diagnostics import report, resource_warning
from app.resume import MAX_FILE, checked, read_upload
from app.scheduler import local_day
from app.services import ServiceError, browserbase_projects
from app.vacancies import HH, TEMPLATE, parse_vacancy, search_links, terms, vacancy_id

PREVIEW = LinkPreviewOptions(is_disabled=True)
MENUS = {
    "run": "Запустить поиск",
    "stop": "Остановить",
    "status": "Статус",
    "add": "Добавить вакансию",
    "found": "Найденные вакансии",
    "drafts": "Очередь откликов",
    "history": "История",
    "stats": "Статистика",
    "settings": "Настройки поиска",
    "resume": "Моё резюме",
    "login": "Авторизация HH",
    "diagnose": "Диагностика",
}
COMMANDS = {
    "start": "Главное меню",
    "help": "Справка",
    "run": "Поисковые ссылки и напоминания",
    "stop": "Остановить напоминания и анализ",
    "status": "Текущее состояние",
    "login": "Вход и проверка аккаунта на HH",
    "mobile": "Войти с iPhone",
    "finish": "Открыть профиль и проверить вход",
    "groq": "Диагностика Groq",
    "browserbase": "Проверка списка проектов Browserbase",
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


def menu():
    entries = list(MENUS.items())
    return markup(
        [
            [button(title, key) for key, title in entries[i : i + 2]]
            for i in range(0, len(entries), 2)
        ]
    )


def search_keyboard(settings):
    rows = [[link(query, url)] for query, url in search_links(settings)]
    rows.append(
        [link("Как включить автопоиск HH", "https://feedback.hh.ru/knowledge-base/article/0048")]
    )
    rows.append([button("Добавить вакансию", "add"), button("Меню", "start")])
    return markup(rows)


class TelegramUI:
    def __init__(self, config, db, scheduler, client):
        self.config = config
        self.db = db
        self.scheduler = scheduler
        self.client = client
        self.input = None
        self.diagnostic_task = None

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
        settings = self.db.settings(self.config)
        if kind == "start":
            title = "HH AutoApply · помощник соискателя"
            if not self.config.persistent:
                title += "\nПредупреждение. Постоянство DATA_DIR не подтверждено. Данные могут исчезнуть при обновлении."
            await self.answer(update, title, menu())
        elif kind == "help":
            await self.answer(
                update,
                "Сохраните автопоиск на HH. Получив вакансию, перенесите "
                "карточку в бот, проверьте письмо и отправьте отклик в HH. "
                "После проверки в разделе откликов отметьте его в боте.",
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
            await self.scheduler.shutdown()
            await self.answer(update, "Напоминания и анализ остановлены.", menu())
        elif kind in PAGES:
            await self.page(update, kind, int(value or 0))
        elif kind in {"status", "stats", "errors"}:
            await self.status(update, kind, settings)
        elif kind in {"login", "mobile", "finish"}:
            await self.answer(
                update,
                "Откройте HH в Safari или приложении. Введите код "
                "и пароль на HH. Проверьте вход по своему резюме или профилю.",
                markup(
                    [
                        [link("Войти в HH", HH + "/account/login")],
                        [link("Мои резюме на HH", HH + "/applicant/resumes")],
                        [button("Меню", "start")],
                    ]
                ),
            )
        elif kind in {"groq", "browserbase", "diagnose"}:
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
        }:
            await self.vacancy(update, kind, value, settings)

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
        rows.append([button("Добавить вакансию", "add"), button("Меню", "start")])
        await self.answer(update, "\n".join(info) or "Здесь пока пусто.", markup(rows))

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
            if kind in {"diagnose", "browserbase"}:
                try:
                    count = await browserbase_projects(self.client, self.config)
                    lines.append(
                        f"Browserbase — доступных проектов {count}. Браузер не запускался."
                    )
                except ServiceError as exc:
                    lines.append(f"Browserbase — {exc}")
            await self.answer(update, "\n".join(lines), menu())
        except Exception as exc:
            import logging

            logging.getLogger(__name__).warning("Ошибка диагностики (%s)", type(exc).__name__)

    async def settings(self, update, kind, value, settings):
        if kind == "set" and value in FIELDS:
            self.input = ("setting", value)
            hint = (
                " Для списков используйте точку с запятой, для очистки — «-»."
                if value in OPTIONAL
                else ""
            )
            await self.answer(update, f"{FIELDS[value]}. Введите новое значение.{hint}")
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
        rows = []
        for key, label in FIELDS.items():
            current = settings[key]
            if isinstance(current, bool):
                current = "да" if current else "нет"
            rows.append([button(f"{label} · {current or '—'}"[:95], f"set:{key}")])
        rows.append([button("Режим откликов", "mode"), button("Меню", "start")])
        await self.answer(update, "Настройки поиска, анализа и писем.", markup(rows))

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
        self.db.set_many({"resume": checked(text), "groq_consent": False})
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
            await self.settings(update, "settings", "", self.db.settings(self.config))
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
        if status in {"approved", "unknown", "questions"}:
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
