import json

import httpx
import pytest

from app.bot.control import result_keyboard, status_outcome
from app.owen import OwenAuthError, OwenClient, OwenForbiddenError, OwenWriteError


def client_with(status: int, body) -> OwenClient:
    """OwenClient on a mock transport, no network."""
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
    # the real API answers 400 without writeParamIds
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
    # unknown status: no retry (the command may have run), only a status check
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
    with pytest.raises(TelegramNetworkError):  # getUpdates is not retried
        await mw(flaky, None, GetUpdates())
    assert len(calls) == 1


async def _noop():
    return None


async def test_stale_callback_ignored():
    from aiogram.exceptions import TelegramBadRequest
    from aiogram.methods import AnswerCallbackQuery, SendMessage

    from app.telegram import IgnoreStaleCallback

    async def stale(bot, method):
        raise TelegramBadRequest(method=method, message="Bad Request: query is too old and response timeout expired")

    mw = IgnoreStaleCallback()
    assert await mw(stale, None, AnswerCallbackQuery(callback_query_id="1")) is True
    with pytest.raises(TelegramBadRequest):  # other requests: the error as is
        await mw(stale, None, SendMessage(chat_id=1, text="x"))


PG_ENV = {"POSTGRES_HOST": "10.0.0.5", "POSTGRES_DB": "bot", "POSTGRES_USER": "u", "POSTGRES_PASSWORD": "p@ss:/1"}


@pytest.fixture
def clean_db_env(monkeypatch):
    for name in ("DB_BACKEND", "POSTGRES_PORT", *PG_ENV):
        monkeypatch.delenv(name, raising=False)
    return monkeypatch


@pytest.mark.parametrize("value, expected", [("sqlite", "sqlite"), ("POSTGRES", "postgres")])
def test_db_backend_choice(clean_db_env, value, expected):
    from app.config import db_backend
    clean_db_env.setenv("DB_BACKEND", value)
    assert db_backend() == expected


@pytest.mark.parametrize("value, message", [(None, "DB_BACKEND не задан"), ("mysql", "mysql")])
def test_db_backend_required(clean_db_env, value, message):
    from app.config import db_backend
    for name, v in PG_ENV.items():
        clean_db_env.setenv(name, v)  # PostgreSQL settings alone do not select the backend
    if value:
        clean_db_env.setenv("DB_BACKEND", value)
    with pytest.raises(SystemExit, match=message):
        db_backend()


def test_postgres_dsn(clean_db_env):
    from app.config import postgres_dsn, postgres_target
    for name, value in PG_ENV.items():
        clean_db_env.setenv(name, value)
    assert postgres_dsn() == "postgresql://u:p%40ss%3A%2F1@10.0.0.5:5432/bot"
    clean_db_env.setenv("POSTGRES_PORT", "6432")
    assert postgres_target() == "u@10.0.0.5:6432/bot"


@pytest.mark.parametrize("missing", list(PG_ENV))
def test_postgres_settings_required(clean_db_env, missing):
    from app.config import postgres_settings
    for name, value in PG_ENV.items():
        if name != missing:
            clean_db_env.setenv(name, value)
    with pytest.raises(SystemExit, match=missing):
        postgres_settings()


def test_is_volume(tmp_path):
    from app.config import is_volume
    assert is_volume("/proc")  # separate filesystem
    assert not is_volume("/usr/lib")  # image layer


def test_access_code_hash_is_keyed():
    from app.bot.access import code_hash, legacy_code_hash
    assert code_hash("2312", "key-a") != code_hash("2312", "key-b")
    assert code_hash("2312", "key-a") != legacy_code_hash("2312")
    assert code_hash(None, "key-a") is None


async def test_rehash_access(db):
    from app.bot.access import code_hash, legacy_code_hash
    await db.grant_access(1, legacy_code_hash("2312"))
    await db.rehash_access(legacy_code_hash("2312"), code_hash("2312", "k"))
    assert await db.has_access(1, code_hash("2312", "k"))
    assert not await db.has_access(1, legacy_code_hash("2312"))


async def test_sqlite_file_is_private(tmp_path):
    import os
    import stat

    from app.storage.backends import SqliteBackend
    backend = await SqliteBackend(str(tmp_path / "x.db")).open()
    assert stat.S_IMODE(os.stat(tmp_path / "x.db").st_mode) == 0o600
    await backend.close()
