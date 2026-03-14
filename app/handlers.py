from datetime import datetime, date, timedelta
import asyncio
import csv
import inspect
import io
import math
import os
import random
from urllib.parse import urlparse

import aiohttp
import requests
from aiogram import BaseMiddleware, types
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, BotCommand

from . import state
from .state import user_sessions, user_filters, user_input_waiting
from .config import bot, dp
from .support import *

def require_agreement_message(handler):
    """Декоратор для проверки соглашения в обработчиках сообщений"""

    async def wrapper(message: types.Message, *args, **kwargs):
        chat_id = message.chat.id

        # Проверяем принятие соглашения
        user = await get_user_by_tg_id(chat_id)
        if not user or not user.get('agreement_accepted'):
            await message.answer(
                "❌ Для использования бота необходимо принять пользовательское соглашение. Отправьте /start"
            )
            return

        # Фильтруем kwargs, оставляя только нужные параметры
        import inspect
        sig = inspect.signature(handler)
        valid_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

        return await handler(message, *args, **valid_kwargs)

    return wrapper


def require_agreement_callback(handler):
    """Декоратор для проверки соглашения в обработчиках callback'ов"""

    async def wrapper(callback: types.CallbackQuery, *args, **kwargs):
        chat_id = callback.message.chat.id

        # Проверяем принятие соглашения
        user = await get_user_by_tg_id(chat_id)
        if not user or not user.get('agreement_accepted'):
            await callback.answer(
                "❌ Для использования бота необходимо принять пользовательское соглашение. Отправьте /start",
                show_alert=True
            )
            return

        # Фильтруем kwargs, оставляя только нужные параметры
        import inspect
        sig = inspect.signature(handler)
        valid_kwargs = {k: v for k, v in kwargs.items() if k in sig.parameters}

        return await handler(callback, *args, **valid_kwargs)

    return wrapper

@dp.message(Command("search"))
@require_agreement_message
async def search_by_tmdb_id(message: types.Message):
    args = message.text.split()
    if len(args) < 3:
        await message.answer("❌ Укажите тип (movie/tv) и TMDB ID. Пример: /search movie 12345")
        return
    type_, tmdb_id = args[1], args[2]
    try:
        tmdb_id = int(tmdb_id)
    except ValueError:
        await message.answer("❌ Некорректный TMDB ID. Укажите число.")
        return
    if type_ not in ["movie", "tv"]:
        await message.answer("❌ Некорректный тип. Укажите movie или tv.")
        return
    details = get_item_details(type_, tmdb_id)

    if await is_banned(tmdb_id, type_):
        await message.answer("❌ Этот контент заблокирован администратором и недоступен для просмотра.")
        return

    if not details:
        await message.answer("❌ Не удалось найти фильм или сериал с таким TMDB ID.")
        return
    title = details.get("title") or details.get("name") or "Без названия"
    year = (details.get("release_date") or details.get("first_air_date") or "")[:4]
    rating = details.get("vote_average") or "—"
    overview = details.get("overview") or "Описание отсутствует."
    if len(overview) > 2000:
        overview = overview[:2000] + "..."
    poster = f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get("poster_path") else None
    avg_ratings = await get_ratings(tmdb_id, type_)
    watched_text = ""
    user_rating = await get_user_rating(message.chat.id, tmdb_id, type_)
    if user_rating and user_rating["watched"]:
        watched_text = "✅ Вы смотрели"

    def create_safe_caption(title, year, rating, avg_ratings, watched_text, overview):
        """Создает caption гарантированно не длиннее 1024 символов"""
        base_info = f"{title} ({year})\n⭐ {rating} | 👍 {avg_ratings['likes']} | 👎 {avg_ratings['dislikes']} | 👀 {avg_ratings['watches']}"
        if watched_text:
            base_info += f"\n{watched_text}"

        base_info += "\n\n"

        # Максимальная длина для Telegram caption
        max_total = 1024
        available = max_total - len(base_info) - 3  # -3 для "..."

        if available <= 50:  # Если почти нет места для описания
            return base_info.strip()

        if len(overview) > available:
            overview = overview[:available] + "..."

        return base_info + overview

    # Используйте так:
    caption = create_safe_caption(title, year, rating, avg_ratings, watched_text, overview)

    # Добавляем информацию о провайдерах для РФ (если есть)
    try:
        prov = get_providers_ru(type_, tmdb_id)
        if prov:
            prov_text = f"\n\nГде смотреть: {prov}"
            if len(caption) + len(prov_text) <= 1024:
                caption += prov_text
    except Exception:
        pass
    if poster:
        await message.answer_photo(photo=poster, caption=caption, reply_markup=await kb_card(message.chat.id, tmdb_id, type_))
    else:
        await message.answer(text=caption, reply_markup=await kb_card(message.chat.id, tmdb_id, type_))


@dp.message(Command("subscription"))
@require_agreement_message
async def subscription_info(message: types.Message):
    """Информация о подписке пользователя"""
    chat_id = message.chat.id
    requests_info = await get_requests_info(chat_id)

    if requests_info["has_subscription"]:
        text = (
            f"🌟 <b>У вас активна подписка!</b>\n\n"
            f"⏰ Дней осталось: {requests_info['days_left']}\n"
            f"📅 Истекает: {(datetime.now() + timedelta(days=requests_info['days_left'])).strftime('%d.%m.%Y')}\n"
            f"🔓 Лимит запросов: Безлимит\n"
            f"💫 Статус: Активна\n\n"
            f"<i>Подписка позволяет использовать бота без ограничений!</i>"
        )
    else:
        text = (
            f"📊 <b>Информация о подписке</b>\n\n"
            f"🔒 Статус: Нет подписки\n"
            f"📈 Использовано запросов: {requests_info['today_requests']}/{requests_info['max_requests']}\n"
            f"🎯 Осталось: {requests_info['remaining']}\n\n"
            f"<i>Подписка дает:</i>\n"
            f"• ♾️ Безлимитные запросы\n"
            f"• ♾️ <b>Безлимитные запросы</b> - ищите сколько угодно\n"
            f"• ⚡ <b>Расширенные функции</b> - все возможности бота\n\n"
            f"<i>Для получения подписки обратитесь к администратору</i>"
        )

    await message.answer(
        text,
        parse_mode="HTML",
        reply_markup=kb_subscription_management(
            requests_info["has_subscription"],
            requests_info.get("days_left", 0)
        )
    )


@dp.message(Command("activate_sub"))
@require_agreement_message
async def activate_subscription_command(message: types.Message):
    """Активация подписки (только для админов)"""
    if not is_admin(message.chat.id):
        await message.answer("❌ Нет доступа к этой команде!")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажите ID пользователя. Пример: /activate_sub 123456789")
        return

    try:
        user_tg_id = int(args[1])
        days = int(args[2]) if len(args) > 2 else 30

        success = await activate_subscription(user_tg_id, days)
        if success:
            await message.answer(f"✅ Подписка активирована для пользователя {user_tg_id} на {days} дней!")

            # Уведомляем пользователя
            try:
                await bot.send_message(
                    user_tg_id,
                    f"🎉 Вам активирована подписка на {days} дней!\n\n"
                    f"Теперь вы можете использовать бота без ограничений! 🚀"
                )
            except:
                pass
        else:
            await message.answer("❌ Пользователь не найден!")

    except ValueError:
        await message.answer("❌ Некорректный ID пользователя!")


@dp.message(Command("deactivate_sub"))
@require_agreement_message
async def deactivate_subscription_command(message: types.Message):
    """Деактивация подписки (только для админов)"""
    if not is_admin(message.chat.id):
        await message.answer("❌ Нет доступа к этой команде!")
        return

    args = message.text.split()
    if len(args) < 2:
        await message.answer("❌ Укажите ID пользователя. Пример: /deactivate_sub 123456789")
        return

    try:
        user_tg_id = int(args[1])

        success = await deactivate_subscription(user_tg_id)
        if success:
            await message.answer(f"✅ Подписка деактивирована для пользователя {user_tg_id}!")
        else:
            await message.answer("❌ Пользователь не найден!")

    except ValueError:
        await message.answer("❌ Некорректный ID пользователя!")

@dp.message(Command("unban"))
@require_agreement_message
async def unban_command(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("❌ Нет доступа к админ-панели!")
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer("❌ Укажите тип (movie/tv) и ID. Пример: /unban movie 12345")
        return

    type_ = args[1].lower()
    tmdb_id_str = args[2]

    if type_ not in ["movie", "tv"]:
        await message.answer("❌ Некорректный тип. Укажите movie или tv.")
        return

    try:
        tmdb_id = int(tmdb_id_str)
    except ValueError:
        await message.answer("❌ Некорректный ID. Укажите число.")
        return

    # Проверяем, существует ли такой бан
    if not await is_banned(tmdb_id, type_):
        await message.answer("❌ Этот контент не забанен.")
        return

    # Убираем из бана
    await unban_content(tmdb_id, type_)
    await message.answer(f"✅ Контент {type_} с ID {tmdb_id} разбанен!")

@dp.message(Command("ban"))  # Команда /ban
@require_agreement_message
async def ban_command(message: types.Message):  # Название функции должно быть ban_command
    if not is_admin(message.chat.id):
        await message.answer("❌ Нет доступа к админ-панели!")
        return

    args = message.text.split()
    if len(args) < 3:
        await message.answer("❌ Укажите тип (movie/tv) и ID. Пример: /ban movie 12345")  # Исправил пример
        return

    type_ = args[1].lower()
    tmdb_id_str = args[2]

    if type_ not in ["movie", "tv"]:
        await message.answer("❌ Некорректный тип. Укажите movie или tv.")
        return

    try:
        tmdb_id = int(tmdb_id_str)
    except ValueError:
        await message.answer("❌ Некорректный ID. Укажите число.")
        return

    # Проверяем, не забанен ли уже
    if await is_banned(tmdb_id, type_):
        await message.answer("❌ Этот контент уже забанен.")
        return

    # Получаем название для бана
    details = get_item_details(type_, tmdb_id)
    title = details.get("title") or details.get("name") or "Unknown"

    # Баним контент
    await ban_content(tmdb_id, type_, title, message.chat.id, "Бан через команду")
    await message.answer(f"✅ Контент {type_} с ID {tmdb_id} забанен!")

@dp.message(Command("admin"))
@require_agreement_message
async def admin_command(message: types.Message):
    if not is_admin(message.chat.id):
        await message.answer("❌ Нет доступа к админ-панели!")
        return

    await message.answer("⚙️ Админ-панель:", reply_markup=kb_admin_panel())


@dp.message(Command("start"))
async def start(message: types.Message):
    chat_id = message.chat.id
    username = message.from_user.username

    await update_user_username(chat_id, username)

    # Проверяем, есть ли пользователь в базе и принял ли он соглашение
    user = await get_user_by_tg_id(chat_id)

    if not user:
        # Пользователя нет - показываем соглашение
        await show_user_agreement(message)
        return

    # Проверяем, принял ли пользователь соглашение
    agreement_accepted = await check_user_agreement(chat_id)
    if not agreement_accepted:
        # Пользователь есть, но соглашение не принял - показываем снова
        await show_user_agreement(message)
        return

    # Пользователь есть и соглашение принято - показываем главное меню
    await show_main_menu(message)


async def show_user_agreement(message: types.Message):
    """Показывает пользовательское соглашение"""
    chat_id = message.chat.id

    agreement_text = """
📋 *ПОЛЬЗОВАТЕЛЬСКОЕ СОГЛАШЕНИЕ*

Перед использованием бота пожалуйста ознакомьтесь с условиями:

1. *Цель бота*: Поиск и рекомендация фильмов и сериалов
2. *Использование данных*: Мы сохраняем ваши предпочтения и историю поиска для улучшения рекомендаций
3. *Ограничения*: Бесплатные пользователи имеют лимит запросов
4. *Контент*: Бот предоставляет информацию о фильмах и сериалах из открытых источников

*Нажимая "Принять", вы соглашаетесь с условиями использования.*
"""

    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="✅ Принять соглашение", callback_data="accept_agreement")],
        [InlineKeyboardButton(text="❌ Отклонить", callback_data="reject_agreement")]
    ])

    # ===== КЛЮЧЕВОЕ ИСПРАВЛЕНИЕ =====
    # Строим абсолютный путь относительно ЭТОГО файла, а не рабочей директории
    base_dir = os.path.dirname(os.path.abspath(__file__))
    file_path = os.path.join(base_dir, "user_agreementv2.pdf")

    # Диагностика — убрать после отладки
    print(f"[DEBUG] __file__    = {__file__}")
    print(f"[DEBUG] base_dir    = {base_dir}")
    print(f"[DEBUG] file_path   = {file_path}")
    print(f"[DEBUG] file exists = {os.path.exists(file_path)}")
    print(f"[DEBUG] cwd         = {os.getcwd()}")

    if not os.path.exists(file_path):
        print(f"[ERROR] Файл НЕ найден: {file_path}")
        # Покажем, что реально лежит в папке
        print(f"[DEBUG] Содержимое base_dir: {os.listdir(base_dir)}")
        await message.answer(
            agreement_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
        return

    try:
        agreement_file = types.FSInputFile(file_path, filename="user_agreementv2.pdf")
        await message.answer_document(
            document=agreement_file,
            caption=agreement_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )
    except Exception as e:
        # НЕ глотаем ошибку молча — логируем полностью
        print(f"[ERROR] Ошибка отправки файла: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        await message.answer(
            agreement_text,
            parse_mode="Markdown",
            reply_markup=keyboard
        )


async def show_main_menu(message: types.Message):
    """Показывает главное меню после принятия соглашения"""
    chat_id = message.chat.id
    filters = await get_user_filters(chat_id)
    user_filters[chat_id] = filters

    # Загружаем сохраненные фильтры поиска
    search_filters = await load_search_filters(chat_id)
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {}
    user_sessions[chat_id]["filters"] = search_filters

    requests_info = await get_requests_info(chat_id)

    if requests_info["has_subscription"]:
        text = (
            f"🎬 Добро пожаловать!\n\n"
            f"🌟 <b>У вас активна подписка!</b>\n"
            f"⏰ Дней осталось: {requests_info['days_left']}\n"
            f"🔓 Лимит запросов: Безлимит\n\n"
            f"Выберите действие:"
        )
    else:
        text = (
            f"🎬 Добро пожаловать!\n\n"
            f"📊 Статистика на сегодня:\n"
            f"• Использовано запросов: {requests_info['today_requests']}/{requests_info['max_requests']}\n"
            f"• Осталось запросов: {requests_info['remaining']}\n\n"
            f"Выберите действие:"
        )

    await message.answer(text, parse_mode="HTML", reply_markup=kb_main())

@dp.message(Command("myid"))
@require_agreement_message
async def get_my_id(message: types.Message):
    await message.answer(f"🆔 Ваш ID: `{message.chat.id}`\n\nПоделитесь этим ID с друзьями, чтобы они могли добавить вас в друзья!", parse_mode="Markdown")


@dp.message(lambda message: message.document and message.document.mime_type == 'text/csv')
async def handle_csv_import(message: types.Message):
    chat_id = message.chat.id

    if chat_id not in user_sessions or not user_sessions[chat_id].get("waiting_import_file"):
        return

    user_sessions[chat_id]["waiting_import_file"] = False

    await message.answer("🔄 Обрабатываю CSV файл...")

    try:
        # Скачиваем файл
        file = await bot.get_file(message.document.file_id)
        file_path = file.file_path

        # Скачиваем содержимое файла
        file_content = await bot.download_file(file_path)
        csv_content = file_content.read().decode('utf-8-sig')

        # Импортируем коллекцию
        result = await import_collection_from_csv(chat_id, csv_content)

        # Формируем отчет
        report_text = (
            f"📊 <b>Результат импорта</b>\n\n"
            f"✅ Импортировано: {result['imported']}\n"
            f"⏭️ Пропущено (уже в коллекции): {result['skipped']}\n"
        )

        if result['errors']:
            report_text += f"❌ Ошибок: {len(result['errors'])}\n\n"
            # Показываем только первые 5 ошибок
            for error in result['errors'][:5]:
                report_text += f"• {error}\n"
            if len(result['errors']) > 5:
                report_text += f"• ... и ещё {len(result['errors']) - 5} ошибок\n"

        await message.answer(report_text, parse_mode="HTML")

        # Возвращаемся к коллекции
        total_items = await get_collection_count(chat_id)
        total_pages = (total_items + 3) // 4
        keyboard = await kb_collection(chat_id, 0, total_pages)

        await message.answer(
            f"📚 Ваша коллекция ({total_items} элементов):",
            reply_markup=keyboard
        )

    except Exception as e:
        await message.answer(f"❌ Ошибка при импорте файла: {str(e)}")


@dp.message()
async def handle_user_input(message: types.Message):
    chat_id = message.chat.id
    user_input = message.text.strip()

    # Добавь в handle_user_input()
    if chat_id in user_sessions and user_sessions[chat_id].get("waiting_admin_search"):
        if not is_admin(chat_id):
            await message.answer("❌ Нет доступа!")
            return

        user_sessions[chat_id]["waiting_admin_search"] = False

        search_query = user_input
        type_filter = None

        # Парсим тип если указан
        if " movie" in search_query.lower():
            type_filter = "movie"
            search_query = search_query.replace(" movie", "").strip()
        elif " tv" in search_query.lower():
            type_filter = "tv"
            search_query = search_query.replace(" tv", "").strip()

        results = search_by_title(search_query, type_filter)
        user_sessions[chat_id]["search_results"] = results
        user_sessions[chat_id]["search_query"] = search_query

        if not results:
            await message.answer("❌ Ничего не найдено")
            await message.answer("⚙️ Админ-панель:", reply_markup=kb_admin_panel())
            return

        await message.answer(
            f"🔍 Найдено {len(results)} результатов по запросу: '{search_query}'\nСтраница 1",
            reply_markup=kb_search_results(results, search_query, page=0)
        )
        return

    if chat_id in user_sessions and user_sessions[chat_id].get("waiting_title_search"):
        user_sessions[chat_id]["waiting_title_search"] = False

        search_query = user_input

        # ПРОВЕРКА ЛИМИТА
        can_request, error_msg = await handle_search_request(chat_id, f"search_title_{search_query}")
        if not can_request:
            await message.answer(error_msg)
            await message.answer("Выберите тип поиска:", reply_markup=kb_search_menu())
            return

        results = search_by_title(search_query)

        if not results:
            await message.answer("❌ Ничего не найдено")
            await message.answer("Выберите тип поиска:", reply_markup=kb_search_menu())
            return

        # ФИЛЬТРАЦИЯ ЗАБАНЕННОГО КОНТЕНТА
        filtered_results = []
        for item in results:
            media_type = item.get("media_type")
            if media_type in ["movie", "tv"]:
                if not await is_banned(item["id"], media_type):
                    filtered_results.append(item)
            else:
                filtered_results.append(item)  # Для person и других типов не проверяем бан

        if not filtered_results:
            await message.answer("❌ Все найденные результаты заблокированы администратором")
            await message.answer("Выберите тип поиска:", reply_markup=kb_search_menu())
            return

        user_sessions[chat_id]["search_results"] = filtered_results
        user_sessions[chat_id]["search_query"] = search_query
        user_sessions[chat_id]["search_page"] = 0

        await send_search_results_page(chat_id, filtered_results, search_query, 0)
        return

    if chat_id in user_sessions and user_sessions[chat_id].get("waiting_friend_id"):
        user_sessions[chat_id]["waiting_friend_id"] = False

        try:
            friend_tg_id = int(user_input)
            if friend_tg_id == chat_id:
                await message.answer("❌ Нельзя добавить самого себя в друзья!")
            else:
                result = await send_friend_request(chat_id, friend_tg_id)
                if result is True:
                    await message.answer("✅ Заявка в друзья отправлена!")
                    # Уведомляем друга
                    try:
                        await bot.send_message(
                            friend_tg_id,
                            f"👋 Вам пришла заявка в друзья от пользователя!",
                            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                                [InlineKeyboardButton(text="📨 Посмотреть заявки", callback_data="friend_requests")],
                            ])
                        )
                    except:
                        pass  # Если бот не может написать пользователю
                elif result == "already_sent":
                    await message.answer("❌ Вы уже отправили заявку этому пользователю")
                else:
                    await message.answer("❌ Пользователь не найден")

        except ValueError:
            await message.answer("❌ Введите корректный числовой ID")

        await message.answer("👥 Система друзей:", reply_markup=kb_friends_menu())
        return

    # Обработка контактов
    if message.contact:
        contact = message.contact
        # Здесь можно обработать контакт, если нужно
        await message.answer("📱 Функция контактов в разработке. Используйте ID для добавления друзей.")
        return

    # Обработка ввода страны
    if chat_id in user_input_waiting and user_input_waiting[chat_id].get("waiting_country"):
        # Удаляем сообщение с инструкцией
        try:
            await bot.delete_message(chat_id, user_input_waiting[chat_id]["message_id"])
        except:
            pass

        if user_input.lower() == 'any':
            if chat_id in user_sessions and "filters" in user_sessions[chat_id]:
                user_sessions[chat_id]["filters"]["country"] = None
                await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
            await message.answer("✅ Фильтр страны убран")
        else:
            if chat_id not in user_sessions:
                user_sessions[chat_id] = {}
            if "filters" not in user_sessions[chat_id]:
                user_sessions[chat_id]["filters"] = {}

            user_sessions[chat_id]["filters"]["country"] = user_input.upper()
            await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
            await message.answer(f"✅ Страна установлена: {user_input}")

        user_input_waiting[chat_id]["waiting_country"] = False
        current_filters = user_sessions.get(chat_id, {}).get("filters", {})
        await message.answer("Настройте фильтры поиска:", reply_markup=kb_filters_menu(current_filters))
        return

    # Обработка ввода года/диапазона
    if chat_id in user_input_waiting and user_input_waiting[chat_id].get("waiting_year"):
        # Удаляем сообщение с инструкцией
        try:
            await bot.delete_message(chat_id, user_input_waiting[chat_id]["message_id"])
        except:
            pass

        if user_input.lower() == 'any':
            # Убираем фильтр года
            if chat_id in user_sessions and "filters" in user_sessions[chat_id]:
                user_sessions[chat_id]["filters"]["year"] = None
                user_sessions[chat_id]["filters"]["start_year"] = None
                user_sessions[chat_id]["filters"]["end_year"] = None
                await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
            await message.answer("✅ Фильтр года убран")
        else:
            # Парсим ввод
            current_year = 2025

            try:
                if '-' in user_input:
                    parts = user_input.split('-')
                    if len(parts) == 2:
                        start_part = parts[0].strip()
                        end_part = parts[1].strip()

                        if start_part and end_part:  # Диапазон: 2010-2020
                            start_year = int(start_part)
                            end_year = int(end_part)
                            if 1920 <= start_year <= current_year and 1920 <= end_year <= current_year and start_year <= end_year:
                                user_sessions[chat_id]["filters"]["start_year"] = start_year
                                user_sessions[chat_id]["filters"]["end_year"] = end_year
                                await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
                                await message.answer(f"✅ Диапазон установлен: {start_year}-{end_year}")
                            else:
                                await message.answer("❌ Некорректный диапазон. Используйте года от 1920 до 2024")
                                return

                        elif start_part and not end_part:  # От года: 2010-
                            start_year = int(start_part)
                            if 1920 <= start_year <= current_year:
                                user_sessions[chat_id]["filters"]["start_year"] = start_year
                                user_sessions[chat_id]["filters"]["end_year"] = current_year
                                await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
                                await message.answer(f"✅ Установлено: с {start_year} года")
                            else:
                                await message.answer("❌ Некорректный год. Используйте года от 1920 до 2024")
                                return

                        elif not start_part and end_part:  # До года: -2020
                            end_year = int(end_part)
                            if 1920 <= end_year <= current_year:
                                user_sessions[chat_id]["filters"]["start_year"] = 1920
                                user_sessions[chat_id]["filters"]["end_year"] = end_year
                                await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
                                await message.answer(f"✅ Установлено: до {end_year} года")
                            else:
                                await message.answer("❌ Некорректный год. Используйте года от 1920 до 2024")
                                return
                        else:
                            await message.answer("❌ Некорректный формат. Примеры: 2010, 2010-2020, 2010-, -2020")
                            return
                    else:
                        await message.answer("❌ Некорректный формат. Используйте один дефис")
                        return
                else:
                    # Один год: 2010
                    year = int(user_input)
                    if 1920 <= year <= current_year:
                        user_sessions[chat_id]["filters"]["start_year"] = year
                        user_sessions[chat_id]["filters"]["end_year"] = year
                        await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
                        await message.answer(f"✅ Год установлен: {year}")
                    else:
                        await message.answer("❌ Некорректный год. Используйте года от 1920 до 2024")
                        return

            except ValueError:
                await message.answer("❌ Введите числа для года. Примеры: 2010, 2010-2020")
                return

        # Убираем флаг ожидания
        user_input_waiting[chat_id]["waiting_year"] = False

        # Показываем обновленное меню фильтров
        current_filters = user_sessions.get(chat_id, {}).get("filters", {})
        await message.answer("Настройте фильтры поиска:", reply_markup=kb_filters_menu(current_filters))
        return

        # В handle_user_input после других обработчиков поиска добавьте:
    if chat_id in user_sessions and user_sessions[chat_id].get("waiting_person_search"):
        user_sessions[chat_id]["waiting_person_search"] = False

        search_query = user_input

        # ПРОВЕРКА ЛИМИТА
        can_request, error_msg = await handle_search_request(chat_id, f"search_person_{search_query}")
        if not can_request:
            await message.answer(error_msg)
            await message.answer("Выберите тип поиска:", reply_markup=kb_search_menu())
            return

        results = search_by_person(search_query)

        if not results:
            await message.answer("❌ Ничего не найдено")
            await message.answer("Выберите тип поиска:", reply_markup=kb_search_menu())
            return

        # Сохраняем результаты в сессию
        user_sessions[chat_id]["person_results"] = results
        user_sessions[chat_id]["person_query"] = search_query
        user_sessions[chat_id]["person_page"] = 0

        await send_person_results_page(chat_id, results, search_query, 0)
        return

    if chat_id in user_sessions and user_sessions[chat_id].get("waiting_subscription_days"):
        user_sessions[chat_id]["waiting_subscription_days"] = False
        target_tg_id = user_sessions[chat_id].get("target_user_tg_id")

        try:
            days = int(user_input)
            if days <= 0:
                await message.answer("❌ Количество дней должно быть положительным числом!")
                return

            success = await activate_subscription(target_tg_id, days)

            if success:
                # Уведомляем пользователя
                try:
                    await bot.send_message(
                        target_tg_id,
                        f"🎉 Вам выдана подписка на {days} дней!\n\n"
                        f"Теперь вы можете использовать бота без ограничений! 🚀"
                    )
                except:
                    pass

                await message.answer(f"✅ Подписка на {days} дней выдана пользователю {target_tg_id}!")

                # Возвращаемся к профилю пользователя
                user = await get_user_by_tg_id(target_tg_id)
                username = user['username'] or f"Пользователь {target_tg_id}"

                # Проверяем реальный статус подписки
                subscription = await get_user_subscription(target_tg_id)
                if subscription:
                    days_left = subscription.get('days_left', -1)
                    if days_left >= 0:
                        subscription_status = f"🌟 Активна\n⏰ Осталось дней: {days_left}"
                        has_active_sub = True
                    else:
                        subscription_status = "❌ Нет подписки"
                        has_active_sub = False
                else:
                    subscription_status = "❌ Нет подписки"
                    has_active_sub = False

                text = (
                    f"👤 Профиль пользователя\n\n"
                    f"🆔 ID: {target_tg_id}\n"
                    f"📛 Имя: @{username}\n"
                    f"💫 Подписка: {subscription_status}"
                )

                await message.answer(
                    text,
                    reply_markup=kb_user_management(target_tg_id, has_active_sub, days_left if has_active_sub else -1)
                )
            else:
                await message.answer("❌ Ошибка при выдаче подписки!")

        except ValueError:
            await message.answer("❌ Введите корректное число дней!")
        return

        # Обработка продления подписки
    if chat_id in user_sessions and user_sessions[chat_id].get("waiting_subscription_extend"):
        user_sessions[chat_id]["waiting_subscription_extend"] = False
        target_tg_id = user_sessions[chat_id].get("target_user_tg_id")

        try:
            days = int(user_input)
            if days <= 0:
                await message.answer("❌ Количество дней должно быть положительным числом!")
                return

            # Получаем текущую подписку
            user = await get_user_by_tg_id(target_tg_id)
            if not user or not user['is_active']:
                await message.answer("❌ У пользователя нет активной подписки!")
                return

            # Продлеваем подписку
            current_expires = user['expires_at']
            new_expires = current_expires + timedelta(days=days)

            async with state.db.acquire() as conn:
                user_db = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", target_tg_id)
                if user_db:
                    await conn.execute("""
                            UPDATE user_subscriptions 
                            SET expires_at = $1, updated_at = NOW()
                            WHERE user_id = $2 AND is_active = TRUE
                        """, new_expires, user_db["user_id"])

            # Уведомляем пользователя
            try:
                await bot.send_message(
                    target_tg_id,
                    f"📅 Ваша подписка продлена на {days} дней!\n\n"
                    f"Новая дата истечения: {new_expires.strftime('%d.%m.%Y')}"
                )
            except:
                pass

            await message.answer(f"✅ Подписка пользователя {target_tg_id} продлена на {days} дней!")

            # Обновляем информацию
            user = await get_user_by_tg_id(target_tg_id)
            username = user['username'] or f"Пользователь {target_tg_id}"
            expires_at = user['expires_at']
            days_left = (expires_at - datetime.now()).days

            text = (
                f"👤 Профиль пользователя\n\n"
                f"🆔 ID: {target_tg_id}\n"
                f"📛 Имя: @{username}\n"
                f"💫 Подписка: 🌟 Активна\n⏰ Осталось дней: {days_left}"
            )

            await message.answer(
                text,
                reply_markup=kb_user_management(target_tg_id, True, days_left)
            )

        except ValueError:
            await message.answer("❌ Введите корректное число дней!")
        return

    if chat_id in user_sessions and user_sessions[chat_id].get("waiting_user_search"):
        user_sessions[chat_id]["waiting_user_search"] = False

        try:
            search_tg_id = int(user_input)
            user = await get_user_by_tg_id(search_tg_id)

            if not user:
                await message.answer("❌ Пользователь не найден!")
                await message.answer("🌟 Управление подписками:", reply_markup=kb_admin_subscriptions_management())
                return

            username = user['username'] or f"Пользователь {search_tg_id}"
            has_subscription = user['is_active']

            if has_subscription:
                expires_at = user['expires_at']
                days_left = (expires_at - datetime.now()).days
                subscription_info = f"🌟 Активна\n⏰ Осталось дней: {days_left}"
            else:
                subscription_info = "❌ Нет подписки"

            text = (
                f"👤 Найден пользователь\n\n"
                f"🆔 ID: {search_tg_id}\n"
                f"📛 Имя: @{username}\n"
                f"💫 Подписка: {subscription_info}"
            )

            await message.answer(
                text,
                reply_markup=kb_user_management(search_tg_id, has_subscription, days_left if has_subscription else 0)
            )

        except ValueError:
            await message.answer("❌ Введите корректный числовой ID!")
            await message.answer("🌟 Управление подписками:", reply_markup=kb_admin_subscriptions_management())
        return

# В обработчиках поиска добавляем проверку:
@dp.callback_query(lambda c: c.data in ("discover_movie", "discover_tv"))
async def handle_discover(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    type_ = "movie" if callback.data == "discover_movie" else "tv"

    # ПРОВЕРКА ЛИМИТА
    can_request, error_msg = await handle_search_request(chat_id, f"discover_{type_}")
    if not can_request:
        await callback.answer(error_msg, show_alert=True)
        return

    # Получаем активные фильтры пользователя
    current_filters = await get_current_filters(chat_id)

    items = await discover_tmdb(type_, filters=current_filters)
    if user_filters.get(chat_id, {}).get("exclude_watched"):
        items = await filter_watched_items(chat_id, items, type_)
    if not items:
        await callback.message.answer("Не удалось получить данные.")
        return
    user_sessions[chat_id] = {
        "results": items,
        "index": 0,
        "type": type_,
        "mode": "random"
    }
    await send_card(chat_id, callback.message.message_id)

# В главном меню можно показывать остаток запросов
@dp.callback_query(lambda c: c.data == "back_to_main")
async def back_to_main_handler(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    requests_info = await get_requests_info(chat_id)

    if requests_info["has_subscription"]:
        text = (f"Выберите действие:")
    else:
        text = (
            f"📊 Статистика на сегодня:\n"
            f"• Использовано запросов: {requests_info['today_requests']}/{requests_info['max_requests']}\n"
            f"• Осталось запросов: {requests_info['remaining']}\n\n"
            f"Выберите действие:"
        )

    try:
        await bot.delete_message(chat_id, callback.message.message_id)
    except Exception:
        pass

    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=kb_main())


@dp.callback_query(lambda c: c.data == "my_profile")
@require_agreement_callback
async def my_profile_handler(callback: types.CallbackQuery):
    """Обработчик кнопки Мой профиль"""
    chat_id = callback.message.chat.id

    # Получаем обновленные данные
    user = await get_user_by_tg_id(chat_id)
    requests_info = await get_requests_info(chat_id)

    username_display = user['username'] or "не установлен"
    registration_date = user.get('agreement_accepted_at', 'N/A')

    if isinstance(registration_date, datetime):
        registration_date = registration_date.strftime("%d.%m.%Y %H:%M")

    # Статистика коллекции
    collection_count = await get_collection_count(chat_id)

    # Статистика оценок
    async with state.db.acquire() as conn:
        user_db = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", chat_id)
        if user_db:
            ratings_stats = await conn.fetchrow("""
                SELECT 
                    COUNT(CASE WHEN liked = TRUE THEN 1 END) as likes_count,
                    COUNT(CASE WHEN disliked = TRUE THEN 1 END) as dislikes_count,
                    COUNT(CASE WHEN watched = TRUE THEN 1 END) as watched_count
                FROM ratings 
                WHERE user_id = $1
            """, user_db["user_id"])
        else:
            ratings_stats = None

    likes_count = ratings_stats['likes_count'] if ratings_stats else 0
    dislikes_count = ratings_stats['dislikes_count'] if ratings_stats else 0
    watched_count = ratings_stats['watched_count'] if ratings_stats else 0

    text = (
        f"👤 <b>Ваш профиль</b>\n\n"
        f"🆔 ID: <code>{chat_id}</code>\n"
        f"📛 Username: @{username_display}\n"
        f"📅 Регистрация: {registration_date}\n\n"
        f"📊 <b>Статистика:</b>\n"
        f"• 🎬 В коллекции: {collection_count}\n"
        f"• 👍 Лайков: {likes_count}\n"
        f"• 👎 Дизлайков: {dislikes_count}\n"
        f"• ✅ Просмотрено: {watched_count}\n\n"
    )

    # Информация о подписке
    if requests_info["has_subscription"]:
        text += f"💫 <b>Подписка активна</b>\n⏰ Осталось дней: {requests_info['days_left']}\n"
    else:
        text += f"🔒 <b>Подписка не активна</b>\n📊 Запросов сегодня: {requests_info['today_requests']}/{requests_info['max_requests']}\n"

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=kb_my_profile()
    )


@dp.callback_query(lambda c: c.data == "update_profile")
@require_agreement_callback
async def update_profile_handler(callback: types.CallbackQuery):
    """Обработчик обновления профиля"""
    chat_id = callback.message.chat.id
    new_username = callback.from_user.username

    print(f"DEBUG: Updating profile for {chat_id}, new username: '{new_username}'")

    # Всегда обновляем username
    await update_user_username(chat_id, new_username)

    # Получаем обновленную информацию
    user = await get_user_by_tg_id(chat_id)
    current_username = user['username'] or "не установлен"

    # Всегда показываем успешное обновление
    text = (
        f"✅ <b>Профиль обновлен!</b>\n\n"
        f"🆔 ID: <code>{chat_id}</code>\n"
        f"📛 Username: @{current_username}\n\n"
        f"<i>Данные успешно синхронизированы с Telegram</i>"
    )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👤 Вернуться к профилю", callback_data="my_profile")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
        ])
    )

    await callback.answer("✅ Профиль обновлен!")


@dp.callback_query(lambda c: c.data == "search_menu")
async def search_menu_handler(callback: types.CallbackQuery):
    """Меню поиска с проверкой подписки"""
    chat_id = callback.message.chat.id
    requests_info = await get_requests_info(chat_id)

    if requests_info["has_subscription"]:
        text = "Выберите тип поиска:"
    else:
        text = (
            "🔍 <b>Меню поиска</b>\n\n"
            "🎲 <b>Случайный поиск</b> - доступен бесплатно\n"
            "🔒 <b>Остальные функции</b> - требуют подписки\n\n"
        )

    keyboard = await get_search_menu_keyboard(chat_id)

    try:
        # Пытаемся отредактировать сообщение
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "no text in the message to edit" in str(e):
            # Если сообщение содержит медиа, удаляем его и отправляем новое текстовое
            try:
                await callback.message.delete()
            except:
                pass
            await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            raise e


@dp.callback_query(lambda c: c.data == "random_search")
async def random_search_handler(callback: types.CallbackQuery):
    """Меню случайного поиска"""
    chat_id = callback.message.chat.id
    requests_info = await get_requests_info(chat_id)

    if requests_info["has_subscription"]:
        text = "Выберите что искать:"
    else:
        text = (
            "🎲 <b>Случайный поиск</b>\n\n"
            "🎬 <b>Случайный фильм/сериал</b> - доступен бесплатно\n"
            "🔒 <b>Поиск по жанрам</b> - требует подписки\n\n"
            "💫 <b>Подписка откроет поиск по жанрам!</b>"
        )

    keyboard = await get_random_search_keyboard(chat_id)
    await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)

@dp.callback_query(lambda c: c.data == "premium_locked")
async def premium_locked_handler(callback: types.CallbackQuery):
    """Обработчик заблокированных функций"""
    await callback.answer(
        "❌ Эта функция доступна только с подпиской!\n\n"
        "💫 Подписка открывает все возможности бота!",
        show_alert=True
    )

@dp.callback_query(lambda c: c.data == "show_collection")
async def show_collection_handler(callback: types.CallbackQuery):
    """Показ коллекции с проверкой подписки"""
    chat_id = callback.message.chat.id
    requests_info = await get_requests_info(chat_id)

    total_items = await get_collection_count(chat_id)
    total_pages = (total_items + 3) // 4

    if total_pages == 0:
        text = "📚 Ваша коллекция пуста."
        if not requests_info["has_subscription"]:
            text += "\n\n💫 Подписка откроет экспорт коллекции в PDF и CSV!"
    else:
        if requests_info["has_subscription"]:
            text = f"📚 Ваша коллекция ({total_items} элементов):\n\nВыберите метод для экспорта вашей коллекции:"
        else:
            text = (
                f"📚 <b>Ваша коллекция</b> ({total_items} элементов)\n\n"
                "💫 <b>Подписка откроет экспорт коллекции!</b>\n"
                "Сохраните свою коллекцию в удобных форматах PDF или CSV."
            )

    keyboard = await kb_collection(chat_id, 0, total_pages)

    try:
        await bot.delete_message(chat_id, callback.message.message_id)
    except Exception:
        pass

    await bot.send_message(chat_id, text, parse_mode="HTML", reply_markup=keyboard)


@dp.callback_query(lambda c: c.data == "export_pdf")
async def export_pdf_handler(callback: types.CallbackQuery):
    """Экспорт коллекции в PDF"""
    chat_id = callback.message.chat.id
    requests_info = await get_requests_info(chat_id)

    # Проверяем подписку
    if not requests_info["has_subscription"]:
        await callback.answer(
            "❌ Экспорт коллекции доступен только с подпиской!\n\n"
            "💫 Подписка откроет все возможности бота!",
            show_alert=True
        )
        return

    await callback.answer("🔄 Создаю PDF...")

    # Создаем PDF
    pdf_buffer = await generate_collection_pdf(chat_id)

    if not pdf_buffer:
        await callback.answer("❌ Коллекция пуста!", show_alert=True)
        return

    # Отправляем файл пользователю
    try:
        await bot.send_document(
            chat_id=chat_id,
            document=types.BufferedInputFile(
                pdf_buffer.getvalue(),
                filename="my_collection.pdf"
            ),
            caption="📚 Ваша коллекция фильмов и сериалов"
        )
        await callback.answer("✅ PDF готов!")
    except Exception as e:
        await callback.answer("❌ Ошибка при создании PDF", show_alert=True)
        print(f"PDF export error: {e}")


@dp.callback_query(lambda c: c.data == "friends_menu")
async def friends_menu_handler(callback: types.CallbackQuery):
    """Меню друзей с проверкой подписки"""
    chat_id = callback.message.chat.id
    requests_info = await get_requests_info(chat_id)

    if requests_info["has_subscription"]:
        text = "👥 Система друзей:"
    else:
        text = (
            "👥 <b>Система друзей</b>\n\n"
            "✅ <b>Бесплатные функции:</b>\n"
            "• Добавление друзей\n"
            "• Просмотр списка друзей\n"
            "• Управление заявками\n\n"
            "🔒 <b>Требует подписки:</b>\n"
            "• Рекомендации друзей\n\n"
            "💫 <b>Подписка откроет умные рекомендации на основе оценок ваших друзей!</b>"
        )

    keyboard = await get_friends_menu_keyboard(chat_id)

    try:
        # Пытаемся отредактировать текущее сообщение
        await callback.message.edit_text(text, parse_mode="HTML", reply_markup=keyboard)
    except TelegramBadRequest as e:
        if "no text in the message to edit" in str(e):
            # Если сообщение содержит медиа, удаляем его и отправляем новое
            try:
                await callback.message.delete()
            except Exception as delete_error:
                print(f"Error deleting message: {delete_error}")

            # Отправляем новое текстовое сообщение
            await callback.message.answer(text, parse_mode="HTML", reply_markup=keyboard)
        else:
            # Если другая ошибка, пробрасываем ее дальше
            raise e

    await callback.answer()


@dp.callback_query(lambda c: c.data == "friends_recommendations")
async def friends_recommendations_handler(callback: types.CallbackQuery):
    """Рекомендации друзей"""
    chat_id = callback.message.chat.id
    requests_info = await get_requests_info(chat_id)

    # Проверяем подписку
    if not requests_info["has_subscription"]:
        await callback.answer(
            "❌ Рекомендации друзей доступны только с подпиской!\n\n"
            "💫 Подписка откроет умные рекомендации на основе оценок ваших друзей!",
            show_alert=True
        )
        return

    # ПРОВЕРКА ЛИМИТА (для подписчиков не нужно, но оставим для логики)
    can_request, error_msg = await handle_search_request(chat_id, "friends_recommendations")
    if not can_request:
        await callback.answer(error_msg, show_alert=True)
        return

    recommendations = await get_friends_likes(chat_id)
    if not recommendations:
        await callback.answer("❌ Нет рекомендаций от друзей")
        await navigate_to_menu(
            chat_id, callback.message.message_id,
            "📭 Пока нет рекомендаций от друзей.\n\nДобавьте друзей и попросите их ставить лайки фильмам и сериалам!",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="➕ Добавить друга", callback_data="add_friend")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="friends_menu")]
            ])
        )
        return

    # Сохраняем рекомендации в сессию
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {}

    user_sessions[chat_id]["friends_recommendations"] = recommendations
    user_sessions[chat_id]["friends_rec_index"] = 0

    await send_friend_recommendation_card(chat_id, callback.message.message_id)

@dp.callback_query(lambda c: c.data == "admin_subscriptions")
async def admin_subscriptions_handler(callback: types.CallbackQuery):
    if not is_admin(callback.message.chat.id):
        await callback.answer("❌ Нет доступа!")
        return

    await callback.message.edit_text(
        "🌟 Управление подписками\n\n"
        "Выберите действие:",
        reply_markup=kb_admin_subscriptions_management()
    )


@dp.callback_query(lambda c: c.data == "subscription_management")
async def subscription_management_handler(callback: types.CallbackQuery):
    """Управление подпиской"""
    chat_id = callback.message.chat.id
    requests_info = await get_requests_info(chat_id)

    if requests_info["has_subscription"]:
        text = (
            f"🌟 <b>Управление подпиской</b>\n\n"
            f"⏰ Дней осталось: {requests_info['days_left']}\n"
            f"📅 Истекает: {(datetime.now() + timedelta(days=requests_info['days_left'])).strftime('%d.%m.%Y')}\n"
            f"<i>Вы можете продлить подписку или посмотреть статистику использования</i>"
        )
    else:
        text = (
            f"💫 <b>Управление подпиской</b>\n\n"
            f"🔒 Статус: Нет подписки\n"
            f"📈 Использовано запросов: {requests_info['today_requests']}/{requests_info['max_requests']}\n"
            f"🎯 Осталось: {requests_info['remaining']}\n\n"
            f"<b>Преимущества подписки:</b>\n"
            f"• ♾️ <b>Безлимитные запросы</b> - ищите сколько угодно\n"
            f"• 🔍 Поиск по названию\n"
            f"• 🎭 Поиск по актерам\n"
            f"• 🎯 Рекомендации на основе предпочтений\n"
            f"• 🔥 Тренды\n"
            f"• 📄 Экспорт коллекции\n"
            f"• 🎯 Рекомендации друзей\n"
            f"• 🧭 Поиск по жанрам\n"
            f"<i>Для получения подписки обратитесь к администратору</i>"
        )

    await callback.message.edit_text(
        text,
        parse_mode="HTML",
        reply_markup=kb_subscription_management(
            requests_info["has_subscription"],
            requests_info.get("days_left", 0)
        )
    )


@dp.callback_query(lambda c: c.data == "buy_subscription")
async def buy_subscription_handler(callback: types.CallbackQuery):
    """Покупка подписки"""
    await callback.message.edit_text(
        "💳 <b>Приобретение подписки</b>\n\n"
        "Для приобретения подписки обратитесь к администратору\n\n"
        "<i>После оплаты администратор активирует вашу подписку</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨‍💼 Написать администратору", url="https://t.me/donk1337228")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="subscription_management")]
        ])
    )


@dp.callback_query(lambda c: c.data == "extend_my_subscription")
async def extend_my_subscription_handler(callback: types.CallbackQuery):
    """Продление подписки"""
    chat_id = callback.message.chat.id
    requests_info = await get_requests_info(chat_id)

    if not requests_info["has_subscription"]:
        await callback.answer("❌ У вас нет активной подписки")
        return

    await callback.message.edit_text(
        f"📅 <b>Продление подписки</b>\n\n"
        f"Текущая подписка истекает через {requests_info['days_left']} дней\n"
        f"Дата истечения: {(datetime.now() + timedelta(days=requests_info['days_left'])).strftime('%d.%m.%Y')}\n\n"
        "Для продления подписки обратитесь к администратору:",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👨‍💼 Написать администратору", url="https://t.me/donk1337228")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="subscription_management")]
        ])
    )


@dp.callback_query(lambda c: c.data == "subscription_info")
async def subscription_info_handler(callback: types.CallbackQuery):
    """Информация о подписке"""
    await callback.message.edit_text(
        "ℹ️ <b>Информация о подписке</b>\n\n"
        "<b>Что дает подписка?</b>\n"
        "• ♾️ <b>Безлимитные запросы</b> - ищите сколько угодно\n"
        "• ⚡ <b>Расширенные функции</b> - все возможности бота\n\n"
        "<b>Тарифы:</b>\n"
        "• 1 месяц - 150 руб.\n"
        "• 3 месяца - 390 руб. (экономия 13%)\n"
        "• 12 месяцев - 1150 руб. (экономия 36%)\n\n"
        "<i>Для приобретения подписки обратитесь к администратору</i>",
        parse_mode="HTML",
        reply_markup=kb_subscription_info()
    )


@dp.callback_query(lambda c: c.data == "subscription_stats")
async def subscription_stats_handler(callback: types.CallbackQuery):
    """Статистика использования подписки"""
    chat_id = callback.message.chat.id
    requests_info = await get_requests_info(chat_id)

    if not requests_info["has_subscription"]:
        await callback.answer("❌ У вас нет активной подписки")
        return

    # Получаем дополнительную статистику
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", chat_id)
        if user:
            # Статистика использования
            total_requests = await conn.fetchval("""
                SELECT COUNT(*) FROM user_requests WHERE user_id=$1
            """, user["user_id"])

            # Запросы за последние 7 дней
            week_requests = await conn.fetchval("""
                SELECT COUNT(*) FROM user_requests 
                WHERE user_id=$1 AND created_at >= CURRENT_DATE - INTERVAL '7 days'
            """, user["user_id"])
        else:
            total_requests = 0
            week_requests = 0

    await callback.message.edit_text(
        f"📊 <b>Статистика использования подписки</b>\n\n"
        f"⏰ Дней осталось: {requests_info['days_left']}\n"
        f"📅 Истекает: {(datetime.now() + timedelta(days=requests_info['days_left'])).strftime('%d.%m.%Y')}\n\n"
        f"<b>Активность:</b>\n"
        f"• Всего запросов: {total_requests}\n"
        f"• За последние 7 дней: {week_requests}\n"
        f"• Сегодня: {requests_info['today_requests']} запросов\n\n"
        f"<i>Подписка активна - используйте бота без ограничений! 🚀</i>",
        parse_mode="HTML",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="📅 Продлить подписку", callback_data="extend_my_subscription")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="subscription_management")]
        ])
    )

@dp.callback_query(lambda c: c.data == "admin_users_list")
async def admin_users_list_handler(callback: types.CallbackQuery):
    if not is_admin(callback.message.chat.id):
        await callback.answer("❌ Нет доступа!")
        return

    users = await get_all_users(limit=10, offset=0)
    total_users = await get_users_count()
    total_pages = (total_users + 9) // 10  # 10 пользователей на страницу

    if not users:
        await callback.message.edit_text(
            "❌ Пользователи не найдены",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_subscriptions")]
            ])
        )
        return

    text = f"👥 Список пользователей\n\nВсего пользователей: {total_users}\nСтраница 1/{total_pages}\n\n"

    await callback.message.edit_text(
        text,
        reply_markup=kb_users_list(users, 0, total_pages)
    )


@dp.callback_query(lambda c: c.data.startswith("users_page_"))
async def users_page_handler(callback: types.CallbackQuery):
    if not is_admin(callback.message.chat.id):
        await callback.answer("❌ Нет доступа!")
        return

    page = int(callback.data.split("_")[2])
    users = await get_all_users(limit=10, offset=page * 10)
    total_users = await get_users_count()
    total_pages = (total_users + 9) // 10

    text = f"👥 Список пользователей\n\nВсего пользователей: {total_users}\nСтраница {page + 1}/{total_pages}\n\n"

    await callback.message.edit_text(
        text,
        reply_markup=kb_users_list(users, page, total_pages)
    )


@dp.callback_query(lambda c: c.data.startswith("admin_user_"))
async def admin_user_handler(callback: types.CallbackQuery):
    if not is_admin(callback.message.chat.id):
        await callback.answer("❌ Нет доступа!")
        return

    tg_id = int(callback.data.split("_")[2])
    user = await get_user_by_tg_id(tg_id)

    if not user:
        await callback.answer("❌ Пользователь не найден")
        return

    username = user['username'] or f"Пользователь {tg_id}"
    has_subscription = user['is_active']

    if has_subscription:
        expires_at = user['expires_at']
        days_left = (expires_at - datetime.now()).days
        subscription_info = f"🌟 Активна\n⏰ Осталось дней: {days_left}\n📅 Истекает: {expires_at.strftime('%d.%m.%Y')}"
    else:
        subscription_info = "❌ Нет подписки"

    text = (
        f"👤 Профиль пользователя\n\n"
        f"🆔 ID: {tg_id}\n"
        f"📛 Имя: @{username}\n"
        f"💫 Подписка: {subscription_info}"
    )

    await callback.message.edit_text(
        text,
        reply_markup=kb_user_management(tg_id, has_subscription, days_left if has_subscription else 0)
    )


@dp.callback_query(lambda c: c.data.startswith("grant_sub_"))
async def grant_sub_handler(callback: types.CallbackQuery):
    if not is_admin(callback.message.chat.id):
        await callback.answer("❌ Нет доступа!")
        return

    tg_id = int(callback.data.split("_")[2])

    # Сохраняем состояние ожидания ввода дней
    chat_id = callback.message.chat.id
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {}

    user_sessions[chat_id]["waiting_subscription_days"] = True
    user_sessions[chat_id]["target_user_tg_id"] = tg_id

    await callback.message.edit_text(
        f"🌟 Выдача подписки пользователю {tg_id}\n\n"
        "Введите количество дней подписки:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_{tg_id}")]
        ])
    )


@dp.callback_query(lambda c: c.data.startswith("revoke_sub_"))
async def revoke_sub_handler(callback: types.CallbackQuery):
    if not is_admin(callback.message.chat.id):
        await callback.answer("❌ Нет доступа!")
        return

    tg_id = int(callback.data.split("_")[2])

    success = await deactivate_subscription(tg_id)

    if success:
        # Уведомляем пользователя
        try:
            await bot.send_message(
                tg_id,
                "❌ Ваша подписка была аннулирована администратором.\n\n"
                "Если это ошибка, обратитесь к администратору."
            )
        except:
            pass

        await callback.answer("✅ Подписка аннулирована!")

        # Возвращаемся к профилю пользователя
        user = await get_user_by_tg_id(tg_id)
        username = user['username'] or f"Пользователь {tg_id}"

        text = (
            f"👤 Профиль пользователя\n\n"
            f"🆔 ID: {tg_id}\n"
            f"📛 Имя: @{username}\n"
            f"💫 Подписка: ❌ Аннулирована"
        )

        await callback.message.edit_text(
            text,
            reply_markup=kb_user_management(tg_id, False)
        )
    else:
        await callback.answer("❌ Ошибка при аннулировании подписки")


@dp.callback_query(lambda c: c.data.startswith("extend_sub_"))
async def extend_sub_handler(callback: types.CallbackQuery):
    if not is_admin(callback.message.chat.id):
        await callback.answer("❌ Нет доступа!")
        return

    tg_id = int(callback.data.split("_")[2])

    # Сохраняем состояние ожидания ввода дней для продления
    chat_id = callback.message.chat.id
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {}

    user_sessions[chat_id]["waiting_subscription_extend"] = True
    user_sessions[chat_id]["target_user_tg_id"] = tg_id

    user = await get_user_by_tg_id(tg_id)
    expires_at = user['expires_at']
    days_left = (expires_at - datetime.now()).days

    await callback.message.edit_text(
        f"📅 Продление подписки пользователю {tg_id}\n\n"
        f"Текущая подписка истекает через {days_left} дней\n"
        f"Дата истечения: {expires_at.strftime('%d.%m.%Y')}\n\n"
        "Введите количество дней для продления:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data=f"admin_user_{tg_id}")]
        ])
    )


@dp.callback_query(lambda c: c.data == "admin_search_user")
async def admin_search_user_handler(callback: types.CallbackQuery):
    if not is_admin(callback.message.chat.id):
        await callback.answer("❌ Нет доступа!")
        return

    # Сохраняем состояние ожидания ввода ID пользователя
    chat_id = callback.message.chat.id
    if chat_id not in user_sessions:
        user_sessions[chat_id] = {}

    user_sessions[chat_id]["waiting_user_search"] = True

    await callback.message.edit_text(
        "🔍 Поиск пользователя\n\n"
        "Введите ID пользователя:",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отмена", callback_data="admin_subscriptions")]
        ])
    )


@dp.callback_query(lambda c: c.data == "accept_agreement")
async def accept_agreement_handler(callback: types.CallbackQuery):
    """Обработчик принятия соглашения"""
    chat_id = callback.message.chat.id
    username = callback.from_user.username

    try:
        # Удаляем сообщение с соглашением
        await callback.message.delete()
    except Exception as e:
        print(f"Error deleting agreement message: {e}")

    try:
        # Проверяем, есть ли уже пользователь
        existing_user = await get_user_by_tg_id(chat_id)

        if existing_user:
            # Пользователь есть, но соглашение не принято - обновляем
            await accept_user_agreement(chat_id)
        else:
            # Пользователя нет - создаем нового с принятым соглашением
            await create_user_with_agreement(chat_id, username)

        # Показываем приветствие
        await callback.message.answer(
            "✅ Спасибо! Пользовательское соглашение принято.\n\n"
            "Добро пожаловать в бот! 🎬"
        )

        # Показываем главное меню
        await show_main_menu(callback.message)

    except Exception as e:
        print(f"Error accepting agreement: {e}")
        await callback.answer("❌ Произошла ошибка. Попробуйте снова.", show_alert=True)


@dp.callback_query(lambda c: c.data == "reject_agreement")
async def reject_agreement_handler(callback: types.CallbackQuery):
    """Обработчик отклонения соглашения"""
    try:
        # Сначала удаляем сообщение с соглашением (включая документ)
        await callback.message.delete()
    except Exception as e:
        print(f"Error deleting agreement message: {e}")

    # Затем отправляем новое сообщение с текстом об отклонении
    await callback.message.answer(
        "❌ Для использования бота необходимо принять пользовательское соглашение.\n\n"
        "Если вы передумаете, просто отправьте команду /start снова.",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🔄 Попробовать снова", callback_data="restart_agreement")]
        ])
    )


@dp.callback_query(lambda c: c.data == "restart_agreement")
async def restart_agreement_handler(callback: types.CallbackQuery):
    """Повторный показ соглашения"""
    try:
        # Удаляем предыдущее сообщение
        await callback.message.delete()
    except Exception as e:
        print(f"Error deleting message: {e}")

    # Показываем соглашение заново
    await show_user_agreement(callback.message)

@dp.callback_query()
async def handle_callback(callback: types.CallbackQuery):
    chat_id = callback.message.chat.id
    data = callback.data
    old_msg_id = callback.message.message_id

    if callback.data not in ["accept_agreement", "reject_agreement", "restart_agreement"]:
        user = await get_user_by_tg_id(chat_id)
        if not user or not user.get('agreement_accepted'):
            await callback.answer("❌ Сначала примите пользовательское соглашение через /start", show_alert=True)
            return

    # В handle_callback добавьте этот обработчик:
    if data == "refresh_main":
        requests_info = await get_requests_info(chat_id)

        if requests_info["has_subscription"]:
            text = (f"Выберите действие:")
        else:
            text = (
                f"📊 Статистика на сегодня:\n"
                f"• Использовано запросов: {requests_info['today_requests']}/{requests_info['max_requests']}\n"
                f"• Осталось запросов: {requests_info['remaining']}\n\n"
                f"Выберите действие:"
            )

        try:
            await callback.message.edit_text(text, parse_mode="HTML", reply_markup=kb_main())
        except TelegramBadRequest as e:
            if "no text in the message to edit" in str(e):
                try:
                    await callback.message.delete()
                except:
                    pass
                await callback.message.answer(text, parse_mode="HTML", reply_markup=kb_main())
            elif "message is not modified" in str(e):
                await callback.answer("✅ Информация актуальная!")
            else:
                raise e
        await callback.answer("✅ Страница обновлена!")
        return

    # В handle_callback добавьте:
    if data == "already_in_collection":
        await callback.answer("✅ Этот контент уже в вашей коллекции!")
        return

    if data == "delete_message":
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass
        await callback.answer("❌ Действие отменено")
        return

    # В handle_callback добавьте:
    if data == "friends_menu":
        await navigate_to_menu(chat_id, old_msg_id, "👥 Система друзей:", kb_friends_menu())
        return

    if data == "my_friends":
        friends = await get_user_friends(chat_id)
        if not friends:
            await callback.answer("❌ У вас пока нет друзей")
            await navigate_to_menu(
                chat_id, old_msg_id,
                "📭 У вас пока нет друзей. Добавьте друзей, чтобы вискать их рекомендации!",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить друга", callback_data="add_friend")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="friends_menu")]
                ])
            )
        else:
            await navigate_to_menu(
                chat_id, old_msg_id,
                f"👥 Ваши друзья ({len(friends)}):",
                kb_my_friends(friends, 0)
            )
        return

    if data == "friends_recommendations":
        # ПРОВЕРКА ЛИМИТА
        can_request, error_msg = await handle_search_request(chat_id, "friends_recommendations")
        if not can_request:
            await callback.answer(error_msg, show_alert=True)
            return

        recommendations = await get_friends_likes(chat_id)
        if not recommendations:
            await callback.answer("❌ Нет рекомендаций от друзей")
            await navigate_to_menu(
                chat_id, old_msg_id,
                "📭 Пока нет рекомендаций от друзей.\n\nДобавьте друзей и попросите их ставить лайки фильмам и сериалам!",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="➕ Добавить друга", callback_data="add_friend")],
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="friends_menu")]
                ])
            )
            return

        # Сохраняем рекомендации в сессию
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}

        user_sessions[chat_id]["friends_recommendations"] = recommendations
        user_sessions[chat_id]["friends_rec_index"] = 0

        await send_friend_recommendation_card(chat_id, old_msg_id)
        return

    if data == "add_friend":
        await navigate_to_menu(
            chat_id, old_msg_id,
            "👥 Чтобы добавить друга:\n\n"
            "1. Попросите друга написать боту\n"
            "2. Попросите друга отправить команду /myid\n"
            "3. Отправьте мне ID вашего друга\n\n"
            "Ваш ID для друзей:",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔢 Узнать мой ID", callback_data="get_my_id")],
                [InlineKeyboardButton(text="🔢 Ввести ID друга", callback_data="input_friend_id")],
                [InlineKeyboardButton(text="📨 Мои заявки", callback_data="friend_requests")],
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="friends_menu")]
            ])
        )
        return

    if data == "friend_requests":
        requests = await get_pending_friend_requests(chat_id)
        if not requests:
            await callback.answer("❌ Нет входящих заявок")
            await navigate_to_menu(
                chat_id, old_msg_id,
                "📭 Нет входящих заявок в друзья",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="friends_menu")]
                ])
            )
            return

        text = "📨 Входящие заявки в друзья:\n\n"
        keyboard = []

        for req in requests:
            username = req['username'] or f"Пользователь {req['tg_id']}"
            text += f"👤 @{username}\n"
            keyboard.append([
                InlineKeyboardButton(text=f"✅ Принять {username}", callback_data=f"accept_request_{req['request_id']}"),
                InlineKeyboardButton(text=f"❌ Отклонить", callback_data=f"reject_request_{req['request_id']}")
            ])

        keyboard.append([InlineKeyboardButton(text="⬅️ Назад", callback_data="friends_menu")])

        await navigate_to_menu(
            chat_id, old_msg_id,
            text,
            InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        return

    if data.startswith("accept_request_"):
        request_id = int(data.split("_")[2])
        result = await accept_friend_request(request_id)

        if result:
            await callback.answer("✅ Заявка принята!")
            # Обновляем меню
            await navigate_to_menu(
                chat_id, old_msg_id,
                "✅ Заявка в друзья принята!",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="👥 К друзьям", callback_data="friends_menu")]
                ])
            )
        else:
            await callback.answer("❌ Ошибка при принятии заявки")
        return

    if data.startswith("reject_request_"):
        request_id = int(data.split("_")[2])
        # Просто удаляем заявку
        async with state.db.acquire() as conn:
            await conn.execute("DELETE FROM friend_requests WHERE request_id = $1", request_id)

        await callback.answer("❌ Заявка отклонена")
        await navigate_to_menu(
            chat_id, old_msg_id,
            "❌ Заявка в друзья отклонена",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="👥 К друзьям", callback_data="friends_menu")]
            ])
        )
        return

    # В handle_callback добавляем:

    if data == "friend_requests_management":
        await navigate_to_menu(
            chat_id, old_msg_id,
            "📨 Управление заявками в друзья:",
            kb_friend_requests_management()
        )
        return

    if data == "outgoing_requests":
        requests = await get_outgoing_friend_requests(chat_id)
        if not requests:
            await callback.answer("❌ Нет исходящих заявок")
            await navigate_to_menu(
                chat_id, old_msg_id,
                "📭 Нет исходящих заявок в друзья",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="friend_requests_management")]
                ])
            )
        else:
            await navigate_to_menu(
                chat_id, old_msg_id,
                f"📤 Ваши исходящие заявки ({len(requests)}):",
                kb_outgoing_requests(requests, 0)
            )
        return

    if data.startswith("outgoing_request_"):
        request_id = int(data.split("_")[2])

        # Создаем клавиатуру для отмены заявки
        keyboard = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="❌ Отменить заявку", callback_data=f"cancel_request_{request_id}")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="outgoing_requests")]
        ])

        await navigate_to_menu(
            chat_id, old_msg_id,
            "Вы можете отменить эту заявку в друзья:",
            keyboard
        )
        return

    if data.startswith("cancel_request_"):
        request_id = int(data.split("_")[2])

        # Получаем информацию о заявке
        async with state.db.acquire() as conn:
            request_info = await conn.fetchrow("""
                SELECT u.tg_id, u.username 
                FROM friend_requests fr
                JOIN users u ON fr.to_user_id = u.user_id
                WHERE fr.request_id = $1
            """, request_id)

        if not request_info:
            await callback.answer("❌ Заявка не найдена")
            return

        # Отменяем заявку
        success = await cancel_friend_request(chat_id, request_info['tg_id'])

        if success:
            await callback.answer("✅ Заявка отменена")
            # Возвращаемся к списку исходящих заявок
            requests = await get_outgoing_friend_requests(chat_id)
            if not requests:
                await navigate_to_menu(
                    chat_id, old_msg_id,
                    "📭 Нет исходящих заявок в друзья",
                    InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="⬅️ Назад", callback_data="friend_requests_management")]
                    ])
                )
            else:
                await navigate_to_menu(
                    chat_id, old_msg_id,
                    f"📤 Ваши исходящие заявки ({len(requests)}):",
                    kb_outgoing_requests(requests, 0)
                )
        else:
            await callback.answer("❌ Ошибка при отмене заявки")
        return


    # Добавь этот обработчик
    if data == "get_my_id":
        await callback.answer(f"🆔 Ваш ID: {chat_id}\n\nПоделитесь этим ID с друзьями!", show_alert=True)
        return

    if data == "input_friend_id":
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        user_sessions[chat_id]["waiting_friend_id"] = True

        await callback.message.edit_text(  # ← ИСПРАВЛЕНО: edit_text вместо answer
            "🔢 Введите ID вашего друга:",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="friends_menu")]
            ])
        )
        return

    if data == "next_friend_rec":
        # ПРОВЕРКА ЛИМИТА
        can_request, error_msg = await handle_search_request(chat_id, "next_friend_rec")
        if not can_request:
            await callback.answer(error_msg, show_alert=True)
            return

        await send_friend_recommendation_card(chat_id, old_msg_id)
        return

    if data == "search_filters":
        # Инициализируем фильтры если их нет
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        if "filters" not in user_sessions[chat_id]:
            # Пробуем загрузить сохраненные фильтры
            saved_filters = await load_search_filters(chat_id)
            user_sessions[chat_id]["filters"] = saved_filters

        current_filters = user_sessions[chat_id]["filters"]
        await navigate_to_menu(chat_id, old_msg_id, "Настройте фильтры поиска:", kb_filters_menu(current_filters))
        return

    if data == "search_by_title":
        # Сохраняем состояние ожидания ввода
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        user_sessions[chat_id]["waiting_title_search"] = True

        await navigate_to_menu(
            chat_id,
            old_msg_id,
            "🔍 Введите название фильма или сериала для поиска:",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="search_menu")]
            ])
        )
        return

    # В handle_callback добавьте:
    if data == "search_by_person":
        # Сохраняем состояние ожидания ввода
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        user_sessions[chat_id]["waiting_person_search"] = True

        await navigate_to_menu(
            chat_id,
            old_msg_id,
            "🎭 Введите имя актера или режиссера:",
            InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="search_menu")]
            ])
        )
        return



    if data == "admin_ban_list":
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        banned_list = await get_banned_list(100)  # увеличиваем лимит
        if not banned_list:
            await callback.message.answer(
                "📭 Список банов пуст",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
                ])
            )
            return

        # Отправляем первую страницу
        await send_banned_page(chat_id, banned_list, 0)
        return

    # Добавляем обработчик для страниц
    if data.startswith("ban_page_"):
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        page = int(data.split("_")[2])
        banned_list = await get_banned_list(100)

        if not banned_list:
            await callback.answer("❌ Список банов пуст")
            return

        await callback.message.edit_text(
            **format_banned_page(banned_list, page),
            reply_markup=kb_banned_pagination(banned_list, page)
        )
        return

    if data.startswith("admin_search_page_"):
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        page = int(data.split("_")[3])

        # Получаем сохраненные результаты поиска
        if "search_results" not in user_sessions.get(chat_id, {}):
            await callback.answer("❌ Результаты поиска устарели")
            return

        search_results = user_sessions[chat_id]["search_results"]
        search_query = user_sessions[chat_id].get("search_query", "")

        await callback.message.edit_text(
            f"🔍 Найдено {len(search_results)} результатов по запросу: '{search_query}'\nСтраница {page + 1}",
            reply_markup=kb_search_results(search_results, search_query, page=page)
        )
        return



    if data.startswith("filmography_page_"):
        page = int(data.split("_")[2])

        if "filmography" not in user_sessions.get(chat_id, {}):
            await callback.answer("❌ Фильмография не найдена")
            return

        filmography = user_sessions[chat_id]["filmography"]
        person_name = user_sessions[chat_id].get("filmography_person_name", "Актер")

        # Обновляем страницу в сессии
        user_sessions[chat_id]["filmography_page"] = page

        text, keyboard = await send_person_filmography_page(
            chat_id, filmography, person_name, page
        )

        await callback.message.edit_text(text, reply_markup=keyboard)
        return

    # В handle_callback добавьте:
    if data.startswith("select_person_"):
        person_id = int(data.split("_")[2])

        # Получаем информацию об актере для отображения имени
        person_results = user_sessions[chat_id]["person_results"]
        person_info = next((p for p in person_results if p["id"] == person_id), None)
        person_name = person_info.get("name", "Актер") if person_info else "Актер"

        # Получаем фильмографию актера
        filmography = await get_person_filmography(person_id)  # ДОБАВЬТЕ await

        if not filmography:
            await callback.answer("❌ Не удалось загрузить фильмографию или все работы заблокированы")
            return

        # Сохраняем фильмографию в сессию
        user_sessions[chat_id]["filmography"] = filmography
        user_sessions[chat_id]["filmography_person_name"] = person_name
        user_sessions[chat_id]["filmography_page"] = 0
        user_sessions[chat_id]["filmography_person_id"] = person_id

        # Отправляем первую страницу фильмографии
        text, keyboard = await send_person_filmography_page(
            chat_id, filmography, person_name, 0
        )

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        await bot.send_message(chat_id, text, reply_markup=keyboard)
        return

    # Обработчик пагинации для поиска актеров
    # В handle_callback добавьте обработчик пагинации:
    if data.startswith("person_page_"):
        page = int(data.split("_")[2])

        if "person_results" not in user_sessions.get(chat_id, {}):
            await callback.answer("❌ Результаты поиска устарели")
            return

        person_results = user_sessions[chat_id]["person_results"]
        person_query = user_sessions[chat_id].get("person_query", "")

        # Обновляем страницу в сессии
        user_sessions[chat_id]["person_page"] = page

        await send_person_results_page(chat_id, person_results, person_query, page)
        return

    # Пагинация поиска
    if data.startswith("search_page_"):
        page = int(data.split("_")[2])

        print(f"DEBUG: search_page_ called, page={page}, chat_id={chat_id}")
        print(f"DEBUG: user_sessions keys: {list(user_sessions.get(chat_id, {}).keys())}")

        if "search_results" not in user_sessions.get(chat_id, {}):
            print(f"DEBUG: search_results not found in session!")
            await callback.answer("❌ Результаты поиска устарели")
            return

        search_results = user_sessions[chat_id]["search_results"]
        search_query = user_sessions[chat_id].get("search_query", "")

        # Обновляем страницу в сессии
        user_sessions[chat_id]["search_page"] = page

        # ИСПОЛЬЗУЕМ ЛОГИКУ ИЗ send_search_results_page (а не kb_search_results)
        total_results = len(search_results)
        results_per_page = 10
        start_idx = page * results_per_page
        end_idx = start_idx + results_per_page
        page_results = search_results[start_idx:end_idx]

        text = f"🔍 Найдено {total_results} результатов по запросу: '{search_query}'\nСтраница {page + 1}/{(total_results + results_per_page - 1) // results_per_page}\n\nВыберите:"

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
                        callback_data=f"select_{item['id']}_{media_type}"  # ВАЖНО: select_ для обычного поиска
                    )
                ])

        # Пагинация
        nav_buttons = []
        if page > 0:
            nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"search_page_{page - 1}"))
        if end_idx < total_results:
            nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"search_page_{page + 1}"))

        if nav_buttons:
            keyboard.append(nav_buttons)

        keyboard.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="search_by_title")])
        keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])

        await callback.message.edit_text(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard)
        )
        return

    # ДОБАВЬТЕ ЭТОТ ОБРАБОТЧИК - ОН ОТСУТСТВУЕТ В ВАШЕМ КОДЕ!
    if data.startswith("select_"):
        print(f"🟢 SELECT handler called: {data}")

        parts = data.split("_")
        if len(parts) < 3:
            await callback.answer("❌ Ошибка: некорректные данные.")
            return

        try:
            tmdb_id = int(parts[1])
            type_ = parts[2]
            print(f"DEBUG: tmdb_id={tmdb_id}, type_={type_}")
        except (ValueError, IndexError) as e:
            await callback.answer("❌ Ошибка данных.")
            return

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            print("Не удалось удалить сообщение с результатами поиска")

        # Получаем детали фильма/сериала
        details = get_item_details(type_, tmdb_id)
        if not details:
            await callback.answer("❌ Не удалось загрузить данные")
            return

        title = details.get("title") or details.get("name") or "Без названия"
        year = (details.get("release_date") or details.get("first_air_date") or "")[:4]
        rating = details.get("vote_average") or "—"
        overview = details.get("overview") or "Описание отсутствует."
        if len(overview) > 2000:
            overview = overview[:2000] + "..."
        poster = f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get("poster_path") else None
        avg_ratings = await get_ratings(tmdb_id, type_)

        # Определяем все роли человека в этом проекте
        roles = set()

        # Проверяем, пришли ли мы из поиска по актерам
        if "filmography" in user_sessions.get(chat_id, {}):
            filmography = user_sessions[chat_id]["filmography"]

            # Собираем все роли для этого проекта
            for item in filmography:
                if item.get("id") == tmdb_id and item.get("media_type") == type_:
                    item_roles = item.get("person_role", [])  # Теперь это список ролей
                    for role in item_roles:
                        if role == "director":
                            roles.add("🎬 Режиссер")
                        elif role == "actor":
                            roles.add("🎭 Актер")

        # Формируем информацию о ролях
        role_info = ""
        if roles:
            role_info = ", ".join(sorted(roles)) + "\n"

        # Формируем caption с информацией о ролях
        caption = (
            f"<b>{title}</b> ({year})\n"
            f"⭐ {rating} | 👍 {avg_ratings['likes']} | 👎 {avg_ratings['dislikes']} | 👀 {avg_ratings['watches']}"
        )

        caption += "\n\n"

        # Добавляем описание как раскрываемую цитату
        if overview and overview != "Описание отсутствует.":
            caption += f"<blockquote expandable>{overview}</blockquote>"
        else:
            caption += "Описание отсутствует."

        # Добавляем информацию о провайдерах для РФ (если есть)
        try:
            prov = get_providers_ru(type_, tmdb_id)
            if prov:
                prov_text = f"\n\nГде смотреть: {prov}"
                if len(caption) + len(prov_text) <= 1024:
                    caption += prov_text
        except Exception:
            pass

        # Определяем, откуда пришли - из поиска или из фильмографии
        is_from_filmography = "filmography" in user_sessions.get(chat_id, {})

        if is_from_filmography:
            # Проверяем, находится ли контент в коллекции
            is_in_collection = await is_in_user_collection(chat_id, tmdb_id, type_)

            # Клавиатура для фильмографии
            buttons = []

            if is_in_collection:
                buttons.append([
                    InlineKeyboardButton(text="✅ В коллекции", callback_data=f"already_in_collection"),
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(text="➕ В коллекцию", callback_data=f"add_{tmdb_id}_{type_}"),
                ])

            buttons.append([InlineKeyboardButton(text="🎯 Похожее", callback_data=f"similar_{tmdb_id}_{type_}")])
            buttons.append([InlineKeyboardButton(text="⬅️ К фильмографии", callback_data="back_to_filmography")])
            buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)
        else:
            # Проверяем, находится ли контент в коллекции
            is_in_collection = await is_in_user_collection(chat_id, tmdb_id, type_)

            # Клавиатура для обычного поиска
            buttons = []

            if is_in_collection:
                buttons.append([
                    InlineKeyboardButton(text="✅ В коллекции", callback_data=f"already_in_collection"),
                ])
            else:
                buttons.append([
                    InlineKeyboardButton(text="➕ В коллекцию", callback_data=f"add_{tmdb_id}_{type_}"),
                ])

            buttons.append([InlineKeyboardButton(text="🎯 Похожее", callback_data=f"similar_{tmdb_id}_{type_}")])
            buttons.append([InlineKeyboardButton(text="⬅️ К результатам", callback_data="back_to_search_results")])
            buttons.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])

            keyboard = InlineKeyboardMarkup(inline_keyboard=buttons)

        if poster:
            await callback.message.answer_photo(
                photo=poster,
                caption=caption,
                reply_markup=keyboard,
                parse_mode="HTML"  # ← добавь эту строку
            )
        else:
            await callback.message.answer(
                text=caption,
                reply_markup=keyboard,
                parse_mode="HTML"  # ← добавь эту строку
            )

    # Кнопка "Похожее"
    if data.startswith("similar_"):
        parts = data.split("_")
        tmdb_id = int(parts[1])
        type_ = parts[2]

        # ПРОВЕРКА ЛИМИТА
        can_request, error_msg = await handle_search_request(chat_id, f"similar_{tmdb_id}_{type_}")
        if not can_request:
            await callback.answer(error_msg, show_alert=True)
            return

        # Сохраняем в сессию для рекомендаций
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}

        user_sessions[chat_id]["user_likes"] = [{"tmdb_id": tmdb_id, "type": type_}]
        user_sessions[chat_id]["type"] = "preferences"
        user_sessions[chat_id]["shown_recommendations"] = []  # очищаем список показанных

        await send_preference_item(chat_id, callback.message.message_id)
        return

    # Назад к результатам поиска
    # Назад к результатам поиска
    if data == "back_to_search_results":
        if "search_results" not in user_sessions.get(chat_id, {}):
            await callback.answer("❌ Результаты поиска устарели")
            return

        search_results = user_sessions[chat_id]["search_results"]
        search_query = user_sessions[chat_id].get("search_query", "")
        search_page = user_sessions[chat_id].get("search_page", 0)

        # УДАЛЯЕМ сообщение с карточкой фильма
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        await send_search_results_page(chat_id, search_results, search_query, search_page)
        return

    if data == "back_to_filmography":
        if "filmography" not in user_sessions.get(chat_id, {}):
            await callback.answer("❌ Фильмография не найдена")
            return

        filmography = user_sessions[chat_id]["filmography"]
        person_name = user_sessions[chat_id].get("filmography_person_name", "Актер")
        filmography_page = user_sessions[chat_id].get("filmography_page", 0)

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        text, keyboard = await send_person_filmography_page(
            chat_id, filmography, person_name, filmography_page
        )

        await bot.send_message(chat_id, text, reply_markup=keyboard)
        return

    # В handle_callback добавьте:
    if data == "back_to_person_results":
        if "person_results" not in user_sessions.get(chat_id, {}):
            await callback.answer("❌ Результаты поиска устарели")
            return

        person_results = user_sessions[chat_id]["person_results"]
        person_query = user_sessions[chat_id].get("person_query", "")
        person_page = user_sessions[chat_id].get("person_page", 0)

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        await send_person_results_page(chat_id, person_results, person_query, person_page)
        return

    if data == "back_to_person_list":
        if "person_results" not in user_sessions.get(chat_id, {}):
            await callback.answer("❌ Результаты поиска устарели")
            return

        person_results = user_sessions[chat_id]["person_results"]
        person_query = user_sessions[chat_id].get("person_query", "")
        person_page = user_sessions[chat_id].get("person_page", 0)

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        await send_person_results_page(chat_id, person_results, person_query, person_page)
        return

    # Просмотр профиля друга
    if data.startswith("friend_"):
        try:
            friend_tg_id = int(data.split("_")[1])

            # Получаем информацию о друге
            async with state.db.acquire() as conn:
                # Получаем основную информацию о друге
                friend = await conn.fetchrow("""
                    SELECT tg_id, username 
                    FROM users 
                    WHERE tg_id = $1
                """, friend_tg_id)

                if not friend:
                    await callback.answer("❌ Друг не найден")
                    return

                friend_name = friend['username'] or f"Пользователь {friend_tg_id}"

                # Получаем дату добавления в друзья
                friendship_data = await conn.fetchrow("""
                    SELECT created_at 
                    FROM user_friends 
                    WHERE user_id = (SELECT user_id FROM users WHERE tg_id = $1)
                    AND friend_user_id = (SELECT user_id FROM users WHERE tg_id = $2)
                """, chat_id, friend_tg_id)

                # Получаем статистику друга
                friend_stats = await conn.fetchrow("""
                    SELECT 
                        COUNT(CASE WHEN liked = TRUE THEN 1 END) as likes_count,
                        COUNT(CASE WHEN watched = TRUE THEN 1 END) as watched_count
                    FROM ratings 
                    WHERE user_id = (SELECT user_id FROM users WHERE tg_id = $1)
                """, friend_tg_id)

                likes_count = friend_stats['likes_count'] if friend_stats else 0
                watched_count = friend_stats['watched_count'] if friend_stats else 0

                # Форматируем время в друзьях
                if friendship_data and friendship_data['created_at']:
                    from datetime import datetime
                    created_at = friendship_data['created_at']
                    now = datetime.now()

                    # Точное вычисление разницы в годах, месяцах и днях
                    def calculate_time_diff(start_date, end_date):
                        years = end_date.year - start_date.year
                        months = end_date.month - start_date.month
                        days = end_date.day - start_date.day

                        # Корректируем отрицательные значения
                        if days < 0:
                            # Занимаем дни из предыдущего месяца
                            months -= 1
                            # Находим сколько дней в предыдущем месяце
                            if start_date.month == 1:
                                prev_month_days = 31  # Декабрь
                            else:
                                import calendar
                                prev_month_days = calendar.monthrange(start_date.year, start_date.month - 1)[1]
                            days += prev_month_days

                        if months < 0:
                            years -= 1
                            months += 12

                        return years, months, days

                    years, months, days = calculate_time_diff(created_at, now)

                    # Функция для правильного склонения дней
                    def format_days(days):
                        if days % 10 == 1 and days % 100 != 11:
                            return f"{days} день"
                        elif 2 <= days % 10 <= 4 and (days % 100 < 10 or days % 100 >= 20):
                            return f"{days} дня"
                        else:
                            return f"{days} дней"

                    # Функция для правильного склонения месяцев
                    def format_months(months):
                        if months % 10 == 1 and months % 100 != 11:
                            return f"{months} месяц"
                        elif 2 <= months % 10 <= 4 and (months % 100 < 10 or months % 100 >= 20):
                            return f"{months} месяца"
                        else:
                            return f"{months} месяцев"

                    # Функция для правильного склонения лет
                    def format_years(years):
                        if years % 10 == 1 and years % 100 != 11:
                            return f"{years} год"
                        elif 2 <= years % 10 <= 4 and (years % 100 < 10 or years % 100 >= 20):
                            return f"{years} года"
                        else:
                            return f"{years} лет"

                    # Форматируем красивый текст
                    if years == 0 and months == 0 and days == 0:
                        # Проверяем разницу в часах для "менее дня"
                        hours_diff = (now - created_at).total_seconds() / 3600
                        if hours_diff < 24:
                            friends_duration = "менее дня"
                        else:
                            friends_duration = format_days(days)
                    elif years == 0 and months == 0:
                        friends_duration = format_days(days)
                    elif years == 0:
                        if days == 0:
                            friends_duration = format_months(months)
                        else:
                            friends_duration = f"{format_months(months)} и {format_days(days)}"
                    else:
                        if months == 0 and days == 0:
                            friends_duration = format_years(years)
                        elif months == 0:
                            friends_duration = f"{format_years(years)} и {format_days(days)}"
                        elif days == 0:
                            friends_duration = f"{format_years(years)} и {format_months(months)}"
                        else:
                            friends_duration = f"{format_years(years)}, {format_months(months)} и {format_days(days)}"
                else:
                    friends_duration = "неизвестно"

                text = (
                    f"👤 Профиль друга\n\n"
                    f"📛 Имя: @{friend_name}\n"
                    f"👍 Лайков: {likes_count}\n"
                    f"🎬 Просмотрено: {watched_count}\n"
                    f"📅 В друзьях: {friends_duration}\n\n"
                )

                await navigate_to_menu(
                    chat_id, old_msg_id,
                    text,
                    kb_friend_profile(friend_tg_id)
                )

        except (ValueError, IndexError) as e:
            await callback.answer("❌ Ошибка при загрузке профиля друга")
            print(f"Error loading friend profile: {e}")
        return

    # Удаление друга
    if data.startswith("remove_friend_"):
        try:
            friend_tg_id = int(data.split("_")[2])

            # Получаем имя друга для сообщения
            async with state.db.acquire() as conn:
                friend = await conn.fetchrow("""
                    SELECT username 
                    FROM users 
                    WHERE tg_id = $1
                """, friend_tg_id)

                friend_name = friend['username'] or f"Пользователь {friend_tg_id}" if friend else "друг"

            # Удаляем друга
            success = await remove_friend(chat_id, friend_tg_id)

            if success:
                await callback.answer(f"❌ Друг {friend_name} удален")

                # Возвращаемся к списку друзей
                friends = await get_user_friends(chat_id)
                if not friends:
                    await navigate_to_menu(
                        chat_id, old_msg_id,
                        "📭 У вас пока нет друзей. Добавьте друзей, чтобы видеть их рекомендации!",
                        InlineKeyboardMarkup(inline_keyboard=[
                            [InlineKeyboardButton(text="➕ Добавить друга", callback_data="add_friend")],
                            [InlineKeyboardButton(text="⬅️ Назад", callback_data="friends_menu")]
                        ])
                    )
                else:
                    await navigate_to_menu(
                        chat_id, old_msg_id,
                        f"👥 Ваши друзья ({len(friends)}):",
                        kb_my_friends(friends, 0)
                    )
            else:
                await callback.answer("❌ Ошибка при удалении друга")

        except (ValueError, IndexError) as e:
            await callback.answer("❌ Ошибка при удалении друга")
            print(f"Error removing friend: {e}")
        return

    # Статус фильтров
    if data == "filters_status":
        current_filters = user_sessions.get(chat_id, {}).get("filters", {})
        filters_active = any(current_filters.values())

        if filters_active:
            filter_text = "Активные фильтры:\n"
            if current_filters.get('start_year') and current_filters.get('end_year'):
                if current_filters['start_year'] == current_filters['end_year']:
                    filter_text += f"• Год: {current_filters['start_year']}\n"
                else:
                    filter_text += f"• Года: {current_filters['start_year']}-{current_filters['end_year']}\n"
            if current_filters.get('country'):
                filter_text += f"• Страна: {current_filters['country']}\n"
            if current_filters.get('rating'):
                filter_text += f"• Рейтинг: {current_filters['rating']}+\n"
            filter_text += "\nФильтры применяются к:\n• Случайный поиск\n• Поиск по жанрам"
        else:
            filter_text = "❌ Фильтры не активны"

        await callback.answer(filter_text, show_alert=True)
        return

    # Обработчик кнопки года
    if data == "filter_year":
        # Устанавливаем флаг ожидания ввода диапазона
        if chat_id not in user_input_waiting:
            user_input_waiting[chat_id] = {}
        user_input_waiting[chat_id]["waiting_year"] = True

        # Удаляем текущее сообщение с меню
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        # Отправляем сообщение с инструкцией и сохраняем его ID
        msg = await callback.message.answer(
            "📅 Введите год или диапазон годов:\n\n"
            "• Один год: 2010\n"
            "• Диапазон: 2010-2020\n"
            "• От года: 2010-\n"
            "• До года: -2020\n\n"
            "❌ Чтобы убрать фильтр года, введите 'any'"
        )
        user_input_waiting[chat_id]["message_id"] = msg.message_id
        return

    # Добавь в handle_callback()

    # Проверка админских прав (замени на свою логику)


    # Админ-панель
    if data == "admin_panel":
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return
        await navigate_to_menu(chat_id, old_msg_id, "⚙️ Админ-панель:", kb_admin_panel())
        return

    # Поиск для бана
    if data == "admin_search_ban":
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        # Сохраняем состояние ожидания ввода
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        user_sessions[chat_id]["waiting_admin_search"] = True

        await callback.message.answer(
            "🔍 Введите название фильма или сериала для поиска:\n\n"
            "Можно уточнить тип:\n"
            "• 'интерстеллар movie' - только фильмы\n"
            "• 'breaking bad tv' - только сериалы\n"
            "• 'матрица' - все результаты"
        )
        return

    # Предпросмотр перед баном
    if data.startswith("admin_preban_"):
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        parts = data.split("_")
        tmdb_id = int(parts[2])
        type_ = parts[3]

        # Получаем детали
        details = get_item_details(type_, tmdb_id)
        if not details:
            await callback.answer("❌ Не удалось загрузить данные")
            return

        title = details.get("title") or details.get("name")
        year = (details.get("release_date") or details.get("first_air_date") or "")[:4]

        # Проверяем статус бана
        is_already_banned = await is_banned(tmdb_id, type_)

        if is_already_banned:
            caption = f"🎯 Контент ЗАБАНЕН:\n\n{title} ({year})\nID: {tmdb_id} | Тип: {type_}\n\nРазблокировать контент?"
        else:
            caption = f"🎯 Подтвердите бан:\n\n{title} ({year})\nID: {tmdb_id} | Тип: {type_}"

        await callback.message.answer(
            caption,
            reply_markup=await kb_ban_confirmation(tmdb_id, type_, title)  # не забудь await!
        )
        return

    if data.startswith("confirm_unban_"):
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        parts = data.split("_")
        tmdb_id = int(parts[2])
        type_ = parts[3]

        details = get_item_details(type_, tmdb_id)
        title = details.get("title") or details.get("name") or "Unknown"

        await unban_content(tmdb_id, type_)

        if type_ == "movie":
            await callback.answer(f"✅ Фильм {title} разбанен!")
        elif type_ == "tv":
            await callback.answer(f"✅ Сериал {title} разбанен!")
        else:
            await callback.answer(f"❌ Произошла ошибка!")

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass
        return

    # Подтверждение бана
    if data.startswith("confirm_ban_"):
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        parts = data.split("_")
        tmdb_id = int(parts[2])
        type_ = parts[3]

        # Получаем детали для названия
        details = get_item_details(type_, tmdb_id)
        title = details.get("title") or details.get("name") or "Unknown"

        await ban_content(tmdb_id, type_, title, chat_id, "Админ-бан")

        if type_ == "movie":
            await callback.answer(f"✅ Фильм {title} забанен!")
        elif type_ == "tv":
            await callback.answer(f"✅ Сериал {title} забанен!")
        else:
            await callback.answer(f"❌ Произошла ошибка!")

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass
        return

    # Список банов
    if data == "admin_ban_list":
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        banned_list = await get_banned_list(20)
        if not banned_list:
            await callback.message.answer(
                "📭 Список банов пуст",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
                ])
            )
            return

        text = "📋 Забаненный контент:\n\n"
        for item in banned_list:
            text += f"• {item['title']} (ID: {item['tmdb_id']})\n"

        text += f"\nВсего: {len(banned_list)}"

        await callback.message.answer(
            text,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="⬅️ Назад", callback_data="admin_panel")]
            ])
        )
        return

    # Разбан
    if data.startswith("unban_"):
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        parts = data.split("_")
        tmdb_id = int(parts[1])
        type_ = parts[2]

        await unban_content(tmdb_id, type_)
        await callback.answer("✅ Контент разбанен!")
        await navigate_to_menu(chat_id, old_msg_id, "⚙️ Админ-панель:", kb_admin_panel())
        return

    # Статистика админ-панели
    # Статистика админ-панели
    if data == "admin_stats":
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        # Сохраняем сортировку по умолчанию
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        user_sessions[chat_id]["current_stats_sort"] = "updated"

        # Загружаем первую страницу статистики
        stats_data = await get_ratings_stats(sort_by="updated", page=0)
        text = format_stats_page(stats_data, "updated", 0)

        await bot.send_message(
            chat_id,
            text,
            reply_markup=kb_admin_stats("updated", 0, stats_data["total_pages"]),
            parse_mode="HTML"
        )
        return

    # Смена сортировки статистики
    if data.startswith("stats_sort_"):
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        sort_by = data.split("_")[2]

        # Сохраняем сортировку в сессии
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        user_sessions[chat_id]["current_stats_sort"] = sort_by

        stats_data = await get_ratings_stats(sort_by=sort_by, page=0)
        text = format_stats_page(stats_data, sort_by, 0)

        await callback.message.edit_text(
            text,
            reply_markup=kb_admin_stats(sort_by, 0, stats_data["total_pages"]),
            parse_mode="HTML"
        )
        return

    # Смена страницы статистики
    if data.startswith("stats_page_"):
        await callback.answer()
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        parts = data.split("_")
        page = int(parts[2])
        sort_by = parts[3]

        # Сохраняем сортировку в сессии
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        user_sessions[chat_id]["current_stats_sort"] = sort_by

        stats_data = await get_ratings_stats(sort_by=sort_by, page=page)
        text = format_stats_page(stats_data, sort_by, page)

        await callback.message.edit_text(
            text,
            reply_markup=kb_admin_stats(sort_by, page, stats_data["total_pages"]),
            parse_mode="HTML"
        )
        return

    # Информация о статистике
    if data == "stats_info":
        await callback.answer("ℹ️ Используйте кнопки для сортировки и навигации", show_alert=True)
        return


    # Переключение страны
    if data == "filter_country":
        # Устанавливаем флаг ожидания ввода
        if chat_id not in user_input_waiting:
            user_input_waiting[chat_id] = {}
        user_input_waiting[chat_id]["waiting_country"] = True

        # Удаляем текущее сообщение с меню
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        # Отправляем сообщение с инструкцией и сохраняем его ID
        msg = await callback.message.answer(
            "🌍 Введите название страны на английском (например: RU, US, FR):\n\n"
            "❌ Чтобы убрать фильтр страны, введите 'any'"
        )
        user_input_waiting[chat_id]["message_id"] = msg.message_id
        return

    if data == "filter_rating":
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass
        await callback.message.answer("Выберите рейтинг:", reply_markup=kb_rating_selection())
        return

    # Установка года
    if data.startswith("set_year_"):
        year = int(data.split("_")[2])

        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        if "filters" not in user_sessions[chat_id]:
            user_sessions[chat_id]["filters"] = {}

        user_sessions[chat_id]["filters"]["year"] = year
        await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
        await callback.answer(f"✅ Год установлен: {year}")

        # Возвращаемся к меню фильтров
        current_filters = user_sessions[chat_id]["filters"]
        await navigate_to_menu(chat_id, old_msg_id, "Настройте фильтры поиска:", kb_filters_menu(current_filters))
        return

    # Установка рейтинга
    if data.startswith("set_rating_"):
        rating = float(data.split("_")[2])

        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        if "filters" not in user_sessions[chat_id]:
            user_sessions[chat_id]["filters"] = {}

        user_sessions[chat_id]["filters"]["rating"] = rating
        await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
        await callback.answer(f"✅ Рейтинг установлен: {rating}+")

        current_filters = user_sessions[chat_id]["filters"]
        await navigate_to_menu(chat_id, old_msg_id, "Настройте фильтры поиска:", kb_filters_menu(current_filters))
        return

    # Переключение страны
    if data == "filter_country":
        # Устанавливаем флаг ожидания ввода
        if chat_id not in user_input_waiting:
            user_input_waiting[chat_id] = {}
        user_input_waiting[chat_id]["waiting_country"] = True

        # Удаляем текущее сообщение с меню
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        # Отправляем сообщение с инструкцией и сохраняем его ID
        msg = await callback.message.answer(
            "🌍 Введите название страны на английском (например: RU, US, FR):\n\n"
            "❌ Чтобы убрать фильтр страны, введите 'any'"
        )
        user_input_waiting[chat_id]["message_id"] = msg.message_id
        return

    # Очистка фильтров
    if data == "clear_year":
        if chat_id in user_sessions and "filters" in user_sessions[chat_id]:
            user_sessions[chat_id]["filters"]["year"] = None
            await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
        await callback.answer("✅ Год сброшен")

        current_filters = user_sessions[chat_id]["filters"]
        await navigate_to_menu(chat_id, old_msg_id, "Настройте фильтры поиска:", kb_filters_menu(current_filters))
        return

    if data == "clear_rating":
        if chat_id in user_sessions and "filters" in user_sessions[chat_id]:
            user_sessions[chat_id]["filters"]["rating"] = None
            await save_search_filters(chat_id, user_sessions[chat_id]["filters"])
        await callback.answer("✅ Рейтинг сброшен")

        current_filters = user_sessions[chat_id]["filters"]
        await navigate_to_menu(chat_id, old_msg_id, "Настройте фильтры поиска:", kb_filters_menu(current_filters))
        return

    # Сброс всех фильтров
    if data == "reset_all_filters":
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass
        if chat_id in user_sessions and "filters" in user_sessions[chat_id]:
            user_sessions[chat_id]["filters"] = {}
            await clear_search_filters(chat_id)
        await callback.answer("✅ Все фильтры сброшены!")
        current_filters = user_sessions.get(chat_id, {}).get("filters", {})
        await callback.message.answer("Настройте фильтры поиска:", reply_markup=kb_filters_menu(current_filters))
        return

    # Меню трендов
    if data == "trending_menu":
        await navigate_to_menu(chat_id, old_msg_id, "Что интересует?", kb_trending_menu())
        return

    # Трендовые фильмы за неделю
    if data == "trending_movie_week":
        # ПРОВЕРКА ЛИМИТА
        can_request, error_msg = await handle_search_request(chat_id, "trending_movie")
        if not can_request:
            await callback.answer(error_msg, show_alert=True)
            return

        items = get_trending("movie", "week")
        if not items:
            await callback.answer("Не удалось получить трендовые фильмы", show_alert=True)
            return

        user_sessions[chat_id] = {
            "results": items,
            "index": 0,
            "type": "movie",
            "mode": "trending"
        }
        await send_card(chat_id, old_msg_id)
        return
    # Трендовые сериалы за неделю
    if data == "trending_tv_week":
        # ПРОВЕРКА ЛИМИТА
        can_request, error_msg = await handle_search_request(chat_id, "trending_tv")
        if not can_request:
            await callback.answer(error_msg, show_alert=True)
            return

        items = get_trending("tv", "week")
        if not items:
            await callback.answer("Не удалось получить трендовые сериалы", show_alert=True)
            return

        user_sessions[chat_id] = {
            "results": items,
            "index": 0,
            "type": "tv",
            "mode": "trending"
        }
        await send_card(chat_id, old_msg_id)
        return

    # Главное меню поиска
    if data == "search_menu":
        await navigate_to_menu(chat_id, old_msg_id, "Выберите тип поиска:", kb_search_menu())
        return

    # Меню случайного поиска
    if data == "random_search":
        await navigate_to_menu(chat_id, old_msg_id, "Выберите что искать:", kb_random_search())
        return

    # Подтверждение очистки коллекции
    if data == "confirm_clear_collection":
        await callback.message.edit_text(
            "⚠️ <b>Подтверждение очистки коллекции</b>\n\n"
            "Вы уверены, что хотите полностью очистить вашу коллекцию?\n"
            "Это действие нельзя отменить!\n\n"
            "Все фильмы и сериалы будут удалены.",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🗑️ Да, очистить", callback_data="clear_collection")],
                [InlineKeyboardButton(text="❌ Отмена", callback_data="show_collection")]
            ])
        )
        return

    # Очистка коллекции
    if data == "clear_collection":
        success = await clear_user_collection(chat_id)

        if success:
            await callback.answer("✅ Коллекция очищена!")
            await navigate_to_menu(
                chat_id, old_msg_id,
                "🗑️ Ваша коллекция была полностью очищена.",
                InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
                ])
            )
        else:
            await callback.answer("❌ Ошибка при очистке коллекции")
        return

    # Импорт коллекции
    if data == "import_collection":
        # Сохраняем состояние ожидания файла
        if chat_id not in user_sessions:
            user_sessions[chat_id] = {}
        user_sessions[chat_id]["waiting_import_file"] = True

        await callback.message.edit_text(
            "📥 <b>Импорт коллекции</b>\n\n"
            "Отправьте CSV файл с вашей коллекцией.\n\n"
            "<b>Формат файла:</b>\n"
            "• Должен быть экспортирован из этого бота\n"
            "• Поддерживаются только CSV файлы\n"
            "• Будут импортированы только новые элементы\n\n"
            "❌ Для отмены нажмите кнопку ниже",
            parse_mode="HTML",
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="❌ Отмена", callback_data="show_collection")]
            ])
        )
        return

    # Меню экспорта
    if data == "export_menu":
        await callback.message.edit_reply_markup(
            reply_markup=kb_export_options()
        )
        return

    # Экспорт в CSV
    if data == "export_csv":
        chat_id = callback.message.chat.id
        requests_info = await get_requests_info(chat_id)

        # Проверяем подписку
        if not requests_info["has_subscription"]:
            await callback.answer(
                "❌ Экспорт коллекции доступен только с подпиской!\n\n"
                "💫 Подписка откроет все возможности бота!",
                show_alert=True
            )
            return

        await callback.answer("🔄 Создаю CSV...")

        # Создаем CSV
        csv_buffer = await generate_collection_csv(chat_id)

        if not csv_buffer:
            await callback.answer("❌ Коллекция пуста!", show_alert=True)
            return

        # Отправляем файл пользователю
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=types.BufferedInputFile(
                    csv_buffer.getvalue(),
                    filename="my_collection.csv"
                ),
                caption="📊 Ваша коллекция в формате CSV"
            )
            await callback.answer("✅ CSV готов!")
        except Exception as e:
            await callback.answer("❌ Ошибка при создании CSV", show_alert=True)
            print(f"CSV export error: {e}")

        # Возвращаемся к коллекции
        total_items = await get_collection_count(chat_id)
        total_pages = (total_items + 3) // 4
        keyboard = await kb_collection(chat_id, 0, total_pages)

        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except:
            pass
        return

    # Назад к коллекции из меню экспорта
    if data == "show_collection":
        chat_id = callback.message.chat.id
        total_items = await get_collection_count(chat_id)
        total_pages = (total_items + 3) // 4
        keyboard = await kb_collection(chat_id, 0, total_pages)

        await callback.message.edit_reply_markup(reply_markup=keyboard)
        return

    # Добавляем в handle_callback
    if data == "export_pdf":
        chat_id = callback.message.chat.id
        requests_info = await get_requests_info(chat_id)

        # Проверяем подписку
        if not requests_info["has_subscription"]:
            await callback.answer(
                "❌ Экспорт коллекции доступен только с подпиской!\n\n"
                "💫 Подписка откроет все возможности бота!",
                show_alert=True
            )
            return

        await callback.answer("🔄 Создаю PDF...")

        # Создаем PDF
        pdf_buffer = await generate_collection_pdf(chat_id)

        if not pdf_buffer:
            await callback.answer("❌ Коллекция пуста!", show_alert=True)
            return

        # Отправляем файл пользователю
        try:
            await bot.send_document(
                chat_id=chat_id,
                document=types.BufferedInputFile(
                    pdf_buffer.getvalue(),
                    filename="my_collection.pdf"
                ),
                caption="📚 Ваша коллекция фильмов и сериалов"
            )
            await callback.answer("✅ PDF готов!")
        except Exception as e:
            await callback.answer("❌ Ошибка при создании PDF", show_alert=True)
            print(f"PDF export error: {e}")

        # Возвращаемся к коллекции
        total_items = await get_collection_count(chat_id)
        total_pages = (total_items + 3) // 4
        keyboard = await kb_collection(chat_id, 0, total_pages)

        try:
            await callback.message.edit_reply_markup(reply_markup=keyboard)
        except:
            pass
        return

    # Экспорт статистики в PDF
    if data == "stats_export_pdf":
        await callback.answer("🔄 Генерирую PDF...")
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        try:
            # Получаем текущую сортировку из сессии ИЛИ используем ту, что сейчас отображается
            current_sort = user_sessions.get(chat_id, {}).get("current_stats_sort", "updated")

            # Если нет в сессии, пробуем определить из текущего сообщения
            if "current_stats_sort" not in user_sessions.get(chat_id, {}):
                # Парсим текущую сортировку из сообщения
                message_text = callback.message.text
                if "Сортировка: по лайкам" in message_text:
                    current_sort = "likes"
                elif "Сортировка: по дизлайкам" in message_text:
                    current_sort = "dislikes"
                elif "Сортировка: по просмотрам" in message_text:
                    current_sort = "watches"
                else:
                    current_sort = "updated"

            stats_data = await get_ratings_stats(sort_by=current_sort, page=0, limit=1000)

            pdf_buffer = await generate_stats_pdf(stats_data, current_sort)

            if pdf_buffer:
                from datetime import datetime
                await bot.send_document(
                    chat_id=chat_id,
                    document=types.BufferedInputFile(
                        pdf_buffer.getvalue(),
                        filename=f"statistics_{current_sort}_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
                    ),
                    caption=f"📊 Статистика контента (сортировка: {current_sort})"
                )
                await callback.answer("✅ PDF готов!")
            else:
                await callback.answer("❌ Ошибка при создании PDF", show_alert=True)

        except Exception as e:
            await callback.answer("❌ Ошибка при создании PDF", show_alert=True)
            print(f"Stats PDF export error: {e}")
        return

    # Диаграммы статистики в PDF
    if data == "stats_charts_pdf":
        await callback.answer("🔄 Генерирую диаграммы...")
        if not is_admin(chat_id):
            await callback.answer("❌ Нет доступа!")
            return

        try:
            stats_data = await get_ratings_stats(sort_by="likes", page=0, limit=1000)

            pdf_buffer = await generate_stats_charts_pdf(stats_data)

            if pdf_buffer:
                from datetime import datetime  # Добавляем импорт здесь
                await bot.send_document(
                    chat_id=chat_id,
                    document=types.BufferedInputFile(
                        pdf_buffer.getvalue(),
                        filename=f"charts_{datetime.now().strftime('%Y%m%d_%H%M')}.pdf"
                    ),
                    caption="📊 Диаграммы статистики"
                )
                await callback.answer("✅ Диаграммы готовы!")
            else:
                await callback.answer("❌ Ошибка при создании диаграмм", show_alert=True)

        except Exception as e:
            await callback.answer("❌ Ошибка при создании диаграмм", show_alert=True)
            print(f"Charts PDF export error: {e}")
        return

    # В handle_callback добавить:
    # В обработчике preferences заменим:
    if data == "preferences":
        # ПРОВЕРКА ЛИМИТА
        can_request, error_msg = await handle_search_request(chat_id, "preferences")
        if not can_request:
            await callback.answer(error_msg, show_alert=True)
            return

        user_likes = await get_user_likes(chat_id)
        if not user_likes:
            await callback.answer("❌ Сначала поставьте лайки некоторым фильмам/сериалам!")
            return

        # Сохраняем лайки пользователя в сессию
        user_sessions[chat_id] = {
            "user_likes": user_likes,
            "type": "preferences"
        }
        await send_preference_item(chat_id, old_msg_id)
        return

    # Обработчик кнопки "Следующая рекомендация"
    if data == "next_preference":
        # ПРОВЕРКА ЛИМИТА
        can_request, error_msg = await handle_search_request(chat_id, "next_preference")
        if not can_request:
            await callback.answer(error_msg, show_alert=True)
            return

        await send_preference_item(chat_id, old_msg_id)
        return



    if data == "toggle_watched":
        filters = await get_user_filters(chat_id)
        new_value = not filters["exclude_watched"]
        await update_user_filter(chat_id, "hide_watched", new_value)
        filters = await get_user_filters(chat_id)
        user_filters[chat_id] = filters
        await callback.message.edit_reply_markup(reply_markup=kb_settings(filters))
        return

    # Назад в главное меню
    if data == "back_to_main":
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except Exception:
            pass
        await bot.send_message(chat_id, "Выберите, что хотите получить:", reply_markup=kb_main())
        return

    # Настройки
    if data == "settings":
        filters = await get_user_filters(chat_id)
        await navigate_to_menu(chat_id, old_msg_id, "Настройки фильтров:", kb_settings(filters))
        return

    if data == "toggle_anime":
        filters = await get_user_filters(chat_id)
        new_value = not filters["exclude_anime"]
        await update_user_filter(chat_id, "disable_anime", new_value)
        filters = await get_user_filters(chat_id)
        user_filters[chat_id] = filters
        await callback.message.edit_reply_markup(reply_markup=kb_settings(filters))
        return

    if data == "toggle_cartoons":
        filters = await get_user_filters(chat_id)
        new_value = not filters["exclude_cartoons"]
        await update_user_filter(chat_id, "disable_cartoons", new_value)
        filters = await get_user_filters(chat_id)
        user_filters[chat_id] = filters
        await callback.message.edit_reply_markup(reply_markup=kb_settings(filters))
        return

    # Случайный фильм/сериал (с применением фильтров)
    if data in ("discover_movie", "discover_tv"):
        type_ = "movie" if data == "discover_movie" else "tv"

        # Получаем активные фильтры пользователя
        current_filters = await get_current_filters(chat_id)

        items = await discover_tmdb(type_, filters=current_filters)  # ДОБАВЬ await
        if user_filters.get(chat_id, {}).get("exclude_watched"):
            items = await filter_watched_items(chat_id, items, type_)
        if not items:
            await callback.message.answer("Не удалось получить данные.")
            return
        user_sessions[chat_id] = {
            "results": items,
            "index": 0,
            "type": type_,
            "mode": "random"
        }
        await send_card(chat_id, old_msg_id)
        return

    # Поиск по жанрам (с применением фильтров)
    if data == "search_genre":
        await navigate_to_menu(chat_id, old_msg_id, "Выберите тип:",
                               InlineKeyboardMarkup(inline_keyboard=[
                                   [InlineKeyboardButton(text="🎬 Фильмы", callback_data="genre_type_movie")],
                                   [InlineKeyboardButton(text="📺 Сериалы", callback_data="genre_type_tv")],
                                   [InlineKeyboardButton(text="⬅️ Назад", callback_data="search_menu")],
                               ]))
        return

    if data in ("genre_type_movie", "genre_type_tv"):
        type_ = "movie" if data == "genre_type_movie" else "tv"
        await navigate_to_menu(chat_id, old_msg_id, "Выберите жанр:", kb_genres(type_))
        return

    # Обновленный обработчик для выбора жанра (с применением фильтров)
    if data.startswith("genre_"):
        parts = data.split("_")
        if len(parts) < 3:
            await callback.answer("Ошибка: некорректные данные.")
            return
        type_ = parts[1]
        gid = int(parts[2])

        # ПРОВЕРКА ЛИМИТА
        can_request, error_msg = await handle_search_request(chat_id, f"genre_{type_}_{gid}")
        if not can_request:
            await callback.answer(error_msg, show_alert=True)
            return

        # Получаем активные фильтры пользователя
        current_filters = await get_current_filters(chat_id)

        items = await discover_tmdb(type_, genre_id=gid, filters=current_filters)
        if not items:
            await callback.message.answer("По этому жанру ничего не найдено.")
            return
        user_sessions[chat_id] = {
            "results": items,
            "index": 0,
            "type": type_,
            "genre_id": gid,
            "mode": "genre"
        }
        await send_card(chat_id, old_msg_id)
        return

    # Обновленный обработчик для кнопки "Следующий"
    if data == "next_item":
        session = user_sessions.get(chat_id)
        if not session or "results" not in session:
            await callback.answer("Сначала сделайте выбор.")
            return

        # ПРОВЕРКА ЛИМИТА для следующего элемента (только если это не первый элемент в сессии)
        if session.get("index", 0) > 0:  # Если это не первый элемент
            can_request, error_msg = await handle_search_request(chat_id, f"next_{session.get('mode', 'unknown')}")
            if not can_request:
                await callback.answer(error_msg, show_alert=True)
                return

        session["index"] += 1
        is_genre_search = session.get("mode") == "genre"
        await send_card(chat_id, old_msg_id)
        return

    # Обновленный обработчик для кнопки "Назад к жанрам"
    if data.startswith("back_to_genres_"):
        type_ = data.split("_")[-1]
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except Exception:
            pass
        await bot.send_message(chat_id, "Выберите жанр:", reply_markup=kb_genres(type_))
        return

    # Коллекция
    if data == "show_collection":
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except Exception:
            pass
        total_items = await get_collection_count(chat_id)
        total_pages = (total_items + 3) // 4  # Округление вверх
        keyboard = await kb_collection(chat_id, 0, total_pages)
        if total_pages == 0:
            await bot.send_message(
                chat_id,
                "Коллекция пуста.",
                reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                    [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
                ])
            )
        else:
            await bot.send_message(chat_id, "📚 Ваша коллекция:", reply_markup=keyboard)
        return

    if data.startswith("collection_page_"):
        page = int(data.split("_")[2])
        total_items = await get_collection_count(chat_id)
        total_pages = (total_items + 3) // 4  # Округление вверх
        keyboard = await kb_collection(chat_id, page, total_pages)
        try:
            await bot.edit_message_reply_markup(chat_id=chat_id, message_id=old_msg_id, reply_markup=keyboard)
        except Exception:
            pass
        return

    if data.startswith("show_collection_item_"):
        parts = data.split("_")
        if len(parts) < 4:
            await callback.answer("Ошибка: некорректные данные.")
            return
        try:
            tmdb_id = int(parts[3])
            type_ = parts[4]
        except (ValueError, IndexError) as e:
            await callback.answer("Ошибка: некорректный ID.")
            return

        details = get_item_details(type_, tmdb_id)
        if not details:
            await callback.message.answer("Не удалось загрузить данные.")
            return

        title = details.get("title") or details.get("name") or "Без названия"
        year = (details.get("release_date") or details.get("first_air_date") or "")[:4]
        rating = details.get("vote_average") or "—"
        overview = details.get("overview") or "Описание отсутствует."
        poster = f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get("poster_path") else None
        avg_ratings = await get_ratings(tmdb_id, type_)

        user_rating = await get_user_rating(chat_id, tmdb_id, type_)
        watched = user_rating["watched"] if user_rating else False
        liked = user_rating["liked"] if user_rating else None
        disliked = user_rating["disliked"] if user_rating else None
        is_hidden = user_rating["is_hidden"] if user_rating else False  # Получаем статус скрытия



        def create_safe_caption(title, year, rating, avg_ratings, overview):
            """Создает caption гарантированно не длиннее 1024 символов"""
            base_info = f"{title} ({year})\n⭐ {rating} | 👍 {avg_ratings['likes']} | 👎 {avg_ratings['dislikes']} | 👀 {avg_ratings['watches']}"

            if watched:
                base_info += "\n\n✅ Вы смотрели"

            if is_hidden:
                base_info += "\n🙈 Скрыто от друзей"

            base_info += "\n\n"

            # Максимальная длина для Telegram caption
            max_total = 1024
            available = max_total - len(base_info) - 3  # -3 для "..."

            if available <= 50:  # Если почти нет места для описания
                return base_info.strip()

            if len(overview) > available:
                overview = overview[:available] + "..."

            return base_info + overview

        # Используйте так:
        caption = create_safe_caption(title, year, rating, avg_ratings, overview)

        # Добавляем информацию о провайдерах для РФ (если есть)
        try:
            prov = get_providers_ru(type_, tmdb_id)
            if prov:
                prov_text = f"\n\nГде смотреть: {prov}"
                if len(caption) + len(prov_text) <= 1024:
                    caption += prov_text
        except Exception:
            pass


        try:
            await bot.delete_message(chat_id, old_msg_id)
        except Exception:
            pass

        if poster:
            await bot.send_photo(chat_id, photo=poster, caption=caption,
                                 reply_markup=kb_collection_item(tmdb_id, type_, watched, liked, disliked, is_hidden))
        else:
            await bot.send_message(chat_id, text=caption,
                                   reply_markup=kb_collection_item(tmdb_id, type_, watched, liked, disliked, is_hidden))
        return

    # Добавить в коллекцию
    # Добавить в коллекцию
    if data.startswith("add_"):
        parts = data.split("_")
        if len(parts) < 3:
            await callback.answer("Ошибка: некорректные данные.")
            return

        tmdb_id = int(parts[1])
        type_ = parts[2]

        # НЕ ИСПОЛЬЗУЕМ СЕССИЮ ИЗ ПОИСКА ПО НАЗВАНИЮ
        # Вместо этого получаем детали напрямую по tmdb_id
        details = get_item_details(type_, tmdb_id)
        if not details:
            await callback.answer("Ошибка: не удалось получить данные.")
            return

        title = details.get("title") or details.get("name") or "Без названия"
        year = (details.get("release_date") or details.get("first_air_date") or "")[:4]
        poster_path = details.get("poster_path") or "/default.jpg"

        success = await add_to_collection(chat_id, tmdb_id, type_, title, year, poster_path)
        if success:
            await callback.answer("✅ Добавлено в коллекцию!")
        else:
            await callback.answer("❌ Ошибка при добавлении.")
        return

    # Удалить из коллекции
    if data.startswith("remove_"):
        parts = data.split("_")
        if len(parts) < 3:
            await callback.answer("Ошибка: некорректные данные.")
            return
        tmdb_id = int(parts[1])
        type_ = parts[2]
        success = await remove_from_collection(chat_id, tmdb_id, type_)
        if success:
            await callback.answer("Удалено из коллекции!")
            try:
                await bot.delete_message(chat_id, callback.message.message_id)
            except Exception:
                pass
            total_items = await get_collection_count(chat_id)
            total_pages = (total_items + 3) // 4
            if total_pages == 0:
                await bot.send_message(
                    chat_id,
                    "Коллекция пуста.",
                    reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                        [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
                    ])
                )
            else:
                keyboard = await kb_collection(chat_id, 0, total_pages)
                await bot.send_message(chat_id, "📚 Ваша коллекция:", reply_markup=keyboard)
        else:
            await callback.answer("Ошибка при удалении.")
        return

    # Просмотрено
    if data.startswith("like_"):
        await handle_rating(callback, "like")
        return
    if data.startswith("dislike_"):
        await handle_rating(callback, "dislike")
        return
    if data.startswith("reset_rating_"):
        parts = data.split("_")
        if len(parts) < 4:
            await callback.answer("Ошибка: некорректные данные.")
            return
        try:
            tmdb_id = int(parts[2])
            type_ = parts[3]
            await handle_rating(callback, "reset", tmdb_id, type_)
        except (ValueError, IndexError):
            await callback.answer("Ошибка: некорректные данные.")
        return

    if data.startswith("mark_watched_"):
        await handle_rating(callback, "watch")
        return

    # Обработчик скрытия/показа оценки
    if data.startswith("toggle_hide_"):
        parts = data.split("_")
        if len(parts) < 4:
            await callback.answer("Ошибка: некорректные данные.")
            return

        tmdb_id = int(parts[2])
        type_ = parts[3]

        details = get_item_details(type_, tmdb_id)
        title = details.get("title") or details.get("name") or "Без названия"

        # Получаем текущий статус
        user_rating = await get_user_rating(chat_id, tmdb_id, type_)
        current_hidden = user_rating["is_hidden"] if user_rating else False

        # Переключаем статус
        new_hidden = not current_hidden

        # Обновляем оценку (ТОЛЬКО is_hidden, сохраняем текущие лайки)
        await add_rating(
            chat_id,
            tmdb_id,
            type_,
            is_hidden=new_hidden,
            title=title,
            # ЯВНО передаем текущие значения лайков, чтобы они не сбрасывались
            liked=user_rating.get('liked') if user_rating else None,
            disliked=user_rating.get('disliked') if user_rating else None,
            watched=user_rating.get('watched') if user_rating else None
        )

        if new_hidden:
            await callback.answer("🙈 Оценка скрыта от друзей")
        else:
            await callback.answer("👀 Оценка видна друзьям")

        # Обновляем интерфейс
        user_rating_updated = await get_user_rating(chat_id, tmdb_id, type_)
        keyboard = kb_collection_item(
            tmdb_id,
            type_,
            user_rating_updated.get('watched', False),
            user_rating_updated.get('liked'),
            user_rating_updated.get('disliked'),
            user_rating_updated.get('is_hidden', False)
        )

        await callback.message.edit_reply_markup(reply_markup=keyboard)
        return

async def clear_user_collection(tg_id: int):
    """Полностью очищает коллекцию пользователя"""
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return False

        # Удаляем все элементы коллекции
        await conn.execute("DELETE FROM collection WHERE user_id = $1", user["user_id"])
        return True

async def import_collection_from_csv(tg_id: int, csv_content: str):
    """Импортирует коллекцию из CSV файла"""
    import csv
    import io

    imported_count = 0
    skipped_count = 0
    errors = []

    try:
        # Читаем CSV файл
        csv_file = io.StringIO(csv_content)
        reader = csv.DictReader(csv_file, delimiter=';')

        for row_num, row in enumerate(reader, 1):
            try:
                # Получаем параметры из строки
                title = row.get('Название', '').strip()
                year = row.get('Год', '').strip()
                content_type = row.get('Тип', '').strip().upper()
                tmdb_id_str = row.get('ID', '').strip()
                watched_status = row.get('Просмотрено', '').strip().upper()
                rating_status = row.get('Оценка', '').strip().upper()

                # Валидация обязательных полей
                if not title or not tmdb_id_str:
                    errors.append(f"Строка {row_num}: Отсутствует название или ID")
                    continue

                # Проверяем тип контента
                if content_type not in ['MOVIE', 'TV']:
                    errors.append(f"Строка {row_num}: Некорректный тип '{content_type}'")
                    continue

                # Преобразуем ID
                try:
                    tmdb_id = int(tmdb_id_str)
                except ValueError:
                    errors.append(f"Строка {row_num}: Некорректный ID '{tmdb_id_str}'")
                    continue

                # Проверяем, есть ли уже в коллекции
                if await is_in_user_collection(tg_id, tmdb_id, content_type.lower()):
                    skipped_count += 1
                    continue

                # Получаем детали из TMDB для проверки существования
                details = get_item_details(content_type.lower(), tmdb_id)
                if not details:
                    errors.append(f"Строка {row_num}: Контент не найден в TMDB (ID: {tmdb_id})")
                    continue

                # Получаем постер
                poster_path = details.get('poster_path') or "/default.jpg"

                # Добавляем в коллекцию
                success = await add_to_collection(tg_id, tmdb_id, content_type.lower(), title, year, poster_path)

                if success:
                    imported_count += 1

                    # Устанавливаем статус просмотра и оценку
                    watched = (watched_status == 'TRUE')
                    liked = (rating_status == 'LIKE')
                    disliked = (rating_status == 'DISLIKE')

                    if watched or liked or disliked:
                        await add_rating(
                            tg_id,
                            tmdb_id,
                            content_type.lower(),
                            watched=watched,
                            liked=liked if liked else None,
                            disliked=disliked if disliked else None,
                            title=title
                        )

            except Exception as e:
                errors.append(f"Строка {row_num}: Ошибка обработки - {str(e)}")
                continue

    except Exception as e:
        errors.append(f"Ошибка чтения CSV файла: {str(e)}")

    return {
        'imported': imported_count,
        'skipped': skipped_count,
        'errors': errors
    }


async def generate_collection_csv(tg_id: int):
    """Генерирует CSV файл с коллекцией в формате таблицы"""
    collection = await get_collection(tg_id, limit=1000, offset=0)

    print(f"DEBUG: collection type = {type(collection)}")  # Для отладки

    # Если collection уже список, используем его напрямую
    if isinstance(collection, list):
        collection_list = collection
    else:
        # Если это async generator, преобразуем в список
        try:
            collection_list = []
            async for item in collection:
                collection_list.append(item)
        except TypeError:
            # Если это не async generator, а обычный итератор
            collection_list = list(collection)

    if not collection_list:
        return None

    import csv
    import io

    # Создаем CSV в памяти
    output = io.StringIO()
    writer = csv.writer(output, delimiter=';', quotechar='"', quoting=csv.QUOTE_MINIMAL)

    # Только заголовки столбцов (без дополнительной строки с A,B,C,D,E,F,G)
    writer.writerow(['Название', 'Год', 'Тип', 'ID', 'Дата добавления', 'Просмотрено', 'Оценка'])

    for item in collection_list:
        # Получаем информацию о просмотре и оценке
        user_rating = await get_user_rating(tg_id, item['tmdb_id'], item['type'])

        # Столбец F: Статус просмотра (TRUE/FALSE)
        watched_status = 'TRUE' if user_rating and user_rating['watched'] else 'FALSE'

        # Столбец G: Оценка (лайк/дизлайк/пусто)
        rating_status = ''
        if user_rating:
            if user_rating['liked']:
                rating_status = 'LIKE'
            elif user_rating['disliked']:
                rating_status = 'DISLIKE'

        # Столбец C: Тип контента
        content_type = 'MOVIE' if item['type'] == 'movie' else 'TV'

        # Столбец E: Дата добавления
        added_date = item['added_at'].strftime("%d.%m.%Y") if item['added_at'] else ''

        # Записываем строку с данными
        writer.writerow([
            item['title'],  # A: Название
            item['year'] or '',  # B: Год
            content_type,  # C: Тип (MOVIE/TV)
            item['tmdb_id'],  # D: ID
            added_date,  # E: Дата добавления
            watched_status,  # F: Просмотрено (TRUE/FALSE)
            rating_status  # G: Оценка (LIKE/DISLIKE/пусто)
        ])

    # Конвертируем в bytes
    csv_content = output.getvalue().encode('utf-8-sig')  # utf-8-sig для корректного отображения кириллицы в Excel
    output.close()

    return io.BytesIO(csv_content)


async def generate_collection_pdf(tg_id: int):
    has_russian_font = register_russian_font()
    collection = await get_collection(tg_id, limit=1000, offset=0)

    if not collection:
        return None

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
    pdf.drawString(50, height - 50, "Моя коллекция")
    pdf.setFont(font_normal, 10)

    # Статистика коллекции
    movie_count = sum(1 for item in collection if item['type'] == 'movie')
    tv_count = sum(1 for item in collection if item['type'] == 'tv')

    # Получаем статистику по просмотрам и оценкам
    watched_count = 0
    liked_count = 0
    disliked_count = 0

    for item in collection:
        user_rating = await get_user_rating(tg_id, item['tmdb_id'], item['type'])
        if user_rating:
            if user_rating['watched']:
                watched_count += 1
            if user_rating['liked']:
                liked_count += 1
            if user_rating['disliked']:
                disliked_count += 1

    pdf.drawString(50, height - 70, f"Всего: {len(collection)} элементов")
    pdf.drawString(50, height - 85, f"Дата генерации: {datetime.now().strftime('%d.%m.%Y %H:%M')}")

    y_position = height - 115

    for i, item in enumerate(collection):
        if y_position < 120:
            pdf.showPage()
            # Заголовок на новой странице
            y_position = height - 50

        item_height = 100
        center_line = y_position - (item_height / 2)

        # ПОСТЕР
        poster_width = 60
        poster_height = 80
        poster_x = width - 80
        poster_y = center_line - (poster_height / 2)

        if item['poster_path'] and item['poster_path'] != "/default.jpg":
            try:
                poster_url = f"https://image.tmdb.org/t/p/w154{item['poster_path']}"
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
        text_start_y = center_line + 25

        # Название
        pdf.setFont(font_bold, 12)
        title = item['title']
        if len(title) > 35:
            title = title[:32] + "..."
        pdf.drawString(50, text_start_y, title)

        # Год и тип
        pdf.setFont(font_normal, 10)
        type_text = "Фильм" if item['type'] == 'movie' else "Сериал"
        pdf.drawString(50, text_start_y - 15, f"Год: {item['year']} | {type_text}")

        # Дата добавления
        added_date = item['added_at'].strftime("%d.%m.%Y") if item['added_at'] else "N/A"
        pdf.drawString(50, text_start_y - 30, f"Добавлено: {added_date}")

        # ID элемента
        pdf.drawString(50, text_start_y - 45, f"ID: {item['tmdb_id']}")

        # Информация о просмотре и оценке - используем тот же шрифт что и для основного текста
        user_rating = await get_user_rating(tg_id, item['tmdb_id'], item['type'])

        if user_rating:
            status_parts = []
            if user_rating['watched']:
                status_parts.append("Просмотрено")
            if user_rating['liked']:
                status_parts.append("Понравилось")
            elif user_rating['disliked']:
                status_parts.append("Не понравилось")

            if status_parts:
                status_text = " | ".join(status_parts)
                # Используем тот же шрифт что работает для русского текста
                pdf.setFont(font_normal, 9)
                pdf.drawString(50, text_start_y - 60, status_text)

        y_position -= item_height

        # Разделитель
        if i < len(collection) - 1:
            pdf.line(50, y_position + 5, width - 50, y_position + 5)
            y_position -= 10

    pdf.save()
    buffer.seek(0)
    return buffer

async def navigate_to_menu(chat_id, old_msg_id, text, keyboard):
    """Универсальная функция для перехода между меню"""
    try:
        # Сначала удаляем старое сообщение
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

        # Затем отправляем новое
        await bot.send_message(chat_id, text, reply_markup=keyboard)

    except Exception as e:
        print(f"Ошибка в navigate_to_menu: {e}")
        # Если не получилось, просто отправляем новое сообщение
        await bot.send_message(chat_id, text, reply_markup=keyboard)


async def send_preference_item(chat_id, old_msg_id=None):
    session = user_sessions.get(chat_id)
    if not session or "user_likes" not in session:
        await bot.send_message(chat_id, "Ошибка сессии.")
        return

    # Инициализируем список показанных рекомендаций если его нет
    if "shown_recommendations" not in session:
        session["shown_recommendations"] = []

    # Случайно выбираем лайкнутый item
    liked_item = random.choice(session["user_likes"])

    # Получаем рекомендации
    recommendations = get_recommendations(liked_item["type"], liked_item["tmdb_id"])

    if not recommendations:
        # Если нет рекомендаций, пробуем другой лайкнутый item
        await send_preference_item(chat_id, old_msg_id)
        return

    # Фильтруем уже показанные рекомендации
    available_recommendations = [r for r in recommendations if r["id"] not in session["shown_recommendations"]]

    # Если все рекомендации уже показаны, очищаем список
    if not available_recommendations:
        session["shown_recommendations"] = []
        available_recommendations = recommendations

    # Случайно выбираем одну рекомендацию из доступных
    chosen_item = random.choice(available_recommendations)

    # Добавляем в список показанных
    session["shown_recommendations"].append(chosen_item["id"])

    # Проверяем, не забанен ли контент
    if await is_banned(chosen_item["id"], liked_item["type"]):
        print(
            f"DEBUG: Пропускаем забаненный контент в рекомендациях - ID: {chosen_item['id']}, Type: {liked_item['type']}")
        await send_preference_item(chat_id, old_msg_id)
        return

    # Получаем детали
    details = get_item_details(liked_item["type"], chosen_item["id"])
    if not details:
        await send_preference_item(chat_id, old_msg_id)
        return

    # Применяем фильтры пользователя
    filters = await get_user_filters(chat_id)
    filters = filters or {"exclude_anime": False, "exclude_cartoons": False, "exclude_watched": False}

    if filters["exclude_anime"] and is_anime_by_details(liked_item["type"], details, chosen_item):
        await send_preference_item(chat_id, old_msg_id)
        return

    if filters["exclude_cartoons"] and is_cartoons_by_details(liked_item["type"], details, chosen_item):
        await send_preference_item(chat_id, old_msg_id)
        return

    if filters["exclude_watched"]:
        user_rating = await get_user_rating(chat_id, chosen_item["id"], liked_item["type"])
        if user_rating and user_rating["watched"]:
            await send_preference_item(chat_id, old_msg_id)
            return

    # Формируем карточку
    title = details.get("title") or details.get("name") or "Без названия"
    year = (details.get("release_date") or details.get("first_air_date") or "")[:4]
    rating = details.get("vote_average") or "—"
    overview = details.get("overview") or "Описание отсутствует."
    if len(overview) > 2000:
        overview = overview[:2000] + "..."
    poster = f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get("poster_path") else None

    avg_ratings = await get_ratings(chosen_item["id"], liked_item["type"])
    user_rating = await get_user_rating(chat_id, chosen_item["id"], liked_item["type"])
    watched_text = "✅ Вы смотрели" if user_rating and user_rating["watched"] else ""

    def create_safe_caption(title, year, rating, avg_ratings, watched_text, overview):
        """Создает caption гарантированно не длиннее 1024 символов"""
        base_info = f"{title} ({year})\n⭐ {rating} | 👍 {avg_ratings['likes']} | 👎 {avg_ratings['dislikes']} | 👀 {avg_ratings['watches']}"
        if watched_text:
            base_info += f"\n{watched_text}"

        base_info += "\n\n"

        # Максимальная длина для Telegram caption
        max_total = 1024
        available = max_total - len(base_info) - 3  # -3 для "..."

        if available <= 50:  # Если почти нет места для описания
            return base_info.strip()

        if len(overview) > available:
            overview = overview[:available] + "..."

        return base_info + overview

    # Используйте так:
    caption = (
        f"<b>{title}</b> ({year})\n"
        f"⭐ {rating} | 👍{avg_ratings['likes']} | 👎{avg_ratings['dislikes']} | 👀{avg_ratings['watches']}"
    )
    if watched_text:
        caption += f"\n{watched_text}"

    caption += "\n\n"

    # Добавляем описание как раскрываемую цитату
    if overview and overview != "Описание отсутствует.":
        caption += f"<blockquote expandable>{overview}</blockquote>"
    else:
        caption += "Описание отсутствует."


    # Клавиатура для рекомендаций
    keyboard = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="➕ В коллекцию", callback_data=f"add_{chosen_item['id']}_{liked_item['type']}")],
        [InlineKeyboardButton(text="➡️ Следующая рекомендация", callback_data="next_preference")],
        [InlineKeyboardButton(text="🔍 Меню поиска", callback_data="search_menu")]
    ])

    if old_msg_id:
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except Exception:
            pass

    try:
        if poster:
            # Дополнительная проверка URL
            if poster.startswith('http') and len(poster) > 10:
                await bot.send_photo(chat_id, photo=poster, caption=caption, parse_mode="HTML", reply_markup=keyboard)
            else:
                # Если poster невалидный, отправляем текстовое сообщение
                await bot.send_message(chat_id, text=caption, parse_mode="HTML", reply_markup=keyboard)
        else:
            await bot.send_message(chat_id, text=caption, reply_markup=keyboard)
    except Exception as e:
        # Если ошибка при отправке фото, отправляем текстовое сообщение
        print(f"Ошибка при отправке фото: {e}")
        await bot.send_message(chat_id, text=caption, reply_markup=keyboard)


async def send_friend_request(from_tg_id: int, to_tg_id: int):
    """Отправляет заявку в друзья"""
    async with state.db.acquire() as conn:
        from_user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", from_tg_id)
        to_user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", to_tg_id)

        if not from_user or not to_user or from_user["user_id"] == to_user["user_id"]:
            return False

        # Проверяем, нет ли уже заявки
        existing = await conn.fetchrow("""
            SELECT 1 FROM friend_requests 
            WHERE from_user_id = $1 AND to_user_id = $2 AND status = 'pending'
        """, from_user["user_id"], to_user["user_id"])

        if existing:
            return "already_sent"

        await conn.execute("""
            INSERT INTO friend_requests (from_user_id, to_user_id, status)
            VALUES ($1, $2, 'pending')
            ON CONFLICT (from_user_id, to_user_id) DO UPDATE 
            SET status = 'pending', created_at = NOW()
        """, from_user["user_id"], to_user["user_id"])

        return True


async def accept_friend_request(request_id: int):
    """Принимает заявку в друзья"""
    async with state.db.acquire() as conn:
        # Получаем информацию о заявке
        request = await conn.fetchrow("""
            SELECT fr.*, u1.tg_id as from_tg_id, u2.tg_id as to_tg_id
            FROM friend_requests fr
            JOIN users u1 ON fr.from_user_id = u1.user_id
            JOIN users u2 ON fr.to_user_id = u2.user_id
            WHERE fr.request_id = $1
        """, request_id)

        if not request:
            return False

        # Обновляем статус заявки
        await conn.execute("""
            UPDATE friend_requests SET status = 'accepted' WHERE request_id = $1
        """, request_id)

        # Добавляем взаимную дружбу
        await conn.execute("""
            INSERT INTO user_friends (user_id, friend_user_id)
            VALUES ($1, $2), ($2, $1)
            ON CONFLICT (user_id, friend_user_id) DO NOTHING
        """, request["from_user_id"], request["to_user_id"])

        return request


async def get_pending_friend_requests(tg_id: int):
    """Получает входящие заявки в друзья"""
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
            JOIN users u ON fr.from_user_id = u.user_id
            WHERE fr.to_user_id = $1 AND fr.status = 'pending'
            ORDER BY fr.created_at DESC
        """, user["user_id"])

        return rows


async def send_friend_recommendation_card(chat_id, old_msg_id=None):
    session = user_sessions.get(chat_id)
    if not session or "friends_recommendations" not in session:
        await bot.send_message(chat_id, "❌ Рекомендации не найдены")
        return

    recommendations = session["friends_recommendations"]
    index = session.get("friends_rec_index", 0)

    if index >= len(recommendations):
        session["friends_rec_index"] = 0
        index = 0

    rec = recommendations[index]

    title = rec.get('title', 'Без названия')
    tmdb_id = rec.get('tmdb_id')
    type_ = rec.get('type', 'movie')

    # Получаем реальные username друзей из БД
    friend_usernames = []
    try:
        async with state.db.acquire() as conn:
            # Получаем username друзей которые лайкнули этот контент
            query = """
            SELECT u.username 
            FROM ratings r
            JOIN users u ON r.user_id = u.user_id
            WHERE r.tmdb_id = $1 AND r.type = $2 AND r.liked = true
            AND u.user_id IN (
                SELECT friend_user_id FROM user_friends WHERE user_id = (
                    SELECT user_id FROM users WHERE tg_id = $3
                )
                UNION
                SELECT user_id FROM user_friends WHERE friend_user_id = (
                    SELECT user_id FROM users WHERE tg_id = $3
                )
            )
            AND u.username IS NOT NULL AND u.username != ''
            """
            user_result = await conn.fetchval("SELECT user_id FROM users WHERE tg_id = $1", chat_id)
            if user_result:
                friends_data = await conn.fetch(query, tmdb_id, type_, chat_id)
                friend_usernames = [friend['username'] for friend in friends_data if friend['username']]
    except Exception as e:
        print(f"Error getting friend usernames: {e}")

    # Формируем упоминания
    if friend_usernames:
        # Берем до 3 друзей для упоминания
        mentioned_friends = []
        for username in friend_usernames[:3]:
            if username and username.strip():
                clean_username = username.lstrip('@').strip()
                mentioned_friends.append(f"@{clean_username}")

        friends_mention = ", ".join(mentioned_friends)

        # Если друзей больше, добавляем счетчик
        if len(friend_usernames) > 3:
            friends_mention += f" и ещё {len(friend_usernames) - 3}"
    else:
        friends_mention = "друзья"

    friend_likes = len(friend_usernames)

    # Получаем детали
    details = get_item_details(type_, tmdb_id)

    if details:
        year = (details.get('release_date') or details.get('first_air_date') or '')[:4]
        rating = details.get('vote_average', '—')
        poster = f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get('poster_path') else None
    else:
        year = "—"
        rating = "—"
        poster = None

    caption = (
        f"👥 Рекомендация от друзей\n\n"
        f"🎬 {title} ({year})\n"
        f"⭐ Рейтинг: {rating} (TMDB)\n"
        f"👍 Лайков от друзей: {friend_likes}\n"
        f"👤 Понравился: {friends_mention}\n\n"
        f"💡 Ваши друзья посмотрели и оценили этот фильм!"
    )

    is_in_collection = await is_in_user_collection(chat_id, tmdb_id, type_)

    keyboard = []
    if is_in_collection:
        keyboard.append([InlineKeyboardButton(text="✅ В коллекции", callback_data="already_in_collection")])
    else:
        keyboard.append(
            [InlineKeyboardButton(text="➕ В коллекцию", callback_data=f"add_{tmdb_id}_{type_}")])

    keyboard.append([InlineKeyboardButton(text="➡️ Следующая рекомендация", callback_data="next_friend_rec")])
    keyboard.append([InlineKeyboardButton(text="👥 К друзьям", callback_data="friends_menu")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])

    if old_msg_id:
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except:
            pass

    if poster:
        await bot.send_photo(
            chat_id,
            photo=poster,
            caption=caption,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="HTML"
        )
    else:
        await bot.send_message(
            chat_id,
            text=caption,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard),
            parse_mode="HTML"
        )

    session["friends_rec_index"] = index + 1


async def get_search_menu_keyboard(chat_id: int):
    """Возвращает меню поиска с учетом подписки"""
    requests_info = await get_requests_info(chat_id)

    if requests_info["has_subscription"]:
        # Полное меню поиска для подписчиков
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Случайный поиск", callback_data="random_search")],
            [InlineKeyboardButton(text="🔍 Поиск по названию", callback_data="search_by_title")],
            [InlineKeyboardButton(text="🎭 Поиск по актеру", callback_data="search_by_person")],
            [InlineKeyboardButton(text="🎯 На основе предпочтений", callback_data="preferences")],
            [InlineKeyboardButton(text="🔥 В тренде сейчас", callback_data="trending_menu")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")],
        ])
    else:
        # Ограниченное меню поиска для обычных пользователей
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎲 Случайный поиск", callback_data="random_search")],
            [InlineKeyboardButton(text="🔍 Поиск по названию 🔒", callback_data="premium_locked")],
            [InlineKeyboardButton(text="🎭 Поиск по актеру 🔒", callback_data="premium_locked")],
            [InlineKeyboardButton(text="🎯 На основе предпочтений 🔒", callback_data="premium_locked")],
            [InlineKeyboardButton(text="🔥 В тренде сейчас 🔒", callback_data="premium_locked")],
            [InlineKeyboardButton(text="💫 Получить подписку", callback_data="subscription_management")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")],
        ])


async def get_random_search_keyboard(chat_id: int):
    """Возвращает меню случайного поиска с учетом подписки"""
    requests_info = await get_requests_info(chat_id)

    if requests_info["has_subscription"]:
        # Полное меню для подписчиков
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Случайный фильм", callback_data="discover_movie")],
            [InlineKeyboardButton(text="📺 Случайный сериал", callback_data="discover_tv")],
            [InlineKeyboardButton(text="🧭 Случайный поиск по жанрам", callback_data="search_genre")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="search_menu")],
        ])
    else:
        # Ограниченное меню для обычных пользователей
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🎬 Случайный фильм", callback_data="discover_movie")],
            [InlineKeyboardButton(text="📺 Случайный сериал", callback_data="discover_tv")],
            [InlineKeyboardButton(text="🧭 Поиск по жанрам 🔒", callback_data="premium_locked")],
            [InlineKeyboardButton(text="💫 Получить подписку", callback_data="subscription_management")],
            [InlineKeyboardButton(text="⬅️ Назад", callback_data="search_menu")],
        ])


async def get_friends_menu_keyboard(chat_id: int):
    """Возвращает меню друзей с учетом подписки"""
    requests_info = await get_requests_info(chat_id)

    if requests_info["has_subscription"]:
        # Полное меню для подписчиков
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Мои друзья", callback_data="my_friends")],
            [InlineKeyboardButton(text="➕ Добавить друга", callback_data="add_friend")],
            [InlineKeyboardButton(text="📨 Управление заявками", callback_data="friend_requests_management")],
            [InlineKeyboardButton(text="🎯 Рекомендации друзей", callback_data="friends_recommendations")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
        ])
    else:
        # Ограниченное меню для обычных пользователей
        return InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="👥 Мои друзья", callback_data="my_friends")],
            [InlineKeyboardButton(text="➕ Добавить друга", callback_data="add_friend")],
            [InlineKeyboardButton(text="📨 Управление заявками", callback_data="friend_requests_management")],
            [InlineKeyboardButton(text="🎯 Рекомендации друзей 🔒", callback_data="premium_locked")],
            [InlineKeyboardButton(text="💫 Получить подписку", callback_data="subscription_management")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
        ])

async def send_person_results_page(chat_id: int, results: list, search_query: str, page: int,
                                   results_per_page: int = 10):
    total_results = len(results)
    start_idx = page * results_per_page
    end_idx = start_idx + results_per_page
    page_results = results[start_idx:end_idx]

    text = f"🎭 Найдено {total_results} человек по запросу: '{search_query}'\nСтраница {page + 1}/{(total_results + results_per_page - 1) // results_per_page}\n\nВыберите актера:"

    keyboard = []

    for item in page_results:
        name = item.get("name", "Без имени")
        known_for = item.get("known_for_department", "Актер")

        # Получаем известные работы для отображения

        btn_text = f"🎭 {name}"

        # Обрезаем текст если слишком длинный
        if len(btn_text) > 50:
            btn_text = btn_text[:47] + "..."

        keyboard.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"select_person_{item['id']}"
            )
        ])

    # Пагинация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"person_page_{page - 1}"))
    if end_idx < total_results:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"person_page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="🔍 Новый поиск", callback_data="search_by_person")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])

    await bot.send_message(chat_id, text, reply_markup=InlineKeyboardMarkup(inline_keyboard=keyboard))


async def send_person_filmography_page(chat_id: int, filmography: list, person_name: str, page: int,
                                       results_per_page: int = 10):
    """Отображает фильмографию актера с пагинацией (сгруппированные роли)"""
    total_results = len(filmography)
    start_idx = page * results_per_page
    end_idx = start_idx + results_per_page
    page_results = filmography[start_idx:end_idx]
    total_pages = (total_results + results_per_page - 1) // results_per_page

    text = f"🎭 Фильмография: {person_name}\n"
    text += f"📁 Найдено {total_results} работ\n"
    text += f"📄 Страница {page + 1}/{total_pages}\n\n"

    keyboard = []

    for i, item in enumerate(page_results, start=start_idx + 1):
        media_type = item.get("media_type")
        title = item.get("title") or "Без названия"
        year = (item.get("release_date") or "")[:4]
        roles = item.get("person_role", [])  # Теперь это список ролей

        # Формируем текст кнопки
        type_icon = "🎬" if media_type == "movie" else "📺"

        btn_text = f"{type_icon} {title}"
        if year:
            btn_text += f" ({year})"

        # Добавляем информацию о ролях


        # Обрезаем если слишком длинный
        if len(btn_text) > 50:
            btn_text = btn_text[:47] + "..."

        keyboard.append([
            InlineKeyboardButton(
                text=btn_text,
                callback_data=f"select_{item['id']}_{media_type}"
            )
        ])

    # Пагинация
    nav_buttons = []
    if page > 0:
        nav_buttons.append(InlineKeyboardButton(text="⬅️ Назад", callback_data=f"filmography_page_{page - 1}"))

    if end_idx < total_results:
        nav_buttons.append(InlineKeyboardButton(text="Вперед ➡️", callback_data=f"filmography_page_{page + 1}"))

    if nav_buttons:
        keyboard.append(nav_buttons)

    keyboard.append([InlineKeyboardButton(text="⬅️ К списку актеров", callback_data="back_to_person_list")])
    keyboard.append([InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")])

    return text, InlineKeyboardMarkup(inline_keyboard=keyboard)


async def show_no_more_content(chat_id, old_msg_id=None):
    """Показывает сообщение о том, что контент закончился"""
    if old_msg_id:
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except Exception:
            pass

    await bot.send_message(
        chat_id,
        "🎬 Контент по вашим фильтрам закончился!\n\n"
        "Попробуйте:\n"
        "• ⚡ Изменить фильтры поиска\n"
        "• 🎲 Начать новый поиск\n"
        "• 🎭 Выбрать другой жанр\n"
        "• 🔥 Посмотреть тренды",
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="⚡ Изменить фильтры", callback_data="search_filters")],
            [InlineKeyboardButton(text="🎲 Новый поиск", callback_data="search_menu")],
            [InlineKeyboardButton(text="🏠 Главное меню", callback_data="back_to_main")]
        ])
    )

    # Очищаем сессию чтобы можно было начать заново
    if chat_id in user_sessions:
        user_sessions[chat_id] = {}

# Обновленная функция send_card
async def send_card(chat_id, old_msg_id=None):
    session = user_sessions.get(chat_id)
    if not session or "results" not in session:
        await bot.send_message(chat_id, "❌ Сессия истекла. Начните поиск заново.")
        return

    # Инициализируем все необходимые ключи если их нет
    if "shown_ids" not in session:
        session["shown_ids"] = set()
    if "repeat_counter" not in session:
        session["repeat_counter"] = {}
    if "index" not in session:
        session["index"] = 0

    print(
        f"DEBUG: send_card - session type: {session.get('type')}, results count: {len(session['results'])}, index: {session['index']}")

    # Если достигли конца списка или нужно загрузить новые результаты
    if session["index"] >= len(session["results"]) or len(session["results"]) == 0:
        print(f"DEBUG: Загружаем новые результаты, показано уже: {len(session['shown_ids'])}")

        # Получаем активные фильтры
        current_filters = await get_current_filters(chat_id)

        # Загружаем новые результаты
        new_results = await discover_tmdb(
            session["type"],
            session.get("genre_id"),
            filters=current_filters
        )

        if not new_results:
            await show_no_more_content(chat_id, old_msg_id)
            return

        # Фильтруем уже показанные элементы
        filtered_results = []
        for item in new_results:
            if item["id"] not in session["shown_ids"]:
                filtered_results.append(item)

        # ЕСЛИ НЕТ НОВЫХ РЕЗУЛЬТАТОВ - показываем сообщение
        if not filtered_results:
            await show_no_more_content(chat_id, old_msg_id)
            return

        # Обновляем сессию
        session["results"] = filtered_results
        session["index"] = 0
        print(f"DEBUG: Загружено {len(filtered_results)} новых результатов (после фильтрации)")

    # Показываем текущий элемент
    item = session["results"][session["index"]]

    # ПРОВЕРКА НА ПОВТОР: если этот фильм уже показывался много раз
    current_item_id = item["id"]
    session["repeat_counter"][current_item_id] = session["repeat_counter"].get(current_item_id, 0) + 1

    # Если фильм показывается больше 3 раз подряд - прерываем цикл
    if session["repeat_counter"][current_item_id] > 3:
        print(f"DEBUG: Обнаружен повторяющийся контент {current_item_id}, прерываем цикл")
        await show_no_more_content(chat_id, old_msg_id)
        return

    print(f"DEBUG: Processing item {session['index']}: {item.get('title') or item.get('name')}")

    # Добавляем ID в показанные
    session["shown_ids"].add(item["id"])

    # Пропускаем забаненные фильмы и применяем фильтры
    max_attempts = len(session["results"])
    attempts = 0

    while attempts < max_attempts and session["index"] < len(session["results"]):
        item = session["results"][session["index"]]

        # Проверяем бан
        if await is_banned(item["id"], session["type"]):
            print(f"DEBUG: Пропускаем забаненный контент - ID: {item['id']}")
            session["index"] += 1
            attempts += 1
            continue

        # Получаем детали
        details = get_item_details(session["type"], item["id"])
        if not details:
            session["index"] += 1
            attempts += 1
            continue

        # Применяем фильтры пользователя
        filters = await get_user_filters(chat_id)
        filters = filters or {"exclude_anime": False, "exclude_cartoons": False, "exclude_watched": False}

        if filters["exclude_anime"] and is_anime_by_details(session["type"], details, item):
            session["index"] += 1
            attempts += 1
            continue

        if filters["exclude_cartoons"] and is_cartoons_by_details(session["type"], details, item):
            session["index"] += 1
            attempts += 1
            continue

        if filters["exclude_watched"]:
            user_rating = await get_user_rating(chat_id, item["id"], session["type"])
            if user_rating and user_rating["watched"]:
                session["index"] += 1
                attempts += 1
                continue

        # Если все проверки пройдены, показываем карточку
        title = details.get("title") or details.get("name") or "Без названия"
        year = (details.get("release_date") or details.get("first_air_date") or "")[:4]
        rating = details.get("vote_average") or "—"
        overview = details.get("overview") or "Описание отсутствует."
        if len(overview) > 2000:
            overview = overview[:2000] + "..."
        poster = f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get("poster_path") else None
        avg_ratings = await get_ratings(item["id"], session["type"])
        user_rating = await get_user_rating(chat_id, item["id"], session["type"])
        watched_text = "✅ Вы смотрели" if user_rating and user_rating["watched"] else ""

        # Общая информация о прогрессе
        total_shown = len(session["shown_ids"]) + 1

        caption = (
            f"<b>{title}</b> ({year})\n"
            f"⭐ {rating} | 👍 {avg_ratings['likes']} | 👎 {avg_ratings['dislikes']} | 👀 {avg_ratings['watches']}"
        )
        if watched_text:
            caption += f"\n{watched_text}"

        caption += "\n\n"

        # Добавляем описание как раскрываемую цитату
        if overview and overview != "Описание отсутствует.":
            caption += f"<blockquote expandable>{overview}</blockquote>"
        else:
            caption += "Описание отсутствует."

        # Добавляем провайдеров (если помещаются)
        try:
            prov = get_providers_ru(session.get("type"), item["id"])
            if prov:
                prov_text = f"\n\nГде смотреть: {prov}"
                if len(caption) + len(prov_text) <= 1024:
                    caption += prov_text
        except Exception:
            pass

        # Добавляем ID в показанные
        session["shown_ids"].add(item["id"])

        is_genre_search = session.get("mode") == "genre"
        is_trending = session.get("mode") == "trending"

        if old_msg_id:
            try:
                await bot.delete_message(chat_id, old_msg_id)
            except Exception:
                pass

        if poster:
            await bot.send_photo(
                chat_id,
                photo=poster,
                caption=caption,
                parse_mode="HTML",  # ← добавляем или меняем
                reply_markup=await kb_card(chat_id, item["id"], session["type"], is_genre_search, is_trending)
            )
        else:
            await bot.send_message(
                chat_id,
                text=caption,
                parse_mode="HTML",  # ← добавляем или меняем
                reply_markup=await kb_card(chat_id, item["id"], session["type"], is_genre_search, is_trending)
            )

        # Увеличиваем индекс для следующего вызова
        session["index"] += 1
        return

    # Если не нашли подходящий контент в текущей выборке
    if old_msg_id:
        try:
            await bot.delete_message(chat_id, old_msg_id)
        except Exception:
            pass

    # Пробуем загрузить новые результаты
    session["index"] = len(session["results"])  # Принудительно загрузим новые результаты
    await send_card(chat_id)


async def get_user_rating(tg_id: int, tmdb_id: int, type_: str):
    async with state.db.acquire() as conn:
        user = await conn.fetchrow("SELECT user_id FROM users WHERE tg_id=$1", tg_id)
        if not user:
            return None
        row = await conn.fetchrow("""
            SELECT liked, disliked, watched, is_hidden
            FROM ratings
            WHERE user_id = $1
            AND tmdb_id = $2
            AND type = $3
        """, user["user_id"], tmdb_id, type_)
        if row:
            return {
                "liked": row["liked"],
                "disliked": row["disliked"],
                "watched": row["watched"],
                "is_hidden": row["is_hidden"]
            }
        return None


async def get_ratings_stats(sort_by: str = "updated", page: int = 0, limit: int = 15):
    """Получает статистику рейтингов с пагинацией и сортировкой"""
    async with state.db.acquire() as conn:
        # Определяем сортировку
        if sort_by == "likes":
            order_by = "likes DESC, watches DESC"
        elif sort_by == "dislikes":
            order_by = "dislikes DESC, watches DESC"
        elif sort_by == "watches":
            order_by = "watches DESC, likes DESC"
        else:  # updated (по умолчанию)
            order_by = "MAX(r.rating_id) DESC"

        # Получаем общее количество
        total_count = await conn.fetchval("""
            SELECT COUNT(DISTINCT CONCAT(r.tmdb_id, '-', r.type))
            FROM ratings r
            WHERE r.liked = TRUE OR r.disliked = TRUE OR r.watched = TRUE
        """)

        # Получаем данные с пагинацией
        offset = page * limit

        query = f"""
            SELECT 
                r.tmdb_id,
                r.type,
                r.title,
                COUNT(CASE WHEN r.liked = TRUE THEN 1 END) as likes,
                COUNT(CASE WHEN r.disliked = TRUE THEN 1 END) as dislikes,
                COUNT(CASE WHEN r.watched = TRUE THEN 1 END) as watches,
                MAX(r.rating_id) as last_updated
            FROM ratings r
            WHERE r.liked = TRUE OR r.disliked = TRUE OR r.watched = TRUE
            GROUP BY r.tmdb_id, r.type, r.title
            ORDER BY {order_by}
            LIMIT $1 OFFSET $2
        """

        rows = await conn.fetch(query, limit, offset)

        return {
            "items": rows,
            "total_count": total_count,
            "page": page,
            "total_pages": (total_count + limit - 1) // limit
        }

async def handle_rating(callback, action, tmdb_id=None, type_=None):
    chat_id = callback.message.chat.id

    # Если tmdb_id и type_ не переданы, извлекаем их из callback.data
    if tmdb_id is None or type_ is None:
        parts = callback.data.split("_")
        print(f"DEBUG: callback.data = {callback.data}, parts = {parts}")  # Отладочная информация

        # Для действия "watch" формат: mark_watched_{tmdb_id}_{type_}
        if action == "watch":
            if len(parts) < 3:
                await callback.answer("Ошибка: некорректные данные для отметки просмотра.")
                return
            try:
                tmdb_id = int(parts[2])
                type_ = parts[3] if len(parts) > 3 else "movie"  # default to movie if not specified
            except (ValueError, IndexError) as e:
                await callback.answer(f"Ошибка данных: {e}")
                return
        # Для действий like/dislike/reset формат: {action}_{tmdb_id}_{type_}
        else:
            if len(parts) < 3:
                await callback.answer("Ошибка: некорректные данные.")
                return
            try:
                tmdb_id = int(parts[1])
                type_ = parts[2]
            except (ValueError, IndexError) as e:
                await callback.answer(f"Ошибка данных: {e}")
                return

    print(f"DEBUG: handle_rating called with action={action}, tmdb_id={tmdb_id}, type_={type_}")

    user_rating = await get_user_rating(chat_id, tmdb_id, type_)
    print(f"DEBUG: user_rating before change = {user_rating}")

    liked = user_rating["liked"] if user_rating else False
    disliked = user_rating["disliked"] if user_rating else False
    watched = user_rating["watched"] if user_rating else False

    # Обновляем значения
    if action == "like":
        liked, disliked = (not liked, False) if liked else (True, False)
        await callback.answer("👍 Лайк добавлен!" if liked else "👍 Лайк убран!")
    elif action == "dislike":
        liked, disliked = (False, not disliked) if disliked else (False, True)
        await callback.answer("👎 Дизлайк добавлен!" if disliked else "👎 Дизлайк убран!")
    elif action == "reset":
        liked = disliked = False
        await callback.answer("🔄 Оценка сброшена!")
    elif action == "watch":
        watched = not watched
        await callback.answer("✅ Отмечено как просмотренное!" if watched else "👀 Снята отметка просмотренного!")

    print(f"DEBUG: after change - liked={liked}, disliked={disliked}, watched={watched}")

    details = get_item_details(type_, tmdb_id)
    title = details.get("title") or details.get("name") or "Без названия"

    success = await add_rating(chat_id, tmdb_id, type_, liked=liked, disliked=disliked, watched=watched, title=title)
    print(f"DEBUG: add_rating success = {success}")

    # Обновляем карточку
    avg_ratings = await get_ratings(tmdb_id, type_)
    watched_text = "✅ Вы смотрели" if watched else ""
    year = (details.get("release_date") or details.get("first_air_date") or "")[:4]
    rating_tmdb = details.get("vote_average") or "—"
    overview = details.get("overview") or "Описание отсутствует."
    if len(overview) > 2000:
        overview = overview[:2000] + "..."
    poster = f"https://image.tmdb.org/t/p/w500{details.get('poster_path')}" if details.get("poster_path") else None
    caption = (
        f"<b>{title}</b> ({year})\n"
        f"⭐ {rating_tmdb} | 👍{avg_ratings['likes']} | 👎{avg_ratings['dislikes']} | 👀{avg_ratings['watches']}"
    )
    if watched_text:
        caption += f"\n{watched_text}"

    caption += "\n\n"

    # Добавляем описание как раскрываемую цитату
    if overview and overview != "Описание отсутствует.":
        caption += f"<blockquote expandable>{overview}</blockquote>"
    else:
        caption += "Описание отсутствует."

    try:
        if poster and callback.message.photo:
            await bot.edit_message_media(
                chat_id=chat_id,
                message_id=callback.message.message_id,
                media=types.InputMediaPhoto(media=poster, caption=caption),
                parse_mode="HTML",
                reply_markup=kb_collection_item(tmdb_id, type_, watched, liked, disliked)
            )
        else:
            await bot.edit_message_caption(
                chat_id=chat_id,
                message_id=callback.message.message_id,
                caption=caption,
                parse_mode="HTML",
                reply_markup=kb_collection_item(tmdb_id, type_, watched, liked, disliked)
            )
    except Exception as e:
        print(f"Ошибка при обновлении карточки: {e}")
        # Если не удалось отредактировать, просто отправляем уведомление
        await callback.answer("✅ Обновлено!")


# -------------------- RUN --------------------
