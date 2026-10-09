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


@dataclass(frozen=True)
class Config:
    bot_token: str
    tg_proxy: str | None
    owen_api_url: str
    owen_proxy: str | None
    poll_interval: int
    database_url: str
    encryption_key: str
    access_code: str | None
    log_level: str
    log_dir: str | None

    @classmethod
    def from_env(cls) -> "Config":
        database_url = _env("DATABASE_URL") or "postgresql://{u}:{p}@{h}:{port}/{db}".format(
            u=_env("POSTGRES_USER", "owenbot"),
            p=_required("POSTGRES_PASSWORD"),
            h=_env("POSTGRES_HOST", "db"),
            port=_env("POSTGRES_PORT", "5432"),
            db=_env("POSTGRES_DB", "owenbot"),
        )
        return cls(
            bot_token=_required("BOT_TOKEN"),
            tg_proxy=check_scheme("TG_PROXY", proxy_url(
                _env("TG_PROXY"), _env("TG_PROXY_LOGIN"), _env("TG_PROXY_PASSWORD")), TG_PROXY_SCHEMES),
            owen_api_url=_env("OWEN_API_URL", "https://api.owencloud.ru/v1"),
            owen_proxy=check_scheme("OWEN_PROXY", proxy_url(
                _env("OWEN_PROXY"), _env("OWEN_PROXY_LOGIN"), _env("OWEN_PROXY_PASSWORD")), OWEN_PROXY_SCHEMES),
            poll_interval=int(_env("POLL_INTERVAL", "60")),
            database_url=database_url,
            encryption_key=_required("ENCRYPTION_KEY"),
            access_code=_env("ACCESS_CODE"),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            log_dir=_env("LOG_DIR"),
        )
