import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..notifier import Notifier, read_mark
from ..owen import OwenError
from ..services import Services
from .ui import NO_ACCOUNT, connection_token, ints, owen_failure, show

log = logging.getLogger(__name__)
router = Router(name="reading")


@router.callback_query(F.data.startswith("rd:"))
async def read_one(cb: CallbackQuery, svc: Services) -> None:
    conn_id, log_id = ints(cb.data)
    conn, token = await connection_token(svc, cb.message.chat.id, conn_id)
    if not token:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    try:
        await svc.owen.mark_read(token, log_id)
    except OwenError as e:
        await cb.answer(await owen_failure(svc, conn, e), show_alert=True)
        return
    try:
        await cb.message.edit_text(cb.message.html_text + read_mark(cb.from_user.first_name), reply_markup=None)
    except TelegramBadRequest as e:
        log.info("Сообщение не изменено: %s", e)
    await svc.db.button_removed(cb.message.chat.id, log_id)
    await cb.answer("Отмечено прочитанным")


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


async def unread_alarms(svc: Services, token: str, device_id: int) -> list[dict]:
    records = await svc.owen.unread_alarms(token)
    return [r for r in records if device_id in (r.get("device_ids") or [r.get("device_id")])]


@router.callback_query(F.data.startswith("ra:"))
async def read_alarms_ask(cb: CallbackQuery, svc: Services) -> None:
    conn_id, device_id = ints(cb.data)
    conn, token = await connection_token(svc, cb.message.chat.id, conn_id)
    if not token:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    try:
        records = await unread_alarms(svc, token, device_id)
    except OwenError as e:
        await cb.answer(await owen_failure(svc, conn, e), show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    if records:
        kb.button(text="☑️ Отметить прочитанными", callback_data=f"rak:{conn_id}:{device_id}")
    kb.button(text="⬅️ Назад", callback_data=f"dev:{conn_id}:{device_id}")
    kb.adjust(1)
    if not records:
        await show(cb, "Непрочитанных аварий нет.", kb.as_markup())
    else:
        lines = [f"Непрочитанных аварий: <b>{len(records)}</b>"]
        lines += [f"• {escape(r.get('message') or '—')}" for r in records[:15]]
        if len(records) > 15:
            lines.append(f"… и ещё {len(records) - 15}")
        await show(cb, "\n".join(lines), kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("rak:"))
async def read_alarms_ok(cb: CallbackQuery, svc: Services) -> None:
    conn_id, device_id = ints(cb.data)
    conn, token = await connection_token(svc, cb.message.chat.id, conn_id)
    if not token:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    try:
        criteria = await svc.criteria(token, device_id)
        marked = await svc.owen.mark_events_read(token, [c["id"] for c in criteria if c.get("is_critical")])
    except OwenError as e:
        await cb.answer(await owen_failure(svc, conn, e), show_alert=True)
        return
    who = marked[0].get("read_by_user") if marked else None
    notifier = Notifier(cb.bot, svc.db)
    for row in await svc.db.messages_with_button(log_ids=[m["id"] for m in marked]):
        await notifier.mark_read(row, who)
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К прибору", callback_data=f"dev:{conn_id}:{device_id}")
    await show(cb, f"☑️ Отмечено прочитанными: {len(marked)}", kb.as_markup())
    await cb.answer()
