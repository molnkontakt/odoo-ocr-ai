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
    # A Char (shown as a text area): res.config.settings only stores Char, not Text, in a
    # system parameter.
    invoice_ocr_account_list = fields.Char(
        string="Accounts for invoice lines",
        config_parameter="invoice_ocr.account_list",
        help="The account codes the AI may choose for invoice lines, one per line as "
             "\"code: hint\", e.g. \"6540: IT services (consulting, managed services)\". "
             "Empty: the built-in Swedish BAS list. Only accounts that exist in the bill "
             "company's chart are sent; a line with another code gets the purchase journal's "
             "default account.",
    )
    # Time limits (#9)
    invoice_ocr_call_timeout = fields.Integer(
        string="AI call timeout (s)",
        config_parameter="invoice_ocr.call_timeout",
        help="Longest wait for one answer from the AI provider, for every provider. 0: the "
             "provider's default (120 s, or INVOICE_AI_TIMEOUT / STAIK_TIMEOUT). Every call is "
             "also cut to what is left of the time limit per document.",
    )
    invoice_ocr_total_deadline = fields.Integer(
        string="Time limit per document (s)",
        config_parameter="invoice_ocr.total_deadline",
        default=80,
        help="Reading one bill or receipt — the text and every call to the AI provider, its "
             "retries included — ends within this many seconds. Never more than three "
             "quarters of Odoo's request and cron time limits (limit_time_real, "
             "limit_time_real_cron: 90 s with Odoo's default 120 s), so the OCR button on "
             "the form returns before Odoo stops the request.",
    )
    invoice_ocr_cron_time_budget = fields.Integer(
        string="Background OCR time per run (s)",
        config_parameter="invoice_ocr.cron_time_budget",
        help="Uploaded and e-mailed documents are read by a background job within seconds. "
             "One run of it reads documents for at most this many seconds (a document is "
             "only started when its time limit still fits), then leaves the rest to the next "
             "run, which starts at once. 0: three quarters of Odoo's cron time limit "
             "(limit_time_real_cron, else limit_time_real: 90 s with Odoo's defaults). Keep "
             "it well under that limit: Odoo stops a worker that exceeds it.",
    )
    invoice_ocr_text_limit = fields.Integer(
        string="Text sent to the AI (characters)",
        config_parameter="invoice_ocr.text_limit",
        default=6000,
        help="At most this much of a document's text is sent to the AI provider; a longer "
             "text is sent as its beginning and its end (totals and payment details are "
             "usually at the end), and the chatter says so. More text costs more tokens and "
             "time, and must fit in the model's context. 0: the default (6000, or "
             "INVOICE_OCR_TEXT_LIMIT).",
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
    invoice_ocr_ollama_num_ctx = fields.Integer(
        string="Ollama context size (tokens)",
        config_parameter="invoice_ocr.ollama_num_ctx",
        default=16384,
        help="The context window Ollama runs the model with (num_ctx), sent with every "
             "request. Ollama's own default is small (4096 tokens on most hosts) and then cuts "
             "the beginning of a long prompt — the instructions — without an error. A larger "
             "value needs more RAM or VRAM. 0: the default (16384, or OLLAMA_NUM_CTX).",
    )

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

        ICP = self.env["ir.config_parameter"].sudo()

        def get(key):
            # A limit without a field on the form: its saved system parameter.
            name = f"invoice_ocr_{key}"
            return self[name] if name in self._fields else ICP.get_param(f"invoice_ocr.{key}")

        return invoice_ocr.config_from_settings(get)

    def action_invoice_ocr_verify_provider(self):
        """Round-trip with the values on the form (saved or not): which model answered, how
        many completion tokens it used and how fast (#23)."""
        self.ensure_one()
        from ..lib import invoice_ocr

        res = invoice_ocr.verify_provider(self._invoice_ocr_form_config())
        return {
            "type": "ir.actions.client", "tag": "display_notification",
            "params": {"title": _("Invoice OCR provider"), "sticky": True,
                       **self._invoice_ocr_verify_message(res)},
        }

    def _invoice_ocr_verify_message(self, res):
        """The notification's message and type for a verify_provider result."""
        from ..lib import invoice_ocr

        provider = res.get("provider")
        tokens = res.get("completion_tokens")
        tokens = tokens if tokens is not None else "?"
        if not res.get("ok"):
            if res.get("finish_reason") == "length":
                return {"type": "warning", "message": _(
                    "%(provider)s is reachable, but its answer was cut off at the token limit "
                    "(%(tokens)s completion tokens) before it was complete. A reasoning model "
                    "may need a higher limit (invoice_ocr.max_tokens).",
                    provider=provider, tokens=tokens)}
            return {"type": "danger", "message": _(
                "%(provider)s failed: %(error)s", provider=provider,
                error=self.env["ocr.queue.mixin"]._ocr_note_text(res.get("error"))
                or _("no valid answer"))}
        served = res.get("model_served") or "?"
        requested = res.get("model_requested") or "?"
        message = _("%(provider)s answered in %(seconds)s s with model %(served)s "
                    "(%(tokens)s completion tokens).", provider=provider,
                    seconds=res.get("latency_s"), served=served, tokens=tokens)
        if res.get("model_matches", True):
            return {"type": "success", "message": message}
        if (invoice_ocr.reasoning_base_name(requested) or "").lower() == served.lower():
            note = _("You asked for the reasoning model %(requested)s, but the answer shows no "
                     "reasoning: the provider probably did not recognise the name and "
                     "answered with its default model.", requested=requested)
        else:
            note = _("You asked for %(requested)s; the provider answered with another model.",
                     requested=requested)
        return {"type": "warning", "message": f"{message} {note}"}
