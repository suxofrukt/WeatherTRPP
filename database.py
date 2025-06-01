import os
from dotenv import load_dotenv
import asyncpg

load_dotenv()

async def get_pool():
    print("🔍 HOST =", os.getenv("POSTGRES_HOST"))
    print("🔍 USER =", os.getenv("POSTGRES_USER"))
    print("🔍 PASS =", os.getenv("POSTGRES_PASSWORD"))
    print("🔍 PORT =", os.getenv("POSTGRES_PORT"))
    print("🔍 DB   =", os.getenv("POSTGRES_DB"))

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
