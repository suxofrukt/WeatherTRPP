import os
import logging
import datetime  # Используем datetime.datetime и datetime.time
import pytz

from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, F, types  # Добавили types для callback_query
from aiogram.types import Update, Message, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, \
    InlineKeyboardButton
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from dotenv import load_dotenv

# Импорты из твоих модулей
from weather_api import get_weather, get_forecast, check_for_precipitation_in_forecast
from database import (
    get_pool, save_request, get_history,
    add_subscription, remove_subscription, get_user_subscriptions,
    get_all_active_subscriptions_with_details, update_last_alert_time
    # get_active_subscriptions_for_notification - если старая функция send_weather_notification не используется, это тоже не нужно
)

# APScheduler
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger

# from pytz import utc # pytz.utc используется напрямую

# Загрузка .env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")  # Убедимся, что он есть для геокодинга

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
scheduler = AsyncIOScheduler(timezone=pytz.utc)


# --- ОПРЕДЕЛЕНИЕ СОСТОЯНИЙ FSM (ОДНО ОБЪЕДИНЕННОЕ ОПРЕДЕЛЕНИЕ) ---
class WeatherStates(StatesGroup):
    waiting_for_city_current = State()  # Для команды "Погода сейчас"
    waiting_for_city_forecast = State()  # Для команды "Прогноз на 3 дня"
    waiting_for_city_subscribe = State()  # Ожидание города для новой подписки
    choosing_timezone = State()  # Ожидание выбора часового пояса для утренних уведомлений
    entering_notification_time = State()  # Ожидание ввода времени для утренних уведомлений
    waiting_for_city_unsubscribe = State()  # Ожидание города для отписки


# --- Клавиатуры ---
def main_menu_keyboard():
    kb = [
        [KeyboardButton(text="🌦 Погода сейчас"), KeyboardButton(text="🗓 Прогноз на 3 дня")],
        [KeyboardButton(text="🔔 Мои подписки"), KeyboardButton(text="📜 Моя история")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)


def subscriptions_menu_keyboard():  # Используется, если у пользователя нет подписок, или после отписки
    kb = [
        [KeyboardButton(text="➕ Подписаться на город")],
        [KeyboardButton(text="➖ Отписаться от города")],  # Эту кнопку можно показывать, только если есть подписки
        [KeyboardButton(text="◀️ Назад в меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)


def back_keyboard():  # Для отмены ввода города
    kb = [[KeyboardButton(text="◀️ Назад в меню")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)


# Словарь популярных таймзон для Inline-кнопок
POPULAR_TIMEZONES = {
    "Москва (UTC+3)": "Europe/Moscow", "Лондон (GMT/BST)": "Europe/London",
    "Екатеринбург (UTC+5)": "Asia/Yekaterinburg", "Нью-Йорк (EST/EDT)": "America/New_York",
    "Новосибирск (UTC+7)": "Asia/Novosibirsk", "Лос-Анджелес (PST/PDT)": "America/Los_Angeles",
    "Владивосток (UTC+10)": "Asia/Vladivostok", "Берлин (CET/CEST)": "Europe/Berlin",
    "Токио (UTC+9)": "Asia/Tokyo", "UTC": "UTC",
}


def timezone_choice_keyboard():
    buttons = [[InlineKeyboardButton(text=name, callback_data=f"tz_{iana}")] for name, iana in
               POPULAR_TIMEZONES.items()]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def initial_config_keyboard(city: str):  # Клавиатура после успешной подписки
    buttons = [
        [InlineKeyboardButton(text=f"⚙️ Настроить время и пояс для {city}", callback_data=f"cfgtime_{city}")],
        [InlineKeyboardButton(text="👌 Оставить по умолчанию (08:00, пояс города)", callback_data=f"cfgdef_{city}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)


def subscriptions_list_actions_keyboard(subscriptions: list):  # Клавиатура для списка подписок
    buttons = []
    for sub in subscriptions:
        city = sub['city']
        time_obj = sub.get('notification_time')  # Может быть None
        tz_str = sub.get('timezone', 'UTC')  # Дефолт UTC, если нет
        time_str = time_obj.strftime('%H:%M') if time_obj else "08:00"

        display_text = f"🏙️ {city} (утром в {time_str} по поясу {tz_str} + осадки)"
        buttons.append([InlineKeyboardButton(text=display_text, callback_data="noop")])  # noop - просто информация
        buttons.append([
            InlineKeyboardButton(text=f"⚙️ Настроить {city}", callback_data=f"cfgtime_{city}"),
            InlineKeyboardButton(text=f"➖ Отписаться от {city}", callback_data=f"unsub_{city}")
        ])
        buttons.append([InlineKeyboardButton(text="-" * 20, callback_data="noop")])  # Разделитель

    buttons.append([InlineKeyboardButton(text="➕ Добавить город", callback_data="cfg_add_new_city")])
    buttons.append([InlineKeyboardButton(text="◀️ В главное меню", callback_data="cfg_back_main")])
    return InlineKeyboardMarkup(inline_keyboard=buttons)


# --- Хендлеры ---
@router.message(CommandStart())
async def start_command(message: Message, state: FSMContext):
    await state.clear()
    await message.answer("Привет! Я погодный бот. Выбери действие:", reply_markup=main_menu_keyboard())


@router.message(F.text == "◀️ Назад в меню")
async def back_to_main_menu(message: Message, state: FSMContext):
    # ... (логика возврата в меню, возможно, нужно будет уточнить для FSM настройки)
    # Пока оставим простой вариант
    current_fsm_state = await state.get_state()  # Получаем текущее состояние
    logger.info(f"Back to menu called from state: {current_fsm_state}")
    await state.clear()
    await message.answer("Вы вернулись в главное меню.", reply_markup=main_menu_keyboard())


# --- Погода сейчас / Прогноз на 3 дня (остаются без изменений) ---
# ... (ask_city_for_current_weather, process_current_weather_city) ...
# ... (ask_city_for_forecast, process_forecast_city) ...

# --- Управление подписками ---
@router.message(F.text == "🔔 Мои подписки")
async def manage_subscriptions_menu_entry(message: Message, state: FSMContext):
    await state.clear()  # Сбрасываем любое предыдущее состояние FSM
    global pool
    if not pool: pool = await get_pool()
    user_id = message.from_user.id

    try:
        subscriptions = await get_user_subscriptions(pool, user_id)
        if subscriptions:
            await message.answer("Ваши подписки и настройки уведомлений:",
                                 reply_markup=subscriptions_list_actions_keyboard(subscriptions))
        else:
            await message.answer("У вас пока нет подписок.\nХотите добавить?",
                                 reply_markup=subscriptions_menu_keyboard())  # Клавиатура с "Подписаться на город"
    except Exception as e:
        logger.error(f"Error fetching subscriptions for user {user_id}: {e}", exc_info=True)
        await message.answer("Не удалось загрузить ваши подписки. Попробуйте позже.", reply_markup=main_menu_keyboard())


# Callback для кнопки "➕ Добавить город" из списка подписок
@router.callback_query(F.data == "cfg_add_new_city")
async def cb_ask_city_to_subscribe(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    await state.set_state(WeatherStates.waiting_for_city_subscribe)
    await callback_query.message.edit_text(  # Редактируем предыдущее сообщение
        "Введите название города для новой подписки.\nВы будете получать:\n"
        "- Ежедневный прогноз в 08:00 (настраивается).\n"
        "- Предупреждения об осадках.",
        reply_markup=None  # Убираем inline клавиатуру, ждем текстовый ввод
    )
    # Можно отправить новое сообщение с ReplyKeyboard "Назад в меню"
    await bot.send_message(callback_query.from_user.id, "Или вернитесь в меню:", reply_markup=back_keyboard())


# Хендлер для текстовой кнопки "➕ Подписаться на город"
@router.message(F.text == "➕ Подписаться на город")
async def text_ask_city_to_subscribe(message: Message, state: FSMContext):
    await state.set_state(WeatherStates.waiting_for_city_subscribe)
    await message.answer("Введите название города для новой подписки.\nВы будете получать:\n"
                         "- Ежедневный прогноз в 08:00 (настраивается).\n"
                         "- Предупреждения об осадках.",
                         reply_markup=back_keyboard())


# Шаг 1 подписки: ввод города
@router.message(WeatherStates.waiting_for_city_subscribe, F.text)
async def process_new_city_for_subscription(message: Message, state: FSMContext):
    city_input = message.text.strip()
    if city_input == "◀️ Назад в меню":
        await state.clear()
        await message.answer("Подписка отменена. Вы в главном меню.", reply_markup=main_menu_keyboard())
        return
    if not city_input or "/" in city_input:
        await message.reply("Некорректное название города. Попробуйте еще раз или вернитесь в меню.",
                            reply_markup=back_keyboard())
        return

    global pool
    if not pool: pool = await get_pool()
    weather_check = await get_weather(city_input)
    if "Ошибка:" in weather_check:
        await message.reply(f"Город '{city_input}' не найден или ошибка API. Попробуйте другой город.",
                            reply_markup=back_keyboard())
        return

    # Определение часового пояса (упрощенный пример)
    user_timezone_str = "UTC"  # Дефолт
    city_lower = city_input.lower()
    if city_lower == "москва":
        user_timezone_str = "Europe/Moscow"
    # ... (другие города из твоего списка) ...
    elif "дуала" in city_lower or "камерун" in city_lower:
        user_timezone_str = "Africa/Douala"

    try:
        await add_subscription(pool, message.from_user.id, city_input,
                               notification_time_str="08:00:00",  # Дефолтное время
                               user_timezone_str=user_timezone_str)  # Дефолтная/определенная таймзона

        await state.clear()  # Очищаем состояние после успешной подписки
        await message.answer(
            f"✅ Город {city_input} добавлен в подписки!\n"
            f"Утренний прогноз по умолчанию: 08:00 (таймзона: {user_timezone_str}).\n"
            "Хотите настроить время и часовой пояс для этого города?",
            reply_markup=initial_config_keyboard(city_input)
        )
    except Exception as e:
        logger.error(f"Error adding initial subscription for {city_input}: {e}", exc_info=True)
        await message.answer("Ошибка при добавлении подписки. Попробуйте позже.", reply_markup=main_menu_keyboard())
        await state.clear()


# Callback для кнопок "Настроить" или "Оставить по умолчанию"
@router.callback_query(F.data.startswith("cfgtime_") | F.data.startswith("cfgdef_"))
async def handle_subscription_config_start(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    action, city_name = callback_query.data.split("_", 1)

    if action == "cfgdef":
        await callback_query.message.edit_text(
            f"Отлично! Настройки для г. {city_name} сохранены (утренний прогноз в 08:00 по таймзоне города, плюс предупреждения об осадках)."
        )
        # Можно предложить вернуться в список подписок или главное меню
        # await bot.send_message(callback_query.from_user.id, "Главное меню:", reply_markup=main_menu_keyboard())
        return

    # Если выбрали "cfgtime_" (настроить)
    await state.update_data(configuring_city=city_name)
    await state.set_state(WeatherStates.choosing_timezone)
    await callback_query.message.edit_text(
        f"Настройка утренних уведомлений для г. {city_name}.\n"
        "Шаг 1: Выберите часовой пояс из списка (или ближайший к вашему).",
        reply_markup=timezone_choice_keyboard()
    )


# Шаг 2 настройки: выбор таймзоны
@router.callback_query(F.data.startswith("tz_"), WeatherStates.choosing_timezone)  # Добавили фильтр по состоянию
async def process_timezone_choice_for_config(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer()
    selected_timezone_iana = callback_query.data.split("_", 1)[1]

    user_data = await state.get_data()
    city_being_configured = user_data.get("configuring_city")

    if not city_being_configured:
        await callback_query.message.edit_text(
            "Ошибка: город для настройки не найден. Попробуйте снова из меню 'Мои подписки'.")
        await state.clear()
        return

    await state.update_data(selected_timezone=selected_timezone_iana)
    await state.set_state(WeatherStates.entering_notification_time)
    await callback_query.message.edit_text(
        f"Для г. {city_being_configured} выбран часовой пояс: {selected_timezone_iana}.\n"
        "Шаг 2: Введите желаемое время для утренних уведомлений в формате ЧЧ:ММ (например, 07:30)."
    )


# Шаг 3 настройки: ввод времени
@router.message(WeatherStates.entering_notification_time, F.text)
async def process_notification_time_input(message: Message, state: FSMContext):
    time_input_str = message.text.strip()
    try:
        parsed_time = datetime.datetime.strptime(time_input_str, "%H:%M").time()
        notification_time_for_db = parsed_time.strftime("%H:%M:00")
    except ValueError:
        await message.reply("Неверный формат времени. Введите ЧЧ:ММ (например, 08:00).")
        return

    user_data = await state.get_data()
    city_to_configure = user_data.get("configuring_city")
    selected_tz = user_data.get("selected_timezone")

    if not city_to_configure or not selected_tz:
        await message.answer("Ошибка данных настройки. Начните заново из 'Мои подписки'.")
        await state.clear()
        return

    global pool
    if not pool: pool = await get_pool()
    user_id = message.from_user.id

    try:
        await add_subscription(pool, user_id, city_to_configure,
                               notification_time_str=notification_time_for_db,
                               user_timezone_str=selected_tz)
        await message.answer(
            f"👍 Настройки сохранены! Утренний прогноз для г. {city_to_configure} будет в {parsed_time.strftime('%H:%M')} "
            f"по времени часового пояса {selected_tz}.",
            reply_markup=main_menu_keyboard()
        )
    except Exception as e:
        logger.error(f"Ошибка сохранения настроек подписки: {e}", exc_info=True)
        await message.answer("Не удалось сохранить настройки. Попробуйте позже.", reply_markup=main_menu_keyboard())
    finally:
        await state.clear()


# --- Отписка (через Inline кнопку из списка подписок) ---
@router.callback_query(F.data.startswith("unsub_"))
async def cb_process_unsubscribe_city(callback_query: types.CallbackQuery,
                                      state: FSMContext):  # Состояние здесь не нужно
    await callback_query.answer()
    city_to_unsubscribe = callback_query.data.split("_", 1)[1]

    global pool
    if not pool: pool = await get_pool()
    user_id = callback_query.from_user.id

    try:
        await remove_subscription(pool, user_id, city_to_unsubscribe)
        await callback_query.message.edit_text(
            f"🗑 Вы отписались от уведомлений для г. {city_to_unsubscribe}."
        )
        # Обновить список подписок или предложить вернуться в меню
        # Простой вариант - отправить сообщение и главную клавиатуру
        await bot.send_message(user_id, "Список подписок обновлен.", reply_markup=main_menu_keyboard())

    except Exception as e:
        logger.error(f"Error removing subscription for user {user_id}, city {city_to_unsubscribe} via CB: {e}",
                     exc_info=True)
        await callback_query.message.edit_text("Ошибка при отписке. Попробуйте позже.")


# Хендлер для текстовой кнопки "➖ Отписаться от города" (если нужна)
# @router.message(F.text == "➖ Отписаться от города") ...
# @router.message(WeatherStates.waiting_for_city_unsubscribe, F.text) ...


# --- История запросов (остается без изменений) ---
# ... (show_history, history_via_button, history_command_handler) ...

# --- ПЛАНИРОВЩИК: ДВЕ ФУНКЦИИ РАССЫЛКИ ---
# 1. send_daily_morning_forecast_local_time (код из предыдущего ответа, который учитывает timezone и notification_time)
async def send_daily_morning_forecast_local_time():
    # ... (ТОЧНО ТАКОЙ ЖЕ КОД, КАК В ПРЕДЫДУЩЕМ ОТВЕТЕ ДЛЯ ЭТОЙ ФУНКЦИИ)
    global pool, bot
    if not pool or not bot: logger.warning("Scheduler (Morning): Pool or Bot not initialized."); return
    logger.info("Scheduler (Morning): >>> Checking for local 08:00 AM forecasts.")
    current_utc_dt = datetime.datetime.now(pytz.utc)
    try:
        all_subscriptions = await get_all_active_subscriptions_with_details(pool)
        if not all_subscriptions: return
        for sub in all_subscriptions:
            user_id, city, user_notification_time_obj, user_timezone_str, _ = sub['user_id'], sub['city'], sub[
                'notification_time'], sub['timezone'], sub.get('last_alert_sent_at')  # _ для last_alert_sent_at
            if not user_notification_time_obj or not user_timezone_str: continue
            try:
                user_tz = pytz.timezone(user_timezone_str)
            except pytz.UnknownTimeZoneError:
                logger.error(f"Unknown tz {user_timezone_str}"); continue
            user_local_date_today = current_utc_dt.astimezone(user_tz).date()
            target_local_datetime_obj = datetime.datetime.combine(user_local_date_today, user_notification_time_obj)
            target_local_datetime_aware = user_tz.localize(target_local_datetime_obj, is_dst=None)
            target_utc_hour = target_local_datetime_aware.astimezone(pytz.utc).hour
            if (current_utc_dt.hour == target_utc_hour and
                    user_notification_time_obj.minute == 0 and  # Точно в ХХ:00
                    current_utc_dt.minute < 5):
                logger.info(f"Scheduler (Morning): Time for {city} (user {user_id})")
                weather_info = await get_weather(city)
                if "Ошибка:" not in weather_info:
                    msg = f"☀️ Доброе утро! Погода в г. {city} на {user_notification_time_obj.strftime('%H:%M')} по вашему времени:\n\n{weather_info}"
                    await bot.send_message(user_id, msg)
                    logger.info(f"Scheduler (Morning): Sent to {user_id} for {city}")
    except Exception as e:
        logger.error(f"Scheduler (Morning): Error: {e}", exc_info=True)


# 2. send_precipitation_alert (код из предыдущего ответа, который использует last_alert_sent_at и check_for_precipitation_in_forecast)
async def send_precipitation_alert():
    # ... (ТОЧНО ТАКОЙ ЖЕ КОД, КАК В ПРЕДЫДУЩЕМ ОТВЕТЕ ДЛЯ ЭТОЙ ФУНКЦИИ)
    global pool, bot
    if not pool or not bot: logger.warning("Scheduler (Precipitation): Pool or Bot not initialized."); return
    logger.info("Scheduler (Precipitation): >>> Checking for precipitation alerts.")
    try:
        subscriptions = await get_all_active_subscriptions_with_details(pool)
        if not subscriptions: return
        for sub in subscriptions:
            user_id, city, _, _, last_alert_time = sub['user_id'], sub['city'], sub['notification_time'], sub[
                'timezone'], sub.get('last_alert_sent_at')  # _ для неиспользуемых полей
            if last_alert_time and (datetime.datetime.now(pytz.utc) - last_alert_time).total_seconds() < 3 * 3600:
                logger.info(f"Scheduler (Precipitation): Alert for {city} (user {user_id}) sent recently. Skipping.")
                continue
            alert_text = await check_for_precipitation_in_forecast(city, min_lead_minutes=30, max_lead_minutes=120)
            if alert_text:
                logger.info(f"Scheduler (Precipitation): Precipitation found for {city} (user {user_id}): {alert_text}")
                message_to_send = f"Внимание! В городе {city} ухудшается погода. {alert_text}"
                await bot.send_message(user_id, message_to_send)
                await update_last_alert_time(pool, user_id, city)
                logger.info(f"Scheduler (Precipitation): Alert sent to {user_id} for {city}")
    except Exception as e:
        logger.error(f"Scheduler (Precipitation): Error: {e}", exc_info=True)


# --- FastAPI эндпоинты и жизненный цикл ---
# ... (root, webhook) ...

@app.on_event("startup")
async def on_startup_combined():
    global pool, scheduler
    logger.info("API: Application startup sequence initiated...")
    # 1. Инициализация пула БД
    if pool is None:
        logger.info("API: Startup - creating database pool.")
        pool = await get_pool()
        logger.info("API: Database pool created on startup.")
    # 2. Проверка вебхука
    try:
        webhook_info = await bot.get_webhook_info()
        if webhook_info.url:
            logger.info(f"Webhook is set to: {webhook_info.url}")
        else:
            logger.warning("Webhook is NOT SET. Consider setting it.")
    except Exception as e:
        logger.error(f"Could not get webhook info: {e}")

    # 3. Настройка и запуск ПЛАНИРОВЩИКА С ДВУМЯ ЗАДАЧАМИ
    # ЗАДАЧА 1: Ежедневные утренние уведомления (проверка каждый час в XX:01 UTC)
    scheduler.add_job(send_daily_morning_forecast_local_time, CronTrigger(minute=1, timezone=pytz.utc),
                      id="hourly_check_for_local_morning", replace_existing=True)
    logger.info("Scheduler: Job 'hourly_check_for_local_morning' set (every hour at XX:01 UTC).")

    # ЗАДАЧА 2: Уведомления об ухудшении погоды (проверка каждый час в XX:05 UTC)
    scheduler.add_job(send_precipitation_alert, CronTrigger(minute=5, timezone=pytz.utc),
                      id="hourly_precipitation_check", replace_existing=True)
    logger.info("Scheduler: Job 'hourly_precipitation_check' set (every hour at XX:05 UTC).")

    if not scheduler.running:
        try:
            scheduler.start(); logger.info("APScheduler started.")
        except Exception as e:
            logger.error(f"Failed to start APScheduler: {e}")
    logger.info("API: Application startup sequence completed.")


@app.on_event("shutdown")
async def on_shutdown():
    # ... (ТОЧНО ТАКОЙ ЖЕ КОД, КАК В ПРЕДЫДУЩЕМ ОТВЕТЕ)
    global scheduler, pool
    logger.info("API: Application shutdown sequence initiated...")
    if scheduler and scheduler.running: scheduler.shutdown(); logger.info("APScheduler shut down.")
    if pool: await pool.close(); logger.info("Database pool closed.")
    logger.info("API: Application shutdown sequence completed.")