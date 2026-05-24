import asyncio
import base64
import io
import json
import re
from datetime import datetime, timedelta
from typing import Any

import httpx
from PIL import Image

from app.config import settings
from app.services.language import parse_category_reply

PRIMARY_MODEL = "gemini-3-flash-preview"
FALLBACK_MODEL = "gemini-3.1-flash-lite"
GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

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

    async def _call_gemini_json(self, prompt: str, schema: dict, image_bytes: bytes | None = None) -> dict | None:
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

        return await asyncio.get_event_loop().run_in_executor(None, _run_sync)

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
                    resp = client.post(url, params={"key": settings.GOOGLE_API_KEY}, json={**base_body, "generationConfig": generation_config})
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

        amount_match = re.search(r"(?:amount|total|sum|סכום)\s*[:\-]?\s*(?:₪|nis|ils|usd|eur|gbp)?\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)", lower, re.IGNORECASE)
        if not amount_match:
            money_match = re.search(r"(?:₪|nis|ils|usd|eur|gbp)\s*([0-9][0-9,]*(?:\.[0-9]{1,2})?)|([0-9][0-9,]*(?:\.[0-9]{1,2})?)\s*(?:₪|nis|ils|usd|eur|gbp)", lower, re.IGNORECASE)
            amount_match = money_match
        if not amount_match:
            amount_match = re.search(r"\b([0-9]{2,}[0-9,]*(?:\.[0-9]{1,2})?)\b", lower)
        if amount_match:
            amount_text = next((g for g in amount_match.groups() if g), amount_match.group(0))
            try:
                data["amount"] = float(amount_text.replace(",", ""))
            except ValueError:
                pass

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

        if raw.get("currency"):
            raw["currency"] = str(raw["currency"]).upper().replace("ILS", "NIS").strip()
        if raw.get("expense_date"):
            raw["expense_date"] = self._normalize_date(str(raw["expense_date"]))
        if raw.get("amount") is not None:
            try:
                raw["amount"] = round(float(str(raw["amount"]).replace(",", "")), 2)
            except (TypeError, ValueError):
                raw["amount"] = None
        if raw.get("category"):
            raw["category"] = parse_category_reply(str(raw["category"])) or raw["category"]
            raw["category_hint"] = raw["category"]
        elif raw.get("category_hint"):
            raw["category_hint"] = parse_category_reply(str(raw["category_hint"])) or raw["category_hint"]
        return raw

    def _normalize_date(self, date_str: str) -> str | None:
        from dateutil import parser as dateutil_parser

        date_str = date_str.strip()
        if not date_str:
            return None
        if len(date_str) == 10 and date_str[4] == "-":
            return date_str
        for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%Y/%m/%d", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(date_str, fmt)
                return parsed.strftime("%Y-%m-%d")
            except ValueError:
                pass
        try:
            parsed = dateutil_parser.parse(date_str, dayfirst=True)
            return parsed.strftime("%Y-%m-%d")
        except Exception:
            return None
