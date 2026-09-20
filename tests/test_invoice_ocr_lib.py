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


def test_slow_first_call_skips_retry(monkeypatch):
    """If the first AI call took >= RETRY_SKIP_SECONDS, no second call is made.

    The synchronous path in the Odoo model must not block for two full
    timeouts on a slow/hung provider.
    """
    monkeypatch.setattr(inv, "RETRY_SKIP_SECONDS", 0.0)
    calls = []

    def slow_provider(text):
        calls.append("call")
        # sub + vat != total → misstänkt svar, skulle normalt trigga omkörning
        return {"vendor_name": "Test AB", "total_amount": 100, "subtotal": 90,
                "vat_amount": 0, "lines": [{"amount": 100}]}

    monkeypatch.setattr(inv, "_call_provider", slow_provider)
    result = inv._extract_fields_ai("text", reference={})
    assert len(calls) == 1
    assert result["vendor_name"] == "Test AB"


def test_fast_suspect_answer_is_rerun(monkeypatch):
    monkeypatch.setattr(inv, "RETRY_SKIP_SECONDS", 60.0)
    calls = []
    answers = [
        {"vendor_name": "Test AB", "total_amount": 100, "subtotal": 90,
         "vat_amount": 0, "lines": [{"amount": 100}]},  # sub + vat != total
        {"vendor_name": "Test AB", "total_amount": 100, "subtotal": 100,
         "vat_amount": 0, "lines": [{"amount": 100}]},
    ]

    def fast_provider(text):
        calls.append("call")
        return answers.pop(0)

    monkeypatch.setattr(inv, "_call_provider", fast_provider)
    result = inv._extract_fields_ai("text", reference={})
    assert len(calls) == 2
    assert result["total_amount"] == 100
    assert result["subtotal"] == 100
