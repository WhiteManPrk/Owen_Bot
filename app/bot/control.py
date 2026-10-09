"""Запись управляемых параметров прибора: выбор параметра → значение → подтверждение → статус."""
import asyncio
import logging
import re
from html import escape

from aiogram import Bot, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..formatting import param_value
from ..owen import OwenError, OwenWriteError
from ..services import Services
from .ui import NO_ACCOUNT, connection_token, ints, owen_failure, show

log = logging.getLogger(__name__)
router = Router(name="control")

SMS_CODES = {"wrong_sms_tag", "expired_sms_code", "wrong_sms_code"}
NUMBER_RE = re.compile(r"^-?\d+([.,]\d+)?$")
STATUS_POLL_TIMEOUT = 90


class Control(StatesGroup):
    value = State()
    confirm = State()


def manageable(device: dict) -> list[dict]:
    return [p for p in device.get("parameters", []) if p.get("in_manageable") and p.get("is_writable")]


def value_options(p: dict) -> list[tuple[str, str]]:
    """[(значение, подпись)] для кнопок."""
    if p.get("value_descriptions"):
        return [(str(v["value"]), v.get("description") or str(v["value"])) for v in p["value_descriptions"]]
    if p.get("format") == 2:  # целочисленный параметр без описаний — чаще всего флаг 0/1
        return [("0", "0"), ("1", "1")]
    return []


@router.callback_query(F.data.startswith("ctl:"))
async def control_menu(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await state.clear()
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
    params = manageable(device)
    kb = InlineKeyboardBuilder()
    for p in params:
        kb.button(text=f"{p['name']} = {param_value(p)}", callback_data=f"cp:{conn_id}:{device_id}:{p['id']}")
    kb.button(text="⬅️ Назад", callback_data=f"dev:{conn_id}:{device_id}")
    kb.adjust(1)
    text = (f"🎛 <b>Управление · {escape(device.get('name', ''))}</b>\n"
            "⚠️ Команды выполняются на реальном оборудовании.\nВыберите параметр:")
    if not params:
        text = f"🎛 <b>{escape(device.get('name', ''))}</b>\nУправляемых параметров нет."
    await show(cb, text, kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("cp:"))
async def pick_param(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    conn_id, device_id, param_id = ints(cb.data)
    conn, token = await connection_token(svc, cb.message.chat.id, conn_id)
    if not token:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    try:
        device = await svc.device_info(token, device_id, fresh=True)
    except OwenError as e:
        await cb.answer(await owen_failure(svc, conn, e), show_alert=True)
        return
    p = next((x for x in manageable(device) if x["id"] == param_id), None)
    if p is None:
        await cb.answer("Параметр больше недоступен для записи", show_alert=True)
        return
    options = value_options(p)
    await state.set_state(Control.value)
    await state.update_data(conn_id=conn_id, device_id=device_id, param_id=param_id,
                            name=p["name"], device_name=device.get("name", ""), options=options)
    kb = InlineKeyboardBuilder()
    for i, (_, label) in enumerate(options):
        kb.button(text=label, callback_data=f"cv:{i}")
    kb.button(text="✖ Отмена", callback_data=f"ctl:{conn_id}:{device_id}")
    kb.adjust(*([2] * (len(options) // 2 + len(options) % 2)), 1)
    hint = "Выберите значение или отправьте его сообщением." if options else "Отправьте новое значение сообщением."
    await show(cb, f"<b>{escape(p['name'])}</b>\nТекущее значение: <b>{escape(param_value(p))}</b>\n{hint}",
               kb.as_markup())
    await cb.answer()


@router.callback_query(Control.value, F.data.startswith("cv:"))
async def value_button(cb: CallbackQuery, state: FSMContext) -> None:
    index = int(cb.data.split(":")[1])
    options = (await state.get_data()).get("options") or []
    if index >= len(options):
        await cb.answer("Устаревшая кнопка", show_alert=True)
        return
    value, label = options[index]
    await ask_confirm(cb, state, value, label)
    await cb.answer()


@router.message(Control.value, F.text)
async def value_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    options = (await state.get_data()).get("options") or []
    for value, label in options:
        if text.lower() in (value.lower(), label.lower()):
            await ask_confirm(message, state, value, label)
            return
    if not NUMBER_RE.match(text):
        await message.answer("Нужно число (например, 1 или 25,5). Отправьте ещё раз или /cancel.")
        return
    await ask_confirm(message, state, text, text)


async def ask_confirm(event: Message | CallbackQuery, state: FSMContext, value: str, label: str) -> None:
    data = await state.get_data()
    await state.update_data(value=value, label=label)
    await state.set_state(Control.confirm)
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Записать", callback_data="cy")
    kb.button(text="✖ Отмена", callback_data="cn")
    shown = label if label == value else f"{label} ({value})"
    await show(event, f"Записать в «{escape(data['name'])}» ({escape(data['device_name'])}) "
                      f"значение <b>{escape(shown)}</b>?", kb.as_markup())


@router.callback_query(Control.confirm, F.data == "cn")
@router.callback_query(Control.value, F.data == "cn")
async def cancel_write(cb: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    await state.clear()
    kb = InlineKeyboardBuilder()
    if data.get("conn_id"):
        kb.button(text="⬅️ К управлению", callback_data=f"ctl:{data['conn_id']}:{data['device_id']}")
    await show(cb, "Запись отменена.", kb.as_markup())
    await cb.answer()


@router.callback_query(Control.confirm, F.data == "cy")
async def confirm_write(cb: CallbackQuery, state: FSMContext, svc: Services, bot: Bot) -> None:
    data = await state.get_data()
    await state.clear()
    conn, token = await connection_token(svc, cb.message.chat.id, data["conn_id"])
    if not token:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return
    title = f"«{escape(data['name'])}» = <b>{escape(data['label'])}</b>"
    try:
        group_id = await svc.owen.write(token, data["param_id"], data["value"])
    except OwenWriteError as e:
        reason = ("у компании включено SMS-подтверждение команд — запись через бота не поддерживается"
                  if e.code in SMS_CODES else str(e))
        await show(cb, f"❌ {title}\nOwenCloud отказал: {escape(reason)}")
        await cb.answer()
        return
    except OwenError as e:
        await show(cb, f"❌ {title}\n{escape(await owen_failure(svc, conn, e))}")
        await cb.answer()
        return
    log.info("Чат %s: запись параметра %s = %s, группа %s",
             cb.message.chat.id, data["param_id"], data["value"], group_id)
    await show(cb, f"⏳ {title}\nКоманда отправлена, ждём выполнения…")
    await cb.answer()
    asyncio.create_task(track_write(bot, svc, token, group_id, cb.message.chat.id, cb.message.message_id,
                                    title, data["conn_id"], data["device_id"]))


async def track_write(bot: Bot, svc: Services, token: str, group_id: int, chat_id: int, message_id: int,
                      title: str, conn_id: int, device_id: int) -> None:
    """Следит за статусом команды и обновляет сообщение."""
    statuses: list[dict] = []
    elapsed = 0
    while elapsed < STATUS_POLL_TIMEOUT:
        await asyncio.sleep(3)
        elapsed += 3
        try:
            statuses = await svc.owen.write_status(token, group_id)
        except OwenError as e:
            log.warning("Статус записи %s: %s", group_id, e)
            continue
        if statuses and not any(s.get("in_progress") for s in statuses):
            break
    if not statuses:
        text = f"❔ {title}\nСтатус команды неизвестен — проверьте в OwenCloud."
    elif any(s.get("in_progress") for s in statuses):
        text = f"⏳ {title}\nКоманда ещё выполняется: {escape(statuses[0].get('status', ''))}"
    else:
        ok = all(s.get("status_code") == 3 for s in statuses)
        text = f"{'✅' if ok else '❌'} {title}\n{escape(', '.join(s.get('status', '') for s in statuses))}"
    kb = InlineKeyboardBuilder()
    kb.button(text="🎛 Управление", callback_data=f"ctl:{conn_id}:{device_id}")
    kb.button(text="📊 Параметры", callback_data=f"prm:{conn_id}:{device_id}")
    try:
        await bot.edit_message_text(text, chat_id=chat_id, message_id=message_id, reply_markup=kb.as_markup())
    except TelegramBadRequest:
        await bot.send_message(chat_id, text, reply_markup=kb.as_markup())
