import asyncio
import io
import json
from datetime import datetime, timedelta

import google.generativeai as genai
from google.generativeai.types import HarmBlockThreshold, HarmCategory
from PIL import Image

from app.config import settings

genai.configure(api_key=settings.GOOGLE_API_KEY)

VISION_MODEL_NAME = "gemini-1.5-flash"
TEXT_MODEL_NAME = "gemini-1.5-flash"

VISION_MODEL = genai.GenerativeModel(
    model_name=VISION_MODEL_NAME,
    safety_settings={
        HarmCategory.HARM_CATEGORY_HARASSMENT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_HATE_SPEECH: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_SEXUALLY_EXPLICIT: HarmBlockThreshold.BLOCK_NONE,
        HarmCategory.HARM_CATEGORY_DANGEROUS_CONTENT: HarmBlockThreshold.BLOCK_NONE,
    },
)
TEXT_MODEL = genai.GenerativeModel(model_name=TEXT_MODEL_NAME)

EXTRACTION_USER_PROMPT = """Analyze this receipt image and extract expense data.
Return ONLY a JSON object with keys: amount, currency, vendor, expense_date,
category_hint, description, confidence, raw_text_summary."""

MANUAL_PARSE_PROMPT = """Extract expense details from this free-text description written by an employee.
Today's date is {today}. "yesterday" = {yesterday}, "last week" = {last_week}.
Text: "{text}"
Return ONLY valid JSON with amount, currency, vendor, expense_date, category, description."""


class ReceiptExtractionError(Exception):
    pass


class ReceiptOCRService:
    async def extract_from_image_bytes(self, image_bytes: bytes) -> dict:
        def _run_sync() -> str:
            image = Image.open(io.BytesIO(image_bytes))
            response = VISION_MODEL.generate_content([EXTRACTION_USER_PROMPT, image])
            return response.text

        raw_text = await asyncio.get_event_loop().run_in_executor(None, _run_sync)
        raw_text = self._strip_fence(raw_text)
        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError as exc:
            raise ReceiptExtractionError(f"Gemini returned invalid JSON: {exc}. Raw: {raw_text[:200]}") from exc
        return self._validate_and_normalize(result)

    async def extract_from_url(self, media_url: str, whatsapp_service) -> dict:
        image_bytes = await whatsapp_service.download_media(media_url)
        return await self.extract_from_image_bytes(image_bytes)

    async def parse_manual_details(self, text: str) -> dict:
        today = datetime.now().date()
        prompt = MANUAL_PARSE_PROMPT.format(
            today=today.isoformat(),
            yesterday=(today - timedelta(days=1)).isoformat(),
            last_week=(today - timedelta(days=7)).isoformat(),
            text=text,
        )

        def _run_sync() -> str:
            response = TEXT_MODEL.generate_content(prompt)
            return response.text

        raw_text = await asyncio.get_event_loop().run_in_executor(None, _run_sync)
        raw_text = self._strip_fence(raw_text)
        try:
            result = json.loads(raw_text)
        except json.JSONDecodeError:
            return {
                "amount": None,
                "currency": "NIS",
                "vendor": None,
                "expense_date": None,
                "category": None,
                "category_hint": None,
                "description": text,
                "raw_text_summary": text,
                "confidence": {"overall": 0.3},
            }
        result["confidence"] = {"overall": 0.8}
        return self._validate_and_normalize(result)

    def _strip_fence(self, raw_text: str) -> str:
        raw_text = raw_text.strip()
        if raw_text.startswith("```"):
            parts = raw_text.split("```")
            raw_text = parts[1] if len(parts) > 1 else raw_text
            if raw_text.startswith("json"):
                raw_text = raw_text[4:]
        return raw_text.strip()

    def _validate_and_normalize(self, raw: dict) -> dict:
        if "category" in raw and "category_hint" not in raw:
            raw["category_hint"] = raw.get("category")
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
            raw["currency"] = str(raw["currency"]).upper().strip()
        if raw.get("expense_date"):
            raw["expense_date"] = self._normalize_date(str(raw["expense_date"]))
        if raw.get("amount") is not None:
            try:
                raw["amount"] = round(float(raw["amount"]), 2)
            except (TypeError, ValueError):
                raw["amount"] = None
        return raw

    def _normalize_date(self, date_str: str) -> str | None:
        from dateutil import parser as dateutil_parser

        date_str = date_str.strip()
        if not date_str:
            return None
        if len(date_str) == 10 and date_str[4] == "-":
            return date_str
        try:
            parsed = datetime.strptime(date_str, "%d/%m/%Y")
            return parsed.strftime("%Y-%m-%d")
        except ValueError:
            pass
        try:
            parsed = dateutil_parser.parse(date_str, dayfirst=True)
            return parsed.strftime("%Y-%m-%d")
        except Exception:
            return None
