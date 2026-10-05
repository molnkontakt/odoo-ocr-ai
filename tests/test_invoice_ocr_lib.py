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


def test_plan_total_adjustments_credit_outside_vat_and_rounding():
    # services 2 061,00 (25 %), credit -0,25 outside VAT, rounding -1,00:
    # printed net 2 060,75, VAT 515,25, amount due 2 575
    plan = inv.plan_total_adjustments({25: 2060.75}, 0.0, 515.19, 515.25, 2575.0)
    assert plan == {"base_shift": (25, 0.25), "rounding": -1.0}


def test_plan_total_adjustments_leaves_correct_and_large_alone():
    assert inv.plan_total_adjustments({25: 2061.0}, -0.25, 515.25, 515.25, 2576.0) == {
        "base_shift": None, "rounding": None}
    # 50 kr is not rounding: flagged by the total check, not "fixed"
    assert inv.plan_total_adjustments({25: 1000.0}, 0.0, 250.0, 300.0, 1300.0) == {
        "base_shift": None, "rounding": None}
    # two rates: unclear which base is wrong
    assert inv.plan_total_adjustments({25: 100.0, 12: 50.0}, 0.0, 31.0, 32.0, 182.0)["base_shift"] is None


def test_plan_total_adjustments_rounding_only():
    assert inv.plan_total_adjustments({25: 100.4}, 0.0, 25.10, 25.10, 125.0) == {
        "base_shift": None, "rounding": -0.5}
