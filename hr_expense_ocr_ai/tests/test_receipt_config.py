"""Receipt OCR gets the invoice OCR's settings as a per-run config, never via module globals."""
from unittest import mock

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
        self.assertEqual(cfg["own_company"], (expense.company_id.name or "").strip().lower())
        self.assertEqual(_globals(), before)
