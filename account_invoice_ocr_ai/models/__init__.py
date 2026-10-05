# The OCR queue mixin first: account.move (and hr.expense) inherit it, and Odoo builds the
# models in the order their classes are defined.
from . import ocr_queue

# isort: split
from . import account_move, res_config_settings
