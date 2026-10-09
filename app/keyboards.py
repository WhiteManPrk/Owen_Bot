"""Клавиатуры, общие для бота и поллера."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, KeyboardButton, ReplyKeyboardMarkup

BTN_DEVICES = "📟 Приборы"
BTN_READ_ALL = "✔ Прочитать все"
BTN_ACCOUNTS = "🔑 Аккаунты"

MAIN_MENU = ReplyKeyboardMarkup(
    keyboard=[[KeyboardButton(text=BTN_DEVICES), KeyboardButton(text=BTN_READ_ALL)],
              [KeyboardButton(text=BTN_ACCOUNTS)]],
    resize_keyboard=True,
    is_persistent=True,
)


def read_button(conn_id: int, log_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✔ Прочитано", callback_data=f"rd:{conn_id}:{log_id}")
    ]])
