"""Receipt OCR gets the invoice OCR's settings as a per-run config, never via module globals."""
from unittest import mock

from odoo import fields
from odoo.addons.account_invoice_ocr_ai.lib import invoice_ocr
from odoo.addons.hr_expense_ocr_ai.lib import receipt_ocr
from odoo.tests import TransactionCase, tagged


def _globals():
    return {k: (set(v) if isinstance(v, set) else v)
            for k, v in vars(invoice_ocr).items() if k.isupper()}


@tagged("post_install", "-at_install", "expense_ocr")
class TestReceiptConfig(TransactionCase):
    def test_read_receipt_passes_the_company_config(self):
        ICP = self.env["ir.config_parameter"].sudo()
        ICP.set_param("expense_ocr.enabled", "False")  # no automatic run on attach
        ICP.set_param("invoice_ocr.provider", "openai_compatible")
        ICP.set_param("invoice_ocr.base_url", "https://receipts.example/v1")
        ICP.set_param("invoice_ocr.api_key", "K-receipt")
        ICP.set_param("invoice_ocr.model", "m")
        employee = self.env["hr.employee"].create({"name": "Example Employee"})
        expense = self.env["hr.expense"].create({"name": "x", "employee_id": employee.id})
        self.env["ir.attachment"].create({
            "name": "receipt.pdf", "res_model": "hr.expense", "res_id": expense.id,
            "raw": b"%PDF-1.4 test", "mimetype": "application/pdf",
        })
        before = _globals()
        result = {"text": "", "fields": {}, "source": "none", "notes": []}
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=result) as extract:
            expense.action_read_receipt()
        cfg = extract.call_args.kwargs["config"]
        self.assertEqual(cfg["provider"], "openai_compatible")
        self.assertEqual(cfg["api_key"], "K-receipt")
        self.assertIn(expense.company_id.name, cfg["own_names"])
        self.assertEqual(cfg["today"], fields.Date.context_today(expense),
                         "a receipt's date is checked against today in the user's time zone")
        self.assertEqual(_globals(), before)

    def test_disable_is_stored_and_respected(self):
        ICP = self.env["ir.config_parameter"].sudo()
        self.env["res.config.settings"].create({"expense_ocr_enabled": False}).set_values()
        self.assertEqual(ICP.get_param("expense_ocr.enabled"), "False")
        self.assertFalse(self.env["hr.expense"]._expense_ocr_enabled())
        self.assertFalse(self.env["res.config.settings"].create({}).expense_ocr_enabled)
        self.env["res.config.settings"].create({"expense_ocr_enabled": True}).set_values()
        self.assertTrue(self.env["hr.expense"]._expense_ocr_enabled())

    def test_category_hint_is_plain_text(self):
        """The category "Guideline" is HTML: the model gets its text, never markup (#36.5)."""
        Product = self.env["product.product"]
        Product.create({"name": "Machinery", "default_code": "OCRTEST_MACH", "can_be_expensed": True,
                        "description": "<p>Fuel, oil &amp; spare parts</p><p><br></p>"})
        Product.create({"name": "Empty guideline", "default_code": "OCRTEST_EMPTY",
                        "can_be_expensed": True, "description": "<p><br></p>"})
        Product.create({"name": "Purchase text", "default_code": "OCRTEST_PURCH", "can_be_expensed": True,
                        "description_purchase": "Tools\nand  ladders", "description": "<p>ignored</p>"})
        Product.create({"name": "Long", "default_code": "OCRTEST_LONG", "can_be_expensed": True,
                        "description": "<p>%s</p>" % ("word " * 100)})
        employee = self.env["hr.employee"].create({"name": "Example Employee"})
        expense = self.env["hr.expense"].create({"name": "x", "employee_id": employee.id})
        cats, by_code = expense._expense_ocr_categories()
        hints = {code: hint for code, _name, hint in cats}
        self.assertEqual(hints["OCRTEST_MACH"], "Fuel, oil & spare parts")
        self.assertEqual(hints["OCRTEST_EMPTY"], "")
        self.assertEqual(hints["OCRTEST_PURCH"], "Tools and ladders")
        self.assertLessEqual(len(hints["OCRTEST_LONG"]), 200)
        self.assertNotIn("<", "".join(hints.values()))
        self.assertEqual(by_code["OCRTEST_MACH"].name, "Machinery")
