import asyncio
import logging

import asyncpg

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS connections (
    id         SERIAL PRIMARY KEY,
    chat_id    BIGINT      NOT NULL,
    token_enc  TEXT        NOT NULL,
    token_hash TEXT        NOT NULL,
    name       TEXT        NOT NULL,
    broken     BOOLEAN     NOT NULL DEFAULT FALSE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (chat_id, token_hash)
);
CREATE TABLE IF NOT EXISTS subscriptions (
    chat_id       BIGINT  NOT NULL,
    device_id     BIGINT  NOT NULL,
    connection_id INTEGER NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
    enabled       BOOLEAN NOT NULL DEFAULT FALSE,
    mode          TEXT    NOT NULL DEFAULT 'critical',
    PRIMARY KEY (chat_id, device_id)
);
CREATE TABLE IF NOT EXISTS sub_events (
    chat_id   BIGINT NOT NULL,
    device_id BIGINT NOT NULL,
    event_id  BIGINT NOT NULL,
    PRIMARY KEY (chat_id, device_id, event_id),
    FOREIGN KEY (chat_id, device_id) REFERENCES subscriptions (chat_id, device_id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS poll_state (
    device_id BIGINT PRIMARY KEY,
    cursor    BIGINT NOT NULL,
    polled_at BIGINT NOT NULL
);
CREATE TABLE IF NOT EXISTS log_records (
    log_id    BIGINT  PRIMARY KEY,
    device_id BIGINT  NOT NULL,
    start_dt  BIGINT  NOT NULL,
    ended     BOOLEAN NOT NULL DEFAULT FALSE
);
CREATE INDEX IF NOT EXISTS log_records_open ON log_records (device_id) WHERE NOT ended;
CREATE TABLE IF NOT EXISTS chat_access (
    chat_id    BIGINT      PRIMARY KEY,
    code_hash  TEXT        NOT NULL,
    granted_at TIMESTAMPTZ NOT NULL DEFAULT now()
);
"""

MODES = ("critical", "all", "custom")


class Database:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.pool: asyncpg.Pool | None = None

    async def open(self, attempts: int = 30) -> None:
        for attempt in range(1, attempts + 1):
            try:
                self.pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
                break
            except (OSError, asyncpg.PostgresError) as e:
                if attempt == attempts:
                    raise
                log.info("БД недоступна (%s), повтор через 2 с", e)
                await asyncio.sleep(2)
        async with self.pool.acquire() as con:
            await con.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()

    # --- аккаунты OwenCloud ---

    async def add_connection(self, chat_id: int, token_enc: str, token_hash: str, name: str) -> int:
        return await self.pool.fetchval(
            "INSERT INTO connections (chat_id, token_enc, token_hash, name) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (chat_id, token_hash) DO UPDATE SET token_enc = excluded.token_enc, "
            "name = excluded.name, broken = FALSE RETURNING id",
            chat_id, token_enc, token_hash, name)

    async def connections(self, chat_id: int) -> list[asyncpg.Record]:
        return await self.pool.fetch("SELECT * FROM connections WHERE chat_id = $1 ORDER BY id", chat_id)

    async def connection(self, conn_id: int, chat_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM connections WHERE id = $1 AND chat_id = $2", conn_id, chat_id)

    async def delete_connection(self, conn_id: int, chat_id: int) -> None:
        await self.pool.execute("DELETE FROM connections WHERE id = $1 AND chat_id = $2", conn_id, chat_id)

    async def set_broken(self, conn_id: int, broken: bool) -> None:
        await self.pool.execute("UPDATE connections SET broken = $2 WHERE id = $1", conn_id, broken)

    # --- подписки ---

    async def subscription(self, chat_id: int, device_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow(
            "SELECT * FROM subscriptions WHERE chat_id = $1 AND device_id = $2", chat_id, device_id)

    async def enabled_devices(self, chat_id: int) -> dict[int, int]:
        """device_id -> connection_id для включённых подписок чата."""
        rows = await self.pool.fetch(
            "SELECT device_id, connection_id FROM subscriptions WHERE chat_id = $1 AND enabled", chat_id)
        return {r["device_id"]: r["connection_id"] for r in rows}

    async def save_subscription(self, chat_id: int, device_id: int, conn_id: int,
                                enabled: bool | None = None, mode: str | None = None) -> None:
        assert mode is None or mode in MODES
        await self.pool.execute(
            "INSERT INTO subscriptions (chat_id, device_id, connection_id, enabled, mode) "
            "VALUES ($1, $2, $3, COALESCE($4, FALSE), COALESCE($5, 'critical')) "
            "ON CONFLICT (chat_id, device_id) DO UPDATE SET connection_id = excluded.connection_id, "
            "enabled = COALESCE($4, subscriptions.enabled), mode = COALESCE($5, subscriptions.mode)",
            chat_id, device_id, conn_id, enabled, mode)

    async def selected_events(self, chat_id: int, device_id: int) -> set[int]:
        rows = await self.pool.fetch(
            "SELECT event_id FROM sub_events WHERE chat_id = $1 AND device_id = $2", chat_id, device_id)
        return {r["event_id"] for r in rows}

    async def set_selected_events(self, chat_id: int, device_id: int, event_ids: set[int]) -> None:
        async with self.pool.acquire() as con, con.transaction():
            await con.execute("DELETE FROM sub_events WHERE chat_id = $1 AND device_id = $2", chat_id, device_id)
            await con.executemany(
                "INSERT INTO sub_events (chat_id, device_id, event_id) VALUES ($1, $2, $3)",
                [(chat_id, device_id, e) for e in event_ids])

    async def active_subscriptions(self, access_hash: str | None = None) -> list[asyncpg.Record]:
        """Включённые подписки с рабочими ключами + выбранные события (для режима custom).

        access_hash — если бот закрыт кодом, только чаты, введшие текущий код.
        """
        return await self.pool.fetch(
            "SELECT s.chat_id, s.device_id, s.mode, c.id AS connection_id, c.token_enc, c.name AS company, "
            "  (SELECT count(*) FROM connections c2 WHERE c2.chat_id = s.chat_id) AS chat_connections, "
            "  COALESCE((SELECT array_agg(e.event_id) FROM sub_events e "
            "            WHERE e.chat_id = s.chat_id AND e.device_id = s.device_id), '{}') AS event_ids "
            "FROM subscriptions s JOIN connections c ON c.id = s.connection_id "
            "WHERE s.enabled AND NOT c.broken AND ($1::text IS NULL OR EXISTS "
            "  (SELECT 1 FROM chat_access a WHERE a.chat_id = s.chat_id AND a.code_hash = $1)) "
            "ORDER BY s.device_id, c.id", access_hash)

    # --- доступ по коду ---

    async def has_access(self, chat_id: int, code_hash: str) -> bool:
        return await self.pool.fetchval(
            "SELECT EXISTS (SELECT 1 FROM chat_access WHERE chat_id = $1 AND code_hash = $2)", chat_id, code_hash)

    async def grant_access(self, chat_id: int, code_hash: str) -> None:
        await self.pool.execute(
            "INSERT INTO chat_access (chat_id, code_hash) VALUES ($1, $2) "
            "ON CONFLICT (chat_id) DO UPDATE SET code_hash = excluded.code_hash, granted_at = now()",
            chat_id, code_hash)

    async def disable_chat(self, chat_id: int) -> None:
        await self.pool.execute("UPDATE subscriptions SET enabled = FALSE WHERE chat_id = $1", chat_id)

    # --- состояние поллера ---

    async def poll_state(self, device_id: int) -> asyncpg.Record | None:
        return await self.pool.fetchrow("SELECT cursor, polled_at FROM poll_state WHERE device_id = $1", device_id)

    async def set_cursor(self, device_id: int, cursor: int, polled_at: int) -> None:
        await self.pool.execute(
            "INSERT INTO poll_state (device_id, cursor, polled_at) VALUES ($1, $2, $3) "
            "ON CONFLICT (device_id) DO UPDATE SET cursor = excluded.cursor, polled_at = excluded.polled_at",
            device_id, cursor, polled_at)

    async def known_records(self, log_ids: list[int]) -> dict[int, bool]:
        """log_id -> ended для уже обработанных записей журнала."""
        rows = await self.pool.fetch(
            "SELECT log_id, ended FROM log_records WHERE log_id = ANY($1::bigint[])", log_ids)
        return {r["log_id"]: r["ended"] for r in rows}

    async def add_record(self, log_id: int, device_id: int, start_dt: int, ended: bool) -> None:
        await self.pool.execute(
            "INSERT INTO log_records (log_id, device_id, start_dt, ended) VALUES ($1, $2, $3, $4) "
            "ON CONFLICT (log_id) DO UPDATE SET ended = log_records.ended OR excluded.ended",
            log_id, device_id, start_dt, ended)

    async def mark_ended(self, log_id: int) -> None:
        await self.pool.execute("UPDATE log_records SET ended = TRUE WHERE log_id = $1", log_id)

    async def open_records(self, device_id: int) -> list[int]:
        rows = await self.pool.fetch(
            "SELECT log_id FROM log_records WHERE device_id = $1 AND NOT ended", device_id)
        return [r["log_id"] for r in rows]

    async def cleanup_records(self, older_than: int) -> None:
        await self.pool.execute("DELETE FROM log_records WHERE ended AND start_dt < $1", older_than)
