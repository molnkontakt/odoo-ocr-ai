"""The payment reference from the OCR run: only valid OCR numbers (modulus 10) are kept."""
from odoo.tests import TransactionCase, tagged


@tagged("post_install", "-at_install", "invoice_ocr")
class TestPaymentReference(TransactionCase):
    def test_valid_ocr_is_kept(self):
        ref = self.env["account.move"]._ocr_valid_payment_reference
        self.assertEqual(ref("1234567897"), "1234567897")
        self.assertEqual(ref("1234 5678 97"), "1234567897")

    def test_invoice_number_plus_postal_code(self):
        ref = self.env["account.move"]._ocr_valid_payment_reference
        self.assertEqual(ref("123456789711123", "1234567897"), "1234567897")
        self.assertFalse(ref("123456789711123"), "without the invoice number there is nothing valid to keep")

    def test_postal_code_alone_is_dropped(self):
        ref = self.env["account.move"]._ocr_valid_payment_reference
        self.assertFalse(ref("11123", "12345678"))

    def test_letters_are_left_alone(self):
        ref = self.env["account.move"]._ocr_valid_payment_reference
        self.assertEqual(ref("RF18 5390 0754 7034"), "RF18 5390 0754 7034")
        self.assertEqual(ref("INV-2026-0042"), "INV-2026-0042")

    def test_numbers_from_json(self):
        ref = self.env["account.move"]._ocr_valid_payment_reference
        self.assertEqual(ref(1234567897), "1234567897")
        self.assertEqual(ref(123456789711123, 1234567897), "1234567897")
        self.assertTrue(self.env["account.move"]._ocr_mod10("1234567897"))
