from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    expense_ocr_enabled = fields.Boolean(
        string="Expense receipt OCR",
        config_parameter="expense_ocr.enabled",
        default=True,
        help="Reads receipt photos and PDFs on e-mailed expenses and on drafts that get a main "
             "attachment. Uses the same AI provider and keys as the invoice OCR.",
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
