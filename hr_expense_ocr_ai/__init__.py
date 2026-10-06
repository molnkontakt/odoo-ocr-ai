from . import models


def _load_ocr_state_translations(env):
    """Load account_invoice_ocr_ai's translations again, for hr.expense's OCR-state labels.

    The labels (Queued, Reading, Read, Failed) belong to account_invoice_ocr_ai, whose mixin
    defines the field, so they are in its .po files. Odoo loads those when it installs or
    updates account_invoice_ocr_ai — before this module gives hr.expense the field and its
    labels — so on hr.expense they stayed English until account_invoice_ocr_ai was updated
    once more. Existing translations are kept.
    """
    langs = [code for code, _name in env["res.lang"].get_installed() if code != "en_US"]
    if langs:
        env["ir.module.module"]._load_module_terms(["account_invoice_ocr_ai"], langs)


def post_init_hook(env):
    _load_ocr_state_translations(env)
