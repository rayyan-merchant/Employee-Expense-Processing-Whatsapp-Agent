from datetime import date, timedelta

import pytest

from app.services.policy_engine import PolicyDecision, PolicyEngine, SyncPolicyEngine
from app.tasks.expense_tasks import process_expense_task, upload_to_priority_task


async def test_meals_under_limit_auto_approved(test_db):
    result = await PolicyEngine().validate({"amount": 250.0, "currency": "NIS", "category": "Meals", "expense_date": date.today().isoformat()}, test_db)
    assert result.decision == PolicyDecision.AUTO_APPROVE


async def test_meals_needs_approval(test_db):
    result = await PolicyEngine().validate({"amount": 350.0, "currency": "NIS", "category": "Meals", "expense_date": date.today().isoformat()}, test_db)
    assert result.decision == PolicyDecision.NEEDS_APPROVAL


async def test_meals_over_max_rejected(test_db):
    result = await PolicyEngine().validate({"amount": 600.0, "currency": "NIS", "category": "Meals", "expense_date": date.today().isoformat()}, test_db)
    assert result.decision == PolicyDecision.REJECTED


async def test_travel_auto_approved(test_db):
    result = await PolicyEngine().validate({"amount": 800.0, "currency": "NIS", "category": "Travel", "expense_date": date.today().isoformat()}, test_db)
    assert result.decision == PolicyDecision.AUTO_APPROVE


async def test_travel_needs_approval(test_db):
    result = await PolicyEngine().validate({"amount": 1500.0, "currency": "NIS", "category": "Travel", "expense_date": date.today().isoformat()}, test_db)
    assert result.decision == PolicyDecision.NEEDS_APPROVAL


async def test_unknown_category_rejected(test_db):
    result = await PolicyEngine().validate({"amount": 100.0, "currency": "NIS", "category": "Pizza", "expense_date": date.today().isoformat()}, test_db)
    assert result.decision == PolicyDecision.REJECTED
    assert "Unknown" in result.reason


async def test_future_date_rejected(test_db):
    future = (date.today() + timedelta(days=5)).isoformat()
    result = await PolicyEngine().validate({"amount": 100.0, "currency": "NIS", "category": "Meals", "expense_date": future}, test_db)
    assert result.decision == PolicyDecision.REJECTED
    assert "future" in result.reason


async def test_meals_receipt_too_old(test_db):
    old_date = (date.today() - timedelta(days=45)).isoformat()
    result = await PolicyEngine().validate({"amount": 100.0, "currency": "NIS", "category": "Meals", "expense_date": old_date}, test_db)
    assert result.decision == PolicyDecision.REJECTED


async def test_travel_receipt_60_days_ok(test_db):
    old_date = (date.today() - timedelta(days=60)).isoformat()
    result = await PolicyEngine().validate({"amount": 500.0, "currency": "NIS", "category": "Travel", "expense_date": old_date}, test_db)
    assert result.decision == PolicyDecision.AUTO_APPROVE


async def test_disallowed_currency_rejected(test_db):
    result = await PolicyEngine().validate({"amount": 100.0, "currency": "JPY", "category": "Meals", "expense_date": date.today().isoformat()}, test_db)
    assert result.decision == PolicyDecision.REJECTED
    assert "JPY" in result.reason


async def test_gl_account_meals(test_db):
    assert await PolicyEngine().get_gl_account("Meals", test_db) == ("6110", "CC-GEN")


async def test_gl_account_software(test_db):
    assert await PolicyEngine().get_gl_account("Software", test_db) == ("6160", "CC-IT")


async def test_gl_account_unknown_raises(test_db):
    with pytest.raises(ValueError):
        await PolicyEngine().get_gl_account("Unknown", test_db)


async def test_boundary_at_threshold_auto_approved(test_db):
    result = await PolicyEngine().validate({"amount": 300.0, "currency": "NIS", "category": "Meals", "expense_date": date.today().isoformat()}, test_db)
    assert result.decision == PolicyDecision.AUTO_APPROVE


async def test_boundary_just_above_threshold(test_db):
    result = await PolicyEngine().validate({"amount": 300.01, "currency": "NIS", "category": "Meals", "expense_date": date.today().isoformat()}, test_db)
    assert result.decision == PolicyDecision.NEEDS_APPROVAL


async def test_software_large_needs_approval(test_db):
    result = await PolicyEngine().validate({"amount": 5000.0, "currency": "NIS", "category": "Software", "expense_date": date.today().isoformat()}, test_db)
    assert result.decision == PolicyDecision.NEEDS_APPROVAL


async def test_all_categories_have_gl_mapping(test_db):
    for category in ["Meals", "Travel", "Accommodation", "Entertainment", "Office Supplies", "Software", "Conference", "Other"]:
        gl, _ = await PolicyEngine().get_gl_account(category, test_db)
        assert gl is not None


def test_sync_policy_engine_importable():
    assert SyncPolicyEngine
    assert process_expense_task
    assert upload_to_priority_task
