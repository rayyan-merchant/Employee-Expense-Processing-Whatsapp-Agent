import logging

from app.config import settings
from app.services.language import render_template
from app.services.whatsapp import WhatsAppService

logger = logging.getLogger(__name__)


class SyncApprovalService:
    def get_manager_phone(self, employee_phone: str, db) -> str:
        from app.models.employee import Employee

        employee = db.query(Employee).filter_by(phone=employee_phone).first()
        if employee and employee.manager_phone:
            return employee.manager_phone
        return settings.DEFAULT_MANAGER_PHONE.replace("whatsapp:+", "")

    def notify_manager(self, expense, manager_phone: str, db) -> None:
        from app.models.employee import Employee

        employee = db.query(Employee).filter_by(phone=expense.whatsapp_number).first()
        employee_name = getattr(employee, "name", None) or expense.whatsapp_number[-4:]
        msg = render_template(
            "manager_approval_request",
            "en",
            employee_name=employee_name,
            phone=f"...{expense.whatsapp_number[-4:]}",
            amount=expense.amount,
            currency=expense.currency,
            vendor=expense.vendor or "N/A",
            category=expense.category,
            expense_date=expense.expense_date,
            description=expense.description or "N/A",
            expense_id=expense.id[:8].upper(),
        )
        WhatsAppService().send_message_sync(f"whatsapp:+{manager_phone}", msg)
        logger.info("Manager notified at ...%s for expense %s", manager_phone[-4:], expense.id[:8])


class AsyncApprovalService:
    async def get_manager_phone(self, employee_phone: str, db) -> str:
        from app.models.employee import get_employee_by_phone

        employee = await get_employee_by_phone(db, employee_phone)
        if employee and employee.manager_phone:
            return employee.manager_phone
        return settings.DEFAULT_MANAGER_PHONE.replace("whatsapp:+", "")
