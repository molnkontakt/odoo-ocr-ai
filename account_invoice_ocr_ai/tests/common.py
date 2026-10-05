"""Shared set-up for the Odoo tests that run the OCR fill on a vendor bill."""
import contextlib
from unittest import mock

from odoo.addons.account_invoice_ocr_ai.lib import invoice_ocr
from odoo.tests import TransactionCase
from odoo.tools import SQL

from . import ocr_fixtures as fx

PDF = [{"filename": "invoice.pdf", "mimetype": "application/pdf", "raw": b"%PDF-1.4 test"}]


def run_ocr_cron(env):
    """Run the OCR cron once, in the test's transaction.

    ir.cron._commit_progress commits (also outside a cron job), which a test must not do:
    it is replaced by a mock, returned so a test can look at the progress reported.
    """
    with mock.patch.object(env.registry["ir.cron"], "_commit_progress",
                           return_value=float("inf")) as progress:
        env["ocr.queue.mixin"]._ocr_cron_process()
    return progress


def _shift(records, sql):
    records.flush_recordset()
    records.env.cr.execute(SQL(sql, SQL.identifier(records._table), tuple(records.ids)))
    records.invalidate_recordset()


def make_due(records):
    """Move the queue times of `records` an hour back, as if their retry delay had passed.

    write_date moves along: in one test transaction every write has the same timestamp, so
    the queue's "changed by someone after it was queued" check would fire otherwise.
    """
    _shift(records, "UPDATE %s SET ocr_requested_at = ocr_requested_at - interval '1 hour', "
                    "write_date = write_date - interval '1 hour' WHERE id IN %s")


def changed_by_someone_else(records):
    """Make `records` look changed after they were queued (write_date after the queue time)."""
    _shift(records, "UPDATE %s SET ocr_requested_at = ocr_requested_at - interval '1 minute' "
                    "WHERE id IN %s")


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
        # The test documents are Swedish and in SEK: so are the test company's books, or a
        # bill in SEK would be a foreign-currency bill (#5).
        sek = cls.env.ref("base.SEK")
        sek.active = True
        if cls.env.company.currency_id != sek:
            cls.env.company.currency_id = sek
        cls.company_data = cls._accounting(cls.env.company)

    def _new_bill(self, **vals):
        return self.env["account.move"].create({"move_type": "in_invoice", **vals})

    def _attach_pdf(self, move, name="invoice.pdf", raw=b"%PDF-1.4 test"):
        return self.env["ir.attachment"].create({
            "name": name, "res_model": "account.move", "res_id": move.id, "raw": raw,
            "mimetype": "application/pdf"})

    def _upload(self, move, attachment=None):
        """The upload path on `move`: core found no decoder for the plain PDF (returns None),
        then the module's hook queues the bill."""
        Move = type(self.env["account.move"])
        base = next(c for c in Move.__mro__ if c.__dict__.get("_extend_with_attachments")
                    and "account_invoice_ocr_ai" not in c.__module__)
        attachment = attachment or self._attach_pdf(move)
        files_data = [{"name": attachment.name, "mimetype": attachment.mimetype,
                       "raw": attachment.raw, "attachment": attachment}]
        with mock.patch.object(base, "_extend_with_attachments", return_value=None):
            return move._extend_with_attachments(files_data, new=True)

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
