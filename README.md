# 🌤 WeatherTRPP — Telegram-бот прогноза погоды

**WeatherTRPP** — это Telegram-бот, который предоставляет пользователю текущую погоду и прогноз, а также умеет присылать:
- 🕗 **Ежедневные утренние прогнозы** в выбранное пользователем время;
- 🌧 **Оповещения об ухудшении погоды** (осадки, снег, гроза и пр.).

Бот использует API OpenWeather и реализован с использованием `Aiogram`, `FastAPI`, `PostgreSQL` и `APScheduler`.

## 🚀 Возможности
- Получение текущей погоды и прогноза по команде `/weather <город>`
- Подписка на утренние уведомления и оповещения о дождях
- Выбор города, времени и часового пояса
- Логгирование всех запросов пользователя

## 🛠 Стек технологий

| Компонент        | Технология        |
|------------------|-------------------|
| Telegram Bot     | Aiogram (v3)      |
| API-сервер       | FastAPI           |
| Планировщик      | APScheduler       |
| База данных      | PostgreSQL        |
| Внешний API      | OpenWeatherMap    |
| Виртуальное окружение | Python 3.11 (.venv) |

## 📦 Зависимости

Проект использует следующие основные зависимости (указаны в `requirements.txt`):

```txt
aiogram==3.*
fastapi==0.110.*
asyncpg
apscheduler
requests
python-dotenv
pytz
```

## ⚙️ Установка и запуск проекта

1. **Клонируйте репозиторий**
```bash
git clone https://github.com/ваш_пользователь/WeatherTRPP.git
cd WeatherTRPP
```

2. **Создайте и активируйте виртуальное окружение**
```bash
python3 -m venv .venv
source .venv/bin/activate  # или .venv\Scripts\activate в Windows
```

3. **Установите зависимости**
```bash
pip install -r requirements.txt
```

4. **Настройте переменные окружения**

Создайте `.env` файл с содержимым:

```env
TELEGRAM_TOKEN=ваш_токен_бота
WEATHER_API_KEY=ваш_ключ_от_OpenWeather
DATABASE_URL=postgresql+asyncpg://postgres:пароль@localhost:5432/weather_db
```

5. **Запустите API-сервер и бота**
```bash
uvicorn api:app --reload
```

## 👥 Команда проекта

| Имя | Роль |
|-----|------|
| Участник 1 | Backend Developer (Bot Logic) |
| Участник 2 | Backend Developer (Scheduler) |
| Участник 3 | API & Database Engineer |

## 📎 Примечания

- Поддерживаются только города, распознаваемые OpenWeather.
- Бот работает в UTC, но учитывает локальный часовой пояс пользователя для уведомлений.
- Все уведомления реализованы с учётом предотвращения дублирования.
