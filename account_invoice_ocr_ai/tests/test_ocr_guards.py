"""Odoo tests for the own-company, auto-debit and double-booking guards.

Scenario: a bank's invoice for a one-off fee was booked with the buyer's OWN company as
vendor and the buyer's OWN bankgiro as recipient account, although the document is
debited automatically and the bank statement line was already reconciled directly
against the expense account. All parties and numbers are invented (see ocr_fixtures).
"""
from odoo.addons.account_invoice_ocr_ai.lib import invoice_ocr
from odoo.tests import tagged

from . import ocr_fixtures as fx
from .common import OcrBillCase


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrGuards(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.company_data["company"]
        cls.company.write({"name": fx.OWN_NAME, "vat": fx.OWN_VAT,
                           "company_registry": fx.OWN_ORG})
        cls.own_partner = cls.company.partner_id
        cls.own_contact = cls.env["res.partner"].create(
            {"name": "Ekonomi", "parent_id": cls.own_partner.id})
        Bank = cls.env["res.partner.bank"]
        cls.own_iban = Bank.create({"partner_id": cls.own_partner.id,
                                    "acc_number": fx.OWN_IBAN})
        cls.own_bg = Bank.create({"partner_id": cls.own_partner.id,
                                  "acc_number": "BG " + fx.OWN_BANKGIRO})
        cls.vendor = cls.env["res.partner"].create(
            {"name": "Example Bank", "vat": fx.VENDOR_VAT, "is_company": True,
             "supplier_rank": 1})
        cls.expense = cls.company_data["default_account_expense"]
        cls.payable = cls.company_data["default_account_payable"]
        cls.bank_journal = cls.company_data["default_journal_bank"]

    # -- helpers -----------------------------------------------------------

    def _resolve(self, data):
        notes = []
        pid = self.env["account.move"]._resolve_partner_from_ocr(data, notes=notes)
        return self.env["res.partner"].browse(pid), notes

    # -- the vendor -------------------------------------------------

    def test_own_context_from_res_company(self):
        own = self.env["account.move"]._ocr_own_context()
        self.assertIn(fx.OWN_VAT, own["ids"])
        self.assertIn(fx.OWN_ORG, own["ids"])
        self.assertIn(self.own_partner.id, own["partner_ids"])
        self.assertTrue(self.env["account.move"]._ocr_is_own_partner(self.own_contact, own))
        self.assertTrue(self.env["account.move"]._ocr_is_own_bank_number(
            "9999-0012345", own))
        self.assertTrue(self.env["account.move"]._ocr_is_own_bank_number(
            fx.OWN_BANKGIRO, own))
        self.assertFalse(self.env["account.move"]._ocr_is_own_bank_number("123-4567", own))
        # own account number truncated by the LLM and called a bankgiro
        for truncated in ("9999-0012", "9999-001"):
            self.assertTrue(self.env["account.move"]._ocr_is_own_bank_number(
                truncated, own), truncated)
        # the start of an own bankgiro is not own (7- and 8-digit bankgiro numbers)
        self.assertFalse(self.env["account.move"]._ocr_is_own_bank_number("999-000", own))

    def test_truncated_own_account_as_bankgiro(self):
        """Re-run: the LLM returned bankgiro '9999-0012' for the own account '9999-0012345'."""
        text = fx.AUTODEBIT_TEXT.replace(
            "Betalningavfakturanskermedautomatikfrånföretagetskonto.", "").replace(
            "Beloppetkommerattdebiterasföretagetskonto", "")
        self.assertFalse(invoice_ocr.detect_auto_debit(text))
        before = self.env["res.partner.bank"].search_count([])
        move = self._run_ocr(self._new_bill(), text=text,
                             ai=dict(fx.AI_ANSWER_OWN_ORG, bankgiro="9999-0012"))
        self.assertEqual(move.partner_id, self.vendor)
        self.assertFalse(move.partner_bank_id)
        self.assertEqual(self.env["res.partner.bank"].search_count([]), before)
        self.assertIn("bolagets eget konto", self._bodies(move))

    def test_resolve_never_returns_own_company(self):
        before = self.env["res.partner"].search_count([])
        partner, notes = self._resolve({"org_number": fx.OWN_ORG,
                                        "vendor_name": fx.OWN_NAME,
                                        "bankgiro": fx.OWN_BANKGIRO})
        self.assertFalse(partner)
        self.assertTrue(notes)
        # and no new "vendor" with the own details was created
        self.assertEqual(self.env["res.partner"].search_count([]), before)

    def test_resolve_falls_through_to_next_method(self):
        """Own org.nr (only the own company matches) → falls through to the name."""
        partner, notes = self._resolve({"org_number": fx.OWN_VAT,
                                        "vendor_name": "Example Bank"})
        self.assertEqual(partner, self.vendor)
        self.assertTrue(any("eget" in n for n in notes))

    def test_resolve_by_own_bankgiro_is_skipped(self):
        partner, _notes = self._resolve({"bankgiro": fx.OWN_BANKGIRO})
        self.assertFalse(partner)

    def test_resolve_by_name_token_skips_own_company(self):
        """The name token 'Acme' only hits the own company → no vendor."""
        partner, _notes = self._resolve({"vendor_name": "Acme Receiver Nord AB"})
        self.assertFalse(partner)

    def test_resolve_vendor_by_org(self):
        partner, _notes = self._resolve({"org_number": fx.VENDOR_ORG})
        self.assertEqual(partner, self.vendor)

    # -- the whole flow on the auto-debit document -------------------------------------

    def test_autodebit_flow(self):
        move = self._run_ocr(self._new_bill())
        self.assertEqual(move.partner_id, self.vendor)
        self.assertFalse(move.partner_bank_id)
        self.assertTrue(move.ocr_auto_debit)
        self.assertEqual(move.ref, fx.INVOICE_NUMBER)
        bodies = self._bodies(move)
        self.assertIn("Dras automatiskt från kontot – ska inte betalas manuellt", bodies)
        self.assertIn("Kontroller", bodies)
        self.assertIn(fx.VENDOR_ORG, bodies)

    def test_preset_own_partner_is_replaced(self):
        move = self._run_ocr(self._new_bill(partner_id=self.own_partner.id))
        self.assertEqual(move.partner_id, self.vendor)
        self.assertFalse(move.partner_bank_id)

    def test_preset_foreign_vendor_is_kept(self):
        other = self.env["res.partner"].create({"name": "Other Vendor AB",
                                                "is_company": True})
        move = self._run_ocr(self._new_bill(partner_id=other.id))
        self.assertEqual(move.partner_id, other)
        self.assertNotIn("ersatt med leverantören", self._bodies(move))

    # -- several companies: only the bill's company is the buyer ----------------

    def _sister(self):
        sister = self.env["res.company"].create({
            "name": fx.SISTER_NAME, "vat": fx.SISTER_VAT,
            "company_registry": fx.SISTER_ORG})
        bank = self.env["res.partner.bank"].create(
            {"partner_id": sister.partner_id.id, "acc_number": "BG " + fx.SISTER_BANKGIRO})
        return sister, bank

    def test_own_context_is_the_moves_company_only(self):
        sister, _bank = self._sister()
        own = self.env["account.move"]._ocr_own_context(self.company)
        self.assertIn(self.own_partner.id, own["partner_ids"])
        self.assertNotIn(sister.partner_id.id, own["partner_ids"])
        self.assertNotIn(fx.SISTER_ORG, own["ids"])
        own_b = self.env["account.move"]._ocr_own_context(sister)
        self.assertIn(sister.partner_id.id, own_b["partner_ids"])
        self.assertNotIn(self.own_partner.id, own_b["partner_ids"])

    def test_sister_company_can_be_vendor(self):
        """A bill from company B booked in company A gets B as vendor."""
        sister, bank = self._sister()
        text = fx.PLAIN_INVOICE_TEXT.replace(fx.PLAIN_VENDOR_ORG, fx.SISTER_ORG).replace(
            fx.PLAIN_VENDOR_BANKGIRO, fx.SISTER_BANKGIRO).replace(fx.PLAIN_VENDOR_NAME, fx.SISTER_NAME)
        ai = {"vendor_name": fx.SISTER_NAME, "invoice_number": "4711",
              "org_number": fx.SISTER_ORG, "total_amount": 1250.0, "subtotal": 1000.0,
              "vat_amount": 250.0,
              "lines": [{"description": "Rent", "amount": 1000.0, "vat_rate": 25,
                         "account_code": "5010"}]}
        move = self._run_ocr(self._new_bill(), text=text, ai=ai)
        self.assertEqual(move.partner_id, sister.partner_id)
        self.assertEqual(move.partner_bank_id, bank)
        # a pre-set sister company is not replaced
        move2 = self._run_ocr(self._new_bill(partner_id=sister.partner_id.id),
                              text=text, ai=ai)
        self.assertEqual(move2.partner_id, sister.partner_id)

    # -- another partner carrying the own number ---------------------------

    def test_duplicate_partner_with_own_vat_counts_as_own(self):
        dup = self.env["res.partner"].create({
            "name": "Acme Receiver Sverige AB", "vat": fx.OWN_VAT,
            "is_company": True, "active": False})
        dup_bank = self.env["res.partner.bank"].create(
            {"partner_id": dup.id, "acc_number": "BG 999-0011"})
        dup.active = True
        own = self.env["account.move"]._ocr_own_context(self.company)
        self.assertIn(dup.id, own["partner_ids"])
        self.assertTrue(self.env["account.move"]._ocr_is_own_bank_number("999-0011", own))
        partner, _notes = self._resolve({"vendor_name": "Acme Receiver Sverige AB"})
        self.assertFalse(partner)
        partner, _notes = self._resolve({"bankgiro": "999-0011"})
        self.assertFalse(partner)
        move = self._new_bill(partner_id=dup.id)
        move.partner_bank_id = dup_bank
        self._run_ocr(move)
        self.assertEqual(move.partner_id, self.vendor)
        self.assertFalse(move.partner_bank_id)

    def test_own_registry_on_partner_counts_as_own(self):
        dup = self.env["res.partner"].create({
            "name": "Acme Duplicate", "company_registry": "9999990006", "is_company": True})
        own = self.env["account.move"]._ocr_own_context(self.company)
        self.assertIn(dup.id, own["partner_ids"])

    # -- a re-run clears a flag set by OCR, not a manual one --------

    def test_rerun_clears_ocr_set_flag(self):
        move = self._run_ocr(self._new_bill())
        self.assertTrue(move.ocr_auto_debit)
        self.assertEqual(move.ocr_auto_debit_phrase, "skermedautomatik")
        self._run_ocr(move, text=fx.PLAIN_INVOICE_TEXT)
        self.assertFalse(move.ocr_auto_debit)
        self.assertFalse(move.ocr_auto_debit_phrase)
        self.assertIn("flaggan togs bort", self._bodies(move))

    def test_rerun_keeps_manual_flag(self):
        move = self._new_bill(partner_id=self.vendor.id)
        move.ocr_auto_debit = True
        self._run_ocr(move, text=fx.PLAIN_INVOICE_TEXT)
        self.assertTrue(move.ocr_auto_debit)
        # a flag set by OCR and then changed by hand becomes manual
        move2 = self._run_ocr(self._new_bill())
        move2.write({"ocr_auto_debit": False})
        self.assertFalse(move2.ocr_auto_debit_phrase)
        move2.write({"ocr_auto_debit": True})
        self._run_ocr(move2, text=fx.PLAIN_INVOICE_TEXT)
        self.assertTrue(move2.ocr_auto_debit)

    def test_own_bank_never_set_on_vendor_bill(self):
        """No auto debit, but the document prints the own bankgiro."""
        text = fx.AUTODEBIT_TEXT.replace(
            "Betalningavfakturanskermedautomatikfrånföretagetskonto.", "").replace(
            "Beloppetkommerattdebiterasföretagetskonto", "Bankgiro: " + fx.OWN_BANKGIRO)
        self.assertFalse(invoice_ocr.detect_auto_debit(text))
        move = self._run_ocr(self._new_bill(), text=text,
                             ai=dict(fx.AI_ANSWER_OWN_ORG, bankgiro=fx.OWN_BANKGIRO))
        self.assertEqual(move.partner_id, self.vendor)
        self.assertFalse(move.partner_bank_id)
        self.assertFalse(move.ocr_auto_debit)
        self.assertIn("bolagets eget konto", self._bodies(move))

    def test_drop_own_partner_bank(self):
        move = self._new_bill(partner_id=self.vendor.id)
        move.partner_bank_id = self.own_bg
        notes = []
        own = self.env["account.move"]._ocr_own_context()
        self.env["account.move"]._ocr_drop_own_partner_bank(move, own, notes)
        self.assertFalse(move.partner_bank_id)
        self.assertTrue(notes)

    def test_vendor_bank_kept_when_not_auto_debit(self):
        vendor = self.env["res.partner"].create(
            {"name": fx.PLAIN_VENDOR_NAME, "vat": "SE999999002201", "is_company": True})
        bank = self.env["res.partner.bank"].create(
            {"partner_id": vendor.id, "acc_number": "BG " + fx.PLAIN_VENDOR_BANKGIRO})
        ai = {"vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711",
              "total_amount": 1250.0, "subtotal": 1000.0, "vat_amount": 250.0,
              "lines": [{"description": "Consulting", "amount": 1000.0, "vat_rate": 25,
                         "account_code": "6540"}]}
        move = self._run_ocr(self._new_bill(), text=fx.PLAIN_INVOICE_TEXT, ai=ai)
        self.assertEqual(move.partner_id, vendor)
        self.assertEqual(move.partner_bank_id, bank)
        self.assertFalse(move.ocr_auto_debit)
        self.assertNotIn("Dras automatiskt från kontot", self._bodies(move))

    # -- already booked through the bank statement line ------------------------------------

    def _st_line(self, payment_ref, amount=-2500.0, date="2026-07-02", account=None):
        vals = {"journal_id": self.bank_journal.id, "payment_ref": payment_ref,
                "amount": amount, "date": date}
        if account:
            vals["counterpart_account_id"] = account.id
        return self.env["account.bank.statement.line"].create(vals)

    def test_prebooked_by_invoice_number(self):
        st = self._st_line("Other " + fx.INVOICE_NUMBER, account=self.expense)
        move = self._run_ocr(self._new_bill())
        bodies = self._bodies(move)
        self.assertIn("Kostnaden kan redan vara bokförd", bodies)
        self.assertIn(st.move_id.name, bodies)

    def test_prebooked_by_amount_date_and_name(self):
        st = self._st_line("EXAMPLE BANK AVGIFT", date="2026-06-28", account=self.expense)
        move = self._run_ocr(self._new_bill())
        self.assertIn(st.move_id.name, self._bodies(move))

    def _assert_not_prebooked(self):
        move = self._run_ocr(self._new_bill())
        self.assertNotIn("Kostnaden kan redan vara bokförd", self._bodies(move))

    def test_not_prebooked_when_reconciled_to_payable(self):
        self._st_line("Other " + fx.INVOICE_NUMBER, account=self.payable)
        self._assert_not_prebooked()

    def test_not_prebooked_when_unreconciled(self):
        self._st_line("Other " + fx.INVOICE_NUMBER)  # suspense = not reconciled
        self._assert_not_prebooked()

    def test_not_prebooked_outside_date_window(self):
        self._st_line("EXAMPLE BANK AVGIFT", date="2026-08-30", account=self.expense)
        self._assert_not_prebooked()

    def test_not_prebooked_other_name(self):
        self._st_line("SOMEONE ELSE", date="2026-07-01", account=self.expense)
        self._assert_not_prebooked()

    def test_not_prebooked_payable_plus_fee(self):
        """Payment against the payable plus a small fee line is not a direct booking."""
        st = self._st_line("Other " + fx.INVOICE_NUMBER, account=self.payable)
        move = st.move_id
        move.button_draft()
        payable_line = move.line_ids.filtered(lambda line: line.account_id == self.payable)
        move.write({"line_ids": [
            (1, payable_line.id, {"debit": 2490.0, "credit": 0.0}),
            (0, 0, {"account_id": self.expense.id, "name": "Fee",
                    "debit": 10.0, "credit": 0.0}),
        ]})
        move.action_post()
        _liq, _susp, other = st._seek_for_lines()
        self.assertEqual(len(other), 2)
        self._assert_not_prebooked()

    def test_prebooked_ignores_other_company(self):
        other = self._accounting(self.env["res.company"].create({"name": "Other Company"}))
        self.env["account.bank.statement.line"].with_company(other["company"]).create({
            "journal_id": other["default_journal_bank"].id,
            "payment_ref": "Other " + fx.INVOICE_NUMBER, "amount": -2500.0,
            "date": "2026-07-02",
            "counterpart_account_id": other["default_account_expense"].id,
        })
        move = self._run_ocr(self._new_bill())
        self.assertNotIn("Kostnaden kan redan vara bokförd", self._bodies(move))
