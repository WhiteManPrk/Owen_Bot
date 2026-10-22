"""Telegram session: proxy and retries on network errors."""
import asyncio
import logging

from aiogram import Bot
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.session.middlewares.base import BaseRequestMiddleware, NextRequestMiddlewareType
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.methods import AnswerCallbackQuery, GetUpdates, TelegramMethod
from aiogram.methods.base import TelegramType, Response

log = logging.getLogger(__name__)

ATTEMPTS = 3


class RetryOnNetworkError(BaseRequestMiddleware):
    """Retry a request when the connection drops (e.g. reset by the proxy).

    getUpdates is left alone: aiogram retries it itself.
    """

    async def __call__(self, make_request: NextRequestMiddlewareType[TelegramType], bot: Bot,
                       method: TelegramMethod[TelegramType]) -> Response[TelegramType]:
        for attempt in range(1, ATTEMPTS + 1):
            try:
                return await make_request(bot, method)
            except TelegramNetworkError as e:
                if isinstance(method, GetUpdates) or attempt == ATTEMPTS:
                    raise
                log.warning("Telegram %s: %s — повтор %s из %s",
                            type(method).__name__, e, attempt, ATTEMPTS - 1)
                await asyncio.sleep(attempt)
        raise AssertionError("unreachable")


STALE_CALLBACK = ("query is too old", "query id is invalid")


class IgnoreStaleCallback(BaseRequestMiddleware):
    """A button press delivered after an outage can no longer be answered (answerCallbackQuery).

    Treat the answer as done, so the handler still shows its result.
    """

    async def __call__(self, make_request: NextRequestMiddlewareType[TelegramType], bot: Bot,
                       method: TelegramMethod[TelegramType]) -> Response[TelegramType]:
        try:
            return await make_request(bot, method)
        except TelegramBadRequest as e:
            if isinstance(method, AnswerCallbackQuery) and any(s in e.message.lower() for s in STALE_CALLBACK):
                log.info("Устаревшее нажатие кнопки — подтверждение пропущено, обработка продолжается")
                return True  # type: ignore[return-value]
            raise


def create_session(proxy: str | None) -> AiohttpSession:
    session = AiohttpSession(proxy=proxy) if proxy else AiohttpSession()
    session.middleware(RetryOnNetworkError())
    session.middleware(IgnoreStaleCallback())
    return session
