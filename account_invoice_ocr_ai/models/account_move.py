"""Run OCR + AI on PDFs uploaded via the 'Ladda upp'-button to pre-fill
vendor bill fields (invoice_date, ref, partner, amounts, lines).

Hooks into account.move._extend_with_attachments which is called both when:
  - User uploads via the journal's "Ladda upp"-button
  - PDF is attached to a draft vendor bill via chatter
"""

import base64
import logging
import re
from collections import Counter
from datetime import timedelta

from markupsafe import Markup

from odoo import _, api, fields, models, modules

logger = logging.getLogger(__name__)


# --- BAS account fallbacks per VAT rate (domestic purchases) ------------------

ACCOUNT_FALLBACKS = {
    # Domestic purchases
    25: 4000,    # 25% Sweden goods/services default
    12: 4000,
    6: 4000,
    0: 4000,
}


# Ord i leverantörsnamn som inte säger något om vem bankraden gäller
_NAME_STOPWORDS = {
    "ab", "aktiebolag", "publ", "bank", "banken", "sverige", "sweden", "svenska",
    "the", "och", "and", "ltd", "limited", "inc", "llc", "gmbh", "group", "services",
    "company", "international", "nordic", "scandinavia",
}


class AccountMove(models.Model):
    _inherit = "account.move"

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
        """Re-run OCR + AI on the latest PDF attachment of each selected draft vendor bill.

        The form button and the list action ("Kör OCR igen"). Each bill runs in its own
        savepoint (_invoice_ocr_extend_safe), so one failure neither stops nor undoes the
        others; a failed bill gets a chatter note. With the context key invoice_ocr_commit
        (the list action) every bill is committed when done, so a worker timeout does not
        lose finished bills. Returns a notification that says how many bills were filled,
        failed or skipped, and why.
        """
        results = []
        commit = self.env.context.get("invoice_ocr_commit") and not modules.module.current_test
        for move in self:
            if move.state != "draft" or move.move_type != "in_invoice":
                results.append((move, self._ocr_result("skipped", _("not a draft vendor bill"))))
                continue
            atts = self.env["ir.attachment"].search([
                ("res_model", "=", "account.move"),
                ("res_id", "=", move.id),
                ("mimetype", "=", "application/pdf"),
            ], order="id desc", limit=1)
            if not atts:
                results.append((move, self._ocr_result("skipped", _("no PDF attachment"))))
                continue
            # Use the most recent PDF attachment
            files_data = [{
                "filename": atts.name,
                "mimetype": atts.mimetype,
                "raw": atts.raw,
            }]
            results.append((move, self._invoice_ocr_extend_safe(move, files_data)))
            if commit:
                self.env.cr.commit()
        return self._ocr_notification(_("Invoice OCR"), results)

    @api.model
    def _ocr_notification(self, title, results):
        """A display_notification summarising [(record, outcome)] (see _ocr_result).

        Shared with hr_expense_ocr_ai. Failed and skipped records are listed with the
        reason; the current view is reloaded afterwards so filled values show.
        """
        counts = Counter(outcome["status"] for _rec, outcome in results)
        parts = [label for label in (
            counts["filled"] and _("%s filled", counts["filled"]),
            counts["failed"] and _("%s failed", counts["failed"]),
            counts["skipped"] and _("%s skipped", counts["skipped"]),
        ) if label] or [_("nothing selected")]
        details = [f"{rec.display_name}: {outcome['reason']}" for rec, outcome in results
                   if outcome["status"] != "filled" and outcome["reason"]]
        shown = 10
        if len(details) > shown:
            details = [*details[:shown], _("… and %s more", len(details) - shown)]
        # The notification is plain text (no line breaks): one sentence per record.
        message = ", ".join(parts) + (". " + "; ".join(details) if details else "")
        if counts["failed"]:
            kind = "warning"
        elif counts["filled"]:
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
        res = super()._extend_with_attachments(files_data, new)

        # Only run on draft vendor bills (and only when invoked at create time)
        if not new:
            return res
        # Odoo already imported an electronic invoice (UBL/Peppol, embedded
        # Factur-X/ZUGFeRD): lines, due date and payment terms come from it, and OCR would
        # only overwrite them with a worse reading of the PDF. A plain PDF has no decoder in
        # CE and gives res = None, so OCR runs as before.
        if res:
            return res
        filled = False
        for move in self:
            if move.move_type != "in_invoice":
                continue
            if move.state != "draft":
                continue
            # No commit here: _extend_with_attachments runs inside the create
            # transaction, so committing would also flush super()'s work and the
            # create itself. A failure rolls back only the OCR's own writes (savepoint).
            result = self._invoice_ocr_extend_safe(move, files_data)
            filled = filled or result["status"] == "filled"
        # Core's _create_records_from_attachments posts "There was an error while
        # importing the bill" when this returns a falsy value: only say "imported"
        # when OCR actually filled the bill.
        return True if filled else res

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

    @staticmethod
    def _ocr_result(status, reason=None):
        """Outcome of one OCR run: status "filled", "skipped" or "failed", and why."""
        return {"status": status, "reason": reason or ""}

    def _invoice_ocr_extend_safe(self, move, files_data):
        """_invoice_ocr_extend in a savepoint: a failure rolls back only the OCR's writes.

        An exception (an SQL error included) no longer leaves half-written partners or
        lines behind, nor an aborted transaction for the caller. A failed run is noted in
        the bill's chatter. Returns the run's outcome (see _ocr_result).
        """
        # The caller's own pending writes are flushed outside the try: an error there
        # belongs to the caller (and Odoo's retry loop), it must not be swallowed here.
        self.env.flush_all()
        try:
            with self.env.cr.savepoint():
                result = self._invoice_ocr_extend(move, files_data) or self._ocr_result("filled")
        except Exception as e:  # noqa: BLE001 — OCR must never break the upload
            logger.warning("OCR failed for move %s", move.id, exc_info=True)
            result = self._ocr_result("failed", str(e)[:300] or type(e).__name__)
        if result["status"] == "failed":
            move.message_post(
                body=_("OCR could not fill in this bill: %(reason)s. Fill it in by hand "
                       "or run OCR again.", reason=result["reason"]),
                message_type="comment",
            )
        return result

    def _invoice_ocr_extend(self, move, files_data):
        """Read the bill's PDF with OCR + AI and pre-fill it.

        Returns the outcome (see _ocr_result): "filled", or "skipped"/"failed" with the
        reason. Exceptions are left to the caller (_invoice_ocr_extend_safe).
        """
        ICP = self.env["ir.config_parameter"].sudo()
        if ICP.get_param("invoice_ocr.enabled", "True").lower() in ("false", "0", ""):
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
        own = self._ocr_own_from_config(cfg)
        try:
            data = invoice_ocr.extract_invoice_data(pdf_data, config=cfg)
        except Exception as e:
            logger.warning("invoice_ocr.extract_invoice_data failed: %s", e)
            return self._ocr_result(
                "failed", _("the PDF could not be read (%s)", str(e)[:300] or type(e).__name__))

        # If no useful data extracted, abort
        if not data or not (data.get("vendor_name") or data.get("invoice_number")):
            return self._ocr_result(
                "failed", _("neither a vendor name nor an invoice number was found in the PDF"))

        # ---- Marketplace VAT-declarer override ----------------------
        # For Amazon/eBay/etc. invoices, prefer "Moms deklarerat av X" /
        # "VAT declared by X" entity as vendor over "Sold by"-merchant.
        raw_text = data.get("raw_text") or ""
        for pattern in [
            r"Moms deklarerat av\s+([^\n]+?)(?:\s*Moms\s*#|$)",
            r"VAT declared by\s+([^\n]+?)(?:\s*VAT\s*#|$)",
            r"Tax collected by\s+([^\n]+?)(?:\s*$)",
        ]:
            m = re.search(pattern, raw_text, re.IGNORECASE)
            if m:
                declared_vendor = m.group(1).strip().rstrip(",.")
                if declared_vendor and len(declared_vendor) > 3:
                    data["vendor_name"] = declared_vendor
                    data.setdefault("_conflicts", []).append(
                        f"vendor_name: marketplace VAT-declarer override → {declared_vendor}")
                    break

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
        partner_id = self._resolve_partner_from_ocr(data, own=own, notes=notes)
        # ---- Build write vals ----------------------------------------
        vals = {}
        current_is_own = bool(move.partner_id) and self._ocr_is_own_partner(move.partner_id, own)
        if partner_id and (not move.partner_id or current_is_own):
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
            # Undantag: aldrig in i en låst period — då behåller vi Odoos default.
            company = move.company_id
            locks = [company.fiscalyear_lock_date, company.tax_lock_date,
                     getattr(company, "hard_lock_date", False)]
            lock = max([d for d in locks if d], default=None)
            inv_date = fields.Date.to_date(data["invoice_date"])
            if not lock or inv_date > lock:
                vals["date"] = inv_date
            else:
                logger.info(
                    "OCR: fakturadatum %s ligger i låst period (lås %s) — "
                    "behåller bokföringsdatum", inv_date, lock)
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

        if vals:
            move.write(vals)

        # Create lines from AI lines if move has none
        if not move.invoice_line_ids:
            self._create_lines_from_ocr(move, data)

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

    def _ocr_partner_search(self, domain, own, notes, how, limit=1):
        """res.partner.search that never returns the buyer's own company.

        If the search would only have hit the own company, that is noted in the chatter.
        """
        Partner = self.env["res.partner"]
        excl = [("id", "not in", own["partner_ids"]),
                ("commercial_partner_id", "not in", own["partner_ids"])]
        found = Partner.search(domain + excl, limit=limit)
        if not found and Partner.search_count(
                domain + [("commercial_partner_id", "in", own["partner_ids"])], limit=1):
            notes.append(_("%s pekade på det egna bolaget – hoppades över.") % how)
        return found

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

    def _resolve_partner_from_ocr(self, data, own=None, notes=None):
        """Match OCR-extracted vendor data to res.partner. Auto-create if needed.

        Never returns the receiving company's partner (or a contact under it): on a vendor
        bill the own company is the buyer, not the seller.
        """
        from ..lib import invoice_ocr

        Partner = self.env["res.partner"]
        own = own if own is not None else self._ocr_own_context()
        notes = notes if notes is not None else []
        own_keys = invoice_ocr.build_own_ids(own["ids"])
        own_names = invoice_ocr.build_own_names(own["names"])

        # 1. VAT (any country prefix already in OCR, or Swedish org number)
        org_raw = (data.get("org_number") or "").strip()
        if org_raw and invoice_ocr.is_own_id(org_raw, own_keys):
            # Lib:en filtrerar redan bort egna nummer; detta är ett extra skydd
            notes.append(_("Org.nr %s är bolagets eget – användes inte.") % org_raw)
            org_raw = ""
        # If looks like a VAT number with letter prefix (e.g. LU20260743, SE556...)
        if org_raw and re.match(r"^[A-Z]{2}\d", org_raw):
            p = self._ocr_partner_search([("vat", "=", org_raw)], own, notes,
                                         _("Momsreg.nr %s") % org_raw)
            if p:
                return p.id

        # 2. Swedish org number — multiple variants
        org_clean = re.sub(r"[^0-9]", "", org_raw)
        if org_clean:
            how = _("Org.nr %s") % org_raw
            for v in [f"SE{org_clean}01", f"SE{org_clean}", org_clean]:
                p = self._ocr_partner_search([("vat", "=", v)], own, [], how)
                if p:
                    return p.id
            p = self._ocr_partner_search([("vat", "ilike", org_clean)], own, notes, how)
            if p:
                return p.id

        # 3. Plusgiro / bankgiro
        for field in ("plusgiro", "bankgiro"):
            bg = (data.get(field) or "").strip()
            if not bg:
                continue
            bg_clean = re.sub(r"[^0-9]", "", bg)
            if not bg_clean or self._ocr_is_own_bank_number(bg_clean, own):
                continue  # det egna kontot säger inget om leverantören
            bank = self.env["res.partner.bank"].search(
                [("sanitized_acc_number", "ilike", bg_clean),
                 ("partner_id", "not in", own["partner_ids"]),
                 ("partner_id.commercial_partner_id", "not in", own["partner_ids"])],
                limit=1)
            if bank:
                return bank.partner_id.id

        # 4. Vendor name fuzzy
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
            # Exact match first
            p = self._ocr_partner_search(
                [("name", "=ilike", name), ("is_company", "=", True)], own, notes,
                _("Namnet \"%s\"") % name)
            if p:
                return p.id
            # Substring match — only if exactly one
            tokens = [t for t in re.split(r"\s+", name) if len(t) >= 4]
            for t in tokens:
                p = self._ocr_partner_search(
                    [("name", "ilike", t), ("is_company", "=", True)], own, [], "", limit=2)
                if len(p) == 1:
                    return p.id

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
            # Country guess from VAT prefix
            if vals.get("vat"):
                cc = vals["vat"][:2]
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
            return new_partner.id

        return None

    def _create_lines_from_ocr(self, move, data):
        """Create invoice_line_ids from AI-extracted data."""
        ai_lines = data.get("lines")

        # Build BAS-code → account.id cache
        Account = self.env["account.account"]

        def acct(code):
            if not code:
                return None
            a = Account.search([("code_store", "=", str(code))], limit=1)
            if a:
                return a.id
            # Fallback: prefix match on first 4 digits
            a = Account.search([("code_store", "=like", f"{str(code)[:4]}%")], limit=1)
            return a.id if a else None

        # Determine VAT context based on partner country
        EU_NON_SE = {
            "AT","BE","BG","HR","CY","CZ","DK","EE","FI","FR","DE","GR",
            "HU","IE","IT","LV","LT","LU","MT","NL","PL","PT","RO","SK",
            "SI","ES",
        }
        partner_cc = (move.partner_id.country_id.code or "").upper() if move.partner_id and move.partner_id.country_id else ""
        is_eu_foreign = partner_cc in EU_NON_SE
        is_outside_eu = bool(partner_cc) and partner_cc != "SE" and not is_eu_foreign

        def find_tax(name_substr, rate):
            """Find SE purchase tax by name substring + rate."""
            return self.env["account.tax"].search([
                ("type_tax_use", "=", "purchase"),
                ("amount", "=", rate),
                ("country_id.code", "=", "SE"),
                ("name", "ilike", name_substr),
            ], limit=1)

        # Alla svenska momssatser, inte bara 25 och 12. 6 % gäller bl.a. persontransport
        # (SJ, taxi, kollektivtrafik), böcker och tidningar — utan den raden hamnade
        # tågbiljetter helt utan moms och totalen stämde inte.
        RATES = (25, 12, 6)
        if is_eu_foreign:
            # EU reverse charge — services (S) by default; goods (G) used if doc indicates
            taxes = {r: find_tax(f"{r}% EU S", r) or find_tax("EU S", r) for r in RATES}
        elif is_outside_eu:
            # Export/import outside EU
            taxes = {r: find_tax(f"{r}% EX S", r) or find_tax("EX", r) for r in RATES}
        else:
            taxes = {r: self.env["account.tax"].search(
                [("type_tax_use", "=", "purchase"), ("amount", "=", r),
                 ("country_id.code", "=", "SE")], limit=1) for r in RATES}
        taxes = {r: t for r, t in taxes.items() if t}

        # For EU/EX: also remap account_code so domestic 4xxx → corresponding foreign account
        # e.g. 4000 (Sw goods) → 4515 (EU goods 25%) ; 6230-range services stay the same
        # Map by description heuristics done per-line below. The actual remap
        # lives in lib/invoice_ocr.remap_account_code so it can be unit-tested
        # without Odoo; the closure only carries the per-move country context.
        from ..lib import invoice_ocr as _ocr

        def remap_account_code(orig_code, line_desc=""):
            return _ocr.remap_account_code(
                orig_code, is_eu_foreign=is_eu_foreign,
                is_outside_eu=is_outside_eu, line_desc=line_desc)

        line_vals_list = []

        if ai_lines and isinstance(ai_lines, list) and len(ai_lines) > 0:
            for al in ai_lines:
                if not isinstance(al, dict):
                    continue
                # The line's amount is what the answer was checked against: quantity and
                # unit price only when they agree with it (#11).
                qty_price = _ocr.line_quantity_and_price(al)
                if not qty_price:
                    continue
                quantity, price_unit = qty_price
                description = str(al.get("description") or "")
                code = remap_account_code(al.get("account_code"), description)
                fallback = "4515" if is_eu_foreign else ("4545" if is_outside_eu else "4000")
                acc_id = acct(code) or acct(fallback)
                lv = {
                    "name": description or data.get("invoice_number") or "Faktura",
                    "quantity": quantity,
                    "price_unit": price_unit,
                    "account_id": acc_id,
                }
                vat_rate = al.get("vat_rate")
                # On EU reverse-charge invoices the AI sees "0%" but Odoo still needs
                # the 25% EU S tax to generate 2614/2645 entries. Default rate to 25.
                if (is_eu_foreign or is_outside_eu) and (vat_rate in (None, 0)):
                    vat_rate = 25
                try:
                    vat_rate = int(round(float(vat_rate))) if vat_rate is not None else None
                except (TypeError, ValueError):
                    vat_rate = None
                if vat_rate in taxes:
                    lv["tax_ids"] = [(6, 0, [taxes[vat_rate].id])]
                elif vat_rate not in (None, 0):
                    logger.warning(
                        "OCR: ingen inköpsmoms hittad för %s%% (%s) — raden får ingen moms",
                        vat_rate, description[:60])
                line_vals_list.append((0, 0, lv))
        else:
            # Single-line fallback from totals
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
            if subtotal and subtotal > 0:
                code = remap_account_code(data.get("account_code"))
                fallback = "4515" if is_eu_foreign else ("4545" if is_outside_eu else "4000")
                acc_id = acct(code) or acct(fallback)
                lv = {
                    "name": data.get("invoice_number") or "Faktura",
                    "quantity": 1,
                    "price_unit": subtotal,
                    "account_id": acc_id,
                }
                # Härled satsen ur tryckta belopp i stället för att anta 25 %. Ett kvitto på
                # 95,00 med 5,38 moms är 6 %, inte 25 % — avrunda till närmaste giltiga sats.
                rate = None
                if vat and subtotal:
                    pct = round(vat / subtotal * 100)
                    rate = min(taxes, key=lambda r: abs(r - pct), default=None)
                    if rate is not None and abs(rate - pct) > 2:
                        logger.warning(
                            "OCR: moms %.2f på netto %.2f ger %s%%, ingen giltig sats matchar",
                            vat, subtotal, pct)
                        rate = None
                # EU/utanför EU: momsen är 0 på fakturan men förvärvsmoms ska ändå bokas
                if rate is None and (is_eu_foreign or is_outside_eu):
                    rate = 25
                if rate in taxes:
                    lv["tax_ids"] = [(6, 0, [taxes[rate].id])]
                line_vals_list.append((0, 0, lv))

        if line_vals_list:
            move.write({"invoice_line_ids": line_vals_list})
            self._ocr_apply_total_adjustments(move, data)
            self._check_ocr_totals(move, data)

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
        account = self.env["account.account"].search(
            [*self.env["account.account"]._check_company_domain(move.company_id), ("code_store", "=", "3740")], limit=1)
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
        printed = data.get("_printed") or {}
        printed_net = printed.get("subtotal")
        printed_total = printed.get("total_amount")
        printed_vat = printed.get("vat_amount")
        if not printed:
            logger.info("OCR: inga tryckta belopp lasta ur PDF:en — "
                        "radsumman kan inte kontrolleras mot fakturan")
            return

        problems = []
        net = move.amount_untaxed - (data.get("_rounding_adjust") or 0.0)
        if printed_net is not None and abs(net - printed_net) > tol:
            problems.append(
                f"netto {net:.2f} mot fakturans {printed_net:.2f}")
        if printed_vat is not None and abs(move.amount_tax - printed_vat) > tol:
            problems.append(
                f"moms {move.amount_tax:.2f} mot fakturans {printed_vat:.2f}")
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
        tokens = set()
        for n in names:
            for t in re.split(r"[\s,.()/&-]+", str(n or "").lower()):
                if len(t) >= 3 and t not in _NAME_STOPWORDS and not t.isdigit():
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
            bank = self.env["res.partner.bank"].search([
                ("partner_id", "=", partner_id),
                ("sanitized_acc_number", "ilike", bg_clean),
                ("active", "=", True),
            ], limit=1)
            if bank:
                move.partner_bank_id = bank.id
                return
