"""The accounting date follows the invoice date unless Odoo's lock rules forbid it (#35):
the purchase lock date, a parent company's lock and the user's lock exceptions count."""
from datetime import date

from odoo.tests import tagged

from . import ocr_fixtures as fx
from .common import OcrBillCase

INVOICE_DATE = date(2026, 6, 1)  # Fakturadatum in fx.PLAIN_INVOICE_TEXT
AI = {"vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711",
      "lines": [{"description": "Consulting", "amount": 1000.0, "vat_rate": 25}]}


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrLockDates(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]

    def _run(self, company=None, text=fx.PLAIN_INVOICE_TEXT, ai=None):
        Move = self.env["account.move"].with_company(company or self.company)
        move = Move.create({"move_type": "in_invoice"})
        return self._run_ocr(move, text=text, ai=AI if ai is None else ai)

    def test_open_period_takes_the_invoice_date(self):
        move = self._run()
        self.assertEqual(move.invoice_date, INVOICE_DATE)
        self.assertEqual(move.date, INVOICE_DATE)

    def test_purchase_lock_date_is_respected(self):
        self.company.purchase_lock_date = date(2026, 6, 30)
        move = self._run()
        self.assertEqual(move.invoice_date, INVOICE_DATE)
        self.assertNotEqual(move.date, INVOICE_DATE)
        self.assertIn("is in a locked period", self._bodies(move))

    def test_parent_company_lock_is_respected(self):
        branch = self.env["res.company"].create(
            {"name": "Example Branch", "parent_id": self.company.id})
        self.company.fiscalyear_lock_date = date(2026, 6, 30)
        # Dates only: a branch made in a test has no payable account for new vendors
        move = self._run(branch, text="Faktura\nFakturadatum: 2026-06-01\n",
                         ai={"vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711"})
        self.assertEqual(move.company_id, branch)
        self.assertEqual(move.invoice_date, INVOICE_DATE)
        self.assertNotEqual(move.date, INVOICE_DATE)

    def test_lock_exception_lets_the_user_use_the_invoice_date(self):
        self.company.fiscalyear_lock_date = date(2026, 6, 30)
        self.env["account.lock_exception"].create({
            "company_id": self.company.id, "user_id": self.env.user.id,
            "fiscalyear_lock_date": False, "reason": "test"})
        move = self._run()
        self.assertEqual(move.date, INVOICE_DATE)
