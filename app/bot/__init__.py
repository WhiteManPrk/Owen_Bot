from aiogram import Router

from . import accounts, control, devices, reading


def build_router() -> Router:
    router = Router()
    router.include_routers(accounts.router, devices.router, control.router, reading.router)
    return router
