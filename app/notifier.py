"""Очередь уведомлений: поллер кладёт сообщения в БД, отправитель доставляет их с повторами.

- порядок внутри чата сохраняется: следующее сообщение чата ждёт, пока не уйдёт предыдущее;
- сетевые сбои, ошибки Telegram 5xx и 429 — повтор с нарастающей паузой (5 с … 5 мин);
- бот заблокирован пользователем — сообщения чата удаляются, его подписки отключаются;
- сообщение, которое Telegram отверг (400), удаляется с записью в лог;
- очередь в БД переживает перезапуск; старше OUTBOX_TTL — удаляется с предупреждением.
"""
import asyncio
import logging
import time

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter, TelegramServerError)
from aiogram.types import InlineKeyboardMarkup

from .db import Database

log = logging.getLogger(__name__)

LATE_AFTER = 120   # доставлено позже — с пометкой о задержке
MAX_BACKOFF = 300
LATE_NOTE = "\n⏳ <i>доставлено с задержкой</i>"


def backoff(attempts: int) -> int:
    """Пауза перед следующей попыткой: 5, 10, 20, 40 … но не больше 5 минут."""
    return min(5 * 2 ** attempts, MAX_BACKOFF)


class Notifier:
    def __init__(self, bot: Bot, db: Database, ttl_hours: int = 24, period: float = 3.0):
        self.bot = bot
        self.db = db
        self.ttl = ttl_hours * 3600
        self.period = period

    async def enqueue(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
        await self.db.outbox_add(chat_id, text, markup.model_dump_json(exclude_none=True) if markup else None)

    async def run(self) -> None:
        log.info("Отправитель уведомлений запущен, в очереди %s", await self.db.outbox_size())
        while True:
            try:
                await self.flush()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("Ошибка отправки очереди уведомлений")
            await asyncio.sleep(self.period)

    async def flush(self) -> None:
        now = int(time.time())
        expired = await self.db.outbox_expire(now - self.ttl)
        if expired:
            log.warning("Очередь: удалено %s уведомлений старше %s ч — так и не доставлены", expired, self.ttl // 3600)
        for msg in await self.db.outbox_heads(now):
            await self.deliver(msg)

    async def deliver(self, msg: dict) -> None:
        chat_id = msg["chat_id"]
        text = msg["text"]
        delay = int(time.time()) - msg["created_at"]
        if delay > LATE_AFTER:
            text += LATE_NOTE
        markup = InlineKeyboardMarkup.model_validate_json(msg["markup"]) if msg["markup"] else None
        try:
            await self.bot.send_message(chat_id, text, reply_markup=markup)
        except TelegramRetryAfter as e:
            await self._retry(msg, e.retry_after + 1, f"429: {e}")
            return
        except (TelegramNetworkError, TelegramServerError) as e:
            await self._retry(msg, backoff(msg["attempts"]), f"{type(e).__name__}: {e}")
            return
        except TelegramForbiddenError:
            dropped = await self.db.outbox_drop_chat(chat_id)
            await self.db.disable_chat(chat_id)
            log.info("Чат %s заблокировал бота — подписки отключены, удалено из очереди %s", chat_id, dropped)
            return
        except TelegramBadRequest as e:
            await self.db.outbox_delete(msg["id"])
            log.warning("Уведомление в чат %s отклонено Telegram и удалено: %s", chat_id, e)
            return
        except TelegramAPIError as e:  # прочие ответы Telegram — лучше повторить, чем потерять
            await self._retry(msg, backoff(msg["attempts"]), f"{type(e).__name__}: {e}")
            return
        await self.db.outbox_delete(msg["id"])
        log.debug("Уведомление в чат %s отправлено (попыток %s, задержка %s с)", chat_id, msg["attempts"] + 1, delay)
        if msg["attempts"]:
            log.info("Уведомление в чат %s доставлено с %s-й попытки, задержка %s с",
                     chat_id, msg["attempts"] + 1, delay)

    async def _retry(self, msg: dict, pause: int, error: str) -> None:
        await self.db.outbox_retry(msg["id"], int(time.time()) + pause, error)
        log.warning("Уведомление в чат %s не отправлено (%s), повтор через %s с", msg["chat_id"], error, pause)
