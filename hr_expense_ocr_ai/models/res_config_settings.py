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
