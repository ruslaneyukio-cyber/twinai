import asyncio
import logging
import os

from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart, Command
from aiogram.types import Message, InlineKeyboardMarkup, InlineKeyboardButton, WebAppInfo

# Frontend URL of the Mini App (Netlify)
FRONTEND_URL = os.getenv(
    "FRONTEND_URL",
    "https://taskboard-tg-miniapp-ruslan.windsurf.build",
)
# Optional backend API override. Default to stable Render backend.
API_BASE = os.getenv(
    "API_BASE",
    "https://twinai-0yz2.onrender.com",
)


def miniapp_keyboard() -> InlineKeyboardMarkup:
    # Provide Web App button with API param so the web app talks to the right backend
    import time as _time
    webapp_url = f"{FRONTEND_URL}?api={API_BASE}&v={int(_time.time())}"
    kb = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="Open TaskBoard", web_app=WebAppInfo(url=webapp_url)
                )
            ]
        ]
    )
    return kb


async def cmd_start(message: Message):
    kb = miniapp_keyboard()
    await message.answer(
        "TaskBoard Mini App — управление задачами в один клик. Открой мини‑приложение:",
        reply_markup=kb,
    )


async def cmd_menu(message: Message):
    await cmd_start(message)


async def main():
    logging.basicConfig(level=logging.INFO)
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        raise RuntimeError("TELEGRAM_BOT_TOKEN is not set")

    bot = Bot(token=token)
    dp = Dispatcher()

    dp.message.register(cmd_start, CommandStart())
    dp.message.register(cmd_menu, Command(commands=["menu", "app", "open"]))

    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
