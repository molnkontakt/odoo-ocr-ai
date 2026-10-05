"""The on/off switch sticks (#6), and OCR leaves electronically imported bills alone (#18)."""
from unittest import mock

from odoo.addons.account_invoice_ocr_ai.lib import invoice_ocr
from odoo.tests import TransactionCase, tagged

PDF = [{"filename": "invoice.pdf", "mimetype": "application/pdf", "raw": b"%PDF-1.4 test"}]


@tagged("post_install", "-at_install", "invoice_ocr")
class TestEnableToggleAndEdi(TransactionCase):
    def test_disable_is_stored_and_respected(self):
        ICP = self.env["ir.config_parameter"].sudo()
        self.env["res.config.settings"].create({"invoice_ocr_enabled": False}).set_values()
        self.assertEqual(ICP.get_param("invoice_ocr.enabled"), "False")
        self.assertFalse(self.env["res.config.settings"].create({}).invoice_ocr_enabled)
        move = self.env["account.move"].create({"move_type": "in_invoice"})
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            self.env["account.move"]._invoice_ocr_extend(move, PDF)
        extract.assert_not_called()
        self.env["res.config.settings"].create({"invoice_ocr_enabled": True}).set_values()
        self.assertEqual(ICP.get_param("invoice_ocr.enabled"), "True")

    def test_edi_import_skips_ocr(self):
        Move = type(self.env["account.move"])
        base = next(c for c in Move.__mro__ if c.__dict__.get("_extend_with_attachments")
                    and "account_invoice_ocr_ai" not in c.__module__)
        move = self.env["account.move"].create({"move_type": "in_invoice"})
        with mock.patch.object(base, "_extend_with_attachments", return_value=True), \
                mock.patch.object(Move, "_invoice_ocr_extend") as ocr:
            move._extend_with_attachments(PDF, new=True)
        ocr.assert_not_called()
        with mock.patch.object(base, "_extend_with_attachments", return_value=None), \
                mock.patch.object(Move, "_invoice_ocr_extend") as ocr:
            move._extend_with_attachments(PDF, new=True)
        ocr.assert_called_once()
