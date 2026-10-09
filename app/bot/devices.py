"""Список приборов, карточка прибора, выбор событий, текущие параметры."""
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..formatting import STATUS_TEXT, fmt_ts, params_message, parse_tz, split_message, status_icon
from ..keyboards import BTN_DEVICES
from ..owen import OwenError
from ..services import Services
from .ui import NO_ACCOUNT, chat_of, connection_token, ints, owen_failure, show

router = Router(name="devices")


@router.message(StateFilter(None), F.text == BTN_DEVICES)
@router.message(Command("devices"))
@router.callback_query(F.data == "devs")
async def devices(event: Message | CallbackQuery, state: FSMContext, svc: Services) -> None:
    await state.clear()
    chat_id = chat_of(event)
    conns = await svc.db.connections(chat_id)
    if not conns:
        kb = InlineKeyboardBuilder()
        kb.button(text="➕ Добавить ключ", callback_data="accadd")
        await show(event, "Нет ни одного аккаунта OwenCloud.", kb.as_markup())
        if isinstance(event, CallbackQuery):
            await event.answer()
        return

    subscribed = await svc.db.enabled_devices(chat_id)
    kb = InlineKeyboardBuilder()
    lines = ["<b>Приборы</b>", "🔴 авария · 🟠 непрочитанные · 🟢 на связи · ⚫ нет связи · 🔔 уведомления"]
    count = 0
    for conn in conns:
        token = svc.token(conn)
        try:
            if token is None:
                raise OwenError("ключ не расшифровывается")
            items = await svc.owen.devices(token)
        except OwenError as e:
            lines.append(f"\n{escape(conn['name'])}: {await owen_failure(svc, conn, e)}")
            continue
        if conn["broken"]:
            await svc.db.set_broken(conn["id"], False)
        prefix = f"{conn['name']} · " if len(conns) > 1 else ""
        for dev in sorted(items, key=lambda d: d.get("name", "")):
            bell = " 🔔" if subscribed.get(dev["id"]) == conn["id"] else ""
            kb.button(text=f"{status_icon(dev)} {prefix}{dev.get('name', dev['id'])}{bell}",
                      callback_data=f"dev:{conn['id']}:{dev['id']}")
            count += 1
    if not count:
        lines.append("\nПриборов нет.")
    kb.button(text="🔄 Обновить", callback_data="devs")
    kb.adjust(1)
    await show(event, "\n".join(lines), kb.as_markup())
    if isinstance(event, CallbackQuery):
        await event.answer()


def _mode_text(sub, criteria: list[dict], selected: set[int]) -> str:
    total = len(criteria)
    if sub is None or sub["mode"] == "critical":
        return f"только аварии ({sum(1 for c in criteria if c.get('is_critical'))} из {total})"
    if sub["mode"] == "all":
        return f"все события ({total})"
    return f"выбрано {len(selected & {c['id'] for c in criteria})} из {total}"


@router.callback_query(F.data.startswith("dev:"))
async def device_card(cb: CallbackQuery, svc: Services) -> None:
    await render_card(cb, svc, *ints(cb.data))


async def render_card(cb: CallbackQuery, svc: Services, conn_id: int, device_id: int) -> None:
    chat_id = cb.message.chat.id
    conn, token = await connection_token(svc, chat_id, conn_id)
    if not token:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    try:
        found = await svc.owen.devices(token, [device_id])
        criteria = await svc.criteria(token, device_id)
    except OwenError as e:
        await cb.answer(await owen_failure(svc, conn, e), show_alert=True)
        return
    dev = next((d for d in found if d["id"] == device_id), None)
    if dev is None:
        await cb.answer("Прибор недоступен этому ключу", show_alert=True)
        return

    sub = await svc.db.subscription(chat_id, device_id)
    enabled = bool(sub and sub["enabled"] and sub["connection_id"] == conn_id)
    selected = await svc.db.selected_events(chat_id, device_id)
    tz = parse_tz(dev.get("time_zone"))
    status = str(dev.get("status"))
    text = "\n".join([
        f"<b>{escape(dev.get('name', ''))}</b>",
        f"{escape(dev.get('type') or '')} · {escape(conn['name'])}",
        f"Состояние: {status_icon(dev)} {STATUS_TEXT.get(status, status)}",
        f"Данные: {fmt_ts(dev.get('last_dt'), tz)}",
        f"{'🔔 Уведомления включены' if enabled else '🔕 Уведомления выключены'}: "
        f"{_mode_text(sub, criteria, selected)}",
    ])
    kb = InlineKeyboardBuilder()
    kb.button(text="🔕 Выключить уведомления" if enabled else "🔔 Включить уведомления",
              callback_data=f"ntf:{conn_id}:{device_id}")
    kb.button(text="⚙️ Выбор событий", callback_data=f"evs:{conn_id}:{device_id}")
    kb.button(text="📊 Параметры", callback_data=f"prm:{conn_id}:{device_id}")
    kb.button(text="🎛 Управление", callback_data=f"ctl:{conn_id}:{device_id}")
    kb.button(text="⬅️ К списку", callback_data="devs")
    kb.adjust(1, 1, 2, 1)
    await show(cb, text, kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("ntf:"))
async def toggle_notifications(cb: CallbackQuery, svc: Services) -> None:
    conn_id, device_id = ints(cb.data)
    chat_id = cb.message.chat.id
    if await svc.db.connection(conn_id, chat_id) is None:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    sub = await svc.db.subscription(chat_id, device_id)
    enabled = bool(sub and sub["enabled"] and sub["connection_id"] == conn_id)
    await svc.db.save_subscription(chat_id, device_id, conn_id, enabled=not enabled)
    await render_card(cb, svc, conn_id, device_id)


# --- выбор событий ---

def _effective(sub, criteria: list[dict], selected: set[int]) -> set[int]:
    if sub is None or sub["mode"] == "critical":
        return {c["id"] for c in criteria if c.get("is_critical")}
    if sub["mode"] == "all":
        return {c["id"] for c in criteria}
    return selected


@router.callback_query(F.data.startswith("evs:"))
async def events_menu(cb: CallbackQuery, svc: Services) -> None:
    await render_events(cb, svc, *ints(cb.data))


async def render_events(cb: CallbackQuery, svc: Services, conn_id: int, device_id: int) -> None:
    chat_id = cb.message.chat.id
    conn, token = await connection_token(svc, chat_id, conn_id)
    if not token:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    try:
        criteria = await svc.criteria(token, device_id)
    except OwenError as e:
        await cb.answer(await owen_failure(svc, conn, e), show_alert=True)
        return
    sub = await svc.db.subscription(chat_id, device_id)
    chosen = _effective(sub, criteria, await svc.db.selected_events(chat_id, device_id))

    kb = InlineKeyboardBuilder()
    for c in sorted(criteria, key=lambda c: (not c.get("is_critical"), c.get("message") or "")):
        title = (c.get("message") or f"Событие {c['id']}")[:48]
        kb.button(text=f"{'✅' if c['id'] in chosen else '⬜'} {'🔴 ' if c.get('is_critical') else ''}{title}",
                  callback_data=f"evt:{conn_id}:{device_id}:{c['id']}")
    kb.button(text="Только аварии", callback_data=f"evm:{conn_id}:{device_id}:critical")
    kb.button(text="Все", callback_data=f"evm:{conn_id}:{device_id}:all")
    kb.button(text="Ничего", callback_data=f"evm:{conn_id}:{device_id}:none")
    kb.button(text="⬅️ Назад", callback_data=f"dev:{conn_id}:{device_id}")
    kb.adjust(*([1] * len(criteria)), 3, 1)
    await show(cb, "<b>Выбор событий</b>\nОтмеченные ✅ события будут приходить в уведомлениях "
                   "(начало и окончание). 🔴 — аварийные.", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("evt:"))
async def toggle_event(cb: CallbackQuery, svc: Services) -> None:
    conn_id, device_id, event_id = ints(cb.data)
    chat_id = cb.message.chat.id
    conn, token = await connection_token(svc, chat_id, conn_id)
    if not token:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    criteria = await svc.criteria(token, device_id)
    sub = await svc.db.subscription(chat_id, device_id)
    chosen = _effective(sub, criteria, await svc.db.selected_events(chat_id, device_id))
    chosen ^= {event_id}
    await svc.db.save_subscription(chat_id, device_id, conn_id, enabled=bool(chosen), mode="custom")
    await svc.db.set_selected_events(chat_id, device_id, chosen)
    await render_events(cb, svc, conn_id, device_id)


@router.callback_query(F.data.startswith("evm:"))
async def events_mode(cb: CallbackQuery, svc: Services) -> None:
    _, conn_id, device_id, mode = cb.data.split(":")
    conn_id, device_id = int(conn_id), int(device_id)
    chat_id = cb.message.chat.id
    if await svc.db.connection(conn_id, chat_id) is None:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    if mode == "none":
        await svc.db.save_subscription(chat_id, device_id, conn_id, enabled=False, mode="custom")
        await svc.db.set_selected_events(chat_id, device_id, set())
    else:
        await svc.db.save_subscription(chat_id, device_id, conn_id, enabled=True, mode=mode)
    await render_events(cb, svc, conn_id, device_id)


# --- параметры ---

@router.callback_query(F.data.startswith("prm:"))
async def parameters(cb: CallbackQuery, svc: Services) -> None:
    conn_id, device_id = ints(cb.data)
    conn, token = await connection_token(svc, cb.message.chat.id, conn_id)
    if not token:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    try:
        device = await svc.device_info(token, device_id, fresh=True)
    except OwenError as e:
        await cb.answer(await owen_failure(svc, conn, e), show_alert=True)
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🔄 Обновить", callback_data=f"prm:{conn_id}:{device_id}")
    kb.button(text="⬅️ Назад", callback_data=f"dev:{conn_id}:{device_id}")
    parts = split_message(params_message(device))
    for part in parts[:-1]:
        await cb.message.answer(part)
    await show(cb, parts[-1], kb.as_markup())
    await cb.answer("Обновлено")
