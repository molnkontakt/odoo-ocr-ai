#!/usr/bin/env python3
"""
Extraherar strukturerad data från svenska leverantörsfakturor (PDF).

Använder pdfplumber för textextraktion, med pytesseract som fallback
för bildbaserade PDF:er. Returnerar ett dict med extraherade fält.

Användning:
    from invoice_ocr import extract_invoice_data
    data = extract_invoice_data(pdf_bytes)
    # {'invoice_number': '1033', 'invoice_date': '2026-03-01', ...}
"""

import base64
import io
import json
import logging
import math
import os
import re
import time
from datetime import date

import pdfplumber

logger = logging.getLogger(__name__)

try:
    import pytesseract
    from PIL import Image  # noqa: F401 — availability probe
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False

# ── Tunables ─────────────────────────────────────────────────────────────────

# How much invoice text the LLM sees. The full document is never sent; long
# specifications are truncated so the prompt (and the bill) stays bounded.
TEXT_LIMIT = int(os.environ.get("INVOICE_OCR_TEXT_LIMIT", "6000"))
# Image-based PDFs are rendered page by page; cap it so a 300-page PDF cannot
# pin a worker for minutes in the synchronous upload path.
MAX_OCR_PAGES = int(os.environ.get("INVOICE_OCR_MAX_PAGES", "10"))
# Render scale for tesseract OCR (2 ≈ 144 dpi — good compromise).
OCR_SCALE = float(os.environ.get("INVOICE_OCR_SCALE", "2"))

# ── Time and size budgets (#9, #26) ─────────────────────────────────────────
# One document — the text extraction and every provider call for it (retries, the 429
# wait, the schema fallback, the reliability re-run) — must be done within this many
# seconds. 80 s keeps the synchronous "Run OCR" button under Odoo's default 120 s
# request limit (limit_time_real), with room for the writes to the record, and one
# background run (document, writes, start-up) within 90 s.
TOTAL_DEADLINE = float(os.environ.get("INVOICE_OCR_DEADLINE", "80"))
# The share of it the text extraction (pdfplumber, tesseract) may use at most (#26).
EXTRACT_TIME_BUDGET = float(os.environ.get("INVOICE_OCR_EXTRACT_BUDGET", "30"))
# pdfplumber reads at most this many pages of a PDF: the first ones and the last one.
MAX_TEXT_PAGES = int(os.environ.get("INVOICE_OCR_MAX_TEXT_PAGES", "20"))
# A page rendered for tesseract, or a receipt photo, is scaled down to at most this many
# pixels (an A4 page at scale 2 is about 2 MP): a huge page cannot become a bitmap of
# several GB.
MAX_PAGE_PIXELS = int(os.environ.get("INVOICE_OCR_MAX_PIXELS", "12000000"))
# One tesseract run (one page or one photo) is stopped after this many seconds.
TESSERACT_TIMEOUT = float(os.environ.get("INVOICE_OCR_TESSERACT_TIMEOUT", "20"))
# A receipt image larger than this is not read (a phone photo is a few MB).
MAX_IMAGE_BYTES = int(os.environ.get("INVOICE_OCR_MAX_IMAGE_BYTES", str(20 * 1024 * 1024)))
# A provider call is not started with less time than this left.
MIN_CALL_SECONDS = 5.0
# The wait before the one retry after an HTTP 429 (rate limited).
RATE_LIMIT_WAIT = 15.0


def _clock():
    """Monotonic seconds; the one clock of the budgets (a seam for tests)."""
    return time.monotonic()


class DeadlineExceeded(TimeoutError):
    """The document's time budget is spent: no further provider call is started."""


class DocumentRun:
    """One document's time budget (#9) and the notes about what a budget cut (#26).

    Created by the entry points (extract_invoice_data, receipt_ocr.extract_receipt_data)
    and carried in the per-run config under "run", so the text extraction and every
    provider call for the document share one deadline.
    """

    def __init__(self, total_seconds=None):
        self.total = float(total_seconds or 0) or TOTAL_DEADLINE
        self.deadline = _clock() + self.total
        self.notes = []

    def remaining(self):
        return self.deadline - _clock()

    def note(self, text):
        if text not in self.notes:
            self.notes.append(text)


def document_run(cfg):
    """The DocumentRun of the per-run config `cfg`, created (and stored in it) on first use."""
    run = cfg.get("run")
    if not isinstance(run, DocumentRun):
        run = cfg["run"] = DocumentRun(cfg.get("total_deadline"))
    return run


def call_timeout(run, timeout):
    """The timeout for the next provider call: the per-call cap, cut to what the document
    has left; DeadlineExceeded when that is less than MIN_CALL_SECONDS."""
    left = run.remaining()
    if left < MIN_CALL_SECONDS:
        raise DeadlineExceeded(
            f"no time left for a call to the AI provider (time limit per document "
            f"{run.total:.0f} s)")
    return min(float(timeout), left)


# ── Date formats ────────────────────────────────────────────────────────────

_MONTH_WORD = r"[A-Za-zÅÄÖåäö]{3,9}\.?"
DATE_PATTERNS = [
    r"\d{4}-\d{2}-\d{2}",                          # 2026-03-01
    r"\d{4}\.\d{2}\.\d{2}",                        # 2026.03.01
    r"\d{4}/\d{2}/\d{2}",                          # 2026/03/01
    r"\d{1,2}\.\d{1,2}\.\d{4}",                    # 31.03.2026 (ALSO/DE format)
    r"\d{1,2}/\d{1,2}/\d{4}",                      # 09/04/2026 (Hetzner format)
    r"\d{1,2}\.?[ \t]+" + _MONTH_WORD + r",?[ \t]+\d{4}",   # 1 mars 2026, 3 March 2026
    _MONTH_WORD + r"[ \t]+\d{1,2}(?:st|nd|rd|th)?,?[ \t]+\d{4}",  # March 3, 2026
]

SWEDISH_MONTHS = {
    "januari": "01", "februari": "02", "mars": "03", "april": "04",
    "maj": "05", "juni": "06", "juli": "07", "augusti": "08",
    "september": "09", "oktober": "10", "november": "11", "december": "12",
}
# Swedish and English month names and their usual abbreviations → month number
MONTHS = {name: int(num) for name, num in SWEDISH_MONTHS.items()}
MONTHS.update({
    "january": 1, "february": 2, "march": 3, "may": 5, "june": 6, "july": 7,
    "august": 8, "october": 10,
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "jun": 6, "jul": 7, "aug": 8,
    "sep": 9, "sept": 9, "okt": 10, "oct": 10, "nov": 11, "dec": 12,
})


def _month(word):
    return MONTHS.get(str(word or "").lower().rstrip("."))


# (full-match pattern, [(year, month, day) group numbers per reading])
_DATE_FORMS = [
    (r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})", [(1, 2, 3)]),
    (r"(\d{1,2})\.(\d{1,2})\.(\d{4})", [(3, 2, 1)]),
    (r"(\d{1,2})[/-](\d{1,2})[/-](\d{4})", [(3, 2, 1), (3, 1, 2)]),   # dd/mm, then mm/dd
    (r"(\d{1,2})\.? (" + _MONTH_WORD + r"),? (\d{4})", [(3, 2, 1)]),
    (r"(" + _MONTH_WORD + r") (\d{1,2})(?:st|nd|rd|th)?,? (\d{4})", [(3, 1, 2)]),
]


def _date_readings(text):
    """The valid ISO dates a printed date can mean: none, one, or two for NN/NN/YYYY.

    'NN/NN/YYYY' is read as dd/mm first (Swedish and European invoices) and as mm/dd
    second; only readings that are real calendar dates are returned, so '09/15/2026'
    gives only 2026-09-15 and '09/04/2026' gives 2026-04-09 and 2026-09-04.
    """
    t = re.sub(r"\s+", " ", str(text or "").strip())
    for pattern, order in _DATE_FORMS:
        m = re.fullmatch(pattern, t)
        if m:
            parts = [tuple(_month(m.group(i)) or m.group(i) for i in ymd) for ymd in order]
            break
    else:
        return []
    readings = []
    for y, mo, d in parts:
        try:
            iso = date(int(y), int(mo), int(d)).isoformat()
        except ValueError:
            continue
        if iso not in readings:
            readings.append(iso)
    return readings


def _parse_date(text):
    """A printed date as YYYY-MM-DD, or None unless it is a real calendar date.

    Ambiguous 'NN/NN/YYYY' gives the dd/mm reading (see _date_readings).
    """
    readings = _date_readings(text)
    return readings[0] if readings else None


def iso_date(value):
    """`value` as a valid YYYY-MM-DD string, or None (also for date objects and junk)."""
    if isinstance(value, date):
        return value.isoformat()
    return _parse_date(value) if isinstance(value, str) else None


# ── Öresavrundning och justeringar utanför moms ─────────────────────────────
# Vissa fakturor (t.ex. från teleoperatörer) trycker "Belopp exkl. moms" EFTER
# tillgodo/justeringar utan moms, men momsen på underlaget FÖRE dem, och "Att betala"
# efter öresavrundning:
#   tjänster 2 061,00 · tillgodo −0,25 · exkl. moms 2 060,75 · moms 515,25 (25 % av
#   2 061,00) · öresavrundning −1,00 · att betala 2 575,00
# AI:n lägger då 2 060,75 som en momsbelagd rad → moms 515,19, totalt 2 575,94.
MAX_VAT_BASE_SHIFT = 2.0   # största del av netto som får flyttas utanför moms
MAX_ROUNDING = 2.0         # största öresavrundning som läggs till automatiskt


def plan_total_adjustments(taxed_net, untaxed_net, current_vat, printed_vat, printed_total):
    """Justeringar som får raderna att stämma med fakturans tryckta moms och totalbelopp.

    taxed_net: {momssats (int): netto på rader med den satsen}; untaxed_net: netto utan moms;
    current_vat: momsen Odoo räknat fram. Returnerar {"base_shift": (sats, belopp) | None,
    "rounding": belopp | None}:

    * base_shift: momsunderlaget enligt den tryckta momsen skiljer sig från radernas –
      flytta beloppet till momsraden och lägg MOTSATT belopp utanför moms (nettot oförändrat).
      Bara när alla momsrader har samma sats och skillnaden är större än momsens egen
      avrundning (2 öre på underlaget) men högst MAX_VAT_BASE_SHIFT.
    * rounding: det som skiljer totalen (efter base_shift) från "Att betala", högst MAX_ROUNDING.
    Större avvikelser lämnas orörda – de är inte avrundning och ska granskas av en människa.
    """
    plan = {"base_shift": None, "rounding": None}
    vat_after = current_vat
    rates = [r for r, net in taxed_net.items() if net]
    if printed_vat is not None and len(rates) == 1 and rates[0]:
        rate = rates[0]
        implied = round(printed_vat * 100.0 / rate, 2)
        delta = round(implied - taxed_net[rate], 2)
        if 0.02 < abs(delta) <= MAX_VAT_BASE_SHIFT:
            plan["base_shift"] = (rate, delta)
            vat_after = printed_vat
    if printed_total is not None:
        total_after = round(sum(taxed_net.values()) + untaxed_net + vat_after, 2)
        diff = round(printed_total - total_after, 2)
        if 0.005 < abs(diff) <= MAX_ROUNDING:
            plan["rounding"] = diff
    return plan


def _group_or_decimal(text, sep):
    """A number with one kind of separator: thousands groups ('1,234,567', '1.234') or decimals.

    The separator is a thousands separator when every group after it has exactly three
    digits and the first group one to three (not starting with 0): '1,234' and '12.500'
    are 1234 and 12500, as printed on invoices without decimals. Otherwise it is the
    decimal mark: '12,50', '104.64', '0,500'.
    """
    if re.fullmatch(r"-?[1-9]\d{0,2}(?:" + re.escape(sep) + r"\d{3})+", text):
        return text.replace(sep, "")
    return text.replace(sep, ".")


def _parse_amount(text):
    """Parse amount: '1 234,56' / '1234.56' / '€539.00' / '$1,234.56' / '1,234' → float.

    Returns None when the text is not an amount.
    """
    text = str(text).strip().replace("\u2212", "-")
    # Remove currency symbols, letters, and common prefixes
    text = re.sub(r"[€$£¥A-Za-z]", "", text)
    # Remove spaces (thousand separators)
    text = text.replace(" ", "").replace("\u00a0", "").replace("\u202f", "")
    # Whole-krona marks on receipts: '418:-', '418,-'
    text = re.sub(r"[:,.]-$", "", text)
    # Determine decimal separator:
    # "1.234,56" → comma is decimal (Swedish/EU)
    # "1,234.56" → dot is decimal (English)
    # "1234,56"  → comma is decimal
    # "1234.56"  → dot is decimal
    # "1,234" / "1.234" → thousands separator (see _group_or_decimal)
    if "," in text and "." in text:
        if text.rindex(",") > text.rindex("."):
            # Comma after dot: "1.234,56" → EU format
            text = text.replace(".", "").replace(",", ".")
        else:
            # Dot after comma: "1,234.56" → English format
            text = text.replace(",", "")
    elif "," in text:
        text = _group_or_decimal(text, ",")
    elif "." in text:
        text = _group_or_decimal(text, ".")
    try:
        return float(text)
    except ValueError:
        return None


# ── Amount tokens ────────────────────────────────────────────────────────────
# Whole amounts in OCR text, for checks like "does the model's total appear on the
# receipt?". A token never starts or ends inside a longer number, a date (2026-09-17,
# 17.09.2026), a time (10:14) or a percentage (25 %), so 18 is not found in '418,00' and
# 17 is not found in a date. Two kinds of tokens:
#   * amounts with two decimals, the integer part plain or grouped by space, no-break
#     space, dot or comma: '418,00', '1234.50', '1 234,50', '1.234,50', '-32,00';
#   * whole amounts only when a currency mark follows: '1 000 kr', '418:-', '418,-'.
# Each token is read with _parse_amount.
_AMOUNT_INT = r"(?:\d{1,3}(?:[ \u00a0\u202f.,]\d{3})+|\d+)"
AMOUNT_TOKEN_RE = re.compile(
    r"(?<![\d.,:/\-])-?(?:"
    + _AMOUNT_INT + r"[.,]\d{2}(?!\d|[.,:/\-]\d|[ \t]*%)"
    + r"|" + _AMOUNT_INT + r"(?=[ \t]*(?:kr\b|sek\b|:-|,-|\.-))"
    + r")",
    re.IGNORECASE)


def amounts_in_text(text):
    """Every whole amount printed in `text` (see AMOUNT_TOKEN_RE), in document order."""
    values = (_parse_amount(m.group(0)) for m in AMOUNT_TOKEN_RE.finditer(str(text or "")))
    return [v for v in values if v is not None]


def amount_in_text(value, text):
    """True when `value` is printed in `text` as a whole amount (not inside another number)."""
    value = _num(value)
    if value is None:
        return False
    return any(abs(v - value) < 0.005 for v in amounts_in_text(text))


# ── Field extraction patterns ────────────────────────────────────────────────

OCR_LABEL = r"\b(?:OCR[ _-]?(?:nummer|nr)|OCR|Betalningsreferens)\b"
BANKGIRO_LABEL = r"(?:\bBankgiro(?:nummer|nr)?|\bBG|\bBg\.?)(?![A-Za-zÅÄÖåäö])"
PLUSGIRO_LABEL = r"(?:\bPlusgiro(?:nummer|nr)?|\bPG|\bPg\.?)(?![A-Za-zÅÄÖåäö])"
# 123-4567, 1234-5678, 12345678
BANKGIRO_VALUE = r"(\d{3,4}[ \t-]?\d{4})(?![\d-])"
# 12 34 56-7, 123456-7, 1234567-8, 12345678
PLUSGIRO_VALUE = r"(\d(?:[ \t]?\d){0,6}[ \t]?-?[ \t]?\d)(?![\d-])"


FIELD_PATTERNS = {
    "invoice_number": [
        # Same-line: "Fakturanummer: 1033", "Invoice no.: 084000802912"
        # Use [\s.:]* to consume any combo of dots/colons/spaces between label and value
        r"(?:Fakturanummer|Faktura\s*nr|Faktura\s*#|Invoice\s*(?:no|number|#))[\s.:]*(\d[\d/-]*)",
        r"(?:Faktnr|Fakt\.?\s*nr)[\s.:]*(\d[\d/-]*)",
        # English: "Order Number: EU50246" or "Invoice #EU50246"
        r"(?:Order\s*(?:Number|No|#)|Invoice\s*#)[\s.:]*(\S+)",
    ],
    "invoice_date": [
        r"(?:Fakturadatum|Invoice\s*date)[\s.:]*(" + "|".join(DATE_PATTERNS) + ")",
        # \b: not the end of Leveransdatum, Förfallodatum, Orderdatum, …
        r"\bDatum\b[\s.:]*(" + "|".join(DATE_PATTERNS) + ")",
        # English: "Order Date: 2026-03-10 16:23:56"
        r"(?:Order\s*Date)[\s.:]*(" + "|".join(DATE_PATTERNS) + ")",
    ],
    "due_date": [
        r"(?:Förfallodatum|Förfallodag|Due\s*date|Betalningsdatum|Payment\s*due)[\s.:]*(" + "|".join(DATE_PATTERNS) + ")",
        r"(?:Förfaller|Bet\.?\s*datum)[\s.:]*(" + "|".join(DATE_PATTERNS) + ")",
        # "2026-03-16 2026-03-26" on a line after "Fakturadatum Förfallodatum"
        r"\d{4}-\d{2}-\d{2}\s+(\d{4}-\d{2}-\d{2})",
    ],
    "total_amount": [
        r"(?:Belopp\s*att\s*betala|Totalt\s*att\s*betala|Att\s*betala|Summa\s*att\s*betala|Amount\s*due)\s*(?:\(SEK\))?[\s.:]*[€$£]?([\d\s.,]+)",
        r"(?:Totalt|Summa\s*inkl\.?\s*moms)[\s.:]*[€$£]?([\d\s.,]+)",
        # English: "Grand total €539.00" or "Total: $100.00"
        r"(?:Grand\s*total|Total\s*amount|Amount\s*paid|Paid\s*by\s*customer)[\s.:]*[€$£]?([\d\s.,]+)",
        # Plain "Total € 104.64" on its own line (Hetzner)
        r"(?:^|\n)Total\s+[€$£]\s*([\d.,]+)\s*$",
        # Hetzner totals row: "Total € 104.64 € 0.00 € 104.64" (subtotal, vat, total) — pick last
        r"(?:^|\n)Total(?:\s+[€$£]\s*[\d.,]+){2}\s+[€$£]\s*([\d.,]+)",
    ],
    "vat_amount": [
        # "Moms 25% 512,00 kr" — rate then amount
        r"Moms\s+\d+%\s+([\d\s.,]+)\s*kr",
        # "Moms 500,00" but NOT "Moms 25%" (that's a rate, not an amount)
        r"^Moms\s+([\d\s.,]+)$",
        r"(?:Varav\s*moms|Mervärdesskatt)[\s.:]*[€$£]?([\d\s.,]+)",
        # "I rutan ... Moms 512,00 kr"
        r"Moms\s+([\d\s.,]+)\s*kr",
        # English: "Tax €0.00" or "VAT: 100.00"
        r"(?:^Tax\s+Amount|^VAT\b)[\s.:]*[€$£]?([\d\s.,]+)",
    ],
    "subtotal": [
        r"(?:Belopp\s*exkl\.?\s*moms|Summa\s*exkl\.?\s*moms|Netto|Exkl\.?\s*moms)[\s.:]*[€$£]?([\d\s.,]+)",
        # English: "Subtotal €549.00" or "Total exclude tax €539.00"
        r"(?:Subtotal|Total\s*exclu\w*\s*tax)[\s.:]*[€$£]?([\d\s.,]+)",
    ],
    # Values stay on the label's line ([ \t], never \s, which also matches a line break and
    # glued the next line's digits on); a label on one row with the value on the next is
    # handled by the next-line patterns below. Labels are whole words ('SUBG 5' is no BG).
    "ocr_number": [
        OCR_LABEL + r"[ \t.:]*(\d[\d \t]*\d)",
    ],
    "bankgiro": [
        BANKGIRO_LABEL + r"[ \t.:]*" + BANKGIRO_VALUE,
    ],
    "plusgiro": [
        PLUSGIRO_LABEL + r"[ \t.:]*" + PLUSGIRO_VALUE,
    ],
    # org_number hanteras separat i _extract_org_number: fakturan trycker ofta
    # BÅDA parternas org.nr, och det första efter en etikett är inte sällan
    # köparens (det egna bolagets).
    "currency": [
        r"(?:Valuta|Currency)[\s.:]*(SEK|EUR|USD|NOK|DKK|GBP)",
        r"\((\s*SEK|EUR|USD|NOK|DKK|GBP)\s*\)",
    ],
}

# ── Next-line patterns ───────────────────────────────────────────────────────
# Some invoices put the label on one line and the value on the next:
#   Fakturanummer  Erreferens
#   1033           Johan Tollstorp
# org_number is extracted before these (see _extract_org_number) so an org.nr
# on the bankgiro line is never taken for the bankgiro
NEXTLINE_PATTERNS_ORDERED = [
    ("invoice_number", [r"(?:Fakturanummer|Faktura\s*nr|Invoice\s*(?:no|number))"]),
    ("invoice_date", [r"(?:Fakturadatum|Invoice\s*date)"]),
    ("due_date", [r"(?:Förfallodatum|Förfallodag|Due\s*date)"]),
    ("bankgiro", [BANKGIRO_LABEL]),
    ("plusgiro", [PLUSGIRO_LABEL]),
    #   Fakturanummer  OCR-nummer
    #   1033           1234567897
    ("ocr_number", [OCR_LABEL]),
]

# A value on the line under its label: the candidates of that kind on the next line,
# filtered by format and check digit; the one closest to the label's column wins
# (several labels often share a row, with their values in the same order below).
NEXTLINE_CANDIDATES = {
    "bankgiro": r"(?<![\d-])(\d{3,4}[ -]?\d{4})(?![\d-])",
    "plusgiro": r"(?<![\d-])(\d(?: ?\d){0,6} ?- ?\d)(?![\d-])",
    "ocr_number": r"(?<![\d-])(\d{2,25})(?![\d-])",
}


def _nextline_number(field, label_col, line, exclude=()):
    """The `field` value on `line` closest to `label_col`, or None (see NEXTLINE_CANDIDATES)."""
    best = None
    for m in re.finditer(NEXTLINE_CANDIDATES[field], line):
        digits = re.sub(r"\D", "", m.group(1))
        if any(digits in o for o in exclude if o):
            continue  # part of an org.nr printed on the same line
        if not valid_giro_or_ocr(field, digits):
            continue
        distance = abs(m.start() - label_col)
        if best is None or distance < best[0]:
            best = (distance, re.sub(r"\s+", "", m.group(1)))
    return best[1] if best else None


# ── Egna identiteter ─────────────────────────────────────────────────────────
# Köparens (det egna bolagets) org.nr, momsreg.nr och namn står på varje faktura.
# Odoo-modulen skickar in fakturabolagets värden från res.company i
# per-körning-configen ("own_ids"/"own_names", se default_config) och ändrar
# aldrig modul-globalerna; OWN_COMPANY / OWN_VAT_NUMBERS (miljövariablerna
# INVOICE_OCR_OWN_COMPANY och INVOICE_OCR_OWN_VAT, se nedan) används bara när
# anroparen inte skickar något, t.ex. i fristående skript.

# "Org.nr", "Org. nummer", "Organisationsnr.", "Organisationsnummer"
ORG_LABEL = r"(?:Organisationsnummer|Organisationsnr|Org\.?\s*(?:nr|nummer))"
ORG_VALUE = r"(\d{6}[\s-]?\d{4})"
# Svenskt momsreg.nr: "Momsreg.nr.: SE556000000001", "Momsregistreringsnummer SE…"
SE_VAT_LABEL = r"(?:Momsreg(?:istrerings)?\.?\s*(?:nr|nummer)|VAT\s*(?:no|number|nr|id))"

# Words in a company name that say nothing about which company it is: legal forms,
# countries, generic business words. Shared by the bank-line matching in the Odoo model
# and the receipt module's merchant check.
NAME_STOPWORDS = frozenset({
    "ab", "aktiebolag", "publ", "bank", "banken", "sverige", "sweden", "svenska",
    "the", "och", "and", "ltd", "limited", "inc", "llc", "gmbh", "group", "services",
    "company", "international", "nordic", "scandinavia",
})

_LEGAL_SUFFIXES = re.compile(
    r"\b(?:ab|aktiebolag|\(publ\)|publ|ltd|limited|gmbh|as|a/s|oy|inc|llc|bv|sa|sarl)\b\.?",
    re.IGNORECASE)


# Currencies written as symbols or words; "kr" is left out (SEK, NOK and DKK all use it).
CURRENCY_ALIASES = {"€": "EUR", "EURO": "EUR", "EUROS": "EUR", "$": "USD", "US$": "USD",
                    "£": "GBP"}


def normalize_currency(value):
    """The ISO 4217 code of an extracted currency ('eur', '€', 'US$' → EUR/USD), or None
    when there is none or it cannot be told ('kr', 'kronor')."""
    text = str(value or "").strip().upper()
    if text in CURRENCY_ALIASES:
        return CURRENCY_ALIASES[text]
    return text if re.fullmatch(r"[A-Z]{3}", text) else None


def _id_keys(value):
    """Jämförelsenycklar för ett org.nr eller momsreg.nr.

    '556000-0000', '5560000000', 'SE556000000001' och '556000000001' ska alla
    räknas som samma identitet. Tomma eller för korta värden ger inga nycklar.
    """
    s = re.sub(r"[^0-9A-Za-z]", "", str(value or "")).upper()
    digits = re.sub(r"\D", "", s)
    if len(digits) < 6:
        return set()
    keys = {s, digits}
    # Svenskt momsreg.nr = SE + org.nr (10 siffror) + 01
    if len(digits) == 12 and digits.endswith("01"):
        keys.add(digits[:10])
    return keys


def build_own_ids(values):
    """Normalisera en lista egna org.nr/momsreg.nr till en mängd jämförelsenycklar."""
    keys = set()
    for v in values or ():
        keys |= _id_keys(v)
    return keys


def _default_own_ids():
    return build_own_ids(OWN_VAT_NUMBERS)


def _default_own_names():
    return [OWN_COMPANY] if OWN_COMPANY else []


def is_own_id(value, own_keys):
    """True om value är ett av det egna bolagets org.nr/momsreg.nr (own_keys från build_own_ids)."""
    return bool(_id_keys(value) & own_keys)


def _name_key(name):
    s = _LEGAL_SUFFIXES.sub("", str(name or "").lower())
    return re.sub(r"[\s,.\-()]+", "", s)


# Legal forms that NAME_STOPWORDS does not list (two-letter ones would be noise there)
LEGAL_FORM_WORDS = frozenset({
    "as", "aps", "oy", "oyj", "bv", "nv", "sa", "sarl", "srl", "spa", "ag", "plc", "hb", "kb",
})


def name_tokens(name):
    """The distinctive words of a company name: lower case, without legal forms, countries
    and generic words (NAME_STOPWORDS, LEGAL_FORM_WORDS)."""
    words = re.findall(r"[^\W_]+", str(name or "").lower())
    return [w for w in words
            if w not in NAME_STOPWORDS and w not in LEGAL_FORM_WORDS and len(w) >= 2]


def name_match(ocr_name, partner_name):
    """How a vendor name read from a document matches a partner's name (#13).

    'full': the same name apart from case, spaces, punctuation and legal form ('Example
    Bank AB (publ)' and 'Example Bank'); 'tokens': every distinctive word of the document's
    name is a word of the partner's name ('Example Supplier AB' and 'Example Supplier
    Stockholm AB'); None otherwise. A name of generic words only never matches by tokens,
    so 'Acme Sverige AB' is not 'Other Sverige AB'.
    """
    key = _name_key(ocr_name)
    if key and len(key) >= 3 and key == _name_key(partner_name):
        return "full"
    tokens = name_tokens(ocr_name)
    if tokens and set(tokens) <= set(name_tokens(partner_name)):
        return "tokens"
    return None


def giro_digits(value):
    """The digits of a bankgiro/plusgiro or account number ('BG 123-4566' → '1234566')."""
    return re.sub(r"\D", "", str(value or ""))


def build_own_names(names):
    """Normaliserade egna bolagsnamn (gemener, utan blanksteg och bolagsform).

    pdfplumber tappar ofta mellanslagen ('EXAMPLERECEIVERAB'), så jämförelsen görs
    utan blanksteg. Namn kortare än fyra tecken hoppas över — de ger falsklarm.
    """
    keys = set()
    for n in names or ():
        k = _name_key(n)
        if len(k) >= 4:
            keys.add(k)
    return keys


def _mentions_own_name(text, own_names):
    compact = re.sub(r"\s+", "", str(text or "").lower())
    return any(k in compact for k in own_names)


def _extract_org_number(text, lines, own_keys):
    """Leverantörens org.nr: första kandidaten efter en org.nr-etikett som INTE är köparens.

    Ordning: etikett+värde på samma rad (i dokumentordning), därefter etikett på
    en rad och värdet på nästa. Returnerar (värde, [egna nummer som hoppades över]).
    """
    skipped = []
    candidates = [m.group(1).strip() for m in re.finditer(
        ORG_LABEL + r"[\s.:]*" + ORG_VALUE, text, re.IGNORECASE)]
    for i, line in enumerate(lines):
        if not re.search(ORG_LABEL, line, re.IGNORECASE):
            continue
        for j in range(i + 1, min(i + 4, len(lines))):
            nxt = lines[j].strip()
            if not nxt:
                continue
            m = re.search(ORG_VALUE, nxt)
            if m:
                candidates.append(m.group(1).strip())
            break
    for c in candidates:
        if is_own_id(c, own_keys):
            if c not in skipped:
                skipped.append(c)
            continue
        return c, skipped
    return None, skipped


# ── Egna bankkonton ──────────────────────────────────────────────────────────

def build_own_bank_keys(acc_numbers):
    """Jämförelsenycklar för det egna bolagets bankkonton (sanitized_acc_number).

    Returnerar (bank_keys, account_keys): bank_keys är alla kontons siffror utan
    inledande nollor (bankgiro, plusgiro, konto); account_keys bara clearing+konto
    (minst tio siffror), som is_own_bank_number även känner igen avkortade.
    """
    bank_keys, account_keys = set(), set()
    for acc in acc_numbers or ():
        acc = re.sub(r"\s+", "", str(acc or "")).upper()
        digits = re.sub(r"\D", "", acc).lstrip("0")
        bank_keys.add(digits)
        # Svenskt IBAN: SEkk + 3 siffror bank-id + 17 siffror clearing/konto.
        # På fakturor står bara clearing+konto ('9999-0012345').
        if re.match(r"^SE\d{22}$", acc):
            bank_keys.add(acc[7:].lstrip("0"))
            account_keys.add(acc[7:].lstrip("0"))
        elif not acc.startswith(("BG", "PG")) and len(digits) >= 10:
            account_keys.add(digits)  # clearing+konto utan IBAN
    return ({k for k in bank_keys if len(k) >= 6},
            {k for k in account_keys if len(k) >= 10})


def is_own_bank_number(number, bank_keys, account_keys=()):
    """True om ett extraherat bankgiro/plusgiro/kontonummer är det egna bolagets.

    Jämför siffrorna utan inledande nollor: 'BG 123-4567' mot '1234567', och
    '9999-0012345' mot clearing+konto ur ett svenskt IBAN.
    """
    digits = re.sub(r"\D", "", str(number or "")).lstrip("0")
    if len(digits) < 6:
        return False
    if digits in bank_keys:
        return True
    # AI:n kortar ibland av det egna kontonumret till bankgirolängd och kallar det
    # bankgiro ('9999-0012' av '9999-0012345'). Början av ett eget
    # clearing+kontonummer är det egna bolagets.
    return len(digits) >= 7 and any(
        k.startswith(digits) and len(k) > len(digits) for k in account_keys)


# ── Betalreferens (OCR-nummer) ───────────────────────────────────────────────

def ocr_mod10(number):
    """Luhn/modulus 10 som Bankgirot och Plusgirot använder för OCR-nummer."""
    total = 0
    for i, ch in enumerate(reversed(str(number))):
        d = int(ch) * (2 if i % 2 else 1)
        total += d - 9 if d > 9 else d
    return total % 10 == 0


def valid_giro_or_ocr(field, value):
    """True when `value` has the length and mod-10 check digit of a bankgiro (7-8 digits),
    a plusgiro (2-8 digits) or an OCR reference (2-25 digits); dashes and spaces ignored."""
    raw = str(value or "")
    if re.search(r"[^\d\s-]", raw):
        return False
    digits = re.sub(r"\D", "", raw)
    low, high = {"bankgiro": (7, 8), "plusgiro": (2, 8), "ocr_number": (2, 25)}[field]
    return low <= len(digits) <= high and ocr_mod10(digits)


def valid_payment_reference(ocr_number, invoice_number=None):
    """Betalreferensen att spara, eller False.

    Bara siffror: måste vara ett giltigt OCR-nummer (2-25 siffror, modulus 10). AI:n har
    klistrat ihop fakturanumret med köparens postnummer och ibland tagit postnumret ensamt.
    Börjar referensen med fakturanumret och är fakturanumret självt giltigt används det
    (vissa leverantörer skriver "Ange fakturanummer som OCR"). Annars sparas ingen
    referens; betalfilen tar då fakturanumret. Referenser med bokstäver (RF-referenser,
    utländska betalreferenser) lämnas som de är.
    """
    raw = str(ocr_number or "").strip()   # AI:n svarar ibland med ett tal
    compact = re.sub(r"[\s\-]", "", raw)
    if not compact:
        return False
    if not compact.isdigit():
        return raw
    if 2 <= len(compact) <= 25 and ocr_mod10(compact):
        return compact
    invoice = re.sub(r"[\s\-]", "", str(invoice_number or ""))
    if (invoice.isdigit() and 2 <= len(invoice) < len(compact)
            and compact.startswith(invoice) and ocr_mod10(invoice)):
        return invoice
    return False


# ── Autogiro / automatisk dragning ───────────────────────────────────────────
# Fakturor som dras automatiskt från köparens konto (bankavgifter, autogiro, SEPA
# direct debit) får INTE betalas manuellt — då betalas de två gånger. pdfplumber
# tappar ofta mellanslagen ('Betalningavfakturanskermedautomatik…'), så matchningen
# görs på text utan blanksteg. Mönstren skrivs därför också utan blanksteg.
#
# Varje mönster måste PÅSTÅ att en dragning sker. Fristående ord som
# 'autogiromedgivande', 'direct debit' eller 'Lastschrift' står lika gärna i
# reklam, villkor och uppräkningar av betalsätt och räcker inte. Ett falsklarm
# gör att en vanlig faktura aldrig betalas.
_NOT_A_LIST = r"(?!eller|or|oder|och|and|und|,|/)"
AUTO_DEBIT_PATTERNS = [
    r"skermedautomatik",                                   # "Betalning sker med automatik"
    r"debiteras(?:ert|ditt|vårt|företagets|bolagets)?konto",  # "Beloppet debiteras företagets konto"
    r"(?:dras|drages|debiteras)(?:automatiskt)?från(?:ert|ditt|vårt|företagets|bolagets)konto",
    r"(?:dras|debiteras|betalas)automatiskt",
    r"(?:betalas|dras|debiteras|betalning(?:en)?sker)(?:via|med|genom|på)autogiro",
    r"autogirodragning(?:en)?(?:sker|görs|kommer)",
    r"(?:betalningssätt|betalsätt|betalningsmetod|betalningsform|paymentmethod|zahlungsart"
    r"|zahlungsweise)[:.]?(?:sepa-?)?(?:autogiro|directdebit|lastschrift)" + _NOT_A_LIST,
    r"(?:will|shall)be(?:automatically)?debited",
    r"(?:paid|collected|charged|debited)(?:automatically)?(?:by|via|through)(?:sepa-?)?directdebit",
    r"(?:wird|werden)(?:per|mittels|durch|via)(?:sepa-?)?lastschrift",
    r"vonihremkonto(?:abgebucht|eingezogen)",
]
# Reklam för autogiro/direct debit på en vanlig faktura är inte en dragning.
AUTO_DEBIT_NEGATIVE = [
    r"(?:anslut(?:a|er)?|ansök(?:a|er)?(?:om)?|anmäl(?:a|er)?|teckna|välj|byttill|betala(?:enkelt)?med)(?:dig)?(?:till)?autogiro",
    r"(?:setup|signupfor|switchto|payby|apply(?:for)?)(?:a)?directdebit",
]
# Nekad dragning: 'Autogirodragning sker ej', 'kommer inte att debiteras …'
_AUTO_DEBIT_NEG_BEFORE = re.compile(
    r"(?:ej|inte|icke|not|nicht|aldrig|never)(?:att|to|be|längre|mehr)?$")
_AUTO_DEBIT_NEG_AFTER = re.compile(
    r"^(?:sker|görs|kommer|will|is|does|shall|wird)?(?:ej|inte|icke|not|nicht|aldrig|never)")
# Villkor: meningen beskriver när dragning sker, inte att den sker för den här fakturan
_AUTO_DEBIT_CONDITION = re.compile(
    r"om(?:du|ni)(?:har|betalar|väljer|valt|anslutit|ansluter|önskar)"
    r"|ifall|såvida|vidbetalning(?:via|med|genom)"
    r"|if(?:you|the(?:customer|buyer|client))|unless|(?:for|to)customers(?:who|with|that)"
    r"|för(?:kunder|er)(?:som|med)|wenn(?:sie|du)|falls(?:sie|du)")
_AUTO_DEBIT_CONDITION_START = re.compile(r"\s*(?:om|ifall|if|when|wenn|falls)\b", re.I)


def _auto_debit_segments(text):
    """Dela texten i meningar/stycken så att negation och villkor bara gäller sin mening.

    Radbrytningar inom en mening (layoutens radbrytning) behålls ihop; en ny
    rad som börjar med versal räknas som ny mening, utom efter ett kolon.
    """
    segments = []
    for block in re.split(r"(?<=[.!?;])\s+|\n\s*\n", str(text or "")):
        cur = []
        for line in block.split("\n"):
            s = line.strip()
            if not s:
                continue
            if cur and s[0].isupper() and not cur[-1].endswith(":"):
                segments.append(" ".join(cur))
                cur = []
            cur.append(s)
        if cur:
            segments.append(" ".join(cur))
    return segments


def detect_auto_debit(text):
    """Returnerar den matchande frasen (utan blanksteg) om fakturan dras automatiskt, annars None.

    Frasen måste påstå en dragning, får inte vara nekad ('sker ej') och får inte
    stå i en villkorsmening ('om du har autogiro', 'Vid betalning via autogiro …').
    """
    for seg in _auto_debit_segments(text):
        compact = re.sub(r"\s+", "", seg.lower())
        if not compact:
            continue
        masked = compact
        for neg in AUTO_DEBIT_NEGATIVE:
            masked = re.sub(neg, "#", masked)
        if _AUTO_DEBIT_CONDITION.search(masked) or _AUTO_DEBIT_CONDITION_START.match(seg):
            continue
        for pat in AUTO_DEBIT_PATTERNS:
            for m in re.finditer(pat, masked):
                if (_AUTO_DEBIT_NEG_BEFORE.search(masked[:m.start()])
                        or _AUTO_DEBIT_NEG_AFTER.search(masked[m.end():])):
                    continue
                return m.group(0)
    return None


def pages_to_read(total, cap):
    """The 1-based page numbers to read of a `total`-page PDF, at most `cap` of them (#26).

    The first cap-1 pages and the last one: totals, the VAT summary and the payment details
    are usually at the end. An unknown page count reads the first `cap`; no cap reads all.
    """
    if not cap or cap <= 0:
        return list(range(1, total + 1)) if total else None
    if total is None:
        return list(range(1, cap + 1))
    if total <= cap:
        return list(range(1, total + 1))
    return [*range(1, cap), total] if cap > 1 else [total]


def _pages_note(kind, total, pages, cap):
    shown = f"1–{pages[-2]} and {pages[-1]}" if len(pages) > 1 else f"{pages[-1]}"
    return f"the PDF has {total} pages; {kind} only pages {shown} (page limit {cap})"


def _pdf_page_count(pdf_bytes):
    """The page count from the PDF's page tree, without parsing the pages; None if unknown."""
    try:
        from pdfminer.pdfdocument import PDFDocument
        from pdfminer.pdfparser import PDFParser
        from pdfminer.pdftypes import resolve1

        doc = PDFDocument(PDFParser(io.BytesIO(pdf_bytes)))
        return int(resolve1(resolve1(doc.catalog["Pages"])["Count"]))
    except Exception:  # noqa: BLE001 — a broken page tree: read the first pages
        return None


def _budget_note(run, done, total, budget):
    note = (f"reading the document text stopped after {done} of {total} pages: the time for "
            f"reading it ({budget:.0f} s) was used up – the rest was not read")
    logger.warning("OCR: %s", note)
    if run:
        run.note(note)


def _extract_text_pdfplumber(pdf_bytes, max_pages=None, stop_at=None, run=None, budget=None):
    """Extract text from PDF using pdfplumber.

    Only `max_pages` pages are read (pages_to_read: the first ones and the last), and no
    page is started after `stop_at` (a _clock() value). What was left out is noted on the
    DocumentRun `run`.
    """
    total = _pdf_page_count(pdf_bytes)
    wanted = pages_to_read(total, max_pages)
    if total and wanted and len(wanted) < total:
        note = _pages_note("the text was read from", total, wanted, max_pages)
        logger.warning("OCR: %s", note)
        if run:
            run.note(note)
    with pdfplumber.open(io.BytesIO(pdf_bytes), pages=wanted) as pdf:
        pages = []
        for done, page in enumerate(pdf.pages):
            if stop_at is not None and _clock() >= stop_at:
                _budget_note(run, done, len(pdf.pages), budget or 0)
                break
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)


def fit_scale(width, height, scale, max_pixels):
    """The render scale for a `width` × `height` page: `scale`, or less, so the bitmap has
    at most `max_pixels` pixels (#26)."""
    area = max(float(width) * float(height), 1.0)
    if max_pixels and area * scale * scale > max_pixels:
        return math.sqrt(max_pixels / area)
    return scale


def _is_tesseract_timeout(error):
    return isinstance(error, RuntimeError) and "timeout" in str(error).lower()


def _extract_text_tesseract(pdf_bytes, max_pages=None, scale=None, max_pixels=None,
                            page_timeout=None, stop_at=None, run=None, budget=None):
    """Fallback: convert PDF pages to images and OCR them.

    Bounded so a small hostile file cannot pin a worker (#26):

    * ``max_pages`` pages are rendered: the first ones and the last (pages_to_read);
    * a page is rendered at ``scale``, or smaller so it stays within ``max_pixels``;
    * one tesseract run stops after ``page_timeout`` seconds (that page is skipped);
    * no page is started after ``stop_at`` (a _clock() value) and a run never goes past it.

    The limits come from the per-run config, with the module globals as defaults; what was
    left out is noted on the DocumentRun ``run``.
    """
    if not HAS_TESSERACT:
        return ""

    try:
        import pypdfium2 as pdfium
    except ImportError:
        return ""

    if max_pages is None:
        max_pages = MAX_OCR_PAGES
    if scale is None:
        scale = OCR_SCALE
    if max_pixels is None:
        max_pixels = MAX_PAGE_PIXELS
    if page_timeout is None:
        page_timeout = TESSERACT_TIMEOUT

    def note(text):
        logger.warning("OCR: %s", text)
        if run:
            run.note(text)

    pdf_doc = pdfium.PdfDocument(pdf_bytes)
    try:
        total = len(pdf_doc)
        wanted = pages_to_read(total, max_pages) or []
        if len(wanted) < total:
            note(_pages_note("OCR read", total, wanted, max_pages))
        pages = []
        for done, number in enumerate(wanted):
            timeout = page_timeout
            if stop_at is not None:
                left = stop_at - _clock()
                if left < 1:
                    _budget_note(run, done, len(wanted), budget or 0)
                    break
                timeout = min(timeout, left) if timeout else left
            page = pdf_doc[number - 1]
            try:
                width, height = page.get_size()
                page_scale = fit_scale(width, height, scale, max_pixels)
                if page_scale < scale:
                    note(f"page {number} is very large: it was scaled down to "
                         f"{max_pixels / 1e6:.0f} megapixels for OCR")
                pil_image = page.render(scale=page_scale).to_pil()
            finally:
                page.close()
            try:
                text = pytesseract.image_to_string(pil_image, lang="swe+eng",
                                                   timeout=timeout or 0)
            except RuntimeError as e:
                if not _is_tesseract_timeout(e):
                    raise
                note(f"tesseract took longer than {timeout:.0f} s on page {number} – that "
                     f"page was not read")
                continue
            if text.strip():
                pages.append(text)
        return "\n\n".join(pages)
    finally:
        pdf_doc.close()


def extract_text(pdf_bytes, config=None):
    """Extract text from PDF, with tesseract fallback for image-based PDFs.

    Within the config's budgets (#26): at most max_text_pages pages for pdfplumber and
    max_ocr_pages for tesseract (the first ones and the last), max_page_pixels per rendered
    page, tesseract_timeout per tesseract run, and extract_time_budget seconds in all — never
    past the document's deadline. A budget that cut the reading is noted on the config's
    DocumentRun (the Odoo modules show it in the chatter).
    """
    cfg = _cfg(config)
    run = document_run(cfg)
    budget = min(float(cfg["extract_time_budget"]), max(run.remaining(), 0))
    stop_at = _clock() + budget
    text = _extract_text_pdfplumber(pdf_bytes, max_pages=cfg["max_text_pages"],
                                    stop_at=stop_at, run=run, budget=budget)
    if len(text.strip()) < 50 and HAS_TESSERACT:
        # Probably an image-based PDF, try OCR
        ocr_text = _extract_text_tesseract(
            pdf_bytes, max_pages=cfg["max_ocr_pages"], scale=cfg["ocr_scale"],
            max_pixels=cfg["max_page_pixels"], page_timeout=cfg["tesseract_timeout"],
            stop_at=stop_at, run=run, budget=budget)
        if len(ocr_text.strip()) > len(text.strip()):
            text = ocr_text
    return text


def extract_fields(text, own_ids=None, own_names=None, config=None):
    """Extract structured invoice fields from text.

    own_ids:   the receiving company's org/VAT numbers; skipped when extracting the
               supplier's org.nr/VAT.
    own_names: the receiving company's names; never taken as vendor_name.
    config:    per-run config (see default_config). Explicit own_ids/own_names win;
               otherwise the config's "own_ids"/"own_names" are used, which default to
               OWN_VAT_NUMBERS/OWN_COMPANY (environment fallback for standalone use).
    """
    cfg = _cfg(config)
    if own_ids is None:
        own_ids = cfg["own_ids"]
    if own_names is None:
        own_names = cfg["own_names"]
    result = {}
    lines = text.split("\n")
    own_keys = build_own_ids(own_ids)
    own_name_keys = build_own_names(own_names)

    def set_date(field, raw):
        """Store a printed date if it is a real date; remember both readings if ambiguous."""
        readings = _date_readings(raw)
        if not readings:
            return False
        result[field] = readings[0]
        if len(readings) > 1:
            result.setdefault("_ambiguous_dates", {})[field] = readings
        return True

    # Standard same-line patterns
    for field, patterns in FIELD_PATTERNS.items():
        if field in ("invoice_date", "due_date"):
            # The first match that is a real date (an unparsable one never wins)
            for pattern in patterns:
                if any(set_date(field, m.group(1)) for m in re.finditer(
                        pattern, text, re.IGNORECASE | re.MULTILINE)):
                    break
            continue
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if m:
                value = m.group(1).strip()
                if field in ("total_amount", "vat_amount", "subtotal"):
                    parsed = _parse_amount(value)
                    if parsed is not None:
                        value = parsed
                    else:
                        continue  # Skip if amount can't be parsed
                elif field in ("bankgiro", "plusgiro", "ocr_number"):
                    value = re.sub(r"\s+", "", value)
                if value:
                    result[field] = value
                    break

    # Leverantörens org.nr — aldrig köparens (det egna bolagets) nummer
    org, own_skipped = _extract_org_number(text, lines, own_keys)
    if org:
        result["org_number"] = org
    if own_skipped:
        result["_own_ids_skipped"] = own_skipped

    # Next-line patterns: label on line N, value on line N+1 or N+2
    # (some PDFs have a blank line between label and value)
    for field, patterns in NEXTLINE_PATTERNS_ORDERED:
        if field in result:
            continue  # Already found via same-line pattern
        for pattern in patterns:
            for i, line in enumerate(lines):
                if not re.search(pattern, line, re.IGNORECASE):
                    continue
                # Look at the next few non-empty lines
                for j in range(i + 1, min(i + 4, len(lines))):
                    next_line = lines[j].strip()
                    if not next_line:
                        continue
                    if field == "invoice_number":
                        m = re.match(r"(\d[\d/-]*)", next_line)
                    elif field in ("invoice_date", "due_date"):
                        m = re.match(r"(" + "|".join(DATE_PATTERNS) + ")", next_line)
                    elif field in ("bankgiro", "plusgiro", "ocr_number"):
                        # Never an org.nr (6-4 digits) printed on the line, the buyer's or
                        # the supplier's, taken for a bankgiro (4-4 digits). A ten-digit OCR
                        # reference looks like an org.nr, so for OCR only the known ones count.
                        orgs = {k for k in own_keys if k.isdigit()}
                        if field != "ocr_number":
                            orgs |= {re.sub(r"\D", "", o) for o in re.findall(ORG_VALUE, lines[j])}
                        if result.get("org_number"):
                            orgs.add(re.sub(r"\D", "", result["org_number"]))
                        label = re.search(pattern, line, re.IGNORECASE)
                        value = _nextline_number(field, label.start(), lines[j], orgs)
                        if value:
                            result[field] = value
                        break
                    else:
                        m = None
                    if m:
                        value = m.group(1).strip()
                        if field in ("invoice_date", "due_date"):
                            set_date(field, value)
                            break
                        result[field] = value
                        break
                    break  # Only check first non-empty line after label
            if field in result:
                break

    # Loopia format: "Netto: Moms % Moms: SEK att betala" header, then "588,00 25.00 147,00 735,00"
    if "subtotal" not in result or "total_amount" not in result:
        for i, line in enumerate(lines):
            if "Netto:" in line and "att betala" in line:
                if i + 1 < len(lines):
                    m = re.match(r"([\d\s.,]+?)\s+[\d.]+\s+([\d\s.,]+?)\s+([\d\s.,]+)$", lines[i + 1].strip())
                    if m:
                        if "subtotal" not in result:
                            parsed = _parse_amount(m.group(1))
                            if parsed:
                                result["subtotal"] = parsed
                        if "vat_amount" not in result:
                            parsed = _parse_amount(m.group(2))
                            if parsed:
                                result["vat_amount"] = parsed
                        if "total_amount" not in result:
                            parsed = _parse_amount(m.group(3))
                            if parsed:
                                result["total_amount"] = parsed
                break

    # Detect currency from € or $ symbols if not already found
    if "currency" not in result:
        if "€" in text:
            result["currency"] = "EUR"
        elif "$" in text and "USD" not in text:
            result["currency"] = "USD"

    # ALSO-specific: "Nummer 11042830" and "Datum 31.03.2026" on same line
    if "invoice_number" not in result:
        m = re.search(r"Nummer\s+(\d{6,})", text)
        if m:
            result["invoice_number"] = m.group(1)
    if "invoice_date" not in result:
        m = re.search(r"\bDatum\s+(\d{2}\.\d{2}\.\d{4})", text)
        if m:
            set_date("invoice_date", m.group(1))
    if "due_date" not in result:
        m = re.search(r"Förfallodatum\s+(\d{2}\.\d{2}\.\d{4})", text)
        if m:
            set_date("due_date", m.group(1))

    # ALSO-specific: "Totalt belopp SEK 3.020,81" or "Totalt belopp 3.020,81SEK"
    if "total_amount" not in result:
        m = re.search(r"Totalt\s+belopp\s+(?:SEK\s+)?([\d\s.,]+?)(?:\s*SEK)?$", text, re.MULTILINE)
        if m:
            parsed = _parse_amount(m.group(1))
            if parsed:
                result["total_amount"] = parsed
    if "subtotal" not in result:
        m = re.search(r"Netto\s+belopp\s+([\d\s.,]+)", text)
        if m:
            parsed = _parse_amount(m.group(1))
            if parsed:
                result["subtotal"] = parsed
    if "vat_amount" not in result:
        # ALSO format: "Moms 25,000 % 2.416,65 604,16" — last number is VAT
        m = re.search(r"Moms\s+[\d,]+\s*%\s+[\d\s.,]+\s+([\d.,]+)", text)
        if m:
            parsed = _parse_amount(m.group(1))
            if parsed:
                result["vat_amount"] = parsed

    # Pick supplier VAT from all "VAT Reg. No.: <CC>NNNN" matches.
    # Hetzner & similar foreign invoices print the SUPPLIER VAT in the footer
    # and the CUSTOMER (the receiving company's SE...) VAT in the header. Prefer
    # non-own, non-SE candidates, and prefer the LAST occurrence (footer).
    vat_candidates = re.findall(r"VAT\s*Reg\.?\s*No\.?[\s:]*([A-Z]{2}\d{6,12})",
                                text, re.IGNORECASE)
    if vat_candidates:
        # Prefer non-own, non-SE; fall back to last occurrence. is_own_id compares
        # normalized keys (spaces/dashes stripped, upper; SE…01 and the bare org.nr
        # count as the same number) — company.vat is often stored with spaces and
        # company_registry as NNNNNN-NNNN.
        non_own = [v for v in vat_candidates if not is_own_id(v, own_keys)]
        non_se = [v for v in non_own if not v.upper().startswith("SE")]
        if non_se:
            result["org_number"] = non_se[-1]
        elif non_own and "org_number" not in result:
            # Only Swedish candidates left and nothing else found. Our own numbers
            # are already filtered out (own_ids), so the remaining one is the
            # supplier's. When the regex extraction already found a number (e.g.
            # "Organisationsnummer") keep it rather than guess between parties.
            result["org_number"] = non_own[-1]
        # else: only own VAT found — leave any prior org_number value alone

    # Inget org.nr alls: svenskt momsreg.nr ("Momsreg.nr.: SE556000000001")
    if "org_number" not in result:
        for m in re.finditer(SE_VAT_LABEL + r"\.?[\s.:]*([A-Z]{2}\s?\d[\d\s]{6,13}\d)",
                             text, re.IGNORECASE):
            v = re.sub(r"\s+", "", m.group(1)).upper()
            if not is_own_id(v, own_keys):
                result["org_number"] = v
                break

    # Extract supplier/vendor name from the PDF
    # Look for the first company-like name (ending in AB, AS, GmbH, Ltd, etc.)
    # at the top of the document, but NOT our own company
    if "vendor_name" not in result:
        for line in lines[:15]:
            stripped = line.strip()
            if re.search(r"\b(?:AB|AS|GmbH|Ltd|Inc|LLC|Oy|A/S)\b", stripped) and not _mentions_own_name(stripped, own_name_keys):
                # Take up to and including the company suffix
                m = re.match(r"(.+?\b(?:AB|AS|GmbH|Ltd|Inc|LLC|Oy|A/S)\b)", stripped)
                result["vendor_name"] = m.group(1).strip() if m else stripped.split("  ")[0].strip()
                break

    if "vendor_name" not in result:
        # Swedish: "Company AB  Organisationsnummer  Bankgiro"
        for line in lines:
            m = re.match(r"^(.+?)\s+" + ORG_LABEL + r"[\s.:]*(\d{6}[\s-]?\d{4})?", line, re.IGNORECASE)
            if m:
                name = m.group(1).strip().rstrip(",")
                if m.group(2) and is_own_id(m.group(2), own_keys):
                    continue  # köparens block ("Sweden ORG.NR: <eget nummer>")
                if name and len(name) > 1 and not _mentions_own_name(name, own_name_keys):
                    result["vendor_name"] = name
                    break

    if "vendor_name" not in result:
        for i, line in enumerate(lines):
            stripped = line.strip()
            if stripped.upper() in ("INVOICE", "FAKTURA", "TAX INVOICE", "CREDIT NOTE"):
                for j in range(i + 1, min(i + 4, len(lines))):
                    candidate = lines[j].strip()
                    if (candidate and len(candidate) > 2
                            and not candidate.startswith(("http", "www"))
                            and not _mentions_own_name(candidate, own_name_keys)
                            # "PERIOD: 2026-04-01", "DATUM: …" är fält, inte ett namn
                            and not re.search(r"\w\s*:\s*\d", candidate)):
                        result["vendor_name"] = candidate
                        break
                break

    return result


# ── AI validation/extraction ────────────────────────────────────────────────

AI_PROVIDER = os.environ.get("INVOICE_AI_PROVIDER", "staik")
VENICE_API_KEY = os.environ.get("VENICE_API_KEY", "")  # set in Odoo settings (invoice_ocr.venice_api_key) or the environment, never here
VENICE_MODEL = os.environ.get("VENICE_MODEL", "google-gemma-3-27b-it")
OLLAMA_URL = os.environ.get("OLLAMA_URL", "http://localhost:11434")
OLLAMA_MODEL = os.environ.get("OLLAMA_MODEL", "qwen2.5:7b")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_MODEL = os.environ.get("OPENAI_MODEL", "gpt-4o-mini")
STAIK_URL = os.environ.get("STAIK_URL", "https://api.staik.se/v1")
STAIK_API_KEY = os.environ.get("STAIK_API_KEY", "")
# Reasoning-varianten. Basmodellen svarar direkt utan att rakna och far da fel pa
# flertermssummor; matt 2026-09-04 gav den 18/24 mot 22/24 for -thinking.
STAIK_MODEL = os.environ.get("STAIK_MODEL", "qwen3.6:35b-a3b-thinking")
# Hard cap on one staik call. The Odoo model runs this synchronously inside the
# create-transaction, so a call blocks a worker; 300 s let a hung request pin a
# worker for minutes. 120 s still covers the slowest reasoning runs we have
# measured (typically well under a minute).
STAIK_TIMEOUT = int(os.environ.get("STAIK_TIMEOUT", "120"))
# If the first AI call already took at least this many seconds, skip the retry —
# two calls at STAIK_TIMEOUT would otherwise block the upload path for minutes.
RETRY_SKIP_SECONDS = float(os.environ.get("INVOICE_AI_RETRY_SKIP_SECONDS", "60"))
# Ett svar under den har granden betyder att modellen hoppade over resonemanget.
# Samtliga korrekta svar i matningen lag pa 3400-7200 completion-tokens, de tva
# felaktiga pa 462 och 649.
STAIK_MIN_COMPLETION_TOKENS = int(os.environ.get("STAIK_MIN_COMPLETION_TOKENS", "1000"))

# Any other provider that speaks OpenAI's /chat/completions (Mistral, Groq, OpenRouter, Together,
# DeepSeek, Azure OpenAI, Anthropic's compatibility layer, a local vLLM or LM Studio, ...):
# provider "openai_compatible" with a base URL, key and model. Odoo's settings page fills these
# into the per-run config (see default_config); the globals are only env-derived defaults.
AI_BASE_URL = os.environ.get("INVOICE_AI_BASE_URL", "")
AI_API_KEY = os.environ.get("INVOICE_AI_API_KEY", "")
AI_MODEL = os.environ.get("INVOICE_AI_MODEL", "")
# Per-call cap for every provider except staik (STAIK_TIMEOUT). Same reasoning as
# STAIK_TIMEOUT: the upload path is synchronous, so a call must never pin a worker for long.
AI_TIMEOUT = int(os.environ.get("INVOICE_AI_TIMEOUT", "120"))
VENICE_URL = "https://api.venice.ai/api/v1"
OPENAI_URL = "https://api.openai.com/v1"

# The receiving company, so its own name/VAT number printed on the invoice is never taken
# for the supplier. The Odoo model passes the invoice company's identities (the company and
# its branches) in the per-run config ("own_ids"/"own_names", see default_config); these
# globals are only the fallback for standalone use: set INVOICE_OCR_OWN_COMPANY ("Acme AB")
# and INVOICE_OCR_OWN_VAT ("SE5566...", comma-separated).
OWN_COMPANY = os.environ.get("INVOICE_OCR_OWN_COMPANY", "").strip().lower()
OWN_VAT_NUMBERS = {v.strip().upper() for v in os.environ.get("INVOICE_OCR_OWN_VAT", "").split(",") if v.strip()}


def default_config():
    """Per-run-konfiguration. Modul-globalerna (env-read vid import) är defaults.

    Odoo-modellen bygger en egen dict från ir.config_parameter + res.company och
    skickar in den till extract_invoice_data — den muterar INTE modul-globalerna,
    som delas av alla körningar i worker-processen. För standalone-bruk räcker
    extract_invoice_data(pdf) utan config.
    """
    return {
        "provider": AI_PROVIDER,
        "venice_api_key": VENICE_API_KEY,
        "venice_model": VENICE_MODEL,
        "ollama_url": OLLAMA_URL,
        "ollama_model": OLLAMA_MODEL,
        "openai_api_key": OPENAI_API_KEY,
        "openai_model": OPENAI_MODEL,
        "base_url": AI_BASE_URL,
        "api_key": AI_API_KEY,
        "model": AI_MODEL,
        "timeout": AI_TIMEOUT,
        "staik_url": STAIK_URL,
        "staik_api_key": STAIK_API_KEY,
        "staik_model": STAIK_MODEL,
        "staik_timeout": STAIK_TIMEOUT,
        "retry_skip_seconds": RETRY_SKIP_SECONDS,
        "staik_min_completion_tokens": STAIK_MIN_COMPLETION_TOKENS,
        # The receiving company, never the supplier: its org/VAT numbers and names.
        # The Odoo model fills these per run from res.company (the bill's company and
        # its branches); the env-derived globals are the standalone fallback.
        "own_ids": sorted(OWN_VAT_NUMBERS),
        "own_names": _default_own_names(),
        "text_limit": TEXT_LIMIT,
        # The account codes the model may choose, [(code, hint)]; None = DEFAULT_ACCOUNTS.
        "accounts": None,
        "max_ocr_pages": MAX_OCR_PAGES,
        "ocr_scale": OCR_SCALE,
        # Time and size budgets (#9, #26). call_timeout: one provider call, for every
        # provider (None = timeout / staik_timeout above); total_deadline: one document,
        # extraction and provider calls together; the rest bound the text extraction.
        "call_timeout": None,
        "total_deadline": TOTAL_DEADLINE,
        "extract_time_budget": EXTRACT_TIME_BUDGET,
        "max_text_pages": MAX_TEXT_PAGES,
        "max_page_pixels": MAX_PAGE_PIXELS,
        "tesseract_timeout": TESSERACT_TIMEOUT,
        "max_image_bytes": MAX_IMAGE_BYTES,
        # The document's DocumentRun (deadline and notes), set by the entry points.
        "run": None,
    }


def _cfg(config):
    """Merge a partial config dict over the defaults; None values are ignored."""
    cfg = default_config()
    if config:
        cfg.update({k: v for k, v in config.items() if v is not None})
    return cfg


# Provider settings the Odoo module exposes. Each <key> is a config key, the system
# parameter "invoice_ocr.<key>" and the settings-form field "invoice_ocr_<key>".
PROVIDER_SETTINGS = (
    "provider",
    "staik_api_key", "staik_model",
    "venice_api_key", "venice_model",
    "openai_api_key", "openai_model",
    "base_url", "api_key", "model",
    "ollama_url", "ollama_model",
)


# Numeric limits the Odoo module reads from the system parameters "invoice_ocr.<key>" (#9,
# #26); the settings page has fields for call_timeout and total_deadline.
LIMIT_SETTINGS = (
    "call_timeout", "total_deadline", "extract_time_budget", "max_text_pages",
    "max_ocr_pages", "max_page_pixels", "tesseract_timeout", "max_image_bytes",
)


def _positive_number(value):
    """`value` as a positive number (int when integral), else None: '', 0, 'abc' keep the default."""
    if isinstance(value, bool) or value in (None, ""):
        return None
    try:
        number = float(str(value).strip())
    except ValueError:
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return int(number) if number == int(number) else number


def config_from_settings(get, base=None):
    """Per-run config from settings: `get(key)` returns the value for a PROVIDER_SETTINGS or
    LIMIT_SETTINGS key.

    Empty values keep the default (env-derived global or `base`), so a run with the
    settings saved behaves exactly like the Verify button with the same values on the
    form; a limit that is not a positive number keeps it too. Nothing is written to the
    module globals.
    """
    cfg = _cfg(base)
    for key in PROVIDER_SETTINGS:
        value = get(key)
        if value:
            cfg[key] = value.strip() if isinstance(value, str) else value
    for key in LIMIT_SETTINGS:
        value = _positive_number(get(key))
        if value is not None:
            cfg[key] = value
    return cfg


# The default account list: Swedish BAS codes with a hint for the model each. The Odoo
# module sends only the ones that exist in the bill company's chart, and the list can be
# replaced in the settings (invoice_ocr.account_list, one "code: hint" per line).
DEFAULT_ACCOUNTS = (
    ("4000", "Inköp av varor från Sverige (physical goods, hardware)"),
    ("4515", "Inköp av varor från annat EU-land, 25%"),
    ("4535", "Inköp av tjänster från annat EU-land, 25%"),
    ("4545", "Import av varor, 25% moms"),
    ("5010", "Lokalhyra (office rent)"),
    ("5252", "Leasing av datorer (computer leasing)"),
    ("5410", "Förbrukningsinventarier (consumables, small equipment)"),
    ("5420", "Programvaror (packaged software, on-premise licenses)"),
    ("5610", "Personbilar (vehicle costs)"),
    ("5810", "Biljetter (train, flight, bus, taxi, public transport tickets)"),
    ("5820", "Hyrbilskostnader (car hire)"),
    ("5831", "Kost och logi i Sverige (hotel/accommodation in Sweden)"),
    ("5832", "Kost och logi i utlandet (hotel/accommodation abroad)"),
    ("5890", "Övriga resekostnader (other travel costs)"),
    ("6110", "Kontorsmateriel (office supplies, paper, toner)"),
    ("6211", "Fast telefoni (fixed-line telephony)"),
    ("6212", "Mobiltelefon (mobile phone subscriptions and call charges)"),
    ("6230", "Datakommunikation (internet, broadband)"),
    ("6231", "Datamolntjänster (cloud services, SaaS, hosting, domains, security software "
             "subscriptions like SentinelOne/Lookout/M365)"),
    ("6250", "Postbefordran (postage)"),
    ("6310", "Företagsförsäkringar (business insurance)"),
    ("6420", "Ersättningar till revisor (audit fees)"),
    ("6530", "Redovisningstjänster (accounting services)"),
    ("6540", "IT-tjänster (IT consulting, managed services)"),
    ("6570", "Bankkostnader (bank fees only)"),
    ("6910", "Licensavgifter och royalties"),
    ("6990", "Övriga externa kostnader (reminder/late-payment fees, other charges)"),
    ("7570", "Premier för arbetsmarknadsförsäkringar (Fora, Collectum etc)"),
)


def parse_account_list(text):
    """[(code, hint)] from the settings text: one account per line, "code: hint" (or
    "code = hint", or just the code). Blank lines and lines starting with # are skipped,
    and so is a line that does not start with an account code. Codes are unique."""
    accounts, seen = [], set()
    for raw in str(text or "").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        m = re.match(r"(\d{3,})\s*(?:[:=–-]\s*)?(.*)$", line)
        if not m or m.group(1) in seen:
            continue
        seen.add(m.group(1))
        accounts.append((m.group(1), m.group(2).strip()))
    return accounts


def accounts_in_chart(accounts, chart_codes, sub_account_codes=None):
    """The `accounts` whose code exists in the chart: the code itself (`chart_codes`), or
    a longer code under it (6540 for a chart that has 65400) among `sub_account_codes`
    (default: all chart codes; the Odoo module passes only expense accounts, so a BAS
    purchase code is never matched to another chart's 400000 sales account). Order and
    hints are kept."""
    codes = {str(c) for c in chart_codes or () if c}
    subs = codes if sub_account_codes is None else {str(c) for c in sub_account_codes if c}
    return [(code, hint) for code, hint in accounts or ()
            if code in codes or any(c.startswith(code) and c != code for c in subs)]


def _accounts(cfg):
    """The run's account list: the config's (filtered by the Odoo module), else the default."""
    accounts = cfg.get("accounts")
    return DEFAULT_ACCOUNTS if accounts is None else accounts


def invoice_json_schema(accounts=DEFAULT_ACCOUNTS):
    """The answer's JSON schema; account_code is limited to the codes of `accounts` (#24)."""
    codes = [code for code, _hint in accounts or ()]
    line_props = {
        "description": {"type": "string"},
        "quantity": {"type": "number"},
        "unit_price": {"type": "number"},
        "amount": {"type": "number"},
        "vat_rate": {"type": "number"},
        "account_code": {"type": "string", "enum": codes} if codes else {"type": ["string", "null"]},
    }
    required = ["description", "amount", "vat_rate"] + (["account_code"] if codes else [])
    return {
        "type": "object",
        "properties": {
            "vendor_name": {"type": ["string", "null"]},
            "invoice_number": {"type": ["string", "null"]},
            "invoice_date": {"type": ["string", "null"]},
            "due_date": {"type": ["string", "null"]},
            "total_amount": {"type": ["number", "null"]},
            "subtotal": {"type": ["number", "null"]},
            "vat_amount": {"type": ["number", "null"]},
            "currency": {"type": ["string", "null"]},
            "ocr_number": {"type": ["string", "null"]},
            "bankgiro": {"type": ["string", "null"]},
            "plusgiro": {"type": ["string", "null"]},
            "org_number": {"type": ["string", "null"]},
            "lines": {
                "type": "array",
                "items": {"type": "object", "properties": line_props, "required": required},
            },
        },
        "required": ["vendor_name", "total_amount", "subtotal", "vat_amount", "lines"],
    }


# json_schema tvingar fram giltig JSON hos staik. Fritt format ger trasiga svar.
INVOICE_JSON_SCHEMA = invoice_json_schema()


def check_account_codes(data, accounts):
    """Drop an AI line's account_code that is not in `accounts` (the list the model was
    given), so the line gets the company's fallback account. Returns (data, notes)."""
    codes = {code for code, _hint in accounts or ()}
    notes = []
    for line in data.get("lines") or []:
        code = line.get("account_code")
        if code and code not in codes:
            line.pop("account_code")
            if codes:
                notes.append(f"line '{line.get('description') or line.get('amount')}': account "
                             f"{code} is not in the account list – the default account is used")
    return data, notes


EXTRACTION_PROMPT_TEMPLATE = """Extract the following fields from this Swedish vendor invoice. Return ONLY valid JSON, no other text.

Fields:
- vendor_name: company name of the invoice sender / accounting counterparty.
  IMPORTANT: For marketplace invoices (Amazon, eBay, etc.) where one party is
  named as "Moms deklarerat av" / "VAT declared by" / "Tax collected by"
  and another as "Såld av" / "Sold by", USE THE VAT-DECLARING ENTITY as
  vendor_name (e.g., "Amazon EU S.a.r.L."), not the underlying merchant.
  The marketplace is the deemed reseller for VAT purposes.
- invoice_number: fakturanummer
- invoice_date: YYYY-MM-DD
- due_date: YYYY-MM-DD (förfallodatum)
- total_amount: total amount to pay incl VAT (number). This is the amount due.
- subtotal: total amount excl VAT (number). Must equal sum of all line amounts excl VAT.
- vat_amount: total VAT/moms amount (number, 0 if no VAT)
- currency: SEK/EUR/USD
- ocr_number: OCR payment reference (digits only)
- bankgiro: format NNNN-NNNN
- plusgiro: plusgiro number if any
- org_number: Swedish org number format NNNNNN-NNNN
- lines: array of invoice line items, each with:
  - description: what was purchased
  - quantity: number (default 1)
  - unit_price: price per unit excl VAT
  - amount: line total excl VAT
  - vat_rate: VAT percentage (25, 12, 6, or 0)
  - account_code: {accounts}

IMPORTANT:
- vat_rate must be one of 25, 12, 6 or 0 — never any other number. Swedish rates:
    6%  = persontransport (tåg, taxi, buss, inrikesflyg), böcker, tidningar
    12% = hotell och logi, restaurang och catering, livsmedel
    25% = allt annat
  Derive the rate from the PRINTED amounts when the document shows both net and VAT
  (e.g. net 89.62 + VAT 5.38 on a total of 95.00 is 6%, not 25%). Never assume 25%.
- RECEIPTS (kvitton) — e.g. train tickets, taxi, restaurant, store receipts — often have no
  invoice number, no due date, no OCR and no bankgiro. Leave those fields null instead of
  guessing. Use the PURCHASE date as invoice_date; if the document also shows a travel or
  delivery date, the purchase date still wins.
- PÅMINNELSEAVGIFT / förseningsavgift / dröjsmålsränta on a supplier invoice is
  OUTSIDE the scope of VAT: give those lines vat_rate 0, never 25, and account_code
  6990 (or, if 6990 is not in the list, the list's account for other external
  costs). A telecom invoice whose printed VAT is less than 25% of the printed
  net almost always contains such a fee — put it on its own line so the VAT adds up.
- A RABATT / discount / credit line is NOT such a fee. It reduces the price of the
  service it belongs to, so it MUST carry the SAME account_code and the SAME
  vat_rate as that service (typically 25) — never the fee account and never vat_rate 0.
  Best of all: subtract it from that service's own line instead of listing it
  separately.
- subtotal + vat_amount must equal total_amount. If the invoice has fees, charges, or adjustments beyond the line items, include them as separate lines.
- Use the SAME account code for similar services on the same invoice. E.g. if all lines are cloud/SaaS services, use one account (6231 if it is in the list) for all of them including platform fees.
- COMPLETENESS BEATS BREVITY: the `lines` amounts MUST sum to `subtotal`. Never
  drop a printed amount to make the list shorter — aggregate instead. A telecom
  invoice with one block per phone number becomes ONE line per subscriber, using
  that block's printed "Delsumma" as the amount (that subtotal already includes
  the block's discounts and extra subscriptions). Check your arithmetic against
  `subtotal` before answering.
- KEEP `lines` SHORT (max 6 entries). For invoices with many small line items
  (e.g. cloud usage broken down per-project, per-resource), AGGREGATE them by
  natural grouping — e.g. group by project name, by service category, or by
  account_code. Each `lines` entry should represent a meaningful summary, not
  a verbatim copy of every PDF row. The amounts must still sum to subtotal.

If a field cannot be found, set to null.

Invoice text:
"""


def build_extraction_prompt(accounts=DEFAULT_ACCOUNTS):
    """The extraction prompt with `accounts` [(code, hint)] as the only account codes (#24)."""
    if accounts:
        rows = "\n".join(f"    {code} = {hint}" if hint else f"    {code}" for code, hint in accounts)
        block = "use ONLY these account codes from our chart of accounts:\n" + rows
    else:
        block = "null (no accounts to choose from)"
    return EXTRACTION_PROMPT_TEMPLATE.replace("{accounts}", block)


EXTRACTION_PROMPT = build_extraction_prompt()


def _post(url, **kwargs):
    """Single seam for HTTP so tests can fake the provider."""
    import requests

    return requests.post(url, **kwargs)


def resolve_endpoint(config=None):
    """(base_url, api_key, model) for the configured OpenAI-compatible provider.

    Everything comes from the per-run config (see default_config; the module globals are
    only its env-derived defaults). Presets carry their URL and default model;
    "openai_compatible" takes all three from base_url / api_key / model. Ollama is not an
    OpenAI endpoint (see chat_json).
    """
    cfg = _cfg(config)
    p = (cfg["provider"] or "").strip().lower()
    if p == "staik":
        base, key, model = cfg["staik_url"], cfg["staik_api_key"], cfg["staik_model"]
    elif p == "venice":
        base, key, model = VENICE_URL, cfg["venice_api_key"], cfg["venice_model"]
    elif p == "openai":
        base, key, model = OPENAI_URL, cfg["openai_api_key"], cfg["openai_model"]
    elif p in ("openai_compatible", "custom"):
        base, key, model = cfg["base_url"], cfg["api_key"], cfg["model"]
    else:
        raise ValueError(f"unknown AI provider {cfg['provider']!r}")
    base = (base or "").rstrip("/")
    if not base:
        raise ValueError(f"AI provider {p!r}: no base URL configured")
    if not model:
        raise ValueError(f"AI provider {p!r}: no model configured")
    return base, key or "", model


def _default_timeout(cfg):
    """Per-call cap: the call_timeout setting for every provider; else STAIK_TIMEOUT for
    staik (1.8.1), INVOICE_AI_TIMEOUT for the rest. Each call is also cut to the time the
    document has left (call_timeout)."""
    if cfg.get("call_timeout"):
        return cfg["call_timeout"]
    if (cfg["provider"] or "").strip().lower() == "staik":
        return cfg["staik_timeout"]
    return cfg["timeout"]


# A text longer than the limit is sent as its head and its tail: totals, the VAT summary
# and payment details are usually at the end of an invoice, so the tail matters (#21).
HEAD_SHARE = 2 / 3
TRUNCATION_MARKER = "\n\n[... {omitted} characters of the document left out here ...]\n\n"


def clip_bounds(length, max_chars):
    """(head, tail) characters of a `length`-character text that fit in `max_chars`.

    (length, 0) when the whole text fits; otherwise about 2/3 head and 1/3 tail.
    """
    max_chars = max(int(max_chars or 0), 0)
    if length <= max_chars:
        return length, 0
    head = int(max_chars * HEAD_SHARE)
    return head, max_chars - head


def clip_text(text, max_chars):
    """`text` cut to `max_chars` as head + a clear marker + tail (see clip_bounds)."""
    text = text or ""
    head, tail = clip_bounds(len(text), max_chars)
    if not tail:
        return text[:head]
    omitted = len(text) - head - tail
    return text[:head] + TRUNCATION_MARKER.format(omitted=omitted) + text[len(text) - tail:]


def chat_json(prompt, text, schema, schema_name, max_tokens=8000, max_chars=None, timeout=None,
              config=None):
    """One structured-output call to the configured provider.

    Returns (data, meta): `data` is the parsed JSON dict ({} when unparseable), `meta` has
    served_model, completion_tokens and finish_reason. Handles the provider quirks in one
    place: a 429 is retried once after 15 s; a 400 on `response_format` (provider without
    JSON-schema support) is retried as a plain completion; Ollama uses its own API.

    Provider, keys, URLs and limits are read from `config` (merged over default_config()),
    never from module globals set at run time — those are shared by every run in an Odoo
    worker. `max_chars` defaults to the config's text_limit, `timeout` to the provider's
    per-call cap. A longer text is sent as head + tail (clip_text).

    Every POST, the 429 wait included, fits in the document's deadline (the config's
    DocumentRun, or a new one of total_deadline seconds): each request gets at most the
    time that is left, and none is started — nor the 429 wait — when too little is left
    (DeadlineExceeded). So one call can no longer take 3 × the per-call cap plus 15 s (#9).
    """
    cfg = _cfg(config)
    run = document_run(cfg)
    if max_chars is None:
        max_chars = cfg["text_limit"]
    timeout = timeout or _default_timeout(cfg)
    if (cfg["provider"] or "").strip().lower() == "ollama":
        return _ollama_chat_json(prompt, text, schema, max_chars, timeout, cfg)
    base, key, model = resolve_endpoint(cfg)
    headers = {"Content-Type": "application/json"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    body = {
        "model": model,
        "messages": [{"role": "user", "content": prompt + clip_text(text, max_chars)}],
        "max_tokens": max_tokens,
        "temperature": 0,
        "response_format": {"type": "json_schema", "json_schema": {"name": schema_name, "schema": schema}},
    }
    url = f"{base}/chat/completions"
    r = _post(url, headers=headers, json=body, timeout=call_timeout(run, timeout))
    if r.status_code == 429:
        if run.remaining() - RATE_LIMIT_WAIT < MIN_CALL_SECONDS:
            raise DeadlineExceeded(
                f"the AI provider is rate limiting (HTTP 429) and the time limit per "
                f"document ({run.total:.0f} s) leaves no time to wait and retry")
        time.sleep(RATE_LIMIT_WAIT)
        r = _post(url, headers=headers, json=body, timeout=call_timeout(run, timeout))
    if r.status_code == 400 and "response_format" in body:
        logger.info("%s rejected response_format — retrying without JSON schema", base)
        body = {k: v for k, v in body.items() if k != "response_format"}
        r = _post(url, headers=headers, json=body, timeout=call_timeout(run, timeout))
    r.raise_for_status()
    j = r.json()
    choice = j["choices"][0]
    finish = choice.get("finish_reason")
    if finish == "length":
        logger.warning("Answer from %s was cut by max_tokens=%s; the JSON is incomplete.", model, max_tokens)
    # staik faller TYST tillbaka till sin default-modell vid okant modellnamn, och
    # model-faltet speglar basmodellen aven for -thinking. Antalet tokens ar darfor
    # enda tillforlitliga tecknet pa att resonemanget faktiskt kordes.
    meta = {
        "served_model": j.get("model"),
        "completion_tokens": (j.get("usage") or {}).get("completion_tokens"),
        "finish_reason": finish,
        "model": model,
    }
    return _parse_ai_json(choice["message"]["content"] or ""), meta


def _ollama_chat_json(prompt, text, schema, max_chars, timeout, cfg):
    """Ollama's native API; `format` takes a JSON schema since 0.5."""
    model = cfg["ollama_model"]
    r = _post(
        f"{(cfg['ollama_url'] or '').rstrip('/')}/api/chat",
        json={
            "model": model, "stream": False, "format": schema or "json",
            "options": {"temperature": 0},
            "messages": [{"role": "user", "content": prompt + clip_text(text, max_chars)}],
        },
        timeout=call_timeout(document_run(cfg), timeout),
    )
    r.raise_for_status()
    j = r.json()
    meta = {"served_model": j.get("model"), "completion_tokens": j.get("eval_count"),
            "finish_reason": j.get("done_reason"), "model": model}
    return _parse_ai_json((j.get("message") or {}).get("content") or ""), meta


def verify_provider(config=None):
    """Cheap round-trip for the settings page: which model actually answers, and how fast.

    Exposes staik's silent fallback (an unknown model name is served by the default model,
    visible only in `served_model`) and any URL/key mistake before a real invoice is sent.
    The settings page passes a config built from the form's (possibly unsaved) values; this
    function never writes module globals, so an unsaved key is never used by real runs.
    """
    cfg = _cfg(config)
    provider = cfg["provider"]
    t0 = time.time()
    schema = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}
    try:
        data, meta = chat_json('Reply with the JSON object {"ok": true} and nothing else.\n', "", schema, "ping",
                               max_tokens=300, max_chars=0, timeout=60, config=cfg)
    except Exception as e:  # noqa: BLE001 — the whole point is to report the failure
        return {"ok": False, "provider": provider, "error": str(e)[:300], "latency_s": round(time.time() - t0, 1)}
    return {
        "ok": bool(isinstance(data, dict) and data.get("ok") is True),
        "provider": provider, "model_requested": meta.get("model"), "model_served": meta.get("served_model"),
        "completion_tokens": meta.get("completion_tokens"), "latency_s": round(time.time() - t0, 1),
    }


def _parse_ai_json(content):
    """Extract JSON from AI response (may have markdown fences or thinking)."""
    # Try to find JSON between ```json ... ``` first
    m = re.search(r'```(?:json)?\s*(\{.*\})\s*```', content, re.DOTALL)
    json_str = m.group(1) if m else None

    if not json_str:
        # Find the outermost { ... } by matching braces
        start = content.find('{')
        if start >= 0:
            depth = 0
            end = start
            for i in range(start, len(content)):
                if content[i] == '{':
                    depth += 1
                elif content[i] == '}':
                    depth -= 1
                    if depth == 0:
                        end = i + 1
                        break
            json_str = content[start:end]

    if not json_str:
        return {}

    try:
        data = json.loads(json_str)
        # Convert string amounts to float
        for key in ("total_amount", "subtotal", "vat_amount"):
            if key in data and data[key] is not None:
                try:
                    data[key] = float(data[key])
                except (ValueError, TypeError):
                    data[key] = None
        # Remove null values
        return {k: v for k, v in data.items() if v is not None}
    except json.JSONDecodeError as e:
        logger.warning("Kunde inte tolka AI-svaret som JSON (%s). Forsta 200 tecken: %s",
                       e, json_str[:200].replace("\n", " "))
        return {}


def _strip_meta(data):
    """Ta bort interna diagnosfalt sa de inte foljer med in i fakturan."""
    if isinstance(data, dict):
        return {k: v for k, v in data.items() if not k.startswith("_")}
    return data


# ── VAT treatment of a vendor bill (BAS accounts, l10n_se taxes) ─────────────
# Every line gets its own tax, chosen after its final account is known (#8): goods or
# services is read off the account (BAS), the region off the supplier's country, and
# whether VAT was charged off the document's printed VAT (#22). The tax is named by its
# l10n_se template id (data/template/account.tax-se.csv), which the Odoo module resolves
# per company.

EU_COUNTRY_CODES = frozenset({
    "AT", "BE", "BG", "CY", "CZ", "DE", "DK", "EE", "ES", "FI", "FR", "GR", "HR", "HU", "IE",
    "IT", "LT", "LU", "LV", "MT", "NL", "PL", "PT", "RO", "SE", "SI", "SK",
})
# VAT number prefixes that are not the ISO country code: the inverse of Odoo's
# EU_EXTRA_VAT_CODES (odoo/addons/base/models/res_partner.py).
VAT_PREFIX_COUNTRY = {"EL": "GR", "XI": "GB"}
SWEDISH_VAT_RATES = (25, 12, 6)

# Goods purchases (BAS): 4000–4499, EU goods 4510–4529 (4515–4517) and import of goods
# 4540–4549 (4545–4547). Everything else is a service: 4530–4539 (4531–4533 services from
# outside the EU, 4535–4537 from the EU) and the other cost accounts, 5xxx–7xxx.
GOODS_ACCOUNT_RANGES = ((4000, 4499), (4510, 4529), (4540, 4549))
# Exempt or out-of-scope costs: a line with VAT rate 0 on these accounts gets no tax, also
# from a foreign supplier (no reverse charge on a reminder fee, bank charge, insurance
# premium or interest): insurance 63xx, bank charges 657x, other external costs and fees
# 699x, statutory insurance premiums 75xx, financial items 8xxx.
OUT_OF_SCOPE_ACCOUNT_RANGES = ((6300, 6399), (6570, 6579), (6990, 6999), (7500, 7599),
                               (8000, 8999))
# The BAS purchase accounts per region, kind and rate, and the 45xx families they form.
FOREIGN_PURCHASE_ACCOUNTS = {
    ("eu", "goods"): {25: "4515", 12: "4516", 6: "4517"},
    ("eu", "services"): {25: "4535", 12: "4536", 6: "4537"},
    ("non_eu", "goods"): {25: "4545", 12: "4546", 6: "4547"},
    ("non_eu", "services"): {25: "4531", 12: "4532", 6: "4533"},
}
_FOREIGN_PURCHASE_RANGES = ((4510, 4529), (4530, 4539), (4540, 4549))


def country_from_vat(vat):
    """The ISO country code of a VAT number's prefix ('EL…' is GR, 'XI…' GB), or None."""
    m = re.match(r"\s*([A-Za-z]{2})(?=[\s\d])", str(vat or ""))
    if not m:
        return None
    prefix = m.group(1).upper()
    return VAT_PREFIX_COUNTRY.get(prefix, prefix)


def vat_region(country_code):
    """'domestic' (Sweden, or unknown), 'eu' (another EU country) or 'non_eu'."""
    code = (country_code or "").strip().upper()
    if not code or code == "SE":
        return "domestic"
    return "eu" if code in EU_COUNTRY_CODES else "non_eu"


def account_number(code):
    """The first four digits of an account code as a number ('45150' → 4515), or None."""
    m = re.match(r"\s*(\d{4})", str(code or ""))
    return int(m.group(1)) if m else None


def _in_ranges(code, ranges):
    n = account_number(code)
    return n is not None and any(lo <= n <= hi for lo, hi in ranges)


def is_goods_account(code):
    """True for a goods-purchase account (see GOODS_ACCOUNT_RANGES); False means a service."""
    return _in_ranges(code, GOODS_ACCOUNT_RANGES)


def is_out_of_scope_account(code):
    """True for an account of exempt or out-of-scope costs (see OUT_OF_SCOPE_ACCOUNT_RANGES)."""
    return _in_ranges(code, OUT_OF_SCOPE_ACCOUNT_RANGES)


def line_vat_rate(value):
    """A line's VAT rate as a whole number (25.0 → 25), or None."""
    number = _to_number(value)
    return None if number is None else int(round(number))


def bill_vat_treatment(region, vat_charged, swedish_vat):
    """How the bill's VAT is booked: 'domestic', 'reverse_charge' or 'foreign_vat'.

    * domestic supplier: Swedish input VAT ('domestic');
    * foreign supplier that charged VAT: Swedish VAT (it shows a Swedish VAT number) is
      booked like a domestic purchase; any other VAT is foreign VAT, which is never
      deductible in Sweden and is booked as part of the cost ('foreign_vat');
    * foreign supplier that charged no VAT: the buyer accounts for it ('reverse_charge').
    """
    if region == "domestic":
        return "domestic"
    if vat_charged:
        return "domestic" if swedish_vat else "foreign_vat"
    return "reverse_charge"


def reverse_charge_rate(rate):
    """The Swedish rate a reverse-charge line is taxed at: the line's rate if Swedish, else 25."""
    return rate if rate in SWEDISH_VAT_RATES else 25


def line_gets_reverse_charge(account_code, rate):
    """False for a VAT-0 line on an out-of-scope account (fee, bank charge …), else True."""
    return bool(rate) or not is_out_of_scope_account(account_code)


def line_tax_xmlid(account_code, rate, region, treatment):
    """The l10n_se template id of the purchase tax for one line, or None (no tax).

    `account_code` is the line's final account: it decides goods or services.
      domestic        → purchase_tax_<rate>_<goods|services>   (input VAT, box 48)
      reverse charge  → purchase_<goods|services>_tax_<rate>_EC  (EU: box 20 / 21)
                        purchase_<goods|services>_tax_<rate>_NEC (outside the EU: box 50 / 22)
      foreign VAT     → None (in the cost, no Swedish VAT)
    A domestic line at 0 % or at a rate that is not Swedish gets no tax; a reverse-charge
    line at 0 % on an out-of-scope account gets none either, any other one is taxed at its
    Swedish rate or 25 %.
    """
    kind = "goods" if is_goods_account(account_code) else "services"
    if treatment == "domestic":
        return f"purchase_tax_{rate}_{kind}" if rate in SWEDISH_VAT_RATES else None
    if treatment != "reverse_charge" or region not in ("eu", "non_eu"):
        return None
    if not line_gets_reverse_charge(account_code, rate):
        return None
    suffix = "EC" if region == "eu" else "NEC"
    return f"purchase_{kind}_tax_{reverse_charge_rate(rate)}_{suffix}"


def remap_account_code(code, region="domestic", rate=25):
    """The BAS purchase account for a foreign purchase on `code`, else `code` unchanged.

    For a reverse-charge purchase from another EU country (`region` 'eu') or from outside
    the EU ('non_eu'): domestic goods purchases 4000–4069/4090–4099 become EU goods
    (4515/4516/4517) or import of goods (4545/4546/4547) by `rate`, and an account of the
    wrong 45xx family or rate is moved to the right one: EU goods 4510–4529, import of
    goods 4540–4549, services 4530–4539 (4531–4533 outside the EU, 4535–4537 EU). The
    4000–4499 goods accounts stay goods and 4530–4539 services stay services. Other cost
    accounts (5xxx–7xxx, e.g. 6540 IT services) and BAS 2026's foreign goods-for-resale
    accounts 4070–4089 are left as they are: the line's tax reports the purchase.
    """
    if region not in ("eu", "non_eu"):
        return code
    n = account_number(code)
    if n is None:
        return code
    foreign = any(lo <= n <= hi for lo, hi in _FOREIGN_PURCHASE_RANGES)
    if not foreign and not (4000 <= n <= 4069 or 4090 <= n <= 4099):
        return code
    kind = "goods" if is_goods_account(code) else "services"
    accounts = FOREIGN_PURCHASE_ACCOUNTS[(region, kind)]
    return accounts.get(rate) or accounts[25]


def account_candidates(code, region, rate, treatment):
    """Account codes to try for a line, best first: the remapped BAS account at the line's
    rate, at 25 %, then the code as given. Only reverse-charge lines that get a tax are
    remapped (a fee line on 6990 stays, and so does a line booked with foreign VAT)."""
    if not code:
        return []
    out = []
    if treatment == "reverse_charge" and line_gets_reverse_charge(code, rate):
        out = [remap_account_code(code, region, reverse_charge_rate(rate)),
               remap_account_code(code, region, 25)]
    return list(dict.fromkeys([*out, str(code)]))


def document_vat(data):
    """The VAT amount on the document: printed (regex) first, else the AI's, else total −
    net. None when nothing is known."""
    printed = (data or {}).get("_printed") or {}
    for source in (printed, data or {}):
        vat = _num(source.get("vat_amount"))
        if vat is not None:
            return vat
    for source in (printed, data or {}):
        total, net = _num(source.get("total_amount")), _num(source.get("subtotal"))
        if total is not None and net is not None:
            return round(total - net, 2)
    return None


def vat_was_charged(vat_total, net=None):
    """True when the document's VAT (document_vat) is real VAT, not öre rounding: at least
    1.00, or at least 1 % of the net."""
    if vat_total is None or vat_total <= 0.005:
        return False
    return vat_total >= 1.0 or bool(net) and vat_total >= 0.01 * abs(net)


def spread_amount(amounts, total):
    """`total` split over `amounts` in proportion, in cents; the remainder goes to the
    largest amount, so the shares add up to `total` exactly."""
    if not amounts:
        return []
    base = sum(amounts)
    shares = ([round(total * a / base, 2) for a in amounts] if base
              else [0.0] * len(amounts))
    largest = max(range(len(amounts)), key=lambda i: abs(amounts[i]))
    shares[largest] = round(shares[largest] + total - sum(shares), 2)
    return shares


# A Swedish VAT number: SE + org.nr (10 digits) + 01, as printed ('SE 999999-0014 01')
SE_VAT_RE = re.compile(r"(?<![A-Za-z0-9])SE[ \t]?(\d{6})[ \t-]?(\d{4})[ \t]?01(?!\d)")


def se_vat_numbers(text, own_keys=frozenset()):
    """The Swedish VAT numbers printed in `text` that are not the buyer's own (`own_keys`
    from build_own_ids)."""
    found = []
    for m in SE_VAT_RE.finditer(text or ""):
        vat = f"SE{m.group(1)}{m.group(2)}01"
        if not is_own_id(vat, set(own_keys)) and vat not in found:
            found.append(vat)
    return found


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_number(value):
    """A number from an AI answer: numbers as they are, strings via _parse_amount, else None.

    '1 234,00' (a locale-formatted amount) is 1234.0; booleans, NaN and lists are None.
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        number = _parse_amount(value)
    else:
        return None
    return number if number is not None and math.isfinite(number) else None


# Text fields of the invoice answer; numbers given for identifiers become text ("1033").
_AI_TEXT_FIELDS = ("vendor_name", "invoice_number", "invoice_date", "due_date", "currency",
                   "ocr_number", "bankgiro", "plusgiro", "org_number")
_AI_AMOUNT_FIELDS = ("total_amount", "subtotal", "vat_amount")
_AI_LINE_NUMBERS = ("quantity", "unit_price", "amount", "vat_rate")
_AI_PLACEHOLDERS = ("", "null", "none", "n/a", "unknown")


def _ai_text(value):
    """A text field from the AI: stripped text, integral numbers as digits, else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and math.isfinite(value) and value == int(value):
        value = int(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, str) and value.strip().lower() not in _AI_PLACEHOLDERS:
        return value.strip()
    return None


def _sanitize_ai_line(line):
    """One invoice line from the AI as a clean dict, or None when it has no usable amount."""
    if not isinstance(line, dict):
        return None
    out = {}
    for key in _AI_LINE_NUMBERS:
        number = _to_number(line.get(key))
        if number is not None:
            out[key] = number
    for key in ("description", "account_code"):
        text = _ai_text(line.get(key))
        if text:
            out[key] = text
    if "amount" not in out and "unit_price" in out:
        out["amount"] = round(out["unit_price"] * out.get("quantity", 1.0), 2)
    return out if "amount" in out else None


def _sanitize_ai(data):
    """The AI's invoice answer in the shape the rest of the code expects (#10).

    The schema is not enforced by every provider (and not at all on the plain-completion
    fallback), so the answer can have `lines` that is not a list, lines that are not
    objects, locale-formatted numbers ('1 234,00') and nulls anywhere. Keeps only: the
    known text fields as text, the amounts as numbers (via _parse_amount), `lines` as a
    list of dicts with numeric quantity/unit_price/amount/vat_rate and no null values, and
    the internal diagnostics (keys starting with "_"). Anything else is dropped.
    """
    if not isinstance(data, dict):
        return {}
    out = {k: v for k, v in data.items() if k.startswith("_") and v is not None}
    for key in _AI_TEXT_FIELDS:
        text = _ai_text(data.get(key))
        if text:
            out[key] = text
    for key in _AI_AMOUNT_FIELDS:
        number = _to_number(data.get(key))
        if number is not None:
            out[key] = number
    lines = data.get("lines")
    if isinstance(lines, list):
        out["lines"] = [ln for ln in (_sanitize_ai_line(x) for x in lines) if ln]
    return out


def line_quantity_and_price(line):
    """(quantity, unit price) for a sanitized AI line; the line's `amount` is the truth (#11).

    `amount` is what the answer was checked against (the lines must sum to the net), so
    the created line must come to exactly that. Quantity and unit price are used only
    when they agree with it; otherwise the line becomes 1 × amount. None: no amount.
    """
    amount = _to_number(line.get("amount"))
    if not amount:
        return None
    qty = _to_number(line.get("quantity")) or 1.0
    unit = _to_number(line.get("unit_price"))
    if unit is not None and abs(qty * unit - amount) <= 0.01 * max(abs(qty), 1):
        return qty, unit
    if qty != 1 and abs(round(amount / qty, 2) * qty - amount) <= 0.005:
        return qty, round(amount / qty, 2)
    return 1.0, amount


def _ai_answer_problems(data, reference=None, config=None):
    """Tecken pa att svaret inte gar att lita pa. Tom lista = svaret ser rimligt ut.

    `reference` ar regex-extraktionen, dvs fakturans TRYCKTA belopp. Den ar det
    starkare facit: uppmatt over sju leverantorer och 23 fakturor hade regex ratt
    pa totalen varje gang, medan AI:n hade fel pa fem.

    Att bara kontrollera AI:ns svar mot sig sjalvt racker inte. Matt 2026-09-04:
    en Tele2-korning gav korrekta rader (2121,00) men lade 25 % moms pa en
    momsfri paminnelseavgift — internt konsistent, men totalen blev 2651,25 i
    stallet for 2636,00. En Hetzner-korning rapporterade noll rakt igenom, ocksa
    internt konsistent. Bada hade passerat en kontroll som saknar facit.
    """
    if not isinstance(data, dict) or not data:
        return ["tomt svar"]
    try:
        return _ai_answer_problems_unsafe(data, reference if isinstance(reference, dict) else {},
                                          _cfg(config))
    except Exception as e:  # noqa: BLE001 — a check must never lose the answer (#10)
        return [f"the answer could not be checked ({type(e).__name__}: {e})"]


def _ai_answer_problems_unsafe(data, reference, cfg):
    problems = []

    # Only a reasoning model is expected to spend tokens before answering; a plain model
    # answering in 500 tokens is normal, a "-thinking" model doing so skipped its reasoning.
    ctok = _num(data.get("_completion_tokens"))
    model_name = str(data.get("_served_model") or data.get("_model") or "").lower()
    if (ctok is not None and ctok < cfg["staik_min_completion_tokens"]
            and ("think" in model_name or "reason" in model_name)):
        problems.append(f"bara {ctok:.0f} completion-tokens (resonemanget hoppades over)")

    # 1. Mot fakturans tryckta belopp
    for key, label in (("total_amount", "total"), ("subtotal", "netto"),
                       ("vat_amount", "moms")):
        ref, got = _num(reference.get(key)), _num(data.get(key))
        if ref is None:
            continue
        if got is None:
            problems.append(f"{label} saknas (fakturan visar {ref:.2f})")
        elif abs(got - ref) > 1:
            problems.append(f"{label} {got:.2f} mot fakturans {ref:.2f}")

    # 2. Internt: subtotal + moms ska bli total
    sub, vat, tot = (_num(data.get(k)) for k in ("subtotal", "vat_amount", "total_amount"))
    if None not in (sub, vat, tot) and abs(sub + vat - tot) > 1:
        problems.append(f"{sub:.2f} + {vat:.2f} != {tot:.2f}")

    # 3. Raderna ska summera till nettot — fakturans om det finns, annars AI:ns
    lines = data.get("lines") or []
    if not isinstance(lines, list):
        problems.append("lines is not a list")
        lines = []
    lines = [ln for ln in lines if isinstance(ln, dict)]
    net = _num(reference.get("subtotal"))
    if net is None:
        net = sub
    if not lines:
        problems.append("inga rader")
    elif net is not None:
        linesum = sum(_to_number(ln.get("amount")) or 0.0 for ln in lines)
        if abs(linesum - net) > 1:
            problems.append(f"radsumma {linesum:.2f} mot netto {net:.2f}")
    return problems


def _call_provider(text, config=None):
    cfg = _cfg(config)
    accounts = _accounts(cfg)
    data, meta = chat_json(build_extraction_prompt(accounts), text, invoice_json_schema(accounts),
                           "invoice", max_tokens=8000, max_chars=cfg["text_limit"], config=cfg)
    data = _sanitize_ai(data)
    if data:
        # Diagnostics for _ai_answer_problems; stripped before the data reaches the invoice.
        data["_completion_tokens"] = meta.get("completion_tokens")
        data["_served_model"] = meta.get("served_model")
        data["_model"] = meta.get("model")
    return data


def _extract_fields_ai(text, reference=None, config=None):
    """AI validation: extract invoice fields using configured LLM provider.

    Kor om anropet en gang om svaret ser opalitligt ut. Reasoning-modeller hoppar
    ibland over resonemanget och svarar rakt av, vilket ger fel pa flertermssummor.
    Det ar sporadiskt, sa en omkorning racker — men vi behaller det basta av de tva
    svaren i stallet for att blint ta det sista.

    Omkörningen hoppas over om första anropet redan tog retry_skip_seconds, or when
    the document's deadline (#9) leaves less time than the first call took: the re-run
    would most likely not finish.

    A failing first call is raised (the caller keeps the regex fields, notes the failure
    and, in Odoo's background job, tries the document again later); a failing re-run
    keeps the first answer.
    """
    cfg = _cfg(config)
    run = document_run(cfg)
    provider = cfg["provider"]
    t0 = _clock()
    try:
        data = _call_provider(text, cfg)
    except Exception as e:
        logger.warning("AI extraction failed (%s): %s", provider, e)
        raise
    elapsed = _clock() - t0

    problems = _ai_answer_problems(data, reference, cfg)
    if not problems:
        return _strip_meta(data)

    if len(text or "") > cfg["text_limit"]:
        # The retry would see the same cut text and fail the same way (#21).
        logger.warning(
            "AI answer looks unreliable (%s) but the text was cut to %s of %s characters — "
            "no retry, the bill needs a manual check.",
            "; ".join(problems), cfg["text_limit"], len(text))
        return _strip_meta(data)

    if elapsed >= cfg["retry_skip_seconds"]:
        logger.warning(
            "AI-svaret ser opalitligt ut (%s) men forsta anropet tog %.0f s — "
            "hoppar over omkorningen for att inte blockera behandlingen. "
            "Fakturan behover granskas manuellt.",
            "; ".join(problems), elapsed)
        return _strip_meta(data)

    if run.remaining() < elapsed + MIN_CALL_SECONDS:
        logger.warning(
            "AI answer looks unreliable (%s) but only %.0f s of the document's time limit are "
            "left and the first call took %.0f s — no retry, the bill needs a manual check.",
            "; ".join(problems), run.remaining(), elapsed)
        return _strip_meta(data)

    logger.warning("AI-svaret ser opalitligt ut (%s) — kor om en gang",
                   "; ".join(problems))
    try:
        retry = _call_provider(text, cfg)
    except Exception as e:
        logger.warning("Omkorningen misslyckades (%s): %s — behaller forsta svaret",
                       provider, e)
        return _strip_meta(data)

    if not _ai_answer_problems(retry, reference, cfg):
        logger.info("Omkorningen gav ett svar som gar ihop — anvander det")
        return _strip_meta(retry)

    logger.warning("Aven omkorningen ser opalitlig ut — behaller det forsta svaret. "
                   "Fakturan behover granskas manuellt.")
    return _strip_meta(data)


def extract_invoice_data(pdf_b64_or_bytes, config=None, *, own_ids=None, own_names=None):
    """Main entry point: extract invoice data from a PDF.

    Uses regex first, then AI to validate and fill gaps.

    Args:
        pdf_b64_or_bytes: Either base64-encoded string or raw bytes
        config: optional per-run config dict (see default_config): provider
            credentials, the receiving company ("own_ids"/"own_names") and OCR
            limits. Falls back to the module globals (env-read defaults) when omitted.
        own_ids:   the receiving company's org/VAT numbers — never the supplier's
                   (overrides config["own_ids"])
        own_names: the receiving company's names (overrides config["own_names"])

    Returns:
        dict with extracted fields + 'raw_text' key. 'auto_debit' is set to the
        matching phrase when the invoice is debited automatically from the buyer's
        account; '_own_ids_skipped' lists own org/VAT numbers that were ignored;
        '_notes' are remarks for the reviewer (a budget that cut the reading among
        them); '_ai_error' says why the AI step failed, when it did.

    The whole document — text extraction and every provider call — runs within the
    config's total_deadline (a DocumentRun, see chat_json).
    """
    if isinstance(pdf_b64_or_bytes, str):
        pdf_bytes = base64.b64decode(pdf_b64_or_bytes)
    else:
        pdf_bytes = pdf_b64_or_bytes

    cfg = _cfg(config)
    document_run(cfg)
    text = extract_text(pdf_bytes, cfg)
    return extract_invoice_data_from_text(text, config=cfg, own_ids=own_ids, own_names=own_names)


def extract_invoice_data_from_text(text, own_ids=None, own_names=None, config=None):
    """Like extract_invoice_data, on already extracted PDF text (testable without a PDF)."""
    cfg = _cfg(config)
    run = document_run(cfg)
    if own_ids is not None:
        cfg["own_ids"] = own_ids
    if own_names is not None:
        cfg["own_names"] = own_names
    own_keys = build_own_ids(cfg["own_ids"])
    regex_fields = extract_fields(text, config=cfg)
    ai_error = None
    try:
        ai_fields = _sanitize_ai(_extract_fields_ai(text, reference=regex_fields, config=cfg))
    except Exception as e:  # noqa: BLE001 — the regex fields must survive any AI failure (#10)
        logger.warning("AI step failed (%s) — keeping the regex fields", e,
                       exc_info=not isinstance(e, (TimeoutError, OSError)))
        ai_fields, ai_error = {}, f"{type(e).__name__}: {e}"[:300]
    ai_fields, account_notes = check_account_codes(ai_fields, _accounts(cfg))
    final = _merge_fields(text, regex_fields, ai_fields, own_keys)
    notes = [*run.notes, *account_notes]
    if notes:
        final.setdefault("_notes", []).extend(notes)
    if ai_error:
        final["_ai_error"] = ai_error
        final.setdefault("_notes", []).append(
            f"the AI step failed ({ai_error}) – only the values read by the regex were used")
    head, tail = clip_bounds(len(text or ""), cfg["text_limit"])
    if tail:
        logger.warning("Invoice text cut for the AI: %s characters, sent the first %s and the last %s",
                       len(text), head, tail)
        final.setdefault("_notes", []).append(
            f"the document text has {len(text)} characters; the AI saw only the first {head} and "
            f"the last {tail} (text limit {cfg['text_limit']}) – its lines may be incomplete")
    return final


MARKETPLACE_DECLARER_PATTERNS = [
    r"Moms deklarerat av\s+([^\n]+?)(?:\s*Moms\s*#|$)",
    r"VAT declared by\s+([^\n]+?)(?:\s*VAT\s*#|$)",
    r"Tax collected by\s+([^\n]+?)(?:\s*$)",
]


def marketplace_vat_declarer(text):
    """The VAT-declaring entity on a marketplace invoice ("Moms deklarerat av X"), or None."""
    for pattern in MARKETPLACE_DECLARER_PATTERNS:
        m = re.search(pattern, text or "", re.IGNORECASE | re.MULTILINE)
        if m:
            declared = m.group(1).strip().rstrip(",.")
            if len(declared) > 3:
                return declared
    return None


def _valid_field_value(key, value, fields):
    """Is a bankgiro/plusgiro/OCR value usable? OCR references with letters (RF) are."""
    if key == "ocr_number":
        return bool(valid_payment_reference(value, fields.get("invoice_number")))
    return valid_giro_or_ocr(key, value)


def _merge_fields(text, regex_fields, ai_fields, own_keys):
    """Merge the regex and AI fields (pure function, no network calls)."""
    # Merge. Regex vinner pa SIFFROR och identifierare, AI pa beskrivande falt.
    #
    # Uppmatt over sju leverantorer och 23 fakturor: regex pa den tryckta totalen
    # hade ratt varje gang, AI:n hade fel pa fem. AI:n ar daremot bra pa det regex
    # inte klarar — radernas text och kontokod. Tidigare vann AI:n allt, vilket
    # innebar att fakturans belopp kom fran modellens rakning i stallet for fran
    # papperet, och att kontrollen i _check_ocr_totals jamforde raderna mot AI:ns
    # egen uppfattning om totalen i stallet for mot fakturan.
    REGEX_WINS = ("total_amount", "subtotal", "vat_amount",
                  "invoice_date", "invoice_number", "ocr_number",
                  "bankgiro", "plusgiro", "org_number")

    final = {}
    all_keys = set(list(regex_fields.keys()) + list(ai_fields.keys()))
    conflicts = []
    notes = []
    own_skipped = list(regex_fields.get("_own_ids_skipped") or [])
    ambiguous = regex_fields.get("_ambiguous_dates") or {}
    for key in sorted(all_keys):
        if key in ("_own_ids_skipped", "_ambiguous_dates"):
            continue
        ai_val = ai_fields.get(key)
        regex_val = regex_fields.get(key)
        ai_has = key in ai_fields and ai_val is not None
        regex_has = key in regex_fields and regex_val is not None

        if key in ("invoice_date", "due_date"):
            # Only real calendar dates take part (the regex only keeps valid ones).
            ai_date = iso_date(ai_val) if ai_has else None
            if ai_has and not ai_date:
                notes.append(f"{key}: the AI's {ai_val!r} is not a valid date – not used")
            readings = ambiguous.get(key) or []
            if regex_has and ai_date and ai_date in readings:
                # NN/NN/YYYY with both parts <= 12: the AI's reading decides
                if ai_date != regex_val:
                    notes.append(f"{key}: the printed date can be read as {' or '.join(readings)} – "
                                 f"used {ai_date}, as the AI read it")
                final[key] = ai_date
                continue
            if readings:
                notes.append(f"{key}: the printed date can be read as {' or '.join(readings)} – "
                             f"used {readings[0]} (day/month)")
            if regex_has and ai_date and ai_date != regex_val:
                conflicts.append(f"{key}: regex={regex_val} ai={ai_date}")
            if regex_has and (key in REGEX_WINS or not ai_date):
                final[key] = regex_val
            elif ai_date:
                final[key] = ai_date
            continue

        if key == "org_number":
            # Köparens eget org.nr är ALDRIG leverantörens. Regex vinner bara om den
            # hittat ett främmande nummer; annars tar AI:n över om den har ett.
            regex_own = regex_has and is_own_id(regex_val, own_keys)
            ai_own = ai_has and is_own_id(ai_val, own_keys)
            for own, val in ((regex_own, regex_val), (ai_own, ai_val)):
                if own and str(val) not in own_skipped:
                    own_skipped.append(str(val))
            regex_ok = regex_has and not regex_own
            ai_ok = ai_has and not ai_own
            if regex_ok and ai_ok and not (_id_keys(ai_val) & _id_keys(regex_val)):
                conflicts.append(f"{key}: regex={regex_val} ai={ai_val}")
            if regex_ok:
                final[key] = regex_val
            elif ai_ok:
                final[key] = ai_val
            continue

        if ai_has and regex_has and str(ai_val) != str(regex_val):
            conflicts.append(f"{key}: regex={regex_val} ai={ai_val}")

        if key in REGEX_WINS and regex_has:
            final[key] = regex_val
        elif ai_has:
            final[key] = ai_val
        elif regex_has:
            final[key] = regex_val

    if conflicts:
        final["_conflicts"] = conflicts
    if own_skipped:
        final["_own_ids_skipped"] = own_skipped
    if notes:
        final["_notes"] = notes

    # Bankgiro (7–8 digits, Peppol SE-R-009), plusgiro (2–8) and OCR reference (2–25) all
    # end in a mod-10 check digit (#14). A value that fails is never used: the regex value
    # wins when it is valid, else the AI's when that one is, else the field stays empty.
    # A longer "bankgiro" is an account number — on an auto-debit document the LLM has
    # taken the buyer's clearing+account number (or a truncated one) for the bankgiro.
    for key in ("bankgiro", "plusgiro", "ocr_number"):
        candidates = [(src, val) for src, val in (("regex", regex_fields.get(key)),
                                                  ("AI", ai_fields.get(key))) if val]
        if not candidates:
            continue
        valid = [(src, val) for src, val in candidates if _valid_field_value(key, val, final)]
        rejected = [f"'{val}'" for src, val in candidates if (src, val) not in valid]
        if valid:
            src, val = valid[0]
            final[key] = val
            if rejected and src == "AI":
                final.setdefault("_notes", []).append(
                    f"{key} {', '.join(rejected)} from the document fails the length or check-digit "
                    f"test – used the AI's {val}")
        else:
            final.pop(key, None)
            final.setdefault("_notes", []).append(
                f"{key} {' / '.join(dict.fromkeys(rejected))} fails the length or check-digit test – not used")

    # Marketplace invoices (Amazon, eBay, …): the VAT-declaring entity is the vendor, not
    # the merchant who "sold" the item. Searched in the full text, not a slice of it.
    declared = marketplace_vat_declarer(text)
    if declared:
        final["vendor_name"] = declared
        final.setdefault("_conflicts", []).append(
            f"vendor_name: marketplace VAT-declarer override → {declared}")

    # Swedish VAT numbers on the document other than the buyer's: a foreign supplier that
    # shows one is registered for VAT in Sweden, so VAT it charges is Swedish VAT (#22).
    se_vat = se_vat_numbers(text, own_keys)
    if se_vat:
        final["_se_vat_numbers"] = se_vat

    # Dras fakturan automatiskt från köparens konto? Då ska den inte betalas manuellt.
    auto_debit = detect_auto_debit(text)
    if auto_debit:
        final["auto_debit"] = auto_debit

    # Fakturans tryckta belopp separat, sa kontrollen langre fram har ett facit
    # som inte ar samma siffror den ska kontrollera.
    printed = {k: regex_fields[k] for k in ("total_amount", "subtotal", "vat_amount")
               if regex_fields.get(k) is not None}
    if printed:
        final["_printed"] = printed

    final["raw_text"] = text[:2000]
    return final


# ── CLI for testing ──────────────────────────────────────────────────────────

if __name__ == "__main__":
    import sys
    if len(sys.argv) < 2:
        print("Användning: python3 invoice_ocr.py <pdf-fil>")
        sys.exit(1)

    pdf_path = sys.argv[1]
    with open(pdf_path, "rb") as f:
        pdf_bytes = f.read()

    data = extract_invoice_data(pdf_bytes)

    raw = data.pop("raw_text", "")
    print("── Extraherade fält ──")
    for key, value in sorted(data.items()):
        print(f"  {key}: {value}")

    if not data:
        print("  (inga fält hittade)")
        print("\n── Råtext (första 500 tecken) ──")
        print(raw[:500])
