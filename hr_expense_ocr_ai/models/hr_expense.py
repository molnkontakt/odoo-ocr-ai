import logging

import psycopg2
from markupsafe import Markup, escape

from odoo import _, api, fields, models, modules
from odoo.exceptions import UserError
from odoo.tools import html2plaintext, is_html_empty

logger = logging.getLogger(__name__)

# Longest category hint sent to the model (characters)
HINT_LIMIT = 200

VIEWABLE = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/tiff", "image/bmp", "application/pdf")


class HrExpense(models.Model):
    _inherit = "hr.expense"

    # ------------------------------------------------------------------ helpers
    @api.model
    def _expense_ocr_enabled(self):
        return self.env["ir.config_parameter"].sudo().get_param("expense_ocr.enabled", "True").lower() not in ("false", "0", "")

    def _expense_ocr_config(self):
        """Per-run config: same provider, keys and own-company guard as the invoice OCR
        (Settings → Invoicing → Invoice OCR), for this expense's company. Nothing is written to
        the invoice library's module globals, which every run in the worker shares."""
        self.ensure_one()
        return self.env["account.move"]._invoice_ocr_config(self.company_id)

    def _expense_ocr_categories(self):
        """[(kod, namn, hint)] + kod→produkt. Hinten är produktens inköpsbeskrivning, eller
        kategorins "Guideline" (product description, an HTML field) as plain text, so the
        administrator can steer the categorisation by describing the categories in Odoo.
        Empty editor content ('<p><br></p>') is no hint; a hint is capped at HINT_LIMIT."""
        self.ensure_one()
        products = self.env["product.product"].sudo().search([
            ("can_be_expensed", "=", True), ("company_id", "in", [False, self.company_id.id]),
        ])
        cats, by_code = [], {}
        for p in products:
            code = p.default_code or f"P{p.id}"
            hint = p.description_purchase or ""
            if not hint.strip() and not is_html_empty(p.description):
                hint = html2plaintext(p.description)
            hint = " ".join(hint.split())[:HINT_LIMIT]
            cats.append((code, p.name, hint))
            by_code[code] = p
        return cats, by_code

    def _expense_ocr_attachment(self):
        self.ensure_one()
        main = self.message_main_attachment_id
        if main and (main.mimetype or "").lower() in VIEWABLE:
            return main
        return self.env["ir.attachment"].search([
            ("res_model", "=", "hr.expense"), ("res_id", "=", self.id), ("mimetype", "in", VIEWABLE),
        ], order="id desc", limit=1)

    # ------------------------------------------------------------------ core
    def action_read_receipt(self, force=False):
        """Läs kvittot och fyll tomma fält. `force` skriver över belopp/datum/kategori/namn.

        Every failure reaches the user as a UserError with a readable message (unreadable
        image or PDF, tesseract missing, a value the write rejects), not as a server error.
        """
        for expense in self:
            att, why_not = expense._expense_ocr_target()
            if not att:
                raise UserError(why_not)
            expense._expense_ocr_read_or_raise(att, force=force)
        return True

    def action_read_receipt_bulk(self):
        """The list action: read every selected receipt and say how it went.

        Each expense is read in its own savepoint (see _expense_ocr_try), so one failure
        neither stops nor undoes the others, and a failed expense gets a chatter note.
        With the context key expense_ocr_commit (the list action) every expense is
        committed when done, so a worker timeout does not lose finished ones. Returns a
        notification that says how many were filled, failed or skipped, and why.
        """
        commit = self.env.context.get("expense_ocr_commit") and not modules.module.current_test
        results = self._expense_ocr_try("bulk", commit=commit)
        return self.env["account.move"]._ocr_notification(_("Receipt OCR"), results)

    def _expense_ocr_target(self):
        """(attachment, None) when OCR can read this expense, else (None, the reason)."""
        self.ensure_one()
        if self.state != "draft":
            return None, _("Kvitto-OCR kan bara köras på utkast.")
        att = self._expense_ocr_attachment()
        if not att:
            return None, _("Ingen bild- eller PDF-bilaga på utlägget.")
        return att, None

    def _expense_ocr_read_or_raise(self, att, force=False):
        """_expense_ocr_read with every failure turned into a readable UserError."""
        self.ensure_one()
        try:
            return self._expense_ocr_read(att, force=force)
        except (UserError, psycopg2.Error):
            raise  # database errors stay as they are (Odoo's retry loop needs them)
        except Exception as e:  # noqa: BLE001 — shown to the user, see action_read_receipt
            logger.warning("Receipt OCR failed for expense %s", self.id, exc_info=True)
            raise UserError(_("Receipt OCR failed for %(name)s: %(error)s",
                              name=att.name, error=str(e)[:300] or type(e).__name__)) from e

    def _expense_ocr_read(self, att, force=False):
        """Read `att` and fill the expense. Returns the outcome (see account.move._ocr_result)."""
        from ..lib import receipt_ocr

        self.ensure_one()
        Move = self.env["account.move"]
        cfg = self._expense_ocr_config()
        cats, by_code = self._expense_ocr_categories()
        result = receipt_ocr.extract_receipt_data(att.raw, att.mimetype, att.name, categories=cats, config=cfg)
        filled = self._expense_ocr_apply(result, by_code, att, force=force)
        if result.get("source") == "none":
            return Move._ocr_result("failed", _("nothing could be read from %s", att.name))
        if not filled:
            return Move._ocr_result("skipped", _("nothing was filled (see the chatter note)"))
        return Move._ocr_result("filled")

    def _expense_ocr_apply(self, result, by_code, att, force=False):
        from ..lib import receipt_ocr

        self.ensure_one()
        f = result.get("fields") or {}
        notes = list(result.get("notes") or [])
        vals, filled = {}, []
        today = fields.Date.context_today(self)
        if f.get("date") and (force or not self.date or self.date == today):
            # Only a real calendar date is written: anything else would make the write
            # raise and lose every other value read from the receipt.
            day = receipt_ocr.inv.iso_date(f["date"])
            if day:
                vals["date"] = day
                filled.append(_("datum %s", day))
            else:
                notes.append(_("the date %s is not a valid date – not used", f["date"]))
        if f.get("category_code") and (force or not self.product_id):
            product = by_code.get(f["category_code"])
            if product:
                vals["product_id"] = product.id
                filled.append(_("kategori %s", product.name))
        if f.get("total") is not None and (force or not self.total_amount_currency):
            self._expense_ocr_amount_vals(f, vals, filled, notes)
        merchant, items, number = f.get("merchant"), f.get("items"), f.get("receipt_number")
        label = " ".join(x for x in (merchant, _("kvitto %s", number) if number else None) if x)
        if items:
            label = f"{label} — {items}" if label else items
        current = (self.name or "").strip()
        if label and (force or len(current) <= 3):
            vals["name"] = label
            filled.append(_("beskrivning"))
        elif label and current.startswith("OKÄND AVSÄNDARE") and label.lower() not in current.lower():
            vals["name"] = f"{current} — {label}"
            filled.append(_("beskrivning"))
        if vals:
            self.write(vals)
        vat_note = self._expense_ocr_vat_note(f, result.get("text") or "")
        if vat_note:
            notes.append(vat_note)

        # chatter
        if result.get("source") == "none":
            body = Markup("<p><b>Kvitto-OCR</b>: kunde inte läsa någon text ur %s.</p>") % att.name
        else:
            rows = Markup("").join(
                Markup("<li>%s: <code>%s</code></li>") % (k, v)
                for k, v in f.items() if k != "confidence" and v not in (None, "")
            )
            conf = f.get("confidence")
            body = Markup("<p><b>Kvitto-OCR</b> läste %s (%s%s)</p><ul>%s</ul>") % (
                att.name, result.get("source"), Markup(", konfidens %.2f") % conf if conf is not None else "", rows)
            body += Markup("<p>Ifyllt: %s</p>") % (", ".join(filled) if filled else _("inget (fälten var redan satta)"))
            if notes:
                body += Markup("<p><b>Anmärkningar:</b> %s</p>") % escape("; ".join(notes))
        self.message_post(body=body, message_type="comment", subtype_xmlid="mail.mt_note")
        return filled

    def _expense_ocr_vat_note(self, f, text):
        """A note when the receipt's printed VAT and the category's tax differ by more than 1
        (#33), else None. The tax is never changed: a receipt can mix rates, and choosing the
        tax is the reviewer's call.

        Only compared when the VAT amount is printed on the receipt (not a model guess), the
        expense has a tax, and its amount is the receipt's total (else the bases differ). Also
        when the category or the amount was set by hand.
        """
        from ..lib import receipt_ocr

        self.ensure_one()
        vat, total = receipt_ocr.inv._num(f.get("vat_amount")), receipt_ocr.inv._num(f.get("total"))
        if vat is None or total is None or not self.tax_ids:
            return None
        if not receipt_ocr.inv.amount_in_text(vat, text):
            return None
        if abs(self.total_amount_currency - total) >= 0.005:
            return None
        if abs(self.tax_amount_currency - vat) <= 1.0:
            return None
        return _("the receipt shows VAT %(printed).2f, the category's tax gives %(computed).2f – "
                 "check the VAT rate", printed=vat, computed=self.tax_amount_currency)

    def _expense_ocr_amount_vals(self, f, vals, filled, notes):
        """Add the receipt's total, in the receipt's currency, to `vals` (#28).

        The currency is set in the same write as the amount (the expense's rate then follows
        the receipt date), and the amount is rounded with that currency's rounding. A
        currency that cannot be used (unknown, inactive, no rate) leaves the amount empty with
        a note. A category with a fixed cost is left alone: its amount is quantity × cost,
        in the company's currency.
        """
        product = self.env["product.product"].browse(vals["product_id"]) if "product_id" in vals \
            else self.product_id
        cost = product.with_company(self.company_id).standard_price if product else 0.0
        if product and not self.company_currency_id.is_zero(cost):
            notes.append(_("the category %s has a fixed cost: the amount is quantity × cost and "
                           "was not filled", product.name))
            return
        date = vals.get("date") or self.date
        currency, problem = self.env["account.move"]._ocr_currency(
            f.get("currency"), self.company_id, date)
        if problem:
            notes.append(_("the receipt is in %(currency)s, but %(problem)s – the amount was not "
                           "filled", currency=f.get("currency"), problem=problem))
            return
        currency = currency or self.currency_id
        vals["total_amount_currency"] = currency.round(float(f["total"]))
        if currency != self.currency_id:
            vals["currency_id"] = currency.id
        filled.append(_("belopp %s", f"{vals['total_amount_currency']} {currency.name}"
                        if currency != self.company_currency_id else vals["total_amount_currency"]))

    def _expense_ocr_try(self, reason, commit=False):
        """OCR får aldrig fälla det som utlöste den (mailhämtning, uppladdning).

        Each expense is read in its own savepoint: a failure (an SQL error included) rolls
        back only that read, in the database and in the ORM cache, leaves the caller's
        transaction usable and is noted in the expense's chatter. The caller's pending
        writes are flushed first, outside the try, so a concurrency error on them still
        reaches Odoo's retry loop instead of being swallowed here. `commit` commits after
        each expense (the list action only). Returns [(expense, outcome)].
        """
        Move = self.env["account.move"]
        self.env.flush_all()
        results = []
        for expense in self:
            att, why_not = expense._expense_ocr_target()
            if not att:
                results.append((expense, Move._ocr_result("skipped", why_not)))
                continue
            try:
                with self.env.cr.savepoint():
                    outcome = expense._expense_ocr_read_or_raise(att)
            except Exception as e:  # noqa: BLE001
                logger.warning("Kvitto-OCR (%s) misslyckades för utlägg %s: %s", reason, expense.id, e)
                message = expense._expense_ocr_error_message(e)
                expense.message_post(body=message, message_type="comment", subtype_xmlid="mail.mt_note")
                outcome = Move._ocr_result("failed", message)
            results.append((expense, outcome))
            if commit:
                self.env.cr.commit()
        return results

    @api.model
    def _expense_ocr_error_message(self, error):
        """The user-facing text of a failed read (UserError text as is)."""
        if isinstance(error, UserError) and error.args:
            return error.args[0]
        return _("Receipt OCR failed: %s", str(error)[:300] or type(error).__name__)

    # ------------------------------------------------------------------ trigger
    # En enda utlösare räcker för både mail och MCP: när utkastet får en (ny) huvudbilaga. Vid
    # inmailade utlägg hängs bilagorna på EFTER message_new (mail_thread postar meddelandet
    # efteråt och sätter då huvudbilagan), så en hook i message_new ser inga bilagor. Läsningen
    # körs bara när det finns något att fylla i: belopp 0 eller ingen kategori.
    def write(self, vals):
        before = {e.id: (e.state, e.total_amount_currency, e.product_id.id, e.message_main_attachment_id.id) for e in self} if "message_main_attachment_id" in vals else {}
        res = super().write(vals)
        if before and self._expense_ocr_enabled() and not self.env.context.get("expense_ocr_skip"):
            for expense in self:
                state, total, product, old_att = before[expense.id]
                att = expense.message_main_attachment_id
                if state == "draft" and (not total or not product) and att and att.id != old_att and (att.mimetype or "").lower() in VIEWABLE:
                    expense.with_context(expense_ocr_skip=True)._expense_ocr_try("bilaga")
        return res
