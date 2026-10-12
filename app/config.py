import os
from dataclasses import dataclass
from urllib.parse import quote


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def _required(name: str) -> str:
    value = _env(name)
    if not value:
        raise SystemExit(f"Переменная окружения {name} не задана")
    return value


TG_PROXY_SCHEMES = ("socks5", "socks4", "http")              # aiohttp-socks
OWEN_PROXY_SCHEMES = ("socks5", "socks5h", "http", "https")  # httpx + socksio


def check_scheme(name: str, url: str | None, allowed: tuple[str, ...]) -> str | None:
    if url and url.partition("://")[0].lower() not in allowed:
        raise SystemExit(f"{name}: неподдерживаемый тип прокси «{url.partition('://')[0]}». "
                         f"Допустимо: {', '.join(s + '://' for s in allowed)}")
    return url


def proxy_url(url: str | None, login: str | None, password: str | None) -> str | None:
    """Адрес прокси с логином и паролем: ('socks5://host:1080', 'user', 'p@ss') -> 'socks5://user:p%40ss@host:1080'.

    Логин и пароль можно задать и прямо в адресе — тогда отдельные переменные не нужны.
    """
    if not url or not login:
        return url
    scheme, _, rest = url.rpartition("://")
    host = rest.rpartition("@")[2]
    return f"{scheme or 'socks5'}://{quote(login, safe='')}:{quote(password or '', safe='')}@{host}"


DB_BACKENDS = ("sqlite", "postgres")


def postgres_configured() -> bool:
    return bool(_env("DATABASE_URL") or _env("POSTGRES_PASSWORD"))


def postgres_dsn() -> str:
    """Подключение к PostgreSQL из DATABASE_URL или POSTGRES_* (нужно только при работе с PostgreSQL)."""
    return _env("DATABASE_URL") or "postgresql://{u}:{p}@{h}:{port}/{db}".format(
        u=quote(_env("POSTGRES_USER", "owenbot"), safe=""),
        p=quote(_required("POSTGRES_PASSWORD"), safe=""),
        h=_env("POSTGRES_HOST", "db"),
        port=_env("POSTGRES_PORT", "5432"),
        db=_env("POSTGRES_DB", "owenbot"),
    )


def sqlite_path() -> str:
    return _env("SQLITE_PATH", "/data/owenbot.db")


def db_backend() -> str:
    """DB_BACKEND, если задан. Иначе — postgres, когда настроен PostgreSQL (так работали версии 1.0.x
    со старым docker-compose.yml), и sqlite во всех остальных случаях."""
    backend = (_env("DB_BACKEND") or ("postgres" if postgres_configured() else "sqlite")).lower()
    if backend not in DB_BACKENDS:
        raise SystemExit(f"DB_BACKEND: «{backend}» — допустимо sqlite или postgres")
    return backend


def is_volume(path: str) -> bool:
    """Лежит ли каталог на смонтированном томе (а не в слое контейнера, который пропадает при пересоздании)."""
    directory = os.path.abspath(path)
    try:
        with open("/proc/self/mountinfo", encoding="utf-8") as f:
            mounts = {line.split()[4] for line in f if len(line.split()) > 4}
    except OSError:
        return os.path.ismount(directory)
    while directory not in ("/", ""):
        if directory in mounts:
            return True
        directory = os.path.dirname(directory)
    return False


@dataclass(frozen=True)
class Config:
    bot_token: str
    tg_proxy: str | None
    owen_api_url: str
    owen_proxy: str | None
    poll_interval: int
    db_backend: str
    sqlite_path: str
    encryption_key: str
    access_code: str | None
    log_level: str
    log_dir: str | None
    outbox_ttl_hours: int
    backup_every_hours: int
    backup_keep: int
    backup_dir: str

    @property
    def database_url(self) -> str:
        return postgres_dsn()

    @classmethod
    def from_env(cls) -> "Config":
        backend = db_backend()
        if backend == "postgres":
            postgres_dsn()  # проверить POSTGRES_PASSWORD сразу при запуске
        return cls(
            bot_token=_required("BOT_TOKEN"),
            tg_proxy=check_scheme("TG_PROXY", proxy_url(
                _env("TG_PROXY"), _env("TG_PROXY_LOGIN"), _env("TG_PROXY_PASSWORD")), TG_PROXY_SCHEMES),
            owen_api_url=_env("OWEN_API_URL", "https://api.owencloud.ru/v1"),
            owen_proxy=check_scheme("OWEN_PROXY", proxy_url(
                _env("OWEN_PROXY"), _env("OWEN_PROXY_LOGIN"), _env("OWEN_PROXY_PASSWORD")), OWEN_PROXY_SCHEMES),
            poll_interval=int(_env("POLL_INTERVAL", "60")),
            db_backend=backend,
            sqlite_path=sqlite_path(),
            encryption_key=_required("ENCRYPTION_KEY"),
            access_code=_env("ACCESS_CODE"),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            log_dir=_env("LOG_DIR"),
            outbox_ttl_hours=int(_env("OUTBOX_TTL_HOURS", "24")),
            backup_every_hours=int(_env("BACKUP_EVERY_HOURS", "0")),
            backup_keep=int(_env("BACKUP_KEEP", "7")),
            backup_dir=_env("BACKUP_DIR", "/data/backups"),
        )
