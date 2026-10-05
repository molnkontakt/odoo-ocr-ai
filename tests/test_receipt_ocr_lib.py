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


def test_low_confidence_leaves_amount_and_date_empty():
    fields = {"total": 418.0, "date": "2026-09-17", "confidence": 0.3,
              "items": "Alkylatbensin", "category_code": "MASKIN"}
    kept, notes = r._apply_guards(fields, TEXT)
    assert "total" not in kept and "date" not in kept
    # the documented behaviour (#36.6): description and category may still be filled
    assert kept["items"] == "Alkylatbensin" and kept["category_code"] == "MASKIN"
    assert any("low confidence (0.30)" in n for n in notes)


def test_missing_confidence_counts_as_low():
    kept, notes = r._apply_guards({"total": 418.0, "date": "2026-09-17"}, TEXT)
    assert "total" not in kept and "date" not in kept
    assert any("no confidence" in n for n in notes)
    kept, notes = r._apply_guards({"total": 418.0, "date": "2026-09-17", "confidence": 0.9}, TEXT)
    assert kept["total"] == 418.0 and kept["date"] == "2026-09-17" and not notes


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


# ── Receipt dates: validated (#30) and checked against the text (#31) ─────────

def test_clean_drops_invalid_dates():
    for bad in ("17/09/26", "170917", "2026-02-30", "Sep 31, 2026"):
        out = r._clean({"date": bad}, [])
        assert "date" not in out and out["_bad_date"] == bad
    for placeholder in ("null", "N/A", " "):
        assert r._clean({"date": placeholder}, []) == {}
    assert r._clean({"date": "2026-09-17"}, [])["date"] == "2026-09-17"
    assert r._clean({"date": "17.09.2026"}, [])["date"] == "2026-09-17"


def test_date_in_text_formats():
    for printed in ("2026-09-17", "2026.09.17", "2026/9/17", "20260917", "17/09/2026",
                    "17.9.2026", "17-09-26", "260917", "09/17/2026", "17 sep 2026",
                    "Sep 17, 2026", "Datum 2026-09-17 10:14"):
        assert r._date_in_text("2026-09-17", f"Kvitto {printed} x"), printed
    assert not r._date_in_text("2026-09-17", "Kvitto 2026-09-18")
    assert not r._date_in_text("2020-09-17", TEXT)
    assert not r._date_in_text("2026-09-17", "Art 20260917123")
    assert not r._date_in_text("not a date", TEXT)


def test_unseen_ai_date_falls_back_to_the_printed_date():
    regex = r._regex_fields(TEXT)
    kept, notes = r._apply_guards({"date": "2020-09-17", "confidence": 0.9}, TEXT, regex)
    assert kept["date"] == "2026-09-17"
    assert any("2020-09-17" in n for n in notes)
    kept, notes = r._apply_guards({"date": "2020-09-17", "confidence": 0.9},
                                  "Totalt 418,00\n", {})
    assert "date" not in kept and any("ignored" in n for n in notes)
    kept, notes = r._apply_guards({"date": "2026-09-17", "confidence": 0.9}, "x 260917 y", {})
    assert kept["date"] == "2026-09-17" and not notes


def test_regex_date_reads_day_first_dates():
    assert r._regex_fields("Kvitto 17/09/2026 10:14")["date"] == "2026-09-17"
    assert r._regex_fields("Kvitto 17.09.2026")["date"] == "2026-09-17"
    assert "date" not in r._regex_fields("Kvitto 31.02.2026")


def test_bad_ai_date_keeps_the_regex_date(monkeypatch):
    monkeypatch.setattr(r, "read_text", lambda *a, **k: TEXT)
    monkeypatch.setattr(r, "_chat_json", lambda *a, **k: {
        "total": 418.0, "date": "17/09/26", "confidence": 0.9})
    out = r.extract_receipt_data(b"x", "image/jpeg", "receipt.jpg")
    assert out["fields"]["date"] == "2026-09-17"
    assert any("17/09/26" in n for n in out["notes"])


# ── Merchant guard: distinctive words, whole words, in order (#36.8) ──────────

def test_merchant_guard_needs_the_distinctive_words():
    m = r._merchant_in_text
    assert not m("Chain A Sverige", "Chain B Sverige AB\nKvitto 1")
    assert m("Chain A Sverige", "CHAIN A SVERIGE AB\nKvitto 1")
    assert m("Chain A", "Chain A Sverige AB")
    assert not m("Foo Maxi", "maximal discount today")
    assert m("Foo Maxi", "FOO MAXI\nKvitto")
    assert m("X&Y", "X&Y\nKvitto")
    assert m("X&Y", "X & Y Store")
    assert not m("X&Y", "Xavier Young")
    assert m("Foo Sverige Bar", "FOO SVERIGE BAR AB")
    assert m("Sverige AB", "Kvitto Sverige AB")       # generic words only: compared whole
    assert not m("Sverige AB", "Kvitto Sverige")
    assert not m("", TEXT) and not m(None, TEXT) and not m("&", TEXT)


def test_regex_skips_foreign_currency_totals():
    """A total in EUR is not read as SEK when the AI is not there to read the currency (#28)."""
    f = r._regex_fields("CAFE\nTOTAL EUR 12,50\n")
    assert "total" not in f
    assert f["_skipped_currency"] == "EUR"
    assert r._regex_fields("CAFE\nTotal € 12,50\n")["_skipped_currency"] == "EUR"
    assert r._regex_fields("Köp 1049,00 SEK\n")["total"] == 1049.0
    # a SEK total line wins over a foreign one
    assert r._regex_fields("Totalt 418,00\nTotal EUR 37,00\n")["total"] == 418.0


def test_regex_only_foreign_total_is_noted(monkeypatch):
    monkeypatch.setattr(r, "read_text", lambda *a, **k: "CAFE EXEMPEL\n2026-09-17\nTOTAL EUR 12,50\n")
    monkeypatch.setattr(r, "_chat_json", lambda *a, **k: {})
    out = r.extract_receipt_data(b"x", "image/jpeg", "receipt.jpg")
    assert out["source"] == "regex"
    assert "total" not in out["fields"]
    assert any("the total is in EUR" in n for n in out["notes"])


# ── AI diagnostics, the token limit and the reasoning check (#36.9) ──────────

import invoice_ocr as inv  # noqa: E402

ANSWER = {"merchant": "Kvitto", "date": "2026-09-17", "total": 418.0, "items": "Bensin",
          "category_code": None, "confidence": 0.9}


def _fake_chat(monkeypatch, answers, seen=None):
    """inv.chat_json answers `answers` in order: (data, meta) pairs."""
    answers = list(answers)

    def chat_json(prompt, text, schema, name, max_tokens=None, max_chars=None, timeout=None,
                  config=None):
        if seen is not None:
            seen.append({"max_tokens": max_tokens, "max_chars": max_chars})
        return answers.pop(0)

    monkeypatch.setattr(inv, "chat_json", chat_json)
    monkeypatch.setattr(r, "read_text", lambda *a, **k: TEXT)


def _meta(tokens=2000, model="m", served=None, finish="stop", notes=()):
    return {"model": model, "served_model": served or model, "completion_tokens": tokens,
            "finish_reason": finish, "notes": list(notes)}


def test_diagnostics_are_kept_and_reported(monkeypatch):
    seen = []
    _fake_chat(monkeypatch, [(dict(ANSWER), _meta(tokens=812, model="x-thinking", served="x"))],
               seen)
    out = r.extract_receipt_data(b"x", "image/jpeg", "r.jpg",
                                 config={"max_tokens": 12000, "text_limit": 3000})
    assert out["ai"]["_model"] == "x-thinking" and out["ai"]["_served_model"] == "x"
    assert out["ai"]["_completion_tokens"] == 812 and out["ai"]["_finish_reason"] == "stop"
    assert not any(k.startswith("_") for k in out["fields"]), "no diagnostics among the fields"
    assert out["fields"]["total"] == 418.0
    # the token limit is the config's (no fixed 4000), the text at most 4000 characters
    assert seen == [{"max_tokens": 12000, "max_chars": 3000}]
    _fake_chat(monkeypatch, [(dict(ANSWER), _meta())], seen)
    r.extract_receipt_data(b"x", "image/jpeg", "r.jpg")
    assert seen[-1] == {"max_tokens": inv.AI_MAX_TOKENS, "max_chars": r.RECEIPT_TEXT_LIMIT}


def test_skipped_reasoning_is_read_once_more(monkeypatch):
    skipped = (dict(ANSWER, total=339.0), _meta(tokens=120, model="x-thinking", served="x"))
    sound = (dict(ANSWER), _meta(tokens=900, model="x-thinking", served="x"))
    _fake_chat(monkeypatch, [skipped, sound])
    out = r.extract_receipt_data(b"x", "image/jpeg", "r.jpg")
    assert out["fields"]["total"] == 418.0 and out["ai"]["_completion_tokens"] == 900
    assert not any("skipped its reasoning" in n for n in out["notes"])
    # both skipped: the first answer, with a note
    _fake_chat(monkeypatch, [skipped, skipped])
    out = r.extract_receipt_data(b"x", "image/jpeg", "r.jpg")
    assert any("only 120 completion tokens" in n for n in out["notes"])
    # a plain model answering briefly is fine: one call, no note
    _fake_chat(monkeypatch, [(dict(ANSWER), _meta(tokens=120, model="gpt-4o-mini"))])
    out = r.extract_receipt_data(b"x", "image/jpeg", "r.jpg")
    assert not any("reasoning" in n for n in out["notes"])


def test_a_cut_answer_is_read_once_more_and_noted(monkeypatch):
    cut = ({}, _meta(finish="length", notes=["the AI's answer was cut off at its token limit"]))
    _fake_chat(monkeypatch, [cut, cut])
    out = r.extract_receipt_data(b"x", "image/jpeg", "r.jpg")
    assert out["source"] == "regex" and out["fields"]["total"] == 418.0
    assert any("cut off at its token limit" in n for n in out["notes"])
    assert any("no usable answer" in n for n in out["notes"])
    _fake_chat(monkeypatch, [cut, (dict(ANSWER), _meta())])
    out = r.extract_receipt_data(b"x", "image/jpeg", "r.jpg")
    assert out["source"] == "ai" and not any("cut off" in n for n in out["notes"])


def test_no_re_read_when_the_first_call_was_slow(monkeypatch):
    # the receipt's deadline starts at 0, the call starts at 0 and ends at 70 s
    clock = iter([0.0, 0.0, 70.0])
    monkeypatch.setattr(inv, "_clock", lambda: next(clock, 70.0))
    seen = []
    skipped = (dict(ANSWER), _meta(tokens=120, model="x-thinking"))
    _fake_chat(monkeypatch, [skipped, skipped], seen)
    out = r.extract_receipt_data(b"x", "image/jpeg", "r.jpg", config={"total_deadline": 200})
    assert len(seen) == 1, "70 s is more than retry_skip_seconds (60 s): no second call"
    assert any("only 120 completion tokens" in n for n in out["notes"])


def test_prompt_has_no_category_rules_of_its_own():
    """Category rules belong in the company's category descriptions, not the prompt (#36.10)."""
    for word in ("Fuel", "fuel", "chain lubricant", "spare parts", "machinery"):
        assert word not in r.PROMPT, word
    prompt = r.build_prompt([("MASKIN", "Machinery", "fuel, oil, tools"), ("FOOD", "Meals")])
    assert "  MASKIN — Machinery: fuel, oil, tools\n  FOOD — Meals\n" in prompt
    assert "Follow each category's description" in prompt
