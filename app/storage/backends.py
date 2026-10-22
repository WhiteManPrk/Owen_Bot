"""Database adapters with one interface for SQLite and PostgreSQL.

Queries use '?' placeholders; the PostgreSQL adapter turns them into $1, $2, ...
Result rows are plain dicts.
"""
import asyncio
import contextlib
import logging
import os
import re
from typing import Any, AsyncIterator, Protocol

log = logging.getLogger(__name__)


class Executor(Protocol):
    dialect: str

    async def fetch(self, sql: str, *args) -> list[dict]: ...
    async def fetchrow(self, sql: str, *args) -> dict | None: ...
    async def fetchval(self, sql: str, *args) -> Any: ...
    async def execute(self, sql: str, *args) -> None: ...
    async def executemany(self, sql: str, rows: list[tuple]) -> None: ...


class Backend(Executor, Protocol):
    def transaction(self) -> contextlib.AbstractAsyncContextManager[Executor]: ...
    async def close(self) -> None: ...


# --- SQLite ---

class _SqliteExec:
    dialect = "sqlite"

    def __init__(self, con):
        self._con = con

    async def fetch(self, sql, *args):
        async with self._con.execute(sql, args) as cur:
            cols = [c[0] for c in cur.description]
            return [dict(zip(cols, row)) for row in await cur.fetchall()]

    async def fetchrow(self, sql, *args):
        rows = await self.fetch(sql, *args)
        return rows[0] if rows else None

    async def fetchval(self, sql, *args):
        async with self._con.execute(sql, args) as cur:
            row = await cur.fetchone()
            return row[0] if row else None

    async def execute(self, sql, *args):
        await self._con.execute(sql, args)

    async def executemany(self, sql, rows):
        await self._con.executemany(sql, rows)


class SqliteBackend:
    """One autocommit aiosqlite connection. A lock serializes operations and a transaction
    holds it for its whole duration, so other queries cannot interleave.
    """
    dialect = "sqlite"

    def __init__(self, path: str):
        self.path = path
        self._con = None
        self._lock = asyncio.Lock()

    async def open(self) -> "SqliteBackend":
        import aiosqlite
        if os.path.dirname(self.path):
            os.makedirs(os.path.dirname(self.path), exist_ok=True)
        self._con = await aiosqlite.connect(self.path, isolation_level=None)
        os.chmod(self.path, 0o600)  # holds encrypted keys and chat data; -wal/-shm inherit these permissions
        await self._con.execute("PRAGMA journal_mode = WAL")
        await self._con.execute("PRAGMA foreign_keys = ON")
        await self._con.execute("PRAGMA busy_timeout = 5000")
        return self

    async def _run(self, method: str, *args):
        async with self._lock:
            return await getattr(_SqliteExec(self._con), method)(*args)

    async def fetch(self, sql, *args):
        return await self._run("fetch", sql, *args)

    async def fetchrow(self, sql, *args):
        return await self._run("fetchrow", sql, *args)

    async def fetchval(self, sql, *args):
        return await self._run("fetchval", sql, *args)

    async def execute(self, sql, *args):
        await self._run("execute", sql, *args)

    async def executemany(self, sql, rows):
        await self._run("executemany", sql, rows)

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[_SqliteExec]:
        async with self._lock:
            await self._con.execute("BEGIN IMMEDIATE")
            try:
                yield _SqliteExec(self._con)
            except BaseException:
                await self._con.execute("ROLLBACK")
                raise
            await self._con.execute("COMMIT")

    async def backup_to(self, target_path: str) -> None:
        """SQLite online backup: a consistent copy while the bot keeps running."""
        import sqlite3

        def copy() -> None:
            src = sqlite3.connect(self.path)
            dst = sqlite3.connect(target_path)
            try:
                src.backup(dst)
            finally:
                dst.close()
                src.close()
        await asyncio.to_thread(copy)

    async def close(self) -> None:
        if self._con:
            await self._con.close()


# --- PostgreSQL ---

_PLACEHOLDER = re.compile(r"\?")


def to_pg(sql: str) -> str:
    counter = iter(range(1, 10_000))
    return _PLACEHOLDER.sub(lambda _: f"${next(counter)}", sql)


class _PgExec:
    dialect = "postgres"

    def __init__(self, target):
        self._t = target  # asyncpg pool or connection

    async def fetch(self, sql, *args):
        return [dict(r) for r in await self._t.fetch(to_pg(sql), *args)]

    async def fetchrow(self, sql, *args):
        row = await self._t.fetchrow(to_pg(sql), *args)
        return dict(row) if row else None

    async def fetchval(self, sql, *args):
        return await self._t.fetchval(to_pg(sql), *args)

    async def execute(self, sql, *args):
        await self._t.execute(to_pg(sql), *args)

    async def executemany(self, sql, rows):
        await self._t.executemany(to_pg(sql), rows)


class PostgresBackend(_PgExec):
    def __init__(self, dsn: str):
        self.dsn = dsn
        self._pool = None
        super().__init__(None)

    async def open(self, attempts: int = 30) -> "PostgresBackend":
        import asyncpg
        for attempt in range(1, attempts + 1):
            try:
                self._pool = await asyncpg.create_pool(self.dsn, min_size=1, max_size=5)
                break
            except (OSError, asyncpg.PostgresError) as e:
                if attempt == attempts:
                    raise
                log.info("PostgreSQL недоступен (%s), повтор через 2 с", e)
                await asyncio.sleep(2)
        self._t = self._pool
        return self

    @contextlib.asynccontextmanager
    async def transaction(self) -> AsyncIterator[_PgExec]:
        async with self._pool.acquire() as con, con.transaction():
            yield _PgExec(con)

    async def close(self) -> None:
        if self._pool:
            await self._pool.close()


async def open_backend(kind: str, *, sqlite_path: str | None = None, dsn: str | None = None,
                       attempts: int = 30) -> Backend:
    if kind == "sqlite":
        return await SqliteBackend(sqlite_path).open()
    if kind == "postgres":
        return await PostgresBackend(dsn).open(attempts)
    raise ValueError(f"Неизвестная база: {kind}")
