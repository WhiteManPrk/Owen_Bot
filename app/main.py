import asyncio
import contextlib
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramNetworkError
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import BotCommand, MenuButtonCommands

from .bot import build_router
from .bot.access import AccessMiddleware, code_hash
from .bot.logging_mw import UpdateLogMiddleware
from .config import Config, is_volume
from .crypto import TokenCrypto
from .db import Database
from .dbtool import backup_loop, migration_hint
from .logs import setup_logging
from .notifier import Notifier
from .owen import OwenClient
from .poller import Poller
from .services import Services
from .storage.backends import open_backend
from .telegram import create_session

COMMANDS = [
    BotCommand(command="devices", description="Приборы"),
    BotCommand(command="readall", description="Отметить все события прочитанными"),
    BotCommand(command="accounts", description="Аккаунты OwenCloud"),
    BotCommand(command="help", description="Справка"),
    BotCommand(command="cancel", description="Отменить ввод"),
]


async def setup_menu(bot: Bot) -> None:
    """Меню команд. Сбой сети (например, прокси) не должен ронять запуск — меню уже могло быть задано раньше."""
    try:
        await bot.set_my_commands(COMMANDS)
        await bot.set_chat_menu_button(menu_button=MenuButtonCommands())
    except TelegramNetworkError as e:
        logging.warning("Меню команд не обновлено (%s), продолжаю без этого", e)


async def main() -> None:
    cfg = Config.from_env()
    setup_logging(cfg.log_level, cfg.log_dir)

    crypto = TokenCrypto(cfg.encryption_key)
    backend = await open_backend(cfg.db_backend, sqlite_path=cfg.sqlite_path,
                                 dsn=cfg.database_url if cfg.db_backend == "postgres" else None)
    db = Database(backend)
    await db.open()
    if cfg.db_backend == "sqlite":
        if not is_volume(os.path.dirname(cfg.sqlite_path)):
            logging.warning("ВНИМАНИЕ: база SQLite %s лежит внутри контейнера и пропадёт при его пересоздании. "
                            "Добавьте том ./data:/data (см. docker-compose.yml)", cfg.sqlite_path)
        await migration_hint(db_is_empty=not await backend.fetchval("SELECT count(*) FROM connections"))
    owen = OwenClient(cfg.owen_api_url, proxy=cfg.owen_proxy)
    svc = Services(db, owen, crypto)

    bot = Bot(cfg.bot_token, session=create_session(cfg.tg_proxy), default=DefaultBotProperties(parse_mode=ParseMode.HTML))
    dp = Dispatcher(storage=MemoryStorage())
    dp["svc"] = svc
    updates_log = UpdateLogMiddleware()
    dp.message.outer_middleware(updates_log)
    dp.callback_query.outer_middleware(updates_log)
    access = AccessMiddleware(cfg.access_code)
    dp.message.outer_middleware(access)
    dp.callback_query.outer_middleware(access)
    dp.include_router(build_router())
    logging.info("Owen Bot %s", os.environ.get("APP_VERSION", "dev"))
    logging.info("Доступ к боту: %s", "по коду (ACCESS_CODE)" if cfg.access_code else "открыт для всех")

    notifier = Notifier(bot, db, ttl_hours=cfg.outbox_ttl_hours)
    poller = Poller(svc, notifier, cfg.poll_interval, access_hash=code_hash(cfg.access_code))
    tasks = [asyncio.create_task(poller.run()), asyncio.create_task(notifier.run())]
    if cfg.backup_every_hours > 0:
        tasks.append(asyncio.create_task(backup_loop(cfg.backup_dir, cfg.backup_every_hours, cfg.backup_keep)))
    try:
        await setup_menu(bot)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        for task in tasks:
            task.cancel()
        for task in tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await task
        await owen.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
