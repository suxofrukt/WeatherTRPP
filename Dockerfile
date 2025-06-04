# 1. Базовый образ с Python
FROM python:3.11-slim

# 2. Устанавливаем рабочую директорию внутри контейнера
WORKDIR /app

# 3. Копируем файл зависимостей (requirements.txt)
COPY requirements.txt .

# 4. Устанавливаем зависимости
RUN pip install --no-cache-dir -r requirements.txt

# 5. Копируем весь код проекта
COPY . .

# 6. Устанавливаем переменные окружения по умолчанию (переопределяются в .env или docker-compose)
ENV TELEGRAM_TOKEN=placeholder
ENV WEATHER_API_KEY=placeholder
ENV DATABASE_URL=placeholder

# 7. Команда запуска
CMD ["python", "api.py"]
