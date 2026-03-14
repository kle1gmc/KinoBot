import asyncio

from app.config import bot, dp
from app.support import init_db, set_bot_commands
import app.handlers  # noqa: F401  # регистрация хендлеров


async def main():
    await init_db()
    await set_bot_commands()
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
