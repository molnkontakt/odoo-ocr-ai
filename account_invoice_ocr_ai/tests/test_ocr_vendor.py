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

    # -- no duplicate vendors (#39) ------------------------------------------------------

    def _vat(self, partner, vat):
        """Store `vat` as it is, as an import or an older version may have (base_vat formats
        what is written through the ORM)."""
        partner.flush_recordset()
        self.env.cr.execute("UPDATE res_partner SET vat = %s WHERE id = %s", (vat, partner.id))
        partner.invalidate_recordset()

    def test_no_new_vendor_next_to_a_vendor_with_a_shorter_name(self):
        """The vendor exists as "Example Market" without a VAT number: a document from
        "Example Market EU S.à r.l." creates no second vendor, it names the existing one."""
        market = self.env["res.partner"].create({"name": "Example Market", "is_company": True,
                                                 "supplier_rank": 1})
        before = self.env["res.partner"].search_count([])
        data = {"vendor_name": "Example Market EU S.à r.l.", "org_number": "999999-0048"}
        for _run in range(2):
            partner, notes = self._resolve(data)
            self.assertFalse(partner)
            self.assertIn("not created because similar vendors exist", " ".join(notes))
            self.assertIn(market.name, " ".join(notes))
        self.assertEqual(self.env["res.partner"].search_count([]), before)
        # without a number nothing would be created anyway: the note still names it
        partner, notes = self._resolve({"vendor_name": "Example Market EU S.a.r.l."})
        self.assertFalse(partner)
        self.assertIn(market.name, " ".join(notes))

    def test_a_new_vendor_is_still_created_when_nothing_is_near(self):
        partner, notes = self._resolve({"vendor_name": "Fjordline Consulting AB",
                                        "org_number": "999999-0048"})
        self.assertTrue(partner)
        self.assertEqual(partner.name, "Fjordline Consulting AB")
        self.assertIn("created from the document", " ".join(notes))

    def test_vat_number_compared_apart_from_formatting(self):
        vendor = self.env["res.partner"].create({"name": "Nordexample Trading", "is_company": True,
                                                 "supplier_rank": 1})
        self._vat(vendor, "SE 999999-0048 01")
        for number in ("999999-0048", "SE999999004801", "SE 9999990048 01"):
            partner, notes = self._resolve({"vendor_name": "Other Name Ltd", "org_number": number})
            self.assertEqual(partner, vendor, number)
            self.assertIn("matched on the", " ".join(notes))
        # a foreign number stored without its prefix
        foreign = self.env["res.partner"].create({"name": "Lux Example SARL", "is_company": True,
                                                  "supplier_rank": 1,
                                                  "country_id": self.env.ref("base.lu").id})
        self._vat(foreign, "1234 5613")
        partner, _notes = self._resolve({"vendor_name": "x", "org_number": "LU12345613"})
        self.assertEqual(partner, foreign)
        partner, notes = self._resolve({"vendor_name": "Another Country GmbH",
                                        "org_number": "DE12345613"})
        self.assertNotEqual(partner, foreign, "another country's number")
        self.assertFalse(partner.vat, "DE12345613 is no valid German VAT number")
        self.assertIn("The VAT number DE12345613 is not valid", " ".join(notes))

    def test_the_same_vat_number_on_two_partners_is_no_match_and_no_new_vendor(self):
        Partner = self.env["res.partner"]
        for name in ("Duo One AB", "Duo Two AB"):
            self._vat(Partner.create({"name": name, "is_company": True}), "SE999999004801")
        before = Partner.search_count([])
        partner, notes = self._resolve({"vendor_name": "Trio Three AB",
                                        "org_number": "999999-0048"})
        self.assertFalse(partner)
        text = " ".join(notes)
        self.assertIn("is the VAT number of several partners", text)
        self.assertIn("not created because similar vendors exist", text)
        self.assertEqual(Partner.search_count([]), before)

    def test_vendor_notes_in_the_users_language(self):
        """The rule note of a match is translated like every other note."""
        self.env["res.lang"]._activate_lang("sv_SE")
        self.env["ir.module.module"]._load_module_terms(["account_invoice_ocr_ai"], ["sv_SE"])
        notes = []
        self.env["account.move"].with_context(lang="sv_SE")._resolve_partner_from_ocr(
            {"bankgiro": fx.PLAIN_VENDOR_BANKGIRO}, notes=notes)
        self.assertIn("Leverantör Example Consulting AB: matchad på", " ".join(notes))
