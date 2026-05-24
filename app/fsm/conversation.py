import json
import logging
from datetime import datetime

import redis as sync_redis
import redis.asyncio as aioredis
from pydantic import ValidationError

from app.config import settings
from app.models.schemas import ConversationState

CONVERSATION_TTL = 3600
logger = logging.getLogger(__name__)


class ConversationFSM:
    """Async FSM used by FastAPI handlers."""

    def __init__(self, redis_client: aioredis.Redis):
        self.redis = redis_client

    def _key(self, phone: str) -> str:
        return f"conv:{phone}"

    async def get_state(self, phone: str) -> ConversationState | None:
        raw = await self.redis.get(self._key(phone))
        if not raw:
            return None
        try:
            return ConversationState(**json.loads(raw))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.error("Corrupted state for ...%s: %s. Resetting.", phone[-4:], exc)
            await self.redis.delete(self._key(phone))
            return None

    async def set_state(self, phone: str, state: ConversationState) -> None:
        state.updated_at = datetime.utcnow().isoformat()
        await self.redis.setex(self._key(phone), CONVERSATION_TTL, json.dumps(state.model_dump()))

    async def transition(self, phone: str, new_state_name: str, **updates) -> ConversationState:
        state = await self.get_state(phone)
        if state is None:
            state = ConversationState(state=new_state_name, phone=phone)
        state.state = new_state_name
        for key, value in updates.items():
            if hasattr(state, key):
                setattr(state, key, value)
        await self.set_state(phone, state)
        return state

    async def reset(self, phone: str) -> None:
        await self.redis.delete(self._key(phone))

    async def acquire_lock(self, phone: str, timeout_seconds: int = 10) -> bool:
        result = await self.redis.set(f"lock:{phone}", "1", nx=True, ex=timeout_seconds)
        return result is not None

    async def release_lock(self, phone: str) -> None:
        await self.redis.delete(f"lock:{phone}")

    async def is_duplicate_message(self, phone: str, message_sid: str) -> bool:
        state = await self.get_state(phone)
        if state is None:
            return False
        return state.last_processed_message_sid == message_sid

    async def mark_message_processed(self, phone: str, message_sid: str) -> None:
        state = await self.get_state(phone) or ConversationState(state="IDLE", phone=phone)
        await self.transition(phone, state.state, last_processed_message_sid=message_sid)


class SyncConversationFSM:
    """Synchronous FSM used inside Celery tasks."""

    def __init__(self):
        self.redis = sync_redis.Redis.from_url(settings.REDIS_URL, decode_responses=True)

    def _key(self, phone: str) -> str:
        return f"conv:{phone}"

    def get_state(self, phone: str) -> ConversationState | None:
        raw = self.redis.get(self._key(phone))
        if not raw:
            return None
        try:
            return ConversationState(**json.loads(raw))
        except (json.JSONDecodeError, ValidationError, TypeError) as exc:
            logger.error("Corrupted state for ...%s: %s. Resetting.", phone[-4:], exc)
            self.redis.delete(self._key(phone))
            return None

    def transition(self, phone: str, new_state_name: str, **updates) -> ConversationState:
        state = self.get_state(phone) or ConversationState(state=new_state_name, phone=phone)
        state.state = new_state_name
        for key, value in updates.items():
            if hasattr(state, key):
                setattr(state, key, value)
        state.updated_at = datetime.utcnow().isoformat()
        self.redis.setex(self._key(phone), CONVERSATION_TTL, json.dumps(state.model_dump()))
        return state

    def reset(self, phone: str) -> None:
        self.redis.delete(self._key(phone))

    def acquire_lock(self, phone: str, timeout_seconds: int = 10) -> bool:
        return self.redis.set(f"lock:{phone}", "1", nx=True, ex=timeout_seconds) is not None

    def release_lock(self, phone: str) -> None:
        self.redis.delete(f"lock:{phone}")
