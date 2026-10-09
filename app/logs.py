"""Логирование: консоль (docker logs) + файл owenbot.log в LOG_DIR."""
import logging
import os
from logging.handlers import RotatingFileHandler

FORMAT = "%(asctime)s %(levelname)s %(name)s: %(message)s"


def setup_logging(level: str, log_dir: str | None) -> None:
    logging.basicConfig(level=level, format=FORMAT)
    logging.getLogger("httpx").setLevel(logging.WARNING)
    if not log_dir:
        return
    try:
        os.makedirs(log_dir, exist_ok=True)
        # стандартная ротация Python: 5 файлов по 10 МБ
        handler = RotatingFileHandler(os.path.join(log_dir, "owenbot.log"),
                                      maxBytes=10 * 1024 * 1024, backupCount=5, encoding="utf-8")
    except OSError as e:
        # например, каталог смонтирован только на чтение — работаем дальше, лог остаётся в docker logs
        logging.warning("Не удалось писать лог в %s (%s) — пишу только в консоль контейнера", log_dir, e)
        return
    handler.setFormatter(logging.Formatter(FORMAT))
    logging.getLogger().addHandler(handler)
