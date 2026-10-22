import glob

import pytest

from app import dbtool
from app.db import Database
from app.dbtool import DbToolError, copy_data, make_backup, row_counts
from app.storage.backends import SqliteBackend
from app.storage.schema import LATEST, current_version, migrate

from .conftest import fresh_sqlite


async def fill(backend):
    """Fill every table."""
    db = Database(backend)
    await db.open()
    conn_id = await db.add_connection(111, "enc-token", "hash1", "Компания РИМ")
    await db.add_connection(222, "enc-2", "hash2", "Другая")
    await db.save_subscription(111, 167962, conn_id, enabled=True, mode="custom")
    await db.set_selected_events(111, 167962, {513927, 513882})
    await db.set_broken(conn_id, True)
    await db.grant_access(111, "code-hash")
    await db.set_cursor(167962, 1791539251, 1791539300)
    await db.add_record(1827786656, 167962, 1791539251, False)
    await db.add_record(1827756613, 167962, 1791536715, True)
    await db.outbox_add(111, "🔴 авария", '{"inline_keyboard": []}')
    return db


async def snapshot(backend):
    db = Database(backend)
    conns = [(c["id"], c["chat_id"], c["token_enc"], c["name"], bool(c["broken"])) for c in await db.connections(111)]
    sub = await db.subscription(111, 167962)
    return {
        "conns": conns,
        "sub": (sub["connection_id"], bool(sub["enabled"]), sub["mode"]),
        "events": await db.selected_events(111, 167962),
        "access": await db.has_access(111, "code-hash"),
        "cursor": dict(await db.poll_state(167962)),
        "open": await db.open_records(167962),
        "outbox": [(m["chat_id"], m["text"]) for m in await db.db.fetch("SELECT * FROM outbox")],
    }


@pytest.mark.parametrize("direction", ["sqlite->postgres", "postgres->sqlite"])
async def test_copy_both_directions(direction, sqlite_backend, pg_backend):
    src, dst = (sqlite_backend, pg_backend) if direction == "sqlite->postgres" else (pg_backend, sqlite_backend)
    await fill(src)
    report = await copy_data(src, dst)
    assert report["connections"] == (2, 2) and report["sub_events"] == (2, 2) and report["outbox"] == (1, 1)
    assert await snapshot(dst) == await snapshot(src)
    # ids continue after the copy (sequences advanced)
    new_id = await Database(dst).add_connection(333, "e", "h3", "Новая")
    assert new_id == 3


async def test_copy_refuses_non_empty_target(sqlite_backend, pg_backend):
    await fill(sqlite_backend)
    await fill(pg_backend)
    with pytest.raises(DbToolError, match="не пустая"):
        await copy_data(sqlite_backend, pg_backend)
    await copy_data(sqlite_backend, pg_backend, force=True)
    assert (await row_counts(pg_backend))["connections"] == 2


async def test_copy_from_old_postgres_schema(pg_backend, sqlite_backend):
    """A 1.0.x database (v1, no schema_version, no outbox) is detected and copied; the source is untouched."""
    from app.storage.schema import MIGRATIONS
    for statement in MIGRATIONS[1]["postgres"]:
        await pg_backend.execute(statement)
    await pg_backend.execute("INSERT INTO connections (chat_id, token_enc, token_hash, name) VALUES (1, 'e', 'h', 'К')")
    report = await copy_data(pg_backend, sqlite_backend)
    assert report["connections"] == (1, 1) and report["outbox"] == (0, 0)
    assert not await pg_backend.fetchval("SELECT to_regclass('outbox') IS NOT NULL")
    assert await current_version(pg_backend) == 1  # detected as v1
    assert await migrate(pg_backend) == LATEST


async def test_backup_and_restore_sqlite(tmp_path, monkeypatch):
    live = tmp_path / "live.db"
    monkeypatch.setenv("SQLITE_PATH", str(live))
    backend = await fresh_sqlite(live)
    await fill(backend)
    path = await make_backup(str(tmp_path / "backups"), backend="sqlite")
    src = await SqliteBackend(path).open()
    target = await fresh_sqlite(tmp_path / "restored.db")
    await copy_data(src, target)
    assert await snapshot(target) == await snapshot(backend)
    for b in (backend, src, target):
        await b.close()


async def test_backup_from_postgres_and_keep(tmp_path, pg_backend, monkeypatch):
    await fill(pg_backend)
    monkeypatch.setattr(dbtool, "open_kind", lambda kind, **_: _same(pg_backend))
    for _ in range(3):
        await make_backup(str(tmp_path), keep=2, backend="postgres")
        await _tick()
    files = sorted(glob.glob(str(tmp_path / "owenbot-*.db")))
    assert len(files) == 2
    copy = await SqliteBackend(files[-1]).open()
    assert await snapshot(copy) == await snapshot(pg_backend)
    await copy.close()


async def test_migrations_idempotent(sqlite_backend):
    assert await migrate(sqlite_backend) == LATEST
    assert await migrate(sqlite_backend) == LATEST
    assert await current_version(sqlite_backend) == LATEST


class _NoClose:
    def __init__(self, backend):
        self._b = backend

    def __getattr__(self, name):
        return getattr(self._b, name)

    async def close(self):
        pass


async def _same(backend):
    return _NoClose(backend)


async def _tick():
    import asyncio
    await asyncio.sleep(1.1)  # backup names have one-second resolution
