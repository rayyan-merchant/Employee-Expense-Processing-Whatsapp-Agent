from unittest.mock import MagicMock

import pytest

from app.services.priority_erp import PriorityERPClient
from app.services.startup_checks import run_startup_checks


async def test_startup_checks_detect_missing_api_key(mocker):
    mocker.patch("app.services.startup_checks.settings.GOOGLE_API_KEY", "your_key")
    redis_factory = mocker.patch("redis.asyncio.Redis.from_url")
    redis_factory.return_value.ping = mocker.AsyncMock(return_value=True)
    redis_factory.return_value.aclose = mocker.AsyncMock(return_value=None)
    errors = await run_startup_checks()
    assert any("GOOGLE_API_KEY" in error for error in errors)


async def test_startup_checks_detect_redis_down(mocker):
    redis_factory = mocker.patch("redis.asyncio.Redis.from_url")
    redis_factory.return_value.ping = mocker.AsyncMock(side_effect=Exception("down"))
    redis_factory.return_value.aclose = mocker.AsyncMock(return_value=None)
    errors = await run_startup_checks()
    assert any("Redis unavailable" in error for error in errors)


async def test_priority_upload_skipped_if_already_uploaded(mocker):
    expense = MagicMock()
    expense.id = "abcd1234"
    expense.priority_document_id = "EXP-ABCD1234"
    client = PriorityERPClient()
    result = await client.create_expense(expense)
    assert result.success is True
    assert result.document_no == "EXP-ABCD1234"


def test_process_task_skips_missing_expense(mocker):
    from app.tasks.expense_tasks import process_expense_task

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    mocker.patch("app.models.database.SyncSessionLocal", return_value=db)
    process_expense_task.run("missing", "972501234567")
    db.close.assert_called_once()


def test_upload_task_skips_missing_expense(mocker):
    from app.tasks.expense_tasks import upload_to_priority_task

    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    mocker.patch("app.models.database.SyncSessionLocal", return_value=db)
    upload_to_priority_task.run("missing", "972501234567")
    db.close.assert_called_once()


def test_upload_task_idempotent_if_already_uploaded(mocker):
    from app.tasks.expense_tasks import upload_to_priority_task

    expense = MagicMock()
    expense.priority_status = "UPLOADED"
    expense.priority_document_id = "EXP-EXISTS"
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = expense
    mocker.patch("app.models.database.SyncSessionLocal", return_value=db)
    client = mocker.patch("app.tasks.expense_tasks.PriorityERPClient", create=True)
    upload_to_priority_task.run("exists", "972501234567")
    client.assert_not_called()


def test_process_task_rejected_branch(mocker):
    from app.services.policy_engine import PolicyDecision, PolicyResult
    from app.tasks.expense_tasks import process_expense_task

    expense = MagicMock()
    expense.amount = 0
    expense.currency = "NIS"
    expense.expense_date = "2026-05-24"
    expense.category = "Meals"
    expense.language = "en"
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = expense
    mocker.patch("app.models.database.SyncSessionLocal", return_value=db)
    engine = mocker.patch("app.services.policy_engine.SyncPolicyEngine")
    engine.return_value.validate.return_value = PolicyResult(PolicyDecision.REJECTED, "bad")
    mocker.patch("app.fsm.conversation.SyncConversationFSM")
    mocker.patch("app.services.whatsapp.WhatsAppService")
    process_expense_task.run("exp", "972501234567")
    assert expense.approval_status == "REJECTED"


def test_process_task_auto_approve_branch(mocker):
    from app.services.policy_engine import PolicyDecision, PolicyResult
    from app.tasks.expense_tasks import process_expense_task

    expense = MagicMock()
    expense.amount = 250
    expense.currency = "NIS"
    expense.expense_date = "2026-05-24"
    expense.category = "Meals"
    expense.language = "en"
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = expense
    mocker.patch("app.models.database.SyncSessionLocal", return_value=db)
    engine = mocker.patch("app.services.policy_engine.SyncPolicyEngine")
    engine.return_value.validate.return_value = PolicyResult(PolicyDecision.AUTO_APPROVE, "ok")
    mocker.patch("app.fsm.conversation.SyncConversationFSM")
    delay = mocker.patch("app.tasks.expense_tasks.upload_to_priority_task.delay")
    process_expense_task.run("exp", "972501234567")
    delay.assert_called_once()


def test_upload_task_success_branch(mocker):
    from app.services.priority_erp import PriorityExpenseResult
    from app.tasks.expense_tasks import upload_to_priority_task

    expense = MagicMock()
    expense.priority_status = "NOT_UPLOADED"
    expense.priority_document_id = None
    expense.language = "en"
    expense.id = "abcd1234"
    expense.amount = 250
    expense.currency = "NIS"
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = expense
    mocker.patch("app.models.database.SyncSessionLocal", return_value=db)
    client_cls = mocker.patch("app.services.priority_erp.PriorityERPClient")
    client_cls.return_value.create_expense = mocker.AsyncMock(return_value=PriorityExpenseResult(success=True, document_no="EXP-OK"))
    mocker.patch("app.fsm.conversation.SyncConversationFSM")
    mocker.patch("app.services.whatsapp.WhatsAppService")
    upload_to_priority_task.run("exp", "972501234567")
    assert expense.priority_status == "UPLOADED"
    assert expense.priority_document_id == "EXP-OK"
