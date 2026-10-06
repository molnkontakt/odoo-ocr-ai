"""A supplier abroad (#39): its VAT or company number is read under the label its country
uses ("VAT-nr.", "USt-IdNr.", "CVR", "BTW", "Y-tunnus" …), never the buyer's own number; the
footer line that carries it gives the vendor's name; and when the AI names the buyer as the
vendor, the name read from the document is used. Every party and number is invented (the
Danish CVR 12345674 has a correct check digit)."""

import importlib.util
from pathlib import Path

import invoice_ocr as inv
import pytest

_FIXTURES = (Path(__file__).resolve().parent.parent
             / "account_invoice_ocr_ai" / "tests" / "ocr_fixtures.py")
_spec = importlib.util.spec_from_file_location("ocr_fixtures", _FIXTURES)
fx = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(fx)

OWN_IDS = ["SE999999000601", "999999-0006"]
OWN_NAMES = ["Acme Receiver AB"]

DANISH_TEXT = fx.FOREIGN_INVOICE_TEXT


def _own():
    return inv.build_own_ids(OWN_IDS), inv.build_own_names(OWN_NAMES)


def test_danish_supplier_footer_vat_and_name():
    out = inv.extract_fields(DANISH_TEXT, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert out["org_number"] == "DK12345674"
    assert out["vendor_name"] == "Example Shop"
    assert out["_sources"]["vendor_name"] == "vat_label"
    # the buyer's "Momsnr" is noted as skipped, never taken for the supplier's
    assert out["_own_ids_skipped"] == ["SE999999000601"]
    assert "invoice_number" not in out  # "Faktura 90001234" has no invoice-number label


def test_ais_own_company_vendor_is_replaced_by_the_documents_name():
    own_keys, own_names = _own()
    regex = inv.extract_fields(DANISH_TEXT, own_ids=OWN_IDS, own_names=OWN_NAMES)
    ai = {"vendor_name": "Acme Receiver AB", "invoice_number": "90001234", "total_amount": 2500.8}
    out = inv._merge_fields(DANISH_TEXT, regex, ai, own_keys, own_names=own_names)
    assert out["vendor_name"] == "Example Shop"
    assert out["org_number"] == "DK12345674"
    assert out["invoice_number"] == "90001234"
    assert any("the AI's Acme Receiver AB is the company itself" in n
               and "used Example Shop" in n for n in out["_notes"])
    # the AI's name stays when it is not the buyer's (the regex name only fills the gap)
    out = inv._merge_fields(DANISH_TEXT, regex, {"vendor_name": "Example Shop A/S"}, own_keys,
                            own_names=own_names)
    assert out["vendor_name"] == "Example Shop A/S"
    # without own names (older callers) nothing changes: the AI's name stays
    out = inv._merge_fields(DANISH_TEXT, regex, ai, own_keys)
    assert out["vendor_name"] == "Acme Receiver AB"
    # the buyer's name from the AI and no other name on the document: the AI's stays for
    # the Odoo module's own guard to drop
    out = inv._merge_fields("x", {"org_number": "DK12345674"}, ai, own_keys, own_names=own_names)
    assert out["vendor_name"] == "Acme Receiver AB"


def test_whole_pipeline_on_the_danish_text(monkeypatch):
    monkeypatch.setattr(inv, "_extract_fields_ai", lambda text, reference=None, config=None: {
        "vendor_name": "Acme Receiver AB", "invoice_number": "90001234", "total_amount": 2500.8,
        "subtotal": 2500.8, "vat_amount": 0.0, "lines": []})
    out = inv.extract_invoice_data_from_text(DANISH_TEXT, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert out["vendor_name"] == "Example Shop"
    assert out["org_number"] == "DK12345674"


@pytest.mark.parametrize("line, number", [
    ("USt-IdNr. DE123456789", "DE123456789"),
    ("USt-IdNr.: DE 123 456 789", "DE123456789"),
    ("BTW-nummer: NL123456789B01", "NL123456789B01"),
    ("CVR-nr. 12345674", "DK12345674"),
    ("CVR: 12 34 56 74", "DK12345674"),
    ("Y-tunnus 1234567-8", "FI12345678"),
    ("ALV-tunnus FI12345678", "FI12345678"),
    ("P.IVA 12345678901", "IT12345678901"),
    ("Partita IVA IT12345678901", "IT12345678901"),
    ("NIP 123-456-78-90", "PL1234567890"),
    ("Org.nr 987 654 321 MVA", "NO987654321"),
    ("Foretaksregisteret NO 987 654 321 MVA", "NO987654321"),
    ("N° TVA intracommunautaire : FR12345678901", "FR12345678901"),
    ("UID-Nr. ATU12345678", "ATU12345678"),
    ("VAT ID: ESB12345678", "ESB12345678"),
    ("VAT no. IE1234567T", "IE1234567T"),
    ("VAT Reg. No.: DE812871812", "DE812871812"),
    ("Tax ID GB123456789", "GB123456789"),
    ("Company Reg. No. XI123456789", "XI123456789"),
])
def test_foreign_vat_labels(line, number):
    text = f"Invoice\nExample Supplier\nTotal 100.00\n{line}\n"
    assert inv.supplier_vat_candidates(text) == [(3, number)]
    assert inv.extract_fields(text, own_ids=OWN_IDS, own_names=OWN_NAMES)["org_number"] == number


@pytest.mark.parametrize("line", [
    "Tax ID: 12-3456789",            # no country prefix: a US EIN
    "Company No. 12345678",           # a UK company number without a prefix
    "VAT no: ZZ12345678",             # no VAT prefix
    "Org.nr 556000-0000",             # a Swedish org number: read by the org-number rule
    "Kundnr 0700000001",              # not a VAT label
    "VAT-nr. DK1234",                 # too short
])
def test_not_a_foreign_vat_number(line):
    assert inv.supplier_vat_candidates(f"Invoice\n{line}\n") == []


def test_the_suffix_must_end_the_token():
    """"SE999999001401 Bankgiro 123-4566": the B of Bankgiro is no part of the number, and
    the bankgiro is still read."""
    text = "Faktura\nVAT no SE999999001401 Bankgiro 123-4566\n"
    out = inv.extract_fields(text, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert out["org_number"] == "SE999999001401"
    assert out["bankgiro"] == "123-4566"


def test_own_number_under_a_foreign_label_is_skipped():
    text = "Invoice\nVAT-nr. SE999999000601\nTotal 100.00\n"
    out = inv.extract_fields(text, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert "org_number" not in out
    assert out["_own_ids_skipped"] == ["SE999999000601"]
    # the supplier's Swedish number under the same label, with the buyer's: the supplier's
    text = "Invoice\nMomsnr SE999999000601\nMomsnr: SE999999001401\n"
    out = inv.extract_fields(text, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert out["org_number"] == "SE999999001401"


def test_name_before_the_vat_label():
    own = inv.build_own_names(OWN_NAMES)
    line = "Example Shop - Example Street 17 - 8000 Aarhus - Tlf. 12 34 56 78 - VAT-nr. DK12345674"
    assert inv.name_before_label(line, line.index("VAT-nr"), own) == "Example Shop"
    line = "Example Power AB • Org nr: 999999-0022 • Tel: 08-123 456 • www.example.se"
    assert inv.name_before_label(line, line.index("Org nr"), own) == "Example Power AB"
    line = "Example GmbH, Musterstraße 1, 12345 Berlin, USt-IdNr. DE123456789"
    assert inv.name_before_label(line, line.index("USt"), own) == "Example GmbH"
    # the label starts the line, or something else does: no name
    line = "VAT Reg. No.: DE812871812 info@example.com | www.example.com"
    assert inv.name_before_label(line, 0, own) is None
    line = "Kundnr: 123 - VAT-nr. DK12345674"
    assert inv.name_before_label(line, line.index("VAT-nr"), own) is None
    line = "Tlf. 12 34 56 78 - VAT-nr. DK12345674"
    assert inv.name_before_label(line, line.index("VAT-nr"), own) is None
    line = "Acme Receiver AB - VAT-nr. DK12345674"
    assert inv.name_before_label(line, line.index("VAT-nr"), own) is None


def test_looks_like_company_name():
    own = inv.build_own_names(OWN_NAMES)
    for name in ("Example Shop", "Example Power AB", "Proxy Trading GmbH", "Example Shop a/s"):
        assert inv.looks_like_company_name(name, own), name
    for name in ("ADRESS KONTAKT", "Tel: 08-123 456", "www.example.se", "info@example.se",
                 "12345 Exempelstad", "Acme Receiver AB", "AB", "", None):
        assert not inv.looks_like_company_name(name, own), name
    assert inv.clean_vendor_name("Example Power AB •") == "Example Power AB"
    assert inv.clean_vendor_name(" - Example AB, ") == "Example AB"


def test_column_header_is_not_a_vendor_name():
    """A shop receipt's address block: "ADRESS KONTAKT ORGANISATIONSNUMMER" over the
    columns is a header, not the vendor; the org number is still read."""
    text = ("ORDERNUMMER MOTTAGARE\n12345678 Example Person\n"
            "SUMMA 10490 kr\nVarav moms 2098 kr\n"
            "ADRESS KONTAKT ORGANISATIONSNUMMER\n"
            "Example Hardware Tel: 08-123 456 00 999999-0022\n"
            "AB support@example.se Godkänd för F-skatt\n")
    out = inv.extract_fields(text, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert "vendor_name" not in out
    assert out["org_number"] == "999999-0022"
    # a one-line footer: the name, without the bullet glued to it
    text = "Kvitto\nExample Power AB • Org nr: 999999-0022 • Tel: 08-123 456\n"
    out = inv.extract_fields(text, own_ids=OWN_IDS, own_names=OWN_NAMES)
    assert out["vendor_name"] == "Example Power AB"


def test_country_from_a_vat_number_with_a_letter_group():
    assert inv.country_from_vat("ATU12345678") == "AT"
    assert inv.country_from_vat("ESB12345678") == "ES"
    assert inv.country_from_vat("DK12345674") == "DK"
    assert inv.country_from_vat("Example Shop") is None
