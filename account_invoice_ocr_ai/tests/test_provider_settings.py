"""Settings reach the library as a per-run config; nothing writes its module globals.

Before, the settings were pushed into invoice_ocr's module globals, which every run in a
worker shares: the Verify button's unsaved key was then used by real extractions, and a
key cleared in the settings kept working until a restart.
"""
from unittest import mock

from odoo.addons.account_invoice_ocr_ai.lib import invoice_ocr
from odoo.tests import TransactionCase, tagged

PDF = [{"filename": "invoice.pdf", "mimetype": "application/pdf", "raw": b"%PDF-1.4 test"}]


class _Response:
    status_code = 200

    def __init__(self, payload):
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        pass


def _globals():
    return {k: (set(v) if isinstance(v, set) else v)
            for k, v in vars(invoice_ocr).items() if k.isupper()}


@tagged("post_install", "-at_install", "invoice_ocr")
class TestProviderSettings(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ICP = cls.env["ir.config_parameter"].sudo()
        cls.ICP.set_param("invoice_ocr.provider", "staik")
        cls.ICP.set_param("invoice_ocr.staik_api_key", "saved-key")

    def test_verify_uses_unsaved_form_values_without_touching_globals(self):
        settings = self.env["res.config.settings"].create({
            "invoice_ocr_provider": "openai_compatible",
            "invoice_ocr_base_url": "https://unsaved.example/v1",
            "invoice_ocr_api_key": "UNSAVED-KEY",
            "invoice_ocr_model": "unsaved-model",
        })
        before = _globals()
        posted = []

        def fake_post(url, **kwargs):
            posted.append((url, kwargs))
            return _Response({"model": "unsaved-model", "usage": {"completion_tokens": 3},
                              "choices": [{"finish_reason": "stop",
                                           "message": {"content": '{"ok": true}'}}]})

        with mock.patch.object(invoice_ocr, "_post", side_effect=fake_post):
            action = settings.action_invoice_ocr_verify_provider()

        self.assertEqual(action["params"]["type"], "success")
        self.assertEqual(posted[0][0], "https://unsaved.example/v1/chat/completions")
        self.assertEqual(posted[0][1]["headers"]["Authorization"], "Bearer UNSAVED-KEY")
        self.assertEqual(_globals(), before, "Verify must not write the library's globals")
        # Nothing was saved, and a real run still uses the saved settings
        self.assertEqual(self.ICP.get_param("invoice_ocr.provider"), "staik")
        self.assertFalse(self.ICP.get_param("invoice_ocr.api_key"))
        cfg = self.env["account.move"]._invoice_ocr_config()
        self.assertEqual(cfg["provider"], "staik")
        self.assertEqual(cfg["staik_api_key"], "saved-key")
        self.assertNotIn("UNSAVED-KEY", str(cfg))

    def test_cleared_key_stops_working_once_saved(self):
        Move = self.env["account.move"]
        self.assertEqual(Move._invoice_ocr_config()["staik_api_key"], "saved-key")
        self.ICP.set_param("invoice_ocr.staik_api_key", False)  # cleared in the settings
        cfg = Move._invoice_ocr_config()
        self.assertEqual(cfg["staik_api_key"], invoice_ocr.STAIK_API_KEY)  # env default
        self.assertNotEqual(cfg["staik_api_key"], "saved-key")

    def test_config_is_per_company(self):
        other = self.env["res.company"].create({"name": "Second Example Company",
                                                "vat": "SE999999999901"})
        before = _globals()
        Move = self.env["account.move"]
        own = Move._invoice_ocr_config(self.env.company)
        second = Move._invoice_ocr_config(other)
        self.assertIn("Second Example Company", second["own_names"])
        self.assertIn("SE999999999901", second["own_ids"])
        self.assertNotIn("SE999999999901", own["own_ids"])
        self.assertEqual(_globals(), before)

    def test_upload_run_passes_the_config(self):
        """The upload path hands the per-run config to the library and leaves the globals."""
        self.ICP.set_param("invoice_ocr.provider", "openai_compatible")
        self.ICP.set_param("invoice_ocr.base_url", "https://llm.example/v1")
        self.ICP.set_param("invoice_ocr.api_key", "K-saved")
        self.ICP.set_param("invoice_ocr.model", "model-x")
        move = self.env["account.move"].create({"move_type": "in_invoice"})
        before = _globals()
        with mock.patch.object(invoice_ocr, "extract_invoice_data", return_value={}) as extract:
            self.env["account.move"]._invoice_ocr_extend(move, PDF)
        cfg = extract.call_args.kwargs["config"]
        self.assertEqual(cfg["provider"], "openai_compatible")
        self.assertEqual(cfg["api_key"], "K-saved")
        self.assertEqual(cfg["base_url"], "https://llm.example/v1")
        self.assertIn(move.company_id.name, cfg["own_names"])
        self.assertEqual(_globals(), before)

    def test_own_context_travels_in_the_config(self):
        """Own ids/names/partners/banks reach the library and the guards via the config."""
        Move = self.env["account.move"]
        company = self.env.company
        company.write({"vat": "SE999999000601", "company_registry": "999999-0006"})
        self.env["res.partner.bank"].create({"partner_id": company.partner_id.id,
                                             "acc_number": "BG 999-0001"})
        before = _globals()
        cfg = Move._invoice_ocr_config(company)
        self.assertIn("SE999999000601", cfg["own_ids"])
        self.assertIn(company.name, cfg["own_names"])
        self.assertIn(company.partner_id.id, cfg["own_partner_ids"])
        self.assertIn("9990001", cfg["own_bank_keys"])
        self.assertEqual(Move._ocr_own_from_config(cfg), Move._ocr_own_context(company))
        # The library reads the own ids from the config: the buyer's org.nr is skipped
        text = "Faktura\nKund: Org.nr: 999999-0006\nLeverantör Org.nr: 999999-0014\n"
        fields = invoice_ocr.extract_fields(text, config=cfg)
        self.assertEqual(fields["org_number"], "999999-0014")
        self.assertEqual(_globals(), before)

    def test_time_limits_are_settings(self):
        """The per-call timeout and the deadline per document are settings (#9); the other
        limits are system parameters (#26)."""
        self.env["res.config.settings"].create({
            "invoice_ocr_call_timeout": 30, "invoice_ocr_total_deadline": 60,
            "invoice_ocr_cron_time_budget": 70}).set_values()
        self.assertEqual(self.ICP.get_param("invoice_ocr.call_timeout"), "30")
        self.assertEqual(self.ICP.get_param("invoice_ocr.cron_time_budget"), "70")
        self.ICP.set_param("invoice_ocr.max_text_pages", "8")
        cfg = self.env["account.move"]._invoice_ocr_config()
        self.assertEqual((cfg["call_timeout"], cfg["total_deadline"]), (30, 60))
        self.assertEqual(cfg["max_text_pages"], 8)
        # 0 means the default
        self.env["res.config.settings"].create({
            "invoice_ocr_call_timeout": 0, "invoice_ocr_total_deadline": 0}).set_values()
        self.assertFalse(self.ICP.get_param("invoice_ocr.call_timeout"))
        cfg = self.env["account.move"]._invoice_ocr_config()
        self.assertIsNone(cfg["call_timeout"])
        self.assertEqual(cfg["total_deadline"], invoice_ocr.TOTAL_DEADLINE)
        # the Verify button sees the form's values, and the saved limits without a field
        form = self.env["res.config.settings"].create({"invoice_ocr_call_timeout": 25})
        self.assertEqual(form._invoice_ocr_form_config()["call_timeout"], 25)
        self.assertEqual(form._invoice_ocr_form_config()["max_text_pages"], 8)

    def test_text_limit_and_ollama_context_are_settings(self):
        """The text sent to the AI (#21) and Ollama's context size (#20) are settings."""
        self.env["res.config.settings"].create({
            "invoice_ocr_text_limit": 9000, "invoice_ocr_ollama_num_ctx": 32768}).set_values()
        self.assertEqual(self.ICP.get_param("invoice_ocr.text_limit"), "9000")
        cfg = self.env["account.move"]._invoice_ocr_config()
        self.assertEqual((cfg["text_limit"], cfg["ollama_num_ctx"]), (9000, 32768))
        self.ICP.set_param("invoice_ocr.max_tokens", "16000")
        self.assertEqual(self.env["account.move"]._invoice_ocr_config()["max_tokens"], 16000)
        form = self.env["res.config.settings"].create({"invoice_ocr_text_limit": 3000})
        self.assertEqual(form._invoice_ocr_form_config()["text_limit"], 3000)
        self.assertEqual(form._invoice_ocr_form_config()["max_tokens"], 16000)

    def _verify(self, model, served, tokens, finish="stop", content='{"ok": true}'):
        settings = self.env["res.config.settings"].create({
            "invoice_ocr_provider": "staik", "invoice_ocr_staik_model": model})

        def fake_post(url, **kwargs):
            return _Response({"model": served, "usage": {"completion_tokens": tokens},
                              "choices": [{"finish_reason": finish,
                                           "message": {"content": content}}]})

        with mock.patch.object(invoice_ocr, "_post", side_effect=fake_post):
            return settings.action_invoice_ocr_verify_provider()["params"]

    def test_verify_shows_tokens_and_a_silent_fallback(self):
        """#23: the base name of a reasoning model only counts when the answer reasoned."""
        params = self._verify("qwen3.6:35b-a3b-thinking", "qwen3.6:35b-a3b", 212)
        self.assertEqual(params["type"], "success")
        self.assertIn("212 completion tokens", params["message"])
        params = self._verify("qwen3.6:35b-a3b-thinking", "qwen3.6:35b-a3b", 6)
        self.assertEqual(params["type"], "warning")
        self.assertIn("shows no reasoning", params["message"])
        params = self._verify("qwen3.6:35b-a3b-thinkng", "qwen3.6:35b-a3b", 6)
        self.assertEqual(params["type"], "warning")
        self.assertIn("answered with another model", params["message"])
        params = self._verify("qwen3.6:35b-a3b-thinking", "qwen3.6:35b-a3b", 1000,
                              finish="length", content="")
        self.assertEqual(params["type"], "warning")
        self.assertIn("cut off at the token limit", params["message"])
