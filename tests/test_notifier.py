import time

from aiogram.exceptions import (TelegramBadRequest, TelegramForbiddenError, TelegramNetworkError,
                                TelegramRetryAfter, TelegramServerError)
from aiogram.methods import SendMessage

from app.keyboards import read_button
from app.notifier import LATE_NOTE, Notifier, backoff

from .conftest import outbox

METHOD = SendMessage(chat_id=1, text="x")


def network_error():
    return TelegramNetworkError(method=METHOD, message="reset by proxy")


async def make_due(db):
    """Сдвинуть «следующую попытку» в прошлое — как будто пауза прошла."""
    await db.db.execute("UPDATE outbox SET next_try_at = 0")


async def test_delivers_and_clears(db, bot, notifier):
    await notifier.enqueue(1, "авария", read_button(5, 77))
    await notifier.flush()
    assert len(bot.sent) == 1
    chat, text, markup = bot.sent[0]
    assert (chat, text) == (1, "авария")
    assert markup.inline_keyboard[0][0].callback_data == "rd:5:77"
    assert await outbox(db) == []


async def test_retry_after_telegram_outage(db, bot, notifier):
    await notifier.enqueue(1, "авария")
    bot.fail = [network_error(), TelegramServerError(method=METHOD, message="Bad Gateway")]
    await notifier.flush()
    await notifier.flush()  # пауза ещё не прошла — попытки нет
    queued = await outbox(db)
    assert len(queued) == 1 and queued[0]["attempts"] == 1 and "reset by proxy" in queued[0]["last_error"]
    assert queued[0]["next_try_at"] >= int(time.time()) + backoff(0) - 1

    await make_due(db)
    await notifier.flush()  # снова сбой (Bad Gateway)
    assert (await outbox(db))[0]["attempts"] == 2
    await make_due(db)
    await notifier.flush()
    assert [t for _, t, _ in bot.sent] == ["авария"]
    assert await outbox(db) == []


async def test_order_inside_chat_kept(db, bot, notifier):
    await notifier.enqueue(1, "начало")
    await notifier.enqueue(1, "окончание")
    await notifier.enqueue(2, "другой чат")
    bot.fail = [network_error()]
    await notifier.flush()  # «начало» в чат 1 не ушло → «окончание» ждёт; чат 2 не блокируется
    assert [(c, t) for c, t, _ in bot.sent] == [(2, "другой чат")]
    await make_due(db)
    await notifier.flush()
    await notifier.flush()
    assert [(c, t) for c, t, _ in bot.sent] == [(2, "другой чат"), (1, "начало"), (1, "окончание")]


async def test_retry_after_429(db, bot, notifier):
    await notifier.enqueue(1, "x")
    bot.fail = [TelegramRetryAfter(method=METHOD, message="Too Many Requests", retry_after=30)]
    await notifier.flush()
    assert (await outbox(db))[0]["next_try_at"] >= int(time.time()) + 30


async def test_blocked_chat_dropped(db, bot, notifier):
    conn_id = await db.add_connection(1, "enc", "hash", "К")
    await db.save_subscription(1, 167962, conn_id, enabled=True)
    await notifier.enqueue(1, "a")
    await notifier.enqueue(1, "b")
    bot.fail = [TelegramForbiddenError(method=METHOD, message="bot was blocked by the user")]
    await notifier.flush()
    assert await outbox(db) == []
    assert not (await db.subscription(1, 167962))["enabled"]


async def test_bad_request_dropped(db, bot, notifier):
    await notifier.enqueue(1, "a")
    await notifier.enqueue(1, "b")
    bot.fail = [TelegramBadRequest(method=METHOD, message="can't parse entities")]
    await notifier.flush()
    await notifier.flush()
    assert [t for _, t, _ in bot.sent] == ["b"]


async def test_late_note_and_ttl(db, bot):
    notifier = Notifier(bot, db, ttl_hours=1)
    await notifier.enqueue(1, "задержалось")
    await notifier.enqueue(2, "протухло")
    now = int(time.time())
    await db.db.execute("UPDATE outbox SET created_at = ? WHERE chat_id = 1", now - 600)
    await db.db.execute("UPDATE outbox SET created_at = ? WHERE chat_id = 2", now - 2 * 3600)
    await notifier.flush()
    assert [(c, t) for c, t, _ in bot.sent] == [(1, "задержалось" + LATE_NOTE)]
    assert await outbox(db) == []


async def test_survives_restart(db, bot):
    await Notifier(bot, db).enqueue(1, "до перезапуска")
    await Notifier(bot, db).flush()  # новый экземпляр — та же БД
    assert [t for _, t, _ in bot.sent] == ["до перезапуска"]


def test_backoff_capped():
    assert [backoff(n) for n in range(4)] == [5, 10, 20, 40]
    assert backoff(20) == 300
