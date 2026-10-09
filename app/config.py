import os
from dataclasses import dataclass


def _env(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or default


def _required(name: str) -> str:
    value = _env(name)
    if not value:
        raise SystemExit(f"Переменная окружения {name} не задана")
    return value


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
            tg_proxy=_env("TG_PROXY"),
            owen_api_url=_env("OWEN_API_URL", "https://api.owencloud.ru/v1"),
            owen_proxy=_env("OWEN_PROXY"),
            poll_interval=int(_env("POLL_INTERVAL", "60")),
            database_url=database_url,
            encryption_key=_required("ENCRYPTION_KEY"),
            access_code=_env("ACCESS_CODE"),
            log_level=_env("LOG_LEVEL", "INFO").upper(),
            log_dir=_env("LOG_DIR"),
        )
