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
    get_all_active_subscriptions_with_details, update_last_alert_time, update_last_daily_sent_time
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
    waiting_for_city_current = State()
    waiting_for_city_forecast = State()
    waiting_for_city_subscribe = State()
    choosing_timezone_text_input = State()  # Было choosing_timezone
    entering_notification_time_text_input = State()  # Было entering_notification_time

    # Состояние для текстовой отписки (которое мы добавили)
    waiting_for_city_unsubscribe = State()  # <--- УБЕДИСЬ, ЧТО ЭТА СТРОКА ЕСТЬ!

    # Состояния для управления подписками через ReplyKeyboard
    managing_subscription_city_choice = State()
    managing_specific_city_action_choice = State()

# --- Клавиатуры ---
def main_menu_keyboard():
    kb = [[KeyboardButton(text="🌦 Погода сейчас"), KeyboardButton(text="🗓 Прогноз на 3 дня")],
          [KeyboardButton(text="🔔 Мои подписки"), KeyboardButton(text="📜 Моя история")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

def subscriptions_initial_menu_keyboard(): # Когда подписок нет, или для первого входа
    kb = [[KeyboardButton(text="➕ Подписаться на город")],
          [KeyboardButton(text="◀️ Назад в главное меню")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)

def back_to_main_menu_keyboard():
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="◀️ Назад в главное меню")]], resize_keyboard=True, one_time_keyboard=True)

def subscribed_cities_reply_keyboard(subscriptions: list):
    buttons = [[KeyboardButton(text=sub['city'])] for sub in subscriptions]
    buttons.append([KeyboardButton(text="➕ Добавить новый город")]) # Изменил текст для ясности
    buttons.append([KeyboardButton(text="◀️ Назад в главное меню")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=False)

def city_management_actions_reply_keyboard(): #city_name здесь не нужен, т.к. он будет в FSM
    kb = [[KeyboardButton(text="⚙️ Настроить время/пояс")],
          [KeyboardButton(text="➖ Отписаться от этого города")],
          [KeyboardButton(text="◀️ Назад к списку городов")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

def back_keyboard():  # Для отмены ввода города
    kb = [[KeyboardButton(text="◀️ Назад в меню")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

def subscriptions_menu_keyboard():  # Используется, если у пользователя нет подписок, или после отписки
    kb = [
        [KeyboardButton(text="➕ Подписаться на город")],
        [KeyboardButton(text="◀️ Назад в главное меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)

POPULAR_TIMEZONES_TEXT_REPLY = {
    "Москва (UTC+3)": "Europe/Moscow", "Лондон (GMT/BST)": "Europe/London",
    "Екатеринбург (UTC+5)": "Asia/Yekaterinburg", "Нью-Йорк (EST/EDT)": "America/New_York",
    "Новосибирск (UTC+7)": "Asia/Novosibirsk", "Лос-Анджелес (PST/PDT)": "America/Los_Angeles",
    "Владивосток (UTC+10)": "Asia/Vladivostok", "Берлин (CET/CEST)": "Europe/Berlin",
    "Токио (UTC+9)": "Asia/Tokyo", "UTC": "UTC",
}

def timezone_choice_reply_keyboard():
    buttons = [[KeyboardButton(text=name)] for name in POPULAR_TIMEZONES_TEXT_REPLY.keys()]
    buttons.append([KeyboardButton(text="◀️ Отмена настройки (в главное меню)")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

# --- Хендлеры ---
@router.message(CommandStart())
async def start_command(message: Message, state: FSMContext):
    await state.clear(); await message.answer("Привет! Я погодный бот.", reply_markup=main_menu_keyboard())

@router.message(F.text == "◀️ Назад в главное меню")
async def back_to_main_menu(message: Message, state: FSMContext):
    logger.info(f"Back to main menu from state: {await state.get_state()}")
    await state.clear(); await message.answer("Вы в главном меню.", reply_markup=main_menu_keyboard())

@router.message(F.text == "🌦 Погода сейчас")
async def ask_city_for_current_weather(message: Message, state: FSMContext):
    await state.set_state(WeatherStates.waiting_for_city_current)
    await message.answer("Введите название города:", reply_markup=back_keyboard())


@router.message(WeatherStates.waiting_for_city_current, F.text)
async def process_current_weather_city(message: Message, state: FSMContext):
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

    weather_info = await get_weather(city)
    await message.answer(weather_info, reply_markup=main_menu_keyboard())

    # Сохранение в историю (опционально, как у тебя было)
    if message.from_user and message.from_user.username and "Ошибка:" not in weather_info:
        try:
            await save_request(pool, message.from_user.username, city, datetime.datetime.now())
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

    # Сохранение в историю (опционально)
    if message.from_user and message.from_user.username and "Ошибка:" not in forecast_info:
        try:
            await save_request(pool, message.from_user.username, city, datetime.datetime.now())
        except Exception as e:
            logger.error(f"Error saving forecast request for {city}: {e}")

# --- Управление подписками ---
@router.message(F.text == "🔔 Мои подписки")
async def manage_subscriptions_menu_entry(message: Message, state: FSMContext):
    await state.clear()
    global pool; pool = pool or await get_pool()
    user_id = message.from_user.id
    try:
        subscriptions = await get_user_subscriptions(pool, user_id)
        if subscriptions:
            await state.set_state(WeatherStates.managing_subscription_city_choice)
            city_data = {sub['city']: sub for sub in subscriptions} # Сохраняем полные данные для быстрого доступа
            await state.update_data(subscribed_cities_data=city_data)
            await message.answer("Ваши подписки. Выберите город для управления:",
                                 reply_markup=subscribed_cities_reply_keyboard(subscriptions))
        else:
            await message.answer("У вас пока нет подписок.", reply_markup=subscriptions_initial_menu_keyboard())
    except Exception as e:
        logger.error(f"Error fetching subs for {user_id}: {e}", exc_info=True)
        await message.answer("Ошибка загрузки подписок.", reply_markup=main_menu_keyboard())

@router.message(WeatherStates.managing_subscription_city_choice, F.text)
async def process_chosen_city_for_management(message: Message, state: FSMContext):
    chosen_text = message.text.strip()
    user_data = await state.get_data()
    subscribed_cities_data = user_data.get("subscribed_cities_data", {})

    if chosen_text == "➕ Добавить новый город":
        await state.set_state(WeatherStates.waiting_for_city_subscribe)
        await message.answer("Введите название города для новой подписки:", reply_markup=back_to_main_menu_keyboard())
    elif chosen_text == "◀️ Назад в главное меню":
        await state.clear(); await message.answer("Вы в главном меню.", reply_markup=main_menu_keyboard())
    elif chosen_text in subscribed_cities_data:
        await state.update_data(city_being_managed=chosen_text)
        await state.set_state(WeatherStates.managing_specific_city_action_choice)
        sub_details = subscribed_cities_data[chosen_text]
        time_obj = sub_details.get('notification_time')
        tz_str = sub_details.get('timezone', 'UTC')
        time_str = time_obj.strftime('%H:%M') if time_obj else "08:00"
        await message.answer(f"Управление подпиской: {chosen_text}\n(Утро: {time_str} {tz_str}, +Осадки). Действие?",
                             reply_markup=city_management_actions_reply_keyboard())
    else:
        await message.reply("Выберите город кнопками.")

@router.message(WeatherStates.managing_specific_city_action_choice, F.text)
async def process_city_management_action(message: Message, state: FSMContext):
    action_text = message.text.strip()
    user_data = await state.get_data()
    city_to_manage = user_data.get("city_being_managed")
    if not city_to_manage: await state.clear(); await message.answer("Ошибка. Начните снова.", reply_markup=main_menu_keyboard()); return

    if action_text == "⚙️ Настроить время/пояс":
        await state.update_data(configuring_city=city_to_manage) # Для след. шага
        await state.set_state(WeatherStates.choosing_timezone_text_input)
        await message.answer(f"Настройка для г. {city_to_manage}.\nШаг 1: Выберите часовой пояс:",
                             reply_markup=timezone_choice_reply_keyboard())
    elif action_text == "➖ Отписаться от этого города":
        # ... (логика отписки, как ты ее написал, с remove_subscription) ...
        global pool; pool = pool or await get_pool()
        try:
            await remove_subscription(pool, message.from_user.id, city_to_manage)
            await state.clear()
            await message.answer(f"🗑 Вы отписались от г. {city_to_manage}.", reply_markup=main_menu_keyboard())
        except Exception as e:
            logger.error(f"Ошибка отписки от {city_to_manage}: {e}", exc_info=True)
            await state.clear(); await message.answer("Ошибка при отписке.", reply_markup=main_menu_keyboard())
    elif action_text == "◀️ Назад к списку городов":
        # Вернуться к выбору города (вызвать часть manage_subscriptions_menu_entry)
        subscriptions = await get_user_subscriptions(pool, message.from_user.id)
        await state.set_state(WeatherStates.managing_subscription_city_choice)
        city_data = {sub['city']: sub for sub in subscriptions}
        await state.update_data(subscribed_cities_data=city_data)
        await message.answer("Выберите город:", reply_markup=subscribed_cities_reply_keyboard(subscriptions))
    else:
        await message.reply("Выберите действие кнопками.", reply_markup=city_management_actions_reply_keyboard())


# Ожидание выбора города из ReplyKeyboard для управления
@router.message(WeatherStates.managing_subscription_city_choice, F.text)
async def process_chosen_city_for_management(message: Message, state: FSMContext):
    chosen_text = message.text.strip()
    user_data = await state.get_data()
    subscribed_cities = user_data.get("subscribed_cities", [])

    if chosen_text == "➕ Подписаться на новый город":
        await state.set_state(WeatherStates.waiting_for_city_subscribe)
        await message.answer("Введите название города для новой подписки:",
                             reply_markup=back_to_main_menu_keyboard())  # Кнопка "Назад в главное меню"
        return
    elif chosen_text == "◀️ Назад в главное меню":
        await state.clear()
        await message.answer("Вы в главном меню.", reply_markup=main_menu_keyboard())
        return
    elif chosen_text in subscribed_cities:  # Пользователь выбрал один из своих городов
        await state.update_data(city_being_managed=chosen_text)  # Сохраняем город
        await state.set_state(WeatherStates.managing_specific_city_action_choice)
        # Ищем данные для этого города, чтобы показать текущие настройки
        raw_subs = user_data.get("raw_subscriptions", [])
        current_sub_details = next((s for s in raw_subs if s['city'] == chosen_text), None)
        time_str = current_sub_details['notification_time'].strftime(
            '%H:%M') if current_sub_details and current_sub_details.get('notification_time') else "08:00"
        tz_str = current_sub_details.get('timezone', 'UTC') if current_sub_details else "UTC"

        await message.answer(
            f"Управление подпиской на г. {chosen_text}.\n"
            f"Текущие настройки утреннего прогноза: {time_str} (таймзона: {tz_str}).\n"
            "Выберите действие:",
            reply_markup=city_management_actions_reply_keyboard(chosen_text)
        )
    else:
        await message.reply("Пожалуйста, выберите город с помощью кнопок.")
        # Клавиатура subscribed_cities_reply_keyboard остается активной


# Ожидание выбора действия для конкретного города
@router.message(WeatherStates.managing_specific_city_action_choice, F.text)
async def process_city_management_action(message: Message, state: FSMContext):
    action_text = message.text.strip()
    user_data = await state.get_data()
    city_to_manage = user_data.get("city_being_managed")

    if not city_to_manage:  # Если вдруг города нет в состоянии
        await state.clear()
        await message.answer("Произошла ошибка. Пожалуйста, начните с 'Мои подписки'.",
                             reply_markup=main_menu_keyboard())
        return

    if action_text == "⚙️ Настроить время/пояс":
        await state.set_state(WeatherStates.choosing_timezone_text_input)
        # configuring_city уже есть как city_being_managed в state.update_data()
        await state.update_data(configuring_city=city_to_manage)  # Перезапишем на всякий случай
        await message.answer(f"Настройка для г. {city_to_manage}.\n"
                             "Шаг 1: Выберите часовой пояс с помощью кнопок ниже.",
                             reply_markup=timezone_choice_reply_keyboard())
    elif action_text == "➖ Отписаться от этого города":
        global pool
        if not pool: pool = await get_pool()
        user_id = message.from_user.id
        try:
            await remove_subscription(pool, user_id, city_to_manage)
            await state.clear()
            await message.answer(f"🗑 Вы отписались от г. {city_to_manage}.", reply_markup=main_menu_keyboard())
        except Exception as e:
            logger.error(f"Ошибка отписки от {city_to_manage}: {e}", exc_info=True)
            await state.clear()
            await message.answer("Ошибка при отписке.", reply_markup=main_menu_keyboard())
    elif action_text == "◀️ Назад к списку городов":
        # Повторяем логику из manage_subscriptions_menu_entry
        subscriptions = await get_user_subscriptions(pool, message.from_user.id)
        if subscriptions:
            await state.set_state(WeatherStates.managing_subscription_city_choice)
            subscribed_city_names = [sub['city'] for sub in subscriptions]
            await state.update_data(subscribed_cities=subscribed_city_names, raw_subscriptions=subscriptions)
            await message.answer("Выберите город:", reply_markup=subscribed_cities_reply_keyboard(subscriptions))
        else:
            await state.clear()
            await message.answer("У вас больше нет подписок.", reply_markup=main_menu_keyboard())
    else:
        await message.reply("Пожалуйста, выберите действие кнопками.",
                            reply_markup=city_management_actions_reply_keyboard(city_to_manage))


# Хендлер для текстовой кнопки "➕ Подписаться на город" (из subscriptions_menu_keyboard)
@router.message(F.text == "➕ Подписаться на город")
async def text_ask_city_to_subscribe(message: Message, state: FSMContext): # Переименовал для ясности
    await state.set_state(WeatherStates.waiting_for_city_subscribe)
    await message.answer("Введите название города для подписки:", reply_markup=back_to_main_menu_keyboard())  # Кнопка "Назад в главное меню"


@router.message(WeatherStates.waiting_for_city_subscribe, F.text)
async def process_new_city_for_subscription(message: Message, state: FSMContext):
    city_input = message.text.strip()
    user_id = message.from_user.id # Получим user_id в начале

    if city_input == "◀️ Назад в главное меню":
        await state.clear()
        await message.answer("Подписка отменена. Вы вернулись в главное меню.", reply_markup=main_menu_keyboard())
        return

    if not city_input or "/" in city_input:
        await message.reply("Некорректное название города. Попробуйте еще раз или вернитесь в меню.",
                            reply_markup=back_to_main_menu_keyboard()) # Используем back_to_main_menu_keyboard для консистентности
        return

    global pool
    if not pool:
        pool = await get_pool()

    weather_check = await get_weather(city_input)
    if "Ошибка:" in weather_check:
        await message.reply(f"Город '{city_input}' не найден или произошла ошибка при проверке API. Попробуйте другой город.",
                            reply_markup=back_to_main_menu_keyboard())
        return

    # --- Определение часового пояса (упрощенный пример) ---
    user_timezone_str = "UTC"  # Значение по умолчанию
    city_lower = city_input.lower()
    if city_lower == "москва":
        user_timezone_str = "Europe/Moscow"
    elif city_lower == "владивосток":
        user_timezone_str = "Asia/Vladivostok"
    elif "дуала" in city_lower or "камерун" in city_lower: # Если город Камеруна - Дуала
        user_timezone_str = "Africa/Douala" # UTC+1
    elif city_lower == "лондон":
        user_timezone_str = "Europe/London"
    elif city_lower == "нью-йорк":
        user_timezone_str = "America/New_York"
    # Добавь другие города по необходимости
    logger.info(f"Для города '{city_input}' определена таймзона: {user_timezone_str}")
    # --- Конец определения часового пояса ---

    try:
        # ... определение user_timezone_str ...
        await add_subscription(pool, message.from_user.id, city_input, "08:00:00", user_timezone_str)
        await state.update_data(configuring_city=city_input, current_timezone=user_timezone_str)
        await state.set_state(WeatherStates.choosing_timezone_text_input)
        await message.answer(f"✅ Город {city_input} добавлен (утро в 08:00, пояс {user_timezone_str}).\n"
                             "Настроим время/пояс? Шаг 1: Выберите часовой пояс:",
                             reply_markup=timezone_choice_reply_keyboard())
    except Exception as e:
        logger.error(f"Ошибка добавления подписки на {city_input}: {e}", exc_info=True)
        await state.clear();
        await message.answer("Ошибка добавления подписки.", reply_markup=main_menu_keyboard()) # Очищаем состояние в случае ошибки


# Шаг 2 настройки: выбор таймзоны (теперь через текст)
@router.message(WeatherStates.choosing_timezone_text_input, F.text)
async def process_timezone_choice_text_input(message: Message, state: FSMContext):
    chosen_tz_text = message.text.strip()
    user_data = await state.get_data()
    city_being_configured = user_data.get("configuring_city")
    if not city_being_configured: await state.clear(); await message.answer("Ошибка. Начните снова.",reply_markup=main_menu_keyboard()); return

    if chosen_tz_text == "◀️ Отмена настройки (в главное меню)": # Новая кнопка
        await state.clear()
        await message.answer(f"Настройка для г. {city_being_configured} отменена.", reply_markup=main_menu_keyboard())
        return

    selected_timezone_iana = POPULAR_TIMEZONES_TEXT_REPLY.get(chosen_tz_text)
    if not selected_timezone_iana:
        await message.reply("Выберите часовой пояс кнопками.", reply_markup=timezone_choice_reply_keyboard())
        return
    await state.update_data(selected_timezone=selected_timezone_iana)
    await state.set_state(WeatherStates.entering_notification_time_text_input)
    await message.answer(f"Пояс: {selected_timezone_iana}.\nШаг 2: Введите время (ЧЧ:ММ):",
                         reply_markup=back_to_main_menu_keyboard())


# Шаг 3 настройки: ввод времени
@router.message(WeatherStates.entering_notification_time_text_input, F.text)
async def process_notification_time_text_input(message: Message, state: FSMContext):
    time_input_str = message.text.strip()
    if time_input_str == "◀️ Назад в главное меню":
        await state.clear();
        await message.answer("Настройка отменена.", reply_markup=main_menu_keyboard());
        return
    try:
        parsed_time = datetime.datetime.strptime(time_input_str, "%H:%M").time()
        time_for_db = parsed_time.strftime("%H:%M:00")
    except ValueError:
        await message.reply("Неверный формат времени (ЧЧ:ММ).", reply_markup=back_to_main_menu_keyboard());
        return

    user_data = await state.get_data()
    city, tz = user_data.get("configuring_city"), user_data.get("selected_timezone")
    if not city or not tz: await state.clear(); await message.answer("Ошибка. Начните снова.",
                                                                     reply_markup=main_menu_keyboard()); return

    global pool;
    pool = pool or await get_pool()
    try:
        await add_subscription(pool, message.from_user.id, city, time_for_db, tz)
        await message.answer(f"👍 Настройки для г. {city} сохранены: {parsed_time.strftime('%H:%M')} ({tz}).",
                             reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"Ошибка сохр. настроек подписки: {e}", exc_info=True)
        await message.answer("Не удалось сохранить.", reply_markup=main_menu_keyboard())
    finally:
        await state.clear()



# Хендлер для текстовой кнопки "➖ Отписаться от города"
@router.message(F.text == "➖ Отписаться от города", flags={"description": "Начать процесс отписки от города"})
async def ask_for_city_to_unsubscribe_text(message: Message, state: FSMContext):
    logger.info(f"User {message.from_user.id} pressed '➖ Отписаться от города' text button.")
    await state.clear()  # Сбрасываем предыдущее состояние на всякий случай

    global pool
    if not pool:
        pool = await get_pool()

    user_id = message.from_user.id
    try:
        subscriptions = await get_user_subscriptions(pool, user_id)  # Эта функция должна возвращать список подписок

        if not subscriptions:
            await message.answer("У вас нет активных подписок для отмены.",
                                 reply_markup=subscriptions_menu_keyboard())  # Или main_menu_keyboard()
            return

        # Формируем список городов для подсказки
        city_names = [sub['city'] for sub in subscriptions]
        subs_list_text = "\n".join([f"- {name}" for name in city_names])

        await state.set_state(WeatherStates.waiting_for_city_unsubscribe)
        await message.answer(
            f"От какого города вы хотите отписаться?\nВаши текущие подписки:\n{subs_list_text}\n\n"
            "Пожалуйста, введите название города точно так, как оно указано в списке, или нажмите '◀️ Назад в меню'.",
            reply_markup=back_keyboard()  # Клавиатура с кнопкой "Назад в меню"
        )
    except Exception as e:
        logger.error(f"Error in ask_for_city_to_unsubscribe_text for user {user_id}: {e}", exc_info=True)
        await message.answer("Произошла ошибка при попытке начать отписку. Попробуйте позже.",
                             reply_markup=main_menu_keyboard())


@router.message(WeatherStates.waiting_for_city_unsubscribe, F.text)
async def process_city_for_unsubscription_text(message: Message, state: FSMContext):
    """
    Этот хендлер обрабатывает текстовый ввод города от пользователя,
    когда бот находится в состоянии ожидания города для отписки.
    """
    city_to_unsubscribe_input = message.text.strip()
    user_id = message.from_user.id
    logger.info(
        f"User {user_id} entered '{city_to_unsubscribe_input}' for unsubscription. State: {await state.get_state()}")

    if city_to_unsubscribe_input == "◀️ Назад в меню":
        await state.clear()
        # Решаем, куда вернуть пользователя. Если он пришел из меню подписок, то туда.
        # Если нет, то в главное меню. Для простоты - в главное.
        await message.answer("Отписка отменена. Вы вернулись в главное меню.",
                             reply_markup=main_menu_keyboard())
        return

    if not city_to_unsubscribe_input:
        await message.reply(
            "Название города не может быть пустым. Пожалуйста, введите город для отписки или вернитесь в меню.",
            reply_markup=back_keyboard())
        return  # Остаемся в том же состоянии

    global pool
    if not pool:
        pool = await get_pool()

    try:
        # Важно: нужно проверить, что введенный город действительно есть в подписках пользователя,
        # чтобы избежать попытки удалить несуществующую подписку или подписку на чужой город (хотя user_id защищает).
        current_subscriptions = await get_user_subscriptions(pool, user_id)
        found_subscription_city = None
        for sub in current_subscriptions:
            if sub['city'].lower() == city_to_unsubscribe_input.lower():
                found_subscription_city = sub['city']  # Берем точное имя из БД для удаления
                break

        if not found_subscription_city:
            await message.reply(
                f"У вас нет активной подписки на город '{city_to_unsubscribe_input}'. "
                "Пожалуйста, проверьте название и попробуйте снова, или вернитесь в меню.",
                reply_markup=back_keyboard()
            )
            return  # Остаемся в том же состоянии

        await remove_subscription(pool, user_id, found_subscription_city)  # Используем точное имя
        await message.answer(
            f"🗑 Вы успешно отписались от уведомлений для г. {found_subscription_city}.",
            reply_markup=main_menu_keyboard()  # Возвращаем в главное меню
        )
    except Exception as e:
        logger.error(f"Error during text unsubscription for user {user_id}, city '{city_to_unsubscribe_input}': {e}",
                     exc_info=True)
        await message.answer("Произошла ошибка во время отписки. Попробуйте позже.",
                             reply_markup=main_menu_keyboard())
    finally:
        await state.clear()


async def show_history(message: Message):  # Убери state: FSMContext, если он не используется
    global pool
    if not pool: pool = await get_pool()
    logger.info(f"User {message.from_user.id} requested history (via show_history function).")

    username = message.from_user.username
    if not username:
        await message.answer("У вас не установлен username в Telegram. История не может быть показана.",
                             reply_markup=main_menu_keyboard())
        return

    try:
        rows = await get_history(pool, username)  # Функция из database.py
        if not rows:
            await message.answer("История запросов пуста.", reply_markup=main_menu_keyboard())
            return

        history_text_parts = [f"📍 {idx + 1}. {row['city']} — {row['request_time'].strftime('%Y-%m-%d %H:%M')}"
                              for idx, row in enumerate(rows)]
        history_text = "\n".join(history_text_parts)

        # Ограничение длины сообщения
        if len(history_text) + len("🕘 Ваша история запросов (последние 10):\n") > 4096:  # Стандартный лимит Telegram
            history_text = "Слишком много записей для отображения. Вот часть из них:\n" + history_text[
                                                                                          :3900] + "\n(...)"  # Оставляем запас

        await message.answer(f"🕘 Ваша история запросов (последние 10):\n{history_text}",
                             reply_markup=main_menu_keyboard())
    except Exception as e:
        logger.error(f"Error fetching/showing history for username {username}: {e}", exc_info=True)
        await message.answer("Ошибка при получении истории.", reply_markup=main_menu_keyboard())


@router.message(F.text == "📜 Моя история")
async def history_via_button(message: Message):  # Убрал state: FSMContext
    logger.info(f">>> HISTORY VIA BUTTON HANDLER TRIGGERED for user {message.from_user.id}")
    await show_history(message)


@router.message(Command("history"))  # Если хочешь оставить и команду /history
async def history_command_handler(message: Message):  # Убрал state: FSMContext
    logger.info(f">>> HISTORY COMMAND HANDLER TRIGGERED for user {message.from_user.id}")
    await show_history(message)


@router.message()
async def catch_all_messages_debug(message: Message, state: FSMContext):
    logger.error(f"!!!!!!!! CATCH_ALL_MESSAGE (DEBUG) !!!!!!!")
    logger.error(f"Text: '{message.text}'") # САМОЕ ВАЖНОЕ
    logger.error(f"Chat ID: {message.chat.id}")
    logger.error(f"User ID: {message.from_user.id}")
    logger.error(f"Content Type: {message.content_type}")
    logger.error(f"Full Message Object: {message.model_dump_json(indent=2)}")
    current_fsm_state = await state.get_state()
    logger.error(f"Current FSM State: {current_fsm_state}")

# --- ПЛАНИРОВЩИК: ДВЕ ФУНКЦИИ РАССЫЛКИ ---
# 1. send_daily_morning_forecast_local_time (код из предыдущего ответа, который учитывает timezone и notification_time)
# ------------------------------------------------------------------
# Отправка утреннего (или любого заданного) прогноза по локальному
# времени из подписки. Вызывается планировщиком каждую минуту.
# ------------------------------------------------------------------
async def send_daily_morning_forecast_local_time() -> None:
    global pool, bot

    # safety-check
    if not pool or not bot:
        logger.warning("Scheduler: pool/bot not initialized")
        return

    now_utc = datetime.datetime.now(pytz.utc).replace(microsecond=0)

    # --- берём все активные подписки ---
    try:
        subscriptions = await get_all_active_subscriptions_with_details(pool)
    except Exception as e:
        logger.error(f"Scheduler: DB error: {e}", exc_info=True)
        return

    if not subscriptions:
        return

    # --- обрабатываем каждую подписку ---
    for sub in subscriptions:
        user_id   = sub.get("user_id")
        city      = sub.get("city")
        notif_tm  = sub.get("notification_time")      # TIME
        tz_name   = sub.get("timezone") or "UTC"
        last_sent = sub.get("last_daily_sent_at")     # TIMESTAMP, может быть None

        # пропускаем неполные записи
        if not user_id or not city or notif_tm is None:
            continue

        # защита: если уже слали < 60 сек назад
        if last_sent and (now_utc - last_sent).total_seconds() < 60:
            continue

        # --- локальное время пользователя ---
        try:
            user_tz = pytz.timezone(tz_name)
        except pytz.UnknownTimeZoneError:
            logger.error(f"Scheduler: unknown tz {tz_name} for user {user_id}")
            continue

        local_today   = now_utc.astimezone(user_tz).date()
        local_target  = datetime.datetime.combine(local_today, notif_tm)
        local_target  = user_tz.localize(local_target, is_dst=None)
        target_utc_dt = local_target.astimezone(pytz.utc).replace(microsecond=0)

        # --- попали в окно ±30 сек? ---
        if abs((now_utc - target_utc_dt).total_seconds()) > 30:
            continue

        logger.info(f"Scheduler: sending forecast for {city} (user {user_id})")

        # --- получаем погоду ----
        weather_txt = await get_weather(city)
        if "Ошибка:" in weather_txt:
            logger.warning(f"Scheduler: weather API error for {city}: {weather_txt}")
            continue

        # --- шлём сообщение ---
        msg = (
            f"☀️ Доброе утро!\n\n"
            f"Погода в {city} на {notif_tm.strftime('%H:%M')} "
            f"(ваш пояс {tz_name}):\n\n{weather_txt}"
        )
        try:
            await bot.send_message(user_id, msg)
            logger.info(f"Scheduler: sent to {user_id} for {city}")

            # фиксируем время последней отправки
            try:
                await update_last_daily_sent_time(pool, user_id, city, now_utc)
            except Exception as e:
                logger.error(f"Scheduler: can't update last_daily_sent_time: {e}", exc_info=True)

        except Exception as e:
            logger.error(f"Scheduler: telegram send error: {e}", exc_info=True)



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
@app.get("/")
async def root():
    logger.info("Root endpoint '/' was called.")
    return {"status": "alive"}


@app.post("/webhook") # <--- ВОТ ОН, КЛЮЧЕВОЙ ОБРАБОТЧИК!
async def telegram_webhook(request: Request):
    logger.info(">>> Webhook endpoint CALLED!")
    try:
        body = await request.json()
        # Логируем только часть тела, чтобы не переполнять логи, если оно большое
        logger.info(f">>> Webhook BODY received (keys): {list(body.keys()) if isinstance(body, dict) else 'Not a dict'}")
        if logger.level == logging.DEBUG: # Полное тело только в DEBUG режиме
             logger.debug(f">>> Full Webhook BODY: {body}")

        update = types.Update(**body) # Используем types.Update для корректного маппинга
        logger.info(">>> Update object CREATED.")
        await dp.feed_update(bot=bot, update=update) # Передаем именованные аргументы
        logger.info(">>> dp.feed_update COMPLETED.")
        return {"ok": True}
    except Exception as e:
        logger.exception(">>> EXCEPTION in webhook processing:")
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
    scheduler.add_job(send_daily_morning_forecast_local_time, CronTrigger(minute='*', timezone=pytz.utc),
                      # minute='*' - каждую минуту
                      id="every_minute_check_for_local_morning", replace_existing=True)
    logger.info("Scheduler: Job 'every_minute_check_for_local_morning' set (every minute).")

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