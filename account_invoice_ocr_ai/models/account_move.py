"""Read vendor bill PDFs with OCR + AI and pre-fill the bill (invoice_date, ref, partner,
amounts, lines).

A bill created from a PDF — the journal's Upload button, the mail alias — is queued in
account.move._extend_with_attachments and read by the OCR cron within seconds (see
ocr_queue.py, #9); so is a bill the list action "Kör OCR igen" is run on. The form button
reads the bill at once. A PDF attached later to an existing bill (the chatter, a reply to
it) is not read automatically (#35.2): the form button reads it on request.
"""

import base64
import logging
import re
from collections import Counter
from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models

logger = logging.getLogger(__name__)


class AccountMove(models.Model):
    _name = "account.move"
    _inherit = ["account.move", "ocr.queue.mixin"]

    ocr_auto_debit = fields.Boolean(
        string="Dras automatiskt",
        copy=False,
        tracking=True,
        help="Fakturan dras automatiskt från bolagets konto (autogiro, bankavgift, "
             "direct debit) och ska INTE betalas manuellt eller tas med i en betalfil. "
             "Sätts av OCR-tolkningen när underlaget säger det; kan ändras för hand.",
    )
    ocr_auto_debit_phrase = fields.Char(
        string="Dragning enligt OCR",
        copy=False,
        readonly=True,
        help="Frasen i underlaget som fick OCR:en att sätta 'Dras automatiskt'. Tom när "
             "flaggan satts eller ändrats för hand – då rör en omkörning av OCR:en den inte.",
    )

    def write(self, vals):
        # Ändras flaggan för hand äger användaren den: glöm OCR-frasen så att en
        # omkörning inte nollställer ett manuellt val.
        if "ocr_auto_debit" in vals and not self.env.context.get("ocr_auto_debit_write"):
            vals = dict(vals, ocr_auto_debit_phrase=False)
        return super().write(vals)

    def action_run_ocr(self):
        """Read the latest PDF attachment of each draft vendor bill now: the form button.

        Synchronous (bounded by the document deadline, see _ocr_document_deadline), so the
        user sees the result at once. Each bill runs in its own savepoint
        (_invoice_ocr_extend_safe), so one failure neither stops nor undoes the others; a
        failed bill gets a chatter note and the OCR state "failed". A bill the background
        job is reading is skipped. Returns a notification that says how many bills were
        filled, failed or skipped, and why. The list action queues instead
        (action_queue_ocr).
        """
        results = []
        for move in self:
            if move.state != "draft" or move.move_type != "in_invoice":
                results.append((move, self._ocr_result("skipped", _("not a draft vendor bill"))))
                continue
            if move.ocr_state == "running":
                results.append((move, self._ocr_result(
                    "skipped", _("OCR is already reading it in the background"))))
                continue
            att = move._ocr_newest_pdf()
            if not att:
                results.append((move, self._ocr_result("skipped", _("no PDF attachment"))))
                continue
            result = self._invoice_ocr_extend_safe(move, self._ocr_files_data(att))
            move._ocr_queue_record_sync(result)
            results.append((move, result))
        return self._ocr_notification(_("Invoice OCR"), results)

    def action_queue_ocr(self):
        """Queue the selected draft vendor bills for OCR: the list action "Kör OCR igen".

        Nothing is read in the request (a worker would hit Odoo's time limit after a few
        bills, #9): the OCR cron reads the bills within seconds, one by one, and the outcome
        shows in the OCR state, the "OCR failed" filter and the chatter. Returns a
        notification with how many bills were queued or skipped, and why.
        """
        results = []
        queued = self.browse()
        enabled = self._invoice_ocr_enabled()
        for move in self:
            if move.state != "draft" or move.move_type != "in_invoice":
                results.append((move, self._ocr_result("skipped", _("not a draft vendor bill"))))
                continue
            if not enabled:
                results.append((move, self._ocr_result(
                    "skipped", _("OCR is turned off in the settings"))))
                continue
            if move.ocr_state == "running":
                results.append((move, self._ocr_result(
                    "skipped", _("OCR is already reading it in the background"))))
                continue
            att = move._ocr_newest_pdf()
            if not att:
                results.append((move, self._ocr_result("skipped", _("no PDF attachment"))))
                continue
            move._ocr_enqueue(att)
            queued |= move
            results.append((move, self._ocr_result("queued")))
        if queued:
            self._ocr_queue_trigger()
        return self._ocr_notification(_("Invoice OCR"), results)

    def _ocr_newest_pdf(self):
        """The bill's most recent PDF attachment."""
        self.ensure_one()
        return self.env["ir.attachment"].search([
            ("res_model", "=", "account.move"),
            ("res_id", "=", self.id),
            ("mimetype", "=", "application/pdf"),
        ], order="id desc", limit=1)

    def _ocr_queue_pdf(self):
        """The PDF the queued read takes: the one it was queued with, if it is still the
        bill's, else the bill's main attachment (the uploaded or e-mailed PDF, as core sets
        it), else its most recent PDF."""
        self.ensure_one()
        for att in (self.ocr_attachment_id, self.message_main_attachment_id):
            if (att and att.res_model == "account.move" and att.res_id == self.id
                    and att.mimetype == "application/pdf"):
                return att
        return self._ocr_newest_pdf()

    @api.model
    def _ocr_files_data(self, att):
        return [{"name": att.name, "filename": att.name, "mimetype": att.mimetype,
                 "raw": att.raw, "attachment": att}]

    # The queue (ocr.queue.mixin)

    def _ocr_queue_skip_reason(self):
        if self.state != "draft" or self.move_type != "in_invoice":
            return _("not a draft vendor bill")
        return None

    def _ocr_queue_read(self, final=True):
        att = self._ocr_queue_pdf()
        if not att:
            return self._ocr_result("skipped", _("no PDF attachment"))
        return self._invoice_ocr_extend(self, self._ocr_files_data(att), final=final)

    @api.model
    def _ocr_notification(self, title, results):
        """A display_notification summarising [(record, outcome)] (see _ocr_result).

        Shared with hr_expense_ocr_ai. Failed and skipped records are listed with the
        reason; the current view is reloaded afterwards so filled values and the OCR state
        show.
        """
        counts = Counter(outcome["status"] for _rec, outcome in results)
        parts = [label for label in (
            counts["filled"] and _("%s filled", counts["filled"]),
            counts["queued"] and _("%s queued for OCR, read in the background within a "
                                   "minute or so", counts["queued"]),
            counts["failed"] and _("%s failed", counts["failed"]),
            counts["skipped"] and _("%s skipped", counts["skipped"]),
        ) if label] or [_("nothing selected")]
        details = [f"{rec.display_name}: {outcome['reason']}" for rec, outcome in results
                   if outcome["status"] not in ("filled", "queued") and outcome["reason"]]
        shown = 10
        if len(details) > shown:
            details = [*details[:shown], _("… and %s more", len(details) - shown)]
        # The notification is plain text (no line breaks): one sentence per record.
        message = ", ".join(parts) + (". " + "; ".join(details) if details else "")
        if counts["failed"]:
            kind = "warning"
        elif counts["filled"] or counts["queued"]:
            kind = "success"
        else:
            kind = "info"
        return {
            "type": "ir.actions.client",
            "tag": "display_notification",
            "params": {
                "title": title,
                "message": message,
                "type": kind,
                "sticky": bool(counts["failed"]),
                "next": {"type": "ir.actions.client", "tag": "soft_reload"},
            },
        }

    def _extend_with_attachments(self, files_data, new=False):
        """Queue a new draft vendor bill created from a PDF for OCR (#9).

        Called at creation (new=True) by the journal's Upload button and the mail alias.
        A file attached to an existing bill (the chatter's attachment box, a message posted
        on it: new=False) is not read, so a supporting document never overwrites a bill
        someone filled in; the form button reads it on request (#35.2). Nothing is read here: the request (or the mail fetch) returns at once and the OCR
        cron reads the bill within seconds. A bill Odoo already imported electronically
        (UBL/Peppol, embedded Factur-X/ZUGFeRD: super() returns a truthy value) is left
        alone (#18), as is a bill when OCR is off.

        Returns True for a queued bill. Core's only use of the value on this path
        (_create_records_from_attachments) is to post "There was an error while importing
        the bill, you can find attached the incoming XML" when it is falsy — wrong for a PDF
        that is about to be read; the bill's OCR state and chatter tell the real outcome.
        Bills that are not queued return core's value unchanged.
        """
        res = super()._extend_with_attachments(files_data, new)
        if not new or res:
            return res
        queued = False
        for move in self:
            if move.move_type != "in_invoice" or move.state != "draft":
                continue
            if not self._invoice_ocr_enabled():
                continue
            pdfs = [fd for fd in files_data
                    if fd.get("mimetype") == "application/pdf"
                    or (fd.get("name") or fd.get("filename") or "").lower().endswith(".pdf")]
            if not pdfs:
                continue
            attachment = next((fd["attachment"] for fd in pdfs if fd.get("attachment")), None)
            move._ocr_enqueue(attachment)
            queued = True
        if queued:
            self._ocr_queue_trigger()
        return True if queued else res

    # ------------------------------------------------------------------
    # OCR + AI fill
    # ------------------------------------------------------------------

    @api.model
    def _invoice_ocr_config(self, company=None):
        """Per-run config for the OCR library from Odoo's settings and the receiving company.

        Shared with hr_expense_ocr_ai. System parameters win over environment defaults; the
        receiving company's identities — org/VAT numbers and names, partners and bank
        accounts of the company and its branches (_ocr_own_context) — go along as own_*
        keys, so they are never taken for the supplier.

        Returns a dict for invoice_ocr.extract_invoice_data / chat_json. It does NOT mutate
        the library's module globals: they are shared by every run in the worker process, so
        concurrent runs (bulk server action, multi-company users, the settings page's Verify
        button) would otherwise read another company's VAT or another provider's key, and a
        key cleared in the settings would keep working until the next restart.
        """
        from ..lib import invoice_ocr

        ICP = self.env["ir.config_parameter"].sudo()
        cfg = invoice_ocr.config_from_settings(lambda key: ICP.get_param(f"invoice_ocr.{key}"))
        # One document's time, within Odoo's time limits (#9).
        cfg["total_deadline"] = self.env["ocr.queue.mixin"]._ocr_document_deadline()
        # Org/VAT numbers are normalized inside the library (invoice_ocr.build_own_ids),
        # so no need to pre-clean here.
        own = self._ocr_own_context(company or self.env.company)
        cfg.update({f"own_{key}": value for key, value in own.items()})
        return cfg

    @api.model
    def _ocr_own_from_config(self, cfg):
        """The own-company context (see _ocr_own_context) carried by a per-run config."""
        return {
            "ids": cfg.get("own_ids") or [],
            "names": cfg.get("own_names") or [],
            "partner_ids": cfg.get("own_partner_ids") or [],
            "bank_keys": cfg.get("own_bank_keys") or set(),
            "account_keys": cfg.get("own_account_keys") or set(),
        }

    @api.model
    def _ocr_currency(self, code, company, date=None):
        """The currency to use for an extracted currency code: (currency, problem).

        Shared with hr_expense_ocr_ai (#5, #28). `currency` is the res.currency when it can be
        used — the company's currency, or another active currency with an exchange rate on
        or before `date` (rates of the company's root or shared ones, as Odoo converts with)
        — else an empty recordset; `problem` says why not, or is None. No code at all ('kr',
        nothing read) gives (empty, None): nothing to change. Inactive currencies are found
        too, so the message can say what to do.
        """
        from ..lib import invoice_ocr

        Currency = self.env["res.currency"].with_context(active_test=False)
        iso = invoice_ocr.normalize_currency(code)
        if not iso:
            return Currency, None
        if iso == company.currency_id.name:
            return company.currency_id, None
        currency = Currency.search([("name", "=", iso)], limit=1)
        if not currency:
            return Currency, _("the currency %s is not known in Odoo", iso)
        if not currency.active:
            return Currency, _("the currency %s is not active in Odoo", iso)
        date = fields.Date.to_date(date) or fields.Date.context_today(self)
        if not self.env["res.currency.rate"].sudo().search_count([
                ("currency_id", "=", currency.id), ("name", "<=", date),
                ("company_id", "in", [False, company.root_id.id])], limit=1):
            return Currency, _("the currency %(currency)s has no exchange rate on or before "
                               "%(date)s", currency=iso, date=date)
        return currency, None

    @api.model
    def _invoice_ocr_enabled(self):
        ICP = self.env["ir.config_parameter"].sudo()
        return ICP.get_param("invoice_ocr.enabled", "True").lower() not in ("false", "0", "")

    def _invoice_ocr_extend_safe(self, move, files_data):
        """_invoice_ocr_extend in a savepoint: a failure rolls back only the OCR's writes.

        An exception (an SQL error included) no longer leaves half-written partners or
        lines behind, nor an aborted transaction for the caller. A failed run is noted in
        the bill's chatter (unless the fill note already says it). Returns the run's
        outcome (see _ocr_result).
        """
        # The caller's own pending writes are flushed outside the try: an error there
        # belongs to the caller (and Odoo's retry loop), it must not be swallowed here.
        self.env.flush_all()
        try:
            with self.env.cr.savepoint():
                result = self._invoice_ocr_extend(move, files_data) or self._ocr_result("filled")
        except Exception as e:  # noqa: BLE001 — OCR must never break the caller
            logger.warning("OCR failed for move %s", move.id, exc_info=True)
            result = self._ocr_result("failed", self._ocr_error_reason(e))
        if result["status"] == "failed" and not result.get("noted"):
            move.message_post(
                body=_("OCR could not fill in this bill: %(reason)s. Fill it in by hand "
                       "or run OCR again.", reason=result["reason"]),
                message_type="comment",
            )
        return result

    def _invoice_ocr_extend(self, move, files_data, final=True):
        """Read the bill's PDF with OCR + AI and pre-fill it.

        Returns the outcome (see _ocr_result): "filled", or "skipped"/"failed" with the
        reason. Exceptions are left to the caller (_invoice_ocr_extend_safe, the cron).

        When the AI step fails (provider down, time limit) and this is not the `final`
        attempt, nothing is written and the outcome asks for a retry; on the final attempt
        (and on the form button) the values read from the text are filled in, the note says
        that the AI failed and the outcome is "failed".

        Runs in the bill's company (#7): the upload path and the list action run in the
        user's active company, and with several companies ticked every company's taxes are
        visible, so accounts, taxes, partners and bank accounts are looked up for
        move.company_id explicitly.
        """
        company = move.company_id
        if self.env.company != company or move.env.company != company:
            return self.with_company(company)._invoice_ocr_extend(
                move.with_company(company), files_data, final=final)
        if not self._invoice_ocr_enabled():
            return self._ocr_result("skipped", _("OCR is turned off in the settings"))

        # Find the first PDF attachment in the file group
        pdf_data = None
        for fd in files_data:
            if (fd.get("mimetype") == "application/pdf"
                    or (fd.get("filename") or "").lower().endswith(".pdf")):
                pdf_data = fd.get("raw") or fd.get("content")
                if isinstance(pdf_data, str):
                    pdf_data = base64.b64decode(pdf_data)
                if pdf_data:
                    break
        if not pdf_data:
            return self._ocr_result("skipped", _("no PDF attachment"))

        # Lazy-import to keep module loadable when libs missing
        from ..lib import invoice_ocr

        # One per-run config: provider settings plus the receiving company's identities,
        # used both by the library and by the own-company guards below.
        cfg = self._invoice_ocr_config(move.company_id)
        cfg["accounts"] = self._ocr_account_list(move.company_id)
        own = self._ocr_own_from_config(cfg)
        try:
            data = invoice_ocr.extract_invoice_data(pdf_data, config=cfg)
        except Exception as e:
            logger.warning("invoice_ocr.extract_invoice_data failed: %s", e)
            return self._ocr_result(
                "failed", _("the PDF could not be read (%s)", str(e)[:300] or type(e).__name__),
                retry=True)

        # The AI step failed (provider down, time limit): try again later before filling in
        # only what the regex read.
        ai_error = (data or {}).get("_ai_error")
        if ai_error and not final:
            return self._ocr_result("failed", _("the AI step failed (%s)", ai_error), retry=True)

        # If no useful data extracted, abort
        if not data or not (data.get("vendor_name") or data.get("invoice_number")):
            if ai_error:
                reason = _("the AI step failed (%s) and the text alone gives neither a vendor "
                           "name nor an invoice number", ai_error)
            else:
                reason = _("neither a vendor name nor an invoice number was found in the PDF")
            # A budget that cut the reading (#26) is part of the reason.
            cuts = [note for note in (data or {}).get("_notes") or []
                    if not str(note).startswith("the AI step failed")]
            if cuts:
                reason = f"{reason} ({'; '.join(cuts)})"
            return self._ocr_result("failed", reason)

        # The marketplace VAT-declarer override ("Moms deklarerat av X" is the vendor, not
        # the "Sold by" merchant) runs in the library, on the full text.

        # ---- Resolve partner from OCR --------------------------------
        notes = []  # kontroller som ska synas i chattern
        for own_nr in data.get("_own_ids_skipped") or []:
            notes.append(_("Org.nr %s på fakturan är bolagets eget (köparen) – "
                           "användes inte som leverantörens.") % own_nr)
        notes += data.get("_notes") or []
        # Only real calendar dates are written: a value the ORM cannot read would make the
        # write raise and lose the whole fill. The library already validates; this guards
        # against anything else (another library version, a patched extraction).
        for key in ("invoice_date", "due_date"):
            if data.get(key) is not None:
                value = invoice_ocr.iso_date(data[key])
                if value:
                    data[key] = value
                else:
                    notes.append(_("%(field)s %(value)s is not a valid date – not used.",
                                   field=key, value=data.pop(key)))
        auto_debit = data.get("auto_debit")
        # ---- Build write vals ----------------------------------------
        vals = {}
        current_is_own = bool(move.partner_id) and self._ocr_is_own_partner(move.partner_id, own)
        # A vendor set on the bill is kept (unless it is the own company): no lookup, no
        # vendor created from the document.
        partner_id = None
        if not move.partner_id or current_is_own:
            partner_id = self._resolve_partner_from_ocr(data, own=own, notes=notes,
                                                        company=company)
        if partner_id:
            vals["partner_id"] = partner_id
            if current_is_own:
                notes.append(_("Leverantören var satt till det egna bolaget (%s) – "
                               "ersatt med leverantören från underlaget.")
                             % move.partner_id.display_name)
        elif current_is_own:
            notes.append(_("Leverantören är det egna bolaget (%s) och ingen annan "
                           "leverantör kunde hittas – välj leverantör för hand.")
                         % move.partner_id.display_name)
        if data.get("invoice_number") and not move.ref:
            vals["ref"] = data["invoice_number"]
        if data.get("invoice_date") and not move.invoice_date:
            vals["invoice_date"] = data["invoice_date"]
            # Bokföringsdatum ska följa fakturadatum, inte den dag underlaget laddades upp.
            # Undantag: aldrig in i en låst period — då behåller vi Odoos default. Odoo's
            # own lock rules decide (#35): the purchase lock date, the parent companies'
            # locks, the hard lock and the user's lock exceptions. has_tax=True: the lines
            # are created after this write, so the tax lock date applies as well.
            inv_date = fields.Date.to_date(data["invoice_date"])
            locks = move._get_violated_lock_dates(inv_date, True)
            if not locks:
                vals["date"] = inv_date
            else:
                logger.info(
                    "OCR: fakturadatum %s ligger i låst period (lås %s) — "
                    "behåller bokföringsdatum", inv_date, locks)
                notes.append(_(
                    "The invoice date %(date)s is in a locked period (%(locks)s): the "
                    "accounting date was left as Odoo set it.", date=inv_date,
                    locks=self.env["res.company"]._format_lock_dates(locks)))
        if data.get("due_date"):
            # Fakturans tryckta forfallodatum vinner alltid over ett berak-
            # nat. Betalningsvillkoret pa leverantorskortet ar ofta en
            # import-default ("40 dagar netto") som inte har med verkligheten
            # att gora, och sa lange invoice_payment_term_id star kvar raknar
            # Odoo om invoice_date_due vid varje sparning och skriver over
            # datumet vi satter har.
            if move.invoice_payment_term_id:
                vals["invoice_payment_term_id"] = False
            if str(move.invoice_date_due or "") != str(data["due_date"]):
                logger.info("OCR: forfallodatum %s -> %s (fran fakturan)",
                            move.invoice_date_due, data["due_date"])
            vals["invoice_date_due"] = data["due_date"]
        # OCR/payment reference. Bara giltiga OCR-nummer: AI:n har klistrat ihop fakturanumret
        # med köparens postnummer och ibland tagit postnumret ensamt. Se
        # _ocr_valid_payment_reference.
        if data.get("ocr_number") and not move.payment_reference:
            ref = self._ocr_valid_payment_reference(data["ocr_number"], data.get("invoice_number"))
            if ref:
                vals["payment_reference"] = ref
            else:
                logger.info("OCR: betalreferensen %r är inget giltigt OCR-nummer, sparas inte",
                            data["ocr_number"])

        # Currency (#5): the bill is in the document's currency, before any line is created.
        # One that cannot be used (unknown, inactive, no rate) gets a warning and no lines:
        # amounts in EUR booked as SEK would be wrong by the exchange rate. Lines already on
        # the bill (a re-run) are not touched, nor is its currency: that is only noted.
        has_lines = bool(move.invoice_line_ids)
        currency, currency_problem = self._ocr_currency(
            data.get("currency"), company,
            vals.get("invoice_date") or move.invoice_date or fields.Date.context_today(self))
        if currency and currency != move.currency_id:
            if has_lines:
                notes.append(_("The document is in %(document)s, the bill in %(bill)s: it already "
                               "has lines, so its currency was not changed.",
                               document=currency.name, bill=move.currency_id.name))
            else:
                vals["currency_id"] = currency.id
        elif currency_problem and has_lines:
            notes.append(_("The document is in %(currency)s, but %(problem)s.",
                           currency=data.get("currency"), problem=currency_problem))

        if vals:
            move.write(vals)

        # Create lines from AI lines if move has none
        if has_lines:
            pass
        elif currency_problem:
            self._ocr_post_currency_warning(move, data, currency_problem)
        else:
            self._create_lines_from_ocr(move, data, notes)

        # Extraherat bankgiro/plusgiro/konto som är bolagets eget
        own_numbers = set()
        for field in ("plusgiro", "bankgiro"):
            if self._ocr_is_own_bank_number(data.get(field), own):
                own_numbers.add(field)
                notes.append(_("%(field)s %(nr)s på fakturan är bolagets eget konto – "
                               "används inte som mottagarkonto.")
                             % {"field": field, "nr": data.get(field)})

        # Resolve partner_bank_id (Bankgiro / Plusgiro)
        # Mjuk koppling till en lokaliseringsmodul som har ett eget autogirofält
        has_l10n_flag = "l10n_se_auto_debit" in move._fields
        if auto_debit:
            upd = {"ocr_auto_debit": True, "ocr_auto_debit_phrase": auto_debit}
            if has_l10n_flag:
                upd["l10n_se_auto_debit"] = True
            if move.partner_bank_id:
                upd["partner_bank_id"] = False
            move.with_context(ocr_auto_debit_write=True).write(upd)
        elif move.ocr_auto_debit and move.ocr_auto_debit_phrase:
            # Flaggan sattes av en tidigare OCR-körning men underlaget ger ingen
            # dragning längre (t.ex. skärpta mönster) — ta bort den. En flagga som
            # satts för hand saknar frasen och lämnas orörd.
            notes.append(_("\"Dras automatiskt\" var satt av OCR (%s) men underlaget "
                           "anger ingen dragning längre – flaggan togs bort.")
                         % move.ocr_auto_debit_phrase)
            upd = {"ocr_auto_debit": False, "ocr_auto_debit_phrase": False}
            if has_l10n_flag:
                upd["l10n_se_auto_debit"] = False
            move.with_context(ocr_auto_debit_write=True).write(upd)
        if not auto_debit and not move.partner_bank_id and move.partner_id:
            self._resolve_partner_bank(move, data, move.partner_id.id,
                                       skip_fields=own_numbers, own=own)
        self._ocr_drop_own_partner_bank(move, own, notes)

        # Redan bokförd via bankraden?
        prebooked = self._ocr_find_prebooked_statement_lines(move, data)

        # Log a chatter note with confidence info. Values come straight out of
        # OCR/LLM output and may contain arbitrary characters, so escape them —
        # same reasoning as _check_ocr_totals, which uses Markup.
        conflicts = data.get("_conflicts") or []
        items = [
            Markup("<li>%s: <code>%s</code></li>") % (k, data[k])
            for k in ("vendor_name", "invoice_number", "invoice_date", "due_date",
                      "total_amount", "subtotal", "vat_amount", "ocr_number", "plusgiro",
                      "bankgiro", "org_number", "currency", "auto_debit")
            if data.get(k) is not None
        ]
        if conflicts:
            items.append(Markup("<li><b>Konflikter regex/AI:</b><br/>%s</li>") % Markup(
                "<br/>").join(Markup("<code>%s</code>") % c for c in conflicts))
        if notes:
            items.append(Markup("<li><b>Kontroller:</b><br/>%s</li>") % Markup(
                "<br/>").join(notes))
        body = (Markup("<p><b>OCR + AI har fyllt i fakturan</b></p><ul>%s</ul>")
                % Markup("").join(items))
        self.env["mail.message"].create({
            "model": "account.move",
            "res_id": move.id,
            "body": body,
            "subject": "OCR-fyllning",
            "message_type": "comment",
            "author_id": self.env.user.partner_id.id,
        })

        if auto_debit:
            move.message_post(
                body=Markup(
                    "<p><b>⚠ Dras automatiskt från kontot – ska inte betalas manuellt</b></p>"
                    "<p>Underlaget anger att beloppet dras från bolagets konto "
                    "(<code>%s</code>). Mottagarkontot har lämnats tomt så att fakturan "
                    "inte hamnar i en betalfil. Stäm av fakturan mot bankraden när "
                    "dragningen syns i stället för att betala den.</p>"
                ) % auto_debit,
                message_type="comment",
            )
        if prebooked:
            self._ocr_post_prebooked_warning(move, prebooked)
        if ai_error:
            return self._ocr_result(
                "failed", _("the AI step failed (%s); only the values read from the text were "
                            "filled in", ai_error), noted=True)
        return self._ocr_result("filled")

    # ------------------------------------------------------------------
    # The receiving company (never the vendor)
    # ------------------------------------------------------------------

    @api.model
    def _ocr_own_companies(self, company=None):
        """The buyer: the invoice's company and its branches (the same legal entity).

        OTHER companies in the database are separate legal entities and may well be the
        vendor (inter-company invoices) — they are not counted as own.
        """
        company = (company or self.env.company).sudo()
        root = company.root_id or company
        return self.env["res.company"].sudo().with_context(active_test=False).search(
            [("id", "child_of", root.id)])

    @api.model
    def _ocr_own_identities(self, company=None):
        """(org/VAT numbers, names) of the buyer, from res.company — not module constants."""
        ids, names = [], []
        for c in self._ocr_own_companies(company):
            ids += [c.vat, c.company_registry, c.partner_id.vat,
                    c.partner_id.company_registry]
            names += [c.name, c.partner_id.name]
        return [i for i in ids if i], [n for n in names if n]

    @api.model
    def _ocr_own_context(self, company=None):
        """The buyer's own identities: org/VAT numbers, names, partners and bank accounts.

        Another partner (archived ones too) carrying the buyer's own org/VAT number also
        counts as own, e.g. a duplicate created by an e-mail import: it is not an external
        vendor, and its bank accounts are the buyer's.
        """
        from ..lib import invoice_ocr

        companies = self._ocr_own_companies(company)
        partners = companies.partner_id
        own_partners = partners | partners.commercial_partner_id
        ids, names = self._ocr_own_identities(company)
        own_partners |= self._ocr_partners_with_own_ids(invoice_ocr.build_own_ids(ids))
        banks = self.env["res.partner.bank"].sudo().with_context(active_test=False).search(
            [("partner_id", "child_of", own_partners.ids)])
        bank_keys, account_keys = invoice_ocr.build_own_bank_keys(
            banks.mapped("sanitized_acc_number"))
        return {
            "ids": ids,
            "names": names,
            "partner_ids": own_partners.ids,
            "bank_keys": bank_keys,
            "account_keys": account_keys,
        }

    @api.model
    def _ocr_partners_with_own_ids(self, own_keys):
        """Partners (archived ones too) whose vat/company_registry is the buyer's own number."""
        from ..lib import invoice_ocr

        Partner = self.env["res.partner"].sudo().with_context(active_test=False)
        orgs = {k for k in own_keys if k.isdigit() and len(k) == 10}
        if not orgs:
            return Partner
        terms = set()
        for o in orgs:
            terms |= {o, f"{o[:6]}-{o[6:]}"}
        fnames = [f for f in ("vat", "company_registry") if f in Partner._fields]
        leaves = [(f, "ilike", t) for f in fnames for t in sorted(terms)]
        domain = ["|"] * (len(leaves) - 1) + leaves
        found = Partner.search(domain)
        return found.filtered(lambda p: any(
            invoice_ocr.is_own_id(p[f], own_keys) for f in fnames if p[f]))

    @api.model
    def _ocr_is_own_partner(self, partner, own):
        partner = partner.sudo()
        return bool(partner) and (
            partner.id in own["partner_ids"]
            or partner.commercial_partner_id.id in own["partner_ids"])

    @api.model
    def _ocr_is_own_bank_number(self, number, own):
        """True if an extracted bankgiro/plusgiro/account number is the buyer's own.

        Also catches the buyer's clearing+account number truncated to bankgiro length.
        """
        from ..lib import invoice_ocr

        return invoice_ocr.is_own_bank_number(
            number, own["bank_keys"], own.get("account_keys", ()))

    def _ocr_drop_own_partner_bank(self, move, own, notes):
        """A vendor bill is never paid to the buyer's own account."""
        if move.move_type not in ("in_invoice", "in_receipt"):
            return
        bank = move.partner_bank_id
        if bank and self._ocr_is_own_partner(bank.partner_id, own):
            notes.append(_("Mottagarkontot %s tillhör det egna bolaget – togs bort.")
                         % bank.display_name)
            move.partner_bank_id = False

    def _ocr_partner_search(self, domain, own, notes, how, limit=1, company=None):
        """res.partner.search that never returns the buyer's own company.

        Only partners the bill's company may use (shared ones and its own, #7). If the
        search would only have hit the own company, that is noted in the chatter.
        """
        Partner = self.env["res.partner"]
        domain = [*Partner._check_company_domain(company or self.env.company), *domain]
        excl = [("id", "not in", own["partner_ids"]),
                ("commercial_partner_id", "not in", own["partner_ids"])]
        found = Partner.search(domain + excl, limit=limit)
        if not found and Partner.search_count(
                domain + [("commercial_partner_id", "in", own["partner_ids"])], limit=1):
            notes.append(_("%s pekade på det egna bolaget – hoppades över.") % how)
        return found

    # ------------------------------------------------------------------
    # Accounts and taxes of the bill's company (#7)
    # ------------------------------------------------------------------

    @api.model
    def _ocr_account(self, company, code):
        """The account with `code` in `company`'s chart, else its first expense sub-account
        (6540 → 65400 on a longer-coded chart), else an empty recordset.

        Searches `code`, not `code_store`: code_store is company-dependent and resolves
        through the active company, while `code` resolves through company.root_id (so it
        also works for branches).
        """
        Account = self.env["account.account"].with_company(company)
        code = str(code or "").strip()
        if not code:
            return Account
        domain = list(Account._check_company_domain(company))
        return (Account.search([*domain, ("code", "=", code)], limit=1)
                or Account.search([*domain, ("code", "=like", f"{code}_%"),
                                   ("account_type", "=like", "expense%")], limit=1))

    @api.model
    def _ocr_account_list(self, company):
        """The accounts the model may choose for `company`'s bills: [(code, hint)] (#24).

        The list in the settings (invoice_ocr.account_list, one "code: hint" per line), or
        the built-in Swedish BAS list when that is empty, limited to the accounts that exist
        in the company's chart: a code the chart does not have is never offered.
        """
        from ..lib import invoice_ocr

        text = self.env["ir.config_parameter"].sudo().get_param("invoice_ocr.account_list")
        accounts = invoice_ocr.parse_account_list(text) or invoice_ocr.DEFAULT_ACCOUNTS
        Account = self.env["account.account"].with_company(company)
        chart = Account.search(list(Account._check_company_domain(company)))
        expense = chart.filtered(lambda a: a.account_type.startswith("expense"))
        return invoice_ocr.accounts_in_chart(accounts, chart.mapped("code"), expense.mapped("code"))

    @api.model
    def _ocr_fallback_account(self, move):
        """The account for a line without a usable account code: the purchase journal's
        default account, else the company's default expense account."""
        journal_account = move.journal_id.default_account_id
        if journal_account and journal_account.account_type.startswith("expense"):
            return journal_account
        return move.company_id.expense_account_id

    @api.model
    def _ocr_tax(self, company, xmlid):
        """The purchase tax `xmlid` (l10n_se template id, e.g. purchase_tax_25_goods) of
        `company`, or an empty recordset.

        Resolved through the chart template, so it is the tax created for this company (or
        its root company, for a branch). Charts not loaded from l10n_se fall back to a
        domain search, see _ocr_tax_search.
        """
        Tax = self.env["account.tax"].with_company(company)
        tax = self.env["account.chart.template"].with_company(company).ref(
            xmlid, raise_if_not_found=False)
        if (tax and tax._name == "account.tax" and tax.active
                and tax.type_tax_use == "purchase" and tax.company_id in company.parent_ids):
            return Tax.browse(tax.id)
        return self._ocr_tax_search(company, xmlid)

    @api.model
    def _ocr_tax_search(self, company, xmlid):
        """A purchase tax of `company` like the l10n_se tax `xmlid`, for other charts.

        Domestic (purchase_tax_<rate>_<kind>): the first percent tax with that rate that is
        not a reverse-charge tax (no negative repartition line), preferring the company's
        fiscal country. Reverse charge (purchase_<kind>_tax_<rate>_EC/NEC): only a tax named
        like l10n_se's ("25% EU G", "25% EX S"); nothing is guessed from rates alone.
        """
        Tax = self.env["account.tax"].with_company(company)
        domain = [*Tax._check_company_domain(company), ("type_tax_use", "=", "purchase"),
                  ("amount_type", "=", "percent")]
        m = re.fullmatch(r"purchase_tax_(\d+)_(goods|services)", xmlid)
        if m:
            candidates = Tax.search([*domain, ("amount", "=", int(m.group(1))),
                                     ("price_include", "=", False)])
            candidates = candidates.filtered(lambda t: not any(
                line.factor_percent < 0 for line in t.invoice_repartition_line_ids))
            country = company.account_fiscal_country_id
            return (candidates.filtered(lambda t: t.country_id == country)[:1]
                    or candidates[:1])
        m = re.fullmatch(r"purchase_(goods|services)_tax_(\d+)_(EC|NEC)", xmlid)
        if m:
            region = "EU" if m.group(3) == "EC" else "EX"
            kind = "G" if m.group(1) == "goods" else "S"
            name = f"{m.group(2)}% {region} {kind}"
            return Tax.search([*domain, ("amount", "=", int(m.group(2))),
                               ("name", "=ilike", name)], limit=1)
        return Tax

    # ------------------------------------------------------------------
    # Payment reference
    # ------------------------------------------------------------------

    @staticmethod
    def _ocr_mod10(number):
        """Luhn/modulus 10 as used by Bankgirot and Plusgirot for OCR numbers."""
        from ..lib import invoice_ocr

        return invoice_ocr.ocr_mod10(number)

    @api.model
    def _ocr_valid_payment_reference(self, ocr_number, invoice_number=None):
        """The payment reference to store, or False (see invoice_ocr.valid_payment_reference)."""
        from ..lib import invoice_ocr

        return invoice_ocr.valid_payment_reference(ocr_number, invoice_number)

    # ------------------------------------------------------------------
    # Helpers (partner, lines, bank)
    # ------------------------------------------------------------------

    def _resolve_partner_from_ocr(self, data, own=None, notes=None, company=None):
        """Match OCR-extracted vendor data to res.partner. Auto-create if needed.

        Never returns the receiving company's partner (or a contact under it): on a vendor
        bill the own company is the buyer, not the seller. Only partners and bank accounts
        the bill's `company` may use are considered (#7). Returns the commercial partner's
        id, and a note says which rule matched (#13):

        1. the VAT number, 2. the Swedish org number, 3. the bankgiro/plusgiro, compared
        digit for digit with the partner's account (never a substring of another number),
        4. the name, among the company's vendors only: the same name apart from legal form
        and punctuation, else every distinctive word of the name (not 'AB', 'Sverige' …).
        More than one partner on a rule is no match. 5. Otherwise a vendor with a name and
        an org/VAT number or giro number is created.
        """
        from ..lib import invoice_ocr

        Partner = self.env["res.partner"]
        company = company or self.env.company
        own = own if own is not None else self._ocr_own_context(company)
        notes = notes if notes is not None else []
        own_keys = invoice_ocr.build_own_ids(own["ids"])
        own_names = invoice_ocr.build_own_names(own["names"])

        def matched(partner, how):
            partner = partner.commercial_partner_id
            notes.append(_("Vendor %(vendor)s: matched on %(how)s.",
                           vendor=partner.display_name, how=how))
            return partner.id

        # 1. VAT (any country prefix already in OCR, or Swedish org number)
        org_raw = (data.get("org_number") or "").strip()
        if org_raw and invoice_ocr.is_own_id(org_raw, own_keys):
            # Lib:en filtrerar redan bort egna nummer; detta är ett extra skydd
            notes.append(_("Org.nr %s är bolagets eget – användes inte.") % org_raw)
            org_raw = ""
        # If looks like a VAT number with letter prefix (e.g. LU20260743, SE556...)
        if org_raw and re.match(r"^[A-Z]{2}\d", org_raw):
            p = self._ocr_partner_search([("vat", "=", org_raw)], own, notes,
                                         _("Momsreg.nr %s") % org_raw, company=company)
            if p:
                return matched(p, _("the VAT number %s", org_raw))

        # 2. Swedish org number — multiple variants
        org_clean = re.sub(r"[^0-9]", "", org_raw)
        if org_clean:
            how = _("Org.nr %s") % org_raw
            for v in [f"SE{org_clean}01", f"SE{org_clean}", org_clean]:
                p = self._ocr_partner_search([("vat", "=", v)], own, [], how, company=company)
                if p:
                    return matched(p, _("the org number %s", org_raw))
            p = self._ocr_partner_search([("vat", "ilike", org_clean)], own, notes, how,
                                         company=company)
            if p:
                return matched(p, _("the org number %s", org_raw))

        # 3. Plusgiro / bankgiro: the same digits, not a substring of another account
        for field in ("plusgiro", "bankgiro"):
            bg = (data.get(field) or "").strip()
            bg_clean = invoice_ocr.giro_digits(bg)
            if not bg_clean or self._ocr_is_own_bank_number(bg_clean, own):
                continue  # det egna kontot säger inget om leverantören
            partners = self._ocr_giro_accounts(bg_clean, company, own).partner_id
            partners = partners.commercial_partner_id
            if len(partners) == 1:
                return matched(partners, f"{field} {bg}")
            if partners:
                notes.append(_("%(field)s %(number)s belongs to several partners (%(names)s) – "
                               "none was chosen.", field=field, number=bg,
                               names=", ".join(partners.mapped("display_name"))))

        # 4. Vendor name, among the company's vendors
        name = (data.get("vendor_name") or "").strip()
        if name:
            # Strip OCR noise prefixes
            name = re.sub(r"^(services from|invoice from|faktura från|leverant.+? från)\s+",
                          "", name, flags=re.IGNORECASE).strip()
        if name and invoice_ocr._name_key(name) in own_names:
            notes.append(_("Leverantörsnamnet \"%s\" är det egna bolaget – "
                           "användes inte.") % name)
            name = ""
        if name:
            partner = self._ocr_partner_by_name(name, own, notes, company)
            if partner:
                notes.append(_("Vendor %(vendor)s: matched on the name \"%(name)s\" only, no "
                               "VAT, org or giro number matched – check that it is the right "
                               "vendor.", vendor=partner.display_name, name=name))
                return partner.id

        # 5. Auto-create partner if we have a name + org/VAT
        # Ett autogiro-underlag trycker KÖPARENS konto, inte leverantörens — lägg
        # inte upp det som leverantörens bankkonto.
        banks = []
        if not data.get("auto_debit"):
            for field, label in [("plusgiro", "PG"), ("bankgiro", "BG")]:
                bg = (data.get(field) or "").strip()
                if bg and not self._ocr_is_own_bank_number(bg, own):
                    banks.append(f"{label} {bg}")
        if name and (org_raw or banks):
            vals = {"name": name, "is_company": True, "supplier_rank": 1}
            # VAT
            if org_raw and re.match(r"^[A-Z]{2}\d", org_raw):
                vals["vat"] = org_raw
            elif org_clean:
                vals["vat"] = f"SE{org_clean}01" if len(org_clean) == 10 else org_clean
            # Country from the VAT prefix; Greek numbers start with EL and Northern Irish
            # ones with XI, which are no country codes (#22)
            cc = invoice_ocr.country_from_vat(vals.get("vat"))
            if cc:
                country = self.env["res.country"].search([("code", "=", cc)], limit=1)
                if country:
                    vals["country_id"] = country.id
            new_partner = Partner.create(vals)
            # Add bank if BG/PG present
            for acc in banks:
                self.env["res.partner.bank"].create({
                    "partner_id": new_partner.id,
                    "acc_number": acc,
                })
            notes.append(_("Vendor %s: not found, created from the document.",
                           new_partner.display_name))
            return new_partner.id

        return None

    def _ocr_giro_accounts(self, digits, company, own):
        """Bank accounts the company may use whose number is exactly these giro digits
        ('BG 123-4566' for '1234566'), never the buyer's own (#13)."""
        from ..lib import invoice_ocr

        Bank = self.env["res.partner.bank"]
        banks = Bank.search([
            *Bank._check_company_domain(company),
            ("sanitized_acc_number", "ilike", digits),
            ("partner_id", "not in", own["partner_ids"]),
            ("partner_id.commercial_partner_id", "not in", own["partner_ids"]),
        ])
        return banks.filtered(
            lambda b: invoice_ocr.giro_digits(b.sanitized_acc_number) == digits)

    def _ocr_partner_by_name(self, name, own, notes, company):
        """The company's one vendor (supplier_rank > 0) whose name matches `name`
        (invoice_ocr.name_match): the same name wins over one with all distinctive words.
        Several partners on the best rule: none, and a note."""
        from ..lib import invoice_ocr

        tokens = invoice_ocr.name_tokens(name)
        if not tokens:
            return self.env["res.partner"]
        found = self._ocr_partner_search(
            [("name", "ilike", max(tokens, key=len)), ("supplier_rank", ">", 0)], own, notes,
            _("Namnet \"%s\"") % name, limit=100, company=company)
        for rule in ("full", "tokens"):
            partners = found.filtered(
                lambda p, rule=rule: invoice_ocr.name_match(name, p.name) == rule
            ).commercial_partner_id
            if len(partners) == 1:
                return partners
            if partners:
                notes.append(_("The name \"%(name)s\" matches several vendors (%(names)s) – "
                               "none was chosen.", name=name,
                               names=", ".join(partners.mapped("display_name"))))
                break
        return self.env["res.partner"]

    @api.model
    def _ocr_partner_country_code(self, partner):
        """The vendor's country code: its country, else its VAT number's prefix (EL is GR)."""
        from ..lib import invoice_ocr

        partner = partner.commercial_partner_id
        if partner.country_id:
            return partner.country_id.code
        code = invoice_ocr.country_from_vat(partner.vat)
        if code and self.env["res.country"].search_count([("code", "=", code)], limit=1):
            return code
        return None

    @api.model
    def _ocr_swedish_vat_number(self, partner, data):
        """A Swedish VAT or org number of the vendor, from the document or the partner, or None.

        A foreign supplier that shows one is registered for VAT in Sweden: VAT it charges is
        Swedish VAT, deductible as input VAT (#22).
        """
        found = list(data.get("_se_vat_numbers") or [])
        org = str(data.get("org_number") or "").strip()
        if org.upper().startswith("SE") or re.fullmatch(r"\d{6}-?\d{4}", org):
            found.append(org)
        vat = (partner.commercial_partner_id.vat or "").strip()
        if vat.upper().startswith("SE"):
            found.append(vat)
        return found[0] if found else None

    @api.model
    def _ocr_bill_lines(self, data):
        """The lines to create: the AI's lines, or one line from the totals.

        Each is a dict with amount, and maybe quantity, unit_price, vat_rate, account_code
        and description. The single line from the totals derives its VAT rate from the
        printed amounts (95,00 with 5,38 VAT is 6 %, not 25 %).
        """
        lines = [dict(line) for line in data.get("lines") or [] if isinstance(line, dict)]
        if lines:
            return lines
        total = data.get("total_amount")
        vat = data.get("vat_amount")
        subtotal = data.get("subtotal")
        if total and vat and not subtotal:
            subtotal = total - vat
        elif subtotal and vat and not total:
            total = subtotal + vat
        elif total and not vat and not subtotal:
            subtotal = total
        elif total and subtotal and not vat:
            vat = round(total - subtotal, 2)
        # Sanity: subtotal+vat ≈ total else trust total
        if total and subtotal and vat:
            expected = round(subtotal + vat, 2)
            if abs(expected - total) > 1.0:
                subtotal = round(total - vat, 2)
        if not subtotal or subtotal <= 0:
            return []
        rate = None
        if vat and subtotal:
            pct = round(vat / subtotal * 100)
            rate = min((25, 12, 6), key=lambda r: abs(r - pct))
            if abs(rate - pct) > 2:
                logger.warning("OCR: moms %.2f på netto %.2f ger %s%%, ingen giltig sats matchar",
                               vat, subtotal, pct)
                rate = None
        return [{"description": data.get("invoice_number") or "Faktura", "amount": subtotal,
                 "vat_rate": rate}]

    def _create_lines_from_ocr(self, move, data, notes=None):
        """Create the bill's lines from the AI's lines (or the totals), each with its own tax.

        The tax is chosen per line once its final account is known (#8): goods or services
        from the account (BAS), the region from the vendor's country, and from the printed
        VAT whether VAT was charged at all (#22) — see invoice_ocr.line_tax_xmlid. Taxes are
        the bill company's, by l10n_se template id (#7).

        * Foreign vendor, no VAT on the document: reverse charge per line (EU goods/services,
          import of goods, services from outside the EU). A 0 % line on an out-of-scope
          account (reminder fee, bank charge …) gets no tax; never 25 %.
        * Foreign vendor charging Swedish VAT (it shows a Swedish VAT number): Swedish input
          VAT, as for a Swedish vendor.
        * Foreign vendor charging foreign VAT (a hotel abroad): that VAT is not deductible in
          Sweden, so it is added to the lines' cost and they get no tax.
        Notes for the chatter are appended to `notes`.
        """
        from ..lib import invoice_ocr as lib

        notes = notes if notes is not None else []
        company = move.company_id
        lines = self._ocr_bill_lines(data)
        if not lines:
            return
        partner = move.partner_id.commercial_partner_id
        region = lib.vat_region(self._ocr_partner_country_code(partner))
        vat_total = lib.document_vat(data)
        vat_charged = lib.vat_was_charged(vat_total, sum(line.get("amount") or 0.0 for line in lines))
        se_number = self._ocr_swedish_vat_number(partner, data) if region != "domestic" else None
        treatment = lib.bill_vat_treatment(region, vat_charged, bool(se_number))
        for line in lines:
            line["vat_rate"] = lib.line_vat_rate(line.get("vat_rate"))

        if treatment == "foreign_vat":
            self._ocr_add_foreign_vat_to_cost(lines, vat_total)
            data["_foreign_vat"] = vat_total
            notes.append(_(
                "The supplier is abroad and charged foreign VAT (%(vat)s). Foreign VAT is not "
                "deductible in Sweden: it is booked as part of the cost, without Swedish VAT "
                "and without reverse charge. If the purchase should have been invoiced without "
                "VAT (reverse charge), ask the supplier for a corrected invoice.",
                vat=f"{vat_total:.2f}"))
        elif treatment == "domestic" and region != "domestic":
            notes.append(_(
                "The supplier is abroad but charged Swedish VAT (%(number)s): booked as "
                "Swedish input VAT, without reverse charge.", number=se_number))

        line_vals_list, imports = [], False
        for line in lines:
            qty_price = lib.line_quantity_and_price(line)
            if not qty_price:
                continue
            quantity, price_unit = qty_price
            rate = line["vat_rate"]
            description = str(line.get("description") or "")
            account = self._ocr_line_account(move, line.get("account_code"), region, rate,
                                             treatment, notes)
            lv = {
                "name": description or data.get("invoice_number") or "Faktura",
                "quantity": quantity,
                "price_unit": price_unit,
                "account_id": account.id,
                "tax_ids": [(5, 0, 0)],
            }
            xmlid = lib.line_tax_xmlid(account.code, rate, region, treatment)
            if xmlid:
                tax = self._ocr_tax(company, xmlid)
                if tax:
                    lv["tax_ids"] = [(6, 0, tax.ids)]
                    imports = imports or xmlid.startswith("purchase_goods_tax_") and xmlid.endswith("_NEC")
                else:
                    notes.append(_("No purchase tax %(tax)s in the chart of accounts – the line "
                                   "\"%(line)s\" has no tax.", tax=xmlid, line=lv["name"]))
            line_vals_list.append((0, 0, lv))

        if imports:
            notes.append(_(
                "Import of goods from outside the EU: the VAT base (box 50) is the customs value "
                "on the customs bill plus duty and freight to Sweden, not the invoice amount. "
                "The goods lines carry the import tax on the invoice amount – check it against "
                "the customs bill."))
        if line_vals_list:
            move.write({"invoice_line_ids": line_vals_list})
            self._ocr_apply_total_adjustments(move, data)
            self._check_ocr_totals(move, data)

    @api.model
    def _ocr_add_foreign_vat_to_cost(self, lines, vat_total):
        """Add the document's foreign VAT to the lines that carry it, in proportion.

        The lines with a VAT rate carry it; if none has one, every line that is not on an
        out-of-scope account (fees), else every line. Quantity and unit price are kept when
        they still come to the new amount, else the line becomes 1 × amount.
        """
        from ..lib import invoice_ocr as lib

        bearing = [line for line in lines if line["vat_rate"]]
        bearing = bearing or [line for line in lines
                              if not lib.is_out_of_scope_account(line.get("account_code"))]
        bearing = bearing or lines
        shares = lib.spread_amount([line["amount"] for line in bearing], vat_total)
        for line, share in zip(bearing, shares, strict=True):
            line["amount"] = round(line["amount"] + share, 2)
            line.pop("unit_price", None)

    def _ocr_line_account(self, move, code, region, rate, treatment, notes):
        """The line's account in the bill's company.

        The BAS account for the purchase (invoice_ocr.account_candidates, e.g. 4000 from an
        EU supplier is 4515), else the code as given, else the fallback account (the
        journal's default, see _ocr_fallback_account), remapped the same way. A code that is
        not in the chart is noted.
        """
        from ..lib import invoice_ocr as lib

        company = move.company_id
        for candidate in lib.account_candidates(code, region, rate, treatment):
            account = self._ocr_account(company, candidate)
            if account:
                return account
        fallback = self._ocr_fallback_account(move)
        if fallback:
            for candidate in lib.account_candidates(fallback.code, region, rate, treatment):
                account = self._ocr_account(company, candidate)
                if account:
                    fallback = account
                    break
        if code:
            notes.append(_("Account %(code)s is not in the chart of accounts – used %(account)s.",
                           code=code, account=fallback.display_name or "–"))
        return fallback

    def _ocr_apply_total_adjustments(self, move, data):
        """Rätta öresavrundning och justeringar utanför moms mot fakturans tryckta belopp.

        Se invoice_ocr.plan_total_adjustments (ex: tillgodo −0,25 utanför moms och
        öresavrundning −1,00 – AI:n gav en momsrad på nettot och totalen blev 2 575,94 i
        stället för 2 575,00). Bara svenska leverantörer och vanliga procentsatser;
        omvänd skattskyldighet och blandade momskoder lämnas orörda.
        """
        from ..lib import invoice_ocr

        printed = data.get("_printed") or {}
        if not printed or move.move_type != "in_invoice":
            return
        country = move.partner_id.commercial_partner_id.country_id.code
        if country and country != "SE":
            return
        move.invalidate_recordset()
        lines = move.invoice_line_ids.filtered(lambda ln: ln.display_type == "product")
        taxed, untaxed = {}, 0.0
        for line in lines:
            if not line.tax_ids:
                untaxed += line.price_subtotal
            elif len(line.tax_ids) == 1 and line.tax_ids.amount_type == "percent" and line.tax_ids.amount:
                rate = int(round(line.tax_ids.amount))
                taxed[rate] = taxed.get(rate, 0.0) + line.price_subtotal
            else:
                return
        plan = invoice_ocr.plan_total_adjustments(
            taxed, untaxed, move.amount_tax, printed.get("vat_amount"), printed.get("total_amount"))
        if not plan["base_shift"] and not plan["rounding"]:
            return
        account = self._ocr_account(move.company_id, "3740")
        if not account:
            logger.warning("OCR: konto 3740 saknas – öresavrundning läggs inte till på move %s", move.id)
            return
        commands, notes = [], []
        if plan["base_shift"]:
            rate, delta = plan["base_shift"]
            target = lines.filtered(lambda ln: ln.tax_ids and int(round(ln.tax_ids.amount)) == rate
                                    and ln.quantity == 1).sorted("price_subtotal", reverse=True)[:1]
            if target:
                commands.append((1, target.id, {"price_unit": round(target.price_unit + delta, 2)}))
            else:
                commands.append((0, 0, {"name": "Justering av momsunderlag", "quantity": 1, "price_unit": delta,
                                        "account_id": lines.filtered("tax_ids")[:1].account_id.id,
                                        "tax_ids": [(6, 0, lines.filtered("tax_ids")[:1].tax_ids.ids)]}))
            commands.append((0, 0, {"name": "Justering utanför moms (t.ex. tillgodo)", "quantity": 1,
                                    "price_unit": -delta, "account_id": account.id, "tax_ids": [(5, 0, 0)]}))
            notes.append(f"momsunderlaget {delta:+.2f} enligt fakturans moms {printed.get('vat_amount'):.2f}, "
                         f"motsvarande {-delta:+.2f} utanför moms på 3740")
        if plan["rounding"]:
            commands.append((0, 0, {"name": "Öresavrundning", "quantity": 1, "price_unit": plan["rounding"],
                                    "account_id": account.id, "tax_ids": [(5, 0, 0)]}))
            notes.append(f"öresavrundning {plan['rounding']:+.2f} på 3740 så att totalen blir "
                         f"fakturans {printed.get('total_amount'):.2f}")
            # Kontrollen nedan jämför nettot med fakturans "exkl. moms", som är före avrundningen.
            data["_rounding_adjust"] = plan["rounding"]
        move.write({"invoice_line_ids": commands})
        move.message_post(
            body=Markup("<p><b>OCR: justerat mot fakturans tryckta belopp</b></p><p>%s</p>")
            % Markup("<br/>").join(notes),
            message_type="comment",
        )

    def _ocr_post_currency_warning(self, move, data, problem):
        logger.warning("OCR: move %s is in %s, which cannot be used: %s", move.id,
                       data.get("currency"), problem)
        move.message_post(
            body=Markup("<p><b>⚠ %s</b></p><p>%s</p>") % (
                _("OCR: no lines were created – the document's currency cannot be used"),
                _("The document is in %(currency)s, but %(problem)s. The bill was left in "
                  "%(bill_currency)s without lines, since its amounts would be wrong by the "
                  "exchange rate. Activate the currency and add a rate (Accounting → "
                  "Configuration → Currencies), then run OCR again, or enter the lines by hand "
                  "in the right currency.",
                  currency=data.get("currency"), problem=problem,
                  bill_currency=move.currency_id.name)),
            message_type="comment",
        )

    def _check_ocr_totals(self, move, data):
        """Varna om de skapade raderna inte summerar till fakturans tryckta belopp.

        AI:n tappar rader pa langa specifikationer och lagger ibland rabatter
        utanfor momsen. Bada ger en faktura som ser komplett ut men ar fel, och
        utan den har kontrollen bokfors den utan att nagon marker det.
        """
        move.invalidate_recordset()
        tol = 1.0  # oresavrundning och enstaka oren ar inte varda en varning

        # Fakturans TRYCKTA belopp, inte de mergade. Efter sammanslagningen vinner
        # regex pa siffrorna, sa de sammanfaller oftast — men saknar regex ett falt
        # star AI:ns varde kvar i data, och da vore kontrollen sjalvbekraftande.
        from ..lib import invoice_ocr

        printed = data.get("_printed") or {}
        printed_net = printed.get("subtotal")
        printed_total = printed.get("total_amount")
        printed_vat = printed.get("vat_amount")
        problems = []
        # The amounts are compared digit for digit, so they must be in the same currency (#5)
        document_currency = invoice_ocr.normalize_currency(data.get("currency"))
        if document_currency and document_currency != move.currency_id.name:
            problems.append(_("the bill is in %(bill)s, the document in %(document)s",
                              bill=move.currency_id.name, document=document_currency))
        if not printed and not problems:
            logger.info("OCR: inga tryckta belopp lasta ur PDF:en — "
                        "radsumman kan inte kontrolleras mot fakturan")
            return

        net = move.amount_untaxed - (data.get("_rounding_adjust") or 0.0)
        # Foreign VAT booked as cost (#22) is in the net and not in the tax.
        foreign_vat = data.get("_foreign_vat") or 0.0
        if printed_net is not None and abs(net - printed_net - foreign_vat) > tol:
            problems.append(
                f"netto {net:.2f} mot fakturans {printed_net + foreign_vat:.2f}")
        if printed_vat is not None and abs(move.amount_tax - printed_vat + foreign_vat) > tol:
            problems.append(
                f"moms {move.amount_tax:.2f} mot fakturans {printed_vat - foreign_vat:.2f}")
        if printed_total is not None and abs(move.amount_total - printed_total) > tol:
            problems.append(
                f"totalt {move.amount_total:.2f} mot fakturans {printed_total:.2f}")
        if not problems:
            return

        logger.warning("OCR: radsumman avviker pa move %s: %s",
                       move.id, "; ".join(problems))
        move.message_post(
            body=Markup(
                "<p><b>⚠ OCR: raderna stämmer inte med fakturan</b></p>"
                "<p>%s</p>"
                "<p>Raderna är skapade av AI-tolkningen och summerar inte till "
                "beloppen som står tryckta på underlaget — troligen har en rad "
                "fallit bort eller fått fel momssats. Kontrollera mot PDF:en "
                "innan fakturan bokförs.</p>"
            ) % Markup("<br/>").join(problems),
            message_type="comment",
        )

    # ------------------------------------------------------------------
    # Already booked through the bank statement?
    # ------------------------------------------------------------------

    def _ocr_find_prebooked_statement_lines(self, move, data, window_days=10):
        """Bank statement lines that already booked the cost directly, without a payable.

        If a debit was already reconciled against e.g. a bank-charges account and the bill
        is uploaded afterwards, the cost is booked twice. Candidates are posted statement
        lines of the same company whose text contains the invoice number/payment
        reference, or that have the same amount within ±window_days of the due date and
        a word from the vendor's name in the text. Only lines whose counterpart is NOT a
        payable/receivable account (and not the suspense account) count.
        """
        SL = self.env["account.bank.statement.line"]
        if move.move_type not in ("in_invoice", "in_receipt"):
            return SL
        base = [("company_id", "=", move.company_id.id),
                ("move_id.state", "=", "posted")]

        refs = []
        for r in (data.get("invoice_number"), move.ref, move.payment_reference,
                  data.get("ocr_number")):
            r = re.sub(r"\s+", "", str(r or ""))
            # korta referenser ('08635') träffar för mycket
            if len(r) >= 6 and r not in refs:
                refs.append(r)
        candidates = SL
        if refs:
            dom = ["|"] * (len(refs) - 1) + [("payment_ref", "ilike", r) for r in refs]
            candidates |= SL.search(base + dom, limit=20)

        total = move.amount_total
        ref_date = (move.invoice_date_due or move.invoice_date
                    or fields.Date.to_date(data.get("due_date") or data.get("invoice_date")))
        if total and ref_date:
            if move.currency_id == move.company_id.currency_id:
                amount_dom = [("amount", ">=", -total - 0.005), ("amount", "<=", -total + 0.005)]
            else:
                amount_dom = [("foreign_currency_id", "=", move.currency_id.id),
                              ("amount_currency", ">=", -total - 0.005),
                              ("amount_currency", "<=", -total + 0.005)]
            same_amount = SL.search(base + amount_dom + [
                ("date", ">=", ref_date - timedelta(days=window_days)),
                ("date", "<=", ref_date + timedelta(days=window_days)),
            ], limit=20)
            tokens = self._ocr_name_tokens(move.partner_id.name, data.get("vendor_name"))
            for st in same_amount:
                text = re.sub(r"\s+", "", " ".join(
                    filter(None, [st.payment_ref, st.partner_name, st.partner_id.name]))).lower()
                if any(t in text for t in tokens):
                    candidates |= st

        prebooked = SL
        for st in candidates:
            liquidity, suspense, other = st._seek_for_lines()
            if suspense or not other:
                continue  # inte avstämd än — inget är bokfört
            direct = other.filtered(lambda line: line.account_id.account_type
                                    not in ("liability_payable", "asset_receivable"))
            # Avstämd mot reskontran plus en liten avgifts-/kursdifferensrad är en
            # vanlig betalning, inte en direktbokad kostnad: kräv att merparten av
            # bankradens belopp gått direkt mot andra konton.
            bank_amount = abs(sum(liquidity.mapped("balance")))
            if direct and sum(abs(b) for b in direct.mapped("balance")) * 2 >= bank_amount:
                prebooked |= st
        return prebooked

    @staticmethod
    def _ocr_name_tokens(*names):
        # Ord i leverantörsnamn som inte säger något om vem bankraden gäller
        # (invoice_ocr.NAME_STOPWORDS, shared with the receipt module's merchant check)
        from ..lib import invoice_ocr

        tokens = set()
        for n in names:
            for t in re.split(r"[\s,.()/&-]+", str(n or "").lower()):
                if len(t) >= 3 and t not in invoice_ocr.NAME_STOPWORDS and not t.isdigit():
                    tokens.add(t)
        return tokens

    def _ocr_post_prebooked_warning(self, move, statement_lines):
        rows = []
        for st in statement_lines:
            _liquidity, _suspense, other = st._seek_for_lines()
            accounts = ", ".join(sorted({
                line.account_id.display_name for line in other
                if line.account_id.account_type not in ("liability_payable", "asset_receivable")}))
            rows.append(Markup("<li>%s – %s, %s %s (%s) – motkonto: <b>%s</b></li>") % (
                st.move_id.name, st.date, st.payment_ref or "",
                st.amount, st.journal_id.name, accounts))
        logger.warning("OCR: move %s may already be booked through statement line(s) %s",
                       move.id, statement_lines.ids)
        move.message_post(
            body=Markup(
                "<p><b>⚠ Kostnaden kan redan vara bokförd via banken</b></p>"
                "<p>Följande bankrad(er) är redan avstämda direkt mot ett kostnads- "
                "eller annat konto, inte mot leverantörsskulden:</p><ul>%s</ul>"
                "<p>Bokförs fakturan ovanpå blir kostnaden dubbel. Gör om avstämningen "
                "av bankraden så att den matchar den här fakturan, eller släng "
                "utkastet om underlaget redan är bokfört.</p>"
            ) % Markup("").join(rows),
            message_type="comment",
        )

    def _resolve_partner_bank(self, move, data, partner_id, skip_fields=(), own=None):
        """Pick a recipient bank account on the partner that matches OCR plusgiro/bankgiro."""
        own = own if own is not None else self._ocr_own_context(move.company_id)
        if move.move_type in ("in_invoice", "in_receipt") and partner_id in own["partner_ids"]:
            return  # betala aldrig till det egna bolaget
        for field in ("plusgiro", "bankgiro"):
            if field in skip_fields:
                continue
            bg = (data.get(field) or "").strip()
            if not bg:
                continue
            bg_clean = re.sub(r"[^0-9]", "", bg)
            if not bg_clean:
                continue
            Bank = self.env["res.partner.bank"]
            banks = Bank.search([
                *Bank._check_company_domain(move.company_id),
                ("partner_id", "child_of", partner_id),
                ("sanitized_acc_number", "ilike", bg_clean),
                ("active", "=", True),
            ])
            # The same number, not one that merely contains these digits (#13)
            bank = banks.filtered(lambda b, digits=bg_clean: re.sub(
                r"\D", "", b.sanitized_acc_number or "") == digits)[:1]
            if bank:
                move.partner_bank_id = bank.id
                return
