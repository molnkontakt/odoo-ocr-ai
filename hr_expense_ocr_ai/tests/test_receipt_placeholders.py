"""Upload's placeholders count as empty, so the receipt fills category and description
(#34); a configured name prefix gets the description appended."""
from unittest import mock

from odoo.addons.hr_expense_ocr_ai.lib import receipt_ocr
from odoo.tests import TransactionCase, tagged

FIELDS = {"total": 112.0, "merchant": "Example Restaurant", "items": "Lunch",
          "category_code": "MEAL", "confidence": 0.9}


@tagged("post_install", "-at_install", "expense_ocr")
class TestReceiptPlaceholders(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("expense_ocr.enabled", "True")
        cls.employee = cls.env.user.employee_id or cls.env["hr.employee"].create(
            {"name": "Example Employee", "user_id": cls.env.user.id})
        Product = cls.env["product.product"]
        cls.meal = Product.create({"name": "Meals", "can_be_expensed": True,
                                   "default_code": "MEAL"})
        cls.travel = Product.create({"name": "Travel", "can_be_expensed": True,
                                     "default_code": "TRAVEL"})

    def _patched(self, fields=FIELDS):
        result = {"text": "Example Restaurant\nTotalt 112,00\n", "source": "ai", "notes": [],
                  "fields": dict(fields)}
        return mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=result)

    def _attachment(self, **vals):
        return self.env["ir.attachment"].create({
            "name": "receipt.jpg", "raw": b"not really a jpeg", "mimetype": "image/jpeg",
            "res_model": "hr.expense", "res_id": 0, **vals})

    def test_upload_placeholders_are_replaced(self):
        Expense = self.env["hr.expense"]
        with self._patched():
            ids = Expense.create_expense_from_attachments(attachment_ids=self._attachment().ids)
        expense = Expense.browse(ids)
        self.assertEqual(expense.product_id, self.meal, "EXP_GEN is a placeholder")
        self.assertEqual(expense.name, "Example Restaurant — Lunch")
        self.assertEqual(expense.total_amount_currency, 112.0)

    def test_untitled_name_and_first_product_without_exp_gen(self):
        """Without EXP_GEN, Upload takes the first expensable product: still a placeholder."""
        self.env.ref("hr_expense.product_product_no_cost").can_be_expensed = False
        first = self.env["product.product"].search([("can_be_expensed", "=", True)], limit=1)
        expense = self.env["hr.expense"].create({
            "name": self.env["hr.expense"]._get_untitled_expense_name("10/05/2026"),
            "employee_id": self.employee.id, "product_id": first.id})
        fields = dict(FIELDS, category_code="TRAVEL" if first == self.meal else "MEAL")
        with self._patched(fields):
            expense.message_main_attachment_id = self._attachment(res_id=expense.id)
        self.assertNotEqual(expense.product_id, first)
        self.assertEqual(expense.name, "Example Restaurant — Lunch")

    def test_values_set_by_hand_are_kept(self):
        expense = self.env["hr.expense"].create({
            "name": "Lunch with a customer", "employee_id": self.employee.id,
            "product_id": self.travel.id})
        with self._patched():
            expense.message_main_attachment_id = self._attachment(res_id=expense.id)
        self.assertEqual(expense.product_id, self.travel)
        self.assertEqual(expense.name, "Lunch with a customer")
        self.assertEqual(expense.total_amount_currency, 112.0)

    def test_configured_name_prefix_gets_the_description(self):
        def read(name):
            expense = self.env["hr.expense"].create(
                {"name": name, "employee_id": self.employee.id, "product_id": self.travel.id})
            with self._patched():
                expense.message_main_attachment_id = self._attachment(res_id=expense.id)
            return expense

        self.assertEqual(read("Unknown sender: receipt").name, "Unknown sender: receipt",
                         "no prefix is configured by default")
        self.env["ir.config_parameter"].sudo().set_param(
            "expense_ocr.placeholder_name_prefixes", "Other prefix, Unknown sender")
        self.assertEqual(read("Unknown sender: receipt").name,
                         "Unknown sender: receipt — Example Restaurant — Lunch")
