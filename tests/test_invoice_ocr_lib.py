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


def test_slow_first_call_skips_retry(monkeypatch):
    """If the first AI call took >= RETRY_SKIP_SECONDS, no second call is made.

    The synchronous path in the Odoo model must not block for two full
    timeouts on a slow/hung provider.
    """
    monkeypatch.setattr(inv, "RETRY_SKIP_SECONDS", 0.0)
    calls = []

    def slow_provider(text, cfg=None):
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

    def fast_provider(text, cfg=None):
        calls.append("call")
        return answers.pop(0)

    monkeypatch.setattr(inv, "_call_provider", fast_provider)
    result = inv._extract_fields_ai("text", reference={})
    assert len(calls) == 2
    assert result["total_amount"] == 100
    assert result["subtotal"] == 100


# ── _parse_amount: punkt som tusentalsavgränsare ─────────────────────────────

def test_parse_amount_dot_without_comma_as_thousands_separator():
    # Punkt utan komma och exakt 3 siffror efter sista punkten = tusentalsavgränsare
    assert inv._parse_amount("1.234") == 1234.0
    assert inv._parse_amount("1.234.567") == 1234567.0
    assert inv._parse_amount("SEK 3.020") == 3020.0
    # Två decimaler är fortfarande decimalpunkt
    assert inv._parse_amount("539.00") == 539.0
    assert inv._parse_amount("104.64") == 104.64
    # Blandade format påverkas inte
    assert inv._parse_amount("1.234,56") == 1234.56
    assert inv._parse_amount("1,234.56") == 1234.56
    assert inv._parse_amount("1 234,56") == 1234.56


# ── Eget-bolag-guarden: normalisering + skip-logik ───────────────────────────

def test_own_vat_comparison_is_normalized(monkeypatch):
    """Egen moms med mellanslag/bindestreck ska ändå matcha kandidater i texten."""
    monkeypatch.setattr(inv, "OWN_COMPANY", "example receiver ab")
    # Lagrad form: mellanslag + bindestreck (t.ex. company_registry "556000-0001")
    monkeypatch.setattr(inv, "OWN_VAT_NUMBERS", {"SE 556000-000001"})
    text = (
        "Example Receiver AB\nVAT Reg No: SE556000000001\n"
        "Supplier AB\nOrganisationsnummer 556123-4567\n"
    )
    fields = inv.extract_fields(text)
    # Vår egen VAT ska inte trigga någon överskrivning — Organisationsnumret står kvar
    assert fields["org_number"] == "556123-4567"


def test_org_number_not_overwritten_when_only_se_candidates(monkeypatch):
    """non_se tom och non_own enbart SE-kandidater → ingen överskrivning.

    På en faktura där leverantören är svensk och vi inte kan skilja leverantör
    från kund bland VAT-numren är det säkrare att behålla det som regexen redan
    hittat (t.ex. "Organisationsnummer") än att gissa.
    """
    monkeypatch.setattr(inv, "OWN_COMPANY", "example receiver ab")
    monkeypatch.setattr(inv, "OWN_VAT_NUMBERS", {"SE556000000001"})
    text = (
        "Example Receiver AB\nVAT Reg No: SE556000000001\n"
        "Swedish Other AB\nVAT Reg No: SE556777777701\n"
        "Supplier AB\nOrganisationsnummer 556123-4567\n"
    )
    fields = inv.extract_fields(text)
    assert fields["org_number"] == "556123-4567"


def test_non_se_candidate_still_wins(monkeypatch):
    """Utländsk kandidat (t.ex. DE) vinner fortfarande över svensk org-nr —
    det är det bättre facit på en utländsk faktura."""
    monkeypatch.setattr(inv, "OWN_COMPANY", "example receiver ab")
    monkeypatch.setattr(inv, "OWN_VAT_NUMBERS", {"SE556000000001"})
    text = (
        "Example Receiver AB\nVAT Reg No: SE556000000001\n"
        "Hetzner GmbH\nVAT Reg No: DE812871512\n"
        "Invoice no: 42\n"
    )
    fields = inv.extract_fields(text)
    assert fields["org_number"] == "DE812871512"


# ── _ai_answer_problems ──────────────────────────────────────────────────────

def test_ai_answer_problems_empty_answer():
    assert inv._ai_answer_problems(None) == ["tomt svar"]
    assert inv._ai_answer_problems({}) == ["tomt svar"]


def test_ai_answer_problems_tolerance():
    """Avvikelser inom ±1.00 (öresavrundning) är inte värda en varning."""
    assert inv._ai_answer_problems(
        {"total_amount": 100.50, "subtotal": 100.00, "vat_amount": 0.50,
         "lines": [{"amount": 100.00}]},
        reference={"total_amount": 100.00, "subtotal": 100.00, "vat_amount": 0.00},
    ) == []


def test_ai_answer_problems_token_limit(monkeypatch):
    monkeypatch.setattr(inv, "STAIK_MIN_COMPLETION_TOKENS", 1000)
    good = {"total_amount": 100, "subtotal": 100, "vat_amount": 0,
            "lines": [{"amount": 100}]}
    reference = {"total_amount": 100, "subtotal": 100, "vat_amount": 0}

    # The token floor only applies to reasoning models (name says thinking/reasoning)
    thinking = "qwen3.6:35b-a3b-thinking"
    low = dict(good, _completion_tokens=462, _served_model=thinking)
    problems = inv._ai_answer_problems(low, reference=reference)
    assert any("462" in p for p in problems)

    high = dict(good, _completion_tokens=3400, _served_model=thinking)
    assert inv._ai_answer_problems(high, reference=reference) == []

    # The floor comes from the per-run config, not only from the global
    assert inv._ai_answer_problems(
        low, reference=reference, config={"staik_min_completion_tokens": 400}) == []

    # Ingen usage-rapportering ska inte vara ett problem i sig
    assert inv._ai_answer_problems(good, reference=reference) == []


def test_ai_answer_problems_reference_comparison():
    """Tele2-fallet: internt konsistent men fel mot fakturans tryckta belopp."""
    problems = inv._ai_answer_problems(
        {"total_amount": 2651.25, "subtotal": 2121.00, "vat_amount": 530.25,
         "lines": [{"amount": 2121.00}]},
        reference={"total_amount": 2636.00, "subtotal": 2121.00, "vat_amount": 515.00},
    )
    assert any(p.startswith("total ") for p in problems)
    assert any(p.startswith("moms ") for p in problems)
    # Internt stämmer 2121 + 530.25 = 2651.25 → inget internt problem
    assert not any("!= " in p for p in problems)


def test_ai_answer_problems_missing_field_against_reference():
    problems = inv._ai_answer_problems(
        {"subtotal": 100, "vat_amount": 25, "lines": [{"amount": 100}]},
        reference={"total_amount": 125.00, "subtotal": 100.00, "vat_amount": 25.00},
    )
    assert any(p.startswith("total saknas") for p in problems)


# ── Mergen i extract_invoice_data (REGEX_WINS / conflicts) ────────────────────

def test_extract_invoice_data_merge_regex_wins_and_conflicts(monkeypatch):
    regex_fields = {
        "vendor_name": "Regex Vendor AB",
        "invoice_number": "1033",
        "invoice_date": "2026-03-01",
        "total_amount": 2636.00,
        "subtotal": 2121.00,
        "vat_amount": 515.00,
        "ocr_number": "123456789",
    }
    ai_fields = {
        "vendor_name": "AI Vendor AB",
        "invoice_number": "1033",
        "invoice_date": "2026-03-02",
        "total_amount": 2651.25,   # AI räknat fel — regex ska vinna
        "subtotal": 2121.00,
        "vat_amount": 530.25,      # AI fel — regex ska vinna
        "lines": [{"description": "Molntjänst", "amount": 2121.00, "vat_rate": 25,
                   "account_code": "6231"}],
        "currency": "SEK",
    }

    monkeypatch.setattr(inv, "extract_text", lambda pdf, config=None: "text")
    monkeypatch.setattr(inv, "extract_fields", lambda text, config=None: regex_fields)
    monkeypatch.setattr(inv, "_extract_fields_ai",
                        lambda text, reference=None, config=None: ai_fields)

    data = inv.extract_invoice_data(b"%PDF-fake")

    # REGEX_WINS-fält: regex vinner även när AI har en annan åsikt
    assert data["total_amount"] == 2636.00
    assert data["vat_amount"] == 515.00
    assert data["invoice_date"] == "2026-03-01"
    assert data["invoice_number"] == "1033"
    assert data["ocr_number"] == "123456789"
    # AI fyller det regex inte hittar
    assert data["lines"] == ai_fields["lines"]
    assert data["currency"] == "SEK"
    # Konflikter loggade — även fält AI ändå vinner (vendor_name syns i noten)
    conflicts = data["_conflicts"]
    assert any(c.startswith("total_amount:") for c in conflicts)
    assert any(c.startswith("vat_amount:") for c in conflicts)
    assert any(c.startswith("invoice_date:") for c in conflicts)
    assert not any(c.startswith("invoice_number:") for c in conflicts)  # samma värde
    # Fakturans tryckta belopp separat som facit
    assert data["_printed"] == {"total_amount": 2636.00, "subtotal": 2121.00,
                                "vat_amount": 515.00}


def test_extract_invoice_data_ai_fills_gaps_when_regex_empty(monkeypatch):
    regex_fields = {"total_amount": 100.00}
    ai_fields = {
        "vendor_name": "Only AI AB",
        "invoice_number": "77",
        "total_amount": 100.00,
        "lines": [{"description": "X", "amount": 100.00, "vat_rate": 25,
                   "account_code": "4000"}],
    }
    monkeypatch.setattr(inv, "extract_text", lambda pdf, config=None: "text")
    monkeypatch.setattr(inv, "extract_fields", lambda text, config=None: regex_fields)
    monkeypatch.setattr(inv, "_extract_fields_ai",
                        lambda text, reference=None, config=None: ai_fields)

    data = inv.extract_invoice_data(b"%PDF-fake")
    assert data["vendor_name"] == "Only AI AB"
    assert data["invoice_number"] == "77"
    assert data["total_amount"] == 100.00
    assert "_conflicts" not in data


def test_extract_invoice_data_config_is_passed_through(monkeypatch):
    """Config-dicten ska flöda hela vägen ner (provider, nycklar, OCR-gränser)."""
    seen = {}

    def fake_ai(text, reference=None, config=None):
        seen["config"] = config
        return {"total_amount": 1.0, "lines": [{"amount": 1.0}]}

    monkeypatch.setattr(inv, "extract_text", lambda pdf, config=None: "text")
    monkeypatch.setattr(inv, "extract_fields", lambda text, config=None: {})
    monkeypatch.setattr(inv, "_extract_fields_ai", fake_ai)

    inv.extract_invoice_data(b"%PDF-fake", config={"provider": "openai",
                                                   "openai_api_key": "sk-test"})
    cfg = seen["config"]
    assert cfg["provider"] == "openai"
    assert cfg["openai_api_key"] == "sk-test"
    # Nycklar som inte sattes i configen behåller sina defaults
    assert cfg["staik_url"] == inv.STAIK_URL
    assert cfg["text_limit"] == inv.TEXT_LIMIT


# ── remap_account_code (EU / EX-vägar) ───────────────────────────────────────

def test_remap_account_code_domestic_unchanged():
    assert inv.remap_account_code("4000") == "4000"
    assert inv.remap_account_code("6231", is_eu_foreign=False, is_outside_eu=False) == "6231"


def test_remap_account_code_eu_reverse_charge():
    assert inv.remap_account_code("4000", is_eu_foreign=True) == "4515"
    assert inv.remap_account_code(4000, is_eu_foreign=True) == "4515"
    assert inv.remap_account_code("4099", is_eu_foreign=True) == "4515"
    assert inv.remap_account_code("4510", is_eu_foreign=True) == "4535"
    assert inv.remap_account_code("4590", is_eu_foreign=True) == "4535"
    # Kostnadsklasser (5xxx/6xxx) remappas inte
    assert inv.remap_account_code("6231", is_eu_foreign=True) == "6231"
    assert inv.remap_account_code("5010", is_eu_foreign=True) == "5010"


def test_remap_account_code_outside_eu():
    assert inv.remap_account_code("4000", is_outside_eu=True) == "4545"
    assert inv.remap_account_code("4050", is_outside_eu=True) == "4545"
    # Utanför EU remappas bara varor; 45xx och kostnadsklasser lämnas orörda
    assert inv.remap_account_code("4510", is_outside_eu=True) == "4510"
    assert inv.remap_account_code("6231", is_outside_eu=True) == "6231"


def test_remap_account_code_unparsable_passthrough():
    assert inv.remap_account_code(None, is_eu_foreign=True) is None
    assert inv.remap_account_code("", is_eu_foreign=True) == ""
    assert inv.remap_account_code("ab12", is_eu_foreign=True) == "ab12"


# ── Config-injektion ─────────────────────────────────────────────────────────

def test_default_config_has_all_keys():
    cfg = inv.default_config()
    for key in ("provider", "venice_api_key", "venice_model", "openai_api_key",
                "openai_model", "staik_url", "staik_api_key", "staik_model",
                "base_url", "api_key", "model", "timeout", "ollama_url", "ollama_model",
                "staik_timeout", "retry_skip_seconds",
                "staik_min_completion_tokens", "own_ids",
                "own_names", "text_limit", "max_ocr_pages", "ocr_scale"):
        assert key in cfg, key


def test_cfg_none_values_do_not_wipe_defaults():
    cfg = inv._cfg({"provider": None, "staik_model": "custom-model"})
    assert cfg["provider"] == inv.AI_PROVIDER
    assert cfg["staik_model"] == "custom-model"


def test_own_company_passed_via_config_not_globals():
    """extract_fields ska läsa eget bolag ur configen — globalerna behöver inte
    muteras (det var själva felet)."""
    text = (
        "Example Receiver AB\nVAT Reg No: SE556000000001\n"
        "Supplier Ltd\nVAT Reg No: GB123456789\nInvoice no: 42\nTotal: 100.00\n"
    )
    before = (inv.OWN_COMPANY, set(inv.OWN_VAT_NUMBERS))
    fields = inv.extract_fields(text, config={
        "own_names": ["Example Receiver AB"],
        "own_ids": ["SE 556000-000001"],  # normalized inside the library
    })
    assert "receiver" not in (fields.get("vendor_name") or "").lower()
    assert fields["org_number"] == "GB123456789"
    assert (inv.OWN_COMPANY, set(inv.OWN_VAT_NUMBERS)) == before


def test_own_identities_flow_through_the_pipeline_config(monkeypatch):
    """extract_invoice_data(config=…) hands the config's own ids to the regex step and
    the merge, explicit own_ids override it, and the env globals stay untouched."""
    monkeypatch.setattr(inv, "OWN_COMPANY", "")
    monkeypatch.setattr(inv, "OWN_VAT_NUMBERS", set())
    text = "Faktura\nKund: Example Receiver AB\nOrg.nr: 556000-0001\nAtt betala: 100,00\n"
    monkeypatch.setattr(inv, "extract_text", lambda pdf, config=None: text)
    monkeypatch.setattr(inv, "_extract_fields_ai",
                        lambda text, reference=None, config=None: {"org_number": "556000-0001"})
    cfg = {"own_ids": ["SE556000000101"], "own_names": ["Example Receiver AB"]}
    out = inv.extract_invoice_data(b"%PDF-fake", config=cfg)
    assert "org_number" not in out, "the buyer's own org.nr is never the supplier's"
    assert "556000-0001" in out["_own_ids_skipped"]
    # explicit own_ids win over the config: nothing is own any more
    out = inv.extract_invoice_data(b"%PDF-fake", config=cfg, own_ids=[])
    assert out["org_number"] == "556000-0001"
    assert inv.OWN_COMPANY == "" and not inv.OWN_VAT_NUMBERS


# ── _parse_amount: a lone comma or dot followed by three digits groups thousands (#15) ──

def test_parse_amount_comma_as_thousands_separator():
    assert inv._parse_amount("1,234") == 1234.0
    assert inv._parse_amount("$1,234") == 1234.0
    assert inv._parse_amount("1,234,567") == 1234567.0
    assert inv._parse_amount("1.234.567") == 1234567.0
    assert inv._parse_amount("12.500") == 12500.0
    assert inv._parse_amount("1.234,5") == 1234.5
    # decimals stay decimals
    assert inv._parse_amount("12,50") == 12.5
    assert inv._parse_amount("12.50") == 12.5
    assert inv._parse_amount("1234,56") == 1234.56
    assert inv._parse_amount("0,500") == 0.5
    assert inv._parse_amount("-32,00") == -32.0
    assert inv._parse_amount("1 234,56") == 1234.56
    # whole-krona marks and odd spaces
    assert inv._parse_amount("418:-") == 418.0
    assert inv._parse_amount("418,-") == 418.0
    assert inv._parse_amount("1 000") == 1000.0
    assert inv._parse_amount("abc") is None
    assert inv._parse_amount("1.23.45") is None


def test_grand_total_with_comma_thousands_separator():
    assert inv.extract_fields("Grand total $1,234\n")["total_amount"] == 1234.0


# ── Dates: only real calendar dates, English months, anchored labels (#16) ────

def test_parse_date_returns_only_valid_iso_dates():
    assert inv._parse_date("3 March 2026") == "2026-03-03"
    assert inv._parse_date("12 May 2026") == "2026-05-12"
    assert inv._parse_date("March 3, 2026") == "2026-03-03"
    assert inv._parse_date("Sep 17, 2026") == "2026-09-17"
    assert inv._parse_date("17 sept. 2026") == "2026-09-17"
    assert inv._parse_date("1 mars 2026") == "2026-03-01"
    assert inv._parse_date("1 okt 2026") == "2026-10-01"
    assert inv._parse_date("2026/09/17") == "2026-09-17"
    assert inv._parse_date("9.4.2026") == "2026-04-09"
    # one valid reading only: the mm/dd one
    assert inv._parse_date("09/15/2026") == "2026-09-15"
    for junk in ("2026-02-30", "2026-15-09", "17/09/26", "170917", "null", "N/A", "",
                 "3 Foo 2026", None):
        assert inv._parse_date(junk) is None, junk


def test_ambiguous_slash_date_has_two_readings():
    assert inv._date_readings("09/04/2026") == ["2026-04-09", "2026-09-04"]
    assert inv._date_readings("12/12/2026") == ["2026-12-12"]
    assert inv.iso_date("09/04/2026") == "2026-04-09"


def test_datum_label_is_anchored():
    text = "Leveransdatum 2026-02-01\nFaktura\nDatum 2026-03-05\n"
    assert inv.extract_fields(text)["invoice_date"] == "2026-03-05"
    text = "Förfallodatum: 2026-04-04\nDatum: 2026-03-05\n"
    fields = inv.extract_fields(text)
    assert fields["invoice_date"] == "2026-03-05"
    assert fields["due_date"] == "2026-04-04"
    assert "invoice_date" not in inv.extract_fields("Orderdatum 2026-01-20\n")


def test_unparsable_regex_date_never_wins():
    assert inv.extract_fields("Invoice date: 3 March 2026\n")["invoice_date"] == "2026-03-03"
    fields = inv.extract_fields("Fakturadatum: 2026-02-30\nDatum 2026-03-05\n")
    assert fields["invoice_date"] == "2026-03-05"
    assert "invoice_date" not in inv.extract_fields("Fakturadatum: 2026-02-30\n")
    assert inv.extract_fields("Due date: 09/15/2026\n")["due_date"] == "2026-09-15"


def _merge_dates(regex_text, ai):
    regex = inv.extract_fields(regex_text)
    return inv._merge_fields(regex_text, regex, ai, set())


def test_ambiguous_date_takes_the_ais_reading():
    text = "Invoice date: 09/04/2026\n"
    out = _merge_dates(text, {"invoice_date": "2026-09-04"})
    assert out["invoice_date"] == "2026-09-04"
    assert any("2026-04-09 or 2026-09-04" in n for n in out["_notes"])
    assert "_conflicts" not in out
    out = _merge_dates(text, {"invoice_date": "2026-04-09"})
    assert out["invoice_date"] == "2026-04-09" and "_notes" not in out
    # no AI date (or another one): day/month, with a note
    out = _merge_dates(text, {})
    assert out["invoice_date"] == "2026-04-09"
    assert any("day/month" in n for n in out["_notes"])
    out = _merge_dates(text, {"invoice_date": "2026-01-01"})
    assert out["invoice_date"] == "2026-04-09"
    assert any(c.startswith("invoice_date:") for c in out["_conflicts"])


def test_invalid_ai_date_is_dropped_with_a_note():
    out = _merge_dates("no dates here\n", {"invoice_date": "2026-02-30", "due_date": "N/A"})
    assert "invoice_date" not in out and "due_date" not in out
    assert sum("not a valid date" in n for n in out["_notes"]) == 2
    # a valid AI due date still wins over the regex one (unchanged)
    out = _merge_dates("Förfallodatum: 2026-04-04\n", {"due_date": "2026-04-05"})
    assert out["due_date"] == "2026-04-05"


# ── Malformed AI answers (#10) and line amounts (#11) ─────────────────────────

MALFORMED_LINES = (["a"], [None], {"x": 1}, "abc", 5, [{"amount": "1 234,00"}],
                   [{"amount": None, "unit_price": None}], [[1, 2]])


def test_ai_answer_problems_never_raises():
    for lines in MALFORMED_LINES:
        data = {"subtotal": 100, "vat_amount": 25, "total_amount": 125, "lines": lines}
        problems = inv._ai_answer_problems(data, reference={"subtotal": 100})
        assert isinstance(problems, list) and problems, lines
    # odd diagnostics and references do not raise either
    assert isinstance(inv._ai_answer_problems(
        {"lines": [{"amount": 1}], "_completion_tokens": "many"}, reference="x"), list)


def test_sanitize_ai_keeps_lists_of_dicts_and_numbers():
    out = inv._sanitize_ai({
        "vendor_name": " Example AB ", "invoice_number": 1033, "ocr_number": 1234567897.0,
        "total_amount": "1 250,00", "subtotal": None, "vat_amount": "n/a",
        "currency": "null", "org_number": {"x": 1},
        "lines": ["a", None, {"description": None, "quantity": None, "unit_price": None,
                               "amount": "1 000,00", "vat_rate": "25", "account_code": 6540},
                  {"description": "no amount"}, {"unit_price": 50, "quantity": 2}],
        "_completion_tokens": 3400, "_served_model": None,
    })
    assert out == {
        "vendor_name": "Example AB", "invoice_number": "1033", "ocr_number": "1234567897",
        "total_amount": 1250.0,
        "lines": [{"amount": 1000.0, "vat_rate": 25.0, "account_code": "6540"},
                  {"unit_price": 50.0, "quantity": 2.0, "amount": 100.0}],
        "_completion_tokens": 3400,
    }
    for lines in MALFORMED_LINES[:5]:
        assert inv._sanitize_ai({"lines": lines}).get("lines", []) == [], lines
    assert inv._sanitize_ai("not a dict") == {}
    assert inv._sanitize_ai(None) == {}


def test_malformed_answer_keeps_the_regex_fields(monkeypatch):
    text = "Fakturanummer: 4711\nFakturadatum: 2026-03-01\nAtt betala: 125,00\nOCR: 1234567897\n"
    for lines in MALFORMED_LINES:
        monkeypatch.setattr(inv, "_call_provider", lambda t, cfg=None, lines=lines: inv._sanitize_ai({
            "subtotal": 100, "vat_amount": 25, "total_amount": 125, "lines": lines}))
        out = inv.extract_invoice_data_from_text(text, config={"retry_skip_seconds": 0})
        assert out["invoice_number"] == "4711" and out["invoice_date"] == "2026-03-01", lines
        assert out["total_amount"] == 125.0 and out["ocr_number"] == "1234567897"


def test_ai_step_failure_keeps_the_regex_fields(monkeypatch):
    def broken(*args, **kwargs):
        raise TypeError("unexpected")

    monkeypatch.setattr(inv, "_extract_fields_ai", broken)
    out = inv.extract_invoice_data_from_text("Fakturanummer: 4711\nAtt betala: 125,00\n")
    assert out["invoice_number"] == "4711" and out["total_amount"] == 125.0
    assert any("the AI step failed" in n for n in out["_notes"])


def test_line_amount_is_the_source_of_truth():
    q = inv.line_quantity_and_price
    assert q({"quantity": 3, "unit_price": 100, "amount": 300}) == (3, 100)
    assert q({"quantity": 3, "amount": 300}) == (3, 100.0)       # unit price missing
    assert q({"amount": 250}) == (1.0, 250)                       # null quantity/unit price
    assert q({"quantity": 1, "unit_price": 500, "amount": 400}) == (1.0, 400)  # discount in amount
    assert q({"quantity": 3, "unit_price": 50, "amount": 100}) == (1.0, 100)   # 3 × 33.33 ≠ 100
    assert q({"quantity": -1, "unit_price": 100, "amount": -100}) == (-1, 100)
    assert q({"amount": 0}) is None and q({}) is None
