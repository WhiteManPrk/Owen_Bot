import time

import pytest

from app.poller import OVERLAP, RESEED_AFTER, Poller

from .conftest import DEVICE, FakeNotifier

CHAT = 111


@pytest.fixture
def notifier():
    return FakeNotifier()


@pytest.fixture
def poller(svc, notifier):
    return Poller(svc, notifier, interval=60)


async def subscribe(svc, chat=CHAT, token="good", mode="critical", name="Компания РИМ", events=None):
    conn_id = await svc.db.add_connection(chat, svc.crypto.encrypt(token), svc.crypto.fingerprint(token), name)
    await svc.db.save_subscription(chat, DEVICE, conn_id, enabled=True, mode=mode)
    if events is not None:
        await svc.db.set_selected_events(chat, DEVICE, set(events))
    return conn_id


async def test_first_poll_does_not_send_history(svc, owen, poller, notifier):
    now = int(time.time())
    owen.add(1, now - 600, now - 300)
    owen.add(2, now - 100)  # ещё идёт
    await subscribe(svc)
    await poller.tick()
    assert notifier.sent == []
    assert await svc.db.open_records(DEVICE) == [2]


async def test_start_and_end_sent_once(svc, owen, poller, notifier):
    now = int(time.time())
    await subscribe(svc)
    await poller.tick()  # начальная загрузка

    owen.add(10, now - 5, message="Не удалось поджечь")
    await poller.tick()
    await poller.tick()
    assert len(notifier.sent) == 1
    chat, text, markup = notifier.sent[0]
    assert chat == CHAT
    assert "Котельная РиМ — авария" in text and "Не удалось поджечь" in text
    assert markup.inline_keyboard[0][0].callback_data == "rd:1:10"

    owen.log[10]["end_dt"] = now
    await poller.tick()
    await poller.tick()
    assert len(notifier.sent) == 2
    assert "авария завершена" in notifier.sent[1][1]
    assert await svc.db.open_records(DEVICE) == []


async def test_short_event_is_single_end_message(svc, owen, poller, notifier):
    now = int(time.time())
    await subscribe(svc)
    await poller.tick()
    owen.add(20, now - 30, now - 10)
    await poller.tick()
    assert len(notifier.sent) == 1
    assert "авария завершена" in notifier.sent[0][1]


async def test_filters(svc, owen, poller, notifier):
    now = int(time.time())
    await subscribe(svc, chat=1, mode="critical")
    await subscribe(svc, chat=2, mode="all")
    await subscribe(svc, chat=3, mode="custom", events=[200])
    await poller.tick()

    owen.add(30, now - 5, critical=0, event_id=200, message="Котельная в режиме поджига")
    owen.add(31, now - 4, critical=1, event_id=100, message="Авария")
    await poller.tick()
    got = {}
    for chat, text, _ in notifier.sent:
        got.setdefault(chat, []).append(text.split("\n")[1])
    assert got[1] == ["Авария"]
    assert got[2] == ["Котельная в режиме поджига", "Авария"]
    assert got[3] == ["Котельная в режиме поджига"]


async def test_disabled_subscription_gets_nothing(svc, owen, poller, notifier):
    now = int(time.time())
    conn_id = await subscribe(svc)
    await poller.tick()
    await svc.db.save_subscription(CHAT, DEVICE, conn_id, enabled=False)
    owen.add(40, now - 5)
    await poller.tick()
    assert notifier.sent == []


async def test_end_of_old_open_record_found_by_id(svc, owen, poller, notifier):
    now = int(time.time())
    owen.add(50, now - 5 * OVERLAP)  # началось давно, выпадает из окна forward
    owen.add(51, now - 60, now - 30)  # свежая запись двигает курсор
    await subscribe(svc)
    await poller.tick()
    assert await svc.db.open_records(DEVICE) == [50]

    owen.log[50]["end_dt"] = now - 1
    await poller.tick()
    assert len(notifier.sent) == 1
    assert "завершена" in notifier.sent[0][1]
    assert "by_ids" in owen.calls


async def test_reseed_after_long_pause(svc, owen, poller, notifier):
    now = int(time.time())
    await subscribe(svc)
    await poller.tick()
    await svc.db.set_cursor(DEVICE, now - 10 * 3600, now - RESEED_AFTER - 60)
    owen.add(60, now - 3600)  # «пропущенное» за время паузы
    await poller.tick()
    assert notifier.sent == []


async def test_bad_key_marks_connection_broken_once(svc, owen, poller, notifier):
    await subscribe(svc, token="bad")
    await poller.tick()
    await poller.tick()
    assert len(notifier.sent) == 1
    assert "больше не работает" in notifier.sent[0][1]
    conn = (await svc.db.connections(CHAT))[0]
    assert conn["broken"] is True


async def test_access_code_limits_recipients(svc, owen, notifier):
    now = int(time.time())
    poller = Poller(svc, notifier, interval=60, access_hash="h1")
    await subscribe(svc, chat=1)
    await subscribe(svc, chat=2)
    await subscribe(svc, chat=3)
    await svc.db.grant_access(1, "h1")
    await svc.db.grant_access(3, "old-code")  # код сменился
    await poller.tick()
    owen.add(80, now - 5)
    await poller.tick()
    assert [chat for chat, _, _ in notifier.sent] == [1]
    assert await svc.db.has_access(1, "h1") and not await svc.db.has_access(3, "h1")


async def test_company_shown_only_with_several_accounts(svc, owen, poller, notifier):
    now = int(time.time())
    await subscribe(svc, name="Компания РИМ")
    await svc.db.add_connection(CHAT, svc.crypto.encrypt("other"), svc.crypto.fingerprint("other"), "Другая")
    await poller.tick()
    owen.add(70, now - 5)
    await poller.tick()
    assert "· Компания РИМ" in notifier.sent[0][1]
