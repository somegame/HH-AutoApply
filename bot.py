import asyncio
import fcntl
import logging
import os
import sqlite3
from pathlib import Path

import httpx
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from app.config import Config
from app.database import Database
from app.mini_app import MiniApp
from app.resume import MAX_FILE, read_upload
from app.scheduler import Scheduler
from app.services import ServiceError
from app.telegram_ui import COMMANDS, TelegramUI

log = logging.getLogger(__name__)


def build(config, db, client, *, telegram_request=None):
    scheduler = Scheduler(config, db, client)
    ui = TelegramUI(config, db, scheduler, client)

    async def startup(app):
        db.recover_browser_applications()
        if config.login_public_url:
            scheduler.browser.view.mini_app = MiniApp(config, db, scheduler, client, app.bot)
            try:
                await scheduler.browser.view.start()
            except ServiceError as exc:
                log.warning("%s", exc)
        await app.bot.set_my_commands(list(COMMANDS.items()))
        path = Path("resume.txt")
        if not db.get("resume") and path.is_file() and path.stat().st_size <= MAX_FILE:
            try:
                db.set("resume", await read_upload(path.name, path.read_bytes()))
            except (OSError, ValueError):
                log.warning("Не удалось прочитать локальный resume.txt")
        if db.get("browser_search") or db.get("processing", True) or db.get("reminders", False):
            await scheduler.start(app.bot)

    async def shutdown(app):
        await scheduler.browser.cancel_login()
        await scheduler.browser.view.shutdown()
        await scheduler.shutdown()
        if ui.diagnostic_task:
            ui.diagnostic_task.cancel()
            await asyncio.gather(ui.diagnostic_task, return_exceptions=True)
        await client.aclose()

    async def error(update, context):
        log.warning("Ошибка обработки Telegram (%s)", type(context.error).__name__)

    builder = Application.builder().token(config.token).post_init(startup).post_stop(shutdown)
    if telegram_request:
        builder.request(telegram_request).get_updates_request(telegram_request)
    app = builder.build()
    app.add_handler(MessageHandler(filters.TEXT | filters.Document.ALL, ui.handle))
    app.add_handler(CallbackQueryHandler(ui.handle))
    app.add_error_handler(error)
    return app


def main():
    os.umask(0o077)
    try:
        config = Config.load()
        db = Database(config.data_dir)
    except (ValueError, RuntimeError) as exc:
        raise SystemExit(f"Ошибка запуска. {exc}") from None
    except (OSError, sqlite3.Error):
        raise SystemExit("Ошибка запуска. Проверьте доступность DATA_DIR и SQLite.") from None
    logging.basicConfig(
        level=getattr(logging, config.log_level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    for name in ("httpx", "httpcore", "telegram", "playwright"):
        logging.getLogger(name).setLevel(logging.CRITICAL + 1)
    client = httpx.AsyncClient(timeout=httpx.Timeout(25, connect=8), follow_redirects=False)
    try:
        with (config.data_dir / "bot.lock").open("a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            build(config, db, client).run_polling(
                allowed_updates=["message", "callback_query"], bootstrap_retries=0
            )
    except BlockingIOError:
        raise SystemExit("В этом DATA_DIR уже запущен экземпляр бота.") from None
    except Exception as exc:
        raise SystemExit(f"Не удалось запустить Telegram polling ({type(exc).__name__}).") from None


if __name__ == "__main__":
    main()
