import uuid

import aiohttp
from aiohttp import web
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from .config import (
    bot,
    YOOKASSA_RETURN_URL,
    YOOKASSA_SECRET_KEY,
    YOOKASSA_SHOP_ID,
    YOOKASSA_WEBHOOK_HOST,
    YOOKASSA_WEBHOOK_PORT,
)
from .support import activate_subscription


YOOKASSA_API_URL = "https://api.yookassa.ru/v3"
ADMIN_SUPPORT_URL = "https://t.me/donk1337228"

SUBSCRIPTION_TARIFFS = {
    "month": {
        "title": "1 месяц",
        "days": 30,
        "price": "75.00",
        "button": "💳 1 месяц - 75 ₽",
    },
    "quarter": {
        "title": "3 месяца",
        "days": 90,
        "price": "190.00",
        "button": "💳 3 месяца - 190 ₽",
    },
    "year": {
        "title": "12 месяцев",
        "days": 365,
        "price": "630.00",
        "button": "💳 12 месяцев - 630 ₽",
    },
}

_processed_payment_ids: set[str] = set()


def yookassa_is_configured() -> bool:
    return bool(YOOKASSA_SHOP_ID and YOOKASSA_SECRET_KEY and YOOKASSA_RETURN_URL)


def kb_subscription_tariffs() -> InlineKeyboardMarkup:
    keyboard = [
        [InlineKeyboardButton(text=tariff["button"], callback_data=f"pay_subscription_{tariff_id}")]
        for tariff_id, tariff in SUBSCRIPTION_TARIFFS.items()
    ]
    keyboard.append([InlineKeyboardButton(text="👨‍💼 Обратиться к администрации", url=ADMIN_SUPPORT_URL)])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="subscription_management")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def create_subscription_payment(tg_id: int, tariff_id: str) -> dict:
    if not yookassa_is_configured():
        raise RuntimeError("YooKassa is not configured")

    tariff = SUBSCRIPTION_TARIFFS.get(tariff_id)
    if not tariff:
        raise ValueError("Unknown subscription tariff")

    payload = {
        "amount": {
            "value": tariff["price"],
            "currency": "RUB",
        },
        "capture": True,
        "confirmation": {
            "type": "redirect",
            "return_url": YOOKASSA_RETURN_URL,
        },
        "description": f"Подписка MovieSurf: {tariff['title']}",
        "metadata": {
            "tg_id": str(tg_id),
            "tariff_id": tariff_id,
            "days": str(tariff["days"]),
        },
    }

    headers = {"Idempotence-Key": str(uuid.uuid4())}
    async with aiohttp.ClientSession(auth=aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)) as session:
        async with session.post(f"{YOOKASSA_API_URL}/payments", json=payload, headers=headers) as response:
            data = await response.json()
            if response.status >= 400:
                raise RuntimeError(data)
            return data


async def get_payment(payment_id: str) -> dict:
    async with aiohttp.ClientSession(auth=aiohttp.BasicAuth(YOOKASSA_SHOP_ID, YOOKASSA_SECRET_KEY)) as session:
        async with session.get(f"{YOOKASSA_API_URL}/payments/{payment_id}") as response:
            data = await response.json()
            if response.status >= 400:
                raise RuntimeError(data)
            return data


async def yookassa_webhook(request: web.Request) -> web.Response:
    try:
        event = await request.json()
    except Exception:
        return web.json_response({"ok": False, "error": "invalid json"}, status=400)

    if event.get("event") != "payment.succeeded":
        return web.json_response({"ok": True})

    payment_object = event.get("object") or {}
    payment_id = payment_object.get("id")
    if not payment_id:
        return web.json_response({"ok": False, "error": "missing payment id"}, status=400)

    if payment_id in _processed_payment_ids:
        return web.json_response({"ok": True})

    try:
        payment = await get_payment(payment_id)
    except Exception as exc:
        print(f"YooKassa payment check failed: {exc}")
        return web.json_response({"ok": False}, status=500)

    if payment.get("status") != "succeeded" or not payment.get("paid"):
        return web.json_response({"ok": True})

    metadata = payment.get("metadata") or {}
    try:
        tg_id = int(metadata["tg_id"])
        days = int(metadata["days"])
    except (KeyError, TypeError, ValueError):
        return web.json_response({"ok": False, "error": "invalid metadata"}, status=400)

    success = await activate_subscription(tg_id, days)
    if not success:
        print(f"YooKassa payment {payment_id}: user {tg_id} was not found")
        return web.json_response({"ok": False}, status=500)

    _processed_payment_ids.add(payment_id)

    tariff_title = SUBSCRIPTION_TARIFFS.get(metadata.get("tariff_id"), {}).get("title", f"{days} дней")
    try:
        await bot.send_message(
            tg_id,
            f"✅ Оплата прошла успешно!\n\n"
            f"Ваша подписка активирована: {tariff_title}.\n"
            f"Теперь можно пользоваться ботом без ограничений.",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🏠 Вернуться в главное меню", callback_data="back_to_main")]
            ])
        )
    except Exception as exc:
        print(f"YooKassa payment {payment_id}: failed to notify user {tg_id}: {exc}")

    return web.json_response({"ok": True})


async def start_yookassa_webhook_server() -> web.AppRunner:
    app = web.Application()
    app.router.add_post("/yookassa/webhook", yookassa_webhook)

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(runner, YOOKASSA_WEBHOOK_HOST, YOOKASSA_WEBHOOK_PORT)
    await site.start()
    print(f"YooKassa webhook server started on {YOOKASSA_WEBHOOK_HOST}:{YOOKASSA_WEBHOOK_PORT}")
    return runner
