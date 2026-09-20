from odoo import fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    invoice_ocr_venice_api_key = fields.Char(
        string="Venice.ai API-nyckel",
        config_parameter="invoice_ocr.venice_api_key",
    )
    invoice_ocr_venice_model = fields.Char(
        string="Venice.ai modell",
        config_parameter="invoice_ocr.venice_model",
        default="google-gemma-3-27b-it",
    )
    invoice_ocr_provider = fields.Selection(
        selection=[("staik", "staik (svensk datahemvist)"),
                   ("venice", "Venice.ai"),
                   ("ollama", "Ollama (lokal)"),
                   ("openai", "OpenAI")],
        string="AI-leverantör för fakturatolkning",
        config_parameter="invoice_ocr.provider",
        default="staik",
    )
    invoice_ocr_staik_api_key = fields.Char(
        string="staik API-nyckel",
        config_parameter="invoice_ocr.staik_api_key",
    )
    invoice_ocr_staik_model = fields.Char(
        string="staik modell",
        config_parameter="invoice_ocr.staik_model",
        default="qwen3.6:35b-a3b-thinking",
        help="Reasoning-varianten kravs. Basmodellen svarar utan att rakna och far "
             "fel pa flertermssummor. OBS: okant modellnamn faller tyst tillbaka "
             "till staiks default-modell.",
    )
    invoice_ocr_openai_api_key = fields.Char(
        string="OpenAI API-nyckel",
        config_parameter="invoice_ocr.openai_api_key",
    )
    invoice_ocr_openai_model = fields.Char(
        string="OpenAI modell",
        config_parameter="invoice_ocr.openai_model",
        default="gpt-4o-mini",
        help="Modell som används när leverantören är OpenAI, t.ex. gpt-4o-mini.",
    )
    invoice_ocr_enabled = fields.Boolean(
        string="OCR-parsning av PDF-uppladdningar",
        config_parameter="invoice_ocr.enabled",
        default=True,
    )
