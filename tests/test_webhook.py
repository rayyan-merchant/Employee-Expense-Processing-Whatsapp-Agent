async def test_health_ok(client):
    resp = await client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_health_redis_connected(client):
    resp = await client.get("/health")
    assert resp.json()["redis"] is True


async def test_health_db_connected(client):
    resp = await client.get("/health")
    assert resp.json()["db"] is True


async def test_webhook_rejects_missing_signature(client, twilio_form_params, mocker):
    mocker.patch("app.services.whatsapp.WhatsAppService.validate_signature", return_value=False)
    resp = await client.post("/webhook/twilio", data=twilio_form_params())
    assert resp.status_code == 403


async def test_webhook_accepts_valid_signature(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params()
    resp = await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert resp.status_code == 200


async def test_webhook_returns_twiml(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params()
    resp = await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert "<?xml" in resp.text
    assert "<Response>" in resp.text


async def test_webhook_deduplicates(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(body="hello")
    headers = twilio_headers(params)
    await client.post("/webhook/twilio", data=params, headers=headers)
    resp2 = await client.post("/webhook/twilio", data=params, headers=headers)
    assert resp2.status_code == 200
    assert mock_whatsapp.call_count <= 1


async def test_webhook_parses_text_message(client, twilio_form_params, twilio_headers, mock_whatsapp):
    params = twilio_form_params(body="Hello agent", has_media=False)
    resp = await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert resp.status_code == 200


async def test_webhook_parses_media_message(client, twilio_form_params, twilio_headers, mock_whatsapp, mocker):
    mocker.patch(
        "app.services.ocr.ReceiptOCRService.extract_from_url",
        return_value={
            "amount": 10,
            "currency": "NIS",
            "vendor": "Test",
            "expense_date": "2024-05-20",
            "category_hint": None,
            "description": "test",
            "raw_text_summary": "test",
            "confidence": {"overall": 0.8, "category": 0.2},
        },
    )
    params = twilio_form_params(has_media=True, media_url="https://api.twilio.com/test.jpg")
    resp = await client.post("/webhook/twilio", data=params, headers=twilio_headers(params))
    assert resp.status_code == 200


async def test_dashboard_returns_200(client):
    resp = await client.get("/")
    assert resp.status_code == 200


async def test_api_expenses_returns_list(client):
    resp = await client.get("/api/expenses")
    assert resp.status_code == 200
    assert isinstance(resp.json(), list)
