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

    low = dict(good, _completion_tokens=462)
    problems = inv._ai_answer_problems(low, reference=reference)
    assert any("462" in p for p in problems)

    high = dict(good, _completion_tokens=3400)
    assert inv._ai_answer_problems(high, reference=reference) == []

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
                "staik_timeout", "retry_skip_seconds",
                "staik_min_completion_tokens", "own_company",
                "own_vat_numbers", "text_limit", "max_ocr_pages", "ocr_scale"):
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
    fields = inv.extract_fields(text, config={
        "own_company": "example receiver ab",
        "own_vat_numbers": {"SE556000000001"},
    })
    assert "receiver" not in (fields.get("vendor_name") or "").lower()
    assert fields["org_number"] == "GB123456789"
