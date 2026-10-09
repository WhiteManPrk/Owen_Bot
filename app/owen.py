"""Клиент OwenCloud API (https://api.owencloud.ru/)."""
import asyncio
import logging
import time
from typing import Any

import httpx

log = logging.getLogger(__name__)


class OwenError(Exception):
    pass


class OwenAuthError(OwenError):
    """Ключ недействителен или отозван."""


class OwenForbiddenError(OwenError):
    """Ключ рабочий, но у пользователя OwenCloud нет прав на действие (HTTP 403)."""


class OwenWriteError(OwenError):
    def __init__(self, message: str, code: str | None = None):
        super().__init__(message)
        self.code = code


def owen_time(ts: int) -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(ts)) + "GMT±0:00"


class OwenClient:
    def __init__(self, base_url: str, proxy: str | None = None, timeout: float = 30):
        self._http = httpx.AsyncClient(base_url=base_url.rstrip("/") + "/", timeout=timeout, proxy=proxy)

    async def close(self) -> None:
        await self._http.aclose()

    async def call(self, token: str, path: str, body: dict | None = None) -> Any:
        headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
        for attempt in range(3):
            started = time.monotonic()
            try:
                resp = await self._http.post(path, json=body or {}, headers=headers)
            except httpx.TransportError as e:
                if attempt == 2:
                    log.warning("OwenCloud %s: сеть %r, попытки исчерпаны", path, e)
                    raise OwenError(f"OwenCloud недоступен: {e!r}") from e
                log.warning("OwenCloud %s: сеть %r, повтор %s из 2", path, e, attempt + 1)
                await asyncio.sleep(2 ** attempt)
                continue
            log.debug("OwenCloud %s -> HTTP %s, %d байт, %.0f мс", path, resp.status_code,
                      len(resp.content), (time.monotonic() - started) * 1000)
            if resp.status_code == 401:
                raise OwenAuthError("Ключ OwenCloud недействителен")
            if resp.status_code >= 500 and attempt < 2:
                log.warning("OwenCloud %s: HTTP %s, повтор %s из 2", path, resp.status_code, attempt + 1)
                await asyncio.sleep(2 ** attempt)
                continue
            try:
                data = resp.json()
            except ValueError:
                raise OwenError(f"OwenCloud: HTTP {resp.status_code}, ответ не JSON")
            if isinstance(data, dict) and data.get("status") == "error":
                raise OwenWriteError(data.get("message") or "ошибка", data.get("code"))
            msg = data.get("message") if isinstance(data, dict) else None
            if resp.status_code == 403:
                raise OwenForbiddenError(msg or "нет прав")
            if resp.status_code >= 400:
                raise OwenError(f"OwenCloud: HTTP {resp.status_code} {msg or ''}".strip())
            return data
        raise OwenError("OwenCloud: исчерпаны попытки")

    async def devices(self, token: str, device_ids: list[int] | None = None) -> list[dict]:
        body = {"device_ids": device_ids} if device_ids else {}
        return await self.call(token, "device/index", body)

    async def device(self, token: str, device_id: int) -> dict:
        return await self.call(token, f"device/{device_id}")

    async def criteria(self, token: str, device_id: int) -> list[dict]:
        return await self.call(token, "event/list-by-device", {"device_ids": [device_id]})

    async def log_forward(self, token: str, device_id: int, start_ts: int, limit: int) -> list[dict]:
        return await self.call(
            token, f"device/events-log-forward/{device_id}",
            {"start": owen_time(start_ts), "limit": str(limit)},
        )

    async def log_backward(self, token: str, device_id: int, end_ts: int, limit: int) -> list[dict]:
        return await self.call(
            token, f"device/events-log-backward/{device_id}",
            {"end": owen_time(end_ts), "limit": str(limit)},
        )

    async def log_by_ids(self, token: str, device_id: int, ids: list[int]) -> list[dict]:
        return await self.call(token, "event-log/index", {"device_id": device_id, "ids": ids})

    async def mark_read(self, token: str, log_id: int) -> None:
        await self.call(token, f"event-log/read/{log_id}")

    async def unread_records(self, token: str) -> list[dict]:
        """Непрочитанные записи журнала событий приборов по всей компании."""
        return await self.call(token, "company/event-registration",
                               {"event_type": 0, "is_read": 0, "is_critical": -1, "is_end": -1})

    async def write(self, token: str, param_id: int, value: str, timeout: int = 60) -> tuple[int, list[int]]:
        """Команда записи -> (writeGroupId, [writeParamId])."""
        data = await self.call(token, "parameters/write-data", {
            "sms_tag": "", "sms_code": "", "timeout": timeout, "sync": False,
            "data": [{"id": param_id, "value": value}],
        })
        if isinstance(data, dict) and "writeGroupId" in data:
            return int(data["writeGroupId"]), [int(p["writeParamId"]) for p in data.get("writeParams") or []]
        if isinstance(data, dict):
            raise OwenWriteError(data.get("message") or str(data), data.get("code"))
        raise OwenWriteError(str(data))

    async def write_status(self, token: str, group_id: int, param_ids: list[int] | None = None) -> list[dict]:
        # API требует оба поля, хотя документация называет их взаимозаменяемыми; пустой список допустим
        return await self.call(token, "parameters/write-status",
                               {"writeGroupIds": [group_id], "writeParamIds": param_ids or []})
