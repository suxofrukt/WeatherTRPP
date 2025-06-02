import os
import logging
import requests
import asyncio
from aiogram import Bot, Dispatcher, types
from aiogram.types import Message
from aiogram.filters import Command
from dotenv import load_dotenv
from weather_api import get_weather, get_forecast
from database import get_pool, save_request
from datetime import datetime


pool = None
# Загружаем токены из .env
load_dotenv()
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

# Настраиваем логирование
logging.basicConfig(level=logging.INFO)

# Создаём бота и диспетчер
bot = Bot(token=TELEGRAM_TOKEN)
dp = Dispatcher()

# Функция для запроса погоды
async def get_weather(city):
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
    response = requests.get(url)
    data = response.json()

    if data.get("cod") != 200:
        return f"Ошибка: {data.get('message', 'Город не найден')}"

    # Разбираем JSON
    weather_desc = data["weather"][0]["description"].capitalize()
    temp = data["main"]["temp"]
    humidity = data["main"]["humidity"]
    wind_speed = data["wind"]["speed"]

    return (f"🌍 Погода в {city}:\n"
            f"🌡 Температура: {temp}°C\n"
            f"💨 Ветер: {wind_speed} м/с\n"
            f"💧 Влажность: {humidity}%\n"
            f"☁ {weather_desc}")

# Команда /start
@dp.message(Command("start"))
async def start_command(message: Message):
    await message.answer("Привет! Напиши /weather <город>, чтобы узнать погоду.\nПример: `/weather Москва`")

# Команда /weather <город>
@dp.message(Command("weather"))
async def weather_command(message: Message):
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
    args = message.text.split(maxsplit=1)
    if len(args) < 2:
        await message.answer("Напиши команду в формате: `/forecast Москва`")
        return

    city = args[1]
    forecast_info = await get_forecast(city)
    await message.answer(forecast_info)
    await save_request(pool, message.from_user.username, city, datetime.now())




if __name__ == '__main__':
    import asyncio

    async def main():
        global pool
        pool = await get_pool()
        await dp.start_polling(bot)

    asyncio.run(main())
