"""Long documents (#21): the chatter says the model saw only part of the text, and the
marketplace VAT declarer is found anywhere in the text, not only in its first 2000
characters."""
from odoo.tests import tagged

from . import ocr_fixtures as fx
from .common import OcrBillCase


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrLongText(OcrBillCase):
    def test_truncation_is_noted_in_the_chatter(self):
        text = fx.PLAIN_INVOICE_TEXT + "Specifikation rad\n" * 600
        ai = {"vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711"}
        move = self._run_ocr(self._new_bill(), text=text, ai=ai)
        self.assertEqual(move.ref, "4711")
        self.assertIn("the AI saw only the first 4000 and the last 2000", self._bodies(move))

    def test_marketplace_declarer_after_2000_characters(self):
        text = (fx.PLAIN_INVOICE_TEXT + "Specifikation rad\n" * 150
                + "Såld av Some Merchant\nMoms deklarerat av Example Marketplace S.a.r.l.\n")
        self.assertGreater(text.index("Moms deklarerat"), 2000)
        ai = {"vendor_name": "Some Merchant", "invoice_number": "4711"}
        move = self._run_ocr(self._new_bill(), text=text, ai=ai)
        self.assertIn("marketplace VAT-declarer override → Example Marketplace S.a.r.l",
                      self._bodies(move))
