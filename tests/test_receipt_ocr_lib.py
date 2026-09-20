"""Guards and parsing of the receipt library (no Odoo, no tesseract, no network)."""

import receipt_ocr as r

TEXT = """Kvitto 3270143 2026-09-17 10:14
Alkylatbensin
Antal: 2 st 209.00 kr/st
Totalt (2 Artiklar) 418,00
Kort: 418.00
Underlag Moms-% Moms Summa
334.40 25% 83.60 418.00
"""


def test_regex_fields_total_and_date():
    f = r._regex_fields(TEXT)
    assert f["total"] == 418.0
    assert f["date"] == "2026-09-17"


def test_merchant_must_appear_in_text():
    assert r._merchant_in_text("Example Store", "receipt from EXAMPLE store") is True
    assert r._merchant_in_text("OKQ8", TEXT) is False


def test_guards_drop_guessed_merchant_and_unseen_total():
    fields = {"merchant": "OKQ8", "total": 339.0, "date": "2026-09-17", "confidence": 0.9}
    kept, notes = r._apply_guards(fields, TEXT)
    assert "merchant" not in kept and "total" not in kept
    assert kept["date"] == "2026-09-17"
    assert len(notes) == 2


def test_low_confidence_fills_nothing():
    fields = {"total": 418.0, "date": "2026-09-17", "confidence": 0.3}
    kept, notes = r._apply_guards(fields, TEXT)
    assert "total" not in kept and "date" not in kept
    assert any("konfidens" in n for n in notes)


def test_clean_keeps_only_known_categories():
    cats = [("MASKIN", "Machinery"), ("FOOD", "Meals")]
    out = r._clean({"category_code": "TRAVEL", "total": "418", "date": "2026-09-17", "items": "fuel"}, cats)
    assert "category_code" not in out and out["total"] == 418.0 and out["date"] == "2026-09-17"
    assert r._clean({"category_code": "FOOD"}, cats)["category_code"] == "FOOD"


def test_prompt_lists_categories_with_hints():
    p = r.build_prompt([("MASKIN", "Machinery", "fuel, oil, tools"), ("FOOD", "Meals")])
    assert "MASKIN — Machinery: fuel, oil, tools" in p and "FOOD — Meals" in p
