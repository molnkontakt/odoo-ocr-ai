"""The payment reference and the invoice number (#39): an amount printed on the document is
never the OCR reference, a value under a header is taken from its own column, and when the
regex and the AI disagree the value printed with the right label wins. All numbers are
invented; the references have a valid mod-10 check digit, and so has the amount 1 248."""

import invoice_ocr as inv

REFERENCE = "900000000019"
OTHER_REFERENCE = "900000000027"

# A telecom-style invoice: the reference follows "OCR/Fakturanummer" on its line, and on the
# payment slip the amount due is printed on the line under it
TELECOM_TEXT = f"""Example Telecom AB
Faktura
Belopp att betala 1248 kr
Belopp exkl. moms 998,40 kr
Moms 249,60 kr
OCR/Fakturanummer {REFERENCE}
Kundnummer 55501234
INBETALNING
OCR/Fakturanummer: {REFERENCE}
Belopp att betala: 1248 kr Avsändare
"""


def _merge(regex, ai, text=""):
    return inv._merge_fields(text, regex, ai, set())


def test_reference_after_a_combined_label():
    out = inv.extract_fields(TELECOM_TEXT)
    assert out["ocr_number"] == REFERENCE
    assert out["invoice_number"] == REFERENCE
    assert out["total_amount"] == 1248.0


def test_the_line_under_a_label_with_its_value_is_not_read():
    """The amount due on the line under "OCR/Fakturanummer: <reference>" is not the
    reference, however well it passes the check digit."""
    text = f"OCR/Fakturanummer: {REFERENCE}\nBelopp att betala: 1248 kr\n"
    assert inv.extract_fields(text)["ocr_number"] == REFERENCE
    assert inv.ocr_mod10("1248")


def test_an_amount_is_never_the_reference():
    out = _merge({"ocr_number": "1248", "total_amount": 1248.0}, {"ocr_number": REFERENCE})
    assert out["ocr_number"] == REFERENCE
    assert any("'1248' is an amount on the document" in n for n in out["_notes"])
    # in öre, and from the AI's amounts too
    out = _merge({"ocr_number": "124800"}, {"total_amount": 1248.0})
    assert "ocr_number" not in out
    out = _merge({}, {"ocr_number": "1248", "total_amount": 1248.0})
    assert "ocr_number" not in out


def test_disagreeing_references_the_labelled_one_wins():
    labelled_ai = f"Fakturanummer 4711\nOCR: {REFERENCE}\n"
    out = _merge({"ocr_number": OTHER_REFERENCE}, {"ocr_number": REFERENCE}, labelled_ai)
    assert out["ocr_number"] == REFERENCE
    assert any("used the AI's" in n for n in out["_notes"])
    labelled_regex = f"Fakturanummer 4711\nOCR: {OTHER_REFERENCE}\n"
    out = _merge({"ocr_number": OTHER_REFERENCE}, {"ocr_number": REFERENCE}, labelled_regex)
    assert out["ocr_number"] == OTHER_REFERENCE
    # neither is printed with a label: the AI's
    out = _merge({"ocr_number": OTHER_REFERENCE}, {"ocr_number": REFERENCE}, "no labels\n")
    assert out["ocr_number"] == REFERENCE
    # the same reference, grouped differently, is no disagreement
    out = _merge({"ocr_number": REFERENCE}, {"ocr_number": "9000 0000 0019"}, labelled_ai)
    assert out["ocr_number"] == REFERENCE and "_notes" not in out


HEADER_TEXT = """Account ID Invoice No Invoice Date Due Date Total Due
100200 2026-01-000123 2026-06-01 2026-06-15 kr 300.00
Bill to: Example Buyer AB
"""


def test_invoice_number_from_its_own_column():
    """Under "Account ID Invoice No …" the invoice number is the second value, not the
    account id that comes first on the line."""
    out = inv.extract_fields(HEADER_TEXT)
    assert out["invoice_number"] == "2026-01-000123"
    assert out["_sources"]["invoice_number"] == "next_line"
    text = "Kundnummer Fakturanummer Datum\n55501234 1033 2026-03-01\n"
    assert inv.extract_fields(text)["invoice_number"] == "1033"
    text = "Fakturanummer Erreferens\n1033 Example Person\n"
    assert inv.extract_fields(text)["invoice_number"] == "1033"


def test_value_on_the_labels_line_is_not_looked_for_below():
    """"Fakturanr EX12AB34" has its value on the line; the house number on the address line
    under it is not an invoice number."""
    text = "Fakturanr EX12AB34\nEXEMPELGATAN 2\n"
    assert "invoice_number" not in inv.extract_fields(text)


def test_the_ais_labelled_invoice_number_wins():
    regex = {"invoice_number": "100200", "_sources": {"invoice_number": "next_line"}}
    out = _merge(regex, {"invoice_number": "2026-01-000123"}, HEADER_TEXT)
    assert out["invoice_number"] == "2026-01-000123"
    assert any("used the AI's 2026-01-000123" in n for n in out["_notes"])
    # an order number never beats the AI's invoice number
    regex = {"invoice_number": "EU50246", "_sources": {"invoice_number": "order"}}
    out = _merge(regex, {"invoice_number": "4711"}, "Order Number: EU50246\n")
    assert out["invoice_number"] == "4711"
    # the regex's labelled number stays when the AI's is not printed with a label
    regex = {"invoice_number": "4711", "_sources": {"invoice_number": "label"}}
    out = _merge(regex, {"invoice_number": "100200"}, "Fakturanummer: 4711\nKund 100200\n")
    assert out["invoice_number"] == "4711"


def test_label_anchored():
    assert inv.label_anchored("invoice_number", "2026-01-000123", HEADER_TEXT)
    assert inv.label_anchored("ocr_number", REFERENCE, TELECOM_TEXT)
    assert not inv.label_anchored("ocr_number", "1248", TELECOM_TEXT)
    assert inv.label_anchored("ocr_number", REFERENCE, "OCR 9000 0000 0019\n")
    assert not inv.label_anchored("invoice_number", "55501234", TELECOM_TEXT)
