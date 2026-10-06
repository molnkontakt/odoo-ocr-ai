"""The OCR of an uploaded bill runs in a savepoint and reports its outcome (#17).

The upload only queues the bill (#9) and reports it as imported, so core posts no "error
while importing" message; the OCR cron reads it. A failure halfway through (after a
partner was auto-created, or an SQL error) rolls back only the OCR's own writes, leaves the
transaction usable and is noted on the bill. A date the ORM cannot read is left out with a
note instead of failing the whole fill (#16).
"""
from unittest import mock

from odoo.addons.account_invoice_ocr_ai.lib import invoice_ocr
from odoo.tests import tagged
from odoo.tools import mute_logger

from . import ocr_fixtures as fx
from .common import PDF, OcrBillCase, run_ocr_cron

AI_NEW_VENDOR = {
    "vendor_name": "Example Newcomer AB", "invoice_number": "4711",
    "org_number": "999999-0048", "total_amount": 1250.0, "subtotal": 1000.0,
    "vat_amount": 250.0,
    "lines": [{"description": "Consulting", "amount": 1000.0, "vat_rate": 25,
               "account_code": "6540"}],
}
TEXT_NEW_VENDOR = fx.PLAIN_INVOICE_TEXT.replace(fx.PLAIN_VENDOR_NAME, "Example Newcomer AB").replace(
    fx.PLAIN_VENDOR_ORG, "999999-0048")


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrSavepoint(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # One attempt: a failure is final at once (the retries are tested in test_ocr_queue).
        cls.env["ir.config_parameter"].sudo().set_param("invoice_ocr.max_attempts", "1")

    def _upload_and_read(self, move, text=TEXT_NEW_VENDOR, ai=AI_NEW_VENDOR):
        """The upload queues the bill, the cron reads it. Returns what the upload hook told
        core."""
        imported = self._upload(move)
        self.assertEqual(move.ocr_state, "pending")
        with self._patch_ocr(text, ai), \
                mute_logger("odoo.addons.account_invoice_ocr_ai.models.ocr_queue"):
            run_ocr_cron(self.env)
        return imported

    def _partners(self):
        return self.env["res.partner"].search_count([("name", "=", "Example Newcomer AB")])

    def test_queued_bill_reports_imported_and_is_filled(self):
        move = self._new_bill()
        self.assertTrue(self._upload_and_read(move))
        self.assertEqual(move.ocr_state, "done")
        self.assertEqual(move.partner_id.name, "Example Newcomer AB")
        self.assertEqual(move.ref, "4711")
        self.assertNotIn("OCR could not read this document", self._bodies(move))

    def test_failure_rolls_back_partial_writes_and_is_noted(self):
        move = self._new_bill()
        Move = type(self.env["account.move"])
        with mock.patch.object(Move, "_create_lines_from_ocr", side_effect=ValueError("boom")):
            self._upload_and_read(move)
        # the partner auto-created before the failure is gone, the header was not written
        self.assertEqual(self._partners(), 0)
        self.assertFalse(move.partner_id)
        self.assertFalse(move.ref)
        self.assertEqual((move.ocr_state, move.ocr_error), ("failed", "boom"))
        self.assertIn("OCR could not read this document (attempts: 1): boom", self._bodies(move))

    def test_sql_error_leaves_the_transaction_usable(self):
        move = self._new_bill()
        Move = type(self.env["account.move"])

        def bad_sql(*args, **kwargs):
            self.env.cr.execute("SELECT 1 / 0")

        with mock.patch.object(Move, "_create_lines_from_ocr", side_effect=bad_sql), \
                mute_logger("odoo.sql_db"):
            self._upload_and_read(move)
        # the caller can go on: the savepoint was rolled back, not the transaction
        self.assertEqual(self._partners(), 0)
        move.ref = "after the failure"
        self.env.flush_all()
        self.assertEqual(move.ocr_state, "failed")
        self.assertIn("OCR could not read this document", self._bodies(move))

    def test_nothing_found_is_a_failure_with_a_note(self):
        move = self._new_bill()
        self._upload_and_read(move, text="unreadable", ai={})
        self.assertEqual(move.ocr_state, "failed")
        self.assertIn("neither a vendor name nor an invoice number", self._bodies(move))

    def test_disabled_is_not_queued(self):
        self.env["ir.config_parameter"].sudo().set_param("invoice_ocr.enabled", "False")
        move = self._new_bill()
        self.assertFalse(self._upload(move), "not queued: core's own message stays")
        self.assertFalse(move.ocr_state)
        self.assertNotIn("OCR could not", self._bodies(move))
        result = self.env["account.move"]._invoice_ocr_extend(move, PDF)
        self.assertEqual(result["status"], "skipped")

    def test_invalid_dates_are_not_written_and_the_rest_is(self):
        """A date the ORM cannot read no longer loses the whole fill (#16)."""
        move = self._new_bill()
        data = dict(AI_NEW_VENDOR, invoice_date="2026-02-30", due_date="15/09/26",
                    raw_text=TEXT_NEW_VENDOR)
        with mock.patch.object(invoice_ocr, "extract_invoice_data", return_value=data):
            result = self.env["account.move"]._invoice_ocr_extend_safe(move, PDF)
        self.assertEqual(result["status"], "filled")
        self.assertEqual(move.ref, "4711")
        self.assertFalse(move.invoice_date)
        bodies = self._bodies(move)
        self.assertIn("invoice_date 2026-02-30 is not a valid date", bodies)
        self.assertIn("due_date 15/09/26 is not a valid date", bodies)

    def test_english_month_name_end_to_end(self):
        move = self._new_bill()
        text = TEXT_NEW_VENDOR.replace("Fakturadatum: 2026-06-01", "Invoice date: 3 March 2026")
        ai = dict(AI_NEW_VENDOR, invoice_date="2026-03-03")
        with self._patch_ocr(text, ai):
            self.env["account.move"]._invoice_ocr_extend(move, PDF)
        self.assertEqual(str(move.invoice_date), "2026-03-03")
