import logging
import re
import uuid
import asyncio
from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.fsm.conversation import ConversationFSM
from app.models.database import AsyncSessionLocal, get_db
from app.models.employee import get_employee_by_phone
from app.models.expense import create_expense, find_potential_duplicate, list_all_expenses, update_expense
from app.models.schemas import ConversationState
from app.services.language import (
    detect_language,
    format_expense_summary,
    parse_category_reply,
    parse_confirmation_reply,
    render_category_menu,
    render_template,
)
from app.services.ocr import ReceiptExtractionError, ReceiptOCRService
from app.services.sanitizer import MAX_DESCRIPTION_LENGTH, MAX_VENDOR_LENGTH, sanitize_text
from app.services.whatsapp import WhatsAppService, safe_send
from app.tasks.expense_tasks import process_expense_task

router = APIRouter()
logger = logging.getLogger(__name__)
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'
UNSUPPORTED_TYPES = {
    "video/mp4",
    "video/3gpp",
    "video/quicktime",
    "audio/ogg",
    "audio/mpeg",
    "audio/mp4",
    "application/pdf",
    "image/gif",
    "text/vcard",
}
SUPPORTED_IMAGE_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
RESTARTABLE_STATES = {"AWAITING_CATEGORY", "AWAITING_MANUAL_DETAILS", "AWAITING_CONFIRMATION", "AWAITING_CORRECTION", "RECEIPT_RECEIVED"}
BUSY_STATES = {"PROCESSING", "UPLOADING_TO_PRIORITY", "PENDING_APPROVAL"}


def _wa_service() -> WhatsAppService:
    return WhatsAppService()


@router.post("/twilio")
async def twilio_webhook(request: Request, db: AsyncSession = Depends(get_db)):
    form_data = dict(await request.form())
    wa = _wa_service()
    signature = request.headers.get("X-Twilio-Signature", "")
    if not wa.validate_signature(str(request.url), form_data, signature):
        logger.warning("Rejected request with invalid Twilio signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        parsed = wa.parse_incoming(form_data)
    except ValueError as exc:
        logger.warning("Invalid Twilio payload rejected: %s", exc)
        raise HTTPException(status_code=400, detail="Invalid Twilio payload") from exc
    phone = parsed["from_number"]
    from app.main import redis_client

    fsm = ConversationFSM(redis_client)
    if await fsm.is_duplicate_message(phone, parsed["message_sid"]):
        logger.info("Duplicate SID %s from ...%s skipped", parsed["message_sid"], phone[-4:])
        return Response(content=EMPTY_TWIML, media_type="application/xml")
    await fsm.mark_message_processed(phone, parsed["message_sid"])
    task_coro = _handle_incoming_message_detached(
            phone=phone,
            body=parsed["body"],
            has_media=parsed["has_media"],
            media_url=parsed["media_url"],
            media_content_type=parsed["media_content_type"],
            media_type=parsed["media_type"],
            message_sid=parsed["message_sid"],
    )
    if getattr(request.app.state, "run_webhook_tasks_inline", False):
        await task_coro
    else:
        asyncio.create_task(task_coro)
    return Response(content=EMPTY_TWIML, media_type="application/xml")


async def _handle_incoming_message_detached(phone, body, has_media, media_url, media_content_type=None, media_type=None, message_sid=None):
    async with AsyncSessionLocal() as db:
        await handle_incoming_message(phone, body, has_media, media_url, message_sid, db, media_content_type=media_content_type, media_type=media_type)


async def handle_incoming_message(phone, body, has_media, media_url, message_sid, db, media_content_type=None, media_type=None):
    from app.main import redis_client

    wa = _wa_service()
    fsm = ConversationFSM(redis_client)
    lock_acquired = False
    try:
        lock_acquired = await fsm.acquire_lock(phone)
        if not lock_acquired:
            await asyncio.sleep(1.5)
            lock_acquired = await fsm.acquire_lock(phone)
            if not lock_acquired:
                await safe_send(wa, f"whatsapp:+{phone}", render_template("waiting", "en"))
                return
    except Exception as exc:
        logger.error("Redis unavailable for ...%s: %s", phone[-4:], exc)
        await safe_send(wa, f"whatsapp:+{phone}", "Our system is experiencing temporary issues. Please try again in a moment.")
        return

    try:
        try:
            state = await fsm.get_state(phone)
        except Exception as exc:
            logger.error("Redis unavailable for ...%s: %s", phone[-4:], exc)
            await safe_send(wa, f"whatsapp:+{phone}", "Our system is experiencing temporary issues. Please try again in a moment.")
            return
        if state is None:
            # If Redis TTL expires mid-flow, the prototype intentionally starts fresh.
            state = ConversationState(state="IDLE", phone=phone)
            await fsm.set_state(phone, state)
        body_clean = sanitize_text(body, max_length=2000) or ""
        if len(body_clean) > 3:
            state.lang = detect_language(body_clean)
            await fsm.set_state(phone, state)
        lang = state.lang

        media_type = (media_type or media_content_type or "").split(";", 1)[0].strip().lower() or None
        if has_media:
            is_webp_sticker = media_type == "image/webp" and not body_clean
            if media_type in UNSUPPORTED_TYPES or is_webp_sticker:
                await safe_send(wa, f"whatsapp:+{phone}", render_template("unsupported_media", lang))
                return
            if not media_url:
                logger.warning("Malformed Twilio media payload from ...%s: content_type=%s", phone[-4:], media_content_type)
                await fsm.transition(phone, "AWAITING_MANUAL_DETAILS", retries=0)
                await safe_send(wa, f"whatsapp:+{phone}", render_template("ocr_low_confidence", lang))
                return

        if not body_clean and not has_media:
            await safe_send(wa, f"whatsapp:+{phone}", render_template("welcome", lang))
            return

        approval_match = re.match(r"^(APPROVE|REJECT|אשר|דחה)\s+([A-Z0-9]+)\s*$", body_clean.strip(), re.IGNORECASE)
        if approval_match:
            await handle_manager_reply(phone, approval_match.group(1).lower(), approval_match.group(2).upper(), db, fsm, wa, lang)
            return

        current = state.state
        if has_media and current in BUSY_STATES:
            await safe_send(wa, f"whatsapp:+{phone}", render_template("waiting", lang))
            return
        if has_media and current in RESTARTABLE_STATES and current != "RECEIPT_RECEIVED":
            await safe_send(wa, f"whatsapp:+{phone}", render_template("restart_fresh", lang))
            await fsm.reset(phone)
            state = ConversationState(state="IDLE", phone=phone, lang=lang)
            await fsm.set_state(phone, state)
            current = "IDLE"
        if current == "IDLE":
            if has_media:
                await fsm.transition(phone, "RECEIPT_RECEIVED")
                await safe_send(wa, f"whatsapp:+{phone}", render_template("receipt_received", lang))
                await _process_receipt(phone, media_url, body_clean, db, fsm, wa, lang)
            elif _looks_like_expense_text(body_clean):
                await _process_manual_details(phone, body_clean, state.expense_data or {}, db, fsm, wa, lang)
            else:
                await safe_send(wa, f"whatsapp:+{phone}", render_template("welcome", lang))
        elif current == "RECEIPT_RECEIVED":
            if has_media:
                await safe_send(wa, f"whatsapp:+{phone}", render_template("receipt_received", lang))
                await _process_receipt(phone, media_url, body_clean, db, fsm, wa, lang)
            elif _looks_like_expense_text(body_clean):
                await _process_manual_details(phone, body_clean, state.expense_data or {}, db, fsm, wa, lang)
            else:
                await safe_send(wa, f"whatsapp:+{phone}", render_template("waiting", lang))
        elif current == "AWAITING_CATEGORY":
            if _has_manual_detail_fields(body_clean):
                await _process_manual_details(phone, body_clean, state.expense_data or {}, db, fsm, wa, lang)
                return
            category = parse_category_reply(body_clean)
            if category:
                expense_data = state.expense_data or {}
                expense_data["category"] = category
                await fsm.transition(phone, "AWAITING_CONFIRMATION", expense_data=expense_data, retries=0)
                await safe_send(wa, f"whatsapp:+{phone}", format_expense_summary(expense_data, lang))
            else:
                retries = state.retries + 1
                if retries >= 3:
                    await fsm.reset(phone)
                    await safe_send(wa, f"whatsapp:+{phone}", render_template("welcome", lang))
                else:
                    await fsm.transition(phone, "AWAITING_CATEGORY", retries=retries)
                    await safe_send(wa, f"whatsapp:+{phone}", render_template("invalid_category", lang, category_menu=render_category_menu(lang)))
        elif current == "AWAITING_MANUAL_DETAILS":
            await _process_manual_details(phone, body_clean, state.expense_data or {}, db, fsm, wa, lang)
        elif current == "AWAITING_CONFIRMATION":
            decision = parse_confirmation_reply(body_clean)
            if decision == "confirm":
                await _submit_expense(phone, state, db, fsm, wa)
            elif decision == "cancel":
                await fsm.reset(phone)
                await safe_send(wa, f"whatsapp:+{phone}", render_template("cancelled", lang))
            elif decision == "edit":
                await fsm.transition(phone, "AWAITING_CORRECTION")
                await safe_send(wa, f"whatsapp:+{phone}", render_template("correction_prompt", lang))
            elif _has_manual_detail_fields(body_clean):
                await _process_manual_details(phone, body_clean, state.expense_data or {}, db, fsm, wa, lang)
            else:
                await safe_send(wa, f"whatsapp:+{phone}", format_expense_summary(state.expense_data or {}, lang))
        elif current == "AWAITING_CORRECTION":
            merged = await _merge_manual_details(body_clean, state.expense_data or {})
            await fsm.transition(phone, "AWAITING_CONFIRMATION", expense_data=merged, retries=0)
            await safe_send(wa, f"whatsapp:+{phone}", format_expense_summary(merged, lang))
        elif current in ("PROCESSING", "UPLOADING_TO_PRIORITY", "PENDING_APPROVAL"):
            await safe_send(wa, f"whatsapp:+{phone}", render_template("waiting", lang))
        elif current in ("COMPLETED", "REJECTED", "PRIORITY_UPLOAD_FAILED"):
            await fsm.reset(phone)
            await safe_send(wa, f"whatsapp:+{phone}", render_template("welcome", lang))
        else:
            logger.warning("Resetting unknown FSM state %s for ...%s", current, phone[-4:])
            await fsm.reset(phone)
            await safe_send(wa, f"whatsapp:+{phone}", render_template("welcome", lang))
    except Exception as exc:
        error_id = str(uuid.uuid4())[:8]
        logger.error("Unhandled error for ...%s [ref:%s]: %s", phone[-4:], error_id, exc, exc_info=True)
        try:
            await safe_send(wa, f"whatsapp:+{phone}", render_template("error_generic", "en", error_id=error_id))
        except Exception:
            pass
    finally:
        if lock_acquired:
            try:
                await fsm.release_lock(phone)
            except Exception as exc:
                logger.warning("Failed to release lock for ...%s: %s", phone[-4:], exc)


async def _process_receipt(phone, media_url, caption, db, fsm, wa, lang):
    try:
        if not media_url:
            await fsm.transition(phone, "AWAITING_MANUAL_DETAILS", retries=0)
            await safe_send(wa, f"whatsapp:+{phone}", render_template("ocr_low_confidence", lang))
            return
        result = await ReceiptOCRService().extract_from_url(media_url, _wa_service())
        expense_data = {
            "amount": result.get("amount"),
            "currency": result.get("currency") or "NIS",
            "vendor": result.get("vendor"),
            "expense_date": result.get("expense_date"),
            "category": None,
            "description": sanitize_text(caption, MAX_DESCRIPTION_LENGTH) or result.get("description"),
            "caption": sanitize_text(caption, MAX_DESCRIPTION_LENGTH),
            "ocr_confidence": result["confidence"]["overall"],
        }
        overall_conf = result["confidence"]["overall"]
        category_conf = result["confidence"].get("category", 0.0)
        category_hint = result.get("category_hint")
        if overall_conf < 0.6:
            await fsm.transition(phone, "AWAITING_MANUAL_DETAILS", expense_data=expense_data, image_url=media_url)
            template = "not_a_receipt" if overall_conf < 0.3 and not expense_data.get("amount") else "ocr_low_confidence"
            await safe_send(wa, f"whatsapp:+{phone}", render_template(template, lang))
        elif category_hint and category_conf >= 0.85:
            expense_data["category"] = category_hint
            await fsm.transition(phone, "AWAITING_CONFIRMATION", expense_data=expense_data, image_url=media_url)
            await safe_send(wa, f"whatsapp:+{phone}", format_expense_summary(expense_data, lang))
        else:
            await fsm.transition(phone, "AWAITING_CATEGORY", expense_data=expense_data, image_url=media_url)
            await safe_send(wa, 
                f"whatsapp:+{phone}",
                render_template(
                    "ocr_success_need_category",
                    lang,
                    amount=expense_data.get("amount", "?"),
                    currency=expense_data.get("currency", "NIS"),
                    vendor=expense_data.get("vendor", "?"),
                    expense_date=expense_data.get("expense_date", "?"),
                    category_menu=render_category_menu(lang),
                ),
            )
    except ReceiptExtractionError as exc:
        logger.warning("OCR extraction failed for ...%s: %s", phone[-4:], exc)
        await fsm.transition(phone, "AWAITING_MANUAL_DETAILS", retries=0)
        await safe_send(wa, f"whatsapp:+{phone}", render_template("ocr_low_confidence", lang))
    except Exception as exc:
        logger.error("Unexpected error in _process_receipt: %s", exc, exc_info=True)
        await fsm.transition(phone, "AWAITING_MANUAL_DETAILS", retries=0)
        await safe_send(wa, f"whatsapp:+{phone}", render_template("ocr_low_confidence", lang))


def _looks_like_expense_text(text: str) -> bool:
    if not text:
        return False
    lowered = text.lower()
    return bool(
        re.search(r"\b(amount|total|vendor|merchant|date|category|description|nis|ils|usd|eur|gbp)\b|₪|ש\"ח|שח", lowered)
        or re.search(r"\b\d{1,4}[./-]\d{1,2}[./-]\d{1,4}\b", lowered)
        or parse_category_reply(text)
    )


def _has_manual_detail_fields(text: str) -> bool:
    if not text:
        return False
    return bool(
        re.search(
            r"\b(amount|total|vendor|merchant|store|supplier|date|description|desc|details|currency)\s*[:\-]",
            text,
            re.IGNORECASE,
        )
    )


async def _merge_manual_details(body_clean: str, existing: dict) -> dict:
    parsed_details = await ReceiptOCRService().parse_manual_details(body_clean)
    merged = {**existing, **{key: value for key, value in parsed_details.items() if value is not None}}
    category = merged.get("category") or merged.get("category_hint")
    if category:
        merged["category"] = category
    merged["ocr_confidence"] = parsed_details.get("confidence", {}).get("overall", merged.get("ocr_confidence", 0.75))
    return merged


async def _process_manual_details(phone, body_clean, existing, db, fsm, wa, lang):
    merged = await _merge_manual_details(body_clean, existing)
    if not merged.get("category"):
        await fsm.transition(phone, "AWAITING_CATEGORY", expense_data=merged, retries=0)
        await safe_send(wa, 
            f"whatsapp:+{phone}",
            render_template(
                "ocr_success_need_category",
                lang,
                amount=merged.get("amount", "?"),
                currency=merged.get("currency", "NIS"),
                vendor=merged.get("vendor", "?"),
                expense_date=merged.get("expense_date", "?"),
                category_menu=render_category_menu(lang),
            ),
        )
        return
    await fsm.transition(phone, "AWAITING_CONFIRMATION", expense_data=merged, retries=0)
    await safe_send(wa, f"whatsapp:+{phone}", format_expense_summary(merged, lang))


async def _submit_expense(phone, state, db, fsm, wa):
    from app.services.policy_engine import PolicyEngine

    expense_data = state.expense_data or {}
    lang = state.lang
    missing = _validate_expense_data_complete(expense_data)
    if missing:
        await fsm.transition(phone, "AWAITING_MANUAL_DETAILS", expense_data=expense_data, retries=0)
        await safe_send(wa, f"whatsapp:+{phone}", render_template("missing_required_fields", lang, missing_fields=", ".join(missing)))
        return

    amount = float(expense_data.get("amount") or 0)
    category = expense_data.get("category") or "Other"
    duplicate = await find_potential_duplicate(
        db,
        phone=phone,
        amount=amount,
        vendor=expense_data.get("vendor"),
        expense_date=expense_data.get("expense_date"),
        category=category,
    )
    if duplicate:
        await fsm.reset(phone)
        await safe_send(wa, f"whatsapp:+{phone}", render_template("duplicate_detected", lang, expense_id=duplicate.id[:8].upper()))
        return

    employee = await get_employee_by_phone(db, phone)
    try:
        gl_account, cost_center = await PolicyEngine().get_gl_account(category, db)
    except ValueError:
        gl_account, cost_center = "6199", "CC-GEN"
    expense = await create_expense(
        db,
        {
            "whatsapp_number": phone,
            "employee_name": sanitize_text(employee.name if employee else None, MAX_VENDOR_LENGTH),
            "employee_id": employee.employee_id if employee else None,
            "amount": amount,
            "currency": expense_data.get("currency") or "NIS",
            "amount_nis": amount,
            "vendor": sanitize_text(expense_data.get("vendor"), MAX_VENDOR_LENGTH),
            "expense_date": expense_data.get("expense_date") or datetime.now().date().isoformat(),
            "category": category,
            "description": sanitize_text(expense_data.get("description"), MAX_DESCRIPTION_LENGTH),
            "gl_account": gl_account,
            "cost_center": cost_center,
            "receipt_image_url": state.image_url,
            "ocr_confidence": expense_data.get("ocr_confidence", 0.0),
            "language": lang,
        },
    )
    await fsm.transition(phone, "PROCESSING", pending_expense_id=expense.id)
    await safe_send(wa, f"whatsapp:+{phone}", render_template("processing", lang))
    process_expense_task.delay(expense.id, phone)
    logger.info("Expense %s created and queued for ...%s", expense.id[:8], phone[-4:])


def _validate_expense_data_complete(expense_data: dict) -> list[str]:
    errors = []
    try:
        amount = float(expense_data.get("amount") or 0)
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        errors.append("amount")
    if not expense_data.get("category"):
        errors.append("category")
    if not expense_data.get("expense_date"):
        errors.append("expense_date")
    return errors


async def handle_manager_reply(phone, action, expense_id_prefix, db, fsm, wa, lang):
    from app.tasks.expense_tasks import upload_to_priority_task

    all_expenses = await list_all_expenses(db, limit=500)
    expense = next((e for e in all_expenses if e.id[:8].upper() == expense_id_prefix.upper()), None)
    if not expense:
        await safe_send(wa, f"whatsapp:+{phone}", f"Expense {expense_id_prefix} not found. Please check the reference number.")
        return
    if expense.approval_status != "PENDING":
        status = "approved" if expense.approval_status == "APPROVED" else "rejected" if expense.approval_status == "REJECTED" else expense.approval_status.lower()
        await safe_send(wa, f"whatsapp:+{phone}", f"Expense {expense_id_prefix} is already {status}.")
        return
    employee = await get_employee_by_phone(db, expense.whatsapp_number)
    if employee and employee.manager_phone != phone:
        await safe_send(wa, f"whatsapp:+{phone}", "You are not authorized to approve this expense.")
        logger.warning("Unauthorized approval attempt for %s from ...%s", expense_id_prefix, phone[-4:])
        return
    emp_phone = expense.whatsapp_number
    emp_lang = expense.language or "en"
    if action in ("approve", "אשר"):
        await update_expense(db, expense.id, approval_status="APPROVED", approver_phone=phone, approved_at=datetime.utcnow().isoformat())
        upload_to_priority_task.delay(expense.id, emp_phone)
        await safe_send(wa, f"whatsapp:+{phone}", f"Expense {expense_id_prefix} approved and sent to Priority.")
        await safe_send(wa, 
            f"whatsapp:+{emp_phone}",
            render_template("approved_by_manager", emp_lang, expense_id=expense_id_prefix, amount=expense.amount, currency=expense.currency),
        )
    elif action in ("reject", "דחה"):
        await update_expense(db, expense.id, approval_status="REJECTED")
        await fsm.transition(emp_phone, "REJECTED")
        await safe_send(wa, f"whatsapp:+{phone}", f"Expense {expense_id_prefix} rejected. Employee notified.")
        await safe_send(wa, 
            f"whatsapp:+{emp_phone}",
            render_template("rejected_by_manager", emp_lang, amount=expense.amount, currency=expense.currency, expense_id=expense_id_prefix, reason="Manager decision"),
        )

