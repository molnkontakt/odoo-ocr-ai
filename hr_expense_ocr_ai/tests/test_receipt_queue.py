"""Receipts are read in the background (#29, #36.3).

A new main attachment — set by a write or already at create — only queues the expense; the
OCR cron reads it shortly after. The form button still reads at once. The queue itself
(budget, retries, changed documents) is tested in account_invoice_ocr_ai.
"""
from unittest import mock

from odoo.addons.account_invoice_ocr_ai.tests.common import make_due, run_ocr_cron
from odoo.addons.hr_expense_ocr_ai.lib import receipt_ocr
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged

TEXT = "Example Restaurant\nTotalt 112,00\n"
FIELDS = {"total": 112.0, "merchant": "Example Restaurant", "items": "Lunch", "confidence": 0.9}


def _result(**extra):
    return {"text": TEXT, "source": "ai", "notes": [], "fields": dict(FIELDS), **extra}


@tagged("post_install", "-at_install", "expense_ocr")
class TestReceiptQueue(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["ir.config_parameter"].sudo().set_param("expense_ocr.enabled", "True")
        cls.employee = cls.env["hr.employee"].create({"name": "Example Employee"})
        cls.cron = cls.env.ref("account_invoice_ocr_ai.ir_cron_ocr_queue")

    def _attachment(self, res_id=0):
        return self.env["ir.attachment"].create({
            "name": "receipt.jpg", "raw": b"not really a jpeg", "mimetype": "image/jpeg",
            "res_model": "hr.expense", "res_id": res_id})

    def _expense(self, **vals):
        return self.env["hr.expense"].create({"name": "x", "employee_id": self.employee.id, **vals})

    def _triggers(self):
        return self.env["ir.cron.trigger"].search_count([("cron_id", "=", self.cron.id)])

    def _bodies(self, expense):
        return " ".join(str(m.body) for m in expense.message_ids)

    def test_new_main_attachment_queues_without_reading(self):
        expense = self._expense()
        att = self._attachment(expense.id)
        before = self._triggers()
        with mock.patch.object(receipt_ocr, "extract_receipt_data") as read:
            expense.message_main_attachment_id = att
        read.assert_not_called()
        self.assertEqual(expense.ocr_state, "pending")
        self.assertEqual(expense.ocr_attachment_id, att)
        self.assertEqual(self._triggers(), before + 1)
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=_result()):
            run_ocr_cron(self.env)
        self.assertEqual(expense.ocr_state, "done")
        self.assertEqual(expense.total_amount_currency, 112.0)

    def test_attachment_set_at_create_is_queued(self):
        """An API client that passes the receipt to create() gets it read too (#36.3)."""
        att = self._attachment()
        with mock.patch.object(receipt_ocr, "extract_receipt_data") as read:
            expense = self._expense(message_main_attachment_id=att.id)
        read.assert_not_called()
        self.assertEqual(expense.ocr_state, "pending")
        att.res_id = expense.id
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=_result()):
            run_ocr_cron(self.env)
        self.assertEqual(expense.ocr_state, "done")
        self.assertEqual(expense.total_amount_currency, 112.0)
        self.assertEqual(expense.name, "Example Restaurant — Lunch")

    def test_nothing_to_fill_or_switched_off_is_not_queued(self):
        product = self.env["product.product"].create({"name": "Meals", "can_be_expensed": True})
        filled = self._expense(product_id=product.id, total_amount_currency=50.0)
        filled.message_main_attachment_id = self._attachment(filled.id)
        self.assertFalse(filled.ocr_state, "amount and category are set: nothing to read")
        self.env["ir.config_parameter"].sudo().set_param("expense_ocr.enabled", "False")
        expense = self._expense()
        expense.message_main_attachment_id = self._attachment(expense.id)
        self.assertFalse(expense.ocr_state)

    def test_ai_failure_is_retried_then_the_text_is_used(self):
        self.env["ir.config_parameter"].sudo().set_param("invoice_ocr.max_attempts", "2")
        expense = self._expense()
        expense.message_main_attachment_id = self._attachment(expense.id)
        regex_only = {"text": TEXT, "source": "regex", "fields": {"total": 112.0},
                      "notes": ["AI-tolkningen misslyckades; bara regex (RuntimeError: HTTP 503)"],
                      "ai_error": "RuntimeError: HTTP 503"}
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=regex_only):
            run_ocr_cron(self.env)
            self.assertEqual((expense.ocr_state, expense.ocr_attempts), ("pending", 1))
            self.assertFalse(expense.total_amount_currency, "nothing is written before the last try")
            make_due(expense)
            run_ocr_cron(self.env)
        self.assertEqual(expense.ocr_state, "failed")
        self.assertEqual(expense.total_amount_currency, 112.0)
        self.assertIn("the AI step failed (RuntimeError: HTTP 503)", expense.ocr_error)
        bodies = self._bodies(expense)
        self.assertIn("AI-tolkningen misslyckades", bodies)
        self.assertNotIn("OCR could not read this document", bodies, "the read note says it")

    def test_expenses_no_longer_drafts_are_cleared(self):
        expense = self._expense(total_amount_currency=10.0)  # only a draft may be 0
        expense.message_main_attachment_id = self._attachment(expense.id)
        self.assertEqual(expense.ocr_state, "pending", "no category yet: queued")
        expense.approval_state = "submitted"
        self.assertEqual(expense.state, "submitted")
        with mock.patch.object(receipt_ocr, "extract_receipt_data") as read:
            run_ocr_cron(self.env)
        read.assert_not_called()
        self.assertFalse(expense.ocr_state)

    def test_form_button_reads_at_once(self):
        expense = self._expense()
        expense.message_main_attachment_id = self._attachment(expense.id)
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=_result()):
            expense.action_read_receipt()
        self.assertEqual((expense.ocr_state, expense.total_amount_currency), ("done", 112.0))
        with mock.patch.object(receipt_ocr, "extract_receipt_data") as read:
            run_ocr_cron(self.env)
        read.assert_not_called()

    def test_form_button_does_not_read_a_receipt_being_read(self):
        expense = self._expense()
        expense.message_main_attachment_id = self._attachment(expense.id)
        expense.ocr_state = "running"
        with mock.patch.object(receipt_ocr, "extract_receipt_data") as read, \
                self.assertRaises(UserError):
            expense.action_read_receipt()
        read.assert_not_called()

    def test_budget_cut_is_noted_when_nothing_was_read(self):
        expense = self._expense()
        expense.message_main_attachment_id = self._attachment(expense.id)
        nothing = {"text": "", "source": "none", "fields": {},
                   "notes": ["the time for reading the image was used up – it was not read"]}
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=nothing):
            run_ocr_cron(self.env)
        self.assertEqual(expense.ocr_state, "failed")
        bodies = self._bodies(expense)
        self.assertIn("the time for reading the image was used up", bodies)
        self.assertNotIn("OCR could not read this document", bodies, "one note is enough")

    def test_receipt_with_everything_set_is_read_not_skipped(self):
        """The list action on an expense filled in by hand: the job reads the receipt, notes
        what it says and fills nothing; it is "Read", not "OCR did not read"."""
        product = self.env["product.product"].create({"name": "Meals", "can_be_expensed": True})
        expense = self._expense(product_id=product.id, total_amount_currency=50.0,
                                name="Lunch with a customer")
        self._attachment(expense.id)
        expense.action_read_receipt_bulk()
        self.assertEqual(expense.ocr_state, "pending")
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=_result()):
            run_ocr_cron(self.env)
        self.assertEqual(expense.ocr_state, "done")
        self.assertEqual((expense.total_amount_currency, expense.name), (50.0, "Lunch with a customer"))
        bodies = self._bodies(expense)
        self.assertIn("Kvitto-OCR", bodies)
        self.assertNotIn("OCR did not read this document", bodies)
