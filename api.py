import os
import logging
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Update, Message
from aiogram.filters import Command
from dotenv import load_dotenv
from weather_api import get_weather, get_forecast
from database import get_pool, save_request
from datetime import datetime

# Загрузка .env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

# Логирование
logging.basicConfig(level=logging.INFO)

# FastAPI приложение
app = FastAPI()

# Aiogram setup
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# Подключение к БД
pool = None

# Команды
@router.message(Command("start"))
async def start_command(message: Message):
    await message.answer("Привет! Напиши /weather <город>, чтобы узнать погоду.\nПример: /weather Москва")

@router.message(Command("weather"))
async def weather_command(message: Message):
    global pool
    if not pool:
        pool = await get_pool()

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Напиши команду в формате: /weather Москва")
        return

    city = args[1]
    weather_info = await get_weather(city)
    await message.answer(weather_info)
    await save_request(pool, message.from_user.username, city, datetime.now())

@router.message(Command("forecast"))
async def forecast_command(message: Message):
    global pool
    if not pool:
        pool = await get_pool()

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Напиши команду в формате: /forecast Москва")
        return

    city = args[1]
    forecast_info = await get_forecast(city)
    await message.answer(forecast_info)
    await save_request(pool, message.from_user.username, city, datetime.now())

# Webhook
@app.post("/webhook")
async def telegram_webhook(request: Request):
    try:
        body = await request.json()
        print("Получен update:", body)  # отладка
        update = Update(**body)
        await dp.feed_update(bot, update)
        return {"ok": True}
    except Exception as e:
        logging.exception("Ошибка обработки webhook:")
        return {"ok": False, "error": str(e)}

# Старт
@app.on_event("startup")
async def on_startup():
    global pool
    pool = await get_pool()
    print("API запущен, pool создан")
