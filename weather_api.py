import requests
import os
from dotenv import load_dotenv
import datetime
import pytz


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
    200, 201, 202, 210, 211, 212, 221, 230, 231, 232,
    800, 801, 802, 803, 804, 805, 810, 811, 812
}


async def check_for_precipitation_in_forecast(city: str,
                                              min_lead_minutes: int = 30,
                                              max_lead_minutes: int = 120):
    url = f"https://api.openweathermap.org/data/2.5/forecast?q={city}&appid={WEATHER_API_KEY}&units=metric&lang=ru"
    try:
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as e:
        print(f"ERROR (check_for_precipitation_in_forecast): Ошибка получения прогноза для {city}: {e}")
        return None

    if str(data.get("cod")) != "200":
        # logger.warning(f"API error for {city}: {data.get('message')}")
        print(f"DEBUG (check_for_precipitation_in_forecast): API error for {city}: {data.get('message')}")
        return None

    city_timezone_offset_seconds = data.get("city", {}).get("timezone")
    current_utc_time = datetime.datetime.now(pytz.utc)

    intervals_to_check = ((max_lead_minutes // 60) + 2) // 3 + 1
    if intervals_to_check < 2:  # Минимум 2 интервала (6 часов прогноза), чтобы было из чего выбирать
        intervals_to_check = 2

    first_relevant_precipitation = None  # Будем хранить здесь первое подходящее предупреждение

    for forecast_item in data.get("list", [])[:intervals_to_check]:
        weather_id = forecast_item.get("weather", [{}])[0].get("id")
        if weather_id in PRECIPITATION_CODES:
            description = forecast_item.get("weather", [{}])[0].get("description", "осадки")
            dt_txt_utc_str = forecast_item.get("dt_txt", "")

            try:
                forecast_utc_time = pytz.utc.localize(datetime.datetime.strptime(dt_txt_utc_str, "%Y-%m-%d %H:%M:%S"))

                time_difference_minutes = (forecast_utc_time - current_utc_time).total_seconds() / 60.0

                # Условие: осадки в будущем, не ранее min_lead_minutes и не позднее max_lead_minutes
                if min_lead_minutes <= time_difference_minutes <= max_lead_minutes:
                    local_time_str = forecast_utc_time.strftime('%H:%M')  # По умолчанию UTC
                    if city_timezone_offset_seconds is not None:
                        city_tz = datetime.timezone(datetime.timedelta(seconds=city_timezone_offset_seconds))
                        local_time = forecast_utc_time.astimezone(city_tz)
                        local_time_str = local_time.strftime('%H:%M')

                    # Сохраняем это как кандидата и выходим из цикла, так как мы ищем *первое* подходящее
                    first_relevant_precipitation = f"Ожидаются осадки ({description}) примерно в {local_time_str} по местному времени (через ~{int(time_difference_minutes // 60)} ч {int(time_difference_minutes % 60)} мин)."
                    break  # Нашли первое подходящее, дальше не ищем
                # else:
                # print(f"DEBUG: Precipitation for {city} at {forecast_utc_time} is outside desired window ({min_lead_minutes}-{max_lead_minutes} min). Diff: {time_difference_minutes:.0f} min")
            except ValueError:
                print(
                    f"WARNING (check_for_precipitation_in_forecast): Could not parse forecast time {dt_txt_utc_str} for {city}")
                continue

    return first_relevant_precipitation