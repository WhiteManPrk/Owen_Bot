import asyncio
import logging
from datetime import date, timedelta
from types import SimpleNamespace

import pytest
from aiogram.dispatcher.event.bases import UNHANDLED
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.base import StorageKey
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import CallbackQuery, User

from app.bot import control, stale_button
from app.bot.logging_mw import UpdateLogMiddleware
from tests.conftest import DEVICE

CHAT = 555


class FakeMessage:
    def __init__(self, message_id=10):
        self.chat = SimpleNamespace(id=CHAT)
        self.message_id = message_id
        self.texts: list[tuple[str, object]] = []

    async def edit_text(self, text, reply_markup=None):
        self.texts.append((text, reply_markup))

    async def answer(self, text, reply_markup=None):
        self.texts.append((text, reply_markup))


class FakeCallback:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or FakeMessage()
        self.from_user = SimpleNamespace(id=1, first_name="Artem")
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False):
        self.answers.append((text, show_alert))


@pytest.fixture
def state():
    return FSMContext(storage=MemoryStorage(), key=StorageKey(bot_id=1, chat_id=CHAT, user_id=1))


@pytest.fixture
async def conn_id(svc, owen, crypto, monkeypatch):
    owen.device_data["parameters"].append(
        {"id": 3, "name": "Пуск", "format": 2, "in_manageable": 1, "is_writable": 1, "value_descriptions": []})
    owen.writes = []

    async def write(token, param_id, value):  # the stub only records; nothing reaches real equipment
        owen.writes.append((token, param_id, value))
        return 77, [1]
    owen.write = write
    monkeypatch.setattr(control, "track_write", lambda *a, **k: asyncio.sleep(0))

    async def show(event, text, markup=None):
        (event.message if isinstance(event, FakeCallback) else event).texts.append((text, markup))
    monkeypatch.setattr(control, "show", show)
    return await svc.db.add_connection(CHAT, crypto.encrypt("tok"), "h", "РИМ")


async def test_confirm_works_after_dialog_state_was_cleared(svc, state, conn_id, owen, bot):
    cb = FakeCallback(f"cv:{conn_id}:{DEVICE}:3:1")
    await control.value_button(cb, state, svc)
    confirm = cb.message.texts[-1][1].inline_keyboard[0][0].callback_data
    assert confirm == f"cy:{conn_id}:{DEVICE}:3:1"

    await state.clear()  # e.g. /devices opened before pressing "Записать"
    await control.confirm_write(FakeCallback(confirm), state, svc, bot)
    assert owen.writes == [("tok", 3, "1")]


async def test_confirm_rejects_forged_value(svc, state, conn_id, owen, bot):
    cb = FakeCallback(f"cy:{conn_id}:{DEVICE}:3:1;drop")
    await control.confirm_write(cb, state, svc, bot)
    assert owen.writes == []
    assert cb.answers == [("Недопустимое значение", True)]


async def test_confirm_rejects_foreign_connection(svc, state, conn_id, owen, bot):
    cb = FakeCallback(f"cy:{conn_id}:{DEVICE}:3:1")
    cb.message.chat.id = CHAT + 1
    await control.confirm_write(cb, state, svc, bot)
    assert owen.writes == []


async def test_double_tap_writes_once(svc, state, conn_id, owen, bot):
    release = asyncio.Event()
    recorded = owen.write

    async def slow_write(*args):
        await release.wait()
        return await recorded(*args)
    owen.write = slow_write
    message = FakeMessage()
    first = asyncio.create_task(control.confirm_write(
        FakeCallback(f"cy:{conn_id}:{DEVICE}:3:1", message), state, svc, bot))
    await asyncio.sleep(0.05)
    second = FakeCallback(f"cy:{conn_id}:{DEVICE}:3:1", message)
    await control.confirm_write(second, state, svc, bot)
    release.set()
    await first
    assert len(owen.writes) == 1
    assert second.answers == [("Команда уже отправляется", False)]


async def test_typed_value_then_confirm(svc, state, conn_id, owen, bot):
    await control.pick_param(FakeCallback(f"cp:{conn_id}:{DEVICE}:3"), state, svc)
    message = FakeMessage()
    message.text = "1"
    await control.value_text(message, state)
    assert await state.get_state() is None
    confirm = message.texts[-1][1].inline_keyboard[0][0].callback_data
    await control.confirm_write(FakeCallback(confirm), state, svc, bot)
    assert owen.writes == [("tok", 3, "1")]


async def test_stale_button_answers():
    cb = FakeCallback("cy")  # a 1.3.0 confirmation button
    await stale_button(cb)
    assert cb.answers and cb.answers[0][1] is True


@pytest.mark.parametrize("until, level", [
    (date.today(), logging.INFO), (date.today() - timedelta(days=1), logging.DEBUG), (None, logging.DEBUG)])
async def test_action_log_until(caplog, until, level):
    mw = UpdateLogMiddleware(until)
    caplog.set_level(logging.DEBUG, logger="app.updates")

    async def unhandled(event, data):
        return UNHANDLED
    event = CallbackQuery(id="1", from_user=User(id=1, is_bot=False, first_name="A"), chat_instance="x", data="cy")
    await mw(unhandled, event, {})
    record = caplog.records[-1]
    assert record.levelno == level
    assert "кнопка cy" in record.getMessage() and "не обработано" in record.getMessage()
