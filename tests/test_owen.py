import httpx
import pytest

from app.bot.control import retry_keyboard
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
    assert await client_with(200, {"writeGroupId": 75, "writeParams": []}).write("t", 1, "1") == 75


def test_retry_keyboard():
    kb = retry_keyboard(12, 167962, 5550397, "1")
    assert kb.inline_keyboard[0][0].callback_data == "cr:12:167962:5550397:1"
    long_value = retry_keyboard(12, 167962, 5550397, "9" * 60)
    assert all(b.text != "🔁 Повторить" for row in long_value.inline_keyboard for b in row)


def test_proxy_url():
    from app.config import proxy_url
    assert proxy_url(None, "u", "p") is None
    assert proxy_url("socks5://h:1080", None, None) == "socks5://h:1080"
    assert proxy_url("socks5://h:1080", "user", "p@ss:w/d") == "socks5://user:p%40ss%3Aw%2Fd@h:1080"
    assert proxy_url("http://old:x@h:3128", "new", "y") == "http://new:y@h:3128"
    assert proxy_url("h:1080", "u", "p") == "socks5://u:p@h:1080"
