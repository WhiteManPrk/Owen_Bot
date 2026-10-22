"""DB schema and migrations for SQLite and PostgreSQL.

Each migration is a numbered step with DDL for both databases; missing steps run at startup.
Add new migrations to the end of MIGRATIONS and new tables/columns to TABLES.
"""
import logging
from datetime import datetime, timezone

from .backends import Backend, Executor

log = logging.getLogger(__name__)

MIGRATIONS: dict[int, dict[str, list[str]]] = {
    # v1: initial schema (same as PostgreSQL in 1.0.x)
    1: {
        "postgres": [
            """CREATE TABLE IF NOT EXISTS connections (
                id SERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, token_enc TEXT NOT NULL,
                token_hash TEXT NOT NULL, name TEXT NOT NULL, broken BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMPTZ NOT NULL DEFAULT now(), UNIQUE (chat_id, token_hash))""",
            """CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id BIGINT NOT NULL, device_id BIGINT NOT NULL,
                connection_id INTEGER NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
                enabled BOOLEAN NOT NULL DEFAULT FALSE, mode TEXT NOT NULL DEFAULT 'critical',
                PRIMARY KEY (chat_id, device_id))""",
            """CREATE TABLE IF NOT EXISTS sub_events (
                chat_id BIGINT NOT NULL, device_id BIGINT NOT NULL, event_id BIGINT NOT NULL,
                PRIMARY KEY (chat_id, device_id, event_id),
                FOREIGN KEY (chat_id, device_id) REFERENCES subscriptions (chat_id, device_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS poll_state (
                device_id BIGINT PRIMARY KEY, cursor BIGINT NOT NULL, polled_at BIGINT NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS log_records (
                log_id BIGINT PRIMARY KEY, device_id BIGINT NOT NULL, start_dt BIGINT NOT NULL,
                ended BOOLEAN NOT NULL DEFAULT FALSE)""",
            "CREATE INDEX IF NOT EXISTS log_records_open ON log_records (device_id) WHERE NOT ended",
            """CREATE TABLE IF NOT EXISTS chat_access (
                chat_id BIGINT PRIMARY KEY, code_hash TEXT NOT NULL,
                granted_at TIMESTAMPTZ NOT NULL DEFAULT now())""",
        ],
        "sqlite": [
            """CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, token_enc TEXT NOT NULL,
                token_hash TEXT NOT NULL, name TEXT NOT NULL, broken INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP, UNIQUE (chat_id, token_hash))""",
            """CREATE TABLE IF NOT EXISTS subscriptions (
                chat_id INTEGER NOT NULL, device_id INTEGER NOT NULL,
                connection_id INTEGER NOT NULL REFERENCES connections (id) ON DELETE CASCADE,
                enabled INTEGER NOT NULL DEFAULT 0, mode TEXT NOT NULL DEFAULT 'critical',
                PRIMARY KEY (chat_id, device_id))""",
            """CREATE TABLE IF NOT EXISTS sub_events (
                chat_id INTEGER NOT NULL, device_id INTEGER NOT NULL, event_id INTEGER NOT NULL,
                PRIMARY KEY (chat_id, device_id, event_id),
                FOREIGN KEY (chat_id, device_id) REFERENCES subscriptions (chat_id, device_id) ON DELETE CASCADE)""",
            """CREATE TABLE IF NOT EXISTS poll_state (
                device_id INTEGER PRIMARY KEY, cursor INTEGER NOT NULL, polled_at INTEGER NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS log_records (
                log_id INTEGER PRIMARY KEY, device_id INTEGER NOT NULL, start_dt INTEGER NOT NULL,
                ended INTEGER NOT NULL DEFAULT 0)""",
            "CREATE INDEX IF NOT EXISTS log_records_open ON log_records (device_id) WHERE NOT ended",
            """CREATE TABLE IF NOT EXISTS chat_access (
                chat_id INTEGER PRIMARY KEY, code_hash TEXT NOT NULL,
                granted_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP)""",
        ],
    },
    # v2: notification outbox
    2: {
        "postgres": [
            """CREATE TABLE IF NOT EXISTS outbox (
                id BIGSERIAL PRIMARY KEY, chat_id BIGINT NOT NULL, text TEXT NOT NULL, markup TEXT,
                created_at BIGINT NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_try_at BIGINT NOT NULL, last_error TEXT)""",
            "CREATE INDEX IF NOT EXISTS outbox_chat ON outbox (chat_id, id)",
        ],
        "sqlite": [
            """CREATE TABLE IF NOT EXISTS outbox (
                id INTEGER PRIMARY KEY AUTOINCREMENT, chat_id INTEGER NOT NULL, text TEXT NOT NULL, markup TEXT,
                created_at INTEGER NOT NULL, attempts INTEGER NOT NULL DEFAULT 0,
                next_try_at INTEGER NOT NULL, last_error TEXT)""",
            "CREATE INDEX IF NOT EXISTS outbox_chat ON outbox (chat_id, id)",
        ],
    },
    # v3: message <-> journal record link (one read button per event)
    3: {
        "postgres": [
            "ALTER TABLE outbox ADD COLUMN IF NOT EXISTS log_id BIGINT",
            "ALTER TABLE outbox ADD COLUMN IF NOT EXISTS device_id BIGINT",
            "ALTER TABLE outbox ADD COLUMN IF NOT EXISTS connection_id INTEGER",
            "ALTER TABLE outbox ADD COLUMN IF NOT EXISTS reply INTEGER NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS event_messages (
                chat_id BIGINT NOT NULL, log_id BIGINT NOT NULL, message_id BIGINT NOT NULL,
                device_id BIGINT NOT NULL, connection_id INTEGER NOT NULL, text TEXT NOT NULL,
                has_button INTEGER NOT NULL DEFAULT 0, created_at BIGINT NOT NULL,
                PRIMARY KEY (chat_id, log_id))""",
            "CREATE INDEX IF NOT EXISTS event_messages_buttons ON event_messages (device_id) WHERE has_button = 1",
        ],
        "sqlite": [
            "ALTER TABLE outbox ADD COLUMN log_id INTEGER",
            "ALTER TABLE outbox ADD COLUMN device_id INTEGER",
            "ALTER TABLE outbox ADD COLUMN connection_id INTEGER",
            "ALTER TABLE outbox ADD COLUMN reply INTEGER NOT NULL DEFAULT 0",
            """CREATE TABLE IF NOT EXISTS event_messages (
                chat_id INTEGER NOT NULL, log_id INTEGER NOT NULL, message_id INTEGER NOT NULL,
                device_id INTEGER NOT NULL, connection_id INTEGER NOT NULL, text TEXT NOT NULL,
                has_button INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL,
                PRIMARY KEY (chat_id, log_id))""",
            "CREATE INDEX IF NOT EXISTS event_messages_buttons ON event_messages (device_id) WHERE has_button = 1",
        ],
    },
}

LATEST = max(MIGRATIONS)

# Tables in dependency order (parents first), for copy and backup.
TABLES: list[tuple[str, list[str]]] = [
    ("connections", ["id", "chat_id", "token_enc", "token_hash", "name", "broken", "created_at"]),
    ("subscriptions", ["chat_id", "device_id", "connection_id", "enabled", "mode"]),
    ("sub_events", ["chat_id", "device_id", "event_id"]),
    ("chat_access", ["chat_id", "code_hash", "granted_at"]),
    ("poll_state", ["device_id", "cursor", "polled_at"]),
    ("log_records", ["log_id", "device_id", "start_dt", "ended"]),
    ("outbox", ["id", "chat_id", "text", "markup", "created_at", "attempts", "next_try_at", "last_error",
                "log_id", "device_id", "connection_id", "reply"]),
    ("event_messages", ["chat_id", "log_id", "message_id", "device_id", "connection_id", "text",
                        "has_button", "created_at"]),
]
BOOL_COLUMNS = {"broken", "enabled", "ended"}
TIMESTAMP_COLUMNS = {("connections", "created_at"), ("chat_access", "granted_at")}
SERIAL_COLUMNS = {"connections": "id", "outbox": "id"}


def convert(table: str, column: str, value, target: str):
    """Convert a value to the target DB type (booleans and timestamps are stored differently)."""
    if value is None:
        return None
    if column in BOOL_COLUMNS:
        return bool(value) if target == "postgres" else int(bool(value))
    if (table, column) in TIMESTAMP_COLUMNS:
        if target == "postgres":
            if isinstance(value, datetime):
                return value
            dt = datetime.fromisoformat(str(value))  # SQLite stores 'YYYY-MM-DD HH:MM:SS' in UTC
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        if isinstance(value, datetime):
            return value.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")
    return value


async def table_exists(db: Executor, name: str) -> bool:
    if db.dialect == "sqlite":
        return bool(await db.fetchval("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", name))
    return bool(await db.fetchval("SELECT to_regclass(?) IS NOT NULL", name))


async def table_columns(db: Executor, name: str) -> set[str]:
    if db.dialect == "sqlite":
        return {r["name"] for r in await db.fetch(f"PRAGMA table_info({name})")}
    return {r["column_name"] for r in await db.fetch(
        "SELECT column_name FROM information_schema.columns WHERE table_name = ? AND table_schema = current_schema()",
        name)}


async def current_version(db: Backend) -> int:
    await db.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
    version = await db.fetchval("SELECT max(version) FROM schema_version")
    if version is None and await table_exists(db, "connections"):
        # created by 1.0.x, before schema_version existed
        await db.execute("INSERT INTO schema_version (version) VALUES (1)")
        version = 1
    return version or 0


async def migrate(db: Backend) -> int:
    """Apply missing migrations and return the resulting schema version."""
    version = await current_version(db)
    for number in sorted(n for n in MIGRATIONS if n > version):
        async with db.transaction() as tx:
            for statement in MIGRATIONS[number][db.dialect]:
                await tx.execute(statement)
            await tx.execute("INSERT INTO schema_version (version) VALUES (?)", number)
        log.info("Схема БД (%s): применена миграция %s", db.dialect, number)
        version = number
    return version
