"""Notification outbox: the poller stores messages in the DB, the sender delivers them.

- order within a chat is preserved: the next message waits until the previous one is sent;
- network errors, Telegram 5xx and 429 are retried with growing pauses (5 s ... 5 min);
- a chat that blocked the bot loses its queue and its subscriptions are disabled;
- a message rejected by Telegram (400) is dropped and logged;
- the queue survives restarts; messages older than OUTBOX_TTL are dropped with a warning.
"""
import asyncio
import logging
import time
from html import escape

from aiogram import Bot
from aiogram.exceptions import (TelegramAPIError, TelegramBadRequest, TelegramForbiddenError,
                                TelegramNetworkError, TelegramRetryAfter, TelegramServerError)
from aiogram.types import InlineKeyboardMarkup, ReplyParameters

from .db import Database

log = logging.getLogger(__name__)

LATE_AFTER = 120  # delivered later than this gets a delay note
MAX_BACKOFF = 300
LATE_NOTE = "\n⏳ <i>доставлено с задержкой</i>"


def read_mark(who: str | None) -> str:
    return f"\n☑️ <i>Прочитано{' · ' + escape(who) if who else ''}</i>"


def backoff(attempts: int) -> int:
    return min(5 * 2 ** attempts, MAX_BACKOFF)


class Notifier:
    def __init__(self, bot: Bot, db: Database, ttl_hours: int = 24, period: float = 3.0):
        self.bot = bot
        self.db = db
        self.ttl = ttl_hours * 3600
        self.period = period

    async def enqueue(self, chat_id: int, text: str, markup: InlineKeyboardMarkup | None = None, *,
                      log_id: int | None = None, device_id: int | None = None, connection_id: int | None = None,
                      reply: bool = False) -> None:
        """log_id ties the message to a journal record; reply sends it as a reply to the start message."""
        await self.db.outbox_add(chat_id, text, markup.model_dump_json(exclude_none=True) if markup else None,
                                 log_id, device_id, connection_id, reply)

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
        reply_to = None
        if msg.get("reply") and msg.get("log_id"):
            start = await self.db.event_message(chat_id, msg["log_id"])
            if start:  # the end replies to the start; the only read button stays on the start
                reply_to, markup = start["message_id"], None
        try:
            sent = await self.bot.send_message(
                chat_id, text, reply_markup=markup,
                reply_parameters=ReplyParameters(message_id=reply_to, allow_sending_without_reply=True)
                if reply_to else None)
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
        except TelegramAPIError as e:  # other Telegram errors: retry rather than lose the message
            await self._retry(msg, backoff(msg["attempts"]), f"{type(e).__name__}: {e}")
            return
        await self.db.outbox_delete(msg["id"])
        if msg.get("log_id") and not reply_to:
            await self.db.save_event_message(chat_id, msg["log_id"], sent.message_id, msg["device_id"],
                                             msg["connection_id"], text, markup is not None)
        log.debug("Уведомление в чат %s отправлено (попыток %s, задержка %s с)", chat_id, msg["attempts"] + 1, delay)
        if msg["attempts"]:
            log.info("Уведомление в чат %s доставлено с %s-й попытки, задержка %s с",
                     chat_id, msg["attempts"] + 1, delay)

    async def mark_read(self, row: dict, who: str) -> bool:
        """Remove the read button and append who read it.

        Returns False if Telegram is unreachable (retry later).
        """
        try:
            await self.bot.edit_message_text(row["text"] + read_mark(who), chat_id=row["chat_id"],
                                             message_id=row["message_id"], reply_markup=None)
        except (TelegramNetworkError, TelegramServerError, TelegramRetryAfter):
            return False
        except TelegramAPIError as e:  # deleted or already edited: nothing left to remove
            log.info("Сообщение %s в чате %s не изменено: %s", row["message_id"], row["chat_id"], e)
        await self.db.button_removed(row["chat_id"], row["log_id"])
        return True

    async def _retry(self, msg: dict, pause: int, error: str) -> None:
        await self.db.outbox_retry(msg["id"], int(time.time()) + pause, error)
        log.warning("Уведомление в чат %s не отправлено (%s), повтор через %s с", msg["chat_id"], error, pause)
