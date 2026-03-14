from datetime import datetime, date, timedelta
import asyncio
import io
import os
import random
from urllib.parse import urlparse

import aiohttp
import asyncpg
import requests
from aiogram import types
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import inch
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.pdfgen import canvas
from reportlab.lib.utils import ImageReader

from . import state
from .state import user_sessions, user_filters
from .config import bot, dp, TMDB_TOKEN, DATABASE_URL
from .constants import GENRES_MOVIE, GENRES_TV, COUNTRY_FLAGS

# -------------------- DB INIT --------------------
async def init_db():
    state.db = await asyncpg.create_pool(DATABASE_URL)
    async with state.db.acquire() as conn:
        # Создаем таблицу для отслеживания запросов пользователей
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_requests (
                request_id SERIAL PRIMARY KEY,
                user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
                request_type TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT NOW()
            );
        """)
        await conn.execute("""
                           CREATE TABLE IF NOT EXISTS users
                           (
                            user_id SERIAL PRIMARY KEY,
                            tg_id BIGINT NOT NULL UNIQUE,
                            username TEXT,
                            disable_anime BOOLEAN DEFAULT FALSE,
                            disable_cartoons BOOLEAN DEFAULT FALSE,
                            hide_watched boolean DEFAULT false,
                            agreement_accepted BOOLEAN DEFAULT FALSE,
                            agreement_accepted_at TIMESTAMP
                           );
                           """)
        await conn.execute("""
                           CREATE TABLE IF NOT EXISTS collection
                           (
                            collection_id SERIAL PRIMARY KEY,
                            user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
                            tmdb_id INT NOT NULL,
                            type TEXT NOT NULL,
                            title TEXT,
                            year TEXT,
                            poster_path TEXT,
                            added_at timestamp DEFAULT now()
                               );
                           """)
        await conn.execute("""
                           CREATE TABLE IF NOT EXISTS ratings
                           (
                            rating_id SERIAL PRIMARY KEY,
                            tmdb_id INT NOT NULL,
                            type TEXT NOT NULL,
                            title TEXT,
                            user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
                            liked BOOLEAN DEFAULT FALSE,
                            disliked BOOLEAN DEFAULT FALSE,
                            watched BOOLEAN DEFAULT FALSE,
                            is_hidden BOOLEAN DEFAULT FALSE,
                            CONSTRAINT unique_user_rating UNIQUE (user_id, tmdb_id, type)
                               );
                           """)
        await conn.execute("""
                           CREATE TABLE IF NOT EXISTS user_filters
                           (
                            filter_id SERIAL PRIMARY KEY,
                            user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
                            start_year INT,
                            end_year INT,
                            country_code VARCHAR(10),
                            min_rating DECIMAL(3,1),
                            created_at TIMESTAMP DEFAULT NOW(),
                            updated_at TIMESTAMP DEFAULT NOW(),
                            CONSTRAINT unique_user_filter UNIQUE (user_id)
                               );
                           """)
        await conn.execute("""
                           CREATE INDEX IF NOT EXISTS idx_user_filters_user_id ON user_filters(user_id);
                           """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS banned_content (
                ban_id SERIAL PRIMARY KEY,
                tmdb_id INT NOT NULL,
                type TEXT NOT NULL,
                title TEXT NOT NULL,
                banned_by BIGINT,
                banned_at TIMESTAMP DEFAULT NOW(),
                reason TEXT,
                CONSTRAINT unique_banned_item UNIQUE (tmdb_id, type)
            );
        """)
        await conn.execute("""
                    CREATE TABLE IF NOT EXISTS user_friends (
                        friendship_id SERIAL PRIMARY KEY,
                        user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
                        friend_user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
                        created_at TIMESTAMP DEFAULT NOW(),
                        CONSTRAINT unique_friendship UNIQUE (user_id, friend_user_id)
                    );
                """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS friend_requests (
                request_id SERIAL PRIMARY KEY,
                from_user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
                to_user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
                status TEXT DEFAULT 'pending', -- pending, accepted, rejected
                created_at TIMESTAMP DEFAULT NOW(),
                CONSTRAINT unique_friend_request UNIQUE (from_user_id, to_user_id)
            );
        """)
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS user_subscriptions (
                subscription_id SERIAL PRIMARY KEY,
                user_id INT REFERENCES users(user_id) ON DELETE CASCADE,
                is_active BOOLEAN DEFAULT FALSE,
                expires_at TIMESTAMP,
                created_at TIMESTAMP DEFAULT NOW(),
                updated_at TIMESTAMP DEFAULT NOW(),
                CONSTRAINT unique_user_subscription UNIQUE (user_id)
            );
        """)


# -------------------- DB HELPERS --------------------
async def get_all_users(limit: int = 50, offset: int = 0):
    """Получает список всех пользователей"""
    async with state.db.acquire() as conn:
        rows = await conn.fetch("""
            SELECT u.tg_id, u.username, 
                   us.is_active, us.expires_at,
                   (SELECT COUNT(*) FROM user_requests ur 
                    WHERE ur.user_id = u.user_id 
                    AND DATE(ur.created_at) = CURRENT_DATE) as today_requests
            FROM users u
            LEFT JOIN user_subscriptions us ON u.user_id = us.user_id AND us.is_active = TRUE
            ORDER BY u.user_id DESC
            LIMIT $1 OFFSET $2
        """, limit, offset)
        return rows

async def get_users_count():
    """Получает общее количество пользователей"""
    async with state.db.acquire() as conn:
        count = await conn.fetchval("SELECT COUNT(*) FROM users")
        return count


async def get_user_by_tg_id(tg_id: int):
    """Получает пользователя по TG ID с корректной проверкой подписки"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return None

        # Автоматически деактивируем истекшие подписки
        await conn.execute("""
            UPDATE user_subscriptions 
            SET is_active = FALSE 
            WHERE user_id = $1 AND is_active = TRUE AND expires_at <= NOW()
        """, user["user_id"])

        # Теперь получаем данные
        user_data = await conn.fetchrow("""
            SELECT 
                u.*, 
                us.is_active, 
                us.expires_at,
                (us.is_active = TRUE AND us.expires_at > NOW()) as has_active_subscription,
                CASE 
                    WHEN us.is_active = TRUE AND us.expires_at > NOW() THEN 
                        EXTRACT(DAY FROM (us.expires_at - NOW()))::integer
                    ELSE -1 
                END as days_left
            FROM users u
            LEFT JOIN user_subscriptions us ON u.user_id = us.user_id
            WHERE u.tg_id = $1
        """, tg_id)

        return user_data

async def get_or_create_user(tg_id: int, username: str | None = None):
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
        if not user:
            user = await conn.fetchrow(
                "INSERT INTO users (tg_id, username) VALUES ($1, $2) RETURNING *",
                tg_id, username
            )
        else:
            # Обновляем username если он изменился
            if user['username'] != username:
                await update_user_username(tg_id, username)
                user = await conn.fetchrow("SELECT * FROM users WHERE tg_id=$1", tg_id)
        return user


async def update_user_filter(tg_id: int, field: str, value: bool):
    async with state.db.acquire() as conn:
        await conn.execute(f"UPDATE users SET {field}=$1 WHERE tg_id=$2", value, tg_id)


async def get_user_filters(tg_id: int):
    async with state.db.acquire() as conn:
        row = await conn.fetchrow(
            "SELECT disable_anime, disable_cartoons, hide_watched FROM users WHERE tg_id=$1", tg_id
        )
        if row:
            return {
                "exclude_anime": row["disable_anime"],
                "exclude_cartoons": row["disable_cartoons"],
                "exclude_watched": row["hide_watched"]
            }
        return {"exclude_anime": False, "exclude_cartoons": False, "exclude_watched": False}


async def save_search_filters(tg_id: int, filters: dict):
    """Сохраняет фильтры поиска в базу данных"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return False

        # Проверяем, есть ли уже фильтры для пользователя
        existing = await conn.fetchrow("SELECT * FROM user_filters WHERE user_id=$1", user["user_id"])

        if existing:
            # Обновляем существующие фильтры
            await conn.execute("""
                               UPDATE user_filters
                               SET start_year=$1,
                                   end_year=$2,
                                   country_code=$3,
                                   min_rating=$4,
                                   updated_at=NOW()
                               WHERE user_id = $5
                               """, filters.get('start_year'), filters.get('end_year'), filters.get('country'),
                               filters.get('rating'), user["user_id"])
        else:
            # Создаем новые фильтры
            await conn.execute("""
                               INSERT INTO user_filters (user_id, start_year, end_year, country_code, min_rating)
                               VALUES ($1, $2, $3, $4, $5)
                               """, user["user_id"], filters.get('start_year'), filters.get('end_year'),
                               filters.get('country'), filters.get('rating'))

        return True


async def load_search_filters(tg_id: int):
    """Загружает фильтры поиска из базы данных"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return {}

        row = await conn.fetchrow("""
                                  SELECT start_year, end_year, country_code, min_rating
                                  FROM user_filters
                                  WHERE user_id = $1
                                  """, user["user_id"])

        if row:
            filters = {}
            if row["start_year"] and row["end_year"]:
                filters["start_year"] = row["start_year"]
                filters["end_year"] = row["end_year"]
            if row["country_code"]:
                filters["country"] = row["country_code"]
            if row["min_rating"]:
                filters["rating"] = float(row["min_rating"])
            return filters

        return {}


async def clear_search_filters(tg_id: int):
    """Очищает фильтры поиска пользователя"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return False

        await conn.execute("DELETE FROM user_filters WHERE user_id=$1", user["user_id"])
        return True

async def get_current_filters(chat_id: int):
    """Возвращает текущие фильтры пользователя (из сессии или БД)"""
    if chat_id in user_sessions and "filters" in user_sessions[chat_id]:
        return user_sessions[chat_id]["filters"]
    else:
        # Загружаем из БД если нет в сессии
        filters = await load_search_filters(chat_id)
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        user_sessions[chat_id]["filters"] = filters
        return filters

async def add_to_collection(tg_id: int, tmdb_id: int, type_: str, title: str, year: str, poster_path: str):
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return False
        await conn.execute("""
                           INSERT INTO collection (user_id, tmdb_id, type, title, year, poster_path)
                           VALUES ($1, $2, $3, $4, $5, $6)
                           """, user["user_id"], tmdb_id, type_, title, year, poster_path)
        return True


async def get_collection(tg_id: int, limit=4, offset=0):
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return []
        rows = await conn.fetch("""
                                SELECT *
                                FROM collection
                                WHERE user_id = $1
                                ORDER BY added_at DESC
                                    LIMIT $2
                                OFFSET $3
                                """, user["user_id"], limit, offset)
        return rows


async def get_collection_count(tg_id: int):
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return 0
        row = await conn.fetchrow("""
                                  SELECT COUNT(*)
                                  FROM collection
                                  WHERE user_id = $1
                                  """, user["user_id"])
        return row["count"]


async def remove_from_collection(tg_id: int, tmdb_id: int, type_: str):
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return False
        await conn.execute("""
                           DELETE
                           FROM collection
                           WHERE user_id = $1
                             AND tmdb_id = $2
                             AND type = $3
                           """, user["user_id"], tmdb_id, type_)
        return True


async def add_friend(user_tg_id: int, friend_tg_id: int):
    """Добавляет друга"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", user_tg_id)
        friend = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", friend_tg_id)

        if not user or not friend or user["user_id"] == friend["user_id"]:
            return False

        # Добавляем взаимную дружбу
        await conn.execute("""
            INSERT INTO user_friends (user_id, friend_user_id)
            VALUES ($1, $2), ($2, $1)
            ON CONFLICT (user_id, friend_user_id) DO NOTHING
        """, user["user_id"], friend["user_id"])

        return True


async def get_user_friends(tg_id: int):
    """Получает список друзей пользователя"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return []

        rows = await conn.fetch("""
            SELECT u.tg_id, u.username
            FROM user_friends uf
            JOIN users u ON uf.friend_user_id = u.user_id
            WHERE uf.user_id = $1
            ORDER BY uf.created_at DESC
        """, user["user_id"])

        return rows


async def get_friends_likes(tg_id: int, limit: int = 20):
    """Получает лайки друзей для рекомендаций (только не скрытые)"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return []

        # Получаем все рекомендации (только не скрытые оценки)
        rows = await conn.fetch("""
            SELECT DISTINCT 
                r.tmdb_id, 
                r.type, 
                r.title,
                COUNT(r.liked) as friend_likes_count,
                u.tg_id as friend_tg_id,
                u.username as friend_username
            FROM user_friends uf
            JOIN ratings r ON uf.friend_user_id = r.user_id
            JOIN users u ON r.user_id = u.user_id
            LEFT JOIN ratings user_ratings ON 
                user_ratings.user_id = $1 AND 
                user_ratings.tmdb_id = r.tmdb_id AND 
                user_ratings.type = r.type
            WHERE 
                uf.user_id = $1 AND 
                r.liked = TRUE AND 
                r.watched = TRUE AND
                r.is_hidden = FALSE AND  -- ТОЛЬКО НЕ СКРЫТЫЕ ОЦЕНКИ
                (user_ratings.watched IS NULL OR user_ratings.watched = FALSE)
            GROUP BY r.tmdb_id, r.type, r.title, u.tg_id, u.username
            ORDER BY friend_likes_count DESC
            LIMIT $2
        """, user["user_id"], limit)

        # Фильтруем забаненный контент
        filtered_rows = []
        for row in rows:
            if not await is_banned(row['tmdb_id'], row['type']):
                filtered_rows.append(row)

        return filtered_rows


async def add_rating(user_id, tmdb_id, type_, liked=None, disliked=None, watched=None, is_hidden=None, title=None):
    try:
        async with state.db.acquire() as conn:
            # Сначала получаем текущие значения
            current = await conn.fetchrow(
                "SELECT liked, disliked, watched, is_hidden FROM ratings WHERE user_id = (SELECT user_id FROM users WHERE tg_id = $1) AND tmdb_id = $2 AND type = $3",
                user_id, tmdb_id, type_
            )

            # Если запись существует, обновляем только переданные поля
            if current:
                # Сохраняем текущие значения для полей, которые не переданы
                update_liked = liked if liked is not None else current['liked']
                update_disliked = disliked if disliked is not None else current['disliked']
                update_watched = watched if watched is not None else current['watched']
                update_hidden = is_hidden if is_hidden is not None else current['is_hidden']

                await conn.execute(
                    "UPDATE ratings SET liked = $1, disliked = $2, watched = $3, is_hidden = $4, title = $5 WHERE user_id = (SELECT user_id FROM users WHERE tg_id = $6) AND tmdb_id = $7 AND type = $8",
                    update_liked, update_disliked, update_watched, update_hidden, title, user_id, tmdb_id, type_
                )
            else:
                # Создаем новую запись
                await conn.execute(
                    "INSERT INTO ratings (user_id, tmdb_id, type, liked, disliked, watched, is_hidden, title) VALUES ((SELECT user_id FROM users WHERE tg_id = $1), $2, $3, $4, $5, $6, $7, $8)",
                    user_id, tmdb_id, type_,
                    liked or False,
                    disliked or False,
                    watched or False,
                    is_hidden or False,
                    title
                )
            return True
    except Exception as e:
        print(f"Error in add_rating: {e}")
        return False


async def get_ratings(tmdb_id: int, type_: str):
    async with state.db.acquire() as conn:
        row = await conn.fetchrow("""
                                  SELECT COUNT(CASE WHEN liked = TRUE THEN 1 END)    as likes,
                                         COUNT(CASE WHEN disliked = TRUE THEN 1 END) as dislikes,
                                         COUNT(CASE WHEN watched = TRUE THEN 1 END)  as watches
                                  FROM ratings
                                  WHERE tmdb_id = $1
                                    AND type = $2
                                  """, tmdb_id, type_)
        if row:
            return {
                "likes": row["likes"] or 0,
                "dislikes": row["dislikes"] or 0,
                "watches": row["watches"] or 0
            }
        return {"likes": 0, "dislikes": 0, "watches": 0}

async def ban_content(tmdb_id: int, type_: str, title: str, banned_by: int, reason: str = None):
    """Добавляет контент в бан-лист"""
    async with state.db.acquire() as conn:
        await conn.execute("""
            INSERT INTO banned_content (tmdb_id, type, title, banned_by, reason)
            VALUES ($1, $2, $3, $4, $5)
            ON CONFLICT (tmdb_id, type) DO NOTHING
        """, tmdb_id, type_, title, banned_by, reason)

async def unban_content(tmdb_id: int, type_: str):
    """Убирает контент из бан-листа"""
    async with state.db.acquire() as conn:
        await conn.execute("""
            DELETE FROM banned_content 
            WHERE tmdb_id = $1 AND type = $2
        """, tmdb_id, type_)

async def is_banned(tmdb_id: int, type_: str) -> bool:
    """Проверяет, забанен ли контент"""
    async with state.db.acquire() as conn:
        row = await conn.fetchrow("""
            SELECT 1 FROM banned_content 
            WHERE tmdb_id = $1 AND type = $2
        """, tmdb_id, type_)
        return bool(row)

async def get_banned_list(limit: int = 50):
    """Возвращает список забаненного контента"""
    async with state.db.acquire() as conn:
        return await conn.fetch("""
            SELECT * FROM banned_content 
            ORDER BY banned_at DESC 
            LIMIT $1
        """, limit)

# -------------------- REQUEST LIMIT FUNCTIONS --------------------
async def get_user_requests_count(tg_id: int, target_date: date = None):
    """Получает количество запросов пользователя за день"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return 0

        if target_date is None:
            target_date = date.today()

        count = await conn.fetchval("""
            SELECT COUNT(*) FROM user_requests 
            WHERE user_id=$1 AND DATE(created_at)=$2
        """, user["user_id"], target_date)

        return count or 0

async def add_user_request(tg_id: int, request_type: str):
    """Добавляет запись о запросе пользователя"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return False

        await conn.execute("""
            INSERT INTO user_requests (user_id, request_type) 
            VALUES ($1, $2)
        """, user["user_id"], request_type)
        return True

async def can_make_request(tg_id: int, max_requests: int = 5):
    """Проверяет, может ли пользователь сделать запрос"""
    today_requests = await get_user_requests_count(tg_id)
    return today_requests < max_requests


async def handle_search_request(tg_id: int, request_type: str):
    """Обрабатывает поисковый запрос с проверкой лимита"""
    print(f"DEBUG: Checking request for {tg_id}, type: {request_type}")

    # Проверяем активную подписку
    subscription = await get_user_subscription(tg_id)
    if subscription:
        print(f"DEBUG: User {tg_id} has active subscription - unlimited requests")
        return True, None  # Пользователь с подпиской - безлимит

    # Исключаем некоторые типы запросов из лимита (если нужно)
    EXCLUDED_FROM_LIMIT = [
        "back_to_main", "search_menu", "random_search", "search_filters",
        "settings", "show_collection", "friends_menu", "admin_panel",
        "subscription_management"  # Добавляем управление подпиской
    ]

    if any(request_type.startswith(excluded) for excluded in EXCLUDED_FROM_LIMIT):
        return True, None

    if not await can_make_request(tg_id):
        today_requests = await get_user_requests_count(tg_id)
        return False, f"❌ Лимит запросов исчерпан! Использовано {today_requests}/5 запросов сегодня. Приходите завтра."

    await add_user_request(tg_id, request_type)
    today_requests = await get_user_requests_count(tg_id)
    print(f"DEBUG: Request added. Total today: {today_requests}")
    return True, None

async def get_requests_info(tg_id: int, max_requests: int = 5):
    """Возвращает информацию о запросах пользователя"""
    subscription = await get_user_subscription(tg_id)  # Используем исправленную функцию

    if subscription:
        expires_at = subscription['expires_at']
        days_left = max(0, (expires_at - datetime.now()).days)  # Добавляем max(0, ...) чтобы не было отрицательных значений
        return {
            "has_subscription": True,
            "days_left": days_left,
            "today_requests": 0,
            "remaining": "∞",
            "max_requests": "∞"
        }
    else:
        today_requests = await get_user_requests_count(tg_id)
        remaining = max(0, max_requests - today_requests)
        return {
            "has_subscription": False,
            "days_left": 0,
            "today_requests": today_requests,
            "remaining": remaining,
            "max_requests": max_requests
        }

async def deactivate_expired_subscriptions():
    """Автоматически деактивирует просроченные подписки"""
    async with state.db.acquire() as conn:
        result = await conn.execute("""
            UPDATE user_subscriptions 
            SET is_active = FALSE, updated_at = NOW()
            WHERE is_active = TRUE AND expires_at <= NOW()
        """)
        return result

async def get_user_subscription(tg_id: int):
    """Получает информацию о подписке пользователя"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return None

        # Автоматически деактивируем истекшие подписки при запросе
        await conn.execute("""
            UPDATE user_subscriptions 
            SET is_active = FALSE 
            WHERE user_id = $1 AND is_active = TRUE AND expires_at <= NOW()
        """, user["user_id"])

        # Теперь получаем только активные подписки
        subscription = await conn.fetchrow("""
            SELECT *,
                   EXTRACT(DAY FROM (expires_at - NOW()))::integer as days_left
            FROM user_subscriptions 
            WHERE user_id = $1 AND is_active = TRUE AND expires_at > NOW()
        """, user["user_id"])

        return subscription


async def activate_subscription(tg_id: int, days: int = 30):
    """Активирует подписку пользователю"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return False

        expires_at = datetime.now() + timedelta(days=days)

        # Сначала деактивируем все истекшие подписки
        await conn.execute("""
            UPDATE user_subscriptions 
            SET is_active = FALSE 
            WHERE user_id = $1 AND is_active = TRUE AND expires_at <= NOW()
        """, user["user_id"])

        # Проверяем есть ли активная подписка
        existing = await conn.fetchrow("""
            SELECT * FROM user_subscriptions 
            WHERE user_id = $1 AND is_active = TRUE AND expires_at > NOW()
        """, user["user_id"])

        if existing:
            # Продлеваем существующую активную подписку
            current_expires = existing['expires_at']
            new_expires_at = current_expires + timedelta(days=days)

            await conn.execute("""
                UPDATE user_subscriptions 
                SET expires_at = $1, updated_at = NOW()
                WHERE user_id = $2 AND is_active = TRUE
            """, new_expires_at, user["user_id"])
        else:
            # Создаем новую подписку
            await conn.execute("""
                INSERT INTO user_subscriptions (user_id, is_active, expires_at)
                VALUES ($1, TRUE, $2)
                ON CONFLICT (user_id) DO UPDATE 
                SET is_active = TRUE, expires_at = $2, updated_at = NOW()
            """, user["user_id"], expires_at)

        return True


async def deactivate_subscription(tg_id: int):
    """Деактивирует подписку пользователя"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return False

        await conn.execute("""
            UPDATE user_subscriptions 
            SET is_active = FALSE, updated_at = NOW()
            WHERE user_id = $1
        """, user["user_id"])

        return True

async def check_user_agreement(tg_id: int) -> bool:
    """Проверяет, принял ли пользователь соглашение"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow(
            "SELECT agreement_accepted FROM users WHERE tg_id=$1",
            tg_id
        )
        return user and user["agreement_accepted"]

async def create_user_with_agreement(tg_id: int, username: str | None = None):
    """Создает пользователя и отмечает соглашение как принятое"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow(
            "INSERT INTO users (tg_id, username, agreement_accepted, agreement_accepted_at) VALUES ($1, $2, $3, $4) RETURNING *",
            tg_id, username, True, datetime.now()
        )
        return user

async def accept_user_agreement(tg_id: int):
    """Отмечает соглашение как принятое для существующего пользователя"""
    async with state.db.acquire() as conn:
        await conn.execute(
            "UPDATE users SET agreement_accepted = $1, agreement_accepted_at = $2 WHERE tg_id = $3",
            True, datetime.now(), tg_id
        )

# -------------------- TMDB --------------------
def tmdb_get(url: str, params: dict):
    headers = {"accept": "application/json", "Authorization": f"Bearer {TMDB_TOKEN}"}
    return requests.get(url, headers=headers, params=params, timeout=10)


async def discover_tmdb(type_: str, genre_id: int | None = None, vote_count_min: int = 50, filters: dict = None):
    base_url = f"https://api.themoviedb.org/3/discover/{type_}"
    common = {
        "language": "ru-RU",
        "sort_by": random.choice(["popularity.desc", "vote_average.desc", "primary_release_date.desc"]),
        "vote_count.gte": vote_count_min,
        "page": 1,  # Сначала получаем первую страницу чтобы узнать total_pages
        "include_adult": "false",
    }

    if genre_id:
        common["with_genres"] = genre_id

    # Применяем дополнительные фильтры
    if filters:
        if filters.get('start_year') and filters.get('end_year'):
            start_year = filters['start_year']
            end_year = filters['end_year']
            if type_ == "movie":
                common["primary_release_date.gte"] = f"{start_year}-01-01"
                common["primary_release_date.lte"] = f"{end_year}-12-31"
            else:
                common["first_air_date.gte"] = f"{start_year}-01-01"
                common["first_air_date.lte"] = f"{end_year}-12-31"

        if filters.get('country'):
            common["with_origin_country"] = filters['country']

        if filters.get('rating'):
            common["vote_average.gte"] = filters['rating']

    # 🔴 ИСПРАВЛЕНИЕ: Сначала получаем total_pages
    r1 = tmdb_get(base_url, common)
    if r1.status_code != 200:
        return []

    data1 = r1.json()
    results = data1.get("results", [])
    total_pages = min(data1.get("total_pages", 1), 500)  # Ограничиваем 500 страницами

    # 🟢 ТЕПЕРЬ выбираем случайную страницу
    if total_pages > 1:
        random_page = random.randint(1, total_pages)
        if random_page != 1:
            common["page"] = random_page
            r2 = tmdb_get(base_url, common)
            if r2.status_code == 200:
                results = r2.json().get("results", [])

    # Если ничего не нашли, пробуем снизить порог голосов
    if not results and vote_count_min > 10:
        return await discover_tmdb(type_, genre_id=genre_id, vote_count_min=10, filters=filters)

    # Фильтруем забаненный контент
    async def filter_banned_items(items):
        filtered_items = []
        for item in items:
            if not await is_banned(item["id"], type_):
                filtered_items.append(item)
        return filtered_items

    results = await filter_banned_items(results)
    return results


def get_item_details(type_: str, tmdb_id: int):
    url = f"https://api.themoviedb.org/3/{type_}/{tmdb_id}"
    r = tmdb_get(url, {"language": "ru-RU"})
    if r.status_code == 200:
        return r.json()
    return {}


def get_trailer_url(type_, tmdb_id):
    url = f"https://api.themoviedb.org/3/{type_}/{tmdb_id}/videos"
    r = tmdb_get(url, {"language": "ru-RU"})
    if r.status_code == 200:
        for v in r.json().get("results", []):
            if v.get("type") == "Trailer" and v.get("site") == "YouTube":
                return f"https://www.youtube.com/watch?v={v.get('key')}"
    return None


def get_providers_ru(type_, tmdb_id):
    """Агрегирует провайдеров для РФ по типам и возвращает читабельную строку.

    Формат: "Сервис (подписка/покупка)". Если для РФ явно нет данных — возвращает
    строку 'недоступно в РФ'. При ошибке возвращает None.
    """
    # Ключевые слова для российских сервисов (проверяется в имени провайдера)
    RUS_PROVIDER_KEYWORDS = [
        'ivi', 'okko', 'megogo', 'kinopoisk', 'more.tv', 'amediatka', 'amediatka', 'kion', 'start', 'tvzavr', 'ivi.ru', 'okko.ru', 'moretv', 'ivi hd'
    ]

    try:
        url = f"https://api.themoviedb.org/3/{type_}/{tmdb_id}/watch/providers"
        r = tmdb_get(url, {})
        if r.status_code != 200:
            return None
        data = r.json().get("results", {})
        ru = data.get("RU")
        # Если данных по РФ нет — считаем, что недоступно
        if not ru:
            return "данные не найдены, попробуйте поискать вручную"

        mapping = {}  # provider_name -> set(labels)

        def add_list(arr, label):
            for p in arr or []:
                name = p.get('provider_name')
                if not name:
                    continue
                mapping.setdefault(name, set()).add(label)

        add_list(ru.get('flatrate'), 'подписка')
        add_list(ru.get('rent'), 'аренда')
        add_list(ru.get('buy'), 'покупка')

        if not mapping:
            return "недоступно в РФ"

        parts = []
        # Оставляем только российские сервисы по ключевым словам
        rus_parts = []
        for name, labels in mapping.items():
            lname = name.lower()
            if any(k in lname for k in RUS_PROVIDER_KEYWORDS):
                labels_str = '/'.join(sorted(labels))
                rus_parts.append(f"{name} ({labels_str})")

        if not rus_parts:
            return "недоступно в РФ"

        return ", ".join(rus_parts)
    except Exception:
        return None


def get_trending(media_type: str, time_window: str = "week"):
    """Получает трендовые фильмы/сериалы за неделю"""
    url = f"https://api.themoviedb.org/3/trending/{media_type}/{time_window}"
    r = tmdb_get(url, {"language": "ru-RU"})
    if r.status_code == 200:
        return r.json().get("results", [])
    return []


def is_anime_by_details(type_: str, details: dict, item: dict) -> bool:
    genre_ids = [g.get("id") for g in details.get("genres", []) if g.get("id")] or item.get("genre_ids", [])
    if 16 not in genre_ids:
        return False
    prod_countries = [c.get("iso_3166_1") for c in details.get("production_countries", []) if c.get("iso_3166_1")]
    origin_country = details.get("origin_country", []) or []
    codes = set(prod_countries + origin_country)
    return "JP" in codes


def is_cartoons_by_details(type_: str, details: dict, item: dict) -> bool:
    genre_ids = [g.get("id") for g in details.get("genres", []) if g.get("id")] or item.get("genre_ids", [])
    return 16 in genre_ids


# -------------------- KEYBOARDS --------------------
def kb_main():
    """Главное меню"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск", callback_data="search_menu")],
        [InlineKeyboardButton(text="⚡ Фильтры поиска", callback_data="search_filters")],
        [InlineKeyboardButton(text="📚 Коллекция", callback_data="show_collection")],
        [InlineKeyboardButton(text="👥 Друзья", callback_data="friends_menu")],
        [InlineKeyboardButton(text="👤 Мой профиль", callback_data="my_profile")],  # НОВАЯ КНОПКА
        [InlineKeyboardButton(text="💫 Управление подпиской", callback_data="subscription_management")],
        [InlineKeyboardButton(text="🔄 Обновить страницу", callback_data="refresh_main")],
        [InlineKeyboardButton(text="⚙️ Настройки", callback_data="settings")],
    ])


def kb_search_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎲 Случайный поиск", callback_data="random_search")],
        [InlineKeyboardButton(text="🔍 Поиск по названию", callback_data="search_by_title")],
        [InlineKeyboardButton(text="🎭 Поиск по актеру", callback_data="search_by_person")],
        [InlineKeyboardButton(text="🎯 На основе предпочтений", callback_data="preferences")],
        [InlineKeyboardButton(text="🔥 В тренде сейчас", callback_data="trending_menu")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")],
    ])


def kb_random_search():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Случайный фильм", callback_data="discover_movie")],
        [InlineKeyboardButton(text="📺 Случайный сериал", callback_data="discover_tv")],
        [InlineKeyboardButton(text="🧭 Случайный поиск по жанрам", callback_data="search_genre")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="search_menu")],
    ])


def kb_settings(filters):
    anime_status = "✅" if filters.get("exclude_anime") else "❌"
    cartoons_status = "✅" if filters.get("exclude_cartoons") else "❌"
    watched_status = "✅" if filters.get("exclude_watched") else "❌"
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"{anime_status} Скрывать аниме", callback_data="toggle_anime")],
        [InlineKeyboardButton(text=f"{cartoons_status} Скрывать мультфильмы", callback_data="toggle_cartoons")],
        [InlineKeyboardButton(text=f"{watched_status} Скрывать просмотренное", callback_data="toggle_watched")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")],
    ])

def kb_my_profile():
    """Клавиатура меню профиля"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 Обновить профиль", callback_data="update_profile")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")],
    ])

def kb_export_options():
    """Клавиатура выбора формата экспорта"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📄 PDF", callback_data="export_pdf"),
         InlineKeyboardButton(text="📊 CSV", callback_data="export_csv")],
        [InlineKeyboardButton(text="⬅️ Назад к коллекции", callback_data="show_collection")]
    ])

def kb_admin_panel():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔍 Поиск для бана", callback_data="admin_search_ban")],
        [InlineKeyboardButton(text="📋 Список банов", callback_data="admin_ban_list")],
        [InlineKeyboardButton(text="📊 Статистика", callback_data="admin_stats")],
        [InlineKeyboardButton(text="🌟 Управление подписками", callback_data="admin_subscriptions")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
    ])


def kb_admin_stats(sort_by: str, page: int, total_pages: int):
    """Клавиатура для панели статистики"""
    sort_buttons = [
        [
            InlineKeyboardButton(
                text=f"🕐 По дате {'✅' if sort_by == 'updated' else ''}",
                callback_data="stats_sort_updated"
            ),
            InlineKeyboardButton(
                text=f"👍 По лайкам {'✅' if sort_by == 'likes' else ''}",
                callback_data="stats_sort_likes"
            )
        ],
        [
            InlineKeyboardButton(
                text=f"👎 По дизлайкам {'✅' if sort_by == 'dislikes' else ''}",
                callback_data="stats_sort_dislikes"
            ),
            InlineKeyboardButton(
                text=f"👀 По просмотрам {'✅' if sort_by == 'watches' else ''}",
                callback_data="stats_sort_watches"
            )
        ]
    ]

    # Кнопки пагинации
    pagination_buttons = []
    if page > 0:
        pagination_buttons.append(
            InlineKeyboardButton(text="⬅️ Назад", callback_data=f"stats_page_{page - 1}_{sort_by}"))

    pagination_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="stats_info"))

    if page < total_pages - 1:
        pagination_buttons.append(
            InlineKeyboardButton(text="Вперед ➡️", callback_data=f"stats_page_{page + 1}_{sort_by}"))

    if pagination_buttons:
        sort_buttons.append(pagination_buttons)

    # НОВЫЕ КНОПКИ ЭКСПОРТА
    sort_buttons.append([
        InlineKeyboardButton(text="📄 Выгрузить в PDF", callback_data="stats_export_pdf"),
        InlineKeyboardButton(text="📊 Диаграммы в PDF", callback_data="stats_charts_pdf")
    ])

    sort_buttons.append([
        InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_panel")
    ])

    return InlineKeyboardMarkup(inline_keyboard=sort_buttons)


def kb_admin_subscriptions_management():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Список пользователей", callback_data="admin_users_list")],
        [InlineKeyboardButton(text="🔍 Поиск пользователя", callback_data="admin_search_user")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
    ])


def kb_subscription_management(has_subscription: bool, days_left: int = 0, expires_at=None):
    """Клавиатура управления подпиской для пользователя"""
    if has_subscription:
        keyboard = [
            [InlineKeyboardButton(text="📅 Продлить подписку", callback_data="extend_my_subscription")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
        ]
    else:
        keyboard = [
            [InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_subscription")],
            [InlineKeyboardButton(text="ℹ️ О подписке", callback_data="subscription_info")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
        ]

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def kb_subscription_info():
    """Клавиатура с информацией о подписке"""
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Купить подписку", callback_data="buy_subscription")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="subscription_management")]
    ])


def kb_users_list(users: list, page: int, total_pages: int):
    """Клавиатура списка пользователей"""
    keyboard = []

    for user in users:
        tg_id = user['tg_id']
        username = user['username'] or f"Пользователь {tg_id}"
        has_subscription = user['is_active']

        # Обрезаем длинные имена
        if len(username) > 20:
            username = username[:17] + "..."

        status_icon = "🌟" if has_subscription else "👤"
        button_text = f"{status_icon} {username}"

        keyboard.append([
            InlineKeyboardButton(
                text=button_text,
                callback_data=f"admin_user_{tg_id}"
            )
        ])

    # Пагинация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"users_page_{page - 1}"))

    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="users_info"))

    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"users_page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_subscriptions")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def kb_user_management(tg_id: int, has_active_subscription: bool, days_left: int = 0):
    """Клавиатура управления конкретным пользователем"""
    # Подписка считается активной только если дней больше или равно 0
    is_really_active = has_active_subscription and days_left >= 0

    if is_really_active:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Аннулировать подписку", callback_data=f"revoke_sub_{tg_id}")],
            [InlineKeyboardButton(text="📅 Продлить подписку", callback_data=f"extend_sub_{tg_id}")],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin_users_list")]
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🌟 Выдать подписку", callback_data=f"grant_sub_{tg_id}")],
            [InlineKeyboardButton(text="⬅️ К списку", callback_data="admin_users_list")]
        ])

async def set_bot_commands():
    commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="admin", description="Админ-панель"),
        BotCommand(command="search", description="Поиск по TMDB ID"),
        BotCommand(command="myid", description="Показать ваш ID"),
        BotCommand(command="subscription", description="Информация о подписке"),  # Новая команда
    ]

    await bot.set_my_commands(commands)
    print("✅ Команды бота установлены")


async def kb_ban_confirmation(tmdb_id: int, type_: str, title: str):
    # Проверяем, забанен ли уже контент
    is_already_banned = await is_banned(tmdb_id, type_)

    if is_already_banned:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🔓 Разбанить", callback_data=f"confirm_unban_{tmdb_id}_{type_}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="delete_message")
            ]
        ])
    else:
        return InlineKeyboardMarkup(inline_keyboard=[
            [
                InlineKeyboardButton(text="🚫 Забанить", callback_data=f"confirm_ban_{tmdb_id}_{type_}"),
                InlineKeyboardButton(text="❌ Отмена", callback_data="delete_message")
            ]
        ])


def get_country_flag(country_code: str) -> str:
    """Возвращает флаг страны или код, если флаг не найден"""
    return COUNTRY_FLAGS.get(country_code, country_code)

def kb_filters_menu(current_filters: dict):
    # Отображаем год/диапазон
    if current_filters.get('start_year') and current_filters.get('end_year'):
        start_year = current_filters['start_year']
        end_year = current_filters['end_year']
        if start_year == end_year:
            year_btn = f"📅 Год: {start_year}"
        else:
            year_btn = f"📅 Года: {start_year}-{end_year}"
    else:
        year_btn = "📅 Года: Любые"

    # Отображаем страну с флагом
    country_value = current_filters.get('country')
    if country_value:
        country_flag = get_country_flag(country_value)
        country_btn = f"🌍 Страна: {country_flag}"
    else:
        country_btn = "🌍 Страна: Любая"

    rating_btn = f"⭐ Рейтинг: {current_filters.get('rating', 'Любой')}+"

    filters_active = any(current_filters.values())
    status_btn = "✅ Фильтры активны" if filters_active else "❌ Фильтры не активны"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=status_btn, callback_data="filters_status")],
        [InlineKeyboardButton(text=year_btn, callback_data="filter_year")],
        [InlineKeyboardButton(text=country_btn, callback_data="filter_country")],
        [InlineKeyboardButton(text=rating_btn, callback_data="filter_rating")],
        [InlineKeyboardButton(text="🔄 Сбросить все фильтры", callback_data="reset_all_filters")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")],
    ])


def kb_rating_selection():
    ratings = [1.0, 2.0, 3.0, 4.0, 5.0, 6.0, 7.0, 8.0, 9.0]

    keyboard = []
    row = []
    for rating in ratings:
        row.append(InlineKeyboardButton(text=f"{rating}+", callback_data=f"set_rating_{rating}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    keyboard.append([InlineKeyboardButton(text="❌ Без рейтинга", callback_data="clear_rating")])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="search_filters")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def is_in_user_collection(tg_id: int, tmdb_id: int, type_: str) -> bool:
    """Проверяет, находится ли контент в коллекции пользователя"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return False

        row = await conn.fetchrow("""
            SELECT 1 FROM collection 
            WHERE user_id = $1 AND tmdb_id = $2 AND type = $3
        """, user["user_id"], tmdb_id, type_)

        return bool(row)


async def kb_card(chat_id: int, tmdb_id: int, type_: str, is_genre_search: bool = False, is_trending: bool = False):
    buttons = []
    trailer_url = get_trailer_url(type_, tmdb_id)
    if trailer_url:
        buttons.append([InlineKeyboardButton(text="▶️ Трейлер", url=trailer_url)])

    is_in_collection = await is_in_user_collection(chat_id, tmdb_id, type_)

    if is_in_collection:
        buttons.append([
            InlineKeyboardButton(text="✅ В коллекции", callback_data=f"already_in_collection"),
        ])
    else:
        buttons.append([
            InlineKeyboardButton(text="➕ В коллекцию", callback_data=f"add_{tmdb_id}_{type_}"),
        ])
    buttons.append([InlineKeyboardButton(text="➡️ Следующий", callback_data="next_item")])

    if is_genre_search:
        buttons.append([InlineKeyboardButton(text="⬅️ К жанрам", callback_data=f"back_to_genres_{type_}")])

    if is_trending:
        buttons.append([InlineKeyboardButton(text="⬅️ К трендам", callback_data="trending_menu")])

    buttons.append([InlineKeyboardButton(text="🔍 Меню поиска", callback_data="search_menu")])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_collection_item(tmdb_id: int, type_: str, watched: bool = False, liked: bool | None = None,
                       disliked: bool | None = None, is_hidden: bool = False):
    buttons = []
    trailer_url = get_trailer_url(type_, tmdb_id)
    if trailer_url:
        buttons.append([InlineKeyboardButton(text="▶️ Трейлер", url=trailer_url)])

    watched_text = "✅ Просмотрено" if watched else "👀 Отметить просмотр"
    buttons.append([InlineKeyboardButton(text=watched_text, callback_data=f"mark_watched_{tmdb_id}_{type_}")])

    like_text = "👍 Лайк ✅" if liked is True else "👍 Лайк"
    dislike_text = "👎 Дизлайк ✅" if disliked is True else "👎 Дизлайк"

    # НОВАЯ КНОПКА - скрыть оценку от друзей
    hide_text = "🙈 Скрыть от друзей ✅" if is_hidden else "🙈 Скрыть от друзей"

    buttons.append([
        InlineKeyboardButton(text=like_text, callback_data=f"like_{tmdb_id}_{type_}"),
        InlineKeyboardButton(text=dislike_text, callback_data=f"dislike_{tmdb_id}_{type_}")
    ])

    # Показываем кнопку скрытия только если есть оценка
    if liked is True or disliked is True:
        buttons.append([
            InlineKeyboardButton(text=hide_text, callback_data=f"toggle_hide_{tmdb_id}_{type_}")
        ])

    # Добавляем кнопку "Снять оценку", показываем только если была оценка
    if (liked is True) or (disliked is True):
        buttons.append([InlineKeyboardButton(text="🔄 Снять оценку", callback_data=f"reset_rating_{tmdb_id}_{type_}")])

    buttons.append([InlineKeyboardButton(text="❌ Удалить", callback_data=f"remove_{tmdb_id}_{type_}")])
    buttons.append([InlineKeyboardButton(text="⬅️ К коллекции", callback_data="show_collection")])
    buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def kb_trending_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🎬 Фильмы за неделю", callback_data="trending_movie_week")],
        [InlineKeyboardButton(text="📺 Сериалы за неделю", callback_data="trending_tv_week")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="search_menu")],
    ])


async def update_user_username(tg_id: int, username: str | None):
    """Обновляет username пользователя в базе данных"""
    async with state.db.acquire() as conn:
        # Сначала получаем текущий username для отладки
        current_user = await conn.fetchrow("SELECT username FROM users WHERE tg_id = $1", tg_id)
        current_username = current_user['username'] if current_user else None

        print(f"DEBUG: Current username: '{current_username}', New username: '{username}'")
        print(f"DEBUG: Are they different? {current_username != username}")

        # Простое обновление без сложных проверок
        result = await conn.execute("""
            UPDATE users 
            SET username = $1 
            WHERE tg_id = $2
        """, username, tg_id)

        print(f"DEBUG: Update result: {result}")

        # Всегда возвращаем True при обновлении, даже если значения совпадают
        # PostgreSQL все равно выполнит UPDATE, но мы можем проверить по результату
        return "UPDATE 1" in result or "UPDATE 0" in result


async def filter_watched_items(tg_id: int, items: list, type_: str):
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return items
        watched_ids = await conn.fetch(
            "SELECT tmdb_id FROM ratings WHERE user_id=$1 AND type=$2 AND watched = true",
            user["user_id"], type_
        )
        watched_ids = {row["tmdb_id"] for row in watched_ids}
        return [item for item in items if item["id"] not in watched_ids]


async def get_user_likes(tg_id: int):
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return []
        rows = await conn.fetch("""
                                SELECT tmdb_id, type
                                FROM ratings
                                WHERE user_id = $1
                                  AND liked = true
                                """, user["user_id"])
        return [{"tmdb_id": row["tmdb_id"], "type": row["type"]} for row in rows]


async def kb_collection(tg_id: int, page: int, total_pages: int):
    """Клавиатура коллекции с кнопками экспорта и очистки"""
    requests_info = await get_requests_info(tg_id)

    keyboard = InlineKeyboardMarkup(inline_keyboard=[])
    collection = await get_collection(tg_id, limit=4, offset=page * 4)

    for item in collection:
        if not await is_banned(item["tmdb_id"], item["type"]):
            keyboard.inline_keyboard.append([
                InlineKeyboardButton(
                    text=f"{item['title']} ({item['year']})",
                    callback_data=f"show_collection_item_{item['tmdb_id']}_{item['type']}"
                )
            ])

    navigation = []
    if page > 0:
        navigation.append(InlineKeyboardButton(text="⬅️ Предыдущая", callback_data=f"collection_page_{page - 1}"))
    if page < total_pages - 1:
        navigation.append(InlineKeyboardButton(text="Следующая ➡️", callback_data=f"collection_page_{page + 1}"))

    if navigation:
        keyboard.inline_keyboard.append(navigation)

    # Кнопки управления коллекцией
    action_buttons = []
    if requests_info["has_subscription"]:
        action_buttons.append(InlineKeyboardButton(text="📤 Экспорт", callback_data="export_menu"))
        action_buttons.append(InlineKeyboardButton(text="📥 Импорт", callback_data="import_collection"))

    if action_buttons:
        keyboard.inline_keyboard.append(action_buttons)

    # Кнопка очистки коллекции (всегда доступна)
    keyboard.inline_keyboard.append([
        InlineKeyboardButton(text="🗑️ Очистить коллекцию", callback_data="confirm_clear_collection")
    ])

    keyboard.inline_keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])
    return keyboard


def kb_friends_menu():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="👥 Мои друзья", callback_data="my_friends")],
        [InlineKeyboardButton(text="➕ Добавить друга", callback_data="add_friend")],
        [InlineKeyboardButton(text="📨 Управление заявками", callback_data="friend_requests_management")],  # НОВАЯ КНОПКА
        [InlineKeyboardButton(text="🎯 Рекомендации друзей", callback_data="friends_recommendations")],
        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
    ])


def kb_friend_requests_management():
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="📥 Входящие заявки", callback_data="friend_requests")],
        [InlineKeyboardButton(text="📤 Исходящие заявки", callback_data="outgoing_requests")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="friends_menu")]
    ])


def kb_outgoing_requests(requests_list, page=0, requests_per_page=10):
    keyboard = []

    start_idx = page * requests_per_page
    end_idx = start_idx + requests_per_page
    page_requests = requests_list[start_idx:end_idx]

    for req in page_requests:
        friend_name = req['username'] or f"Пользователь {req['tg_id']}"
        keyboard.append([
            InlineKeyboardButton(text=f"👤 {friend_name}", callback_data=f"outgoing_request_{req['request_id']}")
        ])

    # Пагинация
    nav_buttons = []
    total_pages = (len(requests_list) + requests_per_page - 1) // requests_per_page

    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"outgoing_page_{page - 1}"))

    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="outgoing_info"))

    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"outgoing_page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="friends_menu")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

def kb_friend_profile(friend_tg_id: int):
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="❌ Удалить друга", callback_data=f"remove_friend_{friend_tg_id}")],
        [InlineKeyboardButton(text="⬅️ Назад", callback_data="my_friends")]
    ])

def kb_my_friends(friends_list, page=0, friends_per_page=10):
    keyboard = []

    start_idx = page * friends_per_page
    end_idx = start_idx + friends_per_page
    page_friends = friends_list[start_idx:end_idx]

    for friend in page_friends:
        friend_name = friend['username'] or f"Пользователь {friend['tg_id']}"
        keyboard.append([
            InlineKeyboardButton(text=f"👤 {friend_name}", callback_data=f"friend_{friend['tg_id']}")
        ])

    # Пагинация
    nav_buttons = []
    total_pages = (len(friends_list) + friends_per_page - 1) // friends_per_page

    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️", callback_data=f"friends_page_{page - 1}"))

    nav_buttons.append(InlineKeyboardButton(text=f"{page + 1}/{total_pages}", callback_data="friends_info"))

    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="➡️", callback_data=f"friends_page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="friends_menu")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)

async def remove_friend(user_tg_id: int, friend_tg_id: int):
    """Удаляет друга"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", user_tg_id)
        friend = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", friend_tg_id)

        if not user or not friend:
            return False

        # Удаляем взаимную дружбу
        await conn.execute("""
            DELETE FROM user_friends 
            WHERE (user_id = $1 AND friend_user_id = $2) 
               OR (user_id = $2 AND friend_user_id = $1)
        """, user["user_id"], friend["user_id"])

        return True

def is_admin(chat_id: int) -> bool:
    # Здесь можешь добавить проверку по ID админов
    admin_ids = [950764975]  # Замени на реальные ID админов
    return chat_id in admin_ids


async def generate_stats_pdf(stats_data: dict, sort_by: str):
    """Генерирует PDF со статистикой с эмодзи"""
    has_russian_font = register_russian_font()

    buffer = io.BytesIO()
    pdf = canvas.Canvas(buffer, pagesize=A4)
    width, height = A4

    if has_russian_font:
        font_normal = "RussianFont"
        font_bold = "RussianFont"
    else:
        font_normal = "Helvetica"
        font_bold = "Helvetica-Bold"

    # Заголовок
    pdf.setFont(font_bold, 16)
    pdf.drawString(50, height - 50, "Статистика контента")
    pdf.setFont(font_normal, 10)

    sort_descriptions = {
        "updated": "по дате обновления",
        "likes": "по лайкам",
        "dislikes": "по дизлайкам",
        "watches": "по просмотрам"
    }
    pdf.drawString(50, height - 70, f"Сортировка: {sort_descriptions.get(sort_by, 'по дате')}")
    pdf.drawString(50, height - 85, f"Всего: {stats_data['total_count']} элементов")
    pdf.drawString(50, height - 100, f"Дата генерации: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

    y_position = height - 130

    for i, item in enumerate(stats_data["items"]):
        if y_position < 120:
            pdf.showPage()
            y_position = height - 50

        item_height = 90
        center_line = y_position - (item_height / 2)

        # ПОСТЕР
        poster_width = 60
        poster_height = 80
        poster_x = width - 80
        poster_y = center_line - (poster_height / 2)

        if item.get('tmdb_id'):
            try:
                details = get_item_details(item['type'], item['tmdb_id'])
                if details and details.get('poster_path') and details['poster_path'] != "/default.jpg":
                    poster_url = f"https://image.tmdb.org/t/p/w154{details['poster_path']}"
                    response = requests.get(poster_url, timeout=10)
                    if response.status_code == 200:
                        img_data = io.BytesIO(response.content)
                        img_reader = ImageReader(img_data)
                        pdf.drawImage(img_reader, poster_x, poster_y,
                                      width=poster_width, height=poster_height,
                                      preserveAspectRatio=True, mask='auto')
            except Exception as e:
                print(f"Poster loading error: {e}")

        # ТЕКСТ
        text_start_y = center_line + 20

        # Название
        pdf.setFont(font_bold, 12)
        title = item['title'] or "Без названия"
        if len(title) > 40:
            title = title[:37] + "..."
        pdf.drawString(50, text_start_y, title)

        # Тип и ID
        pdf.setFont(font_normal, 10)
        type_text = "Фильм" if item['type'] == 'movie' else "Сериал"
        pdf.drawString(50, text_start_y - 15, f"{type_text}, ID: {item['tmdb_id']}")

        # Статистика с эмодзи - используем обычный текст
        # PDF нормально отображает базовые эмодзи
        stats_text = f"Лайки: {item['likes']}   Дизлайки: {item['dislikes']}   Просмотры: {item['watches']}"
        pdf.drawString(50, text_start_y - 30, stats_text)

        y_position -= item_height

        # Разделитель
        if i < len(stats_data["items"]) - 1:
            pdf.line(50, y_position + 5, width - 50, y_position + 5)
            y_position -= 10

    pdf.save()
    buffer.seek(0)
    return buffer


async def can_make_request(tg_id: int, max_requests: int = 5):
    """Проверяет, может ли пользователь сделать запрос"""
    # Проверяем активную подписку
    subscription = await get_user_subscription(tg_id)
    if subscription:
        return True  # У пользователя есть подписка - безлимит

    # Если нет подписки - проверяем лимит
    today_requests = await get_user_requests_count(tg_id)
    return today_requests < max_requests


async def get_requests_info(tg_id: int, max_requests: int = 5):
    """Возвращает информацию о запросах пользователя"""
    subscription = await get_user_subscription(tg_id)

    if subscription and subscription.get('days_left', -1) >= 0:
        expires_at = subscription['expires_at']
        days_left = max(0, (expires_at - datetime.now()).days)  # Не показываем отрицательные дни
        return {
            "has_subscription": True,
            "days_left": days_left,
            "today_requests": 0,
            "remaining": "∞",
            "max_requests": "∞"
        }
    else:
        today_requests = await get_user_requests_count(tg_id)
        remaining = max(0, max_requests - today_requests)
        return {
            "has_subscription": False,
            "days_left": 0,
            "today_requests": today_requests,
            "remaining": remaining,
            "max_requests": max_requests
        }

async def generate_stats_charts_pdf(stats_data: dict):
    """Генерирует PDF с диаграммами статистики (2 диаграммы на страницу)"""
    try:
        import matplotlib.pyplot as plt
        from matplotlib.ticker import MaxNLocator
        import numpy as np
    except ImportError:
        return None

    try:
        items = stats_data["items"]

        # Создаем буфер для всех диаграмм
        chart_buffers = []

        # 1. ПЕРВАЯ СТРАНИЦА: Топ-5 по лайкам и просмотрам
        fig1, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 12))

        # 1.1 Топ-5 по лайкам
        top_likes = sorted(items, key=lambda x: x['likes'], reverse=True)[:5]
        titles_likes = [item['title'][:20] + "..." if len(item['title']) > 20 else item['title'] for item in top_likes]
        likes = [item['likes'] for item in top_likes]

        bars1 = ax1.barh(titles_likes, likes, color=['#4CAF50', '#66BB6A', '#81C784', '#A5D6A7', '#C8E6C9'])
        ax1.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax1.set_title('Топ-5 по лайкам', fontsize=14, fontweight='bold', pad=20)
        ax1.set_xlabel('Количество лайков', fontsize=12)
        ax1.tick_params(axis='y', labelsize=10)

        # Добавляем значения на столбцы
        for i, v in enumerate(likes):
            ax1.text(v + max(likes) * 0.01, i, f"{int(v)}", va='center', fontsize=10, fontweight='bold')

        # 1.2 Топ-5 по просмотрам
        top_watches = sorted(items, key=lambda x: x['watches'], reverse=True)[:5]
        titles_watches = [item['title'][:20] + "..." if len(item['title']) > 20 else item['title'] for item in
                          top_watches]
        watches = [item['watches'] for item in top_watches]

        bars2 = ax2.barh(titles_watches, watches, color=['#2196F3', '#42A5F5', '#64B5F6', '#90CAF9', '#BBDEFB'])
        ax2.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax2.set_title('Топ-5 по просмотрам', fontsize=14, fontweight='bold', pad=20)
        ax2.set_xlabel('Количество просмотров', fontsize=12)
        ax2.tick_params(axis='y', labelsize=10)

        # Добавляем значения на столбцы
        for i, v in enumerate(watches):
            ax2.text(v + max(watches) * 0.01, i, f"{int(v)}", va='center', fontsize=10, fontweight='bold')

        plt.tight_layout(pad=4.0)

        # Сохраняем первую страницу
        buffer1 = io.BytesIO()
        plt.savefig(buffer1, format='png', dpi=150, bbox_inches='tight')
        plt.close(fig1)
        buffer1.seek(0)
        chart_buffers.append(buffer1)

        # 2. ВТОРАЯ СТРАНИЦА: Топ-5 по дизлайкам и соотношение фильмов/сериалов
        fig2, (ax3, ax4) = plt.subplots(2, 1, figsize=(10, 12))

        # 2.1 Топ-5 по дизлайкам
        top_dislikes = sorted(items, key=lambda x: x['dislikes'], reverse=True)[:5]
        titles_dislikes = [item['title'][:20] + "..." if len(item['title']) > 20 else item['title'] for item in
                           top_dislikes]
        dislikes = [item['dislikes'] for item in top_dislikes]

        bars3 = ax3.barh(titles_dislikes, dislikes, color=['#F44336', '#EF5350', '#E57373', '#EF9A9A', '#FFCDD2'])
        ax3.xaxis.set_major_locator(MaxNLocator(integer=True))
        ax3.set_title('Топ-5 по дизлайкам', fontsize=14, fontweight='bold', pad=20)
        ax3.set_xlabel('Количество дизлайков', fontsize=12)
        ax3.tick_params(axis='y', labelsize=10)

        # Добавляем значения на столбцы
        for i, v in enumerate(dislikes):
            ax3.text(v + max(dislikes) * 0.01, i, f"{int(v)}", va='center', fontsize=10, fontweight='bold')

        # 2.2 Соотношение фильмов и сериалов (круговая диаграмма)
        movie_count = sum(1 for item in items if item['type'] == 'movie')
        tv_count = sum(1 for item in items if item['type'] == 'tv')

        sizes = [movie_count, tv_count]
        labels = ['Фильмы', 'Сериалы']
        colors = ['#FF9800', '#9C27B0']
        explode = (0.05, 0.05)  # Немного выдвигаем сектора

        # Если есть данные для круговой диаграммы
        if movie_count > 0 or tv_count > 0:
            wedges, texts, autotexts = ax4.pie(sizes, explode=explode, labels=labels, colors=colors,
                                               autopct=lambda p: f'{int(round(p))}%', shadow=True, startangle=90,
                                               textprops={'fontsize': 12})

            # Делаем проценты жирными
            for autotext in autotexts:
                autotext.set_color('white')
                autotext.set_fontweight('bold')
                autotext.set_fontsize(11)

            ax4.set_title('Соотношение фильмов и сериалов', fontsize=14, fontweight='bold', pad=20)

            # Добавляем легенду с количеством
            legend_labels = [f'{label}: {size}' for label, size in zip(labels, sizes)]
            ax4.legend(wedges, legend_labels, title="Количество", loc="center left",
                       bbox_to_anchor=(0.9, 0, 0.5, 1), fontsize=10)
        else:
            # Если нет данных
            ax4.text(0.5, 0.5, 'Нет данных\nо типах контента',
                     horizontalalignment='center', verticalalignment='center',
                     transform=ax4.transAxes, fontsize=14, fontweight='bold')
            ax4.set_title('Соотношение фильмов и сериалов', fontsize=14, fontweight='bold', pad=20)

        plt.tight_layout(pad=4.0)

        # Сохраняем вторую страницу
        buffer2 = io.BytesIO()
        plt.savefig(buffer2, format='png', dpi=150, bbox_inches='tight')
        plt.close(fig2)
        buffer2.seek(0)
        chart_buffers.append(buffer2)

        # 3. ТРЕТЬЯ СТРАНИЦА: Общая статистика и соотношение лайков/дизлайков
        fig3, (ax5, ax6) = plt.subplots(2, 1, figsize=(10, 12))

        # 3.1 Общая статистика в виде таблицы
        ax5.axis('off')

        total_likes = sum(item['likes'] for item in items)
        total_dislikes = sum(item['dislikes'] for item in items)
        total_watches = sum(item['watches'] for item in items)
        total_items = len(items)

        # Создаем красивую таблицу с общей статистикой
        stats_data_table = [
            ['  ОБЩАЯ СТАТИСТИКА', ''],
            ['Всего записей:', f'{total_items}'],
            ['Фильмы:', f'{movie_count}'],
            ['Сериалы:', f'{tv_count}'],
            ['Всего лайков:', f'{total_likes}'],
            ['Всего дизлайков:', f'{total_dislikes}'],
            ['Всего просмотров:', f'{total_watches}'],
        ]

        # Создаем таблицу
        table = ax5.table(cellText=stats_data_table,
                          cellLoc='left',
                          loc='center',
                          bbox=[0.1, 0.2, 0.8, 0.6])

        table.auto_set_font_size(False)
        table.set_fontsize(11)
        table.scale(1, 2)

        # Стилизуем заголовок таблицы
        for i in range(2):
            table[(0, i)].set_facecolor('#4CAF50')
            table[(0, i)].set_text_props(weight='bold', color='white')

        # Стилизуем остальные ячейки
        for i in range(1, len(stats_data_table)):
            for j in range(2):
                if i % 2 == 0:
                    table[(i, j)].set_facecolor('#f5f5f5')

        ax5.set_title('Общая статистика базы данных', fontsize=16, fontweight='bold', pad=30)

        # 3.2 Соотношение лайков и дизлайков (круговая диаграмма)
        if total_likes > 0 or total_dislikes > 0:
            sizes_likes = [total_likes, total_dislikes]
            labels_likes = ['Лайки', 'Дизлайки']
            colors_likes = ['#4CAF50', '#F44336']

            wedges2, texts2, autotexts2 = ax6.pie(sizes_likes, labels=labels_likes, colors=colors_likes,
                                                  autopct=lambda p: f'{int(round(p))}%', shadow=True, startangle=90,
                                                  textprops={'fontsize': 12})

            # Делаем проценты жирными
            for autotext in autotexts2:
                autotext.set_color('white')
                autotext.set_fontweight('bold')
                autotext.set_fontsize(11)

            ax6.set_title('Соотношение лайков и дизлайков', fontsize=14, fontweight='bold', pad=20)

            # Добавляем легенду с количеством
            legend_labels2 = [f'{label}: {size}' for label, size in zip(labels_likes, sizes_likes)]
            ax6.legend(wedges2, legend_labels2, title="Количество", loc="center left",
                       bbox_to_anchor=(0.9, 0, 0.5, 1), fontsize=10)
        else:
            ax6.text(0.5, 0.5, 'Нет данных\nо реакциях',
                     horizontalalignment='center', verticalalignment='center',
                     transform=ax6.transAxes, fontsize=14, fontweight='bold')
            ax6.set_title('Соотношение лайков и дизлайков', fontsize=14, fontweight='bold', pad=20)

        plt.tight_layout(pad=4.0)

        # Сохраняем третью страницу
        buffer3 = io.BytesIO()
        plt.savefig(buffer3, format='png', dpi=150, bbox_inches='tight')
        plt.close(fig3)
        buffer3.seek(0)
        chart_buffers.append(buffer3)

        # Создаем PDF с несколькими страницами
        has_russian_font = register_russian_font()
        pdf_buffer = io.BytesIO()

        if has_russian_font:
            font_normal = "RussianFont"
            font_bold = "RussianFont"
        else:
            font_normal = "Helvetica"
            font_bold = "Helvetica-Bold"

        pdf = canvas.Canvas(pdf_buffer, pagesize=A4)
        width, height = A4

        for i, chart_buffer in enumerate(chart_buffers):
            # Заголовок
            pdf.setFont(font_bold, 16)
            pdf.drawString(50, height - 50, "Диаграммы статистики")
            pdf.setFont(font_normal, 10)
            pdf.drawString(50, height - 70, f"Всего записей: {stats_data['total_count']}")
            pdf.drawString(350, height - 70, f"Страница {i + 1}/{len(chart_buffers)}")
            pdf.drawString(450, height - 70, f"Дата: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

            # Получаем изображение диаграммы
            img = ImageReader(chart_buffer)
            img_width, img_height = img.getSize()

            # Масштабируем, чтобы не сплющить
            max_width = width * 0.85
            max_height = height * 0.7
            scale = min(max_width / img_width, max_height / img_height)
            new_width = img_width * scale
            new_height = img_height * scale

            # Центрируем
            x = (width - new_width) / 2
            y = height - new_height - 120
            pdf.drawImage(img, x, y, width=new_width, height=new_height)

            # Если не последняя страница — новая
            if i < len(chart_buffers) - 1:
                pdf.showPage()

        pdf.save()
        pdf_buffer.seek(0)

        # Закрываем все буферы
        for buffer in chart_buffers:
            buffer.close()

        return pdf_buffer

    except Exception as e:
        print(f"Error generating charts PDF: {e}")
        return None


def register_russian_font():
    try:
        # Пробуем найти русский шрифт в системе
        font_paths = [
            # Windows
            'C:/Windows/Fonts/arial.ttf',
            'C:/Windows/Fonts/times.ttf',
            # Linux
            '/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf',
            '/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf',
            # macOS
            '/Library/Fonts/Arial.ttf',
            '/System/Library/Fonts/Arial.ttf'
        ]

        for font_path in font_paths:
            if os.path.exists(font_path):
                pdfmetrics.registerFont(TTFont('RussianFont', font_path))
                return True
    except Exception as e:
        print(f"Font registration error: {e}")

    return False


def get_recommendations(type_: str, tmdb_id: int):
    url = f"https://api.themoviedb.org/3/{type_}/{tmdb_id}/recommendations"
    r = tmdb_get(url, {"language": "ru-RU", "page": 1})
    if r.status_code == 200:
        return r.json().get("results", [])
    return []


def kb_genres(type_: str):
    genres = GENRES_MOVIE if type_ == "movie" else GENRES_TV
    keyboard = []
    row = []
    for title, gid in genres.items():
        row.append(InlineKeyboardButton(text=title, callback_data=f"genre_{type_}_{gid}"))
        if len(row) == 3:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])
    return InlineKeyboardMarkup(inline_keyboard=keyboard)


def search_by_title(title: str, type_: str = None, page: int = 1):
    """Поиск контента по названию через TMDB API с проверкой банов"""
    url = "https://api.themoviedb.org/3/search/multi"
    params = {
        "query": title,
        "language": "ru-RU",
        "page": page,
        "include_adult": "false"
    }

    r = tmdb_get(url, params)
    if r.status_code == 200:
        data = r.json()
        results = data.get("results", [])
        total_pages = data.get("total_pages", 1)

        # Если нужно больше результатов, получаем дополнительные страницы
        if total_pages > 1 and page == 1:
            # Ограничим максимум 3 страницы (60 результатов)
            max_pages = min(total_pages, 3)
            for next_page in range(2, max_pages + 1):
                try:
                    params["page"] = next_page
                    next_r = tmdb_get(url, params)
                    if next_r.status_code == 200:
                        next_data = next_r.json()
                        results.extend(next_data.get("results", []))
                except Exception as e:
                    print(f"Error fetching page {next_page}: {e}")
                    break

        # Фильтруем по типу если указан
        if type_:
            results = [item for item in results if item.get("media_type") == type_]

        return results
    return []


def search_by_person(name: str):
    """Поиск актеров/режиссеров через TMDB API"""
    url = "https://api.themoviedb.org/3/search/person"
    params = {
        "query": name,
        "language": "ru-RU",
        "page": 1,
        "include_adult": "false"
    }

    print(f"DEBUG: Searching for person: {name}")  # Отладка

    r = tmdb_get(url, params)
    if r.status_code == 200:
        data = r.json()
        results = data.get("results", [])

        print(f"DEBUG: Found {len(results)} persons")  # Отладка

        # Получаем дополнительные страницы если есть
        total_pages = data.get("total_pages", 1)
        if total_pages > 1:
            max_pages = min(total_pages, 3)
            for next_page in range(2, max_pages + 1):
                try:
                    params["page"] = next_page
                    next_r = tmdb_get(url, params)
                    if next_r.status_code == 200:
                        next_data = next_r.json()
                        results.extend(next_data.get("results", []))
                except Exception as e:
                    print(f"Error fetching person page {next_page}: {e}")
                    break

        print(f"DEBUG: Total persons after pagination: {len(results)}")  # Отладка
        return results

    print(f"DEBUG: TMDB API error: {r.status_code}")  # Отладка
    return []


async def get_person_filmography(person_id: int):
    """Получает фильмографию актера с режиссерскими работами и умной фильтрацией"""
    url = f"https://api.themoviedb.org/3/person/{person_id}/combined_credits"
    params = {
        "language": "ru-RU"
    }

    print(f"DEBUG: Getting filmography for person_id: {person_id}")

    r = tmdb_get(url, params)
    if r.status_code == 200:
        data = r.json()
        cast = data.get("cast", [])
        crew = data.get("crew", [])

        print(f"DEBUG: Raw cast count: {len(cast)}, crew count: {len(crew)}")

        # Жанры для исключения (телешоу, ток-шоу, новости, реалити)
        EXCLUDED_GENRES = {10764, 10767, 10763, 10764}  # Reality, Talk, News, Reality

        # Ключевые слова в названиях для исключения
        EXCLUDED_KEYWORDS = [
            # Телешоу и программы
            "show", "шоу", "поздней ночью", "night show", "tonight show", "late night",
            "утро", "morning", "вечер", "evening", "talk", "ток-шоу", "интервью",
            "interview", "news", "новости", "wwe", "raw", "snl", "субботним вечером",
            "saturday night live", "джимми", "jimmy", "кimmel", "киммел", "фэллон", "fallon",

            # Церемонии и премии
            "золотой глобус", "golden globe", "церемония вручения премии", "awards",
            "премия", "award", "oscar", "оскар", "grammy", "грэмми", "emmy", "эмми",
            "ceremony", "церемония", "награждение", "red carpet", "красная дорожка",
            "met gala", "мет гала", "bafta", "британская академия", "canne", "канны",
            "venice", "венеция", "berlinale", "берлинале", "sundance", "санденс",
            "mtv movie", "mtv music", "vma", "billboard", "биллборд"
        ]

        filmography_dict = {}

        def should_exclude_item(item):
            """Проверяет, нужно ли исключить элемент из фильмографии"""
            media_type = item.get("media_type")
            title = (item.get("title") or item.get("name") or "").lower()

            # Проверяем по жанрам
            genre_ids = set(item.get("genre_ids", []))
            if genre_ids & EXCLUDED_GENRES:
                return True

            # Проверяем по ключевым словам в названии
            if any(keyword in title for keyword in EXCLUDED_KEYWORDS):
                return True

            return False

        # Обрабатываем актерские работы
        for item in cast:
            media_type = item.get("media_type")

            # Проверяем исключения по жанрам и ключевым словам
            if should_exclude_item(item):
                print(f"DEBUG: Excluding actor item by filter: {item.get('title') or item.get('name')}")
                continue

            # Для сериалов применяем строгую фильтрацию
            if media_type == "tv":
                # ИСКЛЮЧАЕМ сериалы где человек снимался только в 1 эпизоде
                episode_count = item.get("episode_count", 0)
                if episode_count <= 1:
                    print(f"DEBUG: Skipping TV show with only {episode_count} episodes: {item.get('name')}")
                    continue

                # Дополнительная проверка: исключаем эпизодические появления
                character = item.get("character", "").lower()
                if any(keyword in character for keyword in ["himself", "себя", "guest", "эпизод", "cameo", "камео"]):
                    print(f"DEBUG: Skipping guest appearance: {item.get('name')} as {character}")
                    continue

            # Для фильмов берем все роли (даже эпизодические)
            elif media_type == "movie":
                # Все фильмы включаем
                pass
            else:
                continue  # Пропускаем другие типы медиа

            item_id = item.get("id")
            if item_id not in filmography_dict:
                filmography_dict[item_id] = {
                    "id": item_id,
                    "media_type": media_type,
                    "title": item.get("title") or item.get("name"),
                    "release_date": item.get("release_date") or item.get("first_air_date"),
                    "popularity": item.get("popularity", 0),
                    "poster_path": item.get("poster_path"),
                    "episode_count": item.get("episode_count", 0),
                    "character": item.get("character", ""),
                    "genre_ids": item.get("genre_ids", []),
                    "roles": set()
                }

            filmography_dict[item_id]["roles"].add("actor")

        # Обрабатываем режиссерские работы с УМНОЙ ФИЛЬТРАЦИЕЙ
        for item in crew:
            media_type = item.get("media_type")
            job = item.get("job", "").lower()
            department = item.get("department", "").lower()

            # Берем только режиссеров из режиссерского департамента
            if department != "directing":
                continue

            # Только основные режиссерские должности
            if job not in ["director"]:
                continue

            if media_type not in ["movie", "tv"]:
                continue

            # Проверяем исключения по жанрам и ключевым словам
            if should_exclude_item(item):
                print(f"DEBUG: Excluding director item by filter: {item.get('title') or item.get('name')}")
                continue

            # ДОПОЛНИТЕЛЬНАЯ ФИЛЬТРАЦИЯ ДЛЯ РЕЖИССЕРОВ:
            # Для сериалов - только если это не эпизодическая режиссура
            if media_type == "tv":
                # Получаем детали сериала для проверки
                series_details = get_item_details("tv", item.get("id"))
                if series_details:
                    # Проверяем создателей сериала
                    created_by = series_details.get("created_by", [])
                    creator_ids = [creator.get("id") for creator in created_by]

                    # Если человек не создатель и сериал имеет много сезонов - возможно это режиссер эпизода
                    if person_id not in creator_ids:
                        number_of_seasons = series_details.get("number_of_seasons", 0)
                        if number_of_seasons > 3:  # Популярный долгоиграющий сериал
                            print(f"DEBUG: Skipping episode director in popular series: {item.get('name')}")
                            continue

            item_id = item.get("id")
            if item_id not in filmography_dict:
                filmography_dict[item_id] = {
                    "id": item_id,
                    "media_type": media_type,
                    "title": item.get("title") or item.get("name"),
                    "release_date": item.get("release_date") or item.get("first_air_date"),
                    "popularity": item.get("popularity", 0),
                    "poster_path": item.get("poster_path"),
                    "episode_count": item.get("episode_count", 0),
                    "genre_ids": item.get("genre_ids", []),
                    "roles": set()
                }

            filmography_dict[item_id]["roles"].add("director")

        # Преобразуем словарь обратно в список
        filmography = []
        for item_data in filmography_dict.values():
            item_data["person_role"] = list(item_data["roles"])
            filmography.append(item_data)

        print(f"DEBUG: Final filmography count (with directors): {len(filmography)}")

        # Выводим отладочную информацию
        for i, item in enumerate(filmography[:10]):
            title = item.get("title", "No title")
            media_type = item.get("media_type")
            roles = item.get("person_role", [])
            print(f"DEBUG: Filmography item {i}: {title} ({media_type}) - Roles: {roles}")

        # ФИЛЬТРАЦИЯ ЗАБАНЕННОГО КОНТЕНТА
        async def filter_banned_filmography(items):
            filtered_items = []
            for item in items:
                media_type = item.get("media_type")
                if not await is_banned(item["id"], media_type):
                    filtered_items.append(item)
                else:
                    print(f"DEBUG: Excluding banned content from filmography: {item.get('title')} (ID: {item['id']})")
            return filtered_items

        # Применяем фильтрацию банов
        filmography = await filter_banned_filmography(filmography)

        print(f"DEBUG: Final filmography count (with ban filter): {len(filmography)}")

        # Сортируем по популярности
        filmography.sort(key=lambda x: (
            x.get("popularity", 0),
            x.get("release_date") or "0000-00-00"
        ), reverse=True)

        return filmography

    print(f"DEBUG: TMDB API error: {r.status_code}")
    return []  # ВАЖНО: возвращаем пустой список при ошибке

def format_banned_page(banned_list: list, page: int, items_per_page: int = 15):
    """Форматирует страницу списка банов"""
    total_items = len(banned_list)
    total_pages = (total_items + items_per_page - 1) // items_per_page
    start_idx = page * items_per_page
    end_idx = min(start_idx + items_per_page, total_items)

    text = f"📋 Забаненный контент (страница {page + 1}/{total_pages}):\n\n"

    for i in range(start_idx, end_idx):
        item = banned_list[i]
        text += f"• {item['title']} (ID: {item['tmdb_id']}, {item['type']})\n"

    text += f"\nВсего: {total_items}"
    return {"text": text}


def format_stats_page(stats_data: dict, sort_by: str, page: int):
    """Форматирует страницу статистики"""
    items = stats_data["items"]
    total_count = stats_data["total_count"]
    total_pages = stats_data["total_pages"]

    # Описание сортировки
    sort_descriptions = {
        "updated": "🕐 по дате обновления",
        "likes": "👍 по лайкам",
        "dislikes": "👎 по дизлайкам",
        "watches": "👀 по просмотрам"
    }

    text = f"📊 <b>Статистика контента</b>\n"
    text += f"📈 Сортировка: {sort_descriptions.get(sort_by, 'по дате')}\n"
    text += f"📄 Страница: {page + 1}/{total_pages}\n"
    text += f"📋 Всего записей: {total_count}\n\n"

    if not items:
        text += "❌ Нет данных для отображения"
        return text

    for i, item in enumerate(items, start=page * len(items) + 1):
        title = item['title'] or "Без названия"
        media_type = "🎬 Фильм" if item['type'] == 'movie' else '📺 Сериал'

        text += f"<b>{i})</b> \"{title}\" - {media_type}, ID: {item['tmdb_id']}\n"
        text += f"   👍 Лайки: {item['likes']} | 👎 Дизлайки: {item['dislikes']} | 👀 Просмотров: {item['watches']}\n\n"

    return text


def kb_banned_pagination(banned_list: list, page: int, items_per_page: int = 15):
    """Клавиатура с пагинацией для списка банов"""
    total_items = len(banned_list)
    total_pages = (total_items + items_per_page - 1) // items_per_page

    keyboard = []

    # Кнопки пагинации
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"ban_page_{page - 1}"))

    if page < total_pages - 1:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"ban_page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="⬅️ В админ-панель", callback_data="admin_panel")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def send_banned_page(chat_id: int, banned_list: list, page: int):
    """Отправляет страницу списка банов"""
    await bot.send_message(
        chat_id,
        **format_banned_page(banned_list, page),
        reply_markup=kb_banned_pagination(banned_list, page)
    )


def kb_search_results(results, search_query: str, page: int = 0, results_per_page: int = 10):
    """Клавиатура с результатами поиска с пагинацией"""
    total_results = len(results)
    start_idx = page * results_per_page
    end_idx = start_idx + results_per_page
    page_results = results[start_idx:end_idx]

    keyboard = []

    for item in page_results:
        media_type = item.get("media_type")
        title = item.get("title") or item.get("name")
        year = (item.get("release_date") or item.get("first_air_date") or "")[:4]

        if media_type in ["movie", "tv"]:
            btn_text = f"{'🎬' if media_type == 'movie' else '📺'} {title}"
            if year:
                btn_text += f" ({year})"

            keyboard.append([
                InlineKeyboardButton(
                    text=btn_text,
                    callback_data=f"admin_preban_{item['id']}_{media_type}"
                )
            ])

    # Добавляем пагинацию
    navigation_buttons = []
    if page > 0:
        navigation_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"admin_search_page_{page - 1}"))

    if end_idx < total_results:
        navigation_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"admin_search_page_{page + 1}"))

    if navigation_buttons:
        keyboard.append(navigation_buttons)

    keyboard.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="admin_search_ban")])
    keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")])

    return InlineKeyboardMarkup(inline_keyboard=keyboard)


async def send_search_results_page(chat_id: int, results: list, search_query: str, page: int,
                                   results_per_page: int = 10):
    total_results = len(results)
    start_idx = page * results_per_page
    end_idx = start_idx + results_per_page
    page_results = results[start_idx:end_idx]

    # СЧИТАЕМ ТОЛЬКО ОТОБРАЖАЕМЫЕ РЕЗУЛЬТАТЫ (movie/tv)
    displayable_results = []
    for item in page_results:
        media_type = item.get("media_type")
        if media_type in ["movie", "tv"]:
            displayable_results.append(item)

    actual_display_count = len(displayable_results)
    total_pages = (actual_display_count + results_per_page - 1) // results_per_page

    text = f"🔍 Найдено {actual_display_count} результатов по запросу: '{search_query}'\nСтраница {page + 1}/{max(total_pages, 1)}\n\nВыберите:"

    keyboard = []

    for item in displayable_results:
        media_type = item.get("media_type")
        title = item.get("title") or item.get("name")
        year = (item.get("release_date") or item.get("first_air_date") or "")[:4]

        btn_text = f"{'🎬' if media_type == 'movie' else '📺'} {title}"
        if year:
            btn_text += f" ({year})"

        keyboard.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"select_{item['id']}_{media_type}"
            )
        ])

    # Пагинация (если есть больше отображаемых результатов)
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"search_page_{page - 1}"))
    if actual_display_count == results_per_page and (page + 1) * results_per_page < total_results:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"search_page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="search_by_title")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])

    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


async def cancel_friend_request(from_tg_id: int, to_tg_id: int):
    """Отменяет исходящую заявку в друзья"""
    async with state.db.acquire() as conn:
        from_user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", from_tg_id)
        to_user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", to_tg_id)

        if not from_user or not to_user:
            return False

        result = await conn.execute("""
            DELETE FROM friend_requests 
            WHERE from_user_id = $1 AND to_user_id = $2 AND status = 'pending'
        """, from_user["user_id"], to_user["user_id"])

        return result != "DELETE 0"


async def get_outgoing_friend_requests(tg_id: int):
    """Получает исходящие заявки пользователя"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return []

        rows = await conn.fetch("""
            SELECT 
                fr.request_id,
                fr.created_at,
                u.tg_id,
                u.username
            FROM friend_requests fr
            JOIN users u ON fr.to_user_id = u.user_id
            WHERE fr.from_user_id = $1 AND fr.status = 'pending'
            ORDER BY fr.created_at DESC
        """, user["user_id"])

        return rows

# -------------------- HANDLERS --------------------
