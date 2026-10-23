from aiogram import Router
from aiogram.types import CallbackQuery

from . import accounts, control, devices, reading

fallback = Router(name="fallback")


@fallback.callback_query()
async def stale_button(cb: CallbackQuery) -> None:
    await cb.answer("Кнопка устарела, откройте меню заново", show_alert=True)


def build_router() -> Router:
    router = Router()
    router.include_routers(accounts.router, devices.router, control.router, reading.router, fallback)
    return router
