import os

import pytest
from cryptography.fernet import Fernet

from app.crypto import TokenCrypto
from app.db import Database
from app.owen import OwenAuthError
from app.services import Services

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


class FakeNotifier:
    def __init__(self):
        self.sent: list[tuple[int, str, object]] = []

    async def send(self, chat_id, text, markup=None):
        self.sent.append((chat_id, text, markup))
        return True


@pytest.fixture
async def db():
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("TEST_DATABASE_URL не задан (запускайте через docker-compose.test.yml)")
    database = Database(url)
    await database.open()
    await database.pool.execute(
        "TRUNCATE connections, subscriptions, sub_events, poll_state, log_records, chat_access "
        "RESTART IDENTITY CASCADE")
    yield database
    await database.close()


@pytest.fixture
def crypto():
    return TokenCrypto(Fernet.generate_key().decode())


@pytest.fixture
def owen():
    return FakeOwen()


@pytest.fixture
def svc(db, owen, crypto):
    return Services(db, owen, crypto)
