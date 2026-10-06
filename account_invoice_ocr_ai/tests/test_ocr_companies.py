"""Two companies on the Swedish chart: every lookup uses the bill's company (#7).

The bills are filled while another company is the active one and both are ticked, as on
the upload path from another company's journal. With both ticked every company's taxes
are visible, so an unscoped search returns the first company's tax; an account searched
on code_store resolves through the active company.
"""
from odoo.tests import tagged

from . import ocr_fixtures as fx
from .common import PDF, OcrBillCase

PRINTED = """Faktura
Example Supplier AB
Org.nr: 999999-0022
Fakturanummer: 4711
Fakturadatum: 2026-06-01
Belopp exkl. moms 2 061,00
Moms 515,25
Att betala: 2 576,00
"""


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrCompanies(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_a = cls._se_company("Example Company A")
        cls.company_b = cls._se_company("Example Company B")
        cls.both = [cls.company_a.id, cls.company_b.id]  # A is the active company

    def _bill_in_b(self):
        return self.env["account.move"].with_company(self.company_b).create(
            {"move_type": "in_invoice"})

    def _run_as_a(self, move, text, ai):
        Move = self.env["account.move"].with_context(allowed_company_ids=self.both)
        with self._patch_ocr(text, ai):
            Move._invoice_ocr_extend(move.with_context(allowed_company_ids=self.both), PDF)
        return move

    def test_accounts_and_taxes_of_the_bills_company(self):
        ai = {"vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711",
              "lines": [{"description": "Consulting", "amount": 1000.0, "vat_rate": 25,
                         "account_code": "6540"}]}
        move = self._run_as_a(self._bill_in_b(), fx.PLAIN_INVOICE_TEXT, ai)
        line = move.invoice_line_ids
        self.assertEqual(len(line), 1)
        self.assertEqual(line.account_id.code, "6540")
        self.assertIn(self.company_b, line.account_id.company_ids)
        self.assertEqual(line.tax_ids.company_id, self.company_b)
        self.assertEqual(line.tax_ids.amount, 25)
        self.assertEqual(move.amount_total, 1250.0)

    def test_partner_of_another_company_is_not_used(self):
        other = self.env["res.partner"].create({
            "name": fx.PLAIN_VENDOR_NAME, "vat": "SE999999002201", "is_company": True,
            "supplier_rank": 1, "company_id": self.company_a.id})
        ai = {"vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711",
              "org_number": fx.PLAIN_VENDOR_ORG,
              "lines": [{"description": "Consulting", "amount": 1000.0, "vat_rate": 25,
                         "account_code": "6540"}]}
        move = self._run_as_a(self._bill_in_b(), fx.PLAIN_INVOICE_TEXT, ai)
        self.assertTrue(move.partner_id)
        self.assertNotEqual(move.partner_id, other)
        self.assertIn(move.partner_id.company_id.id, (False, self.company_b.id))

    def test_rounding_account_of_the_bills_company(self):
        """The 3740 rounding line is the bill company's account, also with A active."""
        ai = {"vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711",
              "subtotal": 2061.0, "vat_amount": 515.25, "total_amount": 2576.0,
              "lines": [{"description": "Services", "amount": 2061.0, "vat_rate": 25,
                         "account_code": "6540"}]}
        move = self._run_as_a(self._bill_in_b(), PRINTED, ai)
        rounding = move.invoice_line_ids.filtered(lambda line: line.account_id.code == "3740")
        self.assertEqual(len(rounding), 1)
        self.assertIn(self.company_b, rounding.account_id.company_ids)
        self.assertAlmostEqual(rounding.price_unit, -0.25)
        self.assertEqual(move.amount_total, 2576.0)

    def test_tax_helper_per_company(self):
        Move = self.env["account.move"].with_context(allowed_company_ids=self.both)
        for company in (self.company_a, self.company_b):
            tax = Move._ocr_tax(company, "purchase_goods_tax_25_EC")
            self.assertEqual(tax, self._tax(company, "purchase_goods_tax_25_EC"))
            self.assertEqual(tax.company_id, company)
            self.assertEqual(Move._ocr_account(company, "4515").company_ids & (
                self.company_a | self.company_b), company)
        self.assertFalse(Move._ocr_tax(self.company_b, "no_such_tax"))

    def test_tax_search_fallback_for_other_charts(self):
        """A chart without l10n_se's template ids: the domain search finds the company's tax."""
        other = self._accounting(self.env["res.company"].create(
            {"name": "Example Generic", "country_id": self.env.ref("base.us").id}))
        company = other["company"]
        Tax = self.env["account.tax"].with_company(company)
        mine = Tax.create({"name": "My 25 %", "amount": 25, "type_tax_use": "purchase",
                           "company_id": company.id, "country_id": company.account_fiscal_country_id.id})
        Move = self.env["account.move"].with_company(company)
        self.assertEqual(Move._ocr_tax(company, "purchase_tax_25_services"), mine)
        # reverse charge is never guessed from a rate
        self.assertFalse(Move._ocr_tax(company, "purchase_services_tax_25_EC"))
