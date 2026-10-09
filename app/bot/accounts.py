"""/start, справка и управление ключами OwenCloud."""
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from ..keyboards import NO_KEYBOARD
from ..owen import OwenAuthError, OwenError
from ..services import Services
from .ui import chat_of, ints, show

router = Router(name="accounts")

HELP = (
    "<b>Бот уведомлений OwenCloud</b>\n\n"
    "1. Добавьте API-ключ OwenCloud: /accounts (OwenCloud → профиль пользователя → API-ключ).\n"
    "2. Откройте прибор в /devices, включите уведомления и выберите события.\n"
    "3. Бот пришлёт сообщение о начале и окончании каждого выбранного события.\n\n"
    "В карточке прибора: «📊 Параметры» — текущие значения, «🎛 Управление» — запись параметров.\n\n"
    "Команды — в кнопке «Меню» слева от поля ввода:\n"
    "/devices — приборы\n"
    "/readall — отметить прочитанными все события по приборам с уведомлениями\n"
    "/accounts — аккаунты OwenCloud\n"
    "/cancel — отменить ввод"
)


class AddAccount(StatesGroup):
    token = State()
    name = State()


@router.message(CommandStart())
async def start(message: Message, state: FSMContext, svc: Services) -> None:
    await state.clear()
    await message.answer(HELP, reply_markup=NO_KEYBOARD)
    if not await svc.db.connections(message.chat.id):
        await ask_token(message, state)


@router.message(Command("help"))
async def help_(message: Message) -> None:
    await message.answer(HELP, reply_markup=NO_KEYBOARD)


@router.message(Command("cancel"))
async def cancel(message: Message, state: FSMContext) -> None:
    if await state.get_state() in (AddAccount.token, AddAccount.name):
        await cleanup(message, state)
    await state.clear()
    await message.answer("Отменено.", reply_markup=NO_KEYBOARD)


@router.message(Command("accounts"))
@router.callback_query(F.data == "acc")
async def accounts(event: Message | CallbackQuery, state: FSMContext, svc: Services) -> None:
    await state.clear()
    conns = await svc.db.connections(chat_of(event))
    kb = InlineKeyboardBuilder()
    lines = ["<b>Аккаунты OwenCloud</b>"]
    if not conns:
        lines.append("Пока нет ни одного ключа.")
    for c in conns:
        mark = " ⚠️ ключ не работает" if c["broken"] else ""
        lines.append(f"• {escape(c['name'])}{mark}")
        kb.button(text=f"🗑 {c['name']}", callback_data=f"accdel:{c['id']}")
    kb.button(text="➕ Добавить ключ", callback_data="accadd")
    kb.adjust(1)
    await show(event, "\n".join(lines), kb.as_markup())
    if isinstance(event, CallbackQuery):
        await event.answer()


@router.callback_query(F.data == "accadd")
async def add_account(cb: CallbackQuery, state: FSMContext) -> None:
    await ask_token(cb.message, state)
    await cb.answer()


async def track(state: FSMContext, *messages: Message) -> None:
    """Запоминает сообщения диалога настройки, чтобы удалить их по завершении."""
    ids = (await state.get_data()).get("cleanup", [])
    await state.update_data(cleanup=ids + [m.message_id for m in messages])


async def cleanup(message: Message, state: FSMContext) -> None:
    for message_id in (await state.get_data()).get("cleanup", []):
        try:
            await message.bot.delete_message(message.chat.id, message_id)
        except Exception:
            pass  # уже удалено или нет прав (в группе бот без прав администратора)


async def ask_token(message: Message, state: FSMContext) -> None:
    await state.set_state(AddAccount.token)
    await state.set_data({})
    prompt = await message.answer(
        "Отправьте API-ключ OwenCloud одним сообщением.\n"
        "Сообщение с ключом бот сразу удалит из чата. /cancel — отмена.")
    await track(state, prompt)


@router.message(AddAccount.token, F.text)
async def got_token(message: Message, state: FSMContext, svc: Services) -> None:
    token = message.text.strip()
    try:
        await message.delete()
    except Exception:
        pass
    if token.startswith("/") or " " in token:
        await track(state, await message.answer("Это не похоже на ключ. Отправьте ключ или /cancel."))
        return
    try:
        devices = await svc.owen.devices(token)
    except OwenAuthError:
        await track(state, await message.answer(
            "❌ OwenCloud не принял ключ. Проверьте его и отправьте снова, или /cancel."))
        return
    except OwenError as e:
        await track(state, await message.answer(
            f"⚠️ Не удалось проверить ключ: {e}\nПопробуйте ещё раз или /cancel."))
        return
    await state.update_data(token=token, devices=len(devices))
    await state.set_state(AddAccount.name)
    kb = InlineKeyboardBuilder()
    kb.button(text="Пропустить", callback_data="accskip")
    await track(state, await message.answer(
        f"✅ Ключ подходит, приборов: {len(devices)}.\n"
        "Как назвать аккаунт (например, название компании)?",
        reply_markup=kb.as_markup()))


@router.message(AddAccount.name, F.text)
async def got_name(message: Message, state: FSMContext, svc: Services) -> None:
    await track(state, message)
    await save_account(message, state, svc, message.text.strip()[:64])


@router.callback_query(AddAccount.name, F.data == "accskip")
async def skip_name(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    await cb.answer()
    await save_account(cb.message, state, svc, None)


async def save_account(message: Message, state: FSMContext, svc: Services, name: str | None) -> None:
    data = await state.get_data()
    await cleanup(message, state)
    await state.clear()
    token = data.get("token")
    if not token:
        await message.answer("Ключ потерялся — начните заново: /accounts → «➕ Добавить ключ».")
        return
    chat_id = message.chat.id
    if not name:
        name = f"Аккаунт {len(await svc.db.connections(chat_id)) + 1}"
    await svc.db.add_connection(chat_id, svc.crypto.encrypt(token), svc.crypto.fingerprint(token), name)
    kb = InlineKeyboardBuilder()
    kb.button(text="📟 Открыть приборы", callback_data="devs")
    await message.answer(
        f"Аккаунт «{escape(name)}» добавлен, приборов: {data.get('devices', 0)}.\n"
        "Откройте прибор и включите уведомления.", reply_markup=kb.as_markup())


@router.callback_query(F.data.startswith("accdel:"))
async def delete_ask(cb: CallbackQuery, svc: Services) -> None:
    (conn_id,) = ints(cb.data)
    conn = await svc.db.connection(conn_id, cb.message.chat.id)
    if conn is None:
        await cb.answer("Уже удалён")
        return
    kb = InlineKeyboardBuilder()
    kb.button(text="🗑 Удалить", callback_data=f"accdelok:{conn_id}")
    kb.button(text="Отмена", callback_data="acc")
    await show(cb, f"Удалить аккаунт «{escape(conn['name'])}»?\n"
                   "Уведомления по его приборам прекратятся.", kb.as_markup())
    await cb.answer()


@router.callback_query(F.data.startswith("accdelok:"))
async def delete_ok(cb: CallbackQuery, state: FSMContext, svc: Services) -> None:
    (conn_id,) = ints(cb.data)
    await svc.db.delete_connection(conn_id, cb.message.chat.id)
    await accounts(cb, state, svc)
