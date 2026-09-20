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
import os
import re

import pdfplumber

logger = logging.getLogger(__name__)

try:
    import pytesseract
    from PIL import Image  # noqa: F401 — availability probe
    HAS_TESSERACT = True
except ImportError:
    HAS_TESSERACT = False


# ── Swedish date formats ─────────────────────────────────────────────────────

DATE_PATTERNS = [
    r"\d{4}-\d{2}-\d{2}",       # 2026-03-01
    r"\d{4}\.\d{2}\.\d{2}",     # 2026.03.01
    r"\d{2}\.\d{2}\.\d{4}",     # 31.03.2026 (ALSO/DE format)
    r"\d{2}/\d{2}/\d{4}",       # 09/04/2026 (Hetzner format)
    r"\d{1,2}\s+\w+\s+\d{4}",   # 1 mars 2026
]

SWEDISH_MONTHS = {
    "januari": "01", "februari": "02", "mars": "03", "april": "04",
    "maj": "05", "juni": "06", "juli": "07", "augusti": "08",
    "september": "09", "oktober": "10", "november": "11", "december": "12",
}


def _parse_date(text):
    """Try to parse a date string into YYYY-MM-DD format."""
    text = text.strip()

    # 2026-03-01
    if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
        return text

    # 2026.03.01
    if re.match(r"^\d{4}\.\d{2}\.\d{2}$", text):
        return text.replace(".", "-")

    # 31.03.2026 (dd.mm.yyyy — ALSO format)
    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    # 01/03/2026
    m = re.match(r"^(\d{2})/(\d{2})/(\d{4})$", text)
    if m:
        return f"{m.group(3)}-{m.group(2)}-{m.group(1)}"

    # 1 mars 2026
    m = re.match(r"^(\d{1,2})\s+(\w+)\s+(\d{4})$", text, re.IGNORECASE)
    if m:
        month = SWEDISH_MONTHS.get(m.group(2).lower())
        if month:
            return f"{m.group(3)}-{month}-{m.group(1).zfill(2)}"

    return text


def _parse_amount(text):
    """Parse amount: '1 234,56' / '1234.56' / '€539.00' / '$1,234.56' → float."""
    text = text.strip()
    # Remove currency symbols, letters, and common prefixes
    text = re.sub(r"[€$£¥A-Za-z]", "", text)
    # Remove spaces (thousand separators)
    text = text.replace(" ", "").replace("\u00a0", "")
    # Determine decimal separator:
    # "1.234,56" → comma is decimal (Swedish/EU)
    # "1,234.56" → dot is decimal (English)
    # "1234,56"  → comma is decimal
    # "1234.56"  → dot is decimal
    if "," in text and "." in text:
        if text.rindex(",") > text.rindex("."):
            # Comma after dot: "1.234,56" → EU format
            text = text.replace(".", "").replace(",", ".")
        else:
            # Dot after comma: "1,234.56" → English format
            text = text.replace(",", "")
    elif "," in text:
        text = text.replace(",", ".")
    try:
        return float(text)
    except ValueError:
        return None


# ── Field extraction patterns ────────────────────────────────────────────────

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
        r"(?:Datum)[\s.:]*(" + "|".join(DATE_PATTERNS) + ")",
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
    "ocr_number": [
        r"(?:OCR|OCR[_-]?nummer|OCR[_-]?nr|Betalningsreferens)[\s.:]*(\d[\d\s]*\d)",
    ],
    "bankgiro": [
        r"(?:Bankgiro|BG|Bg\.?)[\s.:]*([\d\s-]+\d)",
    ],
    "plusgiro": [
        r"(?:Plusgiro|PG|Pg\.?)[\s.:]*([\d\s-]+\d)",
    ],
    "org_number": [
        # Swedish org-nr (NNNNNN-NNNN)
        r"(?:Org\.?\s*(?:nr|nummer)|Organisationsnummer)[\s.:]*(\d{6}[\s-]?\d{4})",
        # VAT Reg No with letter-prefix — but pick the SUPPLIER one: prefer DE/LU/IE etc, NOT a customer SE number on a foreign invoice
        # Match any "VAT Reg. No.: <CC><digits>" (capture all, then heuristic in extract_fields picks supplier)
        r"VAT\s*Reg\.?\s*No\.?[\s:]*([A-Z]{2}\d{6,12})",
    ],
    "currency": [
        r"(?:Valuta|Currency)[\s.:]*(SEK|EUR|USD|NOK|DKK|GBP)",
        r"\((\s*SEK|EUR|USD|NOK|DKK|GBP)\s*\)",
    ],
}

# ── Next-line patterns ───────────────────────────────────────────────────────
# Some invoices put the label on one line and the value on the next:
#   Fakturanummer  Erreferens
#   1033           Johan Tollstorp
# Order matters: org_number must be extracted before bankgiro
# so we can avoid matching org.nr as bankgiro
NEXTLINE_PATTERNS_ORDERED = [
    ("invoice_number", [r"(?:Fakturanummer|Faktura\s*nr|Invoice\s*(?:no|number))"]),
    ("invoice_date", [r"(?:Fakturadatum|Invoice\s*date)"]),
    ("due_date", [r"(?:Förfallodatum|Förfallodag|Due\s*date)"]),
    ("org_number", [r"(?:Organisationsnummer|Org\.?\s*(?:nr|nummer))"]),
    ("bankgiro", [r"(?:Bankgiro|BG\b)"]),
    ("plusgiro", [r"(?:Plusgiro|PG\b)"]),
]


def _extract_text_pdfplumber(pdf_bytes):
    """Extract text from PDF using pdfplumber."""
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        pages = []
        for page in pdf.pages:
            text = page.extract_text()
            if text:
                pages.append(text)
        return "\n\n".join(pages)


def _extract_text_tesseract(pdf_bytes):
    """Fallback: convert PDF pages to images and OCR them."""
    if not HAS_TESSERACT:
        return ""

    try:
        import pypdfium2 as pdfium
    except ImportError:
        return ""

    pdf_doc = pdfium.PdfDocument(pdf_bytes)
    pages = []
    for i in range(len(pdf_doc)):
        page = pdf_doc[i]
        bitmap = page.render(scale=2)  # 2x for better OCR
        pil_image = bitmap.to_pil()
        text = pytesseract.image_to_string(pil_image, lang="swe+eng")
        if text.strip():
            pages.append(text)
    return "\n\n".join(pages)


def extract_text(pdf_bytes):
    """Extract text from PDF, with tesseract fallback for image-based PDFs."""
    text = _extract_text_pdfplumber(pdf_bytes)
    if len(text.strip()) < 50 and HAS_TESSERACT:
        # Probably an image-based PDF, try OCR
        ocr_text = _extract_text_tesseract(pdf_bytes)
        if len(ocr_text.strip()) > len(text.strip()):
            text = ocr_text
    return text


def extract_fields(text):
    """Extract structured invoice fields from text."""
    result = {}
    lines = text.split("\n")

    # Standard same-line patterns
    for field, patterns in FIELD_PATTERNS.items():
        for pattern in patterns:
            m = re.search(pattern, text, re.IGNORECASE | re.MULTILINE)
            if m:
                value = m.group(1).strip()
                if field in ("invoice_date", "due_date"):
                    value = _parse_date(value)
                elif field in ("total_amount", "vat_amount", "subtotal"):
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
                    elif field == "org_number":
                        m = re.search(r"(\d{6}[\s-]?\d{4})", next_line)
                    elif field in ("bankgiro", "plusgiro"):
                        # Avoid matching org.nr (6-4 digits) as bankgiro (4-4 digits)
                        org = result.get("org_number", "").replace("-", "")
                        candidates = re.findall(r"(\d{4}[\s-]?\d{4})", next_line)
                        m = None
                        for c in candidates:
                            if org and c.replace("-", "").replace(" ", "") in org:
                                continue  # Skip — this is the org.nr
                            # Create a fake match-like object
                            class _M:
                                def __init__(self, v): self._v = v
                                def group(self, _): return self._v
                            m = _M(c)
                            break
                    else:
                        m = None
                    if m:
                        value = m.group(1).strip()
                        if field in ("invoice_date", "due_date"):
                            value = _parse_date(value)
                        elif field in ("bankgiro", "plusgiro"):
                            value = re.sub(r"\s+", "", value)
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
        m = re.search(r"Datum\s+(\d{2}\.\d{2}\.\d{4})", text)
        if m:
            result["invoice_date"] = _parse_date(m.group(1))
    if "due_date" not in result:
        m = re.search(r"Förfallodatum\s+(\d{2}\.\d{2}\.\d{4})", text)
        if m:
            result["due_date"] = _parse_date(m.group(1))

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
    # and the CUSTOMER (Molnkontakt SE...) VAT in the header. Prefer non-SE
    # candidates, and prefer the LAST occurrence (footer).
    vat_candidates = re.findall(r"VAT\s*Reg\.?\s*No\.?[\s:]*([A-Z]{2}\d{6,12})",
                                text, re.IGNORECASE)
    if vat_candidates:
        # Prefer non-own, non-SE; fall back to last occurrence
        non_own = [v for v in vat_candidates if v.upper() not in OWN_VAT_NUMBERS]
        non_se = [v for v in non_own if not v.startswith("SE")]
        if non_se:
            result["org_number"] = non_se[-1]
        elif non_own:
            result["org_number"] = non_own[-1]
        # else: only own VAT found — leave any prior org_number value alone

    # Extract supplier/vendor name from the PDF
    # Look for the first company-like name (ending in AB, AS, GmbH, Ltd, etc.)
    # at the top of the document, but NOT our own company
    if "vendor_name" not in result:
        for line in lines[:15]:
            stripped = line.strip()
            if re.search(r"\b(?:AB|AS|GmbH|Ltd|Inc|LLC|Oy|A/S)\b", stripped) and not (OWN_COMPANY and OWN_COMPANY in stripped.lower()):
                # Take up to and including the company suffix
                m = re.match(r"(.+?\b(?:AB|AS|GmbH|Ltd|Inc|LLC|Oy|A/S)\b)", stripped)
                result["vendor_name"] = m.group(1).strip() if m else stripped.split("  ")[0].strip()
                break

    if "vendor_name" not in result:
        # Swedish: "Company AB  Organisationsnummer  Bankgiro"
        for line in lines:
            m = re.match(r"^(.+?)\s+(?:Organisationsnummer|Org\.?\s*(?:nr|nummer))", line, re.IGNORECASE)
            if m:
                name = m.group(1).strip().rstrip(",")
                if name and len(name) > 1 and not (OWN_COMPANY and OWN_COMPANY in name.lower()):
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
                            and not (OWN_COMPANY and OWN_COMPANY in candidate.lower())):
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
STAIK_URL = os.environ.get("STAIK_URL", "https://api.staik.se/v1")
STAIK_API_KEY = os.environ.get("STAIK_API_KEY", "")
# Reasoning-varianten. Basmodellen svarar direkt utan att rakna och far da fel pa
# flertermssummor; matt 2026-09-04 gav den 18/24 mot 22/24 for -thinking.
STAIK_MODEL = os.environ.get("STAIK_MODEL", "qwen3.6:35b-a3b-thinking")
# Ett svar under den har granden betyder att modellen hoppade over resonemanget.
# Samtliga korrekta svar i matningen lag pa 3400-7200 completion-tokens, de tva
# felaktiga pa 462 och 649.
STAIK_MIN_COMPLETION_TOKENS = int(os.environ.get("STAIK_MIN_COMPLETION_TOKENS", "1000"))

# The receiving company, so its own name/VAT number printed on the invoice is never taken
# for the supplier. The Odoo model sets these from res.company before each run; for
# standalone use set INVOICE_OCR_OWN_COMPANY ("Acme AB") and INVOICE_OCR_OWN_VAT ("SE5566...").
OWN_COMPANY = os.environ.get("INVOICE_OCR_OWN_COMPANY", "").strip().lower()
OWN_VAT_NUMBERS = {v.strip().upper() for v in os.environ.get("INVOICE_OCR_OWN_VAT", "").split(",") if v.strip()}

# json_schema tvingar fram giltig JSON hos staik. Fritt format ger trasiga svar.
INVOICE_JSON_SCHEMA = {
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
            "items": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "quantity": {"type": "number"},
                    "unit_price": {"type": "number"},
                    "amount": {"type": "number"},
                    "vat_rate": {"type": "number"},
                    "account_code": {"type": "string"},
                },
                "required": ["description", "amount", "vat_rate", "account_code"],
            },
        },
    },
    "required": ["vendor_name", "total_amount", "subtotal", "vat_amount", "lines"],
}

EXTRACTION_PROMPT = """Extract the following fields from this Swedish vendor invoice. Return ONLY valid JSON, no other text.

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
  - account_code: use ONLY these account codes from our chart of accounts:
    4000 = Inköp av varor från Sverige (physical goods, hardware)
    4515 = Inköp av varor från annat EU-land, 25%
    4535 = Inköp av tjänster från annat EU-land, 25%
    4545 = Import av varor, 25% moms
    5010 = Lokalhyra (office rent)
    5252 = Leasing av datorer (computer leasing)
    5410 = Förbrukningsinventarier (consumables, small equipment)
    5420 = Programvaror (packaged software, on-premise licenses)
    5610 = Personbilar (vehicle costs)
    5810 = Biljetter (train, flight, bus, taxi, public transport tickets)
    5820 = Hyrbilskostnader (car hire)
    5831 = Kost och logi i Sverige (hotel/accommodation in Sweden)
    5832 = Kost och logi i utlandet (hotel/accommodation abroad)
    5890 = Övriga resekostnader (other travel costs)
    6110 = Kontorsmateriel (office supplies, paper, toner)
    6211 = Fast telefoni (fixed-line telephony)
    6212 = Mobiltelefon (mobile phone subscriptions and call charges)
    6230 = Datakommunikation (internet, broadband)
    6231 = Datamolntjänster (cloud services, SaaS, hosting, domains, security software subscriptions like SentinelOne/Lookout/M365)
    6250 = Postbefordran (postage)
    6310 = Företagsförsäkringar (business insurance)
    6420 = Ersättningar till revisor (audit fees)
    6530 = Redovisningstjänster (accounting services)
    6540 = IT-tjänster (IT consulting, managed services)
    6570 = Bankkostnader (bank fees only)
    6910 = Licensavgifter och royalties
    6990 = Övriga externa kostnader (reminder/late-payment fees, other charges)
    7570 = Premier för arbetsmarknadsförsäkringar (Fora, Collectum etc)

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
  OUTSIDE the scope of VAT: give those lines account_code 6990 and vat_rate 0,
  never 25. A telecom invoice whose printed VAT is less than 25% of the printed
  net almost always contains such a fee — put it on its own line so the VAT adds up.
- A RABATT / discount / credit line is NOT such a fee. It reduces the price of the
  service it belongs to, so it MUST carry the SAME account_code and the SAME
  vat_rate as that service (typically 25) — never 6990 and never vat_rate 0.
  Best of all: subtract it from that service's own line instead of listing it
  separately.
- subtotal + vat_amount must equal total_amount. If the invoice has fees, charges, or adjustments beyond the line items, include them as separate lines.
- Use the SAME account code for similar services on the same invoice. E.g. if all lines are cloud/SaaS services, use 6231 for all of them including platform fees.
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


def _call_venice(text):
    """Call Venice.ai API (OpenAI-compatible)."""
    import requests as _req
    r = _req.post("https://api.venice.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {VENICE_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": VENICE_MODEL,
              "messages": [{"role": "user", "content": EXTRACTION_PROMPT + text[:6000]}],
              "max_tokens": 6000, "temperature": 0},
        timeout=120)
    choice = r.json()["choices"][0]
    if choice.get("finish_reason") == "length":
        logger.warning(
            "AI-svaret fran %s klipptes av max_tokens — JSON:en blir ofullstandig "
            "och faltdata gar forlorad. Hoj max_tokens.", VENICE_MODEL)
    content = choice["message"]["content"]
    return _parse_ai_json(content)


def _call_ollama(text):
    """Call local Ollama instance."""
    import requests as _req
    r = _req.post(f"{OLLAMA_URL}/api/chat",
        json={"model": OLLAMA_MODEL, "stream": False,
              "messages": [{"role": "user", "content": EXTRACTION_PROMPT + text[:6000]}]},
        timeout=60)
    content = r.json()["message"]["content"]
    return _parse_ai_json(content)


def _call_openai(text):
    """Call OpenAI API."""
    import requests as _req
    r = _req.post("https://api.openai.com/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENAI_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": "gpt-4o-mini",
              "messages": [{"role": "user", "content": EXTRACTION_PROMPT + text[:6000]}],
              "max_tokens": 6000, "temperature": 0},
        timeout=30)
    content = r.json()["choices"][0]["message"]["content"]
    return _parse_ai_json(content)


def _call_staik(text):
    """Kall staik (OpenAI-kompatibel). Svensk datahemvist — data stannar i Sverige."""
    import requests as _req
    r = _req.post(f"{STAIK_URL}/chat/completions",
        headers={"Authorization": f"Bearer {STAIK_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": STAIK_MODEL,
              "messages": [{"role": "user", "content": EXTRACTION_PROMPT + text[:6000]}],
              "max_tokens": 8000, "temperature": 0,
              "response_format": {"type": "json_schema",
                                  "json_schema": {"name": "invoice",
                                                  "schema": INVOICE_JSON_SCHEMA}}},
        timeout=300)
    j = r.json()
    choice = j["choices"][0]
    if choice.get("finish_reason") == "length":
        logger.warning("AI-svaret fran %s klipptes av max_tokens.", STAIK_MODEL)
    # staik faller TYST tillbaka till sin default-modell vid okant modellnamn, och
    # model-faltet speglar basmodellen aven for -thinking. Antalet tokens ar darfor
    # enda tillforlitliga tecknet pa att resonemanget faktiskt kordes.
    served = j.get("model")
    ctok = (j.get("usage") or {}).get("completion_tokens")
    data = _parse_ai_json(choice["message"]["content"])
    if isinstance(data, dict) and data:
        data["_completion_tokens"] = ctok
        data["_served_model"] = served
    return data


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


def _num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _ai_answer_problems(data, reference=None):
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
    problems = []
    reference = reference or {}

    ctok = data.get("_completion_tokens")
    if ctok is not None and ctok < STAIK_MIN_COMPLETION_TOKENS:
        problems.append(f"bara {ctok} completion-tokens (resonemanget hoppades over)")

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
    net = _num(reference.get("subtotal"))
    if net is None:
        net = sub
    if not lines:
        problems.append("inga rader")
    elif net is not None:
        linesum = sum(float(ln.get("amount") or 0) for ln in lines)
        if abs(linesum - net) > 1:
            problems.append(f"radsumma {linesum:.2f} mot netto {net:.2f}")
    return problems


def _call_provider(text):
    if AI_PROVIDER == "staik":
        return _call_staik(text)
    if AI_PROVIDER == "venice":
        return _call_venice(text)
    elif AI_PROVIDER == "ollama":
        return _call_ollama(text)
    elif AI_PROVIDER == "openai":
        return _call_openai(text)
    return {}


def _extract_fields_ai(text, reference=None):
    """AI validation: extract invoice fields using configured LLM provider.

    Kor om anropet en gang om svaret ser opalitligt ut. Reasoning-modeller hoppar
    ibland over resonemanget och svarar rakt av, vilket ger fel pa flertermssummor.
    Det ar sporadiskt, sa en omkorning racker — men vi behaller det basta av de tva
    svaren i stallet for att blint ta det sista.
    """
    try:
        data = _call_provider(text)
    except Exception as e:
        logger.warning("AI extraction failed (%s): %s", AI_PROVIDER, e)
        return {}

    problems = _ai_answer_problems(data, reference)
    if not problems:
        return _strip_meta(data)

    logger.warning("AI-svaret ser opalitligt ut (%s) — kor om en gang",
                   "; ".join(problems))
    try:
        retry = _call_provider(text)
    except Exception as e:
        logger.warning("Omkorningen misslyckades (%s): %s — behaller forsta svaret",
                       AI_PROVIDER, e)
        return _strip_meta(data)

    if not _ai_answer_problems(retry, reference):
        logger.info("Omkorningen gav ett svar som gar ihop — anvander det")
        return _strip_meta(retry)

    logger.warning("Aven omkorningen ser opalitlig ut — behaller det forsta svaret. "
                   "Fakturan behover granskas manuellt.")
    return _strip_meta(data)


def extract_invoice_data(pdf_b64_or_bytes):
    """Main entry point: extract invoice data from a PDF.

    Uses regex first, then AI to validate and fill gaps.
    AI result wins on conflicts (it sees full context).

    Args:
        pdf_b64_or_bytes: Either base64-encoded string or raw bytes

    Returns:
        dict with extracted fields + 'raw_text' key
    """
    if isinstance(pdf_b64_or_bytes, str):
        pdf_bytes = base64.b64decode(pdf_b64_or_bytes)
    else:
        pdf_bytes = pdf_b64_or_bytes

    text = extract_text(pdf_bytes)
    regex_fields = extract_fields(text)
    ai_fields = _extract_fields_ai(text, reference=regex_fields)

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
    for key in all_keys:
        ai_val = ai_fields.get(key)
        regex_val = regex_fields.get(key)
        ai_has = key in ai_fields and ai_val is not None
        regex_has = key in regex_fields and regex_val is not None
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
