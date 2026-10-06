"""OCR runs in the background (#9, #17, #35.1).

The upload button, the mail alias and the list action only queue the bill; the OCR cron
reads it shortly after: one bill at a time, within its own time budget, with a retry limit,
leaving bills alone that are no longer drafts or were changed by someone after they were
queued. The form button still reads at once. ir.cron._commit_progress commits and is
replaced by a mock (run_ocr_cron).
"""
from unittest import mock

from odoo.addons.account_invoice_ocr_ai.lib import invoice_ocr
from odoo.addons.account_invoice_ocr_ai.models import ocr_queue
from odoo.tests import tagged
from odoo.tools import mute_logger

from . import ocr_fixtures as fx
from .common import OcrBillCase, changed_by_someone_else, make_due, run_ocr_cron

AI = {
    "vendor_name": fx.PLAIN_VENDOR_NAME, "invoice_number": "4711",
    "total_amount": 1250.0, "subtotal": 1000.0, "vat_amount": 250.0,
    "lines": [{"description": "Consulting", "amount": 1000.0, "vat_rate": 25,
               "account_code": "6540"}],
}
DATA = dict(AI, raw_text=fx.PLAIN_INVOICE_TEXT)


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrQueue(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.cron = cls.env.ref("account_invoice_ocr_ai.ir_cron_ocr_queue")

    def _triggers(self, future=False):
        domain = [("cron_id", "=", self.cron.id)]
        if future:
            domain.append(("call_at", ">", self.env.cr.now()))
        return self.env["ir.cron.trigger"].search_count(domain)

    def _queued_bill(self):
        move = self._new_bill()
        self._upload(move)
        return move

    def _set_param(self, key, value):
        self.env["ir.config_parameter"].sudo().set_param(key, value)

    # ---------------------------------------------------------------- queueing

    def test_upload_queues_without_reading(self):
        move = self._new_bill()
        att = self._attach_pdf(move)
        before = self._triggers()
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            self.assertTrue(self._upload(move, att),
                            "a queued bill is reported as imported, so core posts no error")
        extract.assert_not_called()
        self.assertEqual(move.ocr_state, "pending")
        self.assertEqual(move.ocr_attachment_id, att)
        self.assertEqual(move.ocr_attempts, 0)
        self.assertTrue(move.ocr_requested_at)
        self.assertFalse(move.ref)
        self.assertEqual(self._triggers(), before + 1, "the cron is woken at once")

    def test_journal_upload_queues_the_bill(self):
        journal = self.env["account.journal"].search([
            ("company_id", "=", self.env.company.id), ("type", "=", "purchase")], limit=1)
        att = self.env["ir.attachment"].create({
            "name": "bill.pdf", "raw": b"%PDF-1.4 test", "mimetype": "application/pdf"})
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            bill = journal.with_context(default_move_type="in_invoice") \
                ._create_document_from_attachment(att.ids)
        extract.assert_not_called()
        self.assertEqual(bill.ocr_state, "pending")
        self.assertEqual(bill.ocr_attachment_id, att)
        self.assertNotIn("There was an error while importing the bill", self._bodies(bill))

    def test_mail_alias_queues_the_bill(self):
        journal = self.env["account.journal"].search([
            ("company_id", "=", self.env.company.id), ("type", "=", "purchase")], limit=1)
        email = (
            "MIME-Version: 1.0\n"
            "Message-ID: <ocr-queue-test@example.com>\n"
            "Subject: Invoice\n"
            "From: Example Supplier <billing@example.com>\n"
            "To: bills@example.com\n"
            'Content-Type: multipart/mixed; boundary="BOUNDARY"\n\n'
            "--BOUNDARY\n"
            "Content-Type: text/plain\n\n"
            "Please find the invoice attached.\n"
            "--BOUNDARY\n"
            "Content-Type: application/pdf\n"
            "Content-Transfer-Encoding: base64\n"
            'Content-Disposition: attachment; filename="invoice.pdf"\n\n'
            "JVBERi0xLjQgdGVzdA==\n"
            "--BOUNDARY--\n")
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            bill_id = self.env["mail.thread"].message_process(
                "account.move", email,
                custom_values={"move_type": "in_invoice", "journal_id": journal.id})
        extract.assert_not_called()
        bill = self.env["account.move"].browse(bill_id)
        self.assertEqual(bill.move_type, "in_invoice")
        self.assertEqual(bill.ocr_state, "pending")
        self.assertEqual(bill.ocr_attachment_id.name, "invoice.pdf")

    def test_edi_import_and_disabled_ocr_are_not_queued(self):
        Move = type(self.env["account.move"])
        base = next(c for c in Move.__mro__ if c.__dict__.get("_extend_with_attachments")
                    and "account_invoice_ocr_ai" not in c.__module__)
        move = self._new_bill()
        att = self._attach_pdf(move)
        files = [{"name": att.name, "mimetype": att.mimetype, "raw": att.raw, "attachment": att}]
        with mock.patch.object(base, "_extend_with_attachments", return_value=True):
            self.assertTrue(move._extend_with_attachments(files, new=True))
        self.assertFalse(move.ocr_state, "an electronically imported bill is left alone (#18)")
        self._set_param("invoice_ocr.enabled", "False")
        with mock.patch.object(base, "_extend_with_attachments", return_value=None):
            self.assertFalse(move._extend_with_attachments(files, new=True))
        self.assertFalse(move.ocr_state)

    def test_list_action_queues_and_says_so(self):
        good, other = self._new_bill(), self._new_bill()
        self._attach_pdf(good)
        self._attach_pdf(other)
        no_pdf = self._new_bill()
        server_action = self.env.ref("account_invoice_ocr_ai.action_run_ocr_server")
        records = good | other | no_pdf
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            action = server_action.with_context(
                active_model="account.move", active_ids=records.ids, active_id=good.id).run()
        extract.assert_not_called()
        params = action["params"]
        self.assertEqual(params["type"], "success")
        self.assertTrue(params["message"].startswith(
            "2 queued for OCR, read in the background within a minute or so, 1 skipped"),
            params["message"])
        self.assertIn(f"{no_pdf.display_name}: no PDF attachment", params["message"])
        self.assertEqual((good | other).mapped("ocr_state"), ["pending", "pending"])
        self.assertFalse(no_pdf.ocr_state)
        with self._patch_ocr(fx.PLAIN_INVOICE_TEXT, AI):
            run_ocr_cron(self.env)
        self.assertEqual((good | other).mapped("ocr_state"), ["done", "done"])
        self.assertEqual(good.ref, "4711")

    # ---------------------------------------------------------------- the cron

    def test_cron_reads_the_queued_bill(self):
        move = self._queued_bill()
        with self._patch_ocr(fx.PLAIN_INVOICE_TEXT, AI):
            progress = run_ocr_cron(self.env)
        self.assertEqual(move.ocr_state, "done")
        self.assertFalse(move.ocr_error)
        self.assertEqual(move.ocr_attempts, 1)
        self.assertEqual(move.ref, "4711")
        self.assertEqual(move.partner_id.name, fx.PLAIN_VENDOR_NAME)
        self.assertTrue(move.invoice_line_ids)
        self.assertIn("OCR + AI filled in this bill", self._bodies(move))
        self.assertIn(mock.call(remaining=1), progress.call_args_list)
        self.assertIn(mock.call(1), progress.call_args_list)
        # nothing left: a second run reads nothing
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            run_ocr_cron(self.env)
        extract.assert_not_called()

    def test_cron_reads_the_pdf_it_was_queued_with(self):
        move = self._new_bill()
        uploaded = self._attach_pdf(move, "uploaded.pdf", b"%PDF-1.4 uploaded")
        self._upload(move, uploaded)
        self._attach_pdf(move, "later.pdf", b"%PDF-1.4 later")
        with mock.patch.object(invoice_ocr, "extract_invoice_data",
                               return_value=dict(DATA)) as extract:
            run_ocr_cron(self.env)
        self.assertEqual(extract.call_args.args[0], b"%PDF-1.4 uploaded")
        self.assertEqual(move.ocr_state, "done")

    def test_time_budget_stops_early_and_triggers_the_next_run(self):
        """A document is only started when its deadline still fits in the run's budget."""
        self._set_param("invoice_ocr.cron_time_budget", "100")
        self._set_param("invoice_ocr.total_deadline", "40")
        bills = self._queued_bill() | self._queued_bill() | self._queued_bill()
        clock = Clock()

        def extract(pdf, config=None, **kwargs):
            clock.now += 30  # each bill takes 30 s
            return dict(DATA)

        with mock.patch.object(ocr_queue, "_monotonic", clock), \
                mock.patch.object(invoice_ocr, "extract_invoice_data", side_effect=extract):
            triggers = self._triggers()
            progress = run_ocr_cron(self.env)
        # 0 s + 40 + 5 fits in 100, 30 s + 45 too, 60 s + 45 not: the third waits
        self.assertEqual(bills.mapped("ocr_state"), ["done", "done", "pending"])
        self.assertEqual(clock.now, 1060.0)
        self.assertIn(mock.call(remaining=3), progress.call_args_list)
        self.assertEqual(self._triggers(), triggers + 1, "the next run is triggered at once")
        # the next run reads the rest
        with mock.patch.object(ocr_queue, "_monotonic", clock), \
                mock.patch.object(invoice_ocr, "extract_invoice_data", side_effect=extract):
            run_ocr_cron(self.env)
        self.assertEqual(bills.mapped("ocr_state"), ["done", "done", "done"])

    def test_budget_counts_from_the_job_start(self):
        """The cron runner calls the action again while records are left: a later call in
        the same job starts nothing once the job's budget cannot hold another document."""
        self._set_param("invoice_ocr.cron_time_budget", "100")
        self._set_param("invoice_ocr.total_deadline", "40")
        move = self._queued_bill()
        clock = Clock()
        # the job started 60 s ago (cron_end_time is the start plus MIN_TIME_PER_JOB)
        ctx = {"cron_end_time": clock.now - 60 + ocr_queue.MIN_TIME_PER_JOB}
        with mock.patch.object(ocr_queue, "_monotonic", clock), \
                mock.patch.object(invoice_ocr, "extract_invoice_data") as extract, \
                mock.patch.object(self.env.registry["ir.cron"], "_commit_progress",
                                  return_value=0.0):
            self.env["ocr.queue.mixin"].with_context(ctx)._ocr_cron_process()
        extract.assert_not_called()
        self.assertEqual(move.ocr_state, "pending")

    def test_retry_limit_then_failed(self):
        move = self._queued_bill()
        failing = mock.patch.object(invoice_ocr, "extract_invoice_data",
                                    side_effect=RuntimeError("provider down"))
        with failing, mute_logger("odoo.addons.account_invoice_ocr_ai.models.account_move"):
            run_ocr_cron(self.env)
            self.assertEqual((move.ocr_state, move.ocr_attempts), ("pending", 1))
            self.assertIn("provider down", move.ocr_error)
            self.assertNotIn("OCR could not read this document", self._bodies(move))
            self.assertTrue(self._triggers(future=True), "the retry is scheduled")
            run_ocr_cron(self.env)
            self.assertEqual(move.ocr_attempts, 1, "not due yet: the retry waits")
            make_due(move)
            run_ocr_cron(self.env)
            self.assertEqual((move.ocr_state, move.ocr_attempts), ("pending", 2))
            make_due(move)
            run_ocr_cron(self.env)
        self.assertEqual((move.ocr_state, move.ocr_attempts), ("failed", 3))
        self.assertIn("provider down", move.ocr_error)
        self.assertIn("OCR could not read this document (attempts: 3)", self._bodies(move))

    def test_ai_failure_is_retried_then_the_text_is_used(self):
        self._set_param("invoice_ocr.max_attempts", "2")
        move = self._queued_bill()
        ai_down = mock.patch.object(invoice_ocr, "_extract_fields_ai",
                                    side_effect=RuntimeError("HTTP 503"))
        text = mock.patch.object(invoice_ocr, "extract_text", return_value=fx.PLAIN_INVOICE_TEXT)
        with ai_down, text:
            run_ocr_cron(self.env)
        self.assertEqual((move.ocr_state, move.ocr_attempts), ("pending", 1))
        self.assertFalse(move.ref, "nothing is written before the last attempt")
        self.assertIn("HTTP 503", move.ocr_error)
        make_due(move)
        with ai_down, text:
            run_ocr_cron(self.env)
        self.assertEqual(move.ocr_state, "failed")
        self.assertEqual(move.ref, "4711", "the last attempt fills in what the text gave")
        self.assertIn("the AI step failed", move.ocr_error)
        bodies = self._bodies(move)
        self.assertIn("the AI step failed (RuntimeError: HTTP 503)", bodies)
        self.assertNotIn("OCR could not read this document", bodies, "the fill note says it")

    def test_bills_no_longer_drafts_are_cleared(self):
        move = self._queued_bill()
        move.button_cancel()
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            run_ocr_cron(self.env)
        extract.assert_not_called()
        self.assertFalse(move.ocr_state)

    def test_changes_made_after_queueing_are_not_overwritten(self):
        """Someone filled in the bill before the cron got to it: OCR leaves it alone."""
        vendor = self.env["res.partner"].create({"name": "Chosen By Hand AB", "supplier_rank": 1})
        move = self._queued_bill()
        move.write({"partner_id": vendor.id, "ref": "HAND-1"})
        changed_by_someone_else(move)
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            run_ocr_cron(self.env)
        extract.assert_not_called()
        self.assertFalse(move.ocr_state)
        self.assertEqual((move.partner_id, move.ref), (vendor, "HAND-1"))
        self.assertIn("changed after it was queued for OCR", self._bodies(move))
        # the form button still reads it on request (filling only what is empty)
        with self._patch_ocr(fx.PLAIN_INVOICE_TEXT, AI):
            move.action_run_ocr()
        self.assertEqual((move.partner_id, move.ref), (vendor, "HAND-1"))
        self.assertEqual(move.ocr_state, "done")

    def test_interrupted_run_counts_as_an_attempt(self):
        move = self._queued_bill()
        move.write({"ocr_state": "running", "ocr_attempts": 1,
                    "ocr_requested_at": self.env.cr.now()})
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            run_ocr_cron(self.env)
        extract.assert_not_called()  # queued again, after the retry delay
        self.assertEqual((move.ocr_state, move.ocr_attempts), ("pending", 1))
        self.assertIn("stopped while reading it", move.ocr_error)
        move.write({"ocr_state": "running", "ocr_attempts": 3,
                    "ocr_requested_at": self.env.cr.now()})
        run_ocr_cron(self.env)
        self.assertEqual(move.ocr_state, "failed")
        self.assertIn("OCR could not read this document (attempts: 3): the background job "
                      "stopped while reading it", self._bodies(move))

    def test_limits_follow_odoo_time_limits(self):
        """The run's budget and the document deadline stay within Odoo's time limits."""
        Queue = self.env["ocr.queue.mixin"]
        options = ocr_queue.odoo_config.options
        with mock.patch.dict(options, {"limit_time_real": 120, "limit_time_real_cron": -1}):
            self.assertEqual(Queue._ocr_cron_time_budget(), 90, "3/4 of the default 120 s")
            self.assertEqual(Queue._ocr_document_deadline(), 80)
            self._set_param("invoice_ocr.total_deadline", "200")
            self.assertEqual(Queue._ocr_document_deadline(), 90, "never past 3/4 of the limit")
            self.assertEqual(self.env["account.move"]._invoice_ocr_config()["total_deadline"], 90)
        with mock.patch.dict(options, {"limit_time_real": 300, "limit_time_real_cron": 600}):
            self.assertEqual(Queue._ocr_cron_time_budget(), 450)
            self.assertEqual(Queue._ocr_document_deadline(), 200)
        with mock.patch.dict(options, {"limit_time_real": 0, "limit_time_real_cron": 0}):
            self.assertEqual(Queue._ocr_cron_time_budget(), ocr_queue.NO_LIMIT_BUDGET)
            self._set_param("invoice_ocr.cron_time_budget", "60")
            self.assertEqual(Queue._ocr_cron_time_budget(), 60)

    # ---------------------------------------------------------------- the form button

    def test_form_button_reads_at_once(self):
        move = self._queued_bill()
        with self._patch_ocr(fx.PLAIN_INVOICE_TEXT, AI):
            params = move.action_run_ocr()["params"]
        self.assertEqual(params["message"], "1 filled")
        self.assertEqual((move.ocr_state, move.ref), ("done", "4711"))
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            run_ocr_cron(self.env)
        extract.assert_not_called()

    def test_form_button_skips_a_bill_being_read(self):
        move = self._queued_bill()
        move.ocr_state = "running"
        with mock.patch.object(invoice_ocr, "extract_invoice_data") as extract:
            message = move.action_run_ocr()["params"]["message"]
        extract.assert_not_called()
        self.assertIn("OCR is already reading it in the background", message)

    def test_form_button_failure_is_visible(self):
        move = self._new_bill()
        self._attach_pdf(move)
        with mock.patch.object(invoice_ocr, "extract_invoice_data", return_value={}):
            params = move.action_run_ocr()["params"]
        self.assertEqual(params["type"], "warning")
        self.assertEqual(move.ocr_state, "failed")
        self.assertIn("neither a vendor name nor an invoice number", move.ocr_error)

    def test_budget_cut_is_named_in_the_failure(self):
        """Nothing found because a budget cut the reading (#26): the reason says so."""
        move = self._new_bill()
        self._attach_pdf(move)
        cut = ("reading the document text stopped after 0 of 3 pages: the time for reading "
               "it (30 s) was used up – the rest was not read")
        with mock.patch.object(invoice_ocr, "extract_invoice_data", return_value={"_notes": [cut]}):
            move.action_run_ocr()
        self.assertEqual(move.ocr_state, "failed")
        self.assertIn("neither a vendor name nor an invoice number", move.ocr_error)
        self.assertIn("stopped after 0 of 3 pages", move.ocr_error)
        self.assertIn("stopped after 0 of 3 pages", self._bodies(move))
