from unittest.mock import MagicMock

import httpx
import pytest

from app.services.priority_erp import MockPriorityERP, PriorityERPClient, PriorityExpenseResult


@pytest.fixture
def mock_expense():
    m = MagicMock()
    m.id = "abcd1234-efgh-5678-ijkl-mnopqrstuvwx"
    m.employee_id = "EMP001"
    m.expense_date = "2024-05-20"
    m.description = "Team lunch"
    m.amount = 250.0
    m.amount_nis = 250.0
    m.currency = "NIS"
    m.gl_account = "6110"
    m.category = "Meals"
    m.vendor = "Cafe Aroma"
    m.whatsapp_number = "972501234567"
    return m


@pytest.fixture(autouse=True)
def reset_mock():
    MockPriorityERP.reset()
    yield
    MockPriorityERP.reset()


async def test_mock_creates_expense_successfully(mock_expense):
    result = await MockPriorityERP().create_expense(mock_expense)
    assert result.success is True
    assert result.document_no.startswith("EXP-")
    assert result.document_id is not None


async def test_mock_stores_document(mock_expense):
    mock = MockPriorityERP()
    result = await mock.create_expense(mock_expense)
    stored = await mock.get_document_status(result.document_no)
    assert stored is not None
    assert stored["STATDES"] == "Submitted"


async def test_mock_document_has_correct_gl(mock_expense):
    mock = MockPriorityERP()
    result = await mock.create_expense(mock_expense)
    stored = await mock.get_document_status(result.document_no)
    assert stored["LINES"][0]["PARTNAME"] == "6110"


async def test_mock_failure_on_header(mock_expense):
    MockPriorityERP.configure_failure("header")
    with pytest.raises(httpx.HTTPError):
        await MockPriorityERP().create_expense(mock_expense)


async def test_mock_failure_on_line(mock_expense):
    MockPriorityERP.configure_failure("line")
    with pytest.raises(httpx.HTTPError):
        await MockPriorityERP().create_expense(mock_expense)


async def test_mock_failure_on_submit(mock_expense):
    MockPriorityERP.configure_failure("submit")
    with pytest.raises(httpx.HTTPError):
        await MockPriorityERP().create_expense(mock_expense)


async def test_reset_clears_documents(mock_expense):
    mock = MockPriorityERP()
    result = await mock.create_expense(mock_expense)
    MockPriorityERP.reset()
    assert await mock.get_document_status(result.document_no) is None


async def test_client_uses_mock_when_configured(mock_expense, mocker):
    mocker.patch("app.config.settings.PRIORITY_USE_MOCK", True)
    client = PriorityERPClient()
    client._mock = MagicMock()
    mock_result = PriorityExpenseResult(success=True, document_no="EXP-TEST", document_id="12345")
    client._mock.create_expense = mocker.AsyncMock(return_value=mock_result)
    assert client._mock is not None
    assert (await client.create_expense(mock_expense)).document_no == "EXP-TEST"


async def test_doc_no_format(mock_expense):
    result = await MockPriorityERP().create_expense(mock_expense)
    assert result.document_no.startswith("EXP-")
    assert len(result.document_no) == 12


async def test_employee_lookup_by_phone(test_db):
    from app.models.employee import get_employee_by_phone

    employee = await get_employee_by_phone(test_db, "972501234567")
    assert employee is not None
    assert employee.name == "David Cohen"


async def test_employee_unknown_phone(test_db):
    from app.models.employee import get_employee_by_phone

    assert await get_employee_by_phone(test_db, "999999999999") is None
