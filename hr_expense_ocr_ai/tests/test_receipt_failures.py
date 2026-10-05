"""Receipt OCR failures: a readable UserError on the button (#36.1), and a savepoint in the
OCR cron so a failure rolls back only that receipt's read and never stops the queue
(#36.13)."""
from unittest import mock

from odoo.addons.account_invoice_ocr_ai.tests.common import run_ocr_cron
from odoo.addons.hr_expense_ocr_ai.lib import receipt_ocr
from odoo.exceptions import UserError
from odoo.tests import TransactionCase, tagged
from odoo.tools import mute_logger

QUEUE_LOGGER = "odoo.addons.account_invoice_ocr_ai.models.ocr_queue"


@tagged("post_install", "-at_install", "expense_ocr")
class TestReceiptFailures(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env["ir.config_parameter"].sudo()
        ICP.set_param("expense_ocr.enabled", "False")
        ICP.set_param("invoice_ocr.max_attempts", "1")  # a failure is final at once
        cls.employee = cls.env["hr.employee"].create({"name": "Example Employee"})

    def _expense(self):
        expense = self.env["hr.expense"].create({"name": "x", "employee_id": self.employee.id})
        self.env["ir.attachment"].create({
            "name": "receipt.jpg", "res_model": "hr.expense", "res_id": expense.id,
            "raw": b"not really a jpeg", "mimetype": "image/jpeg",
        })
        return expense

    def _bodies(self, expense):
        return " ".join(str(m.body) for m in expense.message_ids)

    def test_unreadable_image_is_a_readable_user_error(self):
        expense = self._expense()
        err = receipt_ocr.ReceiptReadError(
            "the text of receipt.jpg could not be read: the file is not an image that can be read")
        with mock.patch.object(receipt_ocr, "extract_text", side_effect=OSError("truncated")), \
                self.assertRaises(UserError) as cm:
            expense.action_read_receipt()
        self.assertIn("Receipt OCR failed for receipt.jpg", str(cm.exception))
        self.assertIn("could not be read", str(cm.exception))
        with mock.patch.object(receipt_ocr, "extract_receipt_data", side_effect=err), \
                self.assertRaises(UserError) as cm:
            expense.action_read_receipt()
        self.assertIn("not an image that can be read", str(cm.exception))

    def test_bad_value_on_write_is_a_user_error(self):
        expense = self._expense()
        HrExpense = type(self.env["hr.expense"])
        result = {"text": "x", "source": "ai", "notes": [], "fields": {}}
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=result), \
                mock.patch.object(HrExpense, "_expense_ocr_apply", side_effect=ValueError("bad value")), \
                self.assertRaises(UserError) as cm:
            expense.action_read_receipt()
        self.assertIn("Receipt OCR failed for receipt.jpg: bad value", str(cm.exception))

    def test_invalid_date_is_not_written_and_the_rest_is(self):
        """A date the ORM cannot read no longer loses the whole read (#30)."""
        expense = self._expense()
        today = expense.date
        result = {"text": "x", "source": "ai", "notes": [],
                  "fields": {"date": "2026-02-30", "total": 418.0}}
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=result):
            expense.action_read_receipt()
        self.assertEqual(expense.total_amount_currency, 418.0)
        self.assertEqual(expense.date, today)
        self.assertIn("2026-02-30 is not a valid date", self._bodies(expense))

    def test_background_read_rolls_back_and_notes_the_failure(self):
        expense = self._expense()
        expense._ocr_enqueue()
        HrExpense = type(self.env["hr.expense"])

        def apply_then_fail(rec, result, by_code, att, force=False):
            rec.write({"name": "written before the failure"})
            raise ValueError("boom")

        result = {"text": "x", "source": "ai", "notes": [], "fields": {}}
        with mock.patch.object(receipt_ocr, "extract_receipt_data", return_value=result), \
                mock.patch.object(HrExpense, "_expense_ocr_apply", apply_then_fail), \
                mute_logger(QUEUE_LOGGER):
            run_ocr_cron(self.env)  # does not raise
        self.assertEqual(expense.name, "x", "the partial write was rolled back")
        self.assertEqual(expense.ocr_state, "failed")
        self.assertIn("Receipt OCR failed for receipt.jpg: boom", expense.ocr_error)
        self.assertIn("Receipt OCR failed for receipt.jpg: boom", self._bodies(expense))

    def test_sql_error_leaves_the_transaction_usable(self):
        expense = self._expense()
        expense._ocr_enqueue()

        def bad_sql(*args, **kwargs):
            self.env.cr.execute("SELECT 1 / 0")

        with mock.patch.object(receipt_ocr, "extract_receipt_data", side_effect=bad_sql), \
                mute_logger("odoo.sql_db", QUEUE_LOGGER):
            run_ocr_cron(self.env)
        expense.name = "still usable"
        self.env.flush_all()
        self.assertEqual(expense.ocr_state, "failed")
        self.assertIn("OCR could not read this document", self._bodies(expense))


@tagged("post_install", "-at_install", "expense_ocr")
class TestReceiptBulk(TransactionCase):
    """The list action queues the receipts and says so; the outcome shows in the OCR state
    and the chatter, not in an ir.logging row a rollback discarded (#36.4, #29)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        ICP = cls.env["ir.config_parameter"].sudo()
        ICP.set_param("expense_ocr.enabled", "False")
        ICP.set_param("invoice_ocr.max_attempts", "1")
        cls.employee = cls.env["hr.employee"].create({"name": "Example Employee"})

    def _expense(self, name, attach=True):
        expense = self.env["hr.expense"].create({"name": name, "employee_id": self.employee.id})
        if attach:
            self.env["ir.attachment"].create({
                "name": f"{name}.pdf", "res_model": "hr.expense", "res_id": expense.id,
                "raw": b"%PDF-1.4 test", "mimetype": "application/pdf",
            })
        return expense

    def _read(self, raw, mimetype=None, filename=None, categories=None, config=None):
        if filename == "bad.pdf":
            raise ValueError("boom")
        return {"text": "Totalt 418,00", "source": "ai", "notes": [],
                "fields": {"total": 418.0, "merchant": "Example Store"}}

    def test_list_action_queues_and_the_cron_reads(self):
        good, bad, none = self._expense("good"), self._expense("bad"), self._expense("none", attach=False)
        server_action = self.env.ref("hr_expense_ocr_ai.action_read_receipt_server")
        records = good | bad | none
        with mock.patch.object(receipt_ocr, "extract_receipt_data", side_effect=self._read) as read:
            action = server_action.with_context(
                active_model="hr.expense", active_ids=records.ids, active_id=good.id).run()
        read.assert_not_called()
        self.assertEqual(action["tag"], "display_notification")
        params = action["params"]
        self.assertEqual(params["type"], "success")
        message = params["message"]
        self.assertTrue(message.startswith("2 queued for OCR"), message)
        self.assertIn(f"{none.display_name}: Ingen bild- eller PDF-bilaga", message)
        self.assertEqual((good | bad).mapped("ocr_state"), ["pending", "pending"])
        with mock.patch.object(receipt_ocr, "extract_receipt_data", side_effect=self._read), \
                mute_logger(QUEUE_LOGGER):
            run_ocr_cron(self.env)
        self.assertEqual(good.ocr_state, "done")
        self.assertEqual(good.total_amount_currency, 418.0)
        self.assertEqual(bad.ocr_state, "failed")
        self.assertIn("Receipt OCR failed for bad.pdf: boom",
                      " ".join(str(m.body) for m in bad.message_ids))
        self.assertFalse(self.env["ir.logging"].search_count(
            [("path", "=", "action_read_receipt_server")]))
