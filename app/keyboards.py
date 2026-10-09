"""Клавиатуры, общие для бота и поллера."""
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

# Постоянной клавиатуры внизу нет (мешает жесту «назад» на Android) — команды в меню бота.
# NO_KEYBOARD убирает клавиатуру, оставшуюся от прежних версий.
NO_KEYBOARD = ReplyKeyboardRemove()


def read_button(conn_id: int, log_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="✔ Прочитано", callback_data=f"rd:{conn_id}:{log_id}")
    ]])
