import time
from typing import Awaitable, Callable

from .crypto import TokenCrypto
from .db import Database
from .owen import OwenClient

CACHE_TTL = 600


class Services:
    """Общие зависимости бота и поллера + кэш справочников OwenCloud."""

    def __init__(self, db: Database, owen: OwenClient, crypto: TokenCrypto):
        self.db = db
        self.owen = owen
        self.crypto = crypto
        self._cache: dict[tuple, tuple[float, object]] = {}

    def token(self, conn) -> str | None:
        return self.crypto.decrypt(conn["token_enc"])

    async def _cached(self, key: tuple, factory: Callable[[], Awaitable], fresh: bool):
        hit = self._cache.get(key)
        if hit and not fresh and hit[0] > time.monotonic():
            return hit[1]
        value = await factory()
        self._cache[key] = (time.monotonic() + CACHE_TTL, value)
        return value

    async def device_info(self, token: str, device_id: int, fresh: bool = False) -> dict:
        """Прибор с параметрами (device/:id). fresh=True — актуальные значения."""
        return await self._cached(("device", device_id), lambda: self.owen.device(token, device_id), fresh)

    async def criteria(self, token: str, device_id: int, fresh: bool = False) -> list[dict]:
        """Настроенные события прибора."""
        return await self._cached(("criteria", device_id), lambda: self.owen.criteria(token, device_id), fresh)
