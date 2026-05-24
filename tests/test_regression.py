from datetime import date, timedelta
from unittest.mock import MagicMock

from app.fsm.conversation import ConversationFSM
from app.models.expense import create_expense, list_all_expenses
from app.models.schemas import ConversationState


def _mock_expense():
    expense = MagicMock()
    expense.id = "abcd1234-efgh-5678"
    expense.employee_id = "EMP001"
    expense.expense_date = date.today().isoformat()
    expense.description = "Team lunch"
    expense.amount = 250
    expense.amount_nis = 250
    expense.currency = "NIS"
    expense.gl_account = "6110"
    expense.category = "Meals"
    expense.vendor = "Cafe Aroma"
    return expense


async def test_scenario_manual_entry_flow(client, twilio_form_params, twilio_headers, mock_whatsapp, mocker):
    mocker.patch(
        "app.services.ocr.ReceiptOCRService.parse_manual_details",
        return_value={"amount": 250, "currency": "NIS", "vendor": "Cafe Aroma", "expense_date": date.today().isoformat(), "category": "Meals", "description": "lunch", "confidence": {"overall": 0.8}},
    )
    params = twilio_form_params(body="250 nis cafe aroma today meals lunch", has_media=False)
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    from app.main import redis_client

    state = await ConversationFSM(redis_client).get_state("972501234567")
    assert state.state == "AWAITING_CONFIRMATION"
    assert state.expense_data["amount"] == 250


async def test_scenario_unsupported_media_then_receipt(client, twilio_form_params, twilio_headers, mock_whatsapp, mocker):
    video = twilio_form_params(has_media=True, media_url="https://api.twilio.com/video.mp4", sid="SM_video")
    video["MediaContentType0"] = "video/mp4"
    await client.post("/webhook/twilio", data=video, headers=twilio_headers(video))
    assert "receipt photos" in mock_whatsapp.call_args.args[1]

    mocker.patch(
        "app.services.ocr.ReceiptOCRService.extract_from_url",
        return_value={"amount": 250, "currency": "NIS", "vendor": "Cafe Aroma", "expense_date": date.today().isoformat(), "category_hint": "Meals", "description": "lunch", "raw_text_summary": "receipt", "confidence": {"overall": 0.95, "amount": 0.9, "vendor": 0.9, "date": 0.9, "category": 0.9}},
    )
    image = twilio_form_params(has_media=True, media_url="https://api.twilio.com/test.jpg", sid="SM_image")
    await client.post("/webhook/twilio", data=image, headers=twilio_headers(image))
    from app.main import redis_client

    assert (await ConversationFSM(redis_client).get_state("972501234567")).state == "AWAITING_CONFIRMATION"


async def test_scenario_gemini_down_graceful(client, twilio_form_params, twilio_headers, mock_whatsapp, mocker):
    mocker.patch("app.services.ocr.ReceiptOCRService.extract_from_url", side_effect=Exception("Gemini down"))
    params = twilio_form_params(has_media=True, media_url="https://api.twilio.com/test.jpg")
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    from app.main import redis_client

    state = await ConversationFSM(redis_client).get_state("972501234567")
    assert state.state == "AWAITING_MANUAL_DETAILS"


async def test_scenario_correction_flow(redis_client, mock_whatsapp, test_db, mocker):
    import app.main as main_module
    from app.api.webhook import handle_incoming_message

    main_module.redis_client = redis_client
    mocker.patch("app.services.ocr.ReceiptOCRService.parse_manual_details", return_value={"amount": 275, "currency": "NIS", "category": "Meals", "confidence": {"overall": 0.8}})
    fsm = ConversationFSM(redis_client)
    await fsm.set_state("972501234567", ConversationState(state="AWAITING_CORRECTION", phone="972501234567", expense_data={"amount": 250, "currency": "NIS", "category": "Meals", "expense_date": date.today().isoformat()}))
    await handle_incoming_message("972501234567", "amount 275", False, None, "SM_fix", test_db)
    state = await fsm.get_state("972501234567")
    assert state.expense_data["amount"] == 275


async def test_scenario_abandoned_and_restarted(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(body="hello after ttl")
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    from app.main import redis_client

    state = await ConversationFSM(redis_client).get_state("972501234567")
    assert state.state == "IDLE"


async def test_scenario_rejection_flow(redis_client, mock_whatsapp_sync, test_db):
    from app.services.policy_engine import PolicyDecision, PolicyEngine

    old = (date.today() - timedelta(days=45)).isoformat()
    result = await PolicyEngine().validate({"amount": 600, "currency": "NIS", "category": "Meals", "expense_date": old}, test_db)
    assert result.decision == PolicyDecision.REJECTED


async def test_scenario_rapid_double_send(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(body="hello", sid="SM_same")
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert mock_whatsapp.call_count <= 1


async def test_scenario_manager_approval_authorized(redis_client, mock_whatsapp, test_db):
    from app.api.webhook import handle_manager_reply
    from app.services.whatsapp import WhatsAppService

    expense = await create_expense(test_db, {"whatsapp_number": "972501234567", "amount": 850, "currency": "NIS", "amount_nis": 850, "category": "Entertainment", "expense_date": date.today().isoformat(), "approval_status": "PENDING", "language": "en"})
    await handle_manager_reply("972521234567", "approve", expense.id[:8].upper(), test_db, ConversationFSM(redis_client), WhatsAppService(), "en")
    assert "approved" in mock_whatsapp.call_args_list[-2].args[1]


async def test_scenario_duplicate_expense_blocked(redis_client, mock_whatsapp, test_db, mocker):
    import app.main as main_module
    from app.api.webhook import _submit_expense
    from app.services.whatsapp import WhatsAppService

    main_module.redis_client = redis_client
    await create_expense(test_db, {"whatsapp_number": "972501234567", "amount": 250, "currency": "NIS", "amount_nis": 250, "category": "Meals", "expense_date": date.today().isoformat()})
    state = ConversationState(state="AWAITING_CONFIRMATION", phone="972501234567", expense_data={"amount": 250, "currency": "NIS", "category": "Meals", "expense_date": date.today().isoformat()})
    await _submit_expense("972501234567", state, test_db, ConversationFSM(redis_client), WhatsAppService())
    assert "already submitted" in mock_whatsapp.call_args.args[1]


async def test_scenario_priority_down_then_recovered():
    from app.services.priority_erp import MockPriorityERP

    MockPriorityERP.configure_failure("line")
    try:
        await MockPriorityERP().create_expense(_mock_expense())
    except Exception:
        pass
    MockPriorityERP.configure_failure(None)
    result = await MockPriorityERP().create_expense(_mock_expense())
    assert result.success is True
