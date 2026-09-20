"""Run OCR + AI on PDFs uploaded via the 'Ladda upp'-button to pre-fill
vendor bill fields (invoice_date, ref, partner, amounts, lines).

Hooks into account.move._extend_with_attachments which is called both when:
  - User uploads via the journal's "Ladda upp"-button
  - PDF is attached to a draft vendor bill via chatter
"""

import base64
import logging
import re

from markupsafe import Markup

from odoo import _, api, fields, models
from odoo.exceptions import UserError

logger = logging.getLogger(__name__)


# --- BAS account fallbacks per VAT rate (domestic purchases) ------------------

ACCOUNT_FALLBACKS = {
    # Domestic purchases
    25: 4000,    # 25% Sweden goods/services default
    12: 4000,
    6: 4000,
    0: 4000,
}


class AccountMove(models.Model):
    _inherit = "account.move"

    def action_run_ocr(self):
        """Re-run OCR + AI on the latest PDF attachment of this draft bill."""
        for move in self:
            if move.state != "draft":
                raise UserError(_("OCR kan bara köras på utkast."))
            atts = self.env["ir.attachment"].search([
                ("res_model", "=", "account.move"),
                ("res_id", "=", move.id),
                ("mimetype", "=", "application/pdf"),
            ], order="id desc")
            if not atts:
                raise UserError(_("Ingen PDF-bilaga hittad på fakturan."))
            # Use the most recent PDF attachment
            files_data = [{
                "filename": atts[0].name,
                "mimetype": atts[0].mimetype,
                "raw": atts[0].raw,
            }]
            try:
                self._invoice_ocr_extend(move, files_data)
            except Exception as e:
                logger.warning("Manual OCR re-run failed for move %s: %s", move.id, e)
                raise UserError(_("OCR misslyckades: %s") % e) from e
        return True

    def _extend_with_attachments(self, files_data, new=False):
        res = super()._extend_with_attachments(files_data, new)

        # Only run on draft vendor bills (and only when invoked at create time)
        if not new:
            return res
        for move in self:
            if move.move_type != "in_invoice":
                continue
            if move.state != "draft":
                continue
            try:
                self._invoice_ocr_extend(move, files_data)
                # Commit per move so a later timeout doesn't lose prior OCR work
                self.env.cr.commit()
            except Exception as e:
                logger.warning("OCR auto-fill failed for move %s: %s", move.id, e)

        return res

    # ------------------------------------------------------------------
    # OCR + AI fill
    # ------------------------------------------------------------------

    @api.model
    def _invoice_ocr_apply_settings(self, company=None):
        """Push Odoo's settings into the library module and return it.

        Shared with hr_expense_ocr_ai. System parameters win over environment defaults; the
        receiving company's name and VAT number go along so they are never taken for the
        supplier.
        """
        from ..lib import invoice_ocr

        ICP = self.env["ir.config_parameter"].sudo()

        def param(key, default):
            return ICP.get_param(key) or default

        invoice_ocr.AI_PROVIDER = param("invoice_ocr.provider", invoice_ocr.AI_PROVIDER)
        invoice_ocr.STAIK_API_KEY = param("invoice_ocr.staik_api_key", invoice_ocr.STAIK_API_KEY)
        invoice_ocr.STAIK_MODEL = param("invoice_ocr.staik_model", invoice_ocr.STAIK_MODEL)
        invoice_ocr.VENICE_API_KEY = param("invoice_ocr.venice_api_key", invoice_ocr.VENICE_API_KEY)
        invoice_ocr.VENICE_MODEL = param("invoice_ocr.venice_model", invoice_ocr.VENICE_MODEL)
        invoice_ocr.OPENAI_API_KEY = param("invoice_ocr.openai_api_key", invoice_ocr.OPENAI_API_KEY)
        invoice_ocr.OPENAI_MODEL = param("invoice_ocr.openai_model", invoice_ocr.OPENAI_MODEL)
        invoice_ocr.AI_BASE_URL = param("invoice_ocr.base_url", invoice_ocr.AI_BASE_URL)
        invoice_ocr.AI_API_KEY = param("invoice_ocr.api_key", invoice_ocr.AI_API_KEY)
        invoice_ocr.AI_MODEL = param("invoice_ocr.model", invoice_ocr.AI_MODEL)
        invoice_ocr.OLLAMA_URL = param("invoice_ocr.ollama_url", invoice_ocr.OLLAMA_URL)
        invoice_ocr.OLLAMA_MODEL = param("invoice_ocr.ollama_model", invoice_ocr.OLLAMA_MODEL)
        company = company or self.env.company
        invoice_ocr.OWN_COMPANY = (company.name or "").strip().lower()
        invoice_ocr.OWN_VAT_NUMBERS = {v.replace(" ", "").upper() for v in (company.vat, company.company_registry) if v}
        return invoice_ocr

    def _invoice_ocr_extend(self, move, files_data):
        ICP = self.env["ir.config_parameter"].sudo()
        if ICP.get_param("invoice_ocr.enabled", "True").lower() in ("false", "0", ""):
            return

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
            return

        invoice_ocr = self._invoice_ocr_apply_settings(move.company_id)

        try:
            data = invoice_ocr.extract_invoice_data(pdf_data)
        except Exception as e:
            logger.warning("invoice_ocr.extract_invoice_data failed: %s", e)
            return

        # If no useful data extracted, abort
        if not data or not (data.get("vendor_name") or data.get("invoice_number")):
            return

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
        partner_id = self._resolve_partner_from_ocr(data)
        # ---- Build write vals ----------------------------------------
        vals = {}
        if partner_id and not move.partner_id:
            vals["partner_id"] = partner_id
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
        # OCR/payment reference
        if data.get("ocr_number") and not move.payment_reference:
            vals["payment_reference"] = data["ocr_number"]

        if vals:
            move.write(vals)

        # Create lines from AI lines if move has none
        if not move.invoice_line_ids:
            self._create_lines_from_ocr(move, data)

        # Resolve partner_bank_id (Bankgiro / Plusgiro)
        if not move.partner_bank_id and partner_id:
            self._resolve_partner_bank(move, data, partner_id)

        # Log a chatter note with confidence info
        conflicts = data.get("_conflicts") or []
        body = "<p><b>OCR + AI har fyllt i fakturan</b></p><ul>"
        for k in ("vendor_name", "invoice_number", "invoice_date", "due_date",
                  "total_amount", "subtotal", "vat_amount", "ocr_number", "plusgiro",
                  "bankgiro", "org_number", "currency"):
            if data.get(k) is not None:
                body += f"<li>{k}: <code>{data[k]}</code></li>"
        if conflicts:
            body += "<li><b>Konflikter regex/AI:</b><br/>" + "<br/>".join(
                f"<code>{c}</code>" for c in conflicts) + "</li>"
        body += "</ul>"
        self.env["mail.message"].create({
            "model": "account.move",
            "res_id": move.id,
            "body": body,
            "subject": "OCR-fyllning",
            "message_type": "comment",
            "author_id": self.env.user.partner_id.id,
        })

    # ------------------------------------------------------------------
    # Helpers (partner, lines, bank)
    # ------------------------------------------------------------------

    def _resolve_partner_from_ocr(self, data):
        """Match OCR-extracted vendor data to res.partner. Auto-create if needed."""
        Partner = self.env["res.partner"]

        # 1. VAT (any country prefix already in OCR, or Swedish org number)
        org_raw = (data.get("org_number") or "").strip()
        # If looks like a VAT number with letter prefix (e.g. LU20260743, SE556...)
        if org_raw and re.match(r"^[A-Z]{2}\d", org_raw):
            p = Partner.search([("vat", "=", org_raw)], limit=1)
            if p:
                return p.id

        # 2. Swedish org number — multiple variants
        org_clean = re.sub(r"[^0-9]", "", org_raw)
        if org_clean:
            for v in [f"SE{org_clean}01", f"SE{org_clean}", org_clean]:
                p = Partner.search([("vat", "=", v)], limit=1)
                if p:
                    return p.id
            p = Partner.search([("vat", "ilike", org_clean)], limit=1)
            if p:
                return p.id

        # 3. Plusgiro / bankgiro
        for field in ("plusgiro", "bankgiro"):
            bg = (data.get(field) or "").strip()
            if not bg:
                continue
            bg_clean = re.sub(r"[^0-9]", "", bg)
            if bg_clean:
                bank = self.env["res.partner.bank"].search(
                    [("sanitized_acc_number", "ilike", bg_clean)], limit=1)
                if bank:
                    return bank.partner_id.id

        # 4. Vendor name fuzzy
        name = (data.get("vendor_name") or "").strip()
        if name:
            # Strip OCR noise prefixes
            name = re.sub(r"^(services from|invoice from|faktura från|leverant.+? från)\s+",
                          "", name, flags=re.IGNORECASE).strip()
            # Exact match first
            p = Partner.search([("name", "=ilike", name), ("is_company", "=", True)],
                               limit=1)
            if p:
                return p.id
            # Substring match — only if exactly one
            tokens = [t for t in re.split(r"\s+", name) if len(t) >= 4]
            for t in tokens:
                p = Partner.search([("name", "ilike", t), ("is_company", "=", True)],
                                   limit=2)
                if len(p) == 1:
                    return p.id

        # 5. Auto-create partner if we have a name + org/VAT
        if name and (org_raw or data.get("plusgiro") or data.get("bankgiro")):
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
            for field, label in [("plusgiro", "PG"), ("bankgiro", "BG")]:
                bg = (data.get(field) or "").strip()
                if bg:
                    self.env["res.partner.bank"].create({
                        "partner_id": new_partner.id,
                        "acc_number": f"{label} {bg}",
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
        # Map by description heuristics done per-line below
        def remap_account_code(orig_code, line_desc=""):
            if not is_eu_foreign and not is_outside_eu:
                return orig_code
            try:
                code_int = int(str(orig_code or 0)[:4])
            except (ValueError, TypeError):
                return orig_code
            # Goods inköpskonton: 4000-4099 → EU/EX motsvarighet
            if is_eu_foreign and 4000 <= code_int <= 4099:
                return "4515"  # Inköp av varor från annat EU-land 25%
            if is_outside_eu and 4000 <= code_int <= 4099:
                return "4545"  # Import av varor 25% moms
            # Services 4500-4599 in BAS: 4535 (EU services 25%), 4531 (services 25% own use)
            if is_eu_foreign and 4500 <= code_int <= 4599:
                return "4535"
            # Cloud/SaaS in 6230-range stays as-is (it's a cost class, not a "purchase from EU" account)
            # but if AI returned 6231 for an EU vendor, the line still needs the EU tax tag —
            # we keep the cost account but the tax handles VAT side
            return orig_code

        line_vals_list = []

        if ai_lines and isinstance(ai_lines, list) and len(ai_lines) > 0:
            for al in ai_lines:
                amount = al.get("amount") or al.get("unit_price") or 0
                if not amount:
                    continue
                code = remap_account_code(al.get("account_code"), al.get("description", ""))
                fallback = "4515" if is_eu_foreign else ("4545" if is_outside_eu else "4000")
                acc_id = acct(code) or acct(fallback)
                lv = {
                    "name": al.get("description", data.get("invoice_number") or "Faktura"),
                    "quantity": al.get("quantity", 1),
                    "price_unit": al.get("unit_price", amount),
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
                        vat_rate, al.get("description", "")[:60])
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
            self._check_ocr_totals(move, data)

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
        if printed_net is not None and abs(move.amount_untaxed - printed_net) > tol:
            problems.append(
                f"netto {move.amount_untaxed:.2f} mot fakturans {printed_net:.2f}")
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

    def _resolve_partner_bank(self, move, data, partner_id):
        """Pick a recipient bank account on the partner that matches OCR plusgiro/bankgiro."""
        for field in ("plusgiro", "bankgiro"):
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
