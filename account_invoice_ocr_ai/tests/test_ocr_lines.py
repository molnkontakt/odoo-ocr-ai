"""Invoice lines come to the AI line's amount (#11) and malformed lines do not lose the
fill (#10)."""
from odoo.tests import tagged

from . import ocr_fixtures as fx
from .common import OcrBillCase

BASE = {"vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711"}


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrLines(OcrBillCase):
    def _lines(self, lines):
        move = self._run_ocr(self._new_bill(), text=fx.PLAIN_INVOICE_TEXT,
                             ai=dict(BASE, lines=lines))
        return move, move.invoice_line_ids.sorted("sequence")

    def test_amount_wins_over_quantity_and_unit_price(self):
        move, lines = self._lines([
            {"description": "Licences", "quantity": 3, "amount": 300.0, "vat_rate": 25,
             "account_code": "6540"},                              # no unit price
            {"description": None, "quantity": None, "unit_price": None, "amount": 250.0,
             "vat_rate": 25, "account_code": "6540"},             # nulls
            {"description": "Discounted", "quantity": 1, "unit_price": 500.0, "amount": 400.0,
             "vat_rate": 25, "account_code": "6540"},             # discount in the amount
        ])
        self.assertEqual(lines.mapped("price_subtotal"), [300.0, 250.0, 400.0])
        self.assertEqual(lines[0].quantity, 3)
        self.assertEqual(lines[0].price_unit, 100.0)
        self.assertEqual(lines[1].name, "4711", "a null description falls back")

    def test_malformed_lines_keep_the_rest(self):
        move, lines = self._lines(["a", None, {"amount": "1 000,00", "vat_rate": "25",
                                               "description": "Consulting"}])
        self.assertEqual(move.ref, "4711")
        self.assertEqual(lines.mapped("price_subtotal"), [1000.0])
