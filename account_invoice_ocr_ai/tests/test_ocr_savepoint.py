"""The OCR step on upload runs in a savepoint and reports its outcome (#17).

A failure halfway through (after a partner was auto-created, or an SQL error) rolls back
only the OCR's own writes, leaves the transaction usable and is noted on the bill. The
upload hook only reports "imported" to core when OCR actually filled the bill.
"""
from unittest import mock

from odoo.tests import tagged
from odoo.tools import mute_logger

from . import ocr_fixtures as fx
from .common import PDF, OcrBillCase

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
    def _upload(self, move, text=TEXT_NEW_VENDOR, ai=AI_NEW_VENDOR):
        """The upload path: core found no decoder for the plain PDF (returns None)."""
        Move = type(self.env["account.move"])
        base = next(c for c in Move.__mro__ if c.__dict__.get("_extend_with_attachments")
                    and "account_invoice_ocr_ai" not in c.__module__)
        with mock.patch.object(base, "_extend_with_attachments", return_value=None), \
                self._patch_ocr(text, ai):
            return move._extend_with_attachments(PDF, new=True)

    def _partners(self):
        return self.env["res.partner"].search_count([("name", "=", "Example Newcomer AB")])

    def test_filled_bill_reports_imported(self):
        move = self._new_bill()
        self.assertTrue(self._upload(move))
        self.assertEqual(move.partner_id.name, "Example Newcomer AB")
        self.assertEqual(move.ref, "4711")
        self.assertNotIn("OCR could not fill in this bill", self._bodies(move))

    def test_failure_rolls_back_partial_writes_and_is_noted(self):
        move = self._new_bill()
        Move = type(self.env["account.move"])
        with mock.patch.object(Move, "_create_lines_from_ocr", side_effect=ValueError("boom")):
            res = self._upload(move)
        self.assertFalse(res, "nothing was filled: core may say the import failed")
        # the partner auto-created before the failure is gone, the header was not written
        self.assertEqual(self._partners(), 0)
        self.assertFalse(move.partner_id)
        self.assertFalse(move.ref)
        self.assertIn("OCR could not fill in this bill: boom", self._bodies(move))

    def test_sql_error_leaves_the_transaction_usable(self):
        move = self._new_bill()
        Move = type(self.env["account.move"])

        def bad_sql(*args, **kwargs):
            self.env.cr.execute("SELECT 1 / 0")

        with mock.patch.object(Move, "_create_lines_from_ocr", side_effect=bad_sql), \
                mute_logger("odoo.sql_db"):
            self.assertFalse(self._upload(move))
        # the caller can go on: the savepoint was rolled back, not the transaction
        self.assertEqual(self._partners(), 0)
        move.ref = "after the failure"
        self.env.flush_all()
        self.assertIn("OCR could not fill in this bill", self._bodies(move))

    def test_nothing_found_is_a_failure_with_a_note(self):
        move = self._new_bill()
        self.assertFalse(self._upload(move, text="unreadable", ai={}))
        self.assertIn("neither a vendor name nor an invoice number", self._bodies(move))

    def test_disabled_is_skipped_silently(self):
        self.env["ir.config_parameter"].sudo().set_param("invoice_ocr.enabled", "False")
        move = self._new_bill()
        self.assertFalse(self._upload(move))
        self.assertNotIn("OCR could not fill in this bill", self._bodies(move))
        result = self.env["account.move"]._invoice_ocr_extend(move, PDF)
        self.assertEqual(result["status"], "skipped")
