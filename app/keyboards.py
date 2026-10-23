from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardRemove

# No persistent reply keyboard: it breaks the Android back gesture.
# NO_KEYBOARD removes a keyboard left by older versions.
NO_KEYBOARD = ReplyKeyboardRemove()


def read_button(conn_id: int, log_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="Отметить прочитанным", callback_data=f"rd:{conn_id}:{log_id}")
    ]])
