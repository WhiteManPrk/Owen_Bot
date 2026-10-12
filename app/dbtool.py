"""Перенос данных между базами, резервные копии и восстановление.

    python -m app.dbtool copy --from postgres --to sqlite [--force]
    python -m app.dbtool backup [--dir /data/backups]
    python -m app.dbtool restore /data/backups/owenbot-....db [--force]
    python -m app.dbtool info

copy/restore делать при остановленном боте. Резервная копия — всегда файл SQLite,
её можно восстановить в любую из баз. Ключи OwenCloud в копии зашифрованы — нужен тот же ENCRYPTION_KEY.
"""
import argparse
import asyncio
import glob
import logging
import os
import sys
from datetime import datetime, timezone

from .config import db_backend, postgres_configured, postgres_dsn, sqlite_path
from .storage.backends import Backend, SqliteBackend, open_backend
from .storage.schema import SERIAL_COLUMNS, TABLES, convert, migrate, table_exists

log = logging.getLogger(__name__)


class DbToolError(Exception):
    pass


async def open_kind(kind: str, path: str | None = None, attempts: int = 5) -> Backend:
    if kind == "sqlite":
        return await open_backend("sqlite", sqlite_path=path or sqlite_path())
    return await open_backend("postgres", dsn=postgres_dsn(), attempts=attempts)


async def row_counts(db: Backend) -> dict[str, int]:
    counts = {}
    for table, _ in TABLES:
        counts[table] = await db.fetchval(f"SELECT count(*) FROM {table}") if await table_exists(db, table) else 0
    return counts


async def copy_data(src: Backend, dst: Backend, force: bool = False) -> dict[str, tuple[int, int]]:
    """Копирует все данные src → dst в одной транзакции. src не изменяется.

    Возвращает {таблица: (строк в источнике, строк в цели)}.
    """
    await migrate(dst)
    existing = await row_counts(dst)
    if any(existing.values()) and not force:
        busy = ", ".join(f"{t}: {n}" for t, n in existing.items() if n)
        raise DbToolError(f"Целевая база ({dst.dialect}) не пустая ({busy}). Используйте --force, чтобы перезаписать.")

    source_counts: dict[str, int] = {}
    async with dst.transaction() as tx:
        for table, _ in reversed(TABLES):
            await tx.execute(f"DELETE FROM {table}")
        for table, columns in TABLES:
            if not await table_exists(src, table):
                source_counts[table] = 0  # в старой схеме таблицы ещё не было
                continue
            rows = await src.fetch(f"SELECT {', '.join(columns)} FROM {table}")
            source_counts[table] = len(rows)
            if rows:
                placeholders = ", ".join("?" for _ in columns)
                await tx.executemany(
                    f"INSERT INTO {table} ({', '.join(columns)}) VALUES ({placeholders})",
                    [tuple(convert(table, c, r[c], dst.dialect) for c in columns) for r in rows])
        if dst.dialect == "postgres":
            for table, column in SERIAL_COLUMNS.items():
                await tx.execute(
                    f"SELECT setval(pg_get_serial_sequence('{table}', '{column}'), "
                    f"COALESCE((SELECT max({column}) FROM {table}), 0) + 1, false)")

    target_counts = await row_counts(dst)
    report = {t: (source_counts[t], target_counts[t]) for t, _ in TABLES}
    mismatched = [t for t, (a, b) in report.items() if a != b]
    if mismatched:
        raise DbToolError(f"Число строк не совпало: {', '.join(mismatched)}")
    return report


async def make_backup(directory: str, keep: int = 0, backend: str | None = None) -> str:
    """Резервная копия текущей базы в файл SQLite. keep > 0 — оставить столько последних копий."""
    backend = backend or db_backend()
    os.makedirs(directory, exist_ok=True)
    name = f"owenbot-{datetime.now(timezone.utc):%Y%m%d-%H%M%S}.db"
    final = os.path.join(directory, name)
    part = final + ".part"
    if os.path.exists(part):
        os.remove(part)
    if backend == "sqlite":
        await SqliteBackend(sqlite_path()).backup_to(part)  # онлайн-копирование, бот может работать
    else:
        src = await open_kind("postgres")
        dst = await SqliteBackend(part).open()
        try:
            await copy_data(src, dst)
        finally:
            await dst.close()
            await src.close()
    os.replace(part, final)
    if keep > 0:
        for old in sorted(glob.glob(os.path.join(directory, "owenbot-*.db")))[:-keep]:
            os.remove(old)
    return final


async def backup_loop(directory: str, every_hours: int, keep: int) -> None:
    """Автоматические резервные копии (BACKUP_EVERY_HOURS > 0)."""
    log.info("Автобэкап: каждые %s ч в %s, хранить %s", every_hours, directory, keep)
    while True:
        await asyncio.sleep(every_hours * 3600)
        try:
            path = await make_backup(directory, keep)
            log.info("Автобэкап: %s", path)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("Автобэкап не удался")


async def migration_hint(db_is_empty: bool) -> None:
    """Выбран SQLite и он пуст, а PostgreSQL доступен и в нём есть данные — подсказать команду переноса."""
    if not db_is_empty or not postgres_configured():
        return
    try:
        pg = await asyncio.wait_for(open_kind("postgres", attempts=1), timeout=5)
    except Exception:
        return
    try:
        if await table_exists(pg, "connections") and await pg.fetchval("SELECT count(*) FROM connections"):
            log.warning("База SQLite пуста, но в PostgreSQL есть данные. Перенести (бот остановлен): "
                        "python -m app.dbtool copy --from postgres --to sqlite")
    finally:
        await pg.close()


def print_report(report: dict[str, tuple[int, int]]) -> None:
    for table, (src, dst) in report.items():
        print(f"  {table:<14} {src:>6} → {dst}")


async def cli(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.dbtool", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p_copy = sub.add_parser("copy", help="перенести данные между базами")
    p_copy.add_argument("--from", dest="src", choices=["sqlite", "postgres"], required=True)
    p_copy.add_argument("--to", dest="dst", choices=["sqlite", "postgres"], required=True)
    p_copy.add_argument("--force", action="store_true", help="перезаписать непустую целевую базу")
    p_backup = sub.add_parser("backup", help="резервная копия текущей базы (файл SQLite)")
    p_backup.add_argument("--dir", default=os.environ.get("BACKUP_DIR") or "/data/backups")
    p_restore = sub.add_parser("restore", help="восстановить копию в текущую базу")
    p_restore.add_argument("file")
    p_restore.add_argument("--force", action="store_true", help="перезаписать непустую базу")
    sub.add_parser("info", help="текущая база и число строк")
    args = parser.parse_args(argv)

    try:
        if args.cmd == "copy":
            if args.src == args.dst:
                raise DbToolError("--from и --to совпадают")
            src, dst = await open_kind(args.src), await open_kind(args.dst)
            try:
                report = await copy_data(src, dst, args.force)
                print(f"Перенос {args.src} → {args.dst}:")
                print_report(report)
            finally:
                await src.close()
                await dst.close()
            print(f"Готово. Чтобы работать на новой базе, укажите DB_BACKEND={args.dst} и запустите бота.")
        elif args.cmd == "backup":
            path = await make_backup(args.dir)
            print(f"Резервная копия: {path}")
            print("Для восстановления на другом сервере нужен тот же ENCRYPTION_KEY из .env.")
        elif args.cmd == "restore":
            if not os.path.isfile(args.file):
                raise DbToolError(f"Файл не найден: {args.file}")
            src, dst = await SqliteBackend(args.file).open(), await open_kind(db_backend())
            try:
                report = await copy_data(src, dst, args.force)
                print(f"Восстановление {args.file} → {dst.dialect}:")
                print_report(report)
            finally:
                await src.close()
                await dst.close()
        elif args.cmd == "info":
            db = await open_kind(db_backend())
            try:
                print(f"База: {db.dialect}" + (f" ({sqlite_path()})" if db.dialect == "sqlite" else ""))
                for table, count in (await row_counts(db)).items():
                    print(f"  {table:<14} {count}")
            finally:
                await db.close()
    except DbToolError as e:
        print(f"Ошибка: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(message)s")
    sys.exit(asyncio.run(cli(sys.argv[1:])))
