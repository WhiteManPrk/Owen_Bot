"""Закрытый режим: бот работает только в чатах, где ввели код доступа (ACCESS_CODE)."""
import hashlib
import hmac
import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, Message, TelegramObject

from ..services import Services

log = logging.getLogger(__name__)

MAX_FAILS = 5
FAIL_WINDOW = 15 * 60


def code_hash(code: str | None) -> str | None:
    return hashlib.sha256(code.encode()).hexdigest() if code else None


class AccessMiddleware(BaseMiddleware):
    def __init__(self, code: str | None):
        self.code = code
        self.hash = code_hash(code)
        self.fails: dict[int, list[float]] = {}

    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
                       event: TelegramObject, data: dict[str, Any]) -> Any:
        if not self.code:
            return await handler(event, data)
        svc: Services = data["svc"]
        message = event.message if isinstance(event, CallbackQuery) else event
        chat_id = message.chat.id
        if await svc.db.has_access(chat_id, self.hash):
            return await handler(event, data)

        if isinstance(event, CallbackQuery):
            await event.answer("🔒 Сначала введите код доступа", show_alert=True)
            return None
        assert isinstance(event, Message)
        text = (event.text or "").strip()
        if not text or text.startswith("/"):
            await event.answer("🔒 Бот закрыт. Отправьте код доступа одним сообщением.")
            return None

        now = time.monotonic()
        recent = [t for t in self.fails.get(chat_id, []) if now - t < FAIL_WINDOW]
        if len(recent) >= MAX_FAILS:
            minutes = int((FAIL_WINDOW - (now - recent[0])) // 60) + 1
            await event.answer(f"⛔ Слишком много попыток. Попробуйте через {minutes} мин.")
            return None
        try:
            await event.delete()
        except Exception:
            pass
        if not hmac.compare_digest(text.encode(), self.code.encode()):
            recent.append(now)
            self.fails[chat_id] = recent
            log.info("Чат %s: неверный код доступа", chat_id)
            await event.answer("❌ Неверный код.")
            return None

        self.fails.pop(chat_id, None)
        await svc.db.grant_access(chat_id, self.hash)
        log.info("Чат %s: доступ открыт", chat_id)
        await event.answer("✅ Доступ открыт.")
        from .accounts import start  # отложенный импорт: accounts не зависит от access
        return await start(event, data["state"], svc)
