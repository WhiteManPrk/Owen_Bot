"""User action log: INFO until LOG_ACTIONS_UNTIL (inclusive), otherwise DEBUG.

Message text is never logged: it may contain an OwenCloud API key or the access code.
"""
import logging
import time
from datetime import date
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.types import CallbackQuery, Message, TelegramObject

log = logging.getLogger("app.updates")


def describe(event: TelegramObject) -> str:
    if isinstance(event, CallbackQuery):
        return f"чат {event.message.chat.id if event.message else '?'}, кнопка {event.data}"
    if isinstance(event, Message):
        text = event.text or ""
        what = text.split()[0] if text.startswith("/") else f"сообщение ({len(text)} симв.)"
        return f"чат {event.chat.id}, {what}"
    return type(event).__name__


class UpdateLogMiddleware(BaseMiddleware):
    def __init__(self, until: date | None = None):
        self.until = until

    def level(self) -> int:
        return logging.INFO if self.until and date.today() <= self.until else logging.DEBUG

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        level = self.level()
        if not log.isEnabledFor(level):
            return await handler(event, data)
        started = time.monotonic()
        result = None
        try:
            result = await handler(event, data)
            return result
        finally:
            user = getattr(event, "from_user", None)
            log.log(level, "%s, пользователь %s%s — %.0f мс", describe(event), user.id if user else "?",
                    ", не обработано" if result is UNHANDLED else "", (time.monotonic() - started) * 1000)
