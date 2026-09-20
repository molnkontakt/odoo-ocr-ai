import logging

from markupsafe import Markup, escape

from odoo import _, api, fields, models
from odoo.exceptions import UserError

logger = logging.getLogger(__name__)

VIEWABLE = ("image/jpeg", "image/jpg", "image/png", "image/webp", "image/tiff", "image/bmp", "application/pdf")


class HrExpense(models.Model):
    _inherit = "hr.expense"

    # ------------------------------------------------------------------ helpers
    @api.model
    def _expense_ocr_enabled(self):
        return self.env["ir.config_parameter"].sudo().get_param("expense_ocr.enabled", "True").lower() not in ("false", "0", "")

    def _expense_ocr_inject_settings(self):
        """Same provider, keys and own-company guard as the invoice OCR (Settings → Invoicing → Invoice OCR)."""
        self.env["account.move"]._invoice_ocr_apply_settings(self.company_id if len(self) == 1 else None)

    def _expense_ocr_categories(self):
        """[(kod, namn, hint)] + kod→produkt. Hinten är produktens inköpsbeskrivning, så kassören
        kan styra kategoriseringen genom att beskriva kategorierna i Odoo."""
        self.ensure_one()
        products = self.env["product.product"].sudo().search([
            ("can_be_expensed", "=", True), ("company_id", "in", [False, self.company_id.id]),
        ])
        cats, by_code = [], {}
        for p in products:
            code = p.default_code or f"P{p.id}"
            hint = (p.description_purchase or p.description or "").strip().replace("\n", " ")
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
        """Läs kvittot och fyll tomma fält. `force` skriver över belopp/datum/kategori/namn."""
        from ..lib import receipt_ocr
        for expense in self:
            if expense.state != "draft":
                raise UserError(_("Kvitto-OCR kan bara köras på utkast."))
            att = expense._expense_ocr_attachment()
            if not att:
                raise UserError(_("Ingen bild- eller PDF-bilaga på utlägget."))
            self._expense_ocr_inject_settings()
            cats, by_code = expense._expense_ocr_categories()
            result = receipt_ocr.extract_receipt_data(att.raw, att.mimetype, att.name, categories=cats)
            expense._expense_ocr_apply(result, by_code, att, force=force)
        return True

    def _expense_ocr_apply(self, result, by_code, att, force=False):
        self.ensure_one()
        f = result.get("fields") or {}
        notes = list(result.get("notes") or [])
        vals, filled = {}, []
        today = fields.Date.context_today(self)
        if f.get("total") is not None and (force or not self.total_amount_currency):
            vals["total_amount_currency"] = round(float(f["total"]), 2)
            filled.append(_("belopp %s", vals["total_amount_currency"]))
        if f.get("date") and (force or not self.date or self.date == today):
            vals["date"] = f["date"]
            filled.append(_("datum %s", f["date"]))
        if f.get("category_code") and (force or not self.product_id):
            product = by_code.get(f["category_code"])
            if product:
                vals["product_id"] = product.id
                filled.append(_("kategori %s", product.name))
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

    def _expense_ocr_try(self, reason):
        """OCR får aldrig fälla det som utlöste den (mailhämtning, uppladdning)."""
        for expense in self:
            try:
                expense.action_read_receipt()
            except Exception as e:  # noqa: BLE001
                logger.warning("Kvitto-OCR (%s) misslyckades för utlägg %s: %s", reason, expense.id, e)

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
