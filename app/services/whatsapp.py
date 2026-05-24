import asyncio
import logging

import httpx
from twilio.base.exceptions import TwilioRestException
from twilio.request_validator import RequestValidator
from twilio.rest import Client

from app.config import settings
from app.services.sanitizer import MAX_BODY_LENGTH, sanitize_phone

logger = logging.getLogger(__name__)
TWILIO_DAILY_MESSAGE_LIMIT_ERROR = 63038
MAX_WA_MESSAGE_LENGTH = 1600


def _clean_number(value: str | None) -> str:
    value = value or ""
    if value.startswith("whatsapp:"):
        value = value[len("whatsapp:") :]
    return sanitize_phone(value)


class WhatsAppService:
    def __init__(self):
        self.client = Client(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN)
        self.validator = RequestValidator(settings.TWILIO_AUTH_TOKEN)
        self.from_number = settings.TWILIO_WHATSAPP_NUMBER

    def validate_signature(self, url: str, params: dict, signature: str) -> bool:
        return self.validator.validate(url, params, signature)

    def send_message_sync(self, to: str, body: str) -> str:
        try:
            msg = self.client.messages.create(from_=self.from_number, to=to, body=body)
            return msg.sid
        except TwilioRestException as exc:
            if exc.code == TWILIO_DAILY_MESSAGE_LIMIT_ERROR:
                logger.error(
                    "Twilio daily WhatsApp message limit exceeded; outbound message to %s was not sent",
                    _mask_whatsapp_number(to),
                )
                return ""
            raise

    async def send_message(self, to: str, body: str) -> str:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.send_message_sync, to, body)

    def parse_incoming(self, form_data: dict) -> dict:
        num_media = int(form_data.get("NumMedia") or 0)
        body = str(form_data.get("Body") or "")[:MAX_BODY_LENGTH]
        return {
            "message_sid": str(form_data.get("MessageSid") or ""),
            "from_number": _clean_number(form_data.get("From")),
            "to_number": _clean_number(form_data.get("To")),
            "body": body,
            "has_media": num_media > 0,
            "media_url": form_data.get("MediaUrl0") if num_media > 0 else None,
            "media_content_type": form_data.get("MediaContentType0") if num_media > 0 else None,
            "media_type": _media_type(form_data.get("MediaContentType0")) if num_media > 0 else None,
            "num_media": num_media,
        }

    async def download_media(self, media_url: str) -> bytes:
        async with httpx.AsyncClient(follow_redirects=True) as client:
            resp = await client.get(
                media_url,
                auth=(settings.TWILIO_ACCOUNT_SID, settings.TWILIO_AUTH_TOKEN),
                timeout=30.0,
            )
            resp.raise_for_status()
            return resp.content


def _mask_whatsapp_number(value: str) -> str:
    try:
        clean = _clean_number(value)
    except ValueError:
        clean = "".join(ch for ch in (value or "") if ch.isdigit())
    return f"...{clean[-4:]}" if clean else "unknown"


def _media_type(content_type: str | None) -> str | None:
    if not content_type:
        return None
    return content_type.split(";", 1)[0].strip().lower() or None


async def safe_send(wa: WhatsAppService, to: str, body: str) -> bool:
    body = (body or "")[:MAX_WA_MESSAGE_LENGTH]
    try:
        sid = await wa.send_message(to, body)
        logger.debug("Message sent to %s: %s", _mask_whatsapp_number(to), sid)
        return True
    except Exception as exc:
        error_str = str(exc)
        if "21211" in error_str:
            logger.error("Invalid phone number %s: %s", _mask_whatsapp_number(to), exc)
        elif "21610" in error_str:
            logger.warning("User %s has opted out of messages", _mask_whatsapp_number(to))
        else:
            logger.error("Failed to send to %s: %s", _mask_whatsapp_number(to), exc)
        return False
