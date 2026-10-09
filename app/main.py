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
from .config import Config
from .crypto import TokenCrypto
from .db import Database
from .logs import setup_logging
from .notifier import Notifier
from .owen import OwenClient
from .poller import Poller
from .services import Services
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
    db = Database(cfg.database_url)
    await db.open()
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

    poller = Poller(svc, Notifier(bot, db), cfg.poll_interval, access_hash=code_hash(cfg.access_code))
    poll_task = asyncio.create_task(poller.run())
    try:
        await setup_menu(bot)
        await dp.start_polling(bot, allowed_updates=dp.resolve_used_update_types())
    finally:
        poll_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await poll_task
        await owen.close()
        await db.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
