import logging
import re

logger = logging.getLogger(__name__)

TEMPLATES = {
    "welcome": {
        "en": "Hi! I'm the Arad Group expense assistant.\n\nTo submit an expense, send me a photo of your receipt.",
        "he": "שלום! אני עוזר ההוצאות של קבוצת ערד.\n\nכדי להגיש הוצאה, שלח תמונה של הקבלה.",
    },
    "receipt_received": {"en": "Got your receipt! Analyzing it now...", "he": "קיבלתי את הקבלה! מנתח עכשיו..."},
    "ocr_low_confidence": {
        "en": "I could not read the receipt clearly. Please type the amount, vendor, date, category, and description.",
        "he": "לא הצלחתי לקרוא את הקבלה בבירור. אנא הקלד סכום, ספק, תאריך, קטגוריה ותיאור.",
    },
    "ocr_success_need_category": {
        "en": "I extracted:\n\nAmount: *{amount} {currency}*\nVendor: *{vendor}*\nDate: *{expense_date}*\n\nWhat category is this expense?\n\n{category_menu}",
        "he": "חילצתי:\n\nסכום: *{amount} {currency}*\nספק: *{vendor}*\nתאריך: *{expense_date}*\n\nמה קטגוריית ההוצאה?\n\n{category_menu}",
    },
    "category_menu": {
        "en": "1 Meals\n2 Travel\n3 Accommodation\n4 Entertainment\n5 Office Supplies\n6 Software\n7 Conference\n8 Other\n\nReply with the number or category name.",
        "he": "1 ארוחות\n2 נסיעות\n3 לינה\n4 בידור\n5 ציוד משרדי\n6 תוכנה\n7 כנסים\n8 אחר\n\nענה עם מספר או שם קטגוריה.",
    },
    "confirmation_request": {
        "en": "Please confirm the expense:\n\nAmount: *{amount} {currency}*\nVendor: *{vendor}*\nDate: *{expense_date}*\nCategory: *{category}*\nDescription: *{description}*\n\nReply:\n1 Confirm\n2 Cancel\n3 Edit",
        "he": "אנא אשר את פרטי ההוצאה:\n\nסכום: *{amount} {currency}*\nספק: *{vendor}*\nתאריך: *{expense_date}*\nקטגוריה: *{category}*\nתיאור: *{description}*\n\nענה:\n1 אישור\n2 ביטול\n3 עריכה",
    },
    "processing": {"en": "Processing your expense... This usually takes under a minute.", "he": "מעבד את ההוצאה שלך... זה בדרך כלל לוקח פחות מדקה."},
    "sent_for_approval": {
        "en": "Your expense of *{amount} {currency}* has been sent to your manager for approval.\n\nReference: *{expense_id}*",
        "he": "ההוצאה שלך בסך *{amount} {currency}* נשלחה למנהל לאישור.\n\nאסמכתא: *{expense_id}*",
    },
    "manager_approval_request": {
        "en": "Expense Approval Required\n\nEmployee: {employee_name} ({phone})\nAmount: *{amount} {currency}*\nVendor: {vendor}\nCategory: {category}\nDate: {expense_date}\nDescription: {description}\nID: `{expense_id}`\n\nReply:\nAPPROVE {expense_id}\nREJECT {expense_id}",
        "he": "נדרש אישור הוצאה\n\nעובד: {employee_name} ({phone})\nסכום: *{amount} {currency}*\nספק: {vendor}\nקטגוריה: {category}\nתאריך: {expense_date}\nתיאור: {description}\nמזהה: `{expense_id}`\n\nענה:\nאשר {expense_id}\nדחה {expense_id}",
    },
    "approved_by_manager": {"en": "Your expense *{expense_id}* for *{amount} {currency}* was approved.", "he": "ההוצאה *{expense_id}* בסך *{amount} {currency}* אושרה."},
    "rejected_by_manager": {"en": "Your expense *{expense_id}* for *{amount} {currency}* was rejected. Reason: {reason}", "he": "ההוצאה *{expense_id}* בסך *{amount} {currency}* נדחתה. סיבה: {reason}"},
    "policy_rejected": {"en": "Your expense cannot be processed:\n\nReason: {reason}", "he": "לא ניתן לעבד את ההוצאה שלך:\n\nסיבה: {reason}"},
    "priority_success": {
        "en": "Expense successfully submitted to Priority ERP!\n\nReference: *{expense_id}*\nPriority Document: *{priority_doc_id}*\nAmount: *{amount} {currency}*",
        "he": "ההוצאה הוגשה בהצלחה לפריוריטי!\n\nאסמכתא: *{expense_id}*\nמסמך פריוריטי: *{priority_doc_id}*\nסכום: *{amount} {currency}*",
    },
    "priority_failed": {"en": "Your expense was approved but upload to Priority failed. Reference: *{expense_id}*", "he": "ההוצאה אושרה אך העלאה לפריוריטי נכשלה. אסמכתא: *{expense_id}*"},
    "correction_prompt": {"en": "What would you like to correct? Type the updated details.", "he": "מה תרצה לתקן? הקלד את הפרטים המעודכנים."},
    "invalid_category": {"en": "I didn't recognize that. Please choose:\n\n{category_menu}", "he": "לא זיהיתי את הקטגוריה. בחר מתוך:\n\n{category_menu}"},
    "error_generic": {"en": "Something went wrong on our end. Please try again.\n\nError reference: {error_id}", "he": "משהו השתבש מצדנו. אנא נסה שוב.\n\nמזהה שגיאה: {error_id}"},
    "cancelled": {"en": "Expense cancelled. Send a receipt photo whenever you're ready.", "he": "ההוצאה בוטלה. שלח תמונת קבלה כשתהיה מוכן."},
    "waiting": {"en": "Your expense is still being processed. Please wait a moment.", "he": "ההוצאה שלך עדיין בעיבוד. אנא המתן רגע."},
}

CATEGORY_ALIASES = {
    "1": "Meals", "2": "Travel", "3": "Accommodation", "4": "Entertainment",
    "5": "Office Supplies", "6": "Software", "7": "Conference", "8": "Other",
    "meals": "Meals", "meal": "Meals", "food": "Meals", "restaurant": "Meals", "lunch": "Meals", "dinner": "Meals",
    "travel": "Travel", "taxi": "Travel", "bus": "Travel", "transport": "Travel", "uber": "Travel", "gett": "Travel",
    "accommodation": "Accommodation", "hotel": "Accommodation", "airbnb": "Accommodation",
    "entertainment": "Entertainment", "event": "Entertainment", "client": "Entertainment",
    "office supplies": "Office Supplies", "office": "Office Supplies", "supplies": "Office Supplies", "stationery": "Office Supplies",
    "software": "Software", "subscription": "Software", "saas": "Software",
    "conference": "Conference", "training": "Conference", "course": "Conference",
    "other": "Other", "misc": "Other", "miscellaneous": "Other",
    "ארוחות": "Meals", "ארוחה": "Meals", "מסעדה": "Meals",
    "נסיעות": "Travel", "נסיעה": "Travel", "מונית": "Travel", "אובר": "Travel",
    "לינה": "Accommodation", "מלון": "Accommodation",
    "בידור": "Entertainment", "אירוח": "Entertainment",
    "ציוד משרדי": "Office Supplies", "ציוד": "Office Supplies",
    "תוכנה": "Software", "מנוי": "Software",
    "כנס": "Conference", "הדרכה": "Conference", "קורס": "Conference",
    "אחר": "Other",
}

CANONICAL_CATEGORIES = ["Meals", "Travel", "Accommodation", "Entertainment", "Office Supplies", "Software", "Conference", "Other"]


def detect_language(text: str) -> str:
    non_space = [char for char in text if char.strip()]
    if non_space:
        hebrew = sum(1 for char in non_space if "\u0590" <= char <= "\u05FF")
        if hebrew / len(non_space) > 0.10:
            return "he"
    try:
        from langdetect import detect

        if detect(text) == "he":
            return "he"
    except Exception:
        pass
    return "en"


def render_template(key: str, lang: str, **kwargs) -> str:
    template_set = TEMPLATES.get(key)
    if not template_set:
        logger.warning("Template key not found: %s", key)
        return ""
    template = template_set.get(lang) or template_set.get("en", "")
    try:
        return template.format(**kwargs)
    except KeyError as exc:
        logger.warning("Template %s/%s missing placeholder: %s", key, lang, exc)
        return template


def render_category_menu(lang: str) -> str:
    return render_template("category_menu", lang)


def parse_category_reply(text: str) -> str | None:
    cleaned = re.sub(r"[.!?,؟]", "", text.strip().lower()).strip()
    return CATEGORY_ALIASES.get(cleaned)


def parse_confirmation_reply(text: str) -> str | None:
    cleaned = re.sub(r"[.!?,؟]", "", text.strip().lower()).strip()
    if cleaned in {"1", "yes", "confirm", "ok", "okay", "sure", "כן", "אשר", "אישור", "יש"}:
        return "confirm"
    if cleaned in {"2", "no", "cancel", "nope", "לא", "ביטול", "בטל"}:
        return "cancel"
    if cleaned in {"3", "edit", "change", "fix", "ערוך", "עריכה", "שנה", "תיקון"}:
        return "edit"
    return None


def format_expense_summary(expense_data: dict, lang: str) -> str:
    amount = expense_data.get("amount")
    amount_str = f"{amount:.2f}" if amount is not None else "?"
    expense_date = expense_data.get("expense_date") or "?"
    if lang == "he" and expense_date != "?" and len(expense_date) == 10:
        parts = expense_date.split("-")
        if len(parts) == 3:
            expense_date = f"{parts[2]}/{parts[1]}/{parts[0]}"
    return render_template(
        "confirmation_request",
        lang,
        amount=amount_str,
        currency=expense_data.get("currency") or "NIS",
        vendor=expense_data.get("vendor") or "N/A",
        expense_date=expense_date,
        category=expense_data.get("category") or "?",
        description=expense_data.get("description") or "-",
    )
