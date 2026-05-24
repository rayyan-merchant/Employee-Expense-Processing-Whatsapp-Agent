import logging
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.fsm.conversation import ConversationFSM
from app.models.database import get_db
from app.models.employee import get_employee_by_phone
from app.models.expense import create_expense, list_all_expenses, update_expense
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
from app.services.whatsapp import WhatsAppService
from app.tasks.expense_tasks import process_expense_task

router = APIRouter()
logger = logging.getLogger(__name__)
EMPTY_TWIML = '<?xml version="1.0" encoding="UTF-8"?><Response></Response>'


def _wa_service() -> WhatsAppService:
    return WhatsAppService()


@router.post("/twilio")
async def twilio_webhook(request: Request, background_tasks: BackgroundTasks, db: AsyncSession = Depends(get_db)):
    form_data = dict(await request.form())
    wa = _wa_service()
    signature = request.headers.get("X-Twilio-Signature", "")
    if not wa.validate_signature(str(request.url), form_data, signature):
        logger.warning("Rejected request with invalid Twilio signature")
        raise HTTPException(status_code=403, detail="Invalid signature")

    parsed = wa.parse_incoming(form_data)
    phone = parsed["from_number"]
    from app.main import redis_client

    fsm = ConversationFSM(redis_client)
    if await fsm.is_duplicate_message(phone, parsed["message_sid"]):
        logger.info("Duplicate SID %s from ...%s skipped", parsed["message_sid"], phone[-4:])
        return Response(content=EMPTY_TWIML, media_type="application/xml")
    await fsm.mark_message_processed(phone, parsed["message_sid"])
    background_tasks.add_task(
        handle_incoming_message,
        phone=phone,
        body=parsed["body"],
        has_media=parsed["has_media"],
        media_url=parsed["media_url"],
        message_sid=parsed["message_sid"],
        db=db,
    )
    return Response(content=EMPTY_TWIML, media_type="application/xml")


async def handle_incoming_message(phone, body, has_media, media_url, message_sid, db):
    from app.main import redis_client

    wa = _wa_service()
    fsm = ConversationFSM(redis_client)
    try:
        state = await fsm.get_state(phone)
        if state is None:
            state = ConversationState(state="IDLE", phone=phone)
            await fsm.set_state(phone, state)
        body_clean = (body or "").strip()
        if len(body_clean) > 3:
            state.lang = detect_language(body_clean)
            await fsm.set_state(phone, state)
        lang = state.lang

        approval_match = re.match(r"^(APPROVE|REJECT|אשר|דחה)\s+([A-Z0-9]+)$", body_clean, re.IGNORECASE)
        if approval_match:
            await handle_manager_reply(phone, approval_match.group(1).lower(), approval_match.group(2).upper(), db, fsm, wa, lang)
            return

        current = state.state
        if current == "IDLE":
            if has_media:
                await fsm.transition(phone, "RECEIPT_RECEIVED")
                await wa.send_message(f"whatsapp:+{phone}", render_template("receipt_received", lang))
                await _process_receipt(phone, media_url, body_clean, db, fsm, wa, lang)
            else:
                await wa.send_message(f"whatsapp:+{phone}", render_template("welcome", lang))
        elif current == "AWAITING_CATEGORY":
            category = parse_category_reply(body_clean)
            if category:
                expense_data = state.expense_data or {}
                expense_data["category"] = category
                await fsm.transition(phone, "AWAITING_CONFIRMATION", expense_data=expense_data, retries=0)
                await wa.send_message(f"whatsapp:+{phone}", format_expense_summary(expense_data, lang))
            else:
                retries = state.retries + 1
                if retries >= 3:
                    await fsm.reset(phone)
                    await wa.send_message(f"whatsapp:+{phone}", render_template("welcome", lang))
                else:
                    await fsm.transition(phone, "AWAITING_CATEGORY", retries=retries)
                    await wa.send_message(f"whatsapp:+{phone}", render_template("invalid_category", lang, category_menu=render_category_menu(lang)))
        elif current == "AWAITING_MANUAL_DETAILS":
            parsed_details = await ReceiptOCRService().parse_manual_details(body_clean)
            existing = state.expense_data or {}
            merged = {**existing, **{k: v for k, v in parsed_details.items() if v is not None}}
            merged["ocr_confidence"] = 0.8
            category = merged.get("category") or merged.get("category_hint")
            if category:
                merged["category"] = category
                await fsm.transition(phone, "AWAITING_CONFIRMATION", expense_data=merged, retries=0)
                await wa.send_message(f"whatsapp:+{phone}", format_expense_summary(merged, lang))
            else:
                await fsm.transition(phone, "AWAITING_CATEGORY", expense_data=merged, retries=0)
                await wa.send_message(
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
        elif current == "AWAITING_CONFIRMATION":
            decision = parse_confirmation_reply(body_clean)
            if decision == "confirm":
                await _submit_expense(phone, state, db, fsm, wa)
            elif decision == "cancel":
                await fsm.reset(phone)
                await wa.send_message(f"whatsapp:+{phone}", render_template("cancelled", lang))
            elif decision == "edit":
                await fsm.transition(phone, "AWAITING_CORRECTION")
                await wa.send_message(f"whatsapp:+{phone}", render_template("correction_prompt", lang))
            else:
                await wa.send_message(f"whatsapp:+{phone}", format_expense_summary(state.expense_data or {}, lang))
        elif current == "AWAITING_CORRECTION":
            corrected = await ReceiptOCRService().parse_manual_details(body_clean)
            existing = state.expense_data or {}
            merged = {**existing, **{k: v for k, v in corrected.items() if v is not None}}
            if merged.get("category_hint") and not merged.get("category"):
                merged["category"] = merged["category_hint"]
            await fsm.transition(phone, "AWAITING_CONFIRMATION", expense_data=merged, retries=0)
            await wa.send_message(f"whatsapp:+{phone}", format_expense_summary(merged, lang))
        elif current in ("PROCESSING", "UPLOADING_TO_PRIORITY", "PENDING_APPROVAL"):
            await wa.send_message(f"whatsapp:+{phone}", render_template("waiting", lang))
        elif current in ("COMPLETED", "REJECTED", "PRIORITY_UPLOAD_FAILED"):
            await fsm.reset(phone)
            await wa.send_message(f"whatsapp:+{phone}", render_template("welcome", lang))
    except Exception as exc:
        error_id = str(uuid.uuid4())[:8]
        logger.error("Unhandled error for ...%s [ref:%s]: %s", phone[-4:], error_id, exc, exc_info=True)
        try:
            await _wa_service().send_message(f"whatsapp:+{phone}", render_template("error_generic", "en", error_id=error_id))
        except Exception:
            pass


async def _process_receipt(phone, media_url, caption, db, fsm, wa, lang):
    try:
        if not media_url:
            await fsm.transition(phone, "AWAITING_MANUAL_DETAILS", retries=0)
            await wa.send_message(f"whatsapp:+{phone}", render_template("ocr_low_confidence", lang))
            return
        result = await ReceiptOCRService().extract_from_url(media_url, _wa_service())
        expense_data = {
            "amount": result.get("amount"),
            "currency": result.get("currency") or "NIS",
            "vendor": result.get("vendor"),
            "expense_date": result.get("expense_date"),
            "category": None,
            "description": result.get("description"),
            "ocr_confidence": result["confidence"]["overall"],
        }
        overall_conf = result["confidence"]["overall"]
        category_conf = result["confidence"].get("category", 0.0)
        category_hint = result.get("category_hint")
        if overall_conf < 0.6:
            await fsm.transition(phone, "AWAITING_MANUAL_DETAILS", expense_data=expense_data, image_url=media_url)
            await wa.send_message(f"whatsapp:+{phone}", render_template("ocr_low_confidence", lang))
        elif category_hint and category_conf >= 0.85:
            expense_data["category"] = category_hint
            await fsm.transition(phone, "AWAITING_CONFIRMATION", expense_data=expense_data, image_url=media_url)
            await wa.send_message(f"whatsapp:+{phone}", format_expense_summary(expense_data, lang))
        else:
            await fsm.transition(phone, "AWAITING_CATEGORY", expense_data=expense_data, image_url=media_url)
            await wa.send_message(
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
        await wa.send_message(f"whatsapp:+{phone}", render_template("ocr_low_confidence", lang))
    except Exception as exc:
        logger.error("Unexpected error in _process_receipt: %s", exc, exc_info=True)
        await fsm.transition(phone, "AWAITING_MANUAL_DETAILS", retries=0)
        await wa.send_message(f"whatsapp:+{phone}", render_template("ocr_low_confidence", lang))


async def _submit_expense(phone, state, db, fsm, wa):
    from app.services.policy_engine import PolicyEngine

    expense_data = state.expense_data or {}
    lang = state.lang
    employee = await get_employee_by_phone(db, phone)
    try:
        gl_account, cost_center = await PolicyEngine().get_gl_account(expense_data.get("category", "Other"), db)
    except ValueError:
        gl_account, cost_center = "6199", "CC-GEN"
    expense = await create_expense(
        db,
        {
            "whatsapp_number": phone,
            "employee_name": employee.name if employee else None,
            "employee_id": employee.employee_id if employee else None,
            "amount": expense_data.get("amount") or 0,
            "currency": expense_data.get("currency") or "NIS",
            "amount_nis": expense_data.get("amount") or 0,
            "vendor": expense_data.get("vendor"),
            "expense_date": expense_data.get("expense_date") or datetime.now().date().isoformat(),
            "category": expense_data.get("category") or "Other",
            "description": expense_data.get("description"),
            "gl_account": gl_account,
            "cost_center": cost_center,
            "receipt_image_url": state.image_url,
            "ocr_confidence": expense_data.get("ocr_confidence", 0.0),
            "language": lang,
        },
    )
    await fsm.transition(phone, "PROCESSING", pending_expense_id=expense.id)
    await wa.send_message(f"whatsapp:+{phone}", render_template("processing", lang))
    process_expense_task.delay(expense.id, phone)
    logger.info("Expense %s created and queued for ...%s", expense.id[:8], phone[-4:])


async def handle_manager_reply(phone, action, expense_id_prefix, db, fsm, wa, lang):
    from app.tasks.expense_tasks import upload_to_priority_task

    all_expenses = await list_all_expenses(db, limit=500)
    expense = next((e for e in all_expenses if e.id[:8].upper() == expense_id_prefix.upper()), None)
    if not expense:
        await wa.send_message(f"whatsapp:+{phone}", f"Expense {expense_id_prefix} not found.")
        return
    if expense.approval_status != "PENDING":
        await wa.send_message(f"whatsapp:+{phone}", f"Expense {expense_id_prefix} is already {expense.approval_status}.")
        return
    emp_phone = expense.whatsapp_number
    emp_lang = expense.language or "en"
    if action in ("approve", "אשר"):
        await update_expense(db, expense.id, approval_status="APPROVED", approver_phone=phone, approved_at=datetime.utcnow().isoformat())
        upload_to_priority_task.delay(expense.id, emp_phone)
        await wa.send_message(f"whatsapp:+{phone}", f"Expense {expense_id_prefix} approved and sent to Priority.")
        await wa.send_message(
            f"whatsapp:+{emp_phone}",
            render_template("approved_by_manager", emp_lang, expense_id=expense_id_prefix, amount=expense.amount, currency=expense.currency),
        )
    elif action in ("reject", "דחה"):
        await update_expense(db, expense.id, approval_status="REJECTED")
        await fsm.transition(emp_phone, "REJECTED")
        await wa.send_message(f"whatsapp:+{phone}", f"Expense {expense_id_prefix} rejected. Employee notified.")
        await wa.send_message(
            f"whatsapp:+{emp_phone}",
            render_template("rejected_by_manager", emp_lang, amount=expense.amount, currency=expense.currency, expense_id=expense_id_prefix, reason="Manager decision"),
        )
