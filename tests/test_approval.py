from app.config import settings


async def test_approval_service_notifies_manager(mock_whatsapp_sync):
    from app.services.approval import SyncApprovalService
    from unittest.mock import MagicMock

    expense = MagicMock()
    expense.id = "abcd1234efgh5678"
    expense.amount = 850.0
    expense.currency = "NIS"
    expense.vendor = "Test"
    expense.category = "Entertainment"
    expense.expense_date = "2024-05-20"
    expense.description = "Client dinner"
    expense.whatsapp_number = "972501234567"
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    SyncApprovalService().notify_manager(expense, "972521234567", db)
    mock_whatsapp_sync.assert_called_once()


async def test_manager_phone_lookup_returns_seeded_manager(test_db):
    from app.services.approval import AsyncApprovalService

    manager = await AsyncApprovalService().get_manager_phone("972501234567", test_db)
    assert manager == "972521234567"


async def test_manager_phone_unknown_returns_default(test_db):
    from app.services.approval import AsyncApprovalService

    manager = await AsyncApprovalService().get_manager_phone("999000000000", test_db)
    assert manager == settings.DEFAULT_MANAGER_PHONE.replace("whatsapp:+", "")
