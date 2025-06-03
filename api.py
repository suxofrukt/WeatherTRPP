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
    waiting_for_city_current = State()
    waiting_for_city_forecast = State()

    # Для новой подписки
    waiting_for_city_subscribe = State()

    # Для управления существующими подписками
    managing_subscription_city_choice = State()  # Ожидание выбора города (текстом) для управления
    managing_specific_city_action_choice = State()  # Ожидание выбора действия для города (текстом)

    # Для настройки времени и таймзоны (остаются)
    choosing_timezone_text_input = State()  # Ожидание выбора таймзоны (текстом)
    entering_notification_time_text_input = State()

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
        [KeyboardButton(text="◀️ Назад в главное меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=True)

def back_to_main_menu_keyboard(): # Просто кнопка назад
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="◀️ Назад в главное меню")]], resize_keyboard=True, one_time_keyboard=True)

def back_to_subs_list_keyboard(): # Для возврата к списку городов
    return ReplyKeyboardMarkup(keyboard=[[KeyboardButton(text="◀️ Назад к списку городов")]], resize_keyboard=True, one_time_keyboard=True)

def subscribed_cities_reply_keyboard(subscriptions: list):
    buttons = []
    for sub in subscriptions:
        buttons.append([KeyboardButton(text=sub['city'])])
    buttons.append([KeyboardButton(text="➕ Подписаться на новый город")])
    buttons.append([KeyboardButton(text="◀️ Назад в главное меню")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=False) # False - чтобы оставалась

# Клавиатура с действиями для выбранного города
def city_management_actions_reply_keyboard(city_name: str): # city_name не нужен для текстов кнопок, если они общие
    kb = [
        [KeyboardButton(text="⚙️ Настроить время/пояс")],
        [KeyboardButton(text="➖ Отписаться от этого города")],
        [KeyboardButton(text="◀️ Назад к списку городов")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

def back_keyboard():  # Для отмены ввода города
    kb = [[KeyboardButton(text="◀️ Назад в меню")]]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)


# Словарь популярных таймзон для Inline-кнопок
POPULAR_TIMEZONES_TEXT_REPLY = { # Тексты кнопок будут ключами для проверки
    "Москва (UTC+3)": "Europe/Moscow", "Лондон (GMT/BST)": "Europe/London",
    "Екатеринбург (UTC+5)": "Asia/Yekaterinburg", "Нью-Йорк (EST/EDT)": "America/New_York",
    "Новосибирск (UTC+7)": "Asia/Novosibirsk", "Лос-Анджелес (PST/PDT)": "America/Los_Angeles",
    "Владивосток (UTC+10)": "Asia/Vladivostok", "Берлин (CET/CEST)": "Europe/Berlin",
    "Токио (UTC+9)": "Asia/Tokyo", "UTC": "UTC",
}


def timezone_choice_reply_keyboard():
    buttons = []
    row = []
    for display_name in POPULAR_TIMEZONES_TEXT_REPLY.keys():
        row.append(KeyboardButton(text=display_name))
        if len(row) >= 2: # По 2 в ряд
            buttons.append(row)
            row = []
    if row: buttons.append(row)
    buttons.append([KeyboardButton(text="◀️ Отмена настройки")]) # Кнопка для отмены
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=True)

def initial_config_reply_keyboard(city: str):
    kb = [
        [KeyboardButton(text=f"⚙️ Настроить время и пояс для {city}")],
        [KeyboardButton(text="👌 Оставить по умолчанию (08:00)")], # Уточнил текст
        [KeyboardButton(text="◀️ В главное меню")]
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

def initial_config_keyboard(city: str):  # Клавиатура после успешной подписки
    buttons = [
        [InlineKeyboardButton(text=f"⚙️ Настроить время и пояс для {city}", callback_data=f"cfgtime_{city}")],
        [InlineKeyboardButton(text="👌 Оставить по умолчанию (08:00, пояс города)", callback_data=f"cfgdef_{city}")]
    ]
    return InlineKeyboardMarkup(inline_keyboard=buttons)

def subscribed_cities_keyboard(subscriptions: list, add_new_city_button: bool = True, back_to_main_button: bool = True):
    buttons = []
    for sub in subscriptions:
        buttons.append([KeyboardButton(text=sub['city'])]) # Кнопка для каждого города
    if add_new_city_button:
        buttons.append([KeyboardButton(text="➕ Подписаться на новый город")])
    if back_to_main_button:
        buttons.append([KeyboardButton(text="◀️ Назад в главное меню")])
    return ReplyKeyboardMarkup(keyboard=buttons, resize_keyboard=True, one_time_keyboard=False) # one_time=False, чтобы не скрывалась

def city_management_actions_keyboard(city_name: str):
    kb = [
        [KeyboardButton(text=f"⚙️ Настроить время/пояс для {city_name}")],
        [KeyboardButton(text=f"➖ Отписаться от {city_name}")],
        [KeyboardButton(text="◀️ Назад к списку городов")] # Кнопка для возврата
    ]
    return ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)

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
            InlineKeyboardButton(text=f"⚙️ Настр. {city}", callback_data=f"cfgtime_{city}"),
            InlineKeyboardButton(text=f"➖ Отпис. {city}", callback_data=f"unsub_{city}")
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
    global pool
    if not pool: pool = await get_pool()
    user_id = message.from_user.id
    try:
        subscriptions = await get_user_subscriptions(pool, user_id)
        if subscriptions:
            await state.set_state(WeatherStates.managing_subscription_city_choice)
            subscribed_city_names = [sub['city'] for sub in subscriptions]
            await state.update_data(subscribed_cities=subscribed_city_names,  # Сохраняем список городов
                                    raw_subscriptions=subscriptions)  # И полные данные о подписках
            await message.answer("Выберите город для управления или добавьте новый:",
                                 reply_markup=subscribed_cities_reply_keyboard(subscriptions))
        else:
            await message.answer("У вас пока нет подписок.",
                                 reply_markup=subscriptions_menu_keyboard())  # Кнопки "Подписаться", "Назад"
    except Exception as e:
        logger.error(f"Error fetching subscriptions for user {user_id}: {e}", exc_info=True)
        await message.answer("Не удалось загрузить ваши подписки.", reply_markup=main_menu_keyboard())


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
async def ask_city_to_subscribe(message: Message, state: FSMContext):  # Переименовал для ясности
    await state.set_state(WeatherStates.waiting_for_city_subscribe)
    await message.answer("Введите название города для подписки:",
                         reply_markup=back_to_main_menu_keyboard())  # Кнопка "Назад в главное меню"


# api.py

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
        # Время уведомления всегда 08:00:00 по умолчанию при новой подписке
        await add_subscription(pool, user_id, city_input,
                               notification_time_str="08:00:00",
                               user_timezone_str=user_timezone_str)
        logger.info(f"Успешно добавлена подписка для user {user_id} на город {city_input} с таймзоной {user_timezone_str}")

        # После успешной подписки предлагаем сразу настроить
        await state.update_data(
            configuring_city=city_input,          # Город для настройки
            current_timezone=user_timezone_str,   # Текущая (дефолтная) таймзона
            current_notif_time="08:00"            # Текущее (дефолтное) время
        )
        await state.set_state(WeatherStates.choosing_timezone_text_input) # Сразу переходим к выбору таймзоны для нового города
        await message.answer(
            f"✅ Город {city_input} добавлен в ваши подписки!\n"
            f"Утренний прогноз по умолчанию будет в 08:00 (таймзона: {user_timezone_str}).\n\n"
            "Теперь давайте настроим время и часовой пояс для утренних уведомлений.\n"
            "Шаг 1: Выберите часовой пояс с помощью кнопок ниже.",
            reply_markup=timezone_choice_reply_keyboard() # Используем ReplyKeyboard для выбора таймзоны
        )
    except Exception as e:
        logger.error(f"Ошибка при добавлении подписки для user {user_id}, город {city_input}: {e}", exc_info=True)
        await message.answer("Произошла ошибка при добавлении подписки. Попробуйте позже.",
                             reply_markup=main_menu_keyboard())
        await state.clear() # Очищаем состояние в случае ошибки


# ... (обработка ошибки) ...

# Шаг 2 настройки: выбор таймзоны (теперь через текст)
@router.message(WeatherStates.choosing_timezone_text_input, F.text)
async def process_timezone_choice_text_input(message: Message, state: FSMContext):
    chosen_tz_text = message.text.strip()
    user_data = await state.get_data()
    city_being_configured = user_data.get("configuring_city")  # Получаем город из состояния

    if not city_being_configured:  # Проверка
        await state.clear();
        await message.answer("Ошибка. Начните с 'Мои подписки'.", reply_markup=main_menu_keyboard());
        return

    if chosen_tz_text == "◀️ Отмена настройки":
        await state.clear()
        await message.answer(f"Настройка для г. {city_being_configured} отменена.", reply_markup=main_menu_keyboard())
        return

    selected_timezone_iana = POPULAR_TIMEZONES_TEXT_REPLY.get(chosen_tz_text)  # Ищем IANA по тексту кнопки

    if not selected_timezone_iana:
        await message.reply("Пожалуйста, выберите часовой пояс кнопками.",
                            reply_markup=timezone_choice_reply_keyboard())
        return

    await state.update_data(selected_timezone=selected_timezone_iana)
    await state.set_state(WeatherStates.entering_notification_time_text_input)
    await message.answer(f"Для г. {city_being_configured} выбран пояс: {selected_timezone_iana}.\n"
                         "Шаг 2: Введите желаемое время (ЧЧ:ММ, например, 07:30).",
                         reply_markup=back_to_main_menu_keyboard())  # Кнопка "Назад в главное меню" для отмены

# Шаг 3 настройки: ввод времени
@router.message(WeatherStates.entering_notification_time_text_input, F.text)
async def process_notification_time_text_input(message: Message, state: FSMContext):
    time_input_str = message.text.strip()

    if time_input_str == "◀️ Назад в главное меню":  # Отмена на этом шаге
        await state.clear()
        await message.answer("Настройка отменена.", reply_markup=main_menu_keyboard())
        return
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
                                      state: FSMContext):
    logger.info(f">>> CB: cb_process_unsubscribe_city called with data: {callback_query.data}")
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


@router.callback_query(F.data == "cfg_back_main")
async def cb_back_to_main_menu_from_subs_list(callback_query: types.CallbackQuery, state: FSMContext):
    logger.info(">>> CB: cb_back_to_main_menu_from_subs_list called")
    await callback_query.answer()
    await state.clear()
    try:
        await callback_query.message.edit_text("Вы вернулись в главное меню.")
    except Exception as e:
        logger.warning(f"Could not edit message for cb_back_to_main_menu_from_subs_list: {e}")
        # Если редактирование не удалось, новое сообщение все равно будет отправлено ниже
    # Отправляем ReplyKeyboard главного меню
    await bot.send_message(callback_query.from_user.id, "Выберите действие:", reply_markup=main_menu_keyboard())

@router.callback_query()
async def catch_all_callbacks_debug(callback_query: types.CallbackQuery, state: FSMContext):
    await callback_query.answer("DEBUG: Callback поймано catch_all") # Чтобы убрать "загрузку"
    logger.error(f"!!!!!!!! CATCH_ALL_CALLBACK (DEBUG) !!!!!!!")
    logger.error(f"Data: '{callback_query.data}'") # САМОЕ ВАЖНОЕ
    logger.error(f"From User ID: {callback_query.from_user.id}")
    logger.error(f"Message ID: {callback_query.message.message_id if callback_query.message else 'N/A'}")
    logger.error(f"Full CallbackQuery Object: {callback_query.model_dump_json(indent=2)}")
    current_fsm_state = await state.get_state()
    logger.error(f"Current FSM State: {current_fsm_state}")

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