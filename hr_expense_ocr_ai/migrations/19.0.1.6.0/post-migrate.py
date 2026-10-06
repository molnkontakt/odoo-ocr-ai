"""The OCR-state labels of hr.expense in the installed languages (see
hr_expense_ocr_ai._load_ocr_state_translations): an update from an earlier version, or from a
module renamed to this one, does not run the post-init hook."""

from odoo import SUPERUSER_ID, api
from odoo.addons.hr_expense_ocr_ai import _load_ocr_state_translations


def migrate(cr, version):
    _load_ocr_state_translations(api.Environment(cr, SUPERUSER_ID, {}))
