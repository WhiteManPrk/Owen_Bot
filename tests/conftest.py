import os

import pytest
from cryptography.fernet import Fernet

from app.crypto import TokenCrypto
from app.db import Database
from app.notifier import Notifier
from app.owen import OwenAuthError
from app.services import Services
from app.storage.backends import PostgresBackend, SqliteBackend

DEVICE = 167962


class FakeOwen:
    """Заглушка OwenCloud: журнал событий в памяти. Никаких сетевых запросов."""

    def __init__(self):
        self.log: dict[int, dict] = {}
        self.calls: list[str] = []
        self.device_data = {
            "id": DEVICE, "name": "Котельная РиМ", "time_zone": "GMT+7:00",
            "parameters": [
                {"id": 1, "code": "P521", "name": "Режим работы",
                 "value_descriptions": [{"value": "0", "description": "Ожидание"}]},
                {"id": 2, "code": "P514", "name": "Температура в водяном контуре",
                 "value_descriptions": [], "measurement": {"title": "°C", "visible": 1}},
            ],
        }

    def add(self, log_id, start, end=None, critical=1, event_id=100, message="Авария", data=None):
        self.log[log_id] = {"id": log_id, "event_id": event_id, "start_dt": start, "end_dt": end,
                            "read_dt": None, "message": message, "data": data or [],
                            "device_id": DEVICE, "is_critical": critical, "type": 0}

    def _check(self, token):
        if token == "bad":
            raise OwenAuthError("bad")

    async def log_forward(self, token, device_id, start_ts, limit):
        self._check(token)
        self.calls.append("forward")
        recs = sorted((r for r in self.log.values() if r["start_dt"] >= start_ts), key=lambda r: r["start_dt"])
        return [dict(r) for r in recs[:limit]]

    async def log_backward(self, token, device_id, end_ts, limit):
        self._check(token)
        self.calls.append("backward")
        recs = sorted((r for r in self.log.values() if r["start_dt"] <= end_ts), key=lambda r: -r["start_dt"])
        return [dict(r) for r in recs[:limit]]

    async def log_by_ids(self, token, device_id, ids):
        self._check(token)
        self.calls.append("by_ids")
        return [dict(self.log[i]) for i in ids if i in self.log]

    async def device(self, token, device_id):
        self._check(token)
        return self.device_data

    async def criteria(self, token, device_id):
        return []

    async def write(self, *args, **kwargs):
        raise AssertionError("тесты не должны отправлять команды записи")


class FakeBot:
    """Заглушка Telegram: запоминает отправленное; fail — очередь исключений для следующих отправок."""

    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []
        self.fail: list[Exception] = []

    async def send_message(self, chat_id, text, reply_markup=None):
        if self.fail:
            raise self.fail.pop(0)
        self.sent.append((chat_id, text, reply_markup))


async def fresh_postgres() -> PostgresBackend:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL не задан (запускайте через docker-compose.test.yml)")
    backend = await PostgresBackend(url).open(attempts=3)
    await backend.execute("DROP SCHEMA public CASCADE")
    await backend.execute("CREATE SCHEMA public")
    return backend


async def fresh_sqlite(path) -> SqliteBackend:
    return await SqliteBackend(str(path)).open()


@pytest.fixture
async def pg_backend():
    backend = await fresh_postgres()
    yield backend
    await backend.close()


@pytest.fixture
async def sqlite_backend(tmp_path):
    backend = await fresh_sqlite(tmp_path / "owenbot.db")
    yield backend
    await backend.close()


@pytest.fixture(params=["sqlite", "postgres"])
async def db(request, tmp_path):
    """Каждый тест с этой фикстурой выполняется на обеих базах."""
    backend = await (fresh_sqlite(tmp_path / "owenbot.db") if request.param == "sqlite" else fresh_postgres())
    database = Database(backend)
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def bot():
    return FakeBot()


@pytest.fixture
def notifier(bot, db):
    return Notifier(bot, db)


async def outbox(db: Database) -> list[dict]:
    return await db.db.fetch("SELECT * FROM outbox ORDER BY id")


@pytest.fixture
def crypto():
    return TokenCrypto(Fernet.generate_key().decode())


@pytest.fixture
def owen():
    return FakeOwen()


@pytest.fixture
def svc(db, owen, crypto):
    return Services(db, owen, crypto)
