import json
import pytest

from app.services.ocr import ReceiptExtractionError, ReceiptOCRService

GOOD_RESPONSE = json.dumps({
    "amount": 250.0, "currency": "NIS", "vendor": "Cafe Aroma",
    "expense_date": "2024-05-20", "category_hint": "Meals",
    "description": "Team lunch", "raw_text_summary": "Receipt from Cafe Aroma",
    "confidence": {"overall": 0.92, "amount": 0.98, "vendor": 0.90, "date": 0.88, "category": 0.85}
})

LOW_CONF_RESPONSE = json.dumps({
    "amount": None, "currency": "NIS", "vendor": None,
    "expense_date": None, "category_hint": None,
    "description": None, "raw_text_summary": "Blurry image, cannot read",
    "confidence": {"overall": 0.35, "amount": 0.3, "vendor": 0.2, "date": 0.2, "category": 0.2}
})


@pytest.fixture
def sample_image_bytes():
    from PIL import Image
    import io

    img = Image.new("RGB", (100, 100), "white")
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


@pytest.fixture
def mock_gemini_vision(mocker):
    return mocker.patch.object(ReceiptOCRService, "_call_gemini_json", return_value=json.loads(GOOD_RESPONSE))


async def test_extract_returns_structured_data(mock_gemini_vision, sample_image_bytes):
    result = await ReceiptOCRService().extract_from_image_bytes(sample_image_bytes)
    assert result["amount"] == 250.0
    assert result["currency"] == "NIS"
    assert result["vendor"] == "Cafe Aroma"


async def test_extract_high_confidence(mock_gemini_vision, sample_image_bytes):
    result = await ReceiptOCRService().extract_from_image_bytes(sample_image_bytes)
    assert result["confidence"]["overall"] >= 0.9


async def test_extract_low_confidence(mocker, sample_image_bytes):
    mocker.patch.object(ReceiptOCRService, "_call_gemini_json", return_value=json.loads(LOW_CONF_RESPONSE))
    result = await ReceiptOCRService().extract_from_image_bytes(sample_image_bytes)
    assert result["confidence"]["overall"] < 0.6


async def test_extract_invalid_json_raises(mocker, sample_image_bytes):
    mocker.patch.object(ReceiptOCRService, "_call_gemini_json", return_value=None)
    with pytest.raises(ReceiptExtractionError):
        await ReceiptOCRService().extract_from_image_bytes(sample_image_bytes)


async def test_extract_handles_markdown_fence(mocker, sample_image_bytes):
    mocker.patch.object(ReceiptOCRService, "_call_gemini_json", return_value=json.loads(GOOD_RESPONSE))
    result = await ReceiptOCRService().extract_from_image_bytes(sample_image_bytes)
    assert result["amount"] == 250.0


async def test_missing_field_defaults_to_none(mocker, sample_image_bytes):
    mocker.patch.object(ReceiptOCRService, "_call_gemini_json", return_value={"amount": 100.0, "confidence": {"overall": 0.8}})
    result = await ReceiptOCRService().extract_from_image_bytes(sample_image_bytes)
    assert result["vendor"] is None
    assert result["currency"] is None


async def test_confidence_clamped(mocker, sample_image_bytes):
    mocker.patch.object(ReceiptOCRService, "_call_gemini_json", return_value={"amount": 100.0, "currency": "NIS", "vendor": "Test", "expense_date": "2024-01-01", "raw_text_summary": "test", "confidence": {"overall": 1.5, "amount": -0.3}})
    result = await ReceiptOCRService().extract_from_image_bytes(sample_image_bytes)
    assert result["confidence"]["overall"] == 1.0
    assert result["confidence"]["amount"] == 0.0


def test_normalize_date_ddmmyyyy():
    assert ReceiptOCRService()._normalize_date("20/05/2024") == "2024-05-20"


def test_normalize_date_already_iso():
    assert ReceiptOCRService()._normalize_date("2024-05-20") == "2024-05-20"


def test_normalize_date_text_format():
    assert ReceiptOCRService()._normalize_date("May 20, 2024") == "2024-05-20"


def test_normalize_date_invalid_returns_none():
    assert ReceiptOCRService()._normalize_date("not a date") is None


def test_normalize_currency_uppercase():
    result = ReceiptOCRService()._validate_and_normalize({"currency": "nis", "confidence": {}})
    assert result["currency"] == "NIS"


async def test_parse_manual_english(mocker):
    mocker.patch.object(ReceiptOCRService, "_call_gemini_json", return_value={"vendor": "Cafe Aroma"})
    result = await ReceiptOCRService().parse_manual_details("250 NIS cafe aroma yesterday lunch")
    assert result["amount"] == 250.0
    assert result["currency"] == "NIS"


async def test_parse_manual_bad_json_returns_fallback(mocker):
    mocker.patch.object(ReceiptOCRService, "_call_gemini_json", side_effect=Exception("api down"))
    result = await ReceiptOCRService().parse_manual_details("some text")
    assert "amount" in result


async def test_parse_manual_structured_message_without_gemini(mocker):
    gemini = mocker.patch.object(ReceiptOCRService, "_call_gemini_json")
    result = await ReceiptOCRService().parse_manual_details(
        "Amount: 3229.20 NIS\nVendor: Free Style\nDate: 2025-12-19\nCategory: Other\nDescription: POS purchase"
    )
    assert result["amount"] == 3229.20
    assert result["currency"] == "NIS"
    assert result["vendor"] == "Free Style"
    assert result["expense_date"] == "2025-12-19"
    assert result["category"] == "Other"
    gemini.assert_not_called()
