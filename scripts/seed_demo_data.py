import sys
from datetime import date, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.models.database import SyncSessionLocal, sync_engine
from app.models.expense import Expense
from app.models.database import Base

DEMO_EXPENSES = [
    {"employee": "972501234567", "amount": 87.75, "currency": "NIS", "vendor": "Cafe Aroma", "category": "Meals", "description": "Team standup coffee", "expense_date": "5 days ago", "policy_status": "AUTO_APPROVE", "approval_status": "NOT_REQUIRED", "priority_status": "UPLOADED", "priority_document_id": "EXP-A1B2C3D4", "ocr_confidence": 0.96, "lang": "en"},
    {"employee": "972521234567", "amount": 245.0, "currency": "NIS", "vendor": "Gett Taxi", "category": "Travel", "description": "Client meeting ride", "expense_date": "3 days ago", "policy_status": "AUTO_APPROVE", "approval_status": "NOT_REQUIRED", "priority_status": "UPLOADED", "priority_document_id": "EXP-E5F6G7H8", "ocr_confidence": 0.91, "lang": "he"},
    {"employee": "972531234567", "amount": 850.0, "currency": "NIS", "vendor": "Nebo Restaurant", "category": "Entertainment", "description": "Client dinner", "expense_date": "1 days ago", "policy_status": "NEEDS_APPROVAL", "approval_status": "PENDING", "priority_status": "NOT_UPLOADED", "ocr_confidence": 0.89, "lang": "he"},
    {"employee": "972501234567", "amount": 15000.0, "currency": "NIS", "vendor": "Adobe Systems", "category": "Software", "description": "Creative Cloud annual", "expense_date": "2 days ago", "policy_status": "REJECTED", "policy_rejection_reason": "Amount 15000 NIS exceeds maximum of 10000 NIS for Software", "approval_status": "REJECTED", "priority_status": "NOT_UPLOADED", "ocr_confidence": 0.94, "lang": "en"},
    {"employee": "972521234567", "amount": 1200.0, "currency": "NIS", "vendor": "Holiday Inn Tel Aviv", "category": "Accommodation", "description": "Conference overnight stay", "expense_date": "7 days ago", "policy_status": "NEEDS_APPROVAL", "approval_status": "APPROVED", "priority_status": "UPLOADED", "priority_document_id": "EXP-I9J0K1L2", "ocr_confidence": 0.88, "lang": "en"},
    {"employee": "972531234567", "amount": 320.0, "currency": "NIS", "vendor": "Office Depot", "category": "Office Supplies", "description": "printer cartridges and paper", "expense_date": "4 days ago", "policy_status": "AUTO_APPROVE", "approval_status": "NOT_REQUIRED", "priority_status": "FAILED", "priority_error": "Connection timeout after 3 retries", "ocr_confidence": 0.93, "lang": "en"},
    {"employee": "972501234567", "amount": 180.0, "currency": "NIS", "vendor": "Zoom Communications", "category": "Software", "description": "Monthly subscription", "expense_date": "today", "policy_status": "PENDING", "approval_status": "PENDING", "priority_status": "NOT_UPLOADED", "ocr_confidence": 0.97, "lang": "en"},
    {"employee": "972521234567", "amount": 450.0, "currency": "USD", "vendor": "AWS Amazon", "category": "Software", "description": "Cloud hosting invoice", "expense_date": "6 days ago", "policy_status": "EXCEPTION", "approval_status": "PENDING", "priority_status": "NOT_UPLOADED", "ocr_confidence": 0.82, "lang": "en"},
]


def resolve_date(value: str) -> str:
    if value == "today":
        return date.today().isoformat()
    days = int(value.split()[0])
    return (date.today() - timedelta(days=days)).isoformat()


def main() -> None:
    Base.metadata.create_all(bind=sync_engine)
    db = SyncSessionLocal()
    try:
        for item in DEMO_EXPENSES:
            exists = db.query(Expense).filter_by(vendor=item["vendor"], amount=item["amount"]).first()
            if exists:
                continue
            db.add(
                Expense(
                    whatsapp_number=item["employee"],
                    amount=item["amount"],
                    currency=item["currency"],
                    amount_nis=item["amount"],
                    vendor=item["vendor"],
                    category=item["category"],
                    description=item["description"],
                    expense_date=resolve_date(item["expense_date"]),
                    policy_status=item["policy_status"],
                    policy_rejection_reason=item.get("policy_rejection_reason"),
                    approval_status=item["approval_status"],
                    priority_status=item["priority_status"],
                    priority_document_id=item.get("priority_document_id"),
                    priority_error=item.get("priority_error"),
                    ocr_confidence=item["ocr_confidence"],
                    language=item["lang"],
                )
            )
        db.commit()
        print("Demo expenses seeded.")
    finally:
        db.close()


if __name__ == "__main__":
    main()
