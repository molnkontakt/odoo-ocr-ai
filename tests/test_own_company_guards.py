"""Own-company, auto-debit and payment-reference guards of the invoice library.

No Odoo, no network: the LLM answer is passed in directly or patched.
"""

import importlib.util
from pathlib import Path

import invoice_ocr as inv
import pytest

_FIXTURES = (Path(__file__).resolve().parent.parent
             / "account_invoice_ocr_ai" / "tests" / "ocr_fixtures.py")
_spec = importlib.util.spec_from_file_location("ocr_fixtures", _FIXTURES)
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)

OWN_IDS = [fx.OWN_VAT, fx.OWN_ORG]
OWN_NAMES = [fx.OWN_NAME]


@pytest.fixture(autouse=True)
def _no_env_fallback(monkeypatch):
    """The environment fallback must not leak into tests that pass own_ids explicitly."""
    monkeypatch.setattr(inv, "OWN_COMPANY", "")
    monkeypatch.setattr(inv, "OWN_VAT_NUMBERS", set())


# -- own identifiers -------------------------------------------------------------


def test_id_keys_equivalent_formats():
    own = inv.build_own_ids(OWN_IDS)
    for v in ("999999-0006", "9999990006", "SE999999000601", "999999000601",
              "SE 999999-0006 01"):
        assert inv.is_own_id(v, own), v
    for v in (fx.VENDOR_ORG, fx.VENDOR_VAT, "", None, "12345"):
        assert not inv.is_own_id(v, own), v


def test_regex_skips_own_org_number_and_finds_seller():
    """The buyer's 'ORG.NR' is printed first on the bank's invoice — it is not the vendor."""
    res = inv.extract_fields(fx.AUTODEBIT_TEXT, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert res["org_number"] == fx.VENDOR_ORG
    assert res["_own_ids_skipped"] == ["9999990006"]
    assert res["invoice_number"] == fx.INVOICE_NUMBER
    # The vendor name must not be the buyer's address lines
    assert "ACMERECEIVER" not in (res.get("vendor_name") or "").upper()
    assert "PERIOD" not in (res.get("vendor_name") or "").upper()


def test_regex_without_own_ids_takes_first_org_number():
    """Without own numbers (no Odoo, no environment) the first org.nr is taken as before."""
    res = inv.extract_fields(fx.AUTODEBIT_TEXT)
    assert res["org_number"] == "9999990006"
    assert not inv._default_own_ids()


def test_environment_fallback_is_used(monkeypatch):
    monkeypatch.setattr(inv, "OWN_VAT_NUMBERS", {fx.OWN_VAT})
    monkeypatch.setattr(inv, "OWN_COMPANY", fx.OWN_NAME.lower())
    res = inv.extract_fields(fx.AUTODEBIT_TEXT)
    assert res["org_number"] == fx.VENDOR_ORG
    assert inv.build_own_names(inv._default_own_names()) == {"acmereceiver"}


def test_only_own_org_number_gives_none():
    text = "Faktura\nKund: Acme Receiver AB\nOrg.nr: 999999-0006\nAtt betala: 100,00\n"
    res = inv.extract_fields(text, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert "org_number" not in res
    assert res["_own_ids_skipped"] == ["999999-0006"]


def test_own_vat_reg_no_skipped():
    text = ("Invoice\nVAT Reg. No.: SE999999000601\nFoo GmbH\n"
            "VAT Reg. No.: DE999999999\nTotal € 10.00\n")
    res = inv.extract_fields(text, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert res["org_number"] == "DE999999999"


def test_momsreg_fallback_skips_own():
    text = ("Faktura\nMomsreg.nr: SE999999000601\nMomsreg.nr: SE999999001401\n"
            "Att betala: 100,00\n")
    res = inv.extract_fields(text, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert res["org_number"] == fx.VENDOR_VAT


def test_plain_invoice_unchanged():
    res = inv.extract_fields(fx.PLAIN_INVOICE_TEXT, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert res["org_number"] == fx.PLAIN_VENDOR_ORG
    assert res["bankgiro"] == fx.PLAIN_VENDOR_BANKGIRO
    assert res["total_amount"] == 1250.0
    assert "_own_ids_skipped" not in res


# -- merge ------------------------------------------------------------------------


def _merge(regex, ai, text="x"):
    return inv._merge_fields(text, regex, ai, inv.build_own_ids(OWN_IDS))


def test_merge_ai_wins_when_regex_org_is_own():
    out = _merge({"org_number": "9999990006"}, {"org_number": fx.VENDOR_ORG})
    assert out["org_number"] == fx.VENDOR_ORG
    assert "9999990006" in out["_own_ids_skipped"]
    assert "_conflicts" not in out


def test_merge_ai_wins_when_regex_org_empty():
    assert _merge({}, {"org_number": fx.VENDOR_ORG})["org_number"] == fx.VENDOR_ORG


def test_merge_own_ai_org_is_dropped():
    assert "org_number" not in _merge({}, {"org_number": "999999-0006"})
    out = _merge({"org_number": fx.VENDOR_ORG}, {"org_number": "9999990006"})
    assert out["org_number"] == fx.VENDOR_ORG


def test_merge_regex_still_wins_between_two_foreign_numbers():
    out = _merge({"org_number": fx.VENDOR_ORG}, {"org_number": "999999-0022"})
    assert out["org_number"] == fx.VENDOR_ORG
    assert any(c.startswith("org_number") for c in out["_conflicts"])
    # the same number in another format is not a conflict
    out = _merge({"org_number": fx.VENDOR_ORG}, {"org_number": "9999990014"})
    assert "_conflicts" not in out


@pytest.mark.parametrize("bad", ["9999-0012345", "9999-00123", "12-34"])
def test_merge_drops_account_number_posing_as_bankgiro(bad):
    out = _merge({}, {"bankgiro": bad})
    assert "bankgiro" not in out
    assert out["_notes"]


@pytest.mark.parametrize("ok", ["123-4567", "1234-5678"])
def test_merge_keeps_real_bankgiro(ok):
    assert _merge({"bankgiro": ok}, {})["bankgiro"] == ok


def test_full_pipeline_on_autodebit_text(monkeypatch):
    monkeypatch.setattr(inv, "_extract_fields_ai",
                        lambda text, reference=None, config=None: dict(fx.AI_ANSWER_OWN_ORG))
    out = inv.extract_invoice_data_from_text(
        fx.AUTODEBIT_TEXT, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert out["org_number"] == fx.VENDOR_ORG
    assert out["vendor_name"] == fx.VENDOR_NAME
    assert out.get("auto_debit")
    assert "bankgiro" not in out  # the buyer's account number, not a bankgiro


# -- own bank accounts ---------------------------------------------------------------


def test_own_bank_numbers():
    bank_keys, account_keys = inv.build_own_bank_keys(
        [fx.OWN_IBAN, "BG" + fx.OWN_BANKGIRO.replace("-", "")])
    assert inv.is_own_bank_number("9999-0012345", bank_keys, account_keys)
    assert inv.is_own_bank_number(fx.OWN_BANKGIRO, bank_keys, account_keys)
    assert not inv.is_own_bank_number("123-4567", bank_keys, account_keys)
    # own account number truncated by the LLM and called a bankgiro
    for truncated in ("9999-0012", "9999-001"):
        assert inv.is_own_bank_number(truncated, bank_keys, account_keys), truncated
    # the start of an own bankgiro is not own (7- and 8-digit bankgiro numbers)
    assert not inv.is_own_bank_number("999-000", bank_keys, account_keys)
    assert not inv.is_own_bank_number("", bank_keys, account_keys)


# -- auto debit -------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    fx.AUTODEBIT_TEXT,
    "Beloppet kommer att debiteras företagets konto per valutadag 2026-07-02",
    "Betalning av fakturan sker med automatik från företagets konto.",
    "Beloppet dras från ert konto den 28:e.",
    "Fakturan betalas via autogiro.",
    "The amount will be debited from your account on 2026-07-01.",
    "Payment method: SEPA Direct Debit",
    "Denna faktura ska inte betalas. Beloppet dras automatiskt från ert konto.",
    "Beloppet kommer att debiteras\nföretagets konto per valutadag 2026-07-02",
    "Betalningssätt: Autogiro\nFörfallodatum 2026-10-28",
    "Der Betrag wird per SEPA-Lastschrift von Ihrem Konto eingezogen.",
])
def test_auto_debit_detected(text):
    assert inv.detect_auto_debit(text)


@pytest.mark.parametrize("text", [
    fx.PLAIN_INVOICE_TEXT,
    "Att betala 1 250,00 till bankgiro 123-4567 senast 2026-06-30.",
    "Anslut till autogiro så slipper du fakturaavgiften!",
    "Pay by direct debit — sign up in your account settings.",
    "",
    # nouns, negations, conditions and lists of payment methods
    "Anmälan om autogiromedgivande finns på mina sidor.",
    "Autogirobetalning är enkelt – läs mer på vår webb.",
    "Betala med autogiro! Fyll i autogiromedgivandet på baksidan.",
    "OBS! Autogirodragning sker ej för denna faktura",
    "Beloppet kommer inte att debiteras ert konto.",
    "Beloppet dras automatiskt om du har autogiro.",
    "Om fakturan betalas via autogiro dras beloppet den 28:e.",
    "Payment options: bank transfer or direct debit.",
    "If you have a direct debit mandate, the amount will be debited on the due date.",
    "Zahlungsarten: Überweisung, Lastschrift",
    "Betalningssätt: autogiro eller bankgiro",
    "Allmänna villkor: Vid betalning via autogiro debiteras ert konto…",
])
def test_auto_debit_not_detected(text):
    assert not inv.detect_auto_debit(text)


def test_auto_debit_condition_only_mutes_its_own_sentence():
    text = "Om du har frågor, kontakta oss.\nBeloppet dras från ert konto den 28:e."
    assert inv.detect_auto_debit(text) == "drasfrånertkonto"


# -- payment reference ------------------------------------------------------------


def test_valid_ocr_is_kept():
    assert inv.valid_payment_reference("1234567897") == "1234567897"
    assert inv.valid_payment_reference("1234 5678 97") == "1234567897"


def test_invoice_number_plus_postal_code():
    # the LLM glued the invoice number to the buyer's postal code
    assert inv.valid_payment_reference("123456789711123", "1234567897") == "1234567897"
    assert not inv.valid_payment_reference("123456789711123")


def test_postal_code_alone_is_dropped():
    assert not inv.valid_payment_reference("11123", "12345678")


def test_letters_are_left_alone():
    assert inv.valid_payment_reference("RF18 5390 0754 7034") == "RF18 5390 0754 7034"
    assert inv.valid_payment_reference("INV-2026-0042") == "INV-2026-0042"


def test_numbers_from_json():
    assert inv.valid_payment_reference(1234567897) == "1234567897"
    assert inv.valid_payment_reference(123456789711123, 1234567897) == "1234567897"
    assert not inv.valid_payment_reference(None)


def test_mod10():
    assert inv.ocr_mod10("1234567897")
    assert not inv.ocr_mod10("1234567890")
