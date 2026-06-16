import asyncio

from app.config import bot, dp
from app.support import init_db, set_bot_commands
from app.yookassa_payments import start_yookassa_webhook_server
import app.handlers  # noqa: F401  # регистрация хендлеров


async def main():
    await init_db()
    await set_bot_commands()
    webhook_runner = await start_yookassa_webhook_server()
    try:
        await dp.start_polling(bot)
    finally:
        await webhook_runner.cleanup()


if __name__ == "__main__":
    asyncio.run(main())
