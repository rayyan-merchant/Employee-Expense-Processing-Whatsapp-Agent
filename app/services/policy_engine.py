import json
import logging
from dataclasses import dataclass
from datetime import date
from enum import Enum

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Session

from app.models.policy import get_gl_mapping, get_policy

logger = logging.getLogger(__name__)


class PolicyDecision(str, Enum):
    AUTO_APPROVE = "AUTO_APPROVE"
    NEEDS_APPROVAL = "NEEDS_APPROVAL"
    REJECTED = "REJECTED"
    EXCEPTION = "EXCEPTION"


@dataclass
class PolicyResult:
    decision: PolicyDecision
    reason: str
    policy_applied: str | None = None
    requires_approval_above: float | None = None


KNOWN_CATEGORIES = [
    "Meals",
    "Travel",
    "Accommodation",
    "Entertainment",
    "Office Supplies",
    "Software",
    "Conference",
    "Other",
]


def _check_policy(category: str, amount: float, currency: str, expense_date_str: str | None, policy) -> PolicyResult:
    if expense_date_str:
        try:
            expense_date = date.fromisoformat(expense_date_str)
            today = date.today()
            if expense_date > today:
                return PolicyResult(PolicyDecision.REJECTED, f"Receipt date {expense_date_str} is in the future", category)
            days_old = (today - expense_date).days
            if days_old > policy.max_days_old:
                return PolicyResult(
                    PolicyDecision.REJECTED,
                    f"Receipt dated {expense_date_str} is {days_old} days old. Maximum allowed for {category} is {policy.max_days_old} days.",
                    category,
                )
        except ValueError:
            logger.warning("Invalid expense_date format: %s", expense_date_str)

    try:
        allowed = json.loads(policy.allowed_currencies)
    except Exception:
        allowed = ["NIS"]
    if currency not in allowed:
        return PolicyResult(PolicyDecision.REJECTED, f"Currency '{currency}' is not allowed for {category}. Allowed: {', '.join(allowed)}", category)
    if amount > round(policy.max_amount_nis, 2):
        return PolicyResult(PolicyDecision.REJECTED, f"Amount {amount} {currency} exceeds maximum of {policy.max_amount_nis} NIS for {category}", category)
    if amount > round(policy.requires_approval_above, 2):
        return PolicyResult(
            PolicyDecision.NEEDS_APPROVAL,
            f"Amount {amount} {currency} exceeds auto-approval limit of {policy.requires_approval_above} NIS for {category}. Manager approval required.",
            category,
            policy.requires_approval_above,
        )
    return PolicyResult(PolicyDecision.AUTO_APPROVE, f"Expense approved automatically for {category}", category)


class PolicyEngine:
    async def validate(self, expense_data: dict, db: AsyncSession) -> PolicyResult:
        category = expense_data.get("category")
        amount = round(float(expense_data.get("amount") or 0), 2)
        currency = str(expense_data.get("currency") or "NIS").upper()
        expense_date_str = expense_data.get("expense_date")
        if category not in KNOWN_CATEGORIES:
            return PolicyResult(PolicyDecision.REJECTED, f"Unknown expense category: '{category}'")
        policy = await get_policy(db, category)
        if policy is None or not policy.active:
            return PolicyResult(PolicyDecision.EXCEPTION, f"No active policy configured for {category}. Sent for manual review.", category)
        return _check_policy(category, amount, currency, expense_date_str, policy)

    async def get_gl_account(self, category: str, db: AsyncSession) -> tuple[str, str | None]:
        mapping = await get_gl_mapping(db, category)
        if not mapping:
            raise ValueError(f"No GL mapping for category: {category}")
        return mapping.gl_account, mapping.cost_center


class SyncPolicyEngine:
    def validate(self, expense_data: dict, db: Session) -> PolicyResult:
        from app.models.policy import ExpensePolicy

        category = expense_data.get("category")
        amount = round(float(expense_data.get("amount") or 0), 2)
        currency = str(expense_data.get("currency") or "NIS").upper()
        expense_date_str = expense_data.get("expense_date")
        if category not in KNOWN_CATEGORIES:
            return PolicyResult(PolicyDecision.REJECTED, f"Unknown expense category: '{category}'")
        policy = db.query(ExpensePolicy).filter_by(category=category, active=True).first()
        if not policy:
            return PolicyResult(PolicyDecision.EXCEPTION, f"No active policy for {category}. Sent for manual review.", category)
        return _check_policy(category, amount, currency, expense_date_str, policy)

    def get_gl_account(self, category: str, db: Session) -> tuple[str, str | None]:
        from app.models.policy import CategoryGLMapping

        mapping = db.query(CategoryGLMapping).filter_by(category=category).first()
        if not mapping:
            raise ValueError(f"No GL mapping for {category}")
        return mapping.gl_account, mapping.cost_center
