"""The module ships a Swedish translation (#36.12): labels, the button and list action, and
the receipt note with the receipt library's notes in Swedish."""
from odoo.addons.account_invoice_ocr_ai.tests.common import EnglishTestCase
from odoo.addons.hr_expense_ocr_ai.lib import receipt_ocr
from odoo.tests import tagged

TEXT = "Example Restaurant\nTotalt 112,00\n"


@tagged("post_install", "-at_install", "expense_ocr")
class TestReceiptTranslations(EnglishTestCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.env["res.lang"]._activate_lang("sv_SE")
        cls.env["ir.module.module"]._load_module_terms(
            ["account_invoice_ocr_ai", "hr_expense_ocr_ai"], ["sv_SE"])
        cls.sv = cls.env(context=dict(cls.env.context, lang="sv_SE"))
        cls.employee = cls.env["hr.employee"].create({"name": "Example Employee"})

    def test_labels_in_swedish(self):
        self.assertEqual(self.sv.ref("hr_expense_ocr_ai.action_read_receipt_server").name,
                         "Läs kvitto (OCR)")
        settings = self.sv["res.config.settings"].fields_get(["expense_ocr_enabled"])
        self.assertEqual(settings["expense_ocr_enabled"]["string"], "Kvitto-OCR på utlägg")
        state = self.sv["hr.expense"].fields_get(["ocr_state"])["ocr_state"]
        self.assertIn(("failed", "Misslyckades"), state["selection"])

    def test_receipt_note_in_swedish(self):
        expense = self.sv["hr.expense"].create({"name": "x", "employee_id": self.employee.id})
        att = self.env["ir.attachment"].create({
            "name": "kvitto.jpg", "raw": b"x", "mimetype": "image/jpeg",
            "res_model": "hr.expense", "res_id": expense.id})
        fields, notes = receipt_ocr._apply_guards(
            {"total": 112.0, "merchant": "Example Restaurant", "confidence": 0.3}, TEXT)
        result = {"text": TEXT, "source": "ai", "fields": fields, "notes": notes}
        expense._expense_ocr_apply(result, {}, att)
        body = str(expense.message_ids[0].body)
        self.assertIn("Kvitto-OCR", body)
        self.assertIn("läste kvitto.jpg", body)
        self.assertIn("låg konfidens (0.30) — belopp och datum fylls inte i", body)
        self.assertIn("Anmärkningar:", body)


@tagged("post_install", "-at_install", "expense_ocr")
class TestOcrStateLabels(EnglishTestCase):
    def _labels(self):
        self.env.invalidate_all()
        self.env.registry.clear_cache("stable")
        swedish = self.env(context=dict(self.env.context, lang="sv_SE"))
        return swedish["hr.expense"].fields_get(["ocr_state"])["ocr_state"]["selection"]

    def test_ocr_state_labels_are_loaded_for_hr_expense(self):
        """The OCR-state labels come from account_invoice_ocr_ai's .po, loaded before
        hr.expense has them: without the post-init hook (or the migration on an update) they
        stayed English on expenses after one install."""
        from odoo.addons.hr_expense_ocr_ai import _load_ocr_state_translations

        self.env["res.lang"]._activate_lang("sv_SE")
        selections = self.env["ir.model.fields.selection"].search([
            ("field_id.model", "=", "hr.expense"), ("field_id.name", "=", "ocr_state")])
        self.assertTrue(selections)
        selections.flush_recordset()
        self.env.cr.execute("UPDATE ir_model_fields_selection SET name = name - 'sv_SE' "
                            "WHERE id IN %s", [tuple(selections.ids)])
        self.assertIn(("failed", "Failed"), self._labels(), "the state the bug left")
        _load_ocr_state_translations(self.env)
        self.assertIn(("failed", "Misslyckades"), self._labels())
        self.assertIn(("pending", "I kö"), self._labels())
