"""A supplier abroad whose invoice names the buyer at the top (#39): the vendor is found on
the VAT number printed under its country's label in the footer — or created with the footer's
name — and its lines get the EU purchase tax. A receipt without an invoice number gets its
order or booking number as the reference, and the fill note names the total the posting check
keeps. All parties and numbers are invented (see ocr_fixtures)."""
from odoo.tests import tagged

from . import ocr_fixtures as fx
from .common import OcrBillCase

EU_GOODS_LINES = [{"description": "Example Router 5G", "amount": 2500.8, "vat_rate": 0,
                   "account_code": "5410"}]
TABLET_LINES = [{"description": "Example tablet", "amount": 8392.0, "vat_rate": 25,
                 "account_code": "5410"}]
TRAIN_LINES = [{"description": "Train", "amount": 89.62, "vat_rate": 6, "account_code": "5810"}]


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrForeignVendor(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls._se_company(fx.OWN_NAME, vat=fx.OWN_VAT, company_registry=fx.OWN_ORG)
        cls.env.user.company_ids |= cls.company

    def _bill(self, text=fx.FOREIGN_INVOICE_TEXT, partner=None, **ai):
        move = self.env["account.move"].with_company(self.company).create(
            {"move_type": "in_invoice", "partner_id": partner.id if partner else False})
        # the AI names the buyer as the vendor, as it did on the real document
        answer = {"vendor_name": fx.OWN_NAME, "invoice_number": "90001234",
                  "total_amount": 2500.8, "subtotal": 2500.8, "vat_amount": 0.0,
                  "currency": "SEK", "lines": [dict(line) for line in EU_GOODS_LINES], **ai}
        return self._run_ocr(move, text=text, ai=answer)

    def test_vendor_matched_on_the_footer_vat_number(self):
        """The partner carries the number: no vendor is created, the lines are an EU purchase
        of goods (box 20) on 5410."""
        shop = self.env["res.partner"].create({
            "name": "Example Shop A/S", "is_company": True, "supplier_rank": 1,
            "vat": fx.FOREIGN_VENDOR_VAT, "country_id": self.env.ref("base.dk").id})
        before = self.env["res.partner"].search_count([])
        move = self._bill()
        self.assertEqual(move.partner_id, shop)
        self.assertEqual(self.env["res.partner"].search_count([]), before)
        bodies = self._bodies(move)
        self.assertIn(f"matched on the VAT number {fx.FOREIGN_VENDOR_VAT}", bodies)
        self.assertIn("is the company itself (the buyer) – used Example Shop", bodies)
        self.assertIn(f"The org number {fx.OWN_VAT} on the bill is the company's own", bodies)
        self.assertEqual(move.ref, "90001234")
        line = move.invoice_line_ids
        self.assertEqual(line.account_id.code, "5410")
        self.assertEqual(line.tax_ids, self._tax(self.company, "purchase_goods_tax_25_EC"))
        self.assertIn("se_20", line.tax_tag_ids.mapped("name"))
        self.assertAlmostEqual(move.amount_total, 2500.8)

    def test_vendor_created_from_the_footer_name(self):
        before = self.env["res.partner"].search_count([])
        move = self._bill()
        partner = move.partner_id
        self.assertEqual(partner.name, fx.FOREIGN_VENDOR_NAME)
        self.assertEqual(partner.vat, fx.FOREIGN_VENDOR_VAT)
        self.assertEqual(partner.country_id.code, "DK")
        self.assertEqual(self.env["res.partner"].search_count([]), before + 1)
        self.assertIn("created from the document", self._bodies(move))
        self.assertEqual(move.invoice_line_ids.tax_ids,
                         self._tax(self.company, "purchase_goods_tax_25_EC"))

    def test_number_on_no_partner_and_no_other_name(self):
        """The footer gives no name: the vendor stays empty and the note names the number."""
        text = ("Faktura 90001234\nMomsnr SE999999000601\nAcme Receiver AB\n"
                "Totalt belopp (SEK) 2 500,80\nVAT-nr. DK12345674\n")
        before = self.env["res.partner"].search_count([])
        move = self._bill(text=text)
        self.assertFalse(move.partner_id)
        self.assertEqual(self.env["res.partner"].search_count([]), before)
        bodies = self._bodies(move)
        self.assertIn('The vendor name "Acme Receiver AB" is the company itself', bodies)
        self.assertIn("No partner has the VAT number DK12345674 printed on the document, and it "
                      "gives no other vendor name", bodies)

    def test_own_company_preset_is_replaced(self):
        """The bill was created with the buyer as vendor: the footer's vendor replaces it."""
        shop = self.env["res.partner"].create({
            "name": "Example Shop A/S", "is_company": True, "supplier_rank": 1,
            "vat": fx.FOREIGN_VENDOR_VAT})
        move = self._bill(partner=self.company.partner_id)
        self.assertEqual(move.partner_id, shop)
        self.assertIn("replaced by the vendor from the document", self._bodies(move))


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrReceiptReference(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls._se_company("Example Buyer", vat="SE999999000601")
        cls.env.user.company_ids |= cls.company
        cls.vendor = cls.env["res.partner"].create({
            "name": "Example Hardware AB", "is_company": True, "supplier_rank": 1,
            "vat": "SE999999002201", "country_id": cls.env.ref("base.se").id})

    def _receipt(self, text, lines, ref=False, **ai):
        move = self.env["account.move"].with_company(self.company).create(
            {"move_type": "in_invoice", "ref": ref})
        answer = {"vendor_name": self.vendor.name, "invoice_number": None, "currency": "SEK",
                  "lines": [dict(line) for line in lines], **ai}
        return self._run_ocr(move, text=text, ai=answer)

    def test_order_number_is_the_reference_without_an_invoice_number(self):
        header = {"total_amount": 10490.0, "subtotal": 8392.0, "vat_amount": 2098.0}
        move = self._receipt(fx.SHOP_RECEIPT_TEXT, TABLET_LINES, **header)
        self.assertEqual(move.partner_id, self.vendor)
        self.assertEqual(move.ref, "12345678")
        self.assertIn("Reference: the document has no invoice number – used the Ordernummer "
                      "12345678 printed on it.", self._bodies(move))
        self.assertEqual(move.ocr_printed_total, 10490.0, "SUMMA is the printed total")
        # the AI read an invoice number: it is the reference, without the note
        move = self._receipt(fx.SHOP_RECEIPT_TEXT, TABLET_LINES, invoice_number="F-2026-1",
                             **header)
        self.assertEqual(move.ref, "F-2026-1")
        self.assertNotIn("Reference: the document has no invoice number", self._bodies(move))
        # a reference set by hand is kept
        move = self._receipt(fx.SHOP_RECEIPT_TEXT, TABLET_LINES, ref="by hand", **header)
        self.assertEqual(move.ref, "by hand")

    def test_booking_number_and_the_total_kept_for_the_posting_check(self):
        """"Total 95,00 SEK" is no label the regex reads, but the AI's total is printed on
        the document and kept for the posting check: the note says so, instead of "no
        printed amounts could be read" next to a stored printed total."""
        header = {"total_amount": 95.0, "subtotal": 89.62, "vat_amount": 5.38}
        move = self._receipt(fx.TRAIN_TICKET_TEXT, TRAIN_LINES, **header)
        self.assertEqual(move.ref, "WK000XYZ")
        self.assertIn("used the Bokningsnummer WK000XYZ", self._bodies(move))
        self.assertAlmostEqual(move.ocr_printed_total, 95.0)
        bodies = self._bodies(move)
        self.assertNotIn("No amounts printed on the document could be read", bodies)
        self.assertRegex(bodies, r"Its total [^ ]*95\.00[^ ]* is printed on the document and is "
                                 r"kept as the document's total for the posting check")
        self.assertNotIn("the lines do not match", bodies)
        # the lines do not add up: the warning's explanation says the same about the total
        doubled = [dict(TRAIN_LINES[0], amount=179.24)]
        move = self._receipt(fx.TRAIN_TICKET_TEXT, doubled, **header)
        bodies = self._bodies(move)
        self.assertIn("OCR: the lines do not match the amounts read from the bill", bodies)
        self.assertRegex(bodies, r"No amount could be read after a label on the document, so the "
                                 r"lines were compared with the AI's reading .* Its total "
                                 r"[^ ]*95\.00[^ ]* is printed on the document")
        self.assertAlmostEqual(move.ocr_printed_total, 95.0)
