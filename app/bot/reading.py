"""Отметка событий прочитанными в OwenCloud."""
import logging
from collections import Counter
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..owen import OwenError
from ..services import Services
from .ui import NO_ACCOUNT, chat_of, connection_token, ints, owen_failure, show

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
    who = cb.from_user.first_name if cb.from_user else ""
    done = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text=f"☑️ Прочитано · {who}"[:60], callback_data="noop")]])
    try:
        await cb.message.edit_reply_markup(reply_markup=done)
    except Exception:
        pass
    await cb.answer("Отмечено прочитанным")


@router.callback_query(F.data == "noop")
async def noop(cb: CallbackQuery) -> None:
    await cb.answer()


async def collect_unread(svc: Services, chat_id: int) -> tuple[list[tuple], list[str]]:
    """[(conn, token, запись журнала)] непрочитанных по приборам с включёнными уведомлениями."""
    enabled = await svc.db.enabled_devices(chat_id)
    items, errors = [], []
    for conn in await svc.db.connections(chat_id):
        devices = {d for d, c in enabled.items() if c == conn["id"]}
        token = svc.token(conn)
        if not devices or not token or conn["broken"]:
            continue
        try:
            records = await svc.owen.unread_records(token)
        except OwenError as e:
            errors.append(await owen_failure(svc, conn, e))
            continue
        for r in records:
            if set(r.get("device_ids") or [r.get("device_id")]) & devices:
                items.append((conn, token, r))
    return items, errors


@router.message(Command("readall"))
async def read_all_ask(message: Message, svc: Services) -> None:
    items, errors = await collect_unread(svc, message.chat.id)
    lines = [escape(e) for e in errors]
    if not items:
        lines.insert(0, "Непрочитанных событий по приборам с уведомлениями нет.")
        await message.answer("\n".join(lines))
        return
    by_event = Counter(r.get("message") or "—" for _, _, r in items)
    lines.insert(0, f"Непрочитанных событий: <b>{len(items)}</b>")
    lines += [f"• {escape(msg)} — {n}" for msg, n in by_event.most_common(15)]
    kb = InlineKeyboardBuilder()
    kb.button(text="✔ Отметить все прочитанными", callback_data="raok")
    kb.button(text="Отмена", callback_data="racancel")
    kb.adjust(1)
    await message.answer("\n".join(lines), reply_markup=kb.as_markup())


@router.callback_query(F.data == "racancel")
async def read_all_cancel(cb: CallbackQuery) -> None:
    await show(cb, "Отменено.")
    await cb.answer()


@router.callback_query(F.data == "raok")
async def read_all_ok(cb: CallbackQuery, svc: Services) -> None:
    await cb.answer("Отмечаю…")
    items, errors = await collect_unread(svc, chat_of(cb))
    done = failed = 0
    for conn, token, r in items:
        try:
            await svc.owen.mark_read(token, r["id"])
            done += 1
        except OwenError as e:
            failed += 1
            log.warning("Отметка %s: %s", r["id"], e)
    text = f"☑️ Отмечено прочитанными: {done}"
    if failed:
        text += f"\nНе удалось: {failed}"
    if errors:
        text += "\n" + "\n".join(escape(e) for e in errors)
    await show(cb, text)
