from sqlalchemy import Boolean, Float, Integer, String, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


class ExpensePolicy(Base):
    __tablename__ = "expense_policies"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    category: Mapped[str] = mapped_column(String, unique=True, index=True)
    max_amount_nis: Mapped[float] = mapped_column(Float)
    requires_approval_above: Mapped[float] = mapped_column(Float)
    max_days_old: Mapped[int] = mapped_column(Integer)
    allowed_currencies: Mapped[str] = mapped_column(String)
    active: Mapped[bool] = mapped_column(Boolean, default=True)


class CategoryGLMapping(Base):
    __tablename__ = "category_gl_mappings"

    category: Mapped[str] = mapped_column(String, primary_key=True)
    gl_account: Mapped[str] = mapped_column(String)
    cost_center: Mapped[str | None] = mapped_column(String, nullable=True)


async def get_policy(db: AsyncSession, category: str) -> ExpensePolicy | None:
    result = await db.execute(select(ExpensePolicy).where(ExpensePolicy.category == category, ExpensePolicy.active.is_(True)))
    return result.scalar_one_or_none()


async def get_gl_mapping(db: AsyncSession, category: str) -> CategoryGLMapping | None:
    return await db.get(CategoryGLMapping, category)


async def list_policies(db: AsyncSession) -> list[ExpensePolicy]:
    result = await db.execute(select(ExpensePolicy).order_by(ExpensePolicy.category))
    return list(result.scalars().all())
