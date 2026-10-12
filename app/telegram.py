"""Сессия Telegram: прокси и повтор запросов при сетевых сбоях."""
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
    """Повторяет запрос, если соединение оборвалось (например, его сбросил прокси).

    getUpdates не трогаем — aiogram повторяет его сам.
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
    """Нажатие кнопки, дошедшее после сбоя связи, Telegram уже не даёт «подтвердить» (answerCallbackQuery).

    Это не повод прерывать обработчик: считаем подтверждение выполненным, результат всё равно покажется.
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
