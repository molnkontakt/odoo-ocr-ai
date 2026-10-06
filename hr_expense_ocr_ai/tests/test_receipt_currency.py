"""The receipt's currency is used or the amount is left empty, never a foreign amount in
the company's currency (#28)."""
from unittest import mock

from odoo.addons.hr_expense_ocr_ai.lib import receipt_ocr
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "expense_ocr")
class TestReceiptCurrency(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("expense_ocr.enabled", "False")
        cls.employee = cls.env["hr.employee"].create({"name": "Example Employee"})
        cls.company = cls.env.company
        # a currency with no decimals, so the rounding shows
        cls.jpy = cls._currency("JPY", rate_date="2026-01-01", rate=15.0)

    @classmethod
    def _currency(cls, name, active=True, rate_date=None, rate=0.1):
        currency = cls.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", name)], limit=1)
        currency.active = active
        cls.env["res.currency.rate"].search([("currency_id", "=", currency.id)]).unlink()
        if rate_date:
            cls.env["res.currency.rate"].create({
                "currency_id": currency.id, "name": rate_date, "rate": rate,
                "company_id": cls.company.id})
        return currency

    def _read(self, fields, **expense_vals):
        expense = self.env["hr.expense"].create(
            {"name": "x", "employee_id": self.employee.id, **expense_vals})
        self.env["ir.attachment"].create({
            "name": "receipt.jpg", "res_model": "hr.expense", "res_id": expense.id,
            "raw": b"not really a jpeg", "mimetype": "image/jpeg",
        })
        result = {"text": "x", "source": "ai", "notes": [], "fields": fields}
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=result):
            expense.action_read_receipt()
        return expense

    def _bodies(self, expense):
        return " ".join(str(m.body) for m in expense.message_ids)

    def test_foreign_currency_is_set_with_the_amount(self):
        expense = self._read({"total": 1234.4, "currency": "JPY", "date": "2026-09-17"})
        self.assertEqual(expense.currency_id, self.jpy)
        self.assertEqual(expense.total_amount_currency, 1234.0, "rounded like JPY")
        self.assertAlmostEqual(expense.total_amount,
                               self.company.currency_id.round(1234.0 / 15.0))

    def test_company_currency_and_no_currency(self):
        own = self.company.currency_id.name
        expense = self._read({"total": 418.0, "currency": own})
        self.assertEqual(expense.total_amount_currency, 418.0)
        self.assertEqual(expense.currency_id, self.company.currency_id)
        expense = self._read({"total": 418.0, "currency": "kr"})
        self.assertEqual(expense.total_amount_currency, 418.0)

    def test_inactive_currency_leaves_the_amount_empty(self):
        name = "NOK" if self.company.currency_id.name != "NOK" else "DKK"
        self._currency(name, active=False, rate_date="2026-01-01")
        expense = self._read({"total": 12.5, "currency": name, "date": "2026-09-17"})
        self.assertFalse(expense.total_amount_currency)
        self.assertEqual(expense.currency_id, self.company.currency_id)
        self.assertIn(f"{name} is not active", self._bodies(expense))
        self.assertIn("the amount was not filled", self._bodies(expense))

    def test_currency_without_a_rate_leaves_the_amount_empty(self):
        name = "CHF" if self.company.currency_id.name != "CHF" else "PLN"
        self._currency(name)
        expense = self._read({"total": 12.5, "currency": name, "date": "2026-09-17"})
        self.assertFalse(expense.total_amount_currency)
        self.assertIn(f"{name} has no exchange rate", self._bodies(expense))

    def test_fixed_cost_category_is_never_chosen(self):
        """A category with a fixed cost (mileage) is not offered to the model, and not taken
        from its answer: fuel put on it became an expense of 1.00 (#39)."""
        mileage = self.env["product.product"].create({
            "name": "Mileage", "can_be_expensed": True, "standard_price": 2.5,
            "default_code": "MILE", "supplier_taxes_id": [(5, 0, 0)]})
        expense = self._read({"total": 1234.4, "currency": "JPY", "category_code": "MILE"})
        self.assertNotEqual(expense.product_id, mileage)
        self.assertEqual(expense.total_amount_currency, 1234.0)
        cats, by_code = expense._expense_ocr_categories()
        self.assertNotIn("MILE", by_code)
        self.assertNotIn("MILE", [code for code, _name, _hint in cats])

    def test_fixed_cost_category_set_by_hand_is_left_alone(self):
        mileage = self.env["product.product"].create({
            "name": "Mileage", "can_be_expensed": True, "standard_price": 2.5,
            "default_code": "MILE", "supplier_taxes_id": [(5, 0, 0)]})
        expense = self._read({"total": 1234.4, "currency": "JPY"}, product_id=mileage.id)
        self.assertEqual(expense.product_id, mileage)
        self.assertEqual(expense.currency_id, self.company.currency_id)
        self.assertEqual(expense.total_amount_currency, 2.5, "quantity × cost")
        result = {"text": "x", "source": "ai", "notes": [],
                  "fields": {"total": 1234.4, "currency": "JPY"}}
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=result):
            expense.action_read_receipt(force=True)
        self.assertEqual(expense.total_amount_currency, 2.5)
        self.assertIn("has a fixed cost", self._bodies(expense))
