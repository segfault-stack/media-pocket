import asyncio
import os
import re
import sys
import time

from redis.asyncio import Redis
from sqlalchemy import text
from sqlalchemy.ext.asyncio import create_async_engine


def _to_async_database_url(database_url: str) -> str:
    return re.sub(r"^postgresql(?:\+asyncpg)?:", "postgresql+asyncpg:", database_url)


HEARTBEAT_FILE = "/tmp/bot_heartbeat"
MAX_HEARTBEAT_AGE_SECONDS = 60.0


async def check_database() -> bool:
    db_url = os.environ.get("DATABASE_URL")
    if not db_url:
        print("DATABASE_URL is not set", file=sys.stderr)
        return False
    try:
        engine = create_async_engine(
            _to_async_database_url(db_url),
            connect_args={"timeout": 5} if "sqlite" in db_url else {},
        )
        async with engine.connect() as conn:
            await conn.execute(text("SELECT 1"))
        await engine.dispose()
        return True
    except Exception as exc:  # noqa: BLE001 - healthchecks must convert all failures to unhealthy
        print(f"Database check failed: {exc}", file=sys.stderr)
        return False


async def check_redis() -> bool:
    redis_url = os.environ.get("REDIS_URL")
    if not redis_url:
        print("REDIS_URL is not set", file=sys.stderr)
        return False
    client = Redis.from_url(redis_url)
    try:
        return bool(await client.ping())
    except Exception as exc:  # noqa: BLE001 - healthchecks must convert all failures to unhealthy
        print(f"Redis check failed: {exc}", file=sys.stderr)
        return False
    finally:
        await client.aclose()


def check_heartbeat() -> bool:
    if not os.path.exists(HEARTBEAT_FILE):
        print(f"Heartbeat file {HEARTBEAT_FILE} does not exist", file=sys.stderr)
        return False
    try:
        mtime = os.path.getmtime(HEARTBEAT_FILE)
        age = time.time() - mtime
        with open(HEARTBEAT_FILE, "r", encoding="utf-8") as f:
            content = f.read().strip()
            if content:
                ts = float(content)
                age = min(age, time.time() - ts)
        if age > MAX_HEARTBEAT_AGE_SECONDS:
            print(
                f"Heartbeat stale: age={age:.1f}s > {MAX_HEARTBEAT_AGE_SECONDS}s",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as exc:  # noqa: BLE001 - healthchecks must convert all failures to unhealthy
        print(f"Heartbeat check failed: {exc}", file=sys.stderr)
        return False


async def main():
    hb_ok = check_heartbeat()
    db_ok = await check_database()
    redis_ok = await check_redis()
    if hb_ok and db_ok and redis_ok:
        sys.exit(0)
    else:
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
