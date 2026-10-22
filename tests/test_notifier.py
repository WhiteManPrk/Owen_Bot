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
    """Move the next attempt into the past, as if the backoff had elapsed."""
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
    await notifier.flush()  # backoff not elapsed: no attempt
    queued = await outbox(db)
    assert len(queued) == 1 and queued[0]["attempts"] == 1 and "reset by proxy" in queued[0]["last_error"]
    assert queued[0]["next_try_at"] >= int(time.time()) + backoff(0) - 1

    await make_due(db)
    await notifier.flush()  # fails again (Bad Gateway)
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
    await notifier.flush()  # chat 1 start failed, so its end waits; chat 2 is not blocked
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
    await Notifier(bot, db).flush()  # new instance, same DB
    assert [t for _, t, _ in bot.sent] == ["до перезапуска"]


def test_backoff_capped():
    assert [backoff(n) for n in range(4)] == [5, 10, 20, 40]
    assert backoff(20) == 300


async def test_end_replies_to_start_without_button(db, bot, notifier):
    await notifier.enqueue(1, "начало", read_button(5, 77), log_id=77, device_id=9, connection_id=5)
    await notifier.flush()
    await notifier.enqueue(1, "окончание", read_button(5, 77), log_id=77, device_id=9, connection_id=5, reply=True)
    await notifier.flush()
    assert bot.replies == [None, 1001]
    assert bot.sent[1][2] is None  # one button per event, on the start message
    (row,) = await db.messages_with_button(9)
    assert (row["message_id"], row["text"]) == (1001, "начало")


async def test_end_without_start_keeps_button(db, bot, notifier):
    await notifier.enqueue(1, "окончание", read_button(5, 78), log_id=78, device_id=9, connection_id=5, reply=True)
    await notifier.flush()
    assert bot.replies == [None] and bot.sent[0][2] is not None
    assert [r["log_id"] for r in await db.messages_with_button(9)] == [78]


async def test_mark_read_edits_and_forgets(db, bot, notifier):
    await db.save_event_message(1, 79, 555, 9, 5, "🔴 авария", True)
    (row,) = await db.messages_with_button(9)
    bot.fail = [network_error()]
    assert await notifier.mark_read(row, "Artem") is False  # Telegram unreachable: retry later
    assert await db.messages_with_button(9)
    assert await notifier.mark_read(row, "Artem") is True
    assert bot.edited == [(1, 555, "🔴 авария\n☑️ <i>Прочитано · Artem</i>", None)]
    assert await db.messages_with_button(9) == []
