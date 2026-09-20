"""Regex/parsing helpers of the invoice library (no Odoo, no network)."""

import invoice_ocr as inv


def test_parse_amount_swedish_and_international():
    assert inv._parse_amount("1 234,56") == 1234.56
    assert inv._parse_amount("1234.56") == 1234.56
    assert inv._parse_amount("€539.00") == 539.0
    assert inv._parse_amount("$1,234.56") == 1234.56


def test_parse_date_formats():
    assert inv._parse_date("2026-09-17") == "2026-09-17"
    assert inv._parse_date("17.09.2026") == "2026-09-17"
    assert inv._parse_date("09/04/2026") == "2026-04-09"


def test_parse_ai_json_tolerates_fences_and_thinking():
    content = 'Thinking... ```json\n{"vendor_name": "Example AB", "total_amount": "123.45", "lines": []}\n```'
    data = inv._parse_ai_json(content)
    assert data["vendor_name"] == "Example AB"
    assert data["total_amount"] == 123.45


def test_own_company_is_never_the_vendor(monkeypatch):
    monkeypatch.setattr(inv, "OWN_COMPANY", "example receiver ab")
    monkeypatch.setattr(inv, "OWN_VAT_NUMBERS", {"SE556000000001"})
    text = (
        "Example Receiver AB\nVAT Reg No: SE556000000001\n"
        "Supplier Ltd\nVAT Reg No: GB123456789\nInvoice no: 42\nTotal: 100.00\n"
    )
    fields = inv.extract_fields(text)
    assert fields.get("org_number") == "GB123456789"
    assert "receiver" not in (fields.get("vendor_name") or "").lower()
