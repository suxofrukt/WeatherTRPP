import os
import logging
import requests
import asyncio
from fastapi import FastAPI, Request
from aiogram import Bot, Dispatcher
from aiogram.types import Message, Update
from aiogram.filters import Command
from dotenv import load_dotenv
from weather_api import get_weather, get_forecast
from database import get_pool, save_request
from datetime import datetime

# Загружаем переменные окружения
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

# Настройка логов
logging.basicConfig(level=logging.INFO)

# Создаем FastAPI-приложение
app = FastAPI()

# Создаем бота и диспетчер
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Глобальный connection pool
pool = None

# 👇 Хендлеры команд
@dp.message(Command("start"))
async def start_command(message: Message):
    await message.answer("Привет! Напиши /weather <город>, чтобы узнать погоду.\nПример: `/weather Москва`")

@dp.message(Command("weather"))
async def weather_command(message: Message):
    global pool
    if not pool:
        pool = await get_pool()

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Напиши команду в формате: `/weather Москва`")
        return

    city = args[1]
    weather_info = await get_weather(city)
    await message.answer(weather_info)
    await save_request(pool, message.from_user.username, city, datetime.now())

@dp.message(Command("forecast"))
async def forecast_command(message: Message):
    global pool
    if not pool:
        pool = await get_pool()

    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Напиши команду в формате: `/forecast Москва`")
        return

    city = args[1]
    forecast_info = await get_forecast(city)
    await message.answer(forecast_info)
    await save_request(pool, message.from_user.username, city, datetime.now())

# 👇 Обработчик входящих обновлений (webhook)
@app.post("/webhook")
async def telegram_webhook(request: Request):
    body = await request.json()
    update = Update.model_validate(body)
    await dp.feed_update(bot, update)
    return {"ok": True}

# 👇 Инициализация connection pool при старте FastAPI
@app.on_event("startup")
async def on_startup():
    global pool
    pool = await get_pool()
    print("🚀 API запущен, pool создан")
