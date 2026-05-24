import logging

from sqlalchemy import func, select, text

from app.config import settings
from app.models.database import AsyncSessionLocal
from app.models.policy import ExpensePolicy

logger = logging.getLogger(__name__)


async def run_startup_checks() -> list[str]:
    errors: list[str] = []
    required = [
        ("TWILIO_ACCOUNT_SID", settings.TWILIO_ACCOUNT_SID),
        ("TWILIO_AUTH_TOKEN", settings.TWILIO_AUTH_TOKEN),
        ("GOOGLE_API_KEY", settings.GOOGLE_API_KEY),
    ]
    for name, value in required:
        if not value or value.startswith("your_") or "xxxx" in value.lower():
            errors.append(f"{name} is not configured")

    if settings.TWILIO_ACCOUNT_SID and not settings.TWILIO_ACCOUNT_SID.startswith("AC"):
        errors.append("TWILIO_ACCOUNT_SID should start with 'AC'")
    if settings.GOOGLE_API_KEY and not settings.GOOGLE_API_KEY.startswith("AIza"):
        errors.append("GOOGLE_API_KEY format looks wrong (should start with 'AIza')")

    try:
        import redis.asyncio as aioredis

        redis_client = aioredis.Redis.from_url(settings.REDIS_URL)
        await redis_client.ping()
        await redis_client.aclose()
        logger.info("Redis startup check passed")
    except Exception as exc:
        errors.append(f"Redis unavailable: {exc}")

    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        logger.info("Database startup check passed")
    except Exception as exc:
        errors.append(f"Database unavailable: {exc}")

    try:
        async with AsyncSessionLocal() as db:
            count = await db.scalar(select(func.count()).select_from(ExpensePolicy))
            if count == 0:
                errors.append("No expense policies found - run seed_database()")
            else:
                logger.info("Policies startup check passed: %s loaded", count)
    except Exception as exc:
        errors.append(f"Seed check failed: {exc}")

    for error in errors:
        logger.warning("Startup check: %s", error)
    return errors
