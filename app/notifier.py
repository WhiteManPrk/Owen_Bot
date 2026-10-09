import asyncio
import logging

from aiogram import Bot
from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter)
from aiogram.types import InlineKeyboardMarkup

from .db import Database

log = logging.getLogger(__name__)


class Notifier:
    """Отправка уведомлений с учётом лимитов Telegram."""

    def __init__(self, bot: Bot, db: Database, pause: float = 0.05):
        self.bot = bot
        self.db = db
        self.pause = pause

    async def send(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> bool:
        for attempt in range(5):
            try:
                await self.bot.send_message(chat_id, text, reply_markup=markup)
                await asyncio.sleep(self.pause)
                return True
            except TelegramRetryAfter as e:
                await asyncio.sleep(e.retry_after + 1)
            except TelegramForbiddenError:
                log.info("Чат %s заблокировал бота — подписки отключены", chat_id)
                await self.db.disable_chat(chat_id)
                return False
            except TelegramBadRequest as e:
                log.warning("Не удалось отправить в чат %s: %s", chat_id, e)
                return False
            except TelegramNetworkError as e:
                log.warning("Сеть Telegram (%s), попытка %s", e, attempt + 1)
                await asyncio.sleep(5 * (attempt + 1))
        log.error("Сообщение в чат %s не доставлено", chat_id)
        return False
