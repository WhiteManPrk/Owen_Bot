"""Журнал действий пользователей (уровень DEBUG).

Текст обычных сообщений не пишется — в нём могут быть API-ключ OwenCloud или код доступа.
"""
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
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
    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        if not log.isEnabledFor(logging.DEBUG):
            return await handler(event, data)
        started = time.monotonic()
        try:
            return await handler(event, data)
        finally:
            log.debug("%s — %.0f мс", describe(event), (time.monotonic() - started) * 1000)
