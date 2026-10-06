"""OCR in the background (#9, #29): a queue on the document and one cron that reads it.

The upload button, the mail alias, the list actions and the receipt trigger only queue a
document (ocr_state "pending") and wake the cron, so the request that triggered them
returns at once; the cron reads the queued documents within seconds. The form buttons
still read at once.

The cron (ir_cron_ocr_queue) serves every model that inherits ocr.queue.mixin — vendor
bills here, expenses in hr_expense_ocr_ai — oldest first, within one time budget per
run. Odoo runs all ready cron jobs of a database in one worker pass under one time limit
(limit_time_real_cron, by default limit_time_real = 120 s), so one cron with one budget
is safer than one per model.

State machine of a document:

    (empty) --queue--> pending --cron claims--> running --read--> done
                          ^                        |      \\
                          |   failed, attempts left |       --> failed (last attempt)
                          +------------------------+
    pending/running --> (empty)  no longer readable (not a draft) or changed by someone
                                 after it was queued (a note says so)

The claim is committed before the document is read, so an attempt that kills the worker
(time or memory limit) still counts, and a document left "running" is picked up by the
next run as an interrupted attempt.

The job reads a document as the user who queued it (ocr_requested_by), with that user's
access rights, language and company, and its notes have that user as author; only the
queue's own bookkeeping (claim, state, attempts) is done with superuser rights. See
_ocr_queue_reader.
"""

import logging
import time
from datetime import timedelta

from odoo import _, api, fields, models, modules
from odoo.exceptions import UserError
from odoo.tools import config as odoo_config
from odoo.tools.translate import get_translation

try:  # the cron runner's minimum loop time; the job's start is cron_end_time minus it
    from odoo.addons.base.models.ir_cron import MIN_TIME_PER_JOB
except ImportError:  # pragma: no cover
    MIN_TIME_PER_JOB = 10

logger = logging.getLogger(__name__)

# Attempts per document before it is marked failed (system parameter
# invoice_ocr.max_attempts).
MAX_ATTEMPTS = 3
# Seconds before the 2nd and the 3rd attempt (and any later one).
RETRY_DELAYS = (60, 300)
# Seconds the writes to the record may take after the library is done with a document.
ODOO_WORK_MARGIN = 5
# Seconds a run may spend before its first document (finding the queue, the claim): a run
# always has room for at least one document, also with a budget below its deadline.
STARTUP_SLACK = 5
# A run's time budget when Odoo has no time limit for cron jobs.
NO_LIMIT_BUDGET = 300
# Share of Odoo's time limits a run, or one document, may use.
LIMIT_SHARE = 0.75


def _monotonic():
    """The cron's clock (a seam for tests)."""
    return time.monotonic()


class OcrQueueMixin(models.AbstractModel):
    _name = "ocr.queue.mixin"
    _description = "OCR read in the background"

    ocr_state = fields.Selection(
        [("pending", "Queued"), ("running", "Reading"), ("done", "Read"), ("failed", "Failed")],
        string="OCR", copy=False, readonly=True, index="btree_not_null",
        help="Queued: OCR reads the document in the background, within a minute or so. "
             "Reading: it is being read. Read: OCR filled it in. Failed: OCR could not read "
             "it (see the error and the chatter). Empty: never queued.",
    )
    ocr_error = fields.Char(string="OCR error", copy=False, readonly=True)
    ocr_attempts = fields.Integer(string="OCR attempts", copy=False, readonly=True)
    ocr_requested_at = fields.Datetime(
        string="OCR queued at", copy=False, readonly=True,
        help="When the document was queued for OCR, or last taken up by it. A change to "
             "the document after this moment means someone else worked on it: OCR then "
             "leaves it alone.",
    )
    ocr_attachment_id = fields.Many2one(
        "ir.attachment", string="OCR document", copy=False, readonly=True, ondelete="set null",
        help="The attachment OCR reads: the uploaded file, or the one chosen when OCR was "
             "requested.",
    )
    ocr_requested_by = fields.Many2one(
        "res.users", string="OCR requested by", copy=False, readonly=True, ondelete="set null",
        help="The user who queued the document for OCR. The background job reads it as this "
             "user: with their access rights (a vendor is only created by a user who may create "
             "contacts), in their language, and their name is on the notes. A document that "
             "came in by e-mail is read as its sender when the sender is a user; otherwise "
             "this is empty and the job reads it as OdooBot.",
    )

    # ------------------------------------------------------------------
    # Hooks for the models (account.move, hr.expense)
    # ------------------------------------------------------------------

    def _ocr_queue_skip_reason(self):
        """Why this document can no longer be read (not a draft …), or None."""
        return None

    def _ocr_queue_default_user(self):
        """The user to read this document as when no person queued it (the mail gateway runs
        as OdooBot): e.g. the sender of the e-mail, when that is a user. None by default."""
        return self.env["res.users"]

    def _ocr_queue_read(self, final=True):
        """Read this queued document and fill it in; returns the outcome (see _ocr_result).

        A failure that may pass (the AI step failed) is a "failed" outcome with retry=True,
        returned before anything is written, unless `final` (the last attempt). Exceptions
        are left to the caller.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Outcomes
    # ------------------------------------------------------------------

    @staticmethod
    def _ocr_result(status, reason=None, retry=False, noted=False):
        """Outcome of one OCR run: status "filled", "skipped", "failed" or "queued", and why.

        `retry`: a failure that may pass (the AI step failed, an error) — the cron tries
        again. `noted`: the chatter already says what went wrong.
        """
        return {"status": status, "reason": reason or "", "retry": retry, "noted": noted}

    @api.model
    def _ocr_error_reason(self, error):
        """The readable text of an exception, in the user's language: a UserError's message
        as it is, a library error's note translated (_ocr_note_text)."""
        from ..lib import invoice_ocr

        if error.args and isinstance(error.args[0], invoice_ocr.Note):
            return self._ocr_note_text(error.args[0])[:300]
        if isinstance(error, UserError) and error.args:
            return str(error.args[0])[:300]
        return str(error)[:300] or type(error).__name__

    # ------------------------------------------------------------------
    # The libraries' notes, in the user's language (#35.3, #36.12)
    # ------------------------------------------------------------------

    @api.model
    def _ocr_note_text(self, note):
        """`note` in the user's language (self.env.lang).

        The OCR libraries cannot import Odoo, so what they tell the reviewer is an
        invoice_ocr.Note: a str with the English text that also keeps its source text
        (msgid), its parameters and the module whose .po files translate it (the source
        texts are marked with _() in the libraries, so they are in the modules' .pot). It
        is translated here like any _() string of that module; a parameter that is a Note
        itself is translated first. Anything else (plain text, None) is returned as text.
        """
        from ..lib import invoice_ocr

        if not isinstance(note, invoice_ocr.Note):
            return "" if note is None else str(note)
        params = {key: self._ocr_note_text(value) if isinstance(value, invoice_ocr.Note)
                  else value for key, value in note.params.items()}
        return get_translation(note.addon, self.env.lang or "en_US", note.msgid, params)

    @api.model
    def _ocr_notes_text(self, notes):
        """_ocr_note_text of every note in `notes`."""
        return [self._ocr_note_text(note) for note in notes or ()]

    # ------------------------------------------------------------------
    # Limits
    # ------------------------------------------------------------------

    @api.model
    def _ocr_odoo_time_limit(self, cron=False):
        """Odoo's real-time limit in seconds for a request, or for a cron pass; 0 = none.

        limit_time_real_cron = -1 (the default) means limit_time_real (120 s by default).
        """
        limit = odoo_config.get("limit_time_real") or 0
        if cron:
            cron_limit = odoo_config.get("limit_time_real_cron")
            if cron_limit is not None and cron_limit >= 0:
                limit = cron_limit
        return max(int(limit or 0), 0)

    @api.model
    def _ocr_param_number(self, key):
        from ..lib import invoice_ocr

        return invoice_ocr._positive_number(
            self.env["ir.config_parameter"].sudo().get_param(key))

    @api.model
    def _ocr_document_deadline(self):
        """Seconds one document may take: the setting (invoice_ocr.total_deadline, default
        80), never more than three quarters of Odoo's request and cron time limits, so the
        form button and the cron stay within them."""
        from ..lib import invoice_ocr

        deadline = self._ocr_param_number("invoice_ocr.total_deadline") or invoice_ocr.TOTAL_DEADLINE
        limits = [limit for limit in (self._ocr_odoo_time_limit(),
                                      self._ocr_odoo_time_limit(cron=True)) if limit]
        if limits:
            deadline = min(deadline, LIMIT_SHARE * min(limits))
        return deadline

    @api.model
    def _ocr_cron_time_budget(self):
        """Seconds one cron run may spend reading documents: the setting
        (invoice_ocr.cron_time_budget), else three quarters of Odoo's cron time limit (90 s
        with Odoo's defaults), else NO_LIMIT_BUDGET when there is no limit."""
        budget = self._ocr_param_number("invoice_ocr.cron_time_budget")
        if budget:
            return budget
        limit = self._ocr_odoo_time_limit(cron=True)
        return LIMIT_SHARE * limit if limit else NO_LIMIT_BUDGET

    @api.model
    def _ocr_max_attempts(self):
        value = self._ocr_param_number("invoice_ocr.max_attempts")
        return int(value) if value else MAX_ATTEMPTS

    # ------------------------------------------------------------------
    # Queueing
    # ------------------------------------------------------------------

    def _ocr_enqueue(self, attachment=None):
        """Queue these documents for the background OCR (the cron is not woken here, see
        _ocr_queue_trigger). `attachment`: the file to read, if known.

        The current user is stored as the one the job reads the document as (#9, #29), unless it
        is no person (OdooBot, the superuser, a portal or public user): then the model's
        _ocr_queue_default_user, e.g. the sender of an e-mailed document.
        """
        if not self:
            return
        requester = self._ocr_real_user(self.env.user)
        for record in self:
            user = requester or record._ocr_queue_default_user()
            record.sudo().write({
                "ocr_state": "pending",
                "ocr_error": False,
                "ocr_attempts": 0,
                # The transaction's time: writes in this same transaction have exactly this
                # write_date, so only a later change by someone else is newer (see
                # _ocr_queue_changed).
                "ocr_requested_at": self.env.cr.now(),
                "ocr_attachment_id": attachment.id if attachment else False,
                "ocr_requested_by": user.id or False,
            })

    @api.model
    def _ocr_real_user(self, user):
        """`user` when it is a person who can work in the back end: active, internal, not
        OdooBot or the superuser; else an empty recordset."""
        user = user.sudo()[:1]
        if user and user.active and not user.share and not user._is_superuser():
            return user
        return self.env["res.users"]

    def _ocr_queue_company(self):
        return self.company_id if "company_id" in self._fields else self.env.company

    def _ocr_queue_lang(self):
        """The language of the notes on a document no person queued: the company's."""
        return self._ocr_queue_company().sudo().partner_id.lang or self.env.lang or "en_US"

    def _ocr_queue_reader(self):
        """(this document as the job reads it, why it cannot be read or None).

        Read as the user who queued it (ocr_requested_by; #9, #29): with that user's access
        rights, in that user's language, with the document's company as the allowed company
        (the user must still have it); the notes then have that user as author. A user who
        is archived, lost the company or can no longer change the document gives a reason
        instead: the job does not fall back to OdooBot's rights. A document no person
        queued is read as the job's own user, in the company's language.
        """
        self.ensure_one()
        user = self.ocr_requested_by.sudo()
        if not user:
            return self.with_context(lang=self._ocr_queue_lang()), None
        lang = user.lang or self._ocr_queue_lang()
        plain = self.with_context(lang=lang)
        company = self._ocr_queue_company().sudo()
        if not user.active:
            return plain, plain.env._("the user who queued it, %s, is archived", user.name)
        if company not in user.company_ids:
            return plain, plain.env._("%(user)s, who queued it, no longer has access to the "
                                      "company %(company)s", user=user.name, company=company.name)
        reader = self.with_user(user).with_context(lang=lang, allowed_company_ids=[company.id])
        if not reader.has_access("write"):
            return plain, plain.env._("%s, who queued it, may no longer change it", user.name)
        return reader, None

    @api.model
    def _ocr_queue_cron(self):
        return self.env.ref("account_invoice_ocr_ai.ir_cron_ocr_queue",
                            raise_if_not_found=False)

    @api.model
    def _ocr_queue_trigger(self, at=None):
        """Wake the cron now (after this transaction commits), or at `at`."""
        cron = self._ocr_queue_cron()
        if not cron:
            logger.warning("OCR: the cron ir_cron_ocr_queue is missing, queued documents "
                           "are not read")
            return
        cron = cron.sudo()
        if at is not None and self.env["ir.cron.trigger"].sudo().search_count(
                [("cron_id", "=", cron.id), ("call_at", "=", at)], limit=1):
            return
        cron._trigger(at)

    def _ocr_queue_record_sync(self, outcome):
        """The state after a read on request (the form button): read or failed."""
        if outcome["status"] == "filled":
            self.sudo().write({"ocr_state": "done", "ocr_error": False})
        elif outcome["status"] == "failed":
            self.sudo().write({"ocr_state": "failed", "ocr_error": outcome["reason"][:300]})

    # ------------------------------------------------------------------
    # The cron
    # ------------------------------------------------------------------

    @api.model
    def _ocr_queue_models(self):
        return [self.env[name].sudo()
                for name in self.env.registry.descendants(["ocr.queue.mixin"], "_inherit")
                if not self.env[name]._abstract]

    @api.model
    def _ocr_retry_delay(self, attempts):
        """Seconds to wait after `attempts` failed attempts."""
        if attempts <= 0:
            return 0
        return RETRY_DELAYS[min(attempts, len(RETRY_DELAYS)) - 1]

    def _ocr_due_at(self):
        return self.ocr_requested_at + timedelta(seconds=self._ocr_retry_delay(self.ocr_attempts))

    @api.model
    def _ocr_queue_due(self):
        """The queued documents of every model whose turn it is, oldest first."""
        now = self.env.cr.now()
        due = []
        for Model in self._ocr_queue_models():
            for record in Model.search([("ocr_state", "=", "pending")]):
                if not record.ocr_requested_at or record._ocr_due_at() <= now:
                    due.append(record)
        return sorted(due, key=lambda r: (r.ocr_requested_at or now, r._name, r.id))

    @api.model
    def _ocr_cron_started(self):
        """When this cron job started (monotonic): the runner calls the job's action again
        as long as it reports records left, so the budget must count from the job's start,
        not from this call. Outside the cron: now."""
        end = self.env.context.get("cron_end_time")
        return end - MIN_TIME_PER_JOB if end else _monotonic()

    @api.model
    def _ocr_cron_process(self):
        """The cron: read the queued bills and receipts, oldest first.

        * Own time budget (_ocr_cron_time_budget): ir.cron's _commit_progress commits and
          reports progress but is no time budget, so a document is only started when its
          whole deadline (_ocr_document_deadline) still fits in what is left of the run.
          The budget always holds one document (deadline + margins), so with Odoo's
          defaults (90 s budget, 80 s deadline) a run reads one document, or a few fast
          ones, and stays within 90 s. The rest stays queued and the cron is triggered
          again at once.
        * One document at a time: claimed (committed), read in a savepoint, settled and
          committed (_ocr_queue_process_one).
        * Retries: a failed attempt is queued again after RETRY_DELAYS and the cron is
          triggered for that moment; after max attempts the document is failed, with a note.
        """
        IrCron = self.env["ir.cron"]
        started = self._ocr_cron_started()
        deadline = self._ocr_document_deadline()
        budget = max(self._ocr_cron_time_budget(), deadline + ODOO_WORK_MARGIN + STARTUP_SLACK)
        run_end = started + budget
        max_attempts = self._ocr_max_attempts()
        self._ocr_queue_recover(max_attempts)
        queue = self._ocr_queue_due()
        IrCron._commit_progress(remaining=len(queue))
        processed = 0
        for record in queue:
            if _monotonic() + deadline + ODOO_WORK_MARGIN > run_end:
                logger.info("OCR: time budget of the run (%.0f s) spent, %s documents left "
                            "for the next run", budget, len(queue) - processed)
                break
            record._ocr_queue_process_one(max_attempts)
            processed += 1
            IrCron._commit_progress(1)
        if processed < len(queue):
            self._ocr_queue_trigger()
        self._ocr_queue_schedule_retry()

    @api.model
    def _ocr_queue_schedule_retry(self):
        """Wake the cron when the next queued retry is due."""
        now = self.env.cr.now()
        upcoming = [record._ocr_due_at()
                    for Model in self._ocr_queue_models()
                    for record in Model.search([("ocr_state", "=", "pending"),
                                                ("ocr_attempts", ">", 0)])
                    if record.ocr_requested_at]
        upcoming = [at for at in upcoming if at > now]
        if upcoming:
            # Rounded up to the second: the run must not come a moment before it is due.
            self._ocr_queue_trigger(
                at=(min(upcoming) + timedelta(seconds=1)).replace(microsecond=0))

    @api.model
    def _ocr_queue_recover(self, max_attempts):
        """Documents left "running" by a run that was killed (time or memory limit): the
        attempt counts; they are queued again, or failed after the last attempt."""
        for Model in self._ocr_queue_models():
            for record in Model.search([("ocr_state", "=", "running")]):
                record = record.try_lock_for_update(allow_referencing=True)
                if not record:
                    continue
                record.invalidate_recordset()  # the values as committed, not as cached
                keeper = record._ocr_queue_reader()[0].sudo()
                if keeper.ocr_state != "running" or keeper._ocr_queue_drop_if_stale():
                    continue
                logger.warning("OCR: %s was left running by an interrupted run", record)
                reason = keeper.env._("the background job stopped while reading it (Odoo's "
                                      "time or memory limit?)")
                keeper._ocr_queue_settle(self._ocr_result("failed", reason, retry=True),
                                         keeper.ocr_attempts,
                                         keeper.ocr_attempts >= max_attempts)

    def _ocr_queue_changed(self):
        """True when someone changed the document after it was queued (or taken up)."""
        self.ensure_one()
        return bool(self.write_date and self.ocr_requested_at
                    and self.write_date > self.ocr_requested_at)

    def _ocr_queue_drop_if_stale(self):
        """Take this document off the queue when it can no longer be read (not a draft) or
        was changed by someone after it was queued — then OCR does not overwrite their work
        and a note says so. True when dropped."""
        self.ensure_one()
        if self._ocr_queue_skip_reason():
            self.write({"ocr_state": False, "ocr_error": False})
            return True
        if self._ocr_queue_changed():
            self.message_post(
                body=_("This document was changed after it was queued for OCR, so OCR did not "
                       "read it and nothing entered by hand was overwritten. Use the OCR "
                       "button on the form to read it anyway."),
                message_type="comment", subtype_xmlid="mail.mt_note")
            self.write({"ocr_state": False, "ocr_error": False})
            return True
        return False

    def _ocr_queue_rollback(self):
        """After a failed read: a fresh transaction (outside tests), so the failure can be
        recorded even when the read failed on a concurrent update of the document."""
        if not modules.module.current_test:
            self.env.cr.rollback()

    def _ocr_queue_process_one(self, max_attempts):
        """Claim, read and settle one queued document (see _ocr_cron_process).

        The document is read as the user who queued it (_ocr_queue_reader); the claim and
        the settling are done with superuser rights as that user (`keeper`), so the notes
        are theirs and in their language. A document its user can no longer read is failed
        with the reason, without being read.
        """
        self.ensure_one()
        record = self.try_lock_for_update(allow_referencing=True)
        if not record:
            return  # someone is saving it right now; the next run takes it
        # The values as committed (the form button may have read it meanwhile), not as
        # cached when the queue was listed.
        record.invalidate_recordset()
        reader, problem = record._ocr_queue_reader()
        keeper = reader.sudo()
        if keeper.ocr_state != "pending" or keeper._ocr_queue_drop_if_stale():
            return
        attempt = keeper.ocr_attempts + 1
        keeper.write({"ocr_state": "running", "ocr_attempts": attempt,
                      "ocr_requested_at": self.env.cr.now()})
        # Committed before reading: an attempt that kills the worker still counts.
        self.env["ir.cron"]._commit_progress(0)
        final = attempt >= max_attempts
        if problem:
            logger.warning("OCR: %s is not read: %s", record, problem)
            keeper._ocr_queue_settle(self._ocr_result("failed", problem), attempt, True)
            return
        try:
            with self.env.cr.savepoint():
                outcome = reader._ocr_queue_read(final=final)
            with self.env.cr.savepoint():
                keeper._ocr_queue_settle(outcome, attempt, final)
        except Exception as e:  # noqa: BLE001 — one document must never stop the queue
            logger.warning("OCR failed for %s (attempt %s)", record, attempt, exc_info=True)
            keeper._ocr_queue_rollback()
            keeper.invalidate_recordset()
            outcome = self._ocr_result("failed", keeper._ocr_error_reason(e), retry=True)
            keeper._ocr_queue_settle(outcome, attempt, final)

    def _ocr_queue_settle(self, outcome, attempts, final):
        """Record the outcome of an attempt: read, queued again, failed or off the queue."""
        self.ensure_one()
        status, reason = outcome["status"], outcome["reason"]
        if status == "filled":
            self.write({"ocr_state": "done", "ocr_error": False})
        elif status == "skipped":
            self.message_post(body=_("OCR did not read this document: %s.", reason),
                              message_type="comment", subtype_xmlid="mail.mt_note")
            self.write({"ocr_state": False, "ocr_error": False})
        elif outcome.get("retry") and not final:
            # Someone saved the document while it was being read (the read then failed on
            # their change): leave it to them.
            if self._ocr_queue_drop_if_stale():
                return
            self.write({"ocr_state": "pending", "ocr_error": reason[:300],
                        "ocr_requested_at": self.env.cr.now()})
        else:
            if not outcome.get("noted"):
                self.message_post(
                    body=_("OCR could not read this document (attempts: %(attempts)s): "
                           "%(reason)s. Fill it in by hand, or use the OCR button on the form "
                           "to try again.", attempts=attempts, reason=reason),
                    message_type="comment", subtype_xmlid="mail.mt_note")
            self.write({"ocr_state": "failed", "ocr_error": reason[:300]})
