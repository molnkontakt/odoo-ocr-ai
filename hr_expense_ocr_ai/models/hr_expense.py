import logging

import psycopg2
from markupsafe import Markup

from odoo import Command, _, api, fields, models
from odoo.exceptions import UserError
from odoo.tools import html2plaintext, is_html_empty
from odoo.tools.misc import formatLang

logger = logging.getLogger(__name__)

# Longest category hint sent to the model (characters)
HINT_LIMIT = 200

VIEWABLE = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/tiff", "image/bmp", "application/pdf")


class HrExpense(models.Model):
    _name = "hr.expense"
    _inherit = ["hr.expense", "ocr.queue.mixin"]

    # ------------------------------------------------------------------ helpers
    @api.model
    def _expense_ocr_enabled(self):
        return self.env["ir.config_parameter"].sudo().get_param("expense_ocr.enabled", "True").lower() not in ("false", "0", "")

    def _expense_ocr_config(self):
        """Per-run config: same provider, keys and own-company guard as the invoice OCR
        (Settings → Invoicing → Invoice OCR), for this expense's company, and today's date in
        the user's time zone (a receipt's date must be near it). Nothing is written to the
        invoice library's module globals, which every run in the worker shares."""
        self.ensure_one()
        cfg = self.env["account.move"]._invoice_ocr_config(self.company_id)
        cfg["today"] = fields.Date.context_today(self)
        return cfg

    def _expense_ocr_categories(self):
        """[(code, name, hint)] and code → product. The hint is the product's purchase
        description, or the category's "Guideline" (product description, an HTML field) as
        plain text, so the administrator can steer the categorisation by describing the
        categories in Odoo. Empty editor content ('<p><br></p>') is no hint; a hint is capped
        at HINT_LIMIT.

        A category with a fixed cost (mileage: quantity × cost) is never offered, so never
        chosen for a receipt: fuel put on the mileage category became an expense of 1.00
        (#39)."""
        self.ensure_one()
        products = self.env["product.product"].sudo().search([
            ("can_be_expensed", "=", True), ("company_id", "in", [False, self.company_id.id]),
        ])
        currency = self.company_id.currency_id
        products = products.filtered(lambda p: currency.is_zero(
            p.with_company(self.company_id).standard_price))
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
        """Read the receipt and fill the empty fields. `force` overwrites amount, date,
        category and description.

        The form button: reads at once (bounded by the document deadline), and the OCR
        state shows the outcome. Every failure reaches the user as a UserError with a
        readable message (unreadable image or PDF, tesseract missing, a value the write
        rejects), not as a server error. A receipt the background job is reading is not
        read twice.
        """
        for expense in self:
            if expense.ocr_state == "running":
                raise UserError(_("OCR is already reading this receipt in the background. "
                                  "Wait a moment and reload the page."))
            att, why_not = expense._expense_ocr_target()
            if not att:
                raise UserError(why_not)
            outcome = expense._expense_ocr_read_or_raise(att, force=force)
            expense._ocr_queue_record_sync(outcome)
        return True

    def action_read_receipt_bulk(self):
        """The list action: queue the selected receipts for OCR and say so.

        Nothing is read in the request (a worker would hit Odoo's time limit after a few
        receipts, #29): the OCR cron reads them within seconds, one by one, and the outcome
        shows in the OCR state, the "OCR failed" filter and the chatter. Returns a
        notification with how many were queued or skipped, and why.
        """
        results = []
        queued = self.browse()
        for expense in self:
            att, why_not = expense._expense_ocr_target()
            if expense.ocr_state == "running":
                results.append((expense, self._ocr_result(
                    "skipped", _("OCR is already reading it in the background"))))
            elif not att:
                results.append((expense, self._ocr_result("skipped", why_not)))
            else:
                expense._ocr_enqueue(att)
                queued |= expense
                results.append((expense, self._ocr_result("queued")))
        if queued:
            self._ocr_queue_trigger()
        return self.env["account.move"]._ocr_notification(_("Receipt OCR"), results)

    # The queue (ocr.queue.mixin)

    def _ocr_queue_skip_reason(self):
        if self.state != "draft":
            return _("not a draft expense")
        return None

    def _ocr_queue_default_user(self):
        """An expense that came in by e-mail (the mail gateway runs as OdooBot) is read as
        its employee's user."""
        return self._ocr_real_user(self.sudo().employee_id.user_id)

    def _ocr_queue_read(self, final=True):
        att = self.ocr_attachment_id
        if not (att and att.res_model == "hr.expense" and att.res_id == self.id
                and (att.mimetype or "").lower() in VIEWABLE):
            att = self._expense_ocr_attachment()
        if not att:
            return self._ocr_result("skipped", _("no image or PDF attachment"))
        return self._expense_ocr_read_or_raise(att, final=final)

    def _expense_ocr_target(self):
        """(attachment, None) when OCR can read this expense, else (None, the reason)."""
        self.ensure_one()
        if self.state != "draft":
            return None, _("Receipt OCR can only read draft expenses.")
        att = self._expense_ocr_attachment()
        if not att:
            return None, _("The expense has no image or PDF attachment.")
        return att, None

    def _expense_ocr_read_or_raise(self, att, force=False, final=True):
        """_expense_ocr_read with every failure turned into a readable UserError."""
        self.ensure_one()
        try:
            return self._expense_ocr_read(att, force=force, final=final)
        except (UserError, psycopg2.Error):
            raise  # database errors stay as they are (Odoo's retry loop needs them)
        except Exception as e:  # noqa: BLE001 — shown to the user, see action_read_receipt
            logger.warning("Receipt OCR failed for expense %s", self.id, exc_info=True)
            raise UserError(_("Receipt OCR failed for %(name)s: %(error)s",
                              name=att.name, error=self._ocr_error_reason(e))) from e

    def _expense_ocr_read(self, att, force=False, final=True):
        """Read `att` and fill the expense. Returns the outcome (see ocr.queue.mixin._ocr_result).

        When the AI call failed (provider down, time limit) and this is not the `final`
        attempt of the background job, nothing is written and the outcome asks for a retry;
        otherwise what the regex read is filled in, the note says that the AI failed and
        the outcome is "failed".
        """
        from ..lib import receipt_ocr

        self.ensure_one()
        cfg = self._expense_ocr_config()
        cats, by_code = self._expense_ocr_categories()
        result = receipt_ocr.extract_receipt_data(att.raw, att.mimetype, att.name, categories=cats, config=cfg)
        ai_error = self._ocr_note_text(result.get("ai_error")) or None
        if ai_error and not final:
            return self._ocr_result("failed", _("the AI step failed (%s)", ai_error), retry=True)
        filled = self._expense_ocr_apply(result, by_code, att, force=force)
        if result.get("source") == "none":
            # the read note already says so (and why, when a budget cut the reading)
            return self._ocr_result("failed", _("nothing could be read from %s", att.name),
                                    noted=True)
        if ai_error:
            return self._ocr_result("failed", _("the AI step failed (%s); only the values read "
                                                "from the text were filled in", ai_error),
                                    noted=True)
        if not filled:
            # Read, but every field was already set: a read like any other (the chatter
            # note lists what the receipt says), not a document OCR left alone.
            return self._ocr_result("filled", _("nothing was filled: the fields were already "
                                                "set (see the chatter note)"))
        return self._ocr_result("filled")

    def _expense_ocr_apply(self, result, by_code, att, force=False):
        from ..lib import receipt_ocr

        self.ensure_one()
        f = result.get("fields") or {}
        notes = self._ocr_notes_text(result.get("notes"))
        vals, filled = {}, []
        today = fields.Date.context_today(self)
        if f.get("date") and (force or not self.date or self.date == today):
            # Only a real calendar date is written: anything else would make the write
            # raise and lose every other value read from the receipt.
            day = receipt_ocr.inv.iso_date(f["date"])
            if day:
                vals["date"] = day
                filled.append(_("date %s", day))
            else:
                notes.append(_("the date %s is not a valid date – not used", f["date"]))
        placeholder = self._expense_ocr_placeholders()
        if f.get("category_code") and (force or not self.product_id or placeholder["product"]):
            product = by_code.get(f["category_code"])
            if product and product != self.product_id:
                vals["product_id"] = product.id
                filled.append(_("category %s", product.name))
        if f.get("total") is not None and (force or not self.total_amount_currency):
            self._expense_ocr_amount_vals(f, vals, filled, notes)
        merchant, items, number = f.get("merchant"), f.get("items"), f.get("receipt_number")
        label = " ".join(x for x in (merchant, _("receipt %s", number) if number else None) if x)
        if items:
            label = f"{label} — {items}" if label else items
        current = (self.name or "").strip()
        if label and (force or len(current) <= 3 or placeholder["name"]):
            vals["name"] = label
            filled.append(_("description"))
        elif label and placeholder["name_prefix"] and label.lower() not in current.lower():
            vals["name"] = f"{current} — {label}"
            filled.append(_("description"))
        not_registered = self.company_id.ocr_not_vat_registered
        if not_registered and vals and (self.tax_ids or "product_id" in vals):
            # No input VAT to deduct (#39): the receipt's total is the cost, without a tax
            vals["tax_ids"] = [Command.clear()]
            notes.append(_("the company is not VAT-registered: the amount includes the VAT and "
                           "the expense has no tax"))
        if vals:
            self.write(vals)
        vat_note = None if not_registered else self._expense_ocr_vat_note(
            f, result.get("text") or "")
        if vat_note:
            notes.append(vat_note)
        self.message_post(body=self._expense_ocr_note(result, att, filled, notes),
                          message_type="comment", subtype_xmlid="mail.mt_note")
        return filled

    @api.model
    def _expense_ocr_field_labels(self):
        """The receipt's fields in the chatter note, in order, with their labels."""
        return {
            "merchant": _("Merchant"), "receipt_number": _("Receipt number"), "date": _("Date"),
            "total": _("Total"), "vat_amount": _("VAT"), "currency": _("Currency"),
            "items": _("Items"), "card_last4": _("Card (last four digits)"),
            "category_code": _("Category"),
        }

    def _expense_ocr_note(self, result, att, filled, notes):
        """The chatter note: what was read, by which model, what was filled, and why not."""
        f = result.get("fields") or {}
        if result.get("source") == "none":
            if len((result.get("text") or "").strip()) >= 15:
                body = Markup("<p><b>%s</b>: %s</p>") % (
                    _("Receipt OCR"), _("read the text of %s, but found nothing to fill in.",
                                        att.name))
            else:
                body = Markup("<p><b>%s</b>: %s</p>") % (
                    _("Receipt OCR"), _("could not read any text from %s.", att.name))
        else:
            labels = self._expense_ocr_field_labels()
            rows = Markup("").join(
                Markup("<li>%s: <code>%s</code></li>") % (labels[k], f[k])
                for k in labels if f.get(k) not in (None, "")
            )
            how = [_("AI") if result.get("source") == "ai" else _("text patterns only")]
            if f.get("confidence") is not None:
                how.append(_("confidence %s", f"{f['confidence']:.2f}"))
            ai = result.get("ai") or {}
            model = ai.get("_served_model") or ai.get("_model")
            if model:
                tokens = ai.get("_completion_tokens")
                how.append(_("model %(model)s, %(tokens)s completion tokens", model=model,
                             tokens=tokens if tokens is not None else "?"))
            body = Markup("<p><b>%s</b>: %s</p><ul>%s</ul>") % (
                _("Receipt OCR"), _("read %(name)s (%(how)s)", name=att.name, how="; ".join(how)),
                rows)
            body += Markup("<p>%s</p>") % _(
                "Filled in: %s", ", ".join(filled) if filled
                else _("nothing (the fields were already set)"))
        # Also when nothing was read: a budget that cut the reading (#26) says why.
        if notes:
            body += Markup("<p><b>%s</b> %s</p>") % (_("Notes:"), "; ".join(notes))
        return body

    def _expense_ocr_placeholders(self):
        """Which values are placeholders that the receipt may replace (#34).

        * product: the category Upload puts on every expense (create_expense_from_attachments:
          the EXP_GEN product, or the first expensable product when the name is still the
          untitled placeholder);
        * name: Upload's "Untitled Expense <date>";
        * name_prefix: the name starts with one of the prefixes in the system parameter
          expense_ocr.placeholder_name_prefixes (comma-separated, e.g. the subject prefix of
          a mail alias for unknown senders): the receipt's description is appended.
        """
        self.ensure_one()
        name = (self.name or "").strip()
        # Upload names the expense in the uploader's language, and the OCR job does not run
        # in it: every installed language counts.
        langs = {code for code, _name in self.env["res.lang"].get_installed()}
        untitled = {self.with_context(lang=lang)._get_untitled_expense_name("").strip()
                    for lang in langs | {self.env.lang or "en_US", "en_US"}}
        name_is_untitled = any(prefix and name.startswith(prefix) for prefix in untitled)
        product = self.product_id
        upload_product = False
        if product and name_is_untitled:
            expensable = self.env["product.product"].search([("can_be_expensed", "=", True)])
            upload_product = product == (
                expensable.filtered(lambda p: p.default_code == "EXP_GEN")[:1] or expensable[:1])
        param = self.env["ir.config_parameter"].sudo().get_param(
            "expense_ocr.placeholder_name_prefixes") or ""
        prefixes = [p.strip() for p in param.split(",") if p.strip()]
        return {
            "product": bool(product) and (product.default_code == "EXP_GEN" or upload_product),
            "name": name_is_untitled,
            "name_prefix": any(name.startswith(prefix) for prefix in prefixes),
        }

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
        return _("the receipt shows VAT %(printed)s, the category's tax gives %(computed)s – "
                 "check the VAT rate",
                 printed=formatLang(self.env, vat, currency_obj=self.currency_id),
                 computed=formatLang(self.env, self.tax_amount_currency,
                                     currency_obj=self.currency_id))

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
        filled.append(_("amount %s", formatLang(self.env, vals["total_amount_currency"],
                                                currency_obj=currency)))

    # ------------------------------------------------------------------ trigger
    # One trigger serves e-mail and the API: the draft gets a (new) main attachment. On
    # e-mailed expenses the attachments are added AFTER message_new (mail_thread posts the
    # message afterwards and sets the main attachment then), so a hook in message_new sees no
    # attachments. The receipt is only read when there is something to fill: amount 0 or no
    # category.
    #
    # The trigger only queues the expense (#29): the OCR cron reads it within seconds, so the
    # upload, the mail fetch or the API call that set the attachment returns at once.
    @api.model
    def _expense_ocr_wanted(self, state, total, product, att):
        """Whether a receipt that just became the main attachment is to be read."""
        return (state == "draft" and (not total or not product) and bool(att)
                and (att.mimetype or "").lower() in VIEWABLE)

    def write(self, vals):
        before = {e.id: (e.state, e.total_amount_currency, e.product_id.id, e.message_main_attachment_id.id) for e in self} if "message_main_attachment_id" in vals else {}
        res = super().write(vals)
        if before and not self.env.context.get("expense_ocr_skip") and self._expense_ocr_enabled():
            queued = self.browse()
            for expense in self:
                state, total, product, old_att = before[expense.id]
                att = expense.message_main_attachment_id
                if att.id != old_att and self._expense_ocr_wanted(state, total, product, att):
                    expense._ocr_enqueue(att)
                    queued |= expense
            if queued:
                self._ocr_queue_trigger()
        return res

    @api.model_create_multi
    def create(self, vals_list):
        """A receipt set as the main attachment at creation is queued too (#36.3), like one
        set by a later write."""
        expenses = super().create(vals_list)
        if not self.env.context.get("expense_ocr_skip") and any(
                vals.get("message_main_attachment_id") for vals in vals_list) \
                and self._expense_ocr_enabled():
            queued = self.browse()
            for expense, vals in zip(expenses, vals_list, strict=True):
                att = expense.message_main_attachment_id
                if vals.get("message_main_attachment_id") and self._expense_ocr_wanted(
                        expense.state, expense.total_amount_currency, expense.product_id, att):
                    expense._ocr_enqueue(att)
                    queued |= expense
            if queued:
                self._ocr_queue_trigger()
        return expenses
