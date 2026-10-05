from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    expense_ocr_enabled = fields.Boolean(
        string="Kvitto-OCR på utlägg",
        config_parameter="expense_ocr.enabled",
        default=True,
        help="Läser kvittofoton på inmailade utlägg och på utkast som får en huvudbilaga. "
             "Använder samma AI-leverantör och nycklar som faktura-OCR:en.",
    )

    expense_ocr_placeholder_name_prefixes = fields.Char(
        string="Placeholder name prefixes",
        config_parameter="expense_ocr.placeholder_name_prefixes",
        help="Comma-separated. An expense whose description starts with one of these (e.g. the "
             "subject a mail alias gives expenses from unknown senders) gets the receipt's "
             "description appended. Upload's \"Untitled Expense\" is always replaced.",
    )

    def set_values(self):
        """Store the on/off switch explicitly as "True"/"False" (see account_invoice_ocr_ai:
        an unticked Boolean config_parameter is deleted, and a missing one reads as on)."""
        res = super().set_values()
        self.env["ir.config_parameter"].sudo().set_param(
            "expense_ocr.enabled", "True" if self.expense_ocr_enabled else "False")
        return res
