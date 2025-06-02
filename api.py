import os
import logging
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher, Router
from aiogram.types import Message, Update
from aiogram.filters import Command
from dotenv import load_dotenv
from weather_api import get_weather, get_forecast
from database import get_pool, save_request
from datetime import datetime

# 📀 Загрузка переменных окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

# 🔧 Логирование
logging.basicConfig(level=logging.INFO)

# 🚀 Создание FastAPI-приложения
app = FastAPI()

# 🔊 Настройка бота
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()
router = Router()
dp.include_router(router)

# 📆 Глобальный connection pool
pool = None

# 📄 Хендлер /start
@router.message(Command("start"))
async def start_command(message: Message):
    await message.answer("\u041f\u0440\u0438\u0432\u0435\u0442! \u041d\u0430\u043f\u0438\u0448\u0438 /weather <\u0433\u043e\u0440\u043e\u0434>, \u0447\u0442\u043e\u0431\u044b \u0443\u0437\u043d\u0430\u0442\u044c \u043f\u043e\u0433\u043e\u0434\u0443.\n\u041f\u0440\u0438\u043c\u0435\u0440: `/weather \u041c\u043e\u0441\u043a\u0432\u0430`")

# 📄 Хендлер /weather
@router.message(Command("weather"))
async def weather_command(message: Message):
    global pool
    if not pool:
        pool = await get_pool()

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("\u041d\u0430\u043f\u0438\u0448\u0438 \u043a\u043e\u043c\u0430\u043d\u0434\u0443 \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0435: `/weather \u041c\u043e\u0441\u043a\u0432\u0430`")
        return

    city = args[1]
    weather_info = await get_weather(city)
    await message.answer(weather_info)
    await save_request(pool, message.from_user.username, city, datetime.now())

# 📄 Хендлер /forecast
@router.message(Command("forecast"))
async def forecast_command(message: Message):
    global pool
    if not pool:
        pool = await get_pool()

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("\u041d\u0430\u043f\u0438\u0448\u0438 \u043a\u043e\u043c\u0430\u043d\u0434\u0443 \u0432 \u0444\u043e\u0440\u043c\u0430\u0442\u0435: `/forecast \u041c\u043e\u0441\u043a\u0432\u0430`")
        return

    city = args[1]
    forecast_info = await get_forecast(city)
    await message.answer(forecast_info)
    await save_request(pool, message.from_user.username, city, datetime.now())

# 📈 Webhook-обработчик
@app.post("/webhook")
async def telegram_webhook(request: Request):
    body = await request.json()
    update = Update.model_validate(body)
    await dp.feed_update(bot, update)
    return {"ok": True}

# 🚀 Инициализация при старте
@app.on_event("startup")
async def on_startup():
    global pool
    pool = await get_pool()
    print("\ud83d\ude80 API запущен, pool создан")
