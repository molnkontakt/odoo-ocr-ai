"""A note when the receipt's printed VAT and the category's tax differ (#33). Note only:
the tax is never changed."""
from unittest import mock

from odoo.addons.hr_expense_ocr_ai.lib import receipt_ocr
from odoo.tests import TransactionCase, tagged

RECEIPT = "Example Restaurant\nTotalt 112,00\nMoms 12% 12,00\n"


@tagged("post_install", "-at_install", "expense_ocr")
class TestReceiptVat(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("expense_ocr.enabled", "False")
        cls.employee = cls.env["hr.employee"].create({"name": "Example Employee"})
        company = cls.env.company
        Tax = cls.env["account.tax"]
        base = Tax.search([*Tax._check_company_domain(company), ("type_tax_use", "=", "purchase"),
                           ("amount_type", "=", "percent")], limit=1)
        cls.tax25 = base.copy({"name": "Example 25 %", "amount": 25})
        cls.tax12 = base.copy({"name": "Example 12 %", "amount": 12})
        Product = cls.env["product.product"]
        cls.meals25 = Product.create({"name": "Meals (25 %)", "can_be_expensed": True,
                                      "default_code": "MEAL25",
                                      "supplier_taxes_id": [(6, 0, cls.tax25.ids)]})
        cls.meals12 = Product.create({"name": "Meals (12 %)", "can_be_expensed": True,
                                      "default_code": "MEAL12",
                                      "supplier_taxes_id": [(6, 0, cls.tax12.ids)]})
        cls.untaxed = Product.create({"name": "Fees", "can_be_expensed": True,
                                      "default_code": "FEES", "supplier_taxes_id": [(5, 0, 0)]})

    def _read(self, fields, text=RECEIPT, **expense_vals):
        expense = self.env["hr.expense"].create(
            {"name": "x", "employee_id": self.employee.id, **expense_vals})
        self.env["ir.attachment"].create({
            "name": "receipt.jpg", "res_model": "hr.expense", "res_id": expense.id,
            "raw": b"not really a jpeg", "mimetype": "image/jpeg",
        })
        result = {"text": text, "source": "ai", "notes": [], "fields": fields}
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=result):
            expense.action_read_receipt()
        return expense

    def _bodies(self, expense):
        return " ".join(str(m.body) for m in expense.message_ids)

    def test_category_tax_differs_from_the_receipt(self):
        expense = self._read({"total": 112.0, "vat_amount": 12.0, "category_code": "MEAL25"})
        self.assertEqual(expense.tax_ids, self.tax25, "the tax is not changed")
        self.assertAlmostEqual(expense.tax_amount_currency, 22.40)
        self.assertIn("the receipt shows VAT 12.00, the category's tax gives 22.40",
                      self._bodies(expense))
        self.assertFalse(expense.activity_ids)

    def test_also_for_a_category_set_by_hand(self):
        expense = self._read({"total": 112.0, "vat_amount": 12.0}, product_id=self.meals25.id)
        self.assertIn("the receipt shows VAT 12.00", self._bodies(expense))

    def test_no_note_when_it_matches_or_cannot_be_compared(self):
        expense = self._read({"total": 112.0, "vat_amount": 12.0, "category_code": "MEAL12"})
        self.assertNotIn("the receipt shows VAT", self._bodies(expense))
        expense = self._read({"total": 112.0, "vat_amount": 12.0, "category_code": "FEES"})
        self.assertNotIn("the receipt shows VAT", self._bodies(expense), "no tax")
        expense = self._read({"total": 112.0, "vat_amount": 13.0, "category_code": "MEAL25"})
        self.assertNotIn("the receipt shows VAT", self._bodies(expense), "VAT not printed")
        expense = self._read({"vat_amount": 12.0, "category_code": "MEAL25"})
        self.assertNotIn("the receipt shows VAT", self._bodies(expense), "no total to compare")
