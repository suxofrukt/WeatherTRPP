import os
from dotenv import load_dotenv
import asyncpg
import datetime
import logging
import pytz

load_dotenv()
logger = logging.getLogger(__name__)
ALLOWED_ALERT_FIELDS = {"last_alert_sent_at", "last_precip_alert_at"}

async def get_pool():
    return await asyncpg.create_pool(
        user=os.getenv("POSTGRES_USER"),
        password=os.getenv("POSTGRES_PASSWORD"),
        database=os.getenv("POSTGRES_DB"),
        host=os.getenv("POSTGRES_HOST"),
        port=int(os.getenv("POSTGRES_PORT")),
        ssl="require",
        statement_cache_size=0
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

async def add_subscription(pool, user_id: int, city: str, notification_time_str: str = "08:00:00", timezone: str = "UTC"):
    # Преобразуем строку времени в объект datetime.time
    try:
        time_parts = list(map(int, notification_time_str.split(':')))
        time_obj = datetime.time(hour=time_parts[0], minute=time_parts[1], second=time_parts[2] if len(time_parts) > 2 else 0)
    except ValueError:
        logger.error(f"Invalid time string format for notification_time: {notification_time_str}")
        raise ValueError(f"Invalid time format: {notification_time_str}. Expected HH:MM:SS or HH:MM")


    async with pool.acquire() as conn:
        await conn.execute("""
            INSERT INTO subscriptions (user_id, city, notification_time, timezone, is_active)
            VALUES ($1, $2, $3, $4, TRUE) -- Убираем ::TIME, так как передаем уже объект datetime.time
            ON CONFLICT (user_id, city) DO UPDATE
            SET notification_time = EXCLUDED.notification_time,
                timezone = EXCLUDED.timezone,
                is_active = TRUE;
        """, user_id, city, time_obj, timezone)

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

async def get_all_active_subscriptions_with_details(pool):
    """Получает все активные подписки с их деталями."""
    async with pool.acquire() as conn:
        # Добавляем выборку last_alert_sent_at
        rows = await conn.fetch("""
            SELECT user_id, city, notification_time, timezone, last_alert_sent_at FROM subscriptions
            WHERE is_active = TRUE;
        """)
        return rows

async def update_last_alert_time(
    pool,
    user_id: int,
    city: str,
    field_name: str = "last_alert_sent_at",
    timestamp: datetime.datetime | None = None,
) -> None:
    if field_name not in ALLOWED_ALERT_FIELDS:
        raise ValueError(f"update_last_alert_time: field '{field_name}' is not allowed")

    ts = timestamp or datetime.datetime.now(pytz.utc)

    async with pool.acquire() as conn:
        await conn.execute(
            f"""
            UPDATE subscriptions
            SET {field_name} = $3
            WHERE user_id = $1 AND city = $2
            """,
            user_id,
            city,
            ts,
        )

async def update_last_daily_sent_time(pool, user_id: int, city: str, dt: datetime.datetime):
    query = """
        UPDATE subscriptions
        SET last_daily_sent_at = $3
        WHERE user_id = $1 AND city = $2;
    """
    async with pool.acquire() as conn:
        await conn.execute(query, user_id, city, dt)

