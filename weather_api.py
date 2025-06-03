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

PRECIPITATION_CODES = {
    # Drizzle
    300, 301, 302, 310, 311, 312, 313, 314, 321,
    # Rain
    500, 501, 502, 503, 504, 511, 520, 521, 522, 531,
    # Snow
    600, 601, 602, 611, 612, 613, 615, 616, 620, 621, 622,
    # Thunderstorm
    200, 201, 202, 210, 211, 212, 221, 230, 231, 232
}


async def check_for_precipitation_in_forecast(city: str, hours_ahead: int = 6):
    #Проверяет прогноз на наличие осадков (дождь/снег) в ближайшие `hours_ahead` часов.
    # Используем 3-часовой прогноз
    url = f"https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={WEATHER_API_KEY}&units=metric&lang=ru"

    # Делаем синхронный запрос. Для улучшения можно перейти на aiohttp.
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()  # Вызовет ошибку для 4xx/5xx ответов
        data = response.json()
    except requests.RequestException as e:
        # Логируем ошибку и возвращаем None, т.к. не можем получить прогноз
        print(
            f"Error fetching forecast for {city}: {e}")  # Замени на logger.error, если weather_api имеет доступ к логгеру
        return None

    if str(data.get("cod")) != "200":
        return None

    # Рассчитываем, сколько 3-часовых интервалов нужно проверить
    intervals_to_check = (hours_ahead + 2) // 3  # Округляем вверх

    for forecast_item in data.get("list", [])[:intervals_to_check]:
        weather_id = forecast_item.get("weather", [{}])[0].get("id")
        if weather_id in PRECIPITATION_CODES:
            # Нашли осадки! Возвращаем описание погоды.
            description = forecast_item.get("weather", [{}])[0].get("description", "осадки")
            dt_txt = forecast_item.get("dt_txt", "")
            return f"Ожидаются осадки ({description}) примерно в {dt_txt.split(' ')[1][:5]}."

    # Если в цикле не нашли осадков, возвращаем None
    return None