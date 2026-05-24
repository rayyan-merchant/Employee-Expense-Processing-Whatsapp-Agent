import asyncio
import base64
import logging
import random
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger(__name__)


@dataclass
class PriorityExpenseResult:
    success: bool
    document_id: str | None = None
    document_no: str | None = None
    error: str | None = None


class PriorityERPClient:
    def __init__(self):
        self.base_url = settings.PRIORITY_BASE_URL.rstrip("/")
        self._auth_str = base64.b64encode(f"{settings.PRIORITY_USERNAME}:{settings.PRIORITY_PASSWORD}".encode()).decode()
        self._mock = MockPriorityERP() if settings.use_priority_mock else None

    def _headers(self) -> dict:
        return {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": f"Basic {self._auth_str}",
        }

    async def create_expense(self, expense) -> PriorityExpenseResult:
        if settings.use_priority_mock:
            return await self._mock.create_expense(expense)
        return await self._real_create_expense(expense)

    async def get_document_status(self, doc_no: str) -> dict | None:
        if settings.use_priority_mock:
            return await self._mock.get_document_status(doc_no)
        return await self._real_get_status(doc_no)

    async def _real_create_expense(self, expense) -> PriorityExpenseResult:
        doc_no = f"EXP-{expense.id[:8].upper()}"
        async with httpx.AsyncClient(timeout=30.0) as client:
            header_resp = await client.post(
                f"{self.base_url}/DOCUMENTS",
                headers=self._headers(),
                json={
                    "DOCNO": doc_no,
                    "DOCTYPE": "EX",
                    "CUSTNAME": expense.employee_id or "EMP001",
                    "CURDATE": expense.expense_date,
                    "DETAILS": expense.description or "",
                    "STATDES": "Draft",
                },
            )
            header_resp.raise_for_status()
            doc_data = header_resp.json()
            line_resp = await client.post(
                f"{self.base_url}/DOCUMENTS('{doc_no}')/DOCUMENTLINES",
                headers=self._headers(),
                json={
                    "KLINE": 1,
                    "PARTNAME": expense.gl_account or "6199",
                    "TQUANT": 1,
                    "TPRICE": expense.amount_nis or expense.amount,
                    "CURRENCY": expense.currency,
                    "NOTES": f"{expense.category} - {expense.vendor or ''}",
                },
            )
            line_resp.raise_for_status()
            submit_resp = await client.patch(f"{self.base_url}/DOCUMENTS('{doc_no}')", headers=self._headers(), json={"STATDES": "Submitted"})
            submit_resp.raise_for_status()
            return PriorityExpenseResult(success=True, document_no=doc_no, document_id=str(doc_data.get("DOCID", "")))

    async def _real_get_status(self, doc_no: str) -> dict | None:
        async with httpx.AsyncClient(timeout=10.0) as client:
            resp = await client.get(f"{self.base_url}/DOCUMENTS('{doc_no}')", headers=self._headers())
            if resp.status_code == 404:
                return None
            resp.raise_for_status()
            return resp.json()


class MockPriorityERP:
    _documents: dict = {}
    _fail_on_step: str | None = None

    @classmethod
    def configure_failure(cls, step: str | None):
        cls._fail_on_step = step

    @classmethod
    def reset(cls):
        cls._documents = {}
        cls._fail_on_step = None

    async def create_expense(self, expense) -> PriorityExpenseResult:
        await asyncio.sleep(random.uniform(0.01, 0.03))
        doc_no = f"EXP-{expense.id[:8].upper()}"
        doc_id = random.randint(10000, 99999)
        if self._fail_on_step == "header":
            raise httpx.HTTPError("Mock: Priority server error on header creation")
        if self._fail_on_step == "line":
            self._documents[doc_no] = {"DOCNO": doc_no, "DOCID": doc_id, "STATDES": "Draft"}
            raise httpx.HTTPError("Mock: Priority server error on line item creation")
        if self._fail_on_step == "submit":
            self._documents[doc_no] = {"DOCNO": doc_no, "DOCID": doc_id, "STATDES": "Draft", "LINES": [{"KLINE": 1, "PARTNAME": expense.gl_account}]}
            raise httpx.HTTPError("Mock: Priority server error on document submission")
        self._documents[doc_no] = {
            "DOCNO": doc_no,
            "DOCID": doc_id,
            "STATDES": "Submitted",
            "CUSTNAME": getattr(expense, "employee_id", None) or "EMP001",
            "CURDATE": getattr(expense, "expense_date", ""),
            "DETAILS": getattr(expense, "description", "") or "",
            "LINES": [
                {
                    "KLINE": 1,
                    "PARTNAME": getattr(expense, "gl_account", "6199") or "6199",
                    "TQUANT": 1,
                    "TPRICE": getattr(expense, "amount_nis", None) or getattr(expense, "amount", 0),
                    "CURRENCY": getattr(expense, "currency", "NIS"),
                    "NOTES": f"{getattr(expense, 'category', '')} - {getattr(expense, 'vendor', '') or ''}",
                }
            ],
        }
        logger.info("Mock Priority: created document %s", doc_no)
        return PriorityExpenseResult(success=True, document_no=doc_no, document_id=str(doc_id))

    async def get_document_status(self, doc_no: str) -> dict | None:
        await asyncio.sleep(0.01)
        return self._documents.get(doc_no)
