import logging
from contextlib import asynccontextmanager

import redis.asyncio as aioredis
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import text

from app.config import settings
from app.db.seed import seed_database
from app.models.database import AsyncSessionLocal, create_tables
from app.services.startup_checks import run_startup_checks

logger = logging.getLogger(__name__)
redis_client: aioredis.Redis | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global redis_client
    logging.basicConfig(level=settings.LOG_LEVEL, format="%(asctime)s | %(levelname)s | %(name)s | %(message)s")
    redis_client = aioredis.Redis.from_url(settings.REDIS_URL, decode_responses=True)
    try:
        await redis_client.ping()
        logger.info("Redis connected")
    except Exception as exc:
        logger.error("Redis connection failed: %s", exc)

    await create_tables()
    logger.info("Database tables ready")
    async with AsyncSessionLocal() as db:
        await seed_database(db)
    logger.info("Seed data ready")
    startup_errors = await run_startup_checks()
    if startup_errors:
        print("Startup checks found issues:")
        for error in startup_errors:
            print(f" - {error}")
    print(
        """
+----------------------------------------------------------+
|        Expense Processing Agent  v1.0.0                  |
|        WhatsApp -> Priority ERP Integration              |
|        Dashboard : http://localhost:8000                 |
|        Webhook   : http://localhost:8000/webhook/twilio  |
+----------------------------------------------------------+
        """
    )
    yield
    if redis_client is not None:
        await redis_client.aclose()


app = FastAPI(title="Expense Processing Agent", version="1.0.0", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

from app.api import dashboard, webhook  # noqa: E402

app.include_router(webhook.router, prefix="/webhook")
app.include_router(dashboard.router)


@app.get("/health")
async def health():
    redis_ok = False
    try:
        if redis_client is not None:
            await redis_client.ping()
            redis_ok = True
    except Exception:
        pass
    db_ok = False
    try:
        async with AsyncSessionLocal() as db:
            await db.execute(text("SELECT 1"))
        db_ok = True
    except Exception:
        pass
    return {"status": "ok", "redis": redis_ok, "db": db_ok}
