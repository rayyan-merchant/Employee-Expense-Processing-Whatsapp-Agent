import json

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.database import AsyncSessionLocal
from app.models.employee import Employee
from app.models.policy import CategoryGLMapping, ExpensePolicy


POLICIES = [
    ("Meals", 500, 300, 30),
    ("Travel", 5000, 1000, 60),
    ("Accommodation", 1500, 800, 60),
    ("Entertainment", 2000, 500, 30),
    ("Office Supplies", 500, 200, 30),
    ("Software", 10000, 2000, 30),
    ("Conference", 8000, 2000, 90),
    ("Other", 1000, 300, 30),
]

GL_MAPPINGS = [
    ("Meals", "6110", "CC-GEN"),
    ("Travel", "6120", "CC-GEN"),
    ("Accommodation", "6130", "CC-GEN"),
    ("Entertainment", "6140", "CC-ENT"),
    ("Office Supplies", "6150", "CC-OPS"),
    ("Software", "6160", "CC-IT"),
    ("Conference", "6170", "CC-HR"),
    ("Other", "6199", "CC-GEN"),
]

EMPLOYEES = [
    ("972501234567", "David Cohen", "EMP001", "972521234567"),
    ("972521234567", "Sarah Levi", "EMP002", "972531234567"),
    ("972531234567", "Mike Goldstein", "EMP003", "972521234567"),
]


async def seed_database(db: AsyncSession) -> None:
    """Idempotent; checks if policy data exists before inserting."""
    count = await db.scalar(select(func.count()).select_from(ExpensePolicy))
    if count and count > 0:
        return

    allowed = json.dumps(["NIS", "USD", "EUR"])
    db.add_all(
        ExpensePolicy(
            category=category,
            max_amount_nis=max_amount,
            requires_approval_above=approval_above,
            max_days_old=max_days_old,
            allowed_currencies=allowed,
            active=True,
        )
        for category, max_amount, approval_above, max_days_old in POLICIES
    )
    db.add_all(
        CategoryGLMapping(category=category, gl_account=gl, cost_center=cost_center)
        for category, gl, cost_center in GL_MAPPINGS
    )
    db.add_all(
        Employee(phone=phone, name=name, employee_id=employee_id, manager_phone=manager_phone)
        for phone, name, employee_id, manager_phone in EMPLOYEES
    )
    await db.commit()


async def run_seed() -> None:
    async with AsyncSessionLocal() as db:
        await seed_database(db)
