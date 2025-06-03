import os
import logging

import pytz
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Update, Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv
from datetime import datetime

from database import (
    get_pool, save_request, get_history,
    add_subscription, remove_subscription, get_user_subscriptions,
    get_active_subscriptions_for_notification,
    get_all_active_subscriptions_with_details, update_last_alert_time
)

# APScheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from pytz import utc
from weather_api import get_weather, get_forecast, check_for_precipitation_in_forecast

# Загрузка .env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")

# Логирование
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(name)s - %(message)s')
logger = logging.getLogger(__name__)

# FastAPI приложение
app = FastAPI()

# Aiogram setup
storage = MemoryStorage()
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher(storage=storage)
router = Router()
dp.include_router(router)

# Глобальные переменные
pool = None
scheduler = AsyncIOScheduler(timezone=utc) # Инициализируем планировщик здесь

# --- Клавиатуры ---
def main_menu_keyboard():
    kb = [
        [KeyboardButton(text="🌦 Погода сейчас"), KeyboardButton(text="🗓 Прогноз на 3 дня")],
        [KeyboardButton(text="🔔 Мои подписки"), KeyboardButton(text="📜 Моя история")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

def subscriptions_menu_keyboard():
    kb = [
        [KeyboardButton(text="➕ Подписаться на город")],
        [KeyboardButton(text="➖ Отписаться от города")],
        [KeyboardButton(text="◀️ Назад в меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

def back_keyboard():
    kb = [[KeyboardButton(text="◀️ Назад в меню")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

# --- Состояния (States) ---
class WeatherStates(StatesGroup):
    waiting_for_city_current = State()
    waiting_for_city_forecast = State()
    waiting_for_city_subscribe = State()
    waiting_for_city_unsubscribe = State()

# --- Хендлеры ---

@router.message(CommandStart())
async def start_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "Привет! Я погодный бот. Выбери действие из меню ниже:",
        reply_markup=main_menu_keyboard()
    )

@router.message(F.text == "◀️ Назад в меню")
async def back_to_main_menu(message: Message, state: FSMContext):
    current_state = await state.get_state()
    await state.clear()
    # Определяем, из какого меню пользователь вернулся, чтобы показать правильное сообщение
    if current_state and any(sub_state in current_state for sub_state in [
        WeatherStates.waiting_for_city_subscribe.state,
        WeatherStates.waiting_for_city_unsubscribe.state
    ]):
        await message.answer("Вы вернулись в меню подписок.", reply_markup=subscriptions_menu_keyboard())
    else:
        await message.answer("Вы вернулись в главное меню.", reply_markup=main_menu_keyboard())


# --- Погода сейчас ---
@router.message(F.text == "🌦 Погода сейчас")
async def ask_city_for_current_weather(message: Message, state: FSMContext):
    await state.set_state(WeatherStates.waiting_for_city_current)
    await message.answer("Введите название города:", reply_markup=back_keyboard())

@router.message(WeatherStates.waiting_for_city_current, F.text)
async def process_current_weather_city(message: Message, state: FSMContext):
    city = message.text.strip()
    if city == "◀️ Назад в меню": # Обработка кнопки "Назад" до проверки на "/"
        await state.clear()
        await message.answer("Вы вернулись в главное меню.", reply_markup=main_menu_keyboard())
        return
    if not city or "/" in city:
        await message.answer("Некорректное название города. Пожалуйста, введите снова.", reply_markup=back_keyboard())
        return

    await state.clear()
    global pool
    if not pool: pool = await get_pool()

    weather_info = await get_weather(city)
    await message.answer(weather_info, reply_markup=main_menu_keyboard())

    if message.from_user and message.from_user.username and "Ошибка:" not in weather_info:
        try:
            await save_request(pool, message.from_user.username, city, datetime.now())
        except Exception as e:
            logger.error(f"Error saving current weather request for {city}: {e}")

# --- Прогноз на 3 дня ---
@router.message(F.text == "🗓 Прогноз на 3 дня")
async def ask_city_for_forecast(message: Message, state: FSMContext):
    await state.set_state(WeatherStates.waiting_for_city_forecast)
    await message.answer("Введите название города для прогноза:", reply_markup=back_keyboard())

@router.message(WeatherStates.waiting_for_city_forecast, F.text)
async def process_forecast_city(message: Message, state: FSMContext):
    city = message.text.strip()
    if city == "◀️ Назад в меню":
        await state.clear()
        await message.answer("Вы вернулись в главное меню.", reply_markup=main_menu_keyboard())
        return
    if not city or "/" in city:
        await message.answer("Некорректное название города. Пожалуйста, введите снова.", reply_markup=back_keyboard())
        return

    await state.clear()
    global pool
    if not pool: pool = await get_pool()

    forecast_info = await get_forecast(city)
    await message.answer(forecast_info, reply_markup=main_menu_keyboard())

    if message.from_user and message.from_user.username and "Ошибка:" not in forecast_info:
        try:
            await save_request(pool, message.from_user.username, city, datetime.now())
        except Exception as e:
            logger.error(f"Error saving forecast request for {city}: {e}")

# --- Управление подписками ---
@router.message(F.text == "🔔 Мои подписки")
async def manage_subscriptions_menu_handler(message: Message, state: FSMContext): # Изменил имя для ясности
    await state.clear()
    global pool
    if not pool: pool = await get_pool()

    user_id = message.from_user.id
    try:
        subscriptions = await get_user_subscriptions(pool, user_id)
        if subscriptions:
            subs_text_parts = []
            for sub in subscriptions:
                time_str = sub['notification_time'].strftime('%H:%M') if sub['notification_time'] else "N/A"
                tz_str = sub['timezone'] if sub['timezone'] else "N/A"
                subs_text_parts.append(f"🏙️ {sub['city']} (в {time_str} {tz_str})")
            subs_text = "\n".join(subs_text_parts)
            response_text = f"Ваши активные подписки:\n{subs_text}\n\nВыберите действие:"
        else:
            response_text = "У вас пока нет активных подписок.\n\nВыберите действие:"
        await message.answer(response_text, reply_markup=subscriptions_menu_keyboard())
    except Exception as e:
        logger.error(f"Error fetching subscriptions for user {user_id}: {e}")
        await message.answer("Не удалось загрузить ваши подписки. Попробуйте позже.", reply_markup=main_menu_keyboard())


@router.message(F.text == "➕ Подписаться на город")
async def ask_city_to_subscribe(message: Message, state: FSMContext):
    await state.set_state(WeatherStates.waiting_for_city_subscribe)
    await message.answer("Введите название города для подписки (уведомления в ~08:00 UTC):", reply_markup=back_keyboard())

@router.message(WeatherStates.waiting_for_city_subscribe, F.text)
async def process_subscribe_city(message: Message, state: FSMContext):
    city = message.text.strip()
    if city == "◀️ Назад в меню":
        await state.clear()
        await message.answer("Вы вернулись в меню подписок.", reply_markup=subscriptions_menu_keyboard())
        return
    if not city or "/" in city:
        await message.answer("Некорректное название города. Пожалуйста, введите снова.", reply_markup=back_keyboard())
        return

    await state.clear()
    global pool
    if not pool: pool = await get_pool()
    user_id = message.from_user.id

    weather_check = await get_weather(city)
    if "Ошибка:" in weather_check:
        await message.answer(f"Город '{city}' не найден. Подписка не оформлена.\n{weather_check}",
                             reply_markup=subscriptions_menu_keyboard())
        return

    try:
        await add_subscription(pool, user_id, city)
        await message.answer(f"✅ Вы подписались на ежедневные уведомления для г. {city} (в ~08:00 UTC).",
                             reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"Error adding subscription for user {user_id}, city {city}: {e}")
        await message.answer("Ошибка при оформлении подписки.", reply_markup=subscriptions_menu_keyboard())

@router.message(F.text == "➖ Отписаться от города")
async def ask_city_to_unsubscribe(message: Message, state: FSMContext):
    global pool
    if not pool: pool = await get_pool()
    user_id = message.from_user.id

    try:
        subscriptions = await get_user_subscriptions(pool, user_id)
        if not subscriptions:
            await message.answer("У вас нет активных подписок для отмены.", reply_markup=subscriptions_menu_keyboard())
            return

        await state.set_state(WeatherStates.waiting_for_city_unsubscribe)
        subs_list_text = "\n".join([f"- {sub['city']}" for sub in subscriptions])
        await message.answer(f"Введите название города для отписки из списка:\n{subs_list_text}",
                             reply_markup=back_keyboard())
    except Exception as e:
        logger.error(f"Error fetching subscriptions for unsubscribe for user {user_id}: {e}")
        await message.answer("Не удалось загрузить ваши подписки для отмены.", reply_markup=subscriptions_menu_keyboard())


@router.message(WeatherStates.waiting_for_city_unsubscribe, F.text)
async def process_unsubscribe_city(message: Message, state: FSMContext):
    city_to_unsubscribe = message.text.strip()
    if city_to_unsubscribe == "◀️ Назад в меню":
        await state.clear()
        await message.answer("Вы вернулись в меню подписок.", reply_markup=subscriptions_menu_keyboard())
        return
    if not city_to_unsubscribe: # Добавил проверку на пустой ввод
        await message.answer("Название города не может быть пустым. Пожалуйста, введите снова.", reply_markup=back_keyboard())
        return

    await state.clear()
    global pool
    if not pool: pool = await get_pool()
    user_id = message.from_user.id

    try:
        current_subs = await get_user_subscriptions(pool, user_id)
        if not any(sub['city'].lower() == city_to_unsubscribe.lower() for sub in current_subs):
            await message.answer(f"У вас нет подписки на город '{city_to_unsubscribe}'.",
                                 reply_markup=subscriptions_menu_keyboard())
            return

        await remove_subscription(pool, user_id, city_to_unsubscribe)
        await message.answer(f"🗑 Вы отписались от уведомлений для г. {city_to_unsubscribe}.",
                             reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"Error removing subscription for user {user_id}, city {city_to_unsubscribe}: {e}")
        await message.answer("Ошибка при отписке.", reply_markup=subscriptions_menu_keyboard())


# --- История запросов ---
async def show_history(message: Message):
    global pool
    if not pool: pool = await get_pool()
    logger.info(f"User {message.from_user.id} requested history.")

    username = message.from_user.username # История по-прежнему привязана к username
    if not username:
        await message.answer("У вас не установлен username в Telegram. История не может быть показана.", reply_markup=main_menu_keyboard())
        return

    try:
        rows = await get_history(pool, username)
        if not rows:
            await message.answer("История запросов пуста.", reply_markup=main_menu_keyboard())
            return

        history_text_parts = [f"📍 {idx + 1}. {row['city']} — {row['request_time'].strftime('%Y-%m-%d %H:%M')}"
                              for idx, row in enumerate(rows)]
        history_text = "\n".join(history_text_parts)
        if len(history_text) > 4000:
             history_text = "Слишком много записей для отображения. Вот часть из них:\n" + history_text[:3900] + "\n(...)"
        await message.answer(f"🕘 Ваша история запросов (последние 10):\n{history_text}", reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"Error fetching history for username {username}: {e}")
        await message.answer("Ошибка при получении истории.", reply_markup=main_menu_keyboard())

@router.message(F.text == "📜 Моя история")
async def history_via_button(message: Message):
    await show_history(message)

@router.message(Command("history"))
async def history_command_handler(message: Message):
    await show_history(message)


async def send_precipitation_alert():
    global pool, bot
    if not pool or not bot:
        logger.warning("Scheduler: Pool or Bot not initialized. Skipping alert round.")
        return

    logger.info("Scheduler: Checking for precipitation alerts...")

    try:
        # Получаем все активные подписки
        subscriptions = await get_all_active_subscriptions_with_details(pool)
        logger.info(f"Scheduler: Found {len(subscriptions)} active subscriptions to check for alerts.")

        for sub in subscriptions:
            user_id = sub['user_id']
            city = sub['city']
            last_alert_time = sub['last_alert_sent_at']

            # Проверяем, не отправляли ли мы уведомление недавно (например, в последние 3 часа)
            # чтобы не спамить
            if last_alert_time and (datetime.datetime.now(pytz.utc) - last_alert_time).total_seconds() < 3 * 3600:
                logger.info(f"Scheduler: Alert for {city} (user {user_id}) was sent recently. Skipping.")
                continue

            try:
                # Проверяем прогноз на наличие осадков в ближайшие 6 часов
                alert_text = await check_for_precipitation_in_forecast(city, hours_ahead=6)

                if alert_text:
                    # Если найдены осадки, отправляем уведомление
                    message_to_send = f"Внимание! В городе {city} ухудшается погода. {alert_text}"
                    await bot.send_message(user_id, message_to_send)
                    logger.info(f"Scheduler: Sent precipitation alert for {city} to user {user_id}")

                    # Обновляем время последнего уведомления в БД
                    await update_last_alert_time(pool, user_id, city)

            except Exception as e:
                logger.error(f"Scheduler: Failed to process alert for user {user_id} for {city}. Error: {e}",
                             exc_info=True)
    except Exception as e:
        logger.error(f"Scheduler: General error in send_precipitation_alert job: {e}", exc_info=True)


# --- Планировщик уведомлений ---
async def send_weather_notification():
    global pool, bot
    if not pool or not bot:
        logger.warning("Scheduler: Pool or Bot not initialized. Skipping notifications.")
        return

    logger.info("Scheduler: Checking for notifications to send...")
    target_time_str = '08:00' # Отправка в 08:00 UTC

    try:
        subscriptions_to_notify = await get_active_subscriptions_for_notification(pool, target_time_str)
        logger.info(f"Scheduler: Found {len(subscriptions_to_notify)} subscriptions for {target_time_str} UTC.")

        for sub in subscriptions_to_notify:
            user_id = sub['user_id']
            city = sub['city']
            try:
                weather_info = await get_weather(city)
                if "Ошибка:" not in weather_info:
                    await bot.send_message(user_id, f"☀️ Ежедневная сводка погоды для г. {city}:\n\n{weather_info}")
                    logger.info(f"Scheduler: Sent weather update for {city} to user {user_id}")
                else:
                    logger.warning(f"Scheduler: Could not get weather for {city} (user {user_id}): {weather_info}")
            except Exception as e:
                logger.error(f"Scheduler: Failed to send notification to user {user_id} for {city}. Error: {e}", exc_info=True)
                # Возможные действия: деактивировать подписку после N ошибок, уведомить пользователя и т.д.
    except Exception as e:
        logger.error(f"Scheduler: General error in send_weather_notification job: {e}", exc_info=True)

# --- FastAPI эндпоинты и жизненный цикл ---
@app.get("/")
async def root():
    return {"status": "alive"}

@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        body = await request.json()
        logger.debug(f"Received update via webhook: {body}")
        update = Update(**body)
        await dp.feed_update(bot=bot, update=update)
        return {"ok": True}
    except Exception as e:
        logger.exception("Error processing webhook:")
        return {"ok": False, "error": str(e)}


@app.on_event("startup")
async def on_startup_combined():
    global pool, scheduler
    logger.info("API: Application startup sequence initiated...")

    # 1. Инициализация пула БД
    if pool is None:
        logger.info("API: Startup - creating database pool.")
        pool = await get_pool()
        logger.info("API: Database pool created on startup.")
    try:  # Раскомментируй этот блок
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            logger.info(f"Webhook is set to: {webhook_info.url}")
        else:
            logger.warning("Webhook is NOT SET. Consider setting it for production.")
    except Exception as e:
        logger.error(f"Could not get webhook info: {e}")
    # 3. Настройка и запуск планировщика APScheduler
    # Запуск проверки КАЖДЫЙ ЧАС (например, в 5 минут каждого часа, чтобы дать API время обновиться)
    scheduler.add_job(send_precipitation_alert, CronTrigger(minute=5, timezone=utc),
                      id="hourly_precipitation_check", replace_existing=True)
    logger.info("Scheduler: Hourly precipitation check job has been set (at 5 min past the hour).")

    if not scheduler.running:
        scheduler.start()
        logger.info("APScheduler started.")

    logger.info("API: Application startup sequence completed.")


@app.on_event("shutdown")
async def on_shutdown():
    global scheduler, pool
    logger.info("API: Application shutdown sequence initiated...")
    if scheduler and scheduler.running:
        scheduler.shutdown()
        logger.info("APScheduler shut down.")
    if pool:
        await pool.close() # Корректное закрытие пула соединений asyncpg
        logger.info("Database pool closed.")
    logger.info("API: Application shutdown sequence completed.")