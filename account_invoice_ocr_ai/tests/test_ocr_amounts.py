"""Amounts against the document (#39): lines that carry the prices including VAT are booked
net, the lines are checked against the AI's reading when no printed amount was read, and a
bill whose total differs from the total printed on the document is not posted unless someone
with accounting rights confirms it. All parties and numbers are invented."""
from odoo import Command
from odoo.exceptions import AccessError, UserError
from odoo.tests import new_test_user, tagged

from .common import OcrBillCase

# A shop receipt: the prices include VAT, "SUMMA … kr", "Varav moms … kr"
RECEIPT_TEXT = """Example Hardware AB
ORDERNUMMER
12345678
ARTIKELNUMMER PRODUKT ANTAL Á-PRIS
Example mini computer
100200 1 12000 kr
Frakt 345 kr
SUMMA 12345 kr
Varav moms 2469 kr
"""
GROSS_LINES = [
    {"description": "Example mini computer", "amount": 12000.0, "vat_rate": 25,
     "account_code": "5410"},
    {"description": "Frakt", "amount": 345.0, "vat_rate": 25, "account_code": "5410"},
]
NET_LINES = [
    {"description": "Example mini computer", "amount": 9600.0, "vat_rate": 25,
     "account_code": "5410"},
    {"description": "Frakt", "amount": 276.0, "vat_rate": 25, "account_code": "5410"},
]


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrAmounts(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls._se_company("Example Buyer", vat="SE999999000601")
        cls.env.user.company_ids |= cls.company
        cls.vendor = cls.env["res.partner"].create({
            "name": "Example Hardware AB", "is_company": True, "supplier_rank": 1,
            "country_id": cls.env.ref("base.se").id})
        cls.accountant = new_test_user(
            cls.env, "ocr_accountant", groups="base.group_user,account.group_account_user",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.manager = new_test_user(
            cls.env, "ocr_accounting_admin",
            groups="base.group_user,account.group_account_manager",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])
        cls.billing = new_test_user(
            cls.env, "ocr_billing_only", groups="base.group_user,account.group_account_invoice",
            company_id=cls.company.id, company_ids=[Command.set(cls.company.ids)])

    def _receipt(self, lines, text=RECEIPT_TEXT, **header):
        move = self.env["account.move"].with_company(self.company).create(
            {"move_type": "in_invoice", "partner_id": self.vendor.id,
             "invoice_date": "2026-04-17"})
        ai = {"vendor_name": self.vendor.name, "invoice_number": "12345678",
              "total_amount": 12345.0, "subtotal": 9876.0, "vat_amount": 2469.0,
              "lines": [dict(line) for line in lines], **header}
        return self._run_ocr(move, text=text, ai=ai)

    def _bill(self, total_lines, printed=None, currency=None, move_type="in_invoice"):
        """A bill with one 25 % line of net `total_lines` / 1.25, and a printed total."""
        tax = self._tax(self.company, "purchase_tax_25_goods")
        account = self.env["account.move"]._ocr_account(self.company, "5410")
        vals = {"move_type": move_type, "partner_id": self.vendor.id, "invoice_date": "2026-04-17",
                "invoice_line_ids": [Command.create({
                    "name": "Example item", "quantity": 1, "price_unit": total_lines / 1.25,
                    "account_id": account.id, "tax_ids": [Command.set(tax.ids)]})]}
        if currency:
            vals["currency_id"] = currency.id
        move = self.env["account.move"].with_company(self.company).create(vals)
        if printed is not None:
            move.write({"ocr_printed_currency_id": (currency or self.company.currency_id).id,
                        "ocr_printed_total": printed})
        return move

    # -- lines that carry the prices including VAT ----------------------------------------

    def test_lines_including_vat_are_converted_to_net(self):
        move = self._receipt(GROSS_LINES)
        self.assertEqual(sorted(move.invoice_line_ids.mapped("price_subtotal")), [276.0, 9600.0])
        self.assertEqual(set(move.invoice_line_ids.mapped("quantity")), {1.0})
        self.assertAlmostEqual(move.amount_tax, 2469.0)
        self.assertAlmostEqual(move.amount_total, 12345.0)
        bodies = self._bodies(move)
        self.assertIn("prices including VAT", bodies)
        self.assertNotIn("the lines do not match", bodies)
        self.assertEqual(move.ocr_printed_total, 12345.0)
        move.action_post()
        self.assertEqual(move.state, "posted")

    def test_net_lines_with_a_consistent_header_are_kept(self):
        move = self._receipt(NET_LINES)
        self.assertEqual(sorted(move.invoice_line_ids.mapped("price_subtotal")), [276.0, 9600.0])
        self.assertAlmostEqual(move.amount_total, 12345.0)
        self.assertNotIn("prices including VAT", self._bodies(move))

    def test_inconsistent_header_no_conversion_a_warning_and_no_posting(self):
        """The AI's net is the total: the header does not add up, so it confirms nothing —
        the lines stay as they are, the warning names the pattern and posting is refused."""
        move = self._receipt(GROSS_LINES, subtotal=12345.0)
        self.assertAlmostEqual(move.amount_untaxed, 12345.0)
        self.assertAlmostEqual(move.amount_total, 15431.25)
        bodies = self._bodies(move)
        self.assertIn("OCR: the lines do not match the bill", bodies)
        self.assertIn("the line amounts look like prices including VAT", bodies)
        with self.assertRaisesRegex(UserError, r"15,431\.25.*12,345\.00"):
            move.action_post()
        with self.assertRaisesRegex(UserError, "prices including VAT"):
            move.action_post()

    # -- the totals check without printed amounts -------------------------------------------

    def test_lines_checked_against_the_ais_reading_without_printed_amounts(self):
        text = "Example Hardware AB\nOrder 12345678\n"
        move = self._receipt(NET_LINES[:1], text=text)
        self.assertAlmostEqual(move.amount_total, 12000.0)
        bodies = self._bodies(move)
        self.assertIn("OCR: the lines do not match the amounts read from the bill", bodies)
        self.assertRegex(bodies, r"total [^ ]*12,000\.00[^ ]* against the AI's reading [^ ]*12,345\.00")
        self.assertIn("No amounts printed on the document could be read", bodies)
        self.assertFalse(move.ocr_printed_currency_id, "the AI's total is not on the document")
        move = self._receipt(NET_LINES, text=text)
        bodies = self._bodies(move)
        self.assertNotIn("the lines do not match", bodies)
        self.assertIn("checked only against the AI's reading", bodies)

    # -- the posting check --------------------------------------------------------------

    def test_a_bill_differing_from_the_printed_total_is_not_posted(self):
        move = self._bill(1000.0, printed=1250.0)
        with self.assertRaisesRegex(UserError, r"1,000\.00.*1,250\.00"):
            move.action_post()
        self.assertEqual(move.state, "draft")
        self._bill(1250.0, printed=1250.0).action_post()
        self._bill(1250.4, printed=1250.0).action_post()  # öre rounding

    def test_no_printed_total_is_not_checked(self):
        move = self._bill(1000.0)
        move.action_post()
        self.assertEqual(move.state, "posted")

    def test_override_by_someone_with_accounting_rights(self):
        move = self._bill(1000.0, printed=1250.0)
        with self.assertRaises(AccessError):
            move.with_user(self.billing).write({"ocr_amounts_checked": True})
        # the accounting administrator, who in Odoo Community lacks the full accounting
        # features, may tick it too
        self._bill(1000.0, printed=1250.0).with_user(self.manager).write(
            {"ocr_amounts_checked": True})
        move.with_user(self.accountant).write({"ocr_amounts_checked": True})
        self.env.cr.precommit.run()  # the tracking values are written at commit
        move.action_post()
        self.assertEqual(move.state, "posted")
        self.assertTrue(move.ocr_amounts_checked)
        tracked = move.message_ids.tracking_value_ids.filtered(
            lambda t: t.field_id.name == "ocr_amounts_checked")
        self.assertTrue(tracked)

    def test_changing_the_lines_clears_the_override(self):
        move = self._bill(1000.0, printed=1250.0)
        move.with_user(self.accountant).write({"ocr_amounts_checked": True})
        move.write({"invoice_line_ids": [Command.update(move.invoice_line_ids.id,
                                                        {"price_unit": 900.0})]})
        self.assertFalse(move.ocr_amounts_checked)
        move.with_user(self.accountant).write({"ocr_amounts_checked": True})
        move.invoice_line_ids.write({"quantity": 2})  # a direct write on the line
        self.assertFalse(move.ocr_amounts_checked)
        with self.assertRaises(UserError):
            move.action_post()
        # ticked in the same save as a line change: the tick stands
        move.with_user(self.accountant).write({
            "ocr_amounts_checked": True,
            "invoice_line_ids": [Command.update(move.invoice_line_ids.id, {"quantity": 1})]})
        self.assertTrue(move.ocr_amounts_checked)
        move.action_post()

    def test_refund(self):
        move = self._bill(1000.0, printed=-1250.0, move_type="in_refund")
        with self.assertRaisesRegex(UserError, r"1,250\.00"):
            move.action_post()
        self._bill(1250.0, printed=-1250.0, move_type="in_refund").action_post()

    def test_foreign_currency_is_compared_in_the_documents_currency(self):
        eur = self.env.ref("base.EUR")
        eur.active = True
        self.env["res.currency.rate"].create({"currency_id": eur.id, "name": "2026-01-01",
                                              "rate": 0.1, "company_id": self.company.id})
        move = self._bill(125.0, printed=100.0, currency=eur)
        with self.assertRaisesRegex(UserError, r"125\.00.*100\.00"):
            move.action_post()
        posted = self._bill(100.0, printed=100.0, currency=eur)
        posted.action_post()
        self.assertAlmostEqual(abs(posted.amount_total_signed), 1000.0, msg="SEK 1 000")
        # the bill in another currency than the document's total
        move = self._bill(100.0, printed=100.0)
        move.ocr_printed_currency_id = eur
        with self.assertRaisesRegex(UserError, "is in SEK, but the total OCR read"):
            move.action_post()

    def test_bulk_posting_checks_every_bill(self):
        good = self._bill(1250.0, printed=1250.0)
        bad = self._bill(1000.0, printed=1250.0)
        with self.assertRaisesRegex(UserError, bad.display_name):
            (good | bad).action_post()
        self.assertEqual((good | bad).mapped("state"), ["draft", "draft"])
        wizard = self.env["validate.account.move"].with_context(
            active_model="account.move", active_ids=(good | bad).ids).create({"force_hash": True})
        with self.assertRaises(UserError):
            wizard.validate_move()

    def test_a_new_reading_replaces_the_printed_amounts(self):
        move = self._receipt(GROSS_LINES)
        move.with_user(self.accountant).write({"ocr_amounts_checked": True})
        self._run_ocr(move, text="Example Hardware AB\n", ai={"vendor_name": "x"})
        self.assertFalse(move.ocr_amounts_checked)
        self.assertFalse(move.ocr_printed_currency_id)
