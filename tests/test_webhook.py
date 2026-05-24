import pytest
from twilio.base.exceptions import TwilioRestException

from app.services.whatsapp import WhatsAppService


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


def test_whatsapp_daily_limit_does_not_raise(mocker):
    svc = WhatsAppService()
    mocker.patch.object(
        svc.client.messages,
        "create",
        side_effect=TwilioRestException(
            status=429,
            uri="/Messages.json",
            msg="Daily message limit exceeded",
            code=63038,
            method="POST",
        ),
    )
    assert svc.send_message_sync("whatsapp:+972501234567", "hello") == ""


def test_parse_incoming_adds_media_type(twilio_form_params):
    svc = WhatsAppService()
    params = twilio_form_params(has_media=True, media_url="https://api.twilio.com/test.jpg")
    params["MediaContentType0"] = "image/jpeg; charset=binary"
    parsed = svc.parse_incoming(params)
    assert parsed["media_type"] == "image/jpeg"


def test_parse_incoming_invalid_phone_raises(twilio_form_params):
    svc = WhatsAppService()
    params = twilio_form_params(phone="123")
    with pytest.raises(ValueError):
        svc.parse_incoming(params)


async def test_safe_send_returns_false_on_error(mocker):
    from app.services.whatsapp import safe_send

    svc = WhatsAppService()
    mocker.patch.object(svc, "send_message", side_effect=Exception("21211 invalid"))
    assert await safe_send(svc, "whatsapp:+972501234567", "hello") is False


async def test_safe_send_truncates_long_message(mocker):
    from app.services.whatsapp import safe_send

    svc = WhatsAppService()
    send = mocker.patch.object(svc, "send_message", return_value="SM123")
    assert await safe_send(svc, "whatsapp:+972501234567", "x" * 2000) is True
    assert len(send.call_args.args[1]) == 1600
