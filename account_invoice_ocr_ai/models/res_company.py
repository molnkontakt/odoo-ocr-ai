from odoo import fields, models


class ResCompany(models.Model):
    _inherit = "res.company"

    ocr_not_vat_registered = fields.Boolean(
        string="Not VAT-registered (OCR)",
        help="The company is not registered for VAT (e.g. an association below the threshold) "
             "and cannot deduct input VAT. OCR then books bill lines and receipts gross: the "
             "VAT on the document is part of the cost, and no tax is set.",
    )
