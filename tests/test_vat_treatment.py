"""VAT treatment of vendor-bill lines: goods or services by account, region by country,
VAT charged or not by the printed VAT (#8, #22). No Odoo, no network."""

import invoice_ocr as inv
import pytest


@pytest.mark.parametrize(("code", "goods"), [
    ("4000", True), ("4400", True), ("4499", True), ("4515", True), ("4529", True),
    ("4545", True), ("45450", True),
    ("4500", False), ("4531", False), ("4535", False), ("4539", False), ("5010", False),
    ("6540", False), ("6231", False), ("7570", False), ("", False), (None, False), ("x", False),
])
def test_goods_or_services_by_account(code, goods):
    assert inv.is_goods_account(code) is goods


@pytest.mark.parametrize(("vat", "country"), [
    ("EL123456783", "GR"), ("XI123456789", "GB"), ("DE123456789", "DE"),
    ("se999999001401", "SE"), ("999999-0014", None), ("", None), (None, None),
])
def test_country_from_vat(vat, country):
    assert inv.country_from_vat(vat) == country


def test_vat_region():
    assert inv.vat_region("SE") == "domestic"
    assert inv.vat_region(None) == "domestic"
    assert inv.vat_region("de") == "eu"
    assert inv.vat_region("GR") == "eu"
    assert inv.vat_region("GB") == "non_eu"
    assert inv.vat_region("US") == "non_eu"


def test_bill_vat_treatment():
    assert inv.bill_vat_treatment("domestic", True, False) == "domestic"
    assert inv.bill_vat_treatment("domestic", False, False) == "domestic"
    assert inv.bill_vat_treatment("eu", False, False) == "reverse_charge"
    assert inv.bill_vat_treatment("non_eu", False, True) == "reverse_charge"
    assert inv.bill_vat_treatment("eu", True, True) == "domestic"       # Swedish VAT
    assert inv.bill_vat_treatment("eu", True, False) == "foreign_vat"   # e.g. a hotel abroad
    assert inv.bill_vat_treatment("non_eu", True, False) == "foreign_vat"


@pytest.mark.parametrize(("code", "rate", "region", "treatment", "xmlid"), [
    # domestic: goods or services by account, Swedish rates only
    ("4000", 25, "domestic", "domestic", "purchase_tax_25_goods"),
    ("6540", 25, "domestic", "domestic", "purchase_tax_25_services"),
    ("5831", 12, "domestic", "domestic", "purchase_tax_12_services"),
    ("5810", 6, "domestic", "domestic", "purchase_tax_6_services"),
    ("4000", 6, "domestic", "domestic", "purchase_tax_6_goods"),
    ("6990", 0, "domestic", "domestic", None),
    ("6540", None, "domestic", "domestic", None),
    ("6540", 19, "domestic", "domestic", None),
    # a foreign supplier charging Swedish VAT is booked like a domestic one
    ("6540", 25, "eu", "domestic", "purchase_tax_25_services"),
    # reverse charge, EU: goods box 20, services box 21
    ("4515", 0, "eu", "reverse_charge", "purchase_goods_tax_25_EC"),
    ("4515", 25, "eu", "reverse_charge", "purchase_goods_tax_25_EC"),
    ("4516", 12, "eu", "reverse_charge", "purchase_goods_tax_12_EC"),
    ("6540", None, "eu", "reverse_charge", "purchase_services_tax_25_EC"),
    ("4535", 6, "eu", "reverse_charge", "purchase_services_tax_6_EC"),
    # reverse charge, outside the EU: import of goods box 50, services box 22
    ("4545", 0, "non_eu", "reverse_charge", "purchase_goods_tax_25_NEC"),
    ("6540", 0, "non_eu", "reverse_charge", "purchase_services_tax_25_NEC"),
    ("4531", 12, "non_eu", "reverse_charge", "purchase_services_tax_12_NEC"),
    # out-of-scope fee lines are never forced to 25 %
    ("6990", 0, "eu", "reverse_charge", None),
    ("6570", None, "non_eu", "reverse_charge", None),
    ("8410", 0, "eu", "reverse_charge", None),
    ("6990", 25, "eu", "reverse_charge", "purchase_services_tax_25_EC"),
    # foreign VAT: no Swedish tax at all
    ("6540", 25, "eu", "foreign_vat", None),
    ("5832", 12, "non_eu", "foreign_vat", None),
])
def test_line_tax_xmlid(code, rate, region, treatment, xmlid):
    assert inv.line_tax_xmlid(code, rate, region, treatment) == xmlid


def test_account_candidates():
    assert inv.account_candidates("4000", "eu", 12, "reverse_charge") == ["4516", "4515", "4000"]
    assert inv.account_candidates("6540", "eu", 0, "reverse_charge") == ["6540"]
    assert inv.account_candidates("6990", "eu", 0, "reverse_charge") == ["6990"]
    assert inv.account_candidates("4000", "eu", 25, "foreign_vat") == ["4000"]
    assert inv.account_candidates("4000", "domestic", 25, "domestic") == ["4000"]
    assert inv.account_candidates(None, "eu", 25, "reverse_charge") == []


def test_document_vat_prefers_printed():
    assert inv.document_vat({"_printed": {"vat_amount": 7.0}, "vat_amount": 9.0}) == 7.0
    assert inv.document_vat({"vat_amount": 0.0}) == 0.0
    assert inv.document_vat({"total_amount": 107.0, "subtotal": 100.0}) == 7.0
    assert inv.document_vat({}) is None


def test_spread_amount_adds_up():
    assert inv.spread_amount([100.0, 200.0], 30.0) == [10.0, 20.0]
    shares = inv.spread_amount([33.33, 33.33, 33.34], 10.0)
    assert round(sum(shares), 2) == 10.0
    assert inv.spread_amount([], 5.0) == []
    assert inv.spread_amount([0.0], 5.0) == [5.0]


def test_se_vat_numbers_skip_the_buyers_own():
    own = inv.build_own_ids(["SE999999000601"])
    text = ("Buyer VAT: SE999999000601\nMoms deklarerat av Example Marketplace S.a.r.l. "
            "Moms # SE 999999-0014 01\nSeller VAT DE123456789")
    assert inv.se_vat_numbers(text, own) == ["SE999999001401"]
    assert inv.se_vat_numbers("no number here") == []


def test_merge_records_supplier_se_vat_numbers():
    own = inv.build_own_ids(["SE999999000601"])
    final = inv._merge_fields("VAT SE999999001401, buyer SE999999000601", {}, {}, own)
    assert final["_se_vat_numbers"] == ["SE999999001401"]
