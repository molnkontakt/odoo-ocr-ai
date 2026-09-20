"""The libraries are plain Python; put both `lib` dirs on sys.path so they import without Odoo."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
for lib in ("account_invoice_ocr_ai/lib", "hr_expense_ocr_ai/lib"):
    sys.path.insert(0, str(ROOT / lib))
