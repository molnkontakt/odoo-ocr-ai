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
        self.assertEqual(second["own_company"], "second example company")
        self.assertIn("SE999999999901", second["own_vat_numbers"])
        self.assertNotIn("SE999999999901", own["own_vat_numbers"])
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
        self.assertEqual(cfg["own_company"], (move.company_id.name or "").strip().lower())
        self.assertEqual(_globals(), before)
