from collections import Counter

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import get_db
from app.models.expense import get_expense, list_all_expenses
from app.models.schemas import ExpenseRead

router = APIRouter()


def _expense_dict(expense) -> dict:
    return ExpenseRead.model_validate(expense).model_dump()


@router.get("/", response_class=HTMLResponse)
async def dashboard():
    with open("templates/dashboard.html", encoding="utf-8") as f:
        return f.read()


@router.get("/api/expenses")
async def list_expenses(db: AsyncSession = Depends(get_db)):
    expenses = await list_all_expenses(db, limit=500)
    return [_expense_dict(expense) for expense in expenses]


@router.get("/api/expenses/{expense_id}")
async def expense_detail(expense_id: str, db: AsyncSession = Depends(get_db)):
    expense = await get_expense(db, expense_id)
    if not expense:
        raise HTTPException(status_code=404, detail="Expense not found")
    return _expense_dict(expense)


@router.get("/api/stats")
async def get_stats(db: AsyncSession = Depends(get_db)):
    expenses = await list_all_expenses(db, limit=1000)
    total = len(expenses)
    total_amount = round(sum(e.amount_nis or 0 for e in expenses), 2)
    return {
        "total_expenses": total,
        "total_amount_nis": total_amount,
        "avg_amount_nis": round(total_amount / total, 2) if total else 0,
        "by_policy_status": dict(Counter(e.policy_status for e in expenses)),
        "by_priority_status": dict(Counter(e.priority_status for e in expenses)),
        "by_category": dict(Counter(e.category for e in expenses)),
        "pending_approval_count": sum(1 for e in expenses if e.approval_status == "PENDING"),
    }
