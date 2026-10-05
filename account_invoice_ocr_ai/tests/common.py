"""Shared set-up for the Odoo tests that run the OCR fill on a vendor bill."""
import contextlib
from unittest import mock

from odoo.addons.account_invoice_ocr_ai.lib import invoice_ocr
from odoo.tests import TransactionCase

from . import ocr_fixtures as fx

PDF = [{"filename": "invoice.pdf", "mimetype": "application/pdf", "raw": b"%PDF-1.4 test"}]


class OcrBillCase(TransactionCase):
    # Deliberately not AccountTestInvoicingCommon: account/tests imports test_mail,
    # which is not always on the addons path. The chart of accounts is loaded here instead.

    @classmethod
    def _accounting(cls, company):
        Chart = cls.env["account.chart.template"]
        if not company.chart_template:
            Chart.try_loading("generic_coa", company=company, install_demo=False)
        Account = cls.env["account.account"].with_company(company)
        domain = [("company_ids", "in", company.id)]
        return {
            "company": company,
            "default_account_expense": Account.search(
                domain + [("account_type", "=", "expense")], limit=1),
            "default_account_payable": Account.search(
                domain + [("account_type", "=", "liability_payable")], limit=1),
            "default_journal_bank": cls.env["account.journal"].search(
                [("company_id", "=", company.id), ("type", "=", "bank")], limit=1),
        }

    @classmethod
    def _se_company(cls, name, **vals):
        """A new company on l10n_se's Swedish chart ("se"), in SEK."""
        sek = cls.env.ref("base.SEK")
        sek.active = True
        company = cls.env["res.company"].create({
            "name": name, "country_id": cls.env.ref("base.se").id, "currency_id": sek.id, **vals})
        cls.env["account.chart.template"].try_loading("se", company=company, install_demo=False)
        return company

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company_data = cls._accounting(cls.env.company)

    def _new_bill(self, **vals):
        return self.env["account.move"].create({"move_type": "in_invoice", **vals})

    def _tax(self, company, xmlid):
        """l10n_se's tax `xmlid` as created for `company`."""
        return self.env["account.chart.template"].with_company(company).ref(xmlid)

    @contextlib.contextmanager
    def _patch_ocr(self, text=fx.AUTODEBIT_TEXT, ai=None):
        """The library reads `text` from the PDF and the AI answers `ai` (no PDF, no network)."""
        ai = dict(fx.AI_ANSWER_OWN_ORG if ai is None else ai)
        with mock.patch.object(invoice_ocr, "extract_text", return_value=text), \
                mock.patch.object(invoice_ocr, "_extract_fields_ai", return_value=ai):
            yield

    def _run_ocr(self, move, text=fx.AUTODEBIT_TEXT, ai=None):
        with self._patch_ocr(text, ai):
            self.env["account.move"]._invoice_ocr_extend(move, PDF)
        return move

    def _bodies(self, move):
        return " ".join(str(m.body) for m in move.message_ids)
