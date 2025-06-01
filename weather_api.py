import requests
import os
from dotenv import load_dotenv

load_dotenv()
WEATHER_API_KEY = os.getenv("WEATHER_API_KEY")

def format_weather_response(data, city):
    weather_desc = data["weather"][0]["description"].capitalize()
    temp = data["main"]["temp"]
    humidity = data["main"]["humidity"]
    wind_speed = data["wind"]["speed"]

    return (f"🌍 Погода в {city}:\n"
            f"🌡 Температура: {temp}°C\n"
            f"💨 Ветер: {wind_speed} м/с\n"
            f"💧 Влажность: {humidity}%\n"
            f"☁ {weather_desc}")

async def get_weather(city):
    url = f"https://api.openweathermap.org/data/2.5/weather?q={city}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
    response = requests.get(url)
    data = response.json()

    if data.get("cod") != 200:
        return f"Ошибка: {data.get('message', 'Город не найден')}"

    return format_weather_response(data, city)

async def get_forecast(city):
    url = f"https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
    response = requests.get(url)
    data = response.json()

    if data.get("cod") != "200":
        return f"Ошибка: {data.get('message', 'Город не найден')}"

    forecast_text = f"📅 Прогноз погоды для {city} (3 дня):\n"
    count = 0
    for item in data["list"]:
        dt_txt = item["dt_txt"]
        if "12:00:00" in dt_txt:
            desc = item["weather"][0]["description"].capitalize()
            temp = item["main"]["temp"]
            forecast_text += f"\n📆 {dt_txt[:10]}: {temp}°C, {desc}"
            count += 1
        if count == 3:
            break

    return forecast_text

def detect_weather_alerts(data):
    alerts = []
    temp = data["main"]["temp"]
    wind = data["wind"]["speed"]

    if temp < -10:
        alerts.append("🧊 Очень холодно!")
    elif temp > 30:
        alerts.append("🔥 Жара!")

    if wind > 10:
        alerts.append("🌪 Сильный ветер!")

    return alerts
