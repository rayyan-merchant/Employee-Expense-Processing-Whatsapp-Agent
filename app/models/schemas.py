from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ConversationState(BaseModel):
    state: str
    phone: str
    lang: str = "en"
    expense_data: dict | None = None
    image_url: str | None = None
    pending_expense_id: str | None = None
    retries: int = 0
    last_processed_message_sid: str | None = None
    created_at: str | None = None
    updated_at: str | None = None

    @model_validator(mode="after")
    def set_timestamps(self) -> "ConversationState":
        now = datetime.utcnow().isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.updated_at:
            self.updated_at = now
        return self


class ExpenseRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    whatsapp_number: str
    employee_name: str | None = None
    employee_id: str | None = None
    amount: float
    currency: str
    amount_nis: float
    vendor: str | None = None
    expense_date: str
    category: str
    description: str | None = None
    gl_account: str | None = None
    cost_center: str | None = None
    receipt_image_url: str | None = None
    ocr_raw_text: str | None = None
    ocr_confidence: float = 0.0
    policy_status: str = "PENDING"
    policy_rejection_reason: str | None = None
    approval_status: str = "PENDING"
    approver_phone: str | None = None
    approved_at: str | None = None
    priority_document_id: str | None = None
    priority_status: str = "NOT_UPLOADED"
    priority_error: str | None = None
    language: str = "en"
    created_at: str
    updated_at: str


class PolicyRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: int
    category: str
    max_amount_nis: float
    requires_approval_above: float
    max_days_old: int
    allowed_currencies: str
    active: bool


class EmployeeRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    phone: str
    name: str
    employee_id: str
    manager_phone: str
