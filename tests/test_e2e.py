import asyncio

from app.fsm.conversation import ConversationFSM
from app.models.schemas import ConversationState


async def test_image_message_transitions_to_ocr_state(client, twilio_form_params, twilio_headers, mock_whatsapp, mocker):
    mocker.patch(
        "app.services.ocr.ReceiptOCRService.extract_from_url",
        return_value={
            "amount": 250.0,
            "currency": "NIS",
            "vendor": "Cafe Aroma",
            "expense_date": "2024-05-20",
            "category_hint": "Meals",
            "description": "lunch",
            "raw_text_summary": "Test receipt",
            "confidence": {"overall": 0.95, "amount": 0.98, "vendor": 0.92, "date": 0.90, "category": 0.88},
        },
    )
    params = twilio_form_params(has_media=True, media_url="https://api.twilio.com/test.jpg")
    resp = await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert resp.status_code == 200
    await asyncio.sleep(0.25)
    assert mock_whatsapp.called


async def test_text_message_in_idle_sends_welcome(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(body="Hello", has_media=False)
    resp = await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert resp.status_code == 200


async def test_hebrew_text_detected(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(body="שלום, אני רוצה להגיש הוצאה", has_media=False)
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    import app.main as main_module

    await asyncio.sleep(0.25)
    state = await ConversationFSM(main_module.redis_client).get_state("972501234567")
    assert state.lang == "he"


async def test_low_confidence_ocr_triggers_manual_flow(client, twilio_form_params, twilio_headers, mock_whatsapp, mocker):
    mocker.patch(
        "app.services.ocr.ReceiptOCRService.extract_from_url",
        return_value={
            "amount": None,
            "currency": None,
            "vendor": None,
            "expense_date": None,
            "category_hint": None,
            "description": None,
            "raw_text_summary": "Blurry",
            "confidence": {"overall": 0.3, "amount": 0.2, "vendor": 0.2, "date": 0.2, "category": 0.2},
        },
    )
    params = twilio_form_params(has_media=True, media_url="https://api.twilio.com/test.jpg")
    await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    await asyncio.sleep(0.25)
    assert mock_whatsapp.call_count >= 2


async def test_parse_category_reply_number():
    from app.services.language import parse_category_reply

    assert parse_category_reply("1") == "Meals"
    assert parse_category_reply("2") == "Travel"
    assert parse_category_reply("8") == "Other"


async def test_parse_category_reply_english():
    from app.services.language import parse_category_reply

    assert parse_category_reply("meals") == "Meals"
    assert parse_category_reply("software") == "Software"
    assert parse_category_reply("hotel") == "Accommodation"


async def test_parse_category_reply_hebrew():
    from app.services.language import parse_category_reply

    assert parse_category_reply("ארוחות") == "Meals"
    assert parse_category_reply("נסיעות") == "Travel"


async def test_parse_confirmation_confirm():
    from app.services.language import parse_confirmation_reply

    assert parse_confirmation_reply("1") == "confirm"
    assert parse_confirmation_reply("yes") == "confirm"
    assert parse_confirmation_reply("כן") == "confirm"


async def test_parse_confirmation_cancel():
    from app.services.language import parse_confirmation_reply

    assert parse_confirmation_reply("2") == "cancel"
    assert parse_confirmation_reply("no") == "cancel"
    assert parse_confirmation_reply("לא") == "cancel"


async def test_duplicate_not_processed_twice(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(body="hello")
    headers = twilio_headers(params)
    await client.post("/webhook/twilio", data=params, headers=headers)
    await client.post("/webhook/twilio", data=params, headers=headers)
    assert mock_whatsapp.call_count <= 1


async def test_awaiting_category_to_confirmation(redis_client, mock_whatsapp, test_db):
    import app.main as main_module
    from app.api.webhook import handle_incoming_message

    main_module.redis_client = redis_client
    fsm = ConversationFSM(redis_client)
    await fsm.set_state("972501234567", ConversationState(state="AWAITING_CATEGORY", phone="972501234567", expense_data={"amount": 20, "currency": "NIS"}))
    await handle_incoming_message("972501234567", "1", False, None, "SM_x", test_db)
    state = await fsm.get_state("972501234567")
    assert state.state == "AWAITING_CONFIRMATION"


async def test_awaiting_category_accepts_full_manual_details(redis_client, mock_whatsapp, test_db):
    import app.main as main_module
    from app.api.webhook import handle_incoming_message

    main_module.redis_client = redis_client
    fsm = ConversationFSM(redis_client)
    await fsm.set_state("972501234567", ConversationState(state="AWAITING_CATEGORY", phone="972501234567", expense_data={"description": "old"}))
    await handle_incoming_message(
        "972501234567",
        "Amount: 250 NIS\nVendor: Cafe Aroma\nDate: 2026-05-25\nCategory: Meals\nDescription: Team lunch",
        False,
        None,
        "SM_manual_full",
        test_db,
    )
    state = await fsm.get_state("972501234567")
    assert state.state == "AWAITING_CONFIRMATION"
    assert state.expense_data["amount"] == 250.0
    assert state.expense_data["vendor"] == "Cafe Aroma"
    assert state.expense_data["expense_date"] == "2026-05-25"
    assert state.expense_data["category"] == "Meals"
    assert state.expense_data["description"] == "Team lunch"


async def test_awaiting_confirmation_accepts_full_manual_correction(redis_client, mock_whatsapp, test_db):
    import app.main as main_module
    from app.api.webhook import handle_incoming_message

    main_module.redis_client = redis_client
    fsm = ConversationFSM(redis_client)
    await fsm.set_state(
        "972501234567",
        ConversationState(
            state="AWAITING_CONFIRMATION",
            phone="972501234567",
            expense_data={
                "amount": 100.0,
                "currency": "NIS",
                "vendor": "Old Vendor",
                "expense_date": "2026-05-24",
                "category": "Other",
                "description": "old",
            },
        ),
    )
    await handle_incoming_message(
        "972501234567",
        "Amount: 250 NIS Vendor: Cafe Aroma Date: 2026-05-25 Category: Meals Description: Team lunch",
        False,
        None,
        "SM_manual_correction",
        test_db,
    )
    state = await fsm.get_state("972501234567")
    assert state.state == "AWAITING_CONFIRMATION"
    assert state.expense_data["amount"] == 250.0
    assert state.expense_data["vendor"] == "Cafe Aroma"
    assert state.expense_data["expense_date"] == "2026-05-25"
    assert state.expense_data["category"] == "Meals"


async def test_receipt_received_processes_new_image(redis_client, mock_whatsapp, test_db, mocker):
    import app.main as main_module
    from app.api.webhook import handle_incoming_message

    main_module.redis_client = redis_client
    mocker.patch(
        "app.services.ocr.ReceiptOCRService.extract_from_url",
        return_value={
            "amount": 250.0,
            "currency": "NIS",
            "vendor": "Cafe Aroma",
            "expense_date": "2026-05-24",
            "category_hint": "Meals",
            "description": "Team lunch",
            "raw_text_summary": "Test receipt",
            "confidence": {"overall": 0.95, "amount": 0.98, "vendor": 0.92, "date": 0.90, "category": 0.88},
        },
    )
    fsm = ConversationFSM(redis_client)
    await fsm.set_state("972501234567", ConversationState(state="RECEIPT_RECEIVED", phone="972501234567"))
    await handle_incoming_message("972501234567", "", True, "https://api.twilio.com/test.jpg", "SM_x", test_db)
    state = await fsm.get_state("972501234567")
    assert state.state == "AWAITING_CONFIRMATION"
    assert state.expense_data["vendor"] == "Cafe Aroma"


async def test_receipt_received_accepts_manual_details(redis_client, mock_whatsapp, test_db, mocker):
    import app.main as main_module
    from app.api.webhook import handle_incoming_message

    main_module.redis_client = redis_client
    mocker.patch(
        "app.services.ocr.ReceiptOCRService.parse_manual_details",
        return_value={
            "amount": 250.0,
            "currency": "NIS",
            "vendor": "Cafe Aroma",
            "expense_date": "2026-05-24",
            "category": "Meals",
            "description": "Team lunch",
            "confidence": {"overall": 0.8},
        },
    )
    fsm = ConversationFSM(redis_client)
    await fsm.set_state("972501234567", ConversationState(state="RECEIPT_RECEIVED", phone="972501234567"))
    await handle_incoming_message("972501234567", "amount 250 nis meals", False, None, "SM_x", test_db)
    state = await fsm.get_state("972501234567")
    assert state.state == "AWAITING_CONFIRMATION"
    assert state.expense_data["category"] == "Meals"


async def test_awaiting_confirmation_cancel(redis_client, mock_whatsapp, test_db):
    import app.main as main_module
    from app.api.webhook import handle_incoming_message

    main_module.redis_client = redis_client
    fsm = ConversationFSM(redis_client)
    await fsm.set_state("972501234567", ConversationState(state="AWAITING_CONFIRMATION", phone="972501234567", expense_data={"amount": 20, "currency": "NIS", "category": "Meals"}))
    await handle_incoming_message("972501234567", "2", False, None, "SM_x", test_db)
    assert await fsm.get_state("972501234567") is None


async def test_awaiting_correction(redis_client, mock_whatsapp, test_db, mocker):
    import app.main as main_module
    from app.api.webhook import handle_incoming_message

    main_module.redis_client = redis_client
    mocker.patch("app.services.ocr.ReceiptOCRService.parse_manual_details", return_value={"amount": 30, "currency": "NIS", "category": "Meals"})
    fsm = ConversationFSM(redis_client)
    await fsm.set_state("972501234567", ConversationState(state="AWAITING_CORRECTION", phone="972501234567", expense_data={"amount": 20, "currency": "NIS", "category": "Meals"}))
    await handle_incoming_message("972501234567", "amount 30", False, None, "SM_x", test_db)
    state = await fsm.get_state("972501234567")
    assert state.state == "AWAITING_CONFIRMATION"
    assert state.expense_data["amount"] == 30


async def test_terminal_state_resets(redis_client, mock_whatsapp, test_db):
    import app.main as main_module
    from app.api.webhook import handle_incoming_message

    main_module.redis_client = redis_client
    fsm = ConversationFSM(redis_client)
    await fsm.set_state("972501234567", ConversationState(state="COMPLETED", phone="972501234567"))
    await handle_incoming_message("972501234567", "hello", False, None, "SM_x", test_db)
    assert await fsm.get_state("972501234567") is None


async def test_manager_approve_flow(client, twilio_form_params, twilio_headers, mock_whatsapp, test_db):
    from app.models.expense import create_expense

    expense = await create_expense(test_db, {"whatsapp_number": "972501234567", "amount": 100.0, "currency": "NIS", "amount_nis": 100.0, "category": "Meals", "expense_date": "2024-05-20", "approval_status": "PENDING", "priority_status": "NOT_UPLOADED", "language": "en", "ocr_confidence": 0.9})
    params = twilio_form_params(phone="972521234567", body=f"APPROVE {expense.id[:8].upper()}", sid="SM_manager_1")
    resp = await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert resp.status_code == 200


async def test_dashboard_stats_and_detail(client, test_db):
    from app.models.expense import create_expense

    expense = await create_expense(test_db, {"whatsapp_number": "972501234567", "amount": 100.0, "currency": "NIS", "amount_nis": 100.0, "category": "Meals", "expense_date": "2024-05-20", "policy_status": "AUTO_APPROVE", "approval_status": "NOT_REQUIRED", "priority_status": "NOT_UPLOADED", "language": "en", "ocr_confidence": 0.9})
    stats = await client.get("/api/stats")
    assert "by_policy_status" in stats.json()
    detail = await client.get(f"/api/expenses/{expense.id}")
    assert detail.status_code == 200
    assert detail.json()["id"] == expense.id
