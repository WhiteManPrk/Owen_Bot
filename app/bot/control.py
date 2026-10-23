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
from ..owen import OwenError, OwenForbiddenError, OwenWriteError
from ..services import Services
from .ui import NO_ACCOUNT, connection_token, ints, owen_failure, show

log = logging.getLogger(__name__)
router = Router(name="control")

SMS_CODES = {"wrong_sms_tag", "expired_sms_code", "wrong_sms_code"}
NUMBER_RE = re.compile(r"^-?\d{1,12}([.,]\d{1,6})?$")  # short enough to fit into the retry button callback data
STATUS_POLL_TIMEOUT = 90


class Control(StatesGroup):
    value = State()


def manageable(device: dict) -> list[dict]:
    return [p for p in device.get("parameters", []) if p.get("in_manageable") and p.get("is_writable")]


def value_options(p: dict) -> list[tuple[str, str]]:
    if p.get("value_descriptions"):
        return [(str(v["value"]), v.get("description") or str(v["value"])) for v in p["value_descriptions"]]
    if p.get("format") == 2:  # an integer without value descriptions is usually a 0/1 flag
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


async def load_param(cb: CallbackQuery, svc: Services, conn_id: int, device_id: int, param_id: int):
    """Return (device, manageable parameter), or None after the error was shown."""
    conn, token = await connection_token(svc, cb.message.chat.id, conn_id)
    if not token:
        await cb.answer(NO_ACCOUNT, show_alert=True)
        return None
    try:
        device = await svc.device_info(token, device_id, fresh=True)
    except OwenError as e:
        await cb.answer(await owen_failure(svc, conn, e), show_alert=True)
        return None
    p = next((x for x in manageable(device) if x["id"] == param_id), None)
    if p is None:
        await cb.answer("Параметр больше недоступен для записи", show_alert=True)
        return None
    return device, p


@router.callback_query(F.data.startswith("cp:"))
async def pick_param(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    conn_id, device_id, param_id = ints(cb.data)
    loaded = await load_param(cb, svc, conn_id, device_id, param_id)
    if loaded is None:
        return
    device, p = loaded
    options = value_options(p)
    # Only typed values need the dialog state; buttons carry everything in their callback data.
    await state.set_state(Control.value)
    await state.set_data(dict(conn_id=conn_id, device_id=device_id, param_id=param_id,
                              name=p["name"], device_name=device.get("name", ""), options=options))
    kb = InlineKeyboardBuilder()
    for i, (_, label) in enumerate(options):
        kb.button(text=label, callback_data=f"cv:{conn_id}:{device_id}:{param_id}:{i}")
    kb.button(text="✖ Отмена", callback_data=f"ctl:{conn_id}:{device_id}")
    kb.adjust(*([2] * (len(options) // 2 + len(options) % 2)), 1)
    hint = "Выберите значение или отправьте его сообщением." if options else "Отправьте новое значение сообщением."
    await show(cb, f"<b>{escape(p['name'])}</b>\nТекущее значение: <b>{escape(param_value(p))}</b>\n{hint}",
               kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("cv:"))
async def value_button(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    conn_id, device_id, param_id, index = ints(cb.data)
    loaded = await load_param(cb, svc, conn_id, device_id, param_id)
    if loaded is None:
        return
    device, p = loaded
    options = value_options(p)
    if index >= len(options):
        await cb.answer("Кнопка устарела, откройте управление заново", show_alert=True)
        return
    await state.clear()
    value, label = options[index]
    await ask_confirm(cb, conn_id, device_id, p, device.get("name", ""), value, label)
    await cb.answer()


@router.message(Control.value, F.text)
async def value_text(message: Message, state: FSMContext) -> None:
    text = message.text.strip()
    data = await state.get_data()
    p = {"id": data["param_id"], "name": data["name"]}
    for value, label in data.get("options") or []:
        if text.lower() in (value.lower(), label.lower()):
            break
    else:
        if not NUMBER_RE.match(text):
            await message.answer("Нужно число (например, 1 или 25,5). Отправьте ещё раз или /cancel.")
            return
        value = label = text
    await state.clear()
    await ask_confirm(message, data["conn_id"], data["device_id"], p, data["device_name"], value, label)


def value_allowed(p: dict, value: str) -> bool:
    """Callback data can be forged: accept only what the dialog accepts, an option or a plain number."""
    return any(v == value for v, _ in value_options(p)) or bool(NUMBER_RE.match(value))


async def ask_confirm(event: Message | CallbackQuery, conn_id: int, device_id: int, p: dict, device_name: str,
                      value: str, label: str) -> None:
    data = f"cy:{conn_id}:{device_id}:{p['id']}:{value}"
    if len(data.encode()) > 64:
        await show(event, "Значение слишком длинное для подтверждения кнопкой.")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Записать", callback_data=data)
    kb.button(text="✖ Отмена", callback_data=f"cn:{conn_id}:{device_id}")
    shown = label if label == value else f"{label} ({value})"
    await show(event, f"Записать в «{escape(p['name'])}» ({escape(device_name)}) "
                      f"значение <b>{escape(shown)}</b>?", kb.as_markup())


@router.callback_query(F.data.startswith("cn:"))
async def cancel_write(cb: CallbackQuery, state: FSMContext) -> None:
    conn_id, device_id = ints(cb.data)
    await state.clear()
    kb = InlineKeyboardBuilder()
    kb.button(text="⬅️ К управлению", callback_data=f"ctl:{conn_id}:{device_id}")
    await show(cb, "Запись отменена.", kb.as_markup())
    await cb.answer()


_sending: set[tuple[int, int]] = set()  # confirmation messages being processed: a double tap must not write twice


@router.callback_query(F.data.startswith("cy:"))
async def confirm_write(cb: CallbackQuery, state: FSMContext, svc: Services, bot: Bot) -> None:
    key = (cb.message.chat.id, cb.message.message_id)
    if key in _sending:
        await cb.answer("Команда уже отправляется")
        return
    _sending.add(key)
    try:
        await _confirm_write(cb, state, svc, bot)
    finally:
        _sending.discard(key)


async def _confirm_write(cb: CallbackQuery, state: FSMContext, svc: Services, bot: Bot) -> None:
    _, conn_id, device_id, param_id, value = cb.data.split(":", 4)
    conn_id, device_id, param_id = int(conn_id), int(device_id), int(param_id)
    loaded = await load_param(cb, svc, conn_id, device_id, param_id)
    if loaded is None:
        return
    _, p = loaded
    if not value_allowed(p, value):
        await cb.answer("Недопустимое значение", show_alert=True)
        return
    await state.clear()
    conn, token = await connection_token(svc, cb.message.chat.id, conn_id)
    label = next((lbl for v, lbl in value_options(p) if v == value), value)
    title = write_title(p["name"], label)
    await show(cb, f"⏳ {title}\nОтправляю команду…")
    retry = result_keyboard("failed", conn_id, device_id, param_id, value)
    try:
        group_id, write_ids = await svc.owen.write(token, param_id, value)
    except OwenWriteError as e:
        reason = ("у компании включено SMS-подтверждение команд — запись через бота не поддерживается"
                  if e.code in SMS_CODES else str(e))
        await show(cb, f"❌ {title}\nOwenCloud отказал: {escape(reason)}", retry)
        await cb.answer()
        return
    except OwenForbiddenError:
        await show(cb, f"❌ {title}\nУ пользователя OwenCloud, которому принадлежит ключ, нет прав на управление "
                       "(роль «Управляющий командами»).", retry)
        await cb.answer()
        return
    except OwenError as e:
        await show(cb, f"❌ {title}\n{escape(await owen_failure(svc, conn, e))}", retry)
        await cb.answer()
        return
    log.info("Чат %s: запись параметра %s = %s, группа %s", cb.message.chat.id, param_id, value, group_id)
    await show(cb, f"⏳ {title}\nКоманда отправлена, ждём выполнения…")
    await cb.answer()
    asyncio.create_task(track_write(
        bot, svc, token, group_id, write_ids, cb.message.chat.id, cb.message.message_id, title,
        (conn_id, device_id, param_id, value)))


def write_title(name: str, label: str) -> str:
    return f"«{escape(name)}» = <b>{escape(label)}</b>"


def status_outcome(statuses: list[dict] | None) -> tuple[str, str]:
    if not statuses:
        return "unknown", "Статус команды не получен — проверьте позже."
    if any(s.get("in_progress") for s in statuses):
        return "pending", f"Команда ещё выполняется: {statuses[0].get('status', '')}"
    text = ", ".join(s.get("status", "") for s in statuses)
    return ("ok" if all(s.get("status_code") == 3 for s in statuses) else "failed"), text


OUTCOME_ICONS = {"ok": "✅", "failed": "❌", "pending": "⏳", "unknown": "❔"}


def result_keyboard(outcome: str, conn_id: int, device_id: int, param_id: int, value: str,
                    group_id: int | None = None):
    """Retry only after an explicit failure; with unknown status offer a status check,
    so the command is never sent to the equipment twice.
    """
    kb = InlineKeyboardBuilder()
    first_row = 0
    if outcome == "failed":
        data = f"cr:{conn_id}:{device_id}:{param_id}:{value}"
        if len(data.encode()) <= 64:
            kb.button(text="🔁 Повторить", callback_data=data)
            first_row = 1
    elif outcome in ("pending", "unknown") and group_id is not None:
        data = f"cs:{conn_id}:{device_id}:{param_id}:{group_id}:{value}"
        if len(data.encode()) <= 64:
            kb.button(text="🔄 Проверить статус", callback_data=data)
            first_row = 1
    kb.button(text="🎛 Управление", callback_data=f"ctl:{conn_id}:{device_id}")
    kb.button(text="📊 Параметры", callback_data=f"prm:{conn_id}:{device_id}")
    kb.adjust(*([1] if first_row else []), 2)
    return kb.as_markup()


@router.callback_query(F.data.startswith("cs:"))
async def check_status(cb: CallbackQuery, svc: Services) -> None:
    """Read-only status check of an already sent command; never writes."""
    _, conn_id, device_id, param_id, group_id, value = cb.data.split(":", 5)
    conn_id, device_id, param_id, group_id = int(conn_id), int(device_id), int(param_id), int(group_id)
    loaded = await load_param(cb, svc, conn_id, device_id, param_id)
    if loaded is None:
        return
    _, p = loaded
    _, token = await connection_token(svc, cb.message.chat.id, conn_id)
    try:
        statuses = await svc.owen.write_status(token, group_id)
    except OwenError as e:
        await cb.answer(f"OwenCloud не ответил: {e}", show_alert=True)
        return
    outcome, text = status_outcome(statuses)
    label = next((lbl for v, lbl in value_options(p) if v == value), value)
    await show(cb, f"{OUTCOME_ICONS[outcome]} {write_title(p['name'], label)}\n{escape(text)}",
               result_keyboard(outcome, conn_id, device_id, param_id, value, group_id))
    await cb.answer()


@router.callback_query(F.data.startswith("cr:"))
async def retry_write(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    _, conn_id, device_id, param_id, value = cb.data.split(":", 4)
    loaded = await load_param(cb, svc, int(conn_id), int(device_id), int(param_id))
    if loaded is None:
        return
    device, p = loaded
    await state.clear()
    label = next((lbl for v, lbl in value_options(p) if v == value), value)
    await ask_confirm(cb, int(conn_id), int(device_id), p, device.get("name", ""), value, label)
    await cb.answer()


async def track_write(bot: Bot, svc: Services, token: str, group_id: int, write_ids: list[int],
                      chat_id: int, message_id: int, title: str, target: tuple[int, int, int, str]) -> None:
    statuses: list[dict] = []
    elapsed = 0
    while elapsed < STATUS_POLL_TIMEOUT:
        await asyncio.sleep(3)
        elapsed += 3
        try:
            statuses = await svc.owen.write_status(token, group_id, write_ids)
        except OwenError as e:
            log.warning("Статус записи %s: %s", group_id, e)
            continue
        if statuses and not any(s.get("in_progress") for s in statuses):
            break
    outcome, text = status_outcome(statuses)
    log.info("Запись, группа %s: %s (%s)", group_id, outcome, text)
    markup = result_keyboard(outcome, *target, group_id=group_id)
    message = f"{OUTCOME_ICONS[outcome]} {title}\n{escape(text)}"
    try:
        await bot.edit_message_text(message, chat_id=chat_id, message_id=message_id, reply_markup=markup)
    except TelegramBadRequest:
        await bot.send_message(chat_id, message, reply_markup=markup)
