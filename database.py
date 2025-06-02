import os
from dotenv import load_dotenv
import asyncpg

load_dotenv()

async def get_pool():
    return await asyncpg.create_pool(
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        database=os.getenv("POSTGRES_DB"),
        host=os.getenv("POSTGRES_HOST"),
        port=int(os.getenv("POSTGRES_PORT")),
        ssl="require"
    )


async def save_request(pool, username, city, dt):
    async with pool.acquire() as connection:
        await connection.execute(
            "INSERT INTO weather_requests (username, city, request_time) VALUES ($1, $2, $3)",
            username, city, dt
        )

async def get_history(pool, username):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT city, request_time FROM weather_requests
            WHERE username = $1
            ORDER BY request_time DESC
            LIMIT 10
        """, username)
        return rows

async def add_subscription(pool, user_id: int, city: str, notification_time: str = "08:00:00", timezone: str = "UTC"):
    async with pool.acquire() as conn:
        # Проверяем, существует ли уже такая подписка, и если да, активируем её
        # или обновляем (если нужно будет менять время/таймзону в будущем)
        await conn.execute("""
            INSERT INTO subscriptions (user_id, city, notification_time, timezone, is_active)
            VALUES ($1, $2, $3::TIME, $4, TRUE)
            ON CONFLICT (user_id, city) DO UPDATE
            SET notification_time = EXCLUDED.notification_time,
                timezone = EXCLUDED.timezone,
                is_active = TRUE;
        """, user_id, city, notification_time, timezone)

async def remove_subscription(pool, user_id: int, city: str):
    async with pool.acquire() as conn:
        # Деактивируем подписку, а не удаляем, чтобы сохранить историю
        await conn.execute("""
            UPDATE subscriptions SET is_active = FALSE
            WHERE user_id = $1 AND city = $2;
        """, user_id, city)

async def get_user_subscriptions(pool, user_id: int):
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT city, notification_time, timezone FROM subscriptions
            WHERE user_id = $1 AND is_active = TRUE;
        """, user_id)
        return rows

async def get_active_subscriptions_for_notification(pool, current_utc_time_str: str):
    #Получает подписки, для которых пришло время уведомления.
    #Сравнивает время без учета даты.
    async with pool.acquire() as conn:
        rows = await conn.fetch("""
            SELECT user_id, city FROM subscriptions
            WHERE is_active = TRUE AND notification_time = $1::TIME;
        """, current_utc_time_str)
        return rows
