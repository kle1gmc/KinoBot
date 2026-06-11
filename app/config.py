from dotenv import load_dotenv
import os
from aiogram import Bot, Dispatcher
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.session.aiohttp import AiohttpSession

load_dotenv()
BOT_TOKEN = os.getenv("BOT_TOKEN")
TMDB_TOKEN = os.getenv("API_TMDB")
DATABASE_URL = os.getenv("DATABASE_URL")
PROXY_URL = os.getenv("PROXY_URL")
ADMIN_ID = os.getenv("ADMIN_ID", "")
ADMIN_IDS_RAW = os.getenv("ADMIN_IDS", ADMIN_ID)
ADMIN_IDS = [
    int(admin_id.strip())
    for admin_id in ADMIN_IDS_RAW.replace(";", ",").split(",")
    if admin_id.strip().isdigit()
]

session = AiohttpSession(proxy=PROXY_URL, timeout=120)
bot = Bot(token=BOT_TOKEN, session=session)
storage = MemoryStorage()
dp = Dispatcher(storage=storage)
