"""Läser kvitton (foto eller PDF) och plockar ut belopp, datum, butik och kategori.

Bygger på account_invoice_ocr_ai/lib/invoice_ocr.py: samma tesseract, samma
AI-leverantör och nycklar (staik som standard). Skillnaden är att ett kvitto är ett
foto, inte ett PDF-dokument, och att det som ska fyllas i är ett utlägg: totalbelopp
inkl. moms, kvittodatum, butik och en utläggskategori ur föreningens lista.

Fristående användning (utan Odoo):
    data = extract_receipt_data(raw_bytes, "image/jpeg", categories=[("MASKIN", "Maskiner och redskap"), ...])

Leverantör, nycklar och gränser kommer ur en per-körning-config (invoice_ocr.default_config()
+ Odoo-inställningarna, se account.move._invoice_ocr_config); utan config gäller
fakturabibliotekets miljövariabel-defaults. Modul-globalerna ändras aldrig.
"""

import contextlib
import io
import logging
import re
from datetime import date

try:  # i Odoo: fakturamodulens bibliotek
    from odoo.addons.account_invoice_ocr_ai.lib import invoice_ocr as inv
except ImportError:  # fristående test: båda filerna på sys.path
    import invoice_ocr as inv

logger = logging.getLogger(__name__)

try:
    import pytesseract
    from PIL import Image, ImageOps
    HAS_TESSERACT = True
except ImportError:  # pragma: no cover
    HAS_TESSERACT = False

IMAGE_TYPES = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/heic", "image/tiff", "image/bmp")


# ── Text ──────────────────────────────────────────────────────────────────────

def _prepare_image(raw):
    img = Image.open(io.BytesIO(raw))
    img = ImageOps.exif_transpose(img)  # mobilfoton ligger ofta roterade i EXIF
    img = img.convert("L")
    # tesseract vill ha ~300 dpi-motsvarande text; små/nedskalade foton skalas upp
    w, h = img.size
    if max(w, h) < 2000:
        f = 2000 / max(w, h)
        img = img.resize((int(w * f), int(h * f)), Image.LANCZOS)
    return ImageOps.autocontrast(img)


def extract_text(raw, mimetype=None, filename=None, config=None):
    """Text ur kvittot. PDF går via fakturamodulens extract_text (pdfplumber + tesseract)."""
    mt = (mimetype or "").lower()
    name = (filename or "").lower()
    if mt == "application/pdf" or name.endswith(".pdf") or raw[:5] == b"%PDF-":
        return inv.extract_text(raw, config)
    if not HAS_TESSERACT:
        return ""
    img = _prepare_image(raw)
    # psm 4 = en kolumn med rader av varierande storlek, det är vad ett kvitto är
    text = pytesseract.image_to_string(img, lang="swe+eng", config="--psm 4")
    if len(text.strip()) < 40:
        alt = pytesseract.image_to_string(img, lang="swe+eng", config="--psm 6")
        if len(alt.strip()) > len(text.strip()):
            text = alt
    return text


class ReceiptReadError(Exception):
    """The attachment's text could not be read; the message says why, for the user."""


def _describe_read_error(e):
    if HAS_TESSERACT and isinstance(e, getattr(pytesseract, "TesseractNotFoundError", ())):
        return "tesseract is not installed on the server"
    if HAS_TESSERACT and isinstance(e, getattr(pytesseract, "TesseractError", ())):
        return f"tesseract failed ({e})"
    if isinstance(e, getattr(Image, "UnidentifiedImageError", ()) if HAS_TESSERACT else ()):
        return "the file is not an image that can be read"
    return f"{type(e).__name__}: {e}"


def read_text(raw, mimetype=None, filename=None, config=None):
    """extract_text, with every failure turned into a ReceiptReadError with a readable message."""
    try:
        return extract_text(raw, mimetype, filename, config) or ""
    except Exception as e:  # noqa: BLE001 — re-raised with a readable message
        raise ReceiptReadError(
            f"the text of {filename or 'the attachment'} could not be read: {_describe_read_error(e)}") from e


# ── Regex-fallback ────────────────────────────────────────────────────────────

# A line that starts with a total label; the amount is the last amount token on the line
# (inv.AMOUNT_TOKEN_RE), so item counts in between are fine: 'Totalt (2 Artiklar) 418,00',
# 'Summa 2 varor 418,00'. VAT, net and discount totals are not the amount paid.
TOTAL_LINE_RE = re.compile(
    r"(?im)^[ \t]*(att[ \t]+betala|totalt?|totalbelopp|totalsumma|slutsumma|kort|k[oö]p|summa|belopp)\b"
    r"(?![ \t.:]*(?:moms|vat|exkl|netto|underlag|rabatt))(.*)$")
# Which label wins when several lines carry one: the amount to pay, then the card or
# purchase line, then sums ("Summa" is often before a discount, "Belopp" on a card slip
# can include a cash withdrawal). Within the best label the last line wins.
TOTAL_LABEL_RANK = (("att betala", "total", "slutsumma"), ("kort", "kop", "köp"), ("summa", "belopp"))
DATE_RE = re.compile(r"(20\d{2})[-./](\d{2})[-./](\d{2})")


def _total_label_rank(label):
    label = re.sub(r"[ \t]+", " ", label.lower())
    for rank, prefixes in enumerate(TOTAL_LABEL_RANK):
        if label.startswith(prefixes):
            return rank
    return len(TOTAL_LABEL_RANK)


def _regex_total(text):
    """The amount paid according to the labelled total lines, or None."""
    best = None  # (rank, amount)
    for m in TOTAL_LINE_RE.finditer(text):
        amounts = [a for a in inv.amounts_in_text(m.group(2)) if a > 0]
        if not amounts:
            continue
        rank = _total_label_rank(m.group(1))
        if best is None or rank <= best[0]:
            best = (rank, amounts[-1])
    return best[1] if best else None


def _regex_fields(text):
    out = {}
    total = _regex_total(text)
    if total is not None:
        out["total"] = total
    m = DATE_RE.search(text)
    if m:
        y, mo, d = (int(x) for x in m.groups())
        with contextlib.suppress(ValueError):
            out["date"] = date(y, mo, d).isoformat()
    return out


# ── AI ────────────────────────────────────────────────────────────────────────

RECEIPT_SCHEMA = {
    "type": "object",
    "properties": {
        "merchant": {"type": ["string", "null"]},
        "receipt_number": {"type": ["string", "null"]},
        "date": {"type": ["string", "null"]},
        "total": {"type": ["number", "null"]},
        "vat_amount": {"type": ["number", "null"]},
        "currency": {"type": ["string", "null"]},
        "items": {"type": ["string", "null"]},
        "card_last4": {"type": ["string", "null"]},
        "category_code": {"type": ["string", "null"]},
        "confidence": {"type": ["number", "null"]},
    },
    "required": ["merchant", "date", "total", "items", "category_code", "confidence"],
}

PROMPT = """You are reading the OCR text of a Swedish till receipt (kvitto) for an expense claim. Return ONLY valid JSON matching the schema, no other text.

Fields:
- merchant: shop / company name EXACTLY as it appears in the OCR text (e.g. "OKQ8", "Jula"). If no shop name is readable in the text, use null — never guess a chain from the products.
- receipt_number: kvittonummer / kvitto nr if printed, else null.
- date: purchase date as YYYY-MM-DD. Swedish receipts print dates as 2026-09-17 or 17/09/2026 or 170917. null if unreadable.
- total: the amount actually paid, VAT included, as a number (e.g. 389.00). Look for "Totalt", "Summa", "Att betala", "Köp", the card line. Never the VAT base ("Underlag"/"Netto") and never the VAT amount.
- vat_amount: VAT ("Moms") amount as a number, or null.
- currency: "SEK" unless clearly another currency.
- items: one short line in Swedish summarising what was bought (e.g. "Sågkedjeolja 4L", "Alkylatbensin 2 st"). Skip prices.
- card_last4: last four digits of the card if printed (e.g. "9688"), else null.
- category_code: the best expense category for what was bought, chosen ONLY from this list (code — description):
{categories}
  Prefer a specific category over a general one (EXP_GEN / "Expenses" is the last resort). Fuel, oil, chain lubricant, spare parts and tools belong to the machinery/equipment category if one exists. Use null if nothing fits.
- confidence: 0.0–1.0, how sure you are about total and date together.

OCR text follows (it may contain OCR errors like 0/O, 1/l, misplaced spaces):
---
"""


def build_prompt(categories):
    """categories: [(code, name)] eller [(code, name, hint)] — hinten är produktens beskrivning i Odoo."""
    rows = []
    for cat in categories or []:
        code, name = cat[0], cat[1]
        hint = cat[2] if len(cat) > 2 and cat[2] else ""
        rows.append(f"  {code} — {name}" + (f": {hint}" if hint else ""))
    return PROMPT.replace("{categories}", "\n".join(rows) or "  (no categories available)")


def _chat_json(prompt, text, config=None):
    """Structured call through the invoice library: same provider, keys, retries and fallbacks.

    `config` is the per-run config (provider, keys, URLs); None = the library's env defaults.
    """
    data, meta = inv.chat_json(prompt, text, RECEIPT_SCHEMA, "receipt", max_tokens=4000, max_chars=4000,
                               timeout=180, config=config)
    if isinstance(data, dict):
        data["_served_model"] = meta.get("served_model")
        data["_completion_tokens"] = meta.get("completion_tokens")
    return data or {}


def _clean(data, categories):
    out = {}
    for k in ("merchant", "receipt_number", "items", "card_last4", "currency", "category_code"):
        v = data.get(k)
        if isinstance(v, str) and v.strip() and v.strip().lower() not in ("null", "none", "unknown"):
            out[k] = v.strip()
    for k in ("total", "vat_amount", "confidence"):
        v = inv._num(data.get(k))
        if v is not None:
            out[k] = v
    d = data.get("date")
    if isinstance(d, str):
        p = inv._parse_date(d) if hasattr(inv, "_parse_date") else None
        if p:
            out["date"] = p if isinstance(p, str) else p.isoformat()
        elif DATE_RE.search(d):
            out["date"] = _regex_fields(d).get("date")
    codes = {c[0] for c in (categories or [])}
    if out.get("category_code") and out["category_code"] not in codes:
        out.pop("category_code")
    if out.get("total") is not None and out["total"] <= 0:
        out.pop("total")
    return out


MIN_CONFIDENCE = 0.6


def _merchant_in_text(merchant, text):
    """Modellen gissar gärna en kedja utifrån varorna. Namnet måste stå i OCR-texten."""
    if not merchant:
        return False
    words = [w for w in re.split(r"[^0-9A-Za-zÅÄÖåäö]+", merchant) if len(w) >= 3]
    low = text.lower()
    return any(w.lower() in low for w in words)


def _total_in_text(total, text):
    """The total must be printed as a whole amount, not as part of another number or a date."""
    return inv.amount_in_text(total, text)


def _apply_guards(fields, text, regex=None):
    """Returnerar (fält att fylla i, anmärkningar). Osäkra läsningar blir anmärkningar, inte fält.

    `regex` is what _regex_fields read from the text: when the model's value fails a check,
    the printed value is used instead (and noted).
    """
    notes = []
    fields = dict(fields)
    regex = regex or {}
    conf = fields.get("confidence")
    if fields.get("merchant") and not _merchant_in_text(fields["merchant"], text):
        notes.append(f"butiksnamnet \"{fields['merchant']}\" finns inte i kvittotexten — ignorerat")
        fields.pop("merchant")
    if fields.get("total") is not None and not _total_in_text(fields["total"], text):
        printed = regex.get("total")
        if printed is not None and _total_in_text(printed, text):
            notes.append(f"the amount {fields['total']:.2f} is not printed on the receipt — "
                         f"used the receipt's total {printed:.2f}")
            fields["total"] = printed
        else:
            notes.append(f"beloppet {fields['total']:.2f} står inte i kvittotexten — ignorerat")
            fields.pop("total")
    if conf is not None and conf < MIN_CONFIDENCE:
        notes.append(f"låg konfidens ({conf:.2f}) — belopp och datum fylls inte i")
        fields.pop("total", None)
        fields.pop("date", None)
    return fields, notes


def extract_receipt_data(raw, mimetype=None, filename=None, categories=None, config=None):
    """Huvudingång. Returnerar {"text": ..., "fields": {...}, "source": "ai"|"regex"|"none"}.

    config: per-run config för fakturabiblioteket (se invoice_ocr.default_config); None =
    miljövariabel-defaults.
    """
    cfg = inv._cfg(config)
    text = read_text(raw, mimetype, filename, cfg)
    result = {"text": text, "fields": {}, "source": "none", "notes": []}
    if len(text.strip()) < 15:
        return result
    regex = _regex_fields(text)
    try:
        ai = _clean(_chat_json(build_prompt(categories), text, cfg), categories)
    except Exception as e:  # noqa: BLE001 — AI:n får aldrig fälla mailhämtningen
        logger.warning("receipt AI extraction failed (%s): %s", cfg["provider"], e)
        ai = {}
    if ai:
        # AI ser hela sammanhanget; regex fyller bara luckor
        fields = dict(regex)
        fields.update(ai)
        fields, notes = _apply_guards(fields, text, regex)
        result.update(fields=fields, source="ai", notes=notes)
    elif regex:
        result.update(fields=regex, source="regex", notes=["AI-tolkningen misslyckades; bara regex"])
    return result
