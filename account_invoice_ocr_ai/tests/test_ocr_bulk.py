"""The form button and the list action say what happened (#35.1).

Every bill runs in its own savepoint; the result is a notification counting filled, failed
and skipped bills with the reasons, and a failed bill gets a chatter note. The list action
returns that notification instead of writing an ir.logging row that its own rollback
discarded.
"""
from unittest import mock

from odoo.tests import tagged

from . import ocr_fixtures as fx
from .common import OcrBillCase

AI = {
    "vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711",
    "total_amount": 1250.0, "subtotal": 1000.0, "vat_amount": 250.0,
    "lines": [{"description": "Consulting", "amount": 1000.0, "vat_rate": 25,
               "account_code": "6540"}],
}


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrBulk(OcrBillCase):
    def _with_pdf(self, move):
        self.env["ir.attachment"].create({
            "name": "invoice.pdf", "res_model": "account.move", "res_id": move.id,
            "raw": b"%PDF-1.4 test", "mimetype": "application/pdf",
        })
        return move

    def _bills(self):
        good = self._with_pdf(self._new_bill())
        bad = self._with_pdf(self._new_bill())
        no_pdf = self._new_bill()
        customer = self.env["account.move"].create({"move_type": "out_invoice"})
        return good, bad, no_pdf, customer

    def _fail_for(self, bad):
        Move = type(self.env["account.move"])
        orig = Move._create_lines_from_ocr

        def lines(rec, move, data):
            if move == bad:
                raise ValueError("boom")
            return orig(rec, move, data)

        return mock.patch.object(Move, "_create_lines_from_ocr", lines)

    def test_button_summarises_filled_failed_and_skipped(self):
        good, bad, no_pdf, customer = self._bills()
        with self._patch_ocr(fx.PLAIN_INVOICE_TEXT, AI), self._fail_for(bad):
            action = (good | bad | no_pdf | customer).action_run_ocr()
        self.assertEqual(action["tag"], "display_notification")
        params = action["params"]
        self.assertEqual(params["type"], "warning")
        self.assertTrue(params["sticky"])
        self.assertEqual(params["next"]["tag"], "soft_reload")
        message = params["message"]
        self.assertTrue(message.startswith("1 filled, 1 failed, 2 skipped"), message)
        self.assertIn(f"{bad.display_name}: boom", message)
        self.assertIn(f"{no_pdf.display_name}: no PDF attachment", message)
        self.assertIn(f"{customer.display_name}: not a draft vendor bill", message)
        # the good bill was filled although the next one failed
        self.assertEqual(good.ref, "4711")
        self.assertFalse(bad.ref)
        self.assertIn("OCR could not fill in this bill: boom", self._bodies(bad))

    def test_all_filled_is_a_success(self):
        good = self._with_pdf(self._new_bill())
        with self._patch_ocr(fx.PLAIN_INVOICE_TEXT, AI):
            params = good.action_run_ocr()["params"]
        self.assertEqual(params["type"], "success")
        self.assertEqual(params["message"], "1 filled")
        self.assertFalse(params["sticky"])

    def test_list_action_returns_the_summary(self):
        good, bad, no_pdf, _customer = self._bills()
        server_action = self.env.ref("account_invoice_ocr_ai.action_run_ocr_server")
        records = good | bad | no_pdf
        with self._patch_ocr(fx.PLAIN_INVOICE_TEXT, AI), self._fail_for(bad):
            action = server_action.with_context(
                active_model="account.move", active_ids=records.ids, active_id=good.id).run()
        self.assertEqual(action["tag"], "display_notification")
        self.assertTrue(action["params"]["message"].startswith("1 filled, 1 failed, 1 skipped"))
        self.assertEqual(good.ref, "4711")
        self.assertFalse(self.env["ir.logging"].search_count(
            [("path", "=", "action_run_ocr_server")]))
