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


def test_text_extraction_errors_are_readable(monkeypatch):
    def broken(*args, **kwargs):
        raise OSError("truncated file")

    monkeypatch.setattr(r, "extract_text", broken)
    try:
        r.extract_receipt_data(b"x", "image/jpeg", "receipt.jpg")
    except r.ReceiptReadError as e:
        assert "receipt.jpg" in str(e) and "truncated file" in str(e)
    else:
        raise AssertionError("ReceiptReadError expected")


# ── Amount tokens: the receipt amount guard and the regex total (#32, #36.11) ──

def test_total_in_text_rejects_amounts_inside_other_numbers():
    assert not r._total_in_text(18.0, "Totalt 418,00")
    assert not r._total_in_text(89.0, "Kort 389,00")
    assert not r._total_in_text(25.0, "Moms 25%")
    assert not r._total_in_text(25.0, "Moms 25 % 83,60")
    assert not r._total_in_text(25.0, "Moms 25,00% 83,60")
    assert not r._total_in_text(17.0, "Kvitto 3270143 2026-09-17 10:14")
    assert not r._total_in_text(2026.0, "2026-09-17")
    assert not r._total_in_text(9.0, "17.09.2026")
    assert not r._total_in_text(10.0, "10:14")
    assert not r._total_in_text(14.0, "10:14")
    assert not r._total_in_text(3.60, "Moms 83.60")
    assert not r._total_in_text(9688.0, "Kort ************9688")
    assert not r._total_in_text(None, "Totalt 418,00")


def test_total_in_text_accepts_grouped_and_whole_amounts():
    assert r._total_in_text(418.0, "Totalt 418,00")
    assert r._total_in_text(418.0, "Kort: 418.00")
    assert r._total_in_text(1234.5, "Totalt 1 234,50")
    assert r._total_in_text(1234.5, "Totalt 1 234,50")
    assert r._total_in_text(1234.5, "Totalt 1.234,50")
    assert r._total_in_text(1234.5, "Totalt 1234,50")
    assert r._total_in_text(1000.0, "Att betala 1 000 kr")
    assert r._total_in_text(1000.0, "Att betala 1 000,00")
    assert r._total_in_text(418.0, "Totalt 418:-")
    assert r._total_in_text(418.0, "Totalt 418,- kr")


def test_regex_total_handles_separators_and_item_counts():
    assert r._regex_fields("Totalt 1234,50\n")["total"] == 1234.5
    assert r._regex_fields("Totalt 1234.50\n")["total"] == 1234.5
    assert r._regex_fields("Köp 1049,00 SEK\n")["total"] == 1049.0
    assert r._regex_fields("Att betala 12345,00\n")["total"] == 12345.0
    assert r._regex_fields("Totalt (2 Artiklar) 418,00\n")["total"] == 418.0
    assert r._regex_fields("Summa 2 varor 418,00\n")["total"] == 418.0
    assert r._regex_fields(TEXT.replace("Kort: 418.00\n", ""))["total"] == 418.0


def test_regex_total_prefers_labelled_total_over_largest_amount():
    discount = "Summa 450,00\nRabatt -32,00\nAtt betala 418,00\n"
    assert r._regex_fields(discount)["total"] == 418.0
    withdrawal = "Köp 418,00\nUttag 200,00\nBelopp 618,00\n"
    assert r._regex_fields(withdrawal)["total"] == 418.0
    vat_lines = "Totalt moms 83,60\nTotalt exkl. moms 334,40\nKort 418,00\n"
    assert r._regex_fields(vat_lines)["total"] == 418.0
    assert "total" not in r._regex_fields("Totalt antal artiklar: 2\n")


def test_guard_falls_back_to_the_printed_total():
    regex = r._regex_fields(TEXT)
    kept, notes = r._apply_guards({"total": 18.0, "confidence": 0.9}, TEXT, regex)
    assert kept["total"] == 418.0
    assert any("418.00" in n for n in notes)
