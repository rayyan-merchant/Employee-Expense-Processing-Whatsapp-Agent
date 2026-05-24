import asyncio

import httpx
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app.config import settings


def _clean_number(value: str | None) -> str:
    value = value or ""
    if value.startswith("whatsapp:"):
        value = value[len("whatsapp:") :]
    return value.lstrip("+")


class WhatsAppService:
    def __init__(self):
        self.client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        self.validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)
        self.from_number = settings.TWILIO_WHATSAPP_NUMBER

    def validate_signature(self, url: str, params: dict, signature: str) -> bool:
        return self.validator.validate(url, params, signature)

    def send_message_sync(self, to: str, body: str) -> str:
        msg = self.client.messages.create(from_=self.from_number, to=to, body=body)
        return msg.sid

    async def send_message(self, to: str, body: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.send_message_sync, to, body)

    def parse_incoming(self, form_data: dict) -> dict:
        num_media = int(form_data.get("NumMedia") or 0)
        return {
            "message_sid": str(form_data.get("MessageSid") or ""),
            "from_number": _clean_number(form_data.get("From")),
            "to_number": _clean_number(form_data.get("To")),
            "body": str(form_data.get("Body") or ""),
            "has_media": num_media > 0,
            "media_url": form_data.get("MediaUrl0") if num_media > 0 else None,
            "media_content_type": form_data.get("MediaContentType0") if num_media > 0 else None,
            "num_media": num_media,
        }

    async def download_media(self, media_url: str) -> bytes:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                media_url,
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.content
