"""The amounts read from a document (#39): a value is taken only from its label's line, a
rate is never an amount, a label with several different amounts is left to the AI, and the
amounts of the bill that are printed on the document are kept for the posting check. Lines
that carry the prices including VAT are recognised. All documents are invented."""

import invoice_ocr as inv

f = inv.extract_fields


def test_a_glued_column_header_never_takes_the_next_lines_number():
    """"PrisTotalt" (two headers, the space lost) took the article number on the next line,
    4 711 000, for the total; the price including VAT is printed before its label."""
    text = ("Beskrivning Antal PrisTotalt\n"
            "4711000 Example charger 65W för 499,00\n"
            "1 499,00 kr\n"
            "Totalt (exkl. moms)399,20 kr\n"
            "Varav moms 99,80 kr\n"
            "499,00\n"
            "Totalt (inkl. moms)\n"
            "kr\n")
    out = f(text)
    assert "total_amount" not in out
    assert out["subtotal"] == 399.20
    assert out["vat_amount"] == 99.80


def test_a_rate_is_never_the_vat_amount():
    assert f("Totalt 115,00\nVarav moms 25% 23,00\n")["vat_amount"] == 23.0
    assert f("Varav moms 12 % 13,50 kr\n")["vat_amount"] == 13.5
    assert "vat_amount" not in f("Varav moms 25%\n")


def test_amount_after_a_currency_and_amount_due_labels():
    text = ("INVOICE SUMMARY\nVAT kr 60.00\nTOTAL DUE kr 300.00\n"
            "Item A Amount Due kr 36.60\nItem B Amount Due kr 104.88\n")
    out = f(text)
    assert out["total_amount"] == 300.0
    assert out["vat_amount"] == 60.0


def test_summa_on_a_receipt_is_the_total():
    out = f("Example Hardware AB\nSUMMA 12 345 kr\nVarav moms 2 469 kr\n")
    assert out["total_amount"] == 12345.0 and out["vat_amount"] == 2469.0
    out = f("Summa (SEK) 1 250,00\n")
    assert out["total_amount"] == 1250.0 and out["currency"] == "SEK"


def test_summa_that_is_the_net_is_no_total():
    assert "total_amount" not in f("Summa 1 000,00\nMoms 250,00\n")
    assert "total_amount" not in f("Summa exkl. moms 1 000,00\n")
    assert f("Summa 1 000,00\nMoms 250,00\nAtt betala 1 250,00\n")["total_amount"] == 1250.0


def test_a_row_of_amounts_is_no_total():
    assert "total_amount" not in f("Moms Netto Brutto\nTotalt 5,00 95,00 100,00\n")
    assert "total_amount" not in f("Netto Moms Att betala\n800,00 200,00 1 000,00\n")
    assert "total_amount" not in f("Totalt 1 vara\n")


def test_an_amount_alone_on_the_next_line():
    assert f("Att betala\n1 234,00 kr\n")["total_amount"] == 1234.0


def test_common_total_labels():
    assert f("Totalt inkl. moms 1 250,00\n")["total_amount"] == 1250.0
    assert f("Totalt (inkl. moms) 1 250,00 kr\n")["total_amount"] == 1250.0
    assert f("Att betala (SEK): 1 250,00\n")["total_amount"] == 1250.0
    assert f("ATT BETALA SEK 1.250,00\n")["total_amount"] == 1250.0
    assert "total_amount" not in f("Att betala 250 00\n"), "öre split off: no amount"


def test_different_amounts_with_one_label_are_left_to_the_ai():
    """An order and a separate fee receipt in one document: neither "Totalt" is the total."""
    text = ("Orderspecifikation\nProdukt Totalt (SEK)\nExample item 500,00\nFrakt 100,00\n"
            "Totalt 600,00\nVarav moms 25% 20,00\n\nKvitto avgift\nAvgift 30,00\n"
            "Totalt 30,00\nVarav moms 25% 6,00\n")
    out = f(text)
    assert "total_amount" not in out and "vat_amount" not in out
    assert out["_ambiguous_amounts"] == {"total_amount": [600.0, 30.0],
                                         "vat_amount": [20.0, 6.0]}
    merged = inv._merge_fields(text, out, {"total_amount": 630.0}, set())
    assert merged["total_amount"] == 630.0
    # one part's total, even read by the AI, is not the document's printed total
    merged_part = inv._merge_fields(text, out, {"total_amount": 600.0}, set())
    assert "total_amount" not in merged_part.get("_on_document", {})
    # several totals: several receipts or orders in one PDF — the lines must be checked by
    # hand and the posting check has no total (the VAT note stays generic)
    assert any("several different totals (600.00, 30.00)" in n
               and "several receipts or orders" in n and "not checked when the bill is posted" in n
               for n in merged["_notes"])
    assert any(n.startswith("vat_amount: the document prints several different amounts")
               and "20.00, 6.00" in n for n in merged["_notes"])
    # the same amount twice (the invoice and its payment slip) is one printed amount
    assert f("Att betala 1 250,00\nInbetalning\nAtt betala 1 250,00\n")["total_amount"] == 1250.0


def test_amounts_on_the_document_for_the_posting_check():
    text = "Example Shop AB\n499,00\nTotalt (inkl. moms)\nVarav moms 99,80 kr\n"
    regex = f(text)
    out = inv._merge_fields(text, regex, {"total_amount": 499.0, "subtotal": 399.2,
                                          "vat_amount": 99.8}, set())
    # the AI's total is printed as an amount; its net is not printed anywhere
    assert out["_on_document"] == {"total_amount": 499.0, "vat_amount": 99.8}
    out = inv._merge_fields(text, regex, {"total_amount": 500.0}, set())
    assert "total_amount" not in out.get("_on_document", {})


def test_lines_including_vat_are_recognised():
    # header: net 9 876 + VAT 2 469 = total 12 345; the lines carry the prices with VAT
    assert inv.lines_include_vat([12000.0, 345.0], 12345.0, 9876.0, 2469.0)
    assert not inv.lines_include_vat([9600.0, 276.0], 12345.0, 9876.0, 2469.0), "already net"
    assert not inv.lines_include_vat([12000.0, 345.0], 12345.0, 12345.0, 2469.0), \
        "a header that does not add up confirms nothing"
    assert not inv.lines_include_vat([1000.0], 1000.0, 1000.0, 0.0), "no VAT charged"
    assert not inv.lines_include_vat([12000.0, 345.0], None, 9876.0, 2469.0)
    assert inv.amount_excluding_vat(12000.0, 25) == 9600.0
    assert inv.amount_excluding_vat(345.0, 25) == 276.0
    assert inv.amount_excluding_vat(106.0, 6) == 100.0


def test_document_header_prefers_the_printed_amounts():
    data = {"total_amount": 100.0, "subtotal": 80.0, "vat_amount": 20.0,
            "_printed": {"total_amount": 125.0}}
    assert inv.document_header(data) == (125.0, 80.0, 20.0)
    assert inv.document_header({}) == (None, None, None)


def test_the_prompt_asks_for_every_part_of_a_document():
    """An order specification and a separate fee receipt in one PDF: the model left the fee
    out of the lines and the total (#39)."""
    prompt = inv.build_extraction_prompt()
    assert "SEVERAL PARTS" in prompt and "all parts together" in prompt
