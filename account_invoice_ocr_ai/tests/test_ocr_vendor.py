"""Vendor matching (#13): giro numbers digit for digit, names only among the company's
vendors and on all distinctive words, the commercial partner, and a note on the rule."""
from odoo.tests import tagged

from . import ocr_fixtures as fx
from .common import OcrBillCase


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrVendor(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        Partner = cls.env["res.partner"]
        cls.supplier = Partner.create({"name": "Example Consulting AB", "is_company": True,
                                       "supplier_rank": 1})
        cls.contact = Partner.create({"name": "Invoices", "parent_id": cls.supplier.id})
        cls.env["res.partner.bank"].create(
            {"partner_id": cls.contact.id, "acc_number": "BG " + fx.PLAIN_VENDOR_BANKGIRO})
        # an account number that contains the bankgiro's digits
        cls.other = Partner.create({"name": "Other Sverige AB", "is_company": True,
                                    "supplier_rank": 1})
        cls.env["res.partner.bank"].create(
            {"partner_id": cls.other.id, "acc_number": "99" + "1234566" + "12"})

    def _resolve(self, data):
        notes = []
        pid = self.env["account.move"]._resolve_partner_from_ocr(data, notes=notes)
        return self.env["res.partner"].browse(pid), notes

    def test_giro_compared_digit_for_digit(self):
        partner, notes = self._resolve({"bankgiro": fx.PLAIN_VENDOR_BANKGIRO})
        self.assertEqual(partner, self.supplier, "the commercial partner, not the contact")
        self.assertIn(f"matched on the bankgiro {fx.PLAIN_VENDOR_BANKGIRO}", " ".join(notes))
        partner, _notes = self._resolve({"plusgiro": "12"})
        self.assertFalse(partner, "12 is in other account numbers but is none of them")

    def test_same_giro_on_two_partners_is_no_match(self):
        third = self.env["res.partner"].create({"name": "Third AB", "is_company": True})
        self.env["res.partner.bank"].create(
            {"partner_id": third.id, "acc_number": fx.PLAIN_VENDOR_BANKGIRO})
        partner, notes = self._resolve({"bankgiro": fx.PLAIN_VENDOR_BANKGIRO})
        self.assertFalse(partner)
        self.assertIn("belongs to several partners", " ".join(notes))

    def test_name_needs_all_distinctive_words(self):
        partner, _notes = self._resolve({"vendor_name": "Acme Sverige AB"})
        self.assertFalse(partner, "'Sverige' alone says nothing about the vendor")
        partner, notes = self._resolve({"vendor_name": "EXAMPLE CONSULTING AB (publ)"})
        self.assertEqual(partner, self.supplier)
        self.assertIn("matched on the name", " ".join(notes))
        partner, _notes = self._resolve({"vendor_name": "Example Consulting"})
        self.assertEqual(partner, self.supplier)
        partner, _notes = self._resolve({"vendor_name": "Example Design AB"})
        self.assertFalse(partner)

    def test_name_only_among_vendors(self):
        self.env["res.partner"].create({"name": "Example Customer AB", "is_company": True})
        partner, _notes = self._resolve({"vendor_name": "Example Customer AB"})
        self.assertFalse(partner, "a customer is not a vendor")

    def test_ambiguous_name_is_no_match(self):
        Partner = self.env["res.partner"]
        for name in ("Example Supplier Nord AB", "Example Supplier Syd AB"):
            Partner.create({"name": name, "is_company": True, "supplier_rank": 1})
        partner, notes = self._resolve({"vendor_name": "Example Supplier AB"})
        self.assertFalse(partner)
        self.assertIn("matches several vendors", " ".join(notes))

    def test_matching_rule_in_the_chatter(self):
        ai = {"vendor_name": "Example Consulting AB", "invoice_number": "4711",
              "lines": [{"description": "Consulting", "amount": 1000.0, "vat_rate": 25}]}
        text = fx.PLAIN_INVOICE_TEXT.replace("Org.nr: 999999-0022\n", "")
        move = self._run_ocr(self._new_bill(), text=text, ai=ai)
        self.assertEqual(move.partner_id, self.supplier)
        self.assertEqual(move.partner_bank_id.partner_id, self.contact)
        self.assertIn(f"matched on the bankgiro {fx.PLAIN_VENDOR_BANKGIRO}", self._bodies(move))

    def test_name_only_match_fills_the_vendor_with_a_note(self):
        ai = {"vendor_name": "Example Consulting AB", "invoice_number": "4711",
              "lines": [{"description": "Consulting", "amount": 1000.0, "vat_rate": 25}]}
        move = self._run_ocr(self._new_bill(), text="Faktura\nFakturanummer: 4711\n", ai=ai)
        self.assertEqual(move.partner_id, self.supplier)
        self.assertIn("only, no VAT, org or giro number matched", self._bodies(move))

    def test_preset_vendor_is_kept_and_nothing_created(self):
        before = self.env["res.partner"].search_count([])
        move = self._run_ocr(self._new_bill(partner_id=self.other.id), text=fx.PLAIN_INVOICE_TEXT,
                             ai={"vendor_name": "Brand New Vendor AB", "invoice_number": "4711",
                                 "org_number": "999999-0048"})
        self.assertEqual(move.partner_id, self.other)
        self.assertEqual(self.env["res.partner"].search_count([]), before)
