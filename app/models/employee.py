from sqlalchemy import String
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app.models.database import Base


class Employee(Base):
    __tablename__ = "employees"

    phone: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String)
    employee_id: Mapped[str] = mapped_column(String)
    manager_phone: Mapped[str] = mapped_column(String)


async def get_employee_by_phone(db: AsyncSession, phone: str) -> Employee | None:
    return await db.get(Employee, phone)
