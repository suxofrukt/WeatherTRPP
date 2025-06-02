import os
import logging
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router, F
from aiogram.types import Update, Message, ReplyKeyboardMarkup, KeyboardButton
from aiogram.filters import Command, CommandStart
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup

from dotenv import load_dotenv
from weather_api import get_weather, get_forecast
from database import get_pool, save_request, get_history
from datetime import datetime

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

# Подключение к БД
pool = None


# --- Клавиатуры ---
def main_menu_keyboard():
    kb = [
        [KeyboardButton(text="🌦 Погода сейчас")],
        [KeyboardButton(text="🗓 Прогноз на 3 дня")],
        [KeyboardButton(text="📜 Моя история")]
    ]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)
    return keyboard


def back_keyboard():
    kb = [[KeyboardButton(text="◀️ Назад в меню")]]
    keyboard = ReplyKeyboardMarkup(keyboard=kb, resize_keyboard=True, one_time_keyboard=False)
    return keyboard


# --- Состояния (States) для ввода города ---
class WeatherStates(StatesGroup):
    waiting_for_city_current = State()
    waiting_for_city_forecast = State()


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
    await state.clear()
    await message.answer("Вы вернулись в главное меню.", reply_markup=main_menu_keyboard())


# --- Погода сейчас ---
@router.message(F.text == "🌦 Погода сейчас")
async def ask_city_for_current_weather(message: Message, state: FSMContext):
    await state.set_state(WeatherStates.waiting_for_city_current)
    await message.answer(
        "Введите название города, для которого хотите узнать текущую погоду:",
        reply_markup=back_keyboard()
    )


@router.message(WeatherStates.waiting_for_city_current, F.text)
async def process_current_weather_city(message: Message, state: FSMContext):
    city = message.text
    if city == "◀️ Назад в меню":
        await state.clear()
        await message.answer("Вы вернулись в главное меню.", reply_markup=main_menu_keyboard())
        return

    if "/" in city:
        await message.answer(
            "Некорректное название города. Пожалуйста, введите снова или вернитесь в меню.",
            reply_markup=back_keyboard()
        )
        return

    await state.clear()
    global pool
    if not pool:
        pool = await get_pool()

    weather_info = await get_weather(city)
    await message.answer(weather_info, reply_markup=main_menu_keyboard())

    if message.from_user and message.from_user.username and "Ошибка:" not in weather_info:
        try:
            await save_request(pool, message.from_user.username, city, datetime.now())
        except Exception as e:
            logger.error(f"Error saving current weather request: {e}")


# --- Прогноз на 3 дня ---
@router.message(F.text == "🗓 Прогноз на 3 дня")
async def ask_city_for_forecast(message: Message, state: FSMContext):
    await state.set_state(WeatherStates.waiting_for_city_forecast)
    await message.answer(
        "Введите название города, для которого хотите узнать прогноз:",
        reply_markup=back_keyboard()
    )


@router.message(WeatherStates.waiting_for_city_forecast, F.text)
async def process_forecast_city(message: Message, state: FSMContext):
    city = message.text
    if city == "◀️ Назад в меню":
        await state.clear()
        await message.answer("Вы вернулись в главное меню.", reply_markup=main_menu_keyboard())
        return

    if "/" in city:
        await message.answer(
            "Некорректное название города. Пожалуйста, введите снова или вернитесь в меню.",
            reply_markup=back_keyboard()
        )
        return

    await state.clear()
    global pool
    if not pool:
        pool = await get_pool()

    forecast_info = await get_forecast(city)
    await message.answer(forecast_info, reply_markup=main_menu_keyboard())

    if message.from_user and message.from_user.username and "Ошибка:" not in forecast_info:
        try:
            await save_request(pool, message.from_user.username, city, datetime.now())
        except Exception as e:
            logger.error(f"Error saving forecast request: {e}")


# --- История запросов ---
async def show_history(message: Message):
    logger.info("API: /history command or button received by show_history")
    global pool
    try:
        if not pool:
            logger.info("API: Pool is None, creating new pool for history.")
            pool = await get_pool()
            logger.info("API: Pool created for history.")

        username = message.from_user.username
        if not username:
            await message.answer(
                "У вас не установлен username в Telegram. История не может быть показана.",
                reply_markup=main_menu_keyboard()
            )
            logger.warning(f"API: User {message.from_user.id} has no username for history.")
            return

        rows = await get_history(pool, username)
        logger.info(f"API: Rows fetched for history for {username}: {len(rows) if rows else 0} rows")

        if not rows:
            await message.answer("История запросов пуста.", reply_markup=main_menu_keyboard())
            return

        history_text_parts = [
            f"📍 {idx + 1}. {row['city']} — {row['request_time'].strftime('%Y-%m-%d %H:%M')}"
            for idx, row in enumerate(rows)
        ]
        history_text = "\n".join(history_text_parts)

        if len(history_text) > 4000:
            history_text = "Слишком много записей для отображения. Вот часть из них:\n" + history_text[
                                                                                          :3900] + "\n(...)"

        await message.answer(
            f"🕘 Ваша история запросов (последние 10):\n{history_text}",
            reply_markup=main_menu_keyboard()
        )
        logger.info(f"API: History sent for username: {username}")

    except Exception as e:
        logger.error(f"API: Error in show_history: {e}", exc_info=True)
        await message.answer(
            "Произошла ошибка при получении истории. Попробуйте позже.",
            reply_markup=main_menu_keyboard()
        )


@router.message(F.text == "📜 Моя история")
async def history_via_button(message: Message):
    await show_history(message)


@router.message(Command("history"))
async def history_command_handler(message: Message):
    await show_history(message)


# --- Webhook и Startup ---
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
async def on_startup():
    global pool
    logger.info("API: Application startup...")
    if pool is None:
        logger.info("API: Startup - creating database pool.")
        pool = await get_pool()
        logger.info("API: Database pool created on startup.")
    else:
        logger.info("API: Startup - database pool already exists.")

    webhook_info = await bot.get_webhook_info()
    if not webhook_info.url:
        logger.warning("Webhook is NOT SET. Please set it using set_webhook.py or other means.")
    else:
        logger.info(f"Webhook is set to: {webhook_info.url}")