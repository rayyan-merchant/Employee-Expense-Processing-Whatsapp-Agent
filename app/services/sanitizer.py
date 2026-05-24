import html
import re

MAX_VENDOR_LENGTH = 200
MAX_DESCRIPTION_LENGTH = 500
MAX_BODY_LENGTH = 2000


def sanitize_text(text: str | None, max_length: int = 500) -> str | None:
    if text is None:
        return None
    cleaned = text.replace("\x00", "")
    cleaned = cleaned[:max_length]
    cleaned = " ".join(cleaned.split())
    cleaned = html.escape(cleaned)
    return cleaned.strip() or None


def sanitize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone or "")
    if not (10 <= len(digits) <= 15):
        raise ValueError(f"Invalid phone number: {phone!r}")
    return digits
