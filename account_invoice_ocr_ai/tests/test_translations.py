"""The module ships a Swedish translation (#35.3): field labels, selection values, the list
action, chatter notes — including the library's notes, which are translated through the
module's sv.po (ocr.queue.mixin._ocr_note_text) — and the background job writes its notes
in the language of the user who queued the bill."""
from odoo import Command
from odoo.addons.account_invoice_ocr_ai.lib import invoice_ocr
from odoo.tests import new_test_user, tagged

from . import ocr_fixtures as fx
from .common import PDF, OcrBillCase, run_ocr_cron
from .test_ocr_savepoint import AI_NEW_VENDOR, TEXT_NEW_VENDOR


@tagged("post_install", "-at_install", "invoice_ocr")
class TestTranslations(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.lang"]._activate_lang("sv_SE")
        cls.env["ir.module.module"]._load_module_terms(["account_invoice_ocr_ai"], ["sv_SE"])
        cls.sv = cls.env(context=dict(cls.env.context, lang="sv_SE"))

    def test_labels_in_swedish(self):
        fields = self.sv["account.move"].fields_get(["ocr_auto_debit", "ocr_state"])
        self.assertEqual(fields["ocr_auto_debit"]["string"], "Dras automatiskt")
        self.assertIn(("pending", "I kö"), fields["ocr_state"]["selection"])
        self.assertEqual(self.sv.ref("account_invoice_ocr_ai.action_run_ocr_server").name,
                         "Kör OCR igen")
        settings = self.sv["res.config.settings"].fields_get(["invoice_ocr_text_limit"])
        self.assertEqual(settings["invoice_ocr_text_limit"]["string"],
                         "Text som skickas till AI:n (tecken)")
        # the source stays English
        self.assertEqual(self.env["account.move"].fields_get(["ocr_auto_debit"])
                         ["ocr_auto_debit"]["string"], "Debited automatically")

    def test_chatter_note_in_swedish(self):
        """The fill note, a model note and a library note (the cut text) in Swedish."""
        move = self._new_bill().with_env(self.sv)
        text = fx.PLAIN_INVOICE_TEXT + "Specifikation rad\n" * 600
        with self._patch_ocr(text, {"vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711"}):
            self.sv["account.move"]._invoice_ocr_extend(move, PDF)
        bodies = self._bodies(move)
        self.assertIn("OCR + AI har fyllt i fakturan", bodies)
        self.assertIn("Kontroller:", bodies)
        self.assertIn("AI:n såg bara de första 4000 och de sista 2000", bodies)
        self.assertNotIn("the AI saw only", bodies)

    def test_totals_warning_in_swedish(self):
        """The totals check's sentences are translated too (they are built in a nested
        function, where Odoo's _() finds no language)."""
        move = self._new_bill().with_env(self.sv)
        ai = {"vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711",
              "lines": [{"description": "Support", "amount": 800.0, "vat_rate": 0}]}
        with self._patch_ocr(fx.PLAIN_INVOICE_TEXT, ai):
            self.sv["account.move"]._invoice_ocr_extend(move, PDF)
        bodies = self._bodies(move)
        self.assertIn("OCR: raderna stämmer inte med fakturan", bodies)
        self.assertIn("mot fakturans", bodies)
        self.assertNotIn("against the bill's", bodies)

    def test_library_notes_translate_with_their_parameters(self):
        Mixin = self.sv["ocr.queue.mixin"]
        error = invoice_ocr.ProviderError(503, "overloaded")
        note = invoice_ocr._("the AI step failed (%(error)s) – only the values read by the regex "
                             "were used", error=invoice_ocr.error_message(error))
        self.assertEqual(Mixin._ocr_note_text(note),
                         "AI-steget misslyckades (HTTP 503 från AI-tjänsten: overloaded) – bara "
                         "de värden som regex läste ut användes")
        self.assertEqual(self.env["ocr.queue.mixin"].with_context(lang="en_US")._ocr_note_text(note),
                         str(note))
        self.assertEqual(Mixin._ocr_note_text("plain text"), "plain text")
        self.assertEqual(Mixin._ocr_note_text(None), "")

    def test_background_notes_in_the_language_of_the_user_who_queued(self):
        """A Swedish user queues the bill: the job's notes are Swedish and theirs, also the
        note that they may not create the vendor."""
        self.env["ir.config_parameter"].sudo().set_param("invoice_ocr.max_attempts", "1")
        user = new_test_user(self.env, "ocr_sv", lang="sv_SE",
                             groups="base.group_user,account.group_account_invoice",
                             company_id=self.env.company.id,
                             company_ids=[Command.set(self.env.company.ids)])
        move = self._new_bill()
        self._upload(move.with_user(user))
        with self._patch_ocr(TEXT_NEW_VENDOR, AI_NEW_VENDOR):
            run_ocr_cron(self.env)
        self.assertEqual(move.ocr_state, "done")
        bodies = self._bodies(move)
        self.assertIn("OCR + AI har fyllt i fakturan", bodies)
        self.assertIn("Leverantör Example Newcomer AB: hittades inte, och du får inte skapa "
                      "kontakter", bodies)
        fill = move.message_ids.filtered(lambda m: m.subject == "OCR-fyllning")
        self.assertEqual(fill.author_id, user.partner_id)
