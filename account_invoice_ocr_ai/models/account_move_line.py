"""A change to a bill's lines clears "Amounts checked against the document" (#39).

The flag lets someone with accounting rights post a bill whose total differs from the total
OCR read on the document; it confirms the lines as they were when it was ticked. Lines edited
on the form are written through account.move.write (handled there); this covers lines written
directly (imports, the API, automations).
"""

from odoo import api, models

# Line fields that change a bill's amounts
AMOUNT_FIELDS = frozenset({"price_unit", "quantity", "discount", "tax_ids", "price_subtotal",
                           "price_total", "display_type"})


class AccountMoveLine(models.Model):
    _inherit = "account.move.line"

    def _ocr_product_moves(self):
        return self.filtered(lambda line: line.display_type == "product").move_id

    @api.model_create_multi
    def create(self, vals_list):
        lines = super().create(vals_list)
        lines._ocr_product_moves()._ocr_reset_amounts_checked()
        return lines

    def write(self, vals):
        res = super().write(vals)
        if AMOUNT_FIELDS & set(vals):
            self._ocr_product_moves()._ocr_reset_amounts_checked()
        return res

    def unlink(self):
        moves = self._ocr_product_moves()
        res = super().unlink()
        moves.exists()._ocr_reset_amounts_checked()
        return res
