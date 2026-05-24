import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.config import settings
from app.main import app
from app.models.database import Base, get_db

TEST_DB_URL = "sqlite+aiosqlite:///:memory:"
TEST_REDIS_DB = 3


@pytest_asyncio.fixture
async def test_db():
    engine = create_async_engine(TEST_DB_URL)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    session_factory = async_sessionmaker(engine, expire_on_commit=False)
    async with session_factory() as session:
        from app.db.seed import seed_database

        await seed_database(session)
        yield session
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
    await engine.dispose()


@pytest_asyncio.fixture
async def redis_client():
    client = aioredis.Redis(host="localhost", port=6379, db=TEST_REDIS_DB, decode_responses=True)
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture
def mock_whatsapp(mocker):
    return mocker.patch("app.services.whatsapp.WhatsAppService.send_message", return_value="SM_test_message_sid_123")


@pytest.fixture
def mock_whatsapp_sync(mocker):
    return mocker.patch("app.services.whatsapp.WhatsAppService.send_message_sync", return_value="SM_test_message_sid_123")


@pytest.fixture
def twilio_form_params():
    def _make(phone="972501234567", body="Test message", has_media=False, media_url=None, sid="SM_test_123"):
        params = {
            "MessageSid": sid,
            "AccountSid": "AC_test",
            "From": f"whatsapp:+{phone}",
            "To": "whatsapp:+14155238886",
            "Body": body,
            "NumMedia": "1" if has_media else "0",
        }
        if has_media and media_url:
            params["MediaUrl0"] = media_url
            params["MediaContentType0"] = "image/jpeg"
        return params

    return _make


@pytest.fixture
def twilio_headers(mocker):
    mocker.patch("app.services.whatsapp.WhatsAppService.validate_signature", return_value=True)

    def _make(params: dict) -> dict:
        return {"X-Twilio-Signature": "valid_test_signature"}

    return _make


@pytest.fixture(autouse=True)
def celery_delays(mocker):
    mocker.patch("app.tasks.expense_tasks.process_expense_task.delay", return_value=None)
    mocker.patch("app.tasks.expense_tasks.upload_to_priority_task.delay", return_value=None)


@pytest_asyncio.fixture
async def client(test_db, redis_client):
    async def override_get_db():
        yield test_db

    app.dependency_overrides[get_db] = override_get_db
    import app.main as main_module

    main_module.redis_client = redis_client
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c
    app.dependency_overrides.clear()
