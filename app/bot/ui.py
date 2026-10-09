"""Общие помощники обработчиков."""
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from ..owen import OwenAuthError, OwenError
from ..services import Services


def chat_of(event: Message | CallbackQuery) -> int:
    return event.message.chat.id if isinstance(event, CallbackQuery) else event.chat.id


async def show(event: Message | CallbackQuery, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Для кнопки — редактирует сообщение с меню, для команды — отправляет новое."""
    if isinstance(event, CallbackQuery):
        try:
            await event.message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as e:
            if "not modified" in str(e):
                return
        await event.message.answer(text, reply_markup=markup)
    else:
        await event.answer(text, reply_markup=markup)


def ints(data: str) -> list[int]:
    """'dev:3:167962' -> [3, 167962]."""
    return [int(x) for x in data.split(":")[1:]]


async def connection_token(svc: Services, chat_id: int, conn_id: int):
    """(connection, token) аккаунта этого чата или (None, None)."""
    conn = await svc.db.connection(conn_id, chat_id)
    if conn is None:
        return None, None
    return conn, svc.token(conn)


async def owen_failure(svc: Services, conn, e: OwenError) -> str:
    if isinstance(e, OwenAuthError):
        await svc.db.set_broken(conn["id"], True)
        return f"⚠️ Ключ «{conn['name']}» не принят OwenCloud. Добавьте его заново в «🔑 Аккаунты»."
    return f"⚠️ OwenCloud не ответил: {e}"


NO_ACCOUNT = "Аккаунт не найден — возможно, он удалён. Откройте «🔑 Аккаунты»."
