import uuid
from datetime import datetime, timedelta

from sqlalchemy import Float, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


def _now() -> str:
    return datetime.utcnow().isoformat()


class Expense(Base):
    __tablename__ = "expenses"

    id: Mapped[str] = mapped_column(String, primary_key=True, default=lambda: str(uuid.uuid4()))
    whatsapp_number: Mapped[str] = mapped_column(String)
    employee_name: Mapped[str | None] = mapped_column(String, nullable=True)
    employee_id: Mapped[str | None] = mapped_column(String, nullable=True)
    amount: Mapped[float] = mapped_column(Float)
    currency: Mapped[str] = mapped_column(String)
    amount_nis: Mapped[float] = mapped_column(Float)
    vendor: Mapped[str | None] = mapped_column(String, nullable=True)
    expense_date: Mapped[str] = mapped_column(String)
    category: Mapped[str] = mapped_column(String)
    description: Mapped[str | None] = mapped_column(String, nullable=True)
    gl_account: Mapped[str | None] = mapped_column(String, nullable=True)
    cost_center: Mapped[str | None] = mapped_column(String, nullable=True)
    receipt_image_url: Mapped[str | None] = mapped_column(String, nullable=True)
    ocr_raw_text: Mapped[str | None] = mapped_column(String, nullable=True)
    ocr_confidence: Mapped[float] = mapped_column(Float, default=0.0)
    policy_status: Mapped[str] = mapped_column(String, default="PENDING")
    policy_rejection_reason: Mapped[str | None] = mapped_column(String, nullable=True)
    approval_status: Mapped[str] = mapped_column(String, default="PENDING")
    approver_phone: Mapped[str | None] = mapped_column(String, nullable=True)
    approved_at: Mapped[str | None] = mapped_column(String, nullable=True)
    priority_document_id: Mapped[str | None] = mapped_column(String, nullable=True)
    priority_status: Mapped[str] = mapped_column(String, default="NOT_UPLOADED")
    priority_error: Mapped[str | None] = mapped_column(String, nullable=True)
    language: Mapped[str] = mapped_column(String, default="en")
    created_at: Mapped[str] = mapped_column(String, default=_now)
    updated_at: Mapped[str] = mapped_column(String, default=_now)


async def create_expense(db: AsyncSession, data: dict) -> Expense:
    expense = Expense(**data)
    db.add(expense)
    await db.commit()
    await db.refresh(expense)
    return expense


async def get_expense(db: AsyncSession, expense_id: str) -> Expense | None:
    return await db.get(Expense, expense_id)


async def get_expenses_by_phone(db: AsyncSession, phone: str) -> list[Expense]:
    result = await db.execute(select(Expense).where(Expense.whatsapp_number == phone).order_by(Expense.created_at.desc()))
    return list(result.scalars().all())


async def update_expense(db: AsyncSession, expense_id: str, **updates) -> Expense | None:
    expense = await get_expense(db, expense_id)
    if expense is None:
        return None
    for key, value in updates.items():
        if hasattr(expense, key):
            setattr(expense, key, value)
    expense.updated_at = _now()
    await db.commit()
    await db.refresh(expense)
    return expense


async def list_all_expenses(db: AsyncSession, limit: int = 100) -> list[Expense]:
    result = await db.execute(select(Expense).order_by(Expense.created_at.desc()).limit(limit))
    return list(result.scalars().all())


async def find_potential_duplicate(
    db: AsyncSession,
    phone: str,
    amount: float,
    vendor: str | None,
    expense_date: str,
    category: str,
) -> Expense | None:
    cutoff = datetime.utcnow() - timedelta(minutes=10)
    result = await db.execute(
        select(Expense)
        .where(
            Expense.whatsapp_number == phone,
            Expense.expense_date == expense_date,
            Expense.category == category,
        )
        .order_by(Expense.created_at.desc())
    )
    for expense in result.scalars().all():
        if expense.approval_status == "REJECTED" or expense.priority_status in {"FAILED", "PRIORITY_UPLOAD_FAILED"} or expense.policy_status == "REJECTED":
            continue
        try:
            created_at = datetime.fromisoformat(expense.created_at)
        except (TypeError, ValueError):
            continue
        if created_at < cutoff:
            continue
        if abs((expense.amount or 0) - amount) <= 0.01:
            if not vendor or not expense.vendor or expense.vendor.strip().lower() == vendor.strip().lower():
                return expense
    return None
