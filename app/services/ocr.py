import asyncio
import base64
import io
import json
import logging
import re
from datetime import date, datetime, timedelta
from typing import Any

import httpx
from PIL import Image

from app.config import settings
from app.services.language import parse_category_reply

logger = logging.getLogger(__name__)
PRIMARY_MODEL = "gemini-3-flash-preview"
FALLBACK_MODEL = "gemini-3.1-flash-lite"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MAX_IMAGE_BYTES = 10 * 1024 * 1024
CURRENCY_SYMBOLS = {"₪": "NIS", "ILS": "NIS", "NIS": "NIS", "$": "USD", "€": "EUR", "£": "GBP", "USD": "USD", "EUR": "EUR", "GBP": "GBP"}

CANONICAL_CATEGORIES = [
    "Meals",
    "Travel",
    "Accommodation",
    "Entertainment",
    "Office Supplies",
    "Software",
    "Conference",
    "Other",
]

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "amount": {"type": ["number", "null"]},
        "currency": {"type": ["string", "null"], "enum": ["NIS", "USD", "EUR", "GBP", None]},
        "vendor": {"type": ["string", "null"]},
        "expense_date": {"type": ["string", "null"]},
        "category_hint": {"type": ["string", "null"], "enum": CANONICAL_CATEGORIES + [None]},
        "description": {"type": ["string", "null"]},
        "confidence": {
            "type": "object",
            "properties": {
                "overall": {"type": "number"},
                "amount": {"type": "number"},
                "vendor": {"type": "number"},
                "date": {"type": "number"},
                "category": {"type": "number"},
            },
            "required": ["overall", "amount", "vendor", "date", "category"],
        },
        "raw_text_summary": {"type": "string"},
    },
    "required": ["amount", "currency", "vendor", "expense_date", "category_hint", "description", "confidence", "raw_text_summary"],
}

MANUAL_SCHEMA = {
    "type": "object",
    "properties": {
        "amount": {"type": ["number", "null"]},
        "currency": {"type": ["string", "null"], "enum": ["NIS", "USD", "EUR", "GBP", None]},
        "vendor": {"type": ["string", "null"]},
        "expense_date": {"type": ["string", "null"]},
        "category": {"type": ["string", "null"], "enum": CANONICAL_CATEGORIES + [None]},
        "description": {"type": ["string", "null"]},
    },
    "required": ["amount", "currency", "vendor", "expense_date", "category", "description"],
}

EXTRACTION_USER_PROMPT = """You are a receipt OCR specialist for an Israeli expense management system.
Extract structured expense data from this receipt image. Receipts may be in Hebrew or English.
Return JSON only. Do not guess invisible fields. Prefer the receipt grand total/net total/final total.
Normalize dates to YYYY-MM-DD. Default currency to NIS if the receipt uses shekel symbols, ILS, NIS, Hebrew text, or Israeli context.
Category must be one of: Meals, Travel, Accommodation, Entertainment, Office Supplies, Software, Conference, Other."""

MANUAL_PARSE_PROMPT = """Extract expense details from this employee text.
Today's date is {today}. "yesterday" = {yesterday}. "last week" = {last_week}.
Return JSON only using canonical categories.
Text: {text!r}"""


class ReceiptExtractionError(Exception):
    pass


class ReceiptOCRService:
    async def extract_from_image_bytes(self, image_bytes: bytes) -> dict:
        image_bytes = self._prepare_image_bytes(image_bytes)
        raw = await self._call_gemini_json(EXTRACTION_USER_PROMPT, RECEIPT_SCHEMA, image_bytes=image_bytes)
        if raw is None:
            raise ReceiptExtractionError("Gemini did not return valid receipt JSON")
        return self._validate_and_normalize(raw)

    async def extract_from_url(self, media_url: str, whatsapp_service) -> dict:
        image_bytes = await whatsapp_service.download_media(media_url)
        return await self.extract_from_image_bytes(image_bytes)

    async def parse_manual_details(self, text: str) -> dict:
        local = self._parse_manual_locally(text)
        if self._manual_is_complete(local):
            local["confidence"] = {"overall": 0.9}
            return self._validate_and_normalize(local)

        try:
            today = datetime.now().date()
            prompt = MANUAL_PARSE_PROMPT.format(
                today=today.isoformat(),
                yesterday=(today - timedelta(days=1)).isoformat(),
                last_week=(today - timedelta(days=7)).isoformat(),
                text=text,
            )
            model_result = await self._call_gemini_json(prompt, MANUAL_SCHEMA)
        except Exception:
            model_result = None

        merged = {**local}
        if isinstance(model_result, dict):
            for key, value in model_result.items():
                if value is not None and not merged.get(key):
                    merged[key] = value
        merged.setdefault("description", text)
        merged.setdefault("currency", "NIS")
        merged.setdefault("confidence", {"overall": 0.75 if self._manual_is_usable(merged) else 0.35})
        return self._validate_and_normalize(merged)

    async def _call_gemini_json(self, prompt: str, schema: dict, image_bytes: bytes | None = None, max_retries: int = 3) -> dict | None:
        def _run_sync() -> dict | None:
            last_error: Exception | None = None
            for model in (PRIMARY_MODEL, FALLBACK_MODEL):
                try:
                    return self._post_gemini(model, prompt, schema, image_bytes)
                except Exception as exc:
                    last_error = exc
            if last_error:
                raise last_error
            return None

        for attempt in range(max_retries):
            try:
                return await asyncio.get_event_loop().run_in_executor(None, _run_sync)
            except Exception as exc:
                error_str = str(exc).lower()
                is_rate_limit = "429" in error_str or "quota" in error_str or "rate" in error_str
                is_server_error = "500" in error_str or "503" in error_str
                if (is_rate_limit or is_server_error) and attempt < max_retries - 1:
                    wait = (2**attempt) + 1
                    logger.warning(
                        "Gemini %s, retrying in %ss (attempt %s/%s)",
                        "rate limit" if is_rate_limit else "server error",
                        wait,
                        attempt + 1,
                        max_retries,
                    )
                    await asyncio.sleep(wait)
                    continue
                raise ReceiptExtractionError(f"Gemini API error: {exc}") from exc
        return None

    def _prepare_image_bytes(self, image_bytes: bytes) -> bytes:
        try:
            image = Image.open(io.BytesIO(image_bytes))
            image.verify()
        except Exception as exc:
            raise ReceiptExtractionError("Image file is corrupted or unreadable") from exc

        if len(image_bytes) <= MAX_IMAGE_BYTES:
            return image_bytes

        original_size = len(image_bytes)
        image = Image.open(io.BytesIO(image_bytes))
        image.thumbnail((2048, 2048), Image.LANCZOS)
        if image.mode not in ("RGB", "L"):
            image = image.convert("RGB")
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=85)
        resized = buf.getvalue()
        logger.info("Image resized from %s to %s bytes", original_size, len(resized))
        return resized

    def _post_gemini(self, model: str, prompt: str, schema: dict, image_bytes: bytes | None = None) -> dict:
        parts: list[dict[str, Any]] = [{"text": prompt}]
        if image_bytes is not None:
            parts.append(
                {
                    "inline_data": {
                        "mime_type": self._detect_image_mime(image_bytes),
                        "data": base64.b64encode(image_bytes).decode("ascii"),
                    }
                }
            )
        url = GEMINI_ENDPOINT.format(model=model)
        base_body = {"contents": [{"role": "user", "parts": parts}]}
        configs = [
            {"temperature": 0.0, "topP": 0.1, "responseMimeType": "application/json", "responseJsonSchema": schema},
            {"temperature": 0.0, "topP": 0.1, "responseMimeType": "application/json", "responseSchema": schema},
            {"temperature": 0.0, "topP": 0.1, "responseMimeType": "application/json"},
        ]
        last_error: Exception | None = None
        with httpx.Client(timeout=40.0) as client:
            for generation_config in configs:
                try:
                    resp = client.post(
                        url,
                        headers={"x-goog-api-key": settings.GOOGLE_API_KEY},
                        json={**base_body, "generationConfig": generation_config},
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    text = data["candidates"][0]["content"]["parts"][0]["text"]
                    return json.loads(self._strip_fence(text))
                except Exception as exc:
                    last_error = exc
        if last_error:
            raise last_error
        raise ReceiptExtractionError("Gemini returned no response")

    def _detect_image_mime(self, image_bytes: bytes) -> str:
        try:
            image = Image.open(io.BytesIO(image_bytes))
            fmt = (image.format or "JPEG").lower()
            if fmt == "jpg":
                fmt = "jpeg"
            return f"image/{fmt}"
        except Exception:
            return "image/jpeg"

    def _strip_fence(self, raw_text: str) -> str:
        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            parts = raw_text.split("```")
            raw_text = parts[1] if len(parts) > 1 else raw_text
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        return raw_text.strip()

    def _parse_manual_locally(self, text: str) -> dict:
        data: dict[str, Any] = {
            "amount": None,
            "currency": None,
            "vendor": None,
            "expense_date": None,
            "category": None,
            "category_hint": None,
            "description": None,
            "raw_text_summary": text,
        }
        normalized = text.strip()
        lower = normalized.lower()
        labels = r"amount|total|sum|vendor|merchant|store|supplier|date|category|description|desc|details|currency"

        def field_value(label_pattern: str) -> str | None:
            match = re.search(
                rf"(?:{label_pattern})\s*[:\-]\s*(.*?)(?=\s+(?:{labels})\s*[:\-]|$)",
                normalized,
                re.IGNORECASE | re.DOTALL,
            )
            if not match:
                return None
            value = " ".join(match.group(1).split()).strip(" ,;")
            return value or None

        amount_match = re.search(r"(?:amount|total|sum|סכום)\s*[:\-]?\s*(?:₪|nis|ils|usd|eur|gbp)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", lower, re.IGNORECASE)
        if not amount_match:
            money_match = re.search(r"(?:₪|nis|ils|usd|eur|gbp)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)|([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:₪|nis|ils|usd|eur|gbp)", lower, re.IGNORECASE)
            amount_match = money_match
        if not amount_match:
            amount_match = re.search(r"\b([0-9]{2,}[0-9,]*(?:\.[0-9]{1,2})?)\b", lower)
        if amount_match:
            amount_text = next((g for g in amount_match.groups() if g), amount_match.group(0))
            data["amount"] = self._parse_amount_string(amount_text)

        if re.search(r"\b(usd|dollar|dollars|\$)\b", lower):
            data["currency"] = "USD"
        elif re.search(r"\b(eur|euro|euros|€)\b", lower):
            data["currency"] = "EUR"
        elif re.search(r"\b(gbp|pound|pounds|£)\b", lower):
            data["currency"] = "GBP"
        elif re.search(r"\b(nis|ils|shekel|shekels)\b|₪|ש\"ח|שח", lower):
            data["currency"] = "NIS"

        vendor_match = re.search(r"(?:vendor|merchant|store|supplier|ספק)\s*[:\-]\s*([^\n\r,]+)", normalized, re.IGNORECASE)
        if vendor_match:
            data["vendor"] = vendor_match.group(1).strip()
        data["vendor"] = field_value(r"vendor|merchant|store|supplier") or data["vendor"]

        date_match = re.search(r"(?:date|תאריך)\s*[:\-]\s*([0-9]{1,4}[./\-][0-9]{1,2}[./\-][0-9]{1,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})", normalized, re.IGNORECASE)
        if not date_match:
            date_match = re.search(r"\b([0-9]{4}[./\-][0-9]{1,2}[./\-][0-9]{1,2}|[0-9]{1,2}[./\-][0-9]{1,2}[./\-][0-9]{2,4}|[A-Za-z]{3,9}\s+\d{1,2},?\s+\d{4})\b", normalized)
        if date_match:
            data["expense_date"] = date_match.group(1)
        elif "yesterday" in lower:
            data["expense_date"] = (datetime.now().date() - timedelta(days=1)).isoformat()
        elif "today" in lower:
            data["expense_date"] = datetime.now().date().isoformat()

        category_match = re.search(r"(?:category|קטגוריה)\s*[:\-]\s*([^\n\r,]+)", normalized, re.IGNORECASE)
        if category_match:
            data["category"] = parse_category_reply(category_match.group(1)) or category_match.group(1).strip()
        category_field = field_value(r"category")
        if category_field:
            data["category"] = parse_category_reply(category_field) or category_field.strip()
        if not data["category"]:
            for token in re.split(r"[\n\r,|]+", normalized):
                category = parse_category_reply(token)
                if category:
                    data["category"] = category
                    break

        description_match = re.search(r"(?:description|desc|details|תיאור)\s*[:\-]\s*([^\n\r]+)", normalized, re.IGNORECASE)
        if description_match:
            data["description"] = description_match.group(1).strip()
        else:
            data["description"] = normalized
        data["description"] = field_value(r"description|desc|details") or data["description"]

        return data

    def _manual_is_complete(self, data: dict) -> bool:
        return bool(data.get("amount") is not None and data.get("currency") and data.get("expense_date") and data.get("category"))

    def _manual_is_usable(self, data: dict) -> bool:
        return bool(data.get("amount") is not None or data.get("vendor") or data.get("expense_date") or data.get("category"))

    def _validate_and_normalize(self, raw: dict) -> dict:
        if "category" in raw and "category_hint" not in raw:
            raw["category_hint"] = raw.get("category")
        if "category_hint" in raw and "category" not in raw:
            raw["category"] = raw.get("category_hint")
        for field in ["amount", "currency", "vendor", "expense_date", "category_hint", "description", "raw_text_summary"]:
            raw.setdefault(field, None)

        conf = raw.get("confidence") or {}
        if not isinstance(conf, dict):
            conf = {}
        for key, default in {"overall": 0.5, "amount": 0.5, "vendor": 0.5, "date": 0.5, "category": 0.5}.items():
            conf.setdefault(key, default)
        for key, value in list(conf.items()):
            try:
                conf[key] = max(0.0, min(1.0, float(value)))
            except (TypeError, ValueError):
                conf[key] = 0.5
        raw["confidence"] = conf

        currency_from_amount = self._currency_from_amount(raw.get("amount"))
        if raw.get("currency"):
            raw["currency"] = str(raw["currency"]).upper().replace("ILS", "NIS").strip()
        if currency_from_amount:
            raw["currency"] = currency_from_amount
        if raw.get("expense_date"):
            raw["expense_date"] = self._normalize_date(str(raw["expense_date"]))
        raw["amount"] = self._parse_amount_string(raw.get("amount"))
        if raw.get("category"):
            raw["category"] = parse_category_reply(str(raw["category"])) or raw["category"]
            raw["category_hint"] = raw["category"]
        elif raw.get("category_hint"):
            raw["category_hint"] = parse_category_reply(str(raw["category_hint"])) or raw["category_hint"]
        return raw

    def _normalize_date(self, date_str: str) -> str | None:
        from dateutil import parser as dateutil_parser

        if date_str is None:
            return None
        date_str = str(date_str).strip()
        if not date_str:
            return None
        if date_str.lower() in {"n/a", "unknown", "invalid", "none", "-", "—"}:
            return None
        parsed = None
        for fmt in ("%d/%m/%Y", "%m/%d/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%Y-%m-%d", "%d/%m/%y", "%d-%m-%y", "%d.%m.%y"):
            try:
                parsed = datetime.strptime(date_str, fmt)
                break
            except ValueError:
                pass
        if parsed is None:
            try:
                parsed = dateutil_parser.parse(date_str, dayfirst=True)
            except Exception:
                try:
                    parsed = dateutil_parser.parse(date_str, dayfirst=False)
                except Exception:
                    return None
        if parsed.year < 2000:
            return None
        if parsed.date() > date.today() + timedelta(days=365):
            return None
        return parsed.strftime("%Y-%m-%d")

    def _parse_amount_string(self, raw: Any) -> float | None:
        if raw is None:
            return None
        if isinstance(raw, (int, float)):
            value = float(raw)
            return round(value, 2) if value > 0 else None
        text = str(raw).strip()
        if not text or text.lower() in {"n/a", "unknown", "none", "-", "—"}:
            return None
        text = re.sub(r"(₪|â‚ª|\$|€|£|\bNIS\b|\bILS\b|\bUSD\b|\bEUR\b|\bGBP\b)", "", text, flags=re.IGNORECASE).strip()
        text = text.replace(" ", "")
        if text.startswith("-"):
            return None
        if re.fullmatch(r"\d{1,3}(,\d{3})+(\.\d+)?", text):
            text = text.replace(",", "")
        elif re.fullmatch(r"\d{1,3}(\.\d{3})+(,\d+)?", text):
            text = text.replace(".", "").replace(",", ".")
        elif re.fullmatch(r"\d+,\d{1,2}", text):
            text = text.replace(",", ".")
        else:
            text = text.replace(",", "")
        try:
            value = float(text)
        except ValueError:
            return None
        return round(value, 2) if value > 0 else None

    def _currency_from_amount(self, raw: Any) -> str | None:
        if raw is None:
            return None
        text = str(raw).upper()
        for token, currency in CURRENCY_SYMBOLS.items():
            if token in text:
                return currency
        return None
