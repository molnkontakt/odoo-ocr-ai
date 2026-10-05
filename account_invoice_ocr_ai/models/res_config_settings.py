from odoo import _, fields, models


class ResConfigSettings(models.TransientModel):
    _inherit = "res.config.settings"

    invoice_ocr_enabled = fields.Boolean(
        string="Invoice OCR on upload",
        config_parameter="invoice_ocr.enabled",
        default=True,
    )
    invoice_ocr_provider = fields.Selection(
        selection=[
            ("staik", "staik (Swedish data residency)"),
            ("venice", "Venice.ai"),
            ("openai", "OpenAI"),
            ("openai_compatible", "Other OpenAI-compatible endpoint (custom URL)"),
            ("ollama", "Ollama (local)"),
        ],
        string="AI provider",
        config_parameter="invoice_ocr.provider",
        default="staik",
    )
    # staik
    invoice_ocr_staik_api_key = fields.Char(string="staik API key", config_parameter="invoice_ocr.staik_api_key")
    invoice_ocr_staik_model = fields.Char(
        string="staik model", config_parameter="invoice_ocr.staik_model", default="qwen3.6:35b-a3b-thinking",
        help="Use a reasoning model; the base model answers without working out multi-rate sums. "
             "An unknown model name silently falls back to staik's default model — use Verify.",
    )
    # Venice
    invoice_ocr_venice_api_key = fields.Char(string="Venice.ai API key", config_parameter="invoice_ocr.venice_api_key")
    invoice_ocr_venice_model = fields.Char(
        string="Venice.ai model", config_parameter="invoice_ocr.venice_model", default="google-gemma-3-27b-it",
    )
    # OpenAI
    invoice_ocr_openai_api_key = fields.Char(string="OpenAI API key", config_parameter="invoice_ocr.openai_api_key")
    invoice_ocr_openai_model = fields.Char(string="OpenAI model", config_parameter="invoice_ocr.openai_model", default="gpt-4o-mini")
    # Any OpenAI-compatible endpoint
    invoice_ocr_base_url = fields.Char(
        string="Base URL", config_parameter="invoice_ocr.base_url",
        help="Up to and including the API version, e.g. https://api.mistral.ai/v1 or http://vllm.local:8000/v1. "
             "/chat/completions is appended.",
    )
    invoice_ocr_api_key = fields.Char(string="API key", config_parameter="invoice_ocr.api_key")
    invoice_ocr_model = fields.Char(string="Model", config_parameter="invoice_ocr.model")
    # Ollama
    invoice_ocr_ollama_url = fields.Char(string="Ollama URL", config_parameter="invoice_ocr.ollama_url", default="http://localhost:11434")
    invoice_ocr_ollama_model = fields.Char(string="Ollama model", config_parameter="invoice_ocr.ollama_model", default="qwen2.5:7b")

    def set_values(self):
        """Store the on/off switch explicitly as "True"/"False".

        A Boolean with config_parameter deletes the parameter when unticked, and a missing
        parameter reads as "on" (the default for new installs), so switching OCR off never
        stuck. A stored "False" reads as off both in the form (str2bool) and in the code.
        """
        res = super().set_values()
        self.env["ir.config_parameter"].sudo().set_param(
            "invoice_ocr.enabled", "True" if self.invoice_ocr_enabled else "False")
        return res

    def _invoice_ocr_form_config(self):
        """Per-run config from the values on the form (saved or not) — same rules as a real run.

        Empty fields keep the environment defaults, exactly as an empty system parameter does
        in account.move._invoice_ocr_config, so Verify tests what a run would use after Save.
        Nothing is written to the library's module globals: an unsaved key is never used by a
        real extraction, and a cleared key stops working as soon as it is saved.
        """
        self.ensure_one()
        from ..lib import invoice_ocr

        return invoice_ocr.config_from_settings(lambda key: self[f"invoice_ocr_{key}"])

    def action_invoice_ocr_verify_provider(self):
        """Round-trip with the values on the form (saved or not) and report which model answered."""
        self.ensure_one()
        from ..lib import invoice_ocr

        res = invoice_ocr.verify_provider(self._invoice_ocr_form_config())
        if res.get("ok"):
            served = res.get("model_served") or "?"
            requested = res.get("model_requested") or "?"
            # staik reports the base model name even for its "-thinking" variant, so a prefix
            # relation counts as a match; anything else is a silent substitution worth a warning.
            mismatch = served != "?" and not (served == requested or requested.startswith(served) or served.startswith(requested))
            message = _("%(provider)s answered in %(s)s s with model %(served)s%(note)s",
                        provider=res["provider"], s=res["latency_s"], served=served,
                        note=_(" — NOTE: you asked for %s; the provider substituted another model", requested) if mismatch else "")
            kind = "warning" if mismatch else "success"
        else:
            message = _("%(provider)s failed: %(err)s", provider=res.get("provider"), err=res.get("error") or _("no valid answer"))
            kind = "danger"
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Invoice OCR provider"), "message": message, "type": kind, "sticky": True},
        }
