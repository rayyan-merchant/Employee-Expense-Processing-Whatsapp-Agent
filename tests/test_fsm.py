import asyncio

from app.fsm.conversation import ConversationFSM
from app.models.schemas import ConversationState


async def test_get_state_none_for_new_user(redis_client):
    assert await ConversationFSM(redis_client).get_state("999999999") is None


async def test_set_and_get_state(redis_client):
    fsm = ConversationFSM(redis_client)
    await fsm.set_state("972501111111", ConversationState(state="IDLE", phone="972501111111"))
    retrieved = await fsm.get_state("972501111111")
    assert retrieved is not None
    assert retrieved.state == "IDLE"


async def test_transition_creates_if_not_exists(redis_client):
    state = await ConversationFSM(redis_client).transition("972502222222", "AWAITING_CATEGORY")
    assert state.state == "AWAITING_CATEGORY"


async def test_transition_updates_state_name(redis_client):
    fsm = ConversationFSM(redis_client)
    await fsm.transition("972503333333", "IDLE")
    state = await fsm.transition("972503333333", "RECEIPT_RECEIVED")
    assert state.state == "RECEIPT_RECEIVED"


async def test_transition_applies_kwargs(redis_client):
    state = await ConversationFSM(redis_client).transition("972504444444", "AWAITING_CATEGORY", lang="he", retries=1)
    assert state.lang == "he"
    assert state.retries == 1


async def test_reset_deletes_state(redis_client):
    fsm = ConversationFSM(redis_client)
    await fsm.transition("972505555555", "IDLE")
    await fsm.reset("972505555555")
    assert await fsm.get_state("972505555555") is None


async def test_state_ttl_is_set(redis_client):
    await ConversationFSM(redis_client).transition("972506666666", "IDLE")
    ttl = await redis_client.ttl("conv:972506666666")
    assert 3500 <= ttl <= 3600


async def test_duplicate_detection(redis_client):
    fsm = ConversationFSM(redis_client)
    await fsm.transition("972507777777", "IDLE")
    assert not await fsm.is_duplicate_message("972507777777", "SM_abc123")
    await fsm.mark_message_processed("972507777777", "SM_abc123")
    assert await fsm.is_duplicate_message("972507777777", "SM_abc123")


async def test_updated_at_changes_on_transition(redis_client):
    fsm = ConversationFSM(redis_client)
    s1 = await fsm.transition("972508888888", "IDLE")
    await asyncio.sleep(0.01)
    s2 = await fsm.transition("972508888888", "RECEIPT_RECEIVED")
    assert s2.updated_at >= s1.updated_at


async def test_expense_data_persisted_in_state(redis_client):
    expense = {"amount": 250.0, "currency": "NIS", "vendor": "Test"}
    fsm = ConversationFSM(redis_client)
    await fsm.transition("972509999999", "AWAITING_CONFIRMATION", expense_data=expense)
    state = await fsm.get_state("972509999999")
    assert state.expense_data["amount"] == 250.0
    assert state.expense_data["vendor"] == "Test"
