import json

import httpx
import pytest

from app.bot.control import result_keyboard, status_outcome
from app.owen import OwenAuthError, OwenClient, OwenForbiddenError, OwenWriteError


def client_with(status: int, body) -> OwenClient:
    """OwenClient на подменённом транспорте — без сетевых запросов."""
    client = OwenClient("https://example.invalid/v1")
    transport = httpx.MockTransport(lambda request: httpx.Response(status, json=body))
    client._http = httpx.AsyncClient(base_url="https://example.invalid/v1/", transport=transport)
    return client


async def test_forbidden():
    with pytest.raises(OwenForbiddenError):
        await client_with(403, {"message": "Forbidden"}).write("t", 1, "1")


async def test_auth():
    with pytest.raises(OwenAuthError):
        await client_with(401, {"message": "no"}).devices("t")


async def test_write_error_code():
    with pytest.raises(OwenWriteError) as e:
        await client_with(200, {"status": "error", "code": "wrong_sms_code", "message": "x"}).write("t", 1, "1")
    assert e.value.code == "wrong_sms_code"


async def test_write_ok():
    body = {"writeGroupId": 75, "writeParams": [{"paramId": 1, "writeParamId": 701}]}
    assert await client_with(200, body).write("t", 1, "1") == (75, [701])


async def test_write_status_sends_both_fields():
    sent = []

    def handler(request):
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=[{"writeParamId": 701, "in_progress": False,
                                          "status": "Выполнена", "status_code": 3}])

    client = OwenClient("https://example.invalid/v1")
    client._http = httpx.AsyncClient(base_url="https://example.invalid/v1/", transport=httpx.MockTransport(handler))
    await client.write_status("t", 75)
    await client.write_status("t", 75, [701])
    # реальный API отвечает 400, если нет writeParamIds
    assert sent == [{"writeGroupIds": [75], "writeParamIds": []}, {"writeGroupIds": [75], "writeParamIds": [701]}]


def test_status_outcome():
    assert status_outcome([])[0] == "unknown"
    assert status_outcome([{"in_progress": True, "status": "В обработке"}])[0] == "pending"
    assert status_outcome([{"in_progress": False, "status": "Выполнена", "status_code": 3}]) == ("ok", "Выполнена")
    assert status_outcome([{"in_progress": False, "status": "Отменена", "status_code": 6}])[0] == "failed"


def buttons(markup):
    return [b.callback_data for row in markup.inline_keyboard for b in row]


def test_result_keyboard():
    assert buttons(result_keyboard("failed", 12, 167962, 5550397, "1"))[0] == "cr:12:167962:5550397:1"
    # статус неизвестен — повтора нет (команда могла выполниться), только проверка статуса
    unknown = buttons(result_keyboard("unknown", 12, 167962, 5550397, "1", group_id=973773941))
    assert unknown[0] == "cs:12:167962:5550397:973773941:1"
    assert not any(b.startswith("cr:") for b in unknown)
    assert not any(b.startswith(("cr:", "cs:")) for b in buttons(result_keyboard("ok", 12, 1, 2, "1", group_id=3)))
    assert not any(b.startswith("cr:") for b in buttons(result_keyboard("failed", 12, 1, 2, "9" * 60)))


def test_proxy_url():
    from app.config import proxy_url
    assert proxy_url(None, "u", "p") is None
    assert proxy_url("socks5://h:1080", None, None) == "socks5://h:1080"
    assert proxy_url("socks5://h:1080", "user", "p@ss:w/d") == "socks5://user:p%40ss%3Aw%2Fd@h:1080"
    assert proxy_url("http://old:x@h:3128", "new", "y") == "http://new:y@h:3128"
    assert proxy_url("h:1080", "u", "p") == "socks5://u:p@h:1080"


def test_proxy_schemes():
    from app.config import OWEN_PROXY_SCHEMES, TG_PROXY_SCHEMES, check_scheme
    assert check_scheme("TG_PROXY", "socks4://h:1", TG_PROXY_SCHEMES) == "socks4://h:1"
    assert check_scheme("TG_PROXY", None, TG_PROXY_SCHEMES) is None
    with pytest.raises(SystemExit):
        check_scheme("TG_PROXY", "https://h:1", TG_PROXY_SCHEMES)
    with pytest.raises(SystemExit):
        check_scheme("OWEN_PROXY", "socks4://h:1", OWEN_PROXY_SCHEMES)


async def test_telegram_retry(monkeypatch):
    from aiogram.exceptions import TelegramNetworkError
    from aiogram.methods import GetMe, GetUpdates

    from app import telegram
    monkeypatch.setattr(telegram.asyncio, "sleep", lambda *_: _noop())
    calls = []

    async def flaky(bot, method):
        calls.append(method)
        if len(calls) < 3:
            raise TelegramNetworkError(method=method, message="reset")
        return "ok"

    mw = telegram.RetryOnNetworkError()
    assert await mw(flaky, None, GetMe()) == "ok" and len(calls) == 3

    calls.clear()
    with pytest.raises(TelegramNetworkError):  # getUpdates не повторяем
        await mw(flaky, None, GetUpdates())
    assert len(calls) == 1


async def _noop():
    return None
