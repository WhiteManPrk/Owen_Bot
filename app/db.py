"""Работа с данными бота. Один набор запросов для SQLite и PostgreSQL (через адаптер storage.backends)."""
import logging
import time

from .storage.backends import Backend
from .storage.schema import migrate

log = logging.getLogger(__name__)

MODES = ("critical", "all", "custom")


def _in(values) -> str:
    return ", ".join("?" for _ in values)


class Database:
    def __init__(self, backend: Backend):
        self.db = backend

    @property
    def dialect(self) -> str:
        return self.db.dialect

    async def open(self) -> None:
        version = await migrate(self.db)
        log.info("БД: %s, версия схемы %s", self.db.dialect, version)

    async def close(self) -> None:
        await self.db.close()

    # --- аккаунты OwenCloud ---

    async def add_connection(self, chat_id: int, token_enc: str, token_hash: str, name: str) -> int:
        return await self.db.fetchval(
            "INSERT INTO connections (chat_id, token_enc, token_hash, name) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (chat_id, token_hash) DO UPDATE SET token_enc = excluded.token_enc, "
            "name = excluded.name, broken = ? RETURNING id",
            chat_id, token_enc, token_hash, name, False)

    async def connections(self, chat_id: int) -> list[dict]:
        return await self.db.fetch("SELECT * FROM connections WHERE chat_id = ? ORDER BY id", chat_id)

    async def connection(self, conn_id: int, chat_id: int) -> dict | None:
        return await self.db.fetchrow("SELECT * FROM connections WHERE id = ? AND chat_id = ?", conn_id, chat_id)

    async def delete_connection(self, conn_id: int, chat_id: int) -> None:
        await self.db.execute("DELETE FROM connections WHERE id = ? AND chat_id = ?", conn_id, chat_id)

    async def set_broken(self, conn_id: int, broken: bool) -> None:
        await self.db.execute("UPDATE connections SET broken = ? WHERE id = ?", broken, conn_id)

    # --- подписки ---

    async def subscription(self, chat_id: int, device_id: int) -> dict | None:
        return await self.db.fetchrow(
            "SELECT * FROM subscriptions WHERE chat_id = ? AND device_id = ?", chat_id, device_id)

    async def enabled_devices(self, chat_id: int) -> dict[int, int]:
        """device_id -> connection_id для включённых подписок чата."""
        rows = await self.db.fetch(
            "SELECT device_id, connection_id FROM subscriptions WHERE chat_id = ? AND enabled = ?", chat_id, True)
        return {r["device_id"]: r["connection_id"] for r in rows}

    async def save_subscription(self, chat_id: int, device_id: int, conn_id: int,
                                enabled: bool | None = None, mode: str | None = None) -> None:
        assert mode is None or mode in MODES
        # None — «не менять»: при вставке берётся значение по умолчанию, при обновлении — текущее
        await self.db.execute(
            "INSERT INTO subscriptions (chat_id, device_id, connection_id, enabled, mode) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT (chat_id, device_id) DO UPDATE SET connection_id = excluded.connection_id, "
            "enabled = COALESCE(CAST(? AS BOOLEAN), subscriptions.enabled), "
            "mode = COALESCE(CAST(? AS TEXT), subscriptions.mode)",
            chat_id, device_id, conn_id, bool(enabled), mode or "critical", enabled, mode)

    async def selected_events(self, chat_id: int, device_id: int) -> set[int]:
        rows = await self.db.fetch(
            "SELECT event_id FROM sub_events WHERE chat_id = ? AND device_id = ?", chat_id, device_id)
        return {r["event_id"] for r in rows}

    async def set_selected_events(self, chat_id: int, device_id: int, event_ids: set[int]) -> None:
        async with self.db.transaction() as tx:
            await tx.execute("DELETE FROM sub_events WHERE chat_id = ? AND device_id = ?", chat_id, device_id)
            if event_ids:
                await tx.executemany("INSERT INTO sub_events (chat_id, device_id, event_id) VALUES (?, ?, ?)",
                                     [(chat_id, device_id, e) for e in event_ids])

    async def active_subscriptions(self, access_hash: str | None = None) -> list[dict]:
        """Включённые подписки с рабочими ключами + выбранные события (для режима custom).

        access_hash — если бот закрыт кодом, только чаты, введшие текущий код.
        """
        sql = ("SELECT s.chat_id, s.device_id, s.mode, c.id AS connection_id, c.token_enc, c.name AS company, "
               "  (SELECT count(*) FROM connections c2 WHERE c2.chat_id = s.chat_id) AS chat_connections "
               "FROM subscriptions s JOIN connections c ON c.id = s.connection_id "
               "WHERE s.enabled = ? AND c.broken = ?")
        args: list = [True, False]
        if access_hash is not None:
            sql += " AND EXISTS (SELECT 1 FROM chat_access a WHERE a.chat_id = s.chat_id AND a.code_hash = ?)"
            args.append(access_hash)
        subs = await self.db.fetch(sql + " ORDER BY s.device_id, c.id", *args)
        events: dict[tuple[int, int], list[int]] = {}
        for r in await self.db.fetch("SELECT chat_id, device_id, event_id FROM sub_events"):
            events.setdefault((r["chat_id"], r["device_id"]), []).append(r["event_id"])
        for s in subs:
            s["event_ids"] = events.get((s["chat_id"], s["device_id"]), [])
        return subs

    async def disable_chat(self, chat_id: int) -> None:
        await self.db.execute("UPDATE subscriptions SET enabled = ? WHERE chat_id = ?", False, chat_id)

    # --- доступ по коду ---

    async def has_access(self, chat_id: int, code_hash: str) -> bool:
        return bool(await self.db.fetchval(
            "SELECT 1 FROM chat_access WHERE chat_id = ? AND code_hash = ?", chat_id, code_hash))

    async def grant_access(self, chat_id: int, code_hash: str) -> None:
        await self.db.execute(
            "INSERT INTO chat_access (chat_id, code_hash) VALUES (?, ?) "
            "ON CONFLICT (chat_id) DO UPDATE SET code_hash = excluded.code_hash, granted_at = CURRENT_TIMESTAMP",
            chat_id, code_hash)

    # --- состояние поллера ---

    async def poll_state(self, device_id: int) -> dict | None:
        return await self.db.fetchrow("SELECT cursor, polled_at FROM poll_state WHERE device_id = ?", device_id)

    async def set_cursor(self, device_id: int, cursor: int, polled_at: int) -> None:
        await self.db.execute(
            "INSERT INTO poll_state (device_id, cursor, polled_at) VALUES (?, ?, ?) "
            "ON CONFLICT (device_id) DO UPDATE SET cursor = excluded.cursor, polled_at = excluded.polled_at",
            device_id, cursor, polled_at)

    async def known_records(self, log_ids: list[int]) -> dict[int, bool]:
        """log_id -> ended для уже обработанных записей журнала."""
        if not log_ids:
            return {}
        rows = await self.db.fetch(f"SELECT log_id, ended FROM log_records WHERE log_id IN ({_in(log_ids)})",
                                   *log_ids)
        return {r["log_id"]: bool(r["ended"]) for r in rows}

    async def add_record(self, log_id: int, device_id: int, start_dt: int, ended: bool) -> None:
        await self.db.execute(
            "INSERT INTO log_records (log_id, device_id, start_dt, ended) VALUES (?, ?, ?, ?) "
            "ON CONFLICT (log_id) DO UPDATE SET ended = (log_records.ended OR excluded.ended)",
            log_id, device_id, start_dt, ended)

    async def mark_ended(self, log_id: int) -> None:
        await self.db.execute("UPDATE log_records SET ended = ? WHERE log_id = ?", True, log_id)

    async def open_records(self, device_id: int) -> list[int]:
        rows = await self.db.fetch(
            "SELECT log_id FROM log_records WHERE device_id = ? AND ended = ?", device_id, False)
        return [r["log_id"] for r in rows]

    async def cleanup_records(self, older_than: int) -> None:
        await self.db.execute("DELETE FROM log_records WHERE ended = ? AND start_dt < ?", True, older_than)

    # --- очередь уведомлений ---

    async def outbox_add(self, chat_id: int, text: str, markup: str | None) -> None:
        now = int(time.time())
        await self.db.execute(
            "INSERT INTO outbox (chat_id, text, markup, created_at, next_try_at) VALUES (?, ?, ?, ?, ?)",
            chat_id, text, markup, now, now)

    async def outbox_heads(self, now: int) -> list[dict]:
        """Самое старое сообщение каждого чата, если ему пора отправляться (порядок внутри чата сохраняется)."""
        return await self.db.fetch(
            "SELECT o.* FROM outbox o WHERE o.id IN (SELECT min(id) FROM outbox GROUP BY chat_id) "
            "AND o.next_try_at <= ? ORDER BY o.id", now)

    async def outbox_delete(self, message_id: int) -> None:
        await self.db.execute("DELETE FROM outbox WHERE id = ?", message_id)

    async def outbox_drop_chat(self, chat_id: int) -> int:
        count = await self.db.fetchval("SELECT count(*) FROM outbox WHERE chat_id = ?", chat_id)
        await self.db.execute("DELETE FROM outbox WHERE chat_id = ?", chat_id)
        return count or 0

    async def outbox_retry(self, message_id: int, next_try_at: int, error: str) -> None:
        await self.db.execute(
            "UPDATE outbox SET attempts = attempts + 1, next_try_at = ?, last_error = ? WHERE id = ?",
            next_try_at, error[:500], message_id)

    async def outbox_expire(self, older_than: int) -> int:
        count = await self.db.fetchval("SELECT count(*) FROM outbox WHERE created_at < ?", older_than)
        if count:
            await self.db.execute("DELETE FROM outbox WHERE created_at < ?", older_than)
        return count or 0

    async def outbox_size(self) -> int:
        return await self.db.fetchval("SELECT count(*) FROM outbox") or 0
