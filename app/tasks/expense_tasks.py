import logging

from celery import Celery

from app.config import settings

logger = logging.getLogger(__name__)

celery_app = Celery(
    "expense_agent",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
)
celery_app.conf.task_serializer = "json"
celery_app.conf.result_serializer = "json"
celery_app.conf.accept_content = ["json"]
celery_app.conf.timezone = "UTC"
celery_app.conf.task_always_eager = settings.CELERY_TASK_ALWAYS_EAGER
celery_app.conf.task_eager_propagates = False


@celery_app.task(bind=True, max_retries=3, default_retry_delay=30, name="tasks.process_expense")
def process_expense_task(self, expense_id: str, phone: str):
    from app.fsm.conversation import SyncConversationFSM
    from app.models.database import SyncSessionLocal
    from app.models.expense import Expense
    from app.services.approval import SyncApprovalService
    from app.services.language import render_template
    from app.services.policy_engine import PolicyDecision, SyncPolicyEngine
    from app.services.whatsapp import WhatsAppService

    db = SyncSessionLocal()
    try:
        expense = db.query(Expense).filter_by(id=expense_id).first()
        if not expense:
            logger.error("Expense %s not found in DB", expense_id)
            return
        result = SyncPolicyEngine().validate(
            {
                "amount": expense.amount,
                "currency": expense.currency,
                "expense_date": expense.expense_date,
                "category": expense.category,
            },
            db,
        )
        expense.policy_status = result.decision.value
        expense.policy_rejection_reason = result.reason
        db.commit()

        wa = WhatsAppService()
        fsm = SyncConversationFSM()
        lang = expense.language or "en"

        if result.decision == PolicyDecision.AUTO_APPROVE:
            expense.approval_status = "NOT_REQUIRED"
            db.commit()
            fsm.transition(phone, "UPLOADING_TO_PRIORITY")
            upload_to_priority_task.delay(expense_id, phone)
        elif result.decision in (PolicyDecision.NEEDS_APPROVAL, PolicyDecision.EXCEPTION):
            expense.approval_status = "PENDING"
            db.commit()
            approval_svc = SyncApprovalService()
            manager_phone = approval_svc.get_manager_phone(phone, db)
            approval_svc.notify_manager(expense, manager_phone, db)
            fsm.transition(phone, "PENDING_APPROVAL")
            wa.send_message_sync(
                f"whatsapp:+{phone}",
                render_template("sent_for_approval", lang, amount=expense.amount, currency=expense.currency, expense_id=expense.id[:8].upper()),
            )
        elif result.decision == PolicyDecision.REJECTED:
            expense.approval_status = "REJECTED"
            db.commit()
            fsm.transition(phone, "REJECTED")
            wa.send_message_sync(f"whatsapp:+{phone}", render_template("policy_rejected", lang, reason=result.reason))
    except Exception as exc:
        logger.error("Error in process_expense_task for %s: %s", expense_id, exc, exc_info=True)
        try:
            self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            logger.error("Max retries exceeded for expense %s", expense_id)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=5, default_retry_delay=60, name="tasks.upload_to_priority")
def upload_to_priority_task(self, expense_id: str, phone: str):
    import asyncio

    from app.fsm.conversation import SyncConversationFSM
    from app.models.database import SyncSessionLocal
    from app.models.expense import Expense
    from app.services.language import render_template
    from app.services.priority_erp import PriorityERPClient
    from app.services.whatsapp import WhatsAppService

    db = SyncSessionLocal()
    try:
        expense = db.query(Expense).filter_by(id=expense_id).first()
        if not expense:
            logger.error("Expense %s not found for Priority upload", expense_id)
            return
        result = asyncio.run(PriorityERPClient().create_expense(expense))
        wa = WhatsAppService()
        fsm = SyncConversationFSM()
        lang = expense.language or "en"
        if result.success:
            expense.priority_status = "UPLOADED"
            expense.priority_document_id = result.document_no
            db.commit()
            fsm.transition(phone, "COMPLETED")
            wa.send_message_sync(
                f"whatsapp:+{phone}",
                render_template("priority_success", lang, expense_id=expense.id[:8].upper(), priority_doc_id=result.document_no, amount=expense.amount, currency=expense.currency),
            )
        else:
            raise Exception(f"Priority upload failed: {result.error}")
    except Exception as exc:
        logger.error("Priority upload error for %s: %s", expense_id, exc, exc_info=True)
        try:
            self.retry(exc=exc)
        except self.MaxRetriesExceededError:
            expense = db.query(Expense).filter_by(id=expense_id).first()
            if expense:
                expense.priority_status = "FAILED"
                expense.priority_error = str(exc)
                db.commit()
            SyncConversationFSM().transition(phone, "PRIORITY_UPLOAD_FAILED")
            WhatsAppService().send_message_sync(
                f"whatsapp:+{phone}",
                render_template("priority_failed", expense.language or "en" if expense else "en", expense_id=expense_id[:8].upper()),
            )
    finally:
        db.close()
