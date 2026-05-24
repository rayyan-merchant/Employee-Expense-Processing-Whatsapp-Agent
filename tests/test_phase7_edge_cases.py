from datetime import datetime, timedelta
from unittest.mock import MagicMock

import pytest

from app.fsm.conversation import ConversationFSM
from app.models.expense import create_expense, find_potential_duplicate
from app.models.schemas import ConversationState
from app.services.language import detect_language, parse_confirmation_reply
from app.services.ocr import ReceiptExtractionError, ReceiptOCRService
from app.services.policy_engine import PolicyDecision, PolicyEngine, normalize_category
from app.services.policy_engine import SyncPolicyEngine, _parse_amount
from app.services.sanitizer import sanitize_phone, sanitize_text


def test_sanitize_removes_script_tag():
    assert sanitize_text("<script>alert(1)</script>") == "&lt;script&gt;alert(1)&lt;/script&gt;"


def test_sanitize_null_bytes_removed():
    assert sanitize_text("A\x00 B") == "A B"


def test_sanitize_collapses_whitespace():
    assert sanitize_text("  hello\n\n world  ") == "hello world"


def test_sanitize_empty_string_returns_none():
    assert sanitize_text("   ") is None


def test_sanitize_phone_with_plus_stripped():
    assert sanitize_phone("+972-50-123-4567") == "972501234567"


def test_sanitize_phone_too_short_raises():
    with pytest.raises(ValueError):
        sanitize_phone("123")


async def test_video_message_rejected_gracefully(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(has_media=True, media_url="https://api.twilio.com/video.mp4")
    params["MediaContentType0"] = "video/mp4"
    resp = await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert resp.status_code == 200
    assert "receipt photos" in mock_whatsapp.call_args.args[1]


async def test_pdf_message_rejected_gracefully(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(has_media=True, media_url="https://api.twilio.com/file.pdf")
    params["MediaContentType0"] = "application/pdf"
    resp = await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert resp.status_code == 200
    assert "receipt photos" in mock_whatsapp.call_args.args[1]


async def test_sticker_does_not_advance_fsm(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(body="", has_media=True, media_url="https://api.twilio.com/sticker.webp")
    params["MediaContentType0"] = "image/webp"
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    from app.main import redis_client

    state = await ConversationFSM(redis_client).get_state("972501234567")
    assert state.state == "IDLE"
    assert "receipt photos" in mock_whatsapp.call_args.args[1]


async def test_image_with_caption_stores_caption_as_description(client, twilio_form_params, twilio_headers, mock_whatsapp, mocker):
    mocker.patch(
        "app.services.ocr.ReceiptOCRService.extract_from_url",
        return_value={
            "amount": 250,
            "currency": "NIS",
            "vendor": "Cafe Aroma",
            "expense_date": "2026-05-24",
            "category_hint": "Meals",
            "description": "OCR desc",
            "raw_text_summary": "receipt",
            "confidence": {"overall": 0.95, "amount": 0.9, "vendor": 0.9, "date": 0.9, "category": 0.9},
        },
    )
    params = twilio_form_params(body="Team lunch caption", has_media=True, media_url="https://api.twilio.com/test.jpg")
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    from app.main import redis_client

    state = await ConversationFSM(redis_client).get_state("972501234567")
    assert state.expense_data["description"] == "Team lunch caption"


async def test_null_media_url_falls_back_to_manual(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(has_media=True, media_url=None)
    params["NumMedia"] = "1"
    params["MediaContentType0"] = "image/jpeg"
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    from app.main import redis_client

    state = await ConversationFSM(redis_client).get_state("972501234567")
    assert state.state == "AWAITING_MANUAL_DETAILS"


async def test_unknown_media_type_attempts_ocr(client, twilio_form_params, twilio_headers, mock_whatsapp, mocker):
    ocr = mocker.patch("app.services.ocr.ReceiptOCRService.extract_from_url", return_value={
        "amount": None,
        "currency": None,
        "vendor": None,
        "expense_date": None,
        "category_hint": None,
        "description": None,
        "raw_text_summary": "unknown",
        "confidence": {"overall": 0.2, "amount": 0.1, "vendor": 0.1, "date": 0.1, "category": 0.1},
    })
    params = twilio_form_params(has_media=True, media_url="https://api.twilio.com/blob")
    params["MediaContentType0"] = "application/octet-stream"
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert ocr.called


async def test_lock_prevents_concurrent_processing(redis_client):
    fsm = ConversationFSM(redis_client)
    assert await fsm.acquire_lock("972501234567", timeout_seconds=1)
    assert not await fsm.acquire_lock("972501234567", timeout_seconds=1)


async def test_lock_released_after_handler_completes(client, twilio_form_params, twilio_headers, redis_client, mock_whatsapp):
    params = twilio_form_params(body="hello")
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert await redis_client.get("lock:972501234567") is None


async def test_corrupted_redis_state_resets_conversation(redis_client):
    await redis_client.set("conv:972501234567", "{bad json")
    assert await ConversationFSM(redis_client).get_state("972501234567") is None


async def test_amount_european_comma_decimal():
    assert ReceiptOCRService()._parse_amount_string("250,00") == 250.0


async def test_amount_thousands_separator():
    assert ReceiptOCRService()._parse_amount_string("1.250,00") == 1250.0


async def test_amount_with_shekel_symbol():
    result = ReceiptOCRService()._validate_and_normalize({"amount": "₪250", "currency": "USD", "confidence": {}})
    assert result["amount"] == 250.0
    assert result["currency"] == "NIS"


async def test_amount_invalid_string_returns_none():
    assert ReceiptOCRService()._parse_amount_string("Unknown") is None


async def test_corrupted_image_raises_extraction_error():
    with pytest.raises(ReceiptExtractionError):
        await ReceiptOCRService().extract_from_image_bytes(b"not-image")


async def test_gemini_rate_limit_retries_with_backoff(mocker):
    svc = ReceiptOCRService()
    attempts = {"count": 0}

    def flaky():
        attempts["count"] += 1
        if attempts["count"] < 2:
            raise Exception("429 quota")
        return {"amount": 1}

    mocker.patch.object(svc, "_post_gemini", side_effect=lambda *args, **kwargs: flaky())
    mocker.patch("asyncio.sleep", new=mocker.AsyncMock())
    result = await svc._call_gemini_json("prompt", {}, image_bytes=None)
    assert result == {"amount": 1}
    assert attempts["count"] == 2


async def test_gemini_fails_all_retries_goes_to_manual(mocker):
    svc = ReceiptOCRService()
    mocker.patch.object(svc, "_post_gemini", side_effect=Exception("503 server"))
    mocker.patch("asyncio.sleep", new=mocker.AsyncMock())
    with pytest.raises(ReceiptExtractionError):
        await svc._call_gemini_json("prompt", {}, image_bytes=None, max_retries=2)


def test_strip_markdown_fence():
    assert ReceiptOCRService()._strip_fence("```json\n{\"a\": 1}\n```") == "{\"a\": 1}"


def test_detect_image_mime_invalid_defaults_jpeg():
    assert ReceiptOCRService()._detect_image_mime(b"bad") == "image/jpeg"


def test_date_short_year():
    assert ReceiptOCRService()._normalize_date("20/05/24") == "2024-05-20"


def test_date_far_future_returns_none():
    far_future = (datetime.now().date() + timedelta(days=400)).isoformat()
    assert ReceiptOCRService()._normalize_date(far_future) is None


async def test_zero_amount_rejected(test_db):
    result = await PolicyEngine().validate({"amount": 0, "currency": "NIS", "category": "Meals", "expense_date": "2026-05-24"}, test_db)
    assert result.decision == PolicyDecision.REJECTED


async def test_very_large_amount_goes_to_exception(test_db):
    result = await PolicyEngine().validate({"amount": 100001, "currency": "NIS", "category": "Meals", "expense_date": "2026-05-24"}, test_db)
    assert result.decision == PolicyDecision.EXCEPTION


def test_category_lowercase_normalized():
    assert normalize_category("  meals  ") == "Meals"


def test_normalize_category_none():
    assert normalize_category(None) is None


def test_parse_amount_invalid_to_zero():
    assert _parse_amount("not money") == 0.0


def test_sync_policy_zero_amount_rejected():
    result = SyncPolicyEngine().validate({"amount": 0, "currency": "NIS", "category": "Meals"}, None)
    assert result.decision == PolicyDecision.REJECTED


def test_sync_policy_large_amount_exception():
    result = SyncPolicyEngine().validate({"amount": 100001, "currency": "NIS", "category": "Meals"}, None)
    assert result.decision == PolicyDecision.EXCEPTION


def test_sync_policy_unknown_category_rejected():
    result = SyncPolicyEngine().validate({"amount": 1, "currency": "NIS", "category": "Nope"}, None)
    assert result.decision == PolicyDecision.REJECTED


def test_sync_policy_db_error_returns_exception(mocker):
    db = MagicMock()
    db.query.side_effect = Exception("db down")
    result = SyncPolicyEngine().validate({"amount": 1, "currency": "NIS", "category": "Meals"}, db)
    assert result.decision == PolicyDecision.EXCEPTION


def test_sync_policy_no_policy_exception(mocker):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    result = SyncPolicyEngine().validate({"amount": 1, "currency": "NIS", "category": "Meals"}, db)
    assert result.decision == PolicyDecision.EXCEPTION


def test_sync_policy_get_gl_account_unknown_raises(mocker):
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = None
    with pytest.raises(ValueError):
        SyncPolicyEngine().get_gl_account("Meals", db)


def test_sync_policy_get_gl_account_success(mocker):
    mapping = MagicMock()
    mapping.gl_account = "6110"
    mapping.cost_center = "CC-GEN"
    db = MagicMock()
    db.query.return_value.filter_by.return_value.first.return_value = mapping
    assert SyncPolicyEngine().get_gl_account("Meals", db) == ("6110", "CC-GEN")


async def test_duplicate_expense_detected_and_blocked(test_db):
    expense = await create_expense(test_db, {
        "whatsapp_number": "972501234567",
        "amount": 250,
        "currency": "NIS",
        "amount_nis": 250,
        "category": "Meals",
        "expense_date": "2026-05-24",
    })
    duplicate = await find_potential_duplicate(test_db, "972501234567", 250, None, "2026-05-24", "Meals")
    assert duplicate.id == expense.id


async def test_arabic_text_uses_hebrew_template():
    assert detect_language("مرحبا اريد تقديم مصروف") == "he"


def test_emoji_reply_doesnt_match_confirmation():
    assert parse_confirmation_reply("👍") is None


async def test_unauthorized_manager_blocked(redis_client, mock_whatsapp, test_db):
    import app.main as main_module
    from app.api.webhook import handle_manager_reply
    from app.services.whatsapp import WhatsAppService

    main_module.redis_client = redis_client
    expense = await create_expense(test_db, {
        "whatsapp_number": "972501234567",
        "amount": 850,
        "currency": "NIS",
        "amount_nis": 850,
        "category": "Entertainment",
        "expense_date": "2026-05-24",
        "approval_status": "PENDING",
    })
    await handle_manager_reply("972999999999", "approve", expense.id[:8].upper(), test_db, ConversationFSM(redis_client), WhatsAppService(), "en")
    assert "not authorized" in mock_whatsapp.call_args.args[1]


async def test_missing_amount_blocks_submission(redis_client, mock_whatsapp, test_db):
    import app.main as main_module
    from app.api.webhook import _submit_expense
    from app.services.whatsapp import WhatsAppService

    main_module.redis_client = redis_client
    fsm = ConversationFSM(redis_client)
    state = ConversationState(state="AWAITING_CONFIRMATION", phone="972501234567", expense_data={"category": "Meals", "expense_date": "2026-05-24"})
    await _submit_expense("972501234567", state, test_db, fsm, WhatsAppService())
    assert "missing" in mock_whatsapp.call_args.args[1].lower()
    assert (await fsm.get_state("972501234567")).state == "AWAITING_MANUAL_DETAILS"


async def test_duplicate_submission_blocks_db_insert(redis_client, mock_whatsapp, test_db):
    import app.main as main_module
    from app.api.webhook import _submit_expense
    from app.services.whatsapp import WhatsAppService

    main_module.redis_client = redis_client
    await create_expense(test_db, {
        "whatsapp_number": "972501234567",
        "amount": 250,
        "currency": "NIS",
        "amount_nis": 250,
        "category": "Meals",
        "expense_date": "2026-05-24",
    })
    state = ConversationState(
        state="AWAITING_CONFIRMATION",
        phone="972501234567",
        expense_data={"amount": 250, "currency": "NIS", "category": "Meals", "expense_date": "2026-05-24"},
    )
    await _submit_expense("972501234567", state, test_db, ConversationFSM(redis_client), WhatsAppService())
    assert "already submitted" in mock_whatsapp.call_args.args[1]


async def test_successful_submit_creates_expense(redis_client, mock_whatsapp, test_db, mocker):
    import app.main as main_module
    from app.api.webhook import _submit_expense
    from app.services.whatsapp import WhatsAppService

    main_module.redis_client = redis_client
    delay = mocker.patch("app.api.webhook.process_expense_task.delay")
    state = ConversationState(
        state="AWAITING_CONFIRMATION",
        phone="972501234567",
        expense_data={
            "amount": 251,
            "currency": "NIS",
            "vendor": "<Cafe>",
            "category": "Meals",
            "expense_date": "2026-05-24",
            "description": "<lunch>",
        },
    )
    await _submit_expense("972501234567", state, test_db, ConversationFSM(redis_client), WhatsAppService())
    delay.assert_called_once()
    assert "Processing" in mock_whatsapp.call_args.args[1]


async def test_approve_nonexistent_expense_id(redis_client, mock_whatsapp, test_db):
    from app.api.webhook import handle_manager_reply
    from app.services.whatsapp import WhatsAppService

    await handle_manager_reply("972521234567", "approve", "NOPE9999", test_db, ConversationFSM(redis_client), WhatsAppService(), "en")
    assert "not found" in mock_whatsapp.call_args.args[1]


async def test_approve_already_approved_expense(redis_client, mock_whatsapp, test_db):
    from app.api.webhook import handle_manager_reply
    from app.services.whatsapp import WhatsAppService

    expense = await create_expense(test_db, {
        "whatsapp_number": "972501234567",
        "amount": 850,
        "currency": "NIS",
        "amount_nis": 850,
        "category": "Entertainment",
        "expense_date": "2026-05-24",
        "approval_status": "APPROVED",
    })
    await handle_manager_reply("972521234567", "approve", expense.id[:8].upper(), test_db, ConversationFSM(redis_client), WhatsAppService(), "en")
    assert "already approved" in mock_whatsapp.call_args.args[1]


async def test_reject_command_lowercase(redis_client, mock_whatsapp, test_db):
    from app.api.webhook import handle_manager_reply
    from app.services.whatsapp import WhatsAppService

    expense = await create_expense(test_db, {
        "whatsapp_number": "972501234567",
        "amount": 850,
        "currency": "NIS",
        "amount_nis": 850,
        "category": "Entertainment",
        "expense_date": "2026-05-24",
        "approval_status": "PENDING",
    })
    await handle_manager_reply("972521234567", "reject", expense.id[:8].upper(), test_db, ConversationFSM(redis_client), WhatsAppService(), "en")
    assert "rejected" in mock_whatsapp.call_args.args[1]
