{
    "name": "Expense receipt OCR + AI",
    "version": "19.0.1.5.0",
    "category": "Human Resources/Expenses",
    "summary": "Read receipt photos on expense claims with tesseract + an LLM and fill amount, date, merchant and category",
    "description": """
Odoo Community has no receipt scanning (hr_expense_extract is Enterprise). This module reuses the
OCR/LLM pipeline of account_invoice_ocr_ai on hr.expense: tesseract, and every AI provider that
module supports (staik, Venice, OpenAI, any OpenAI-compatible endpoint, a local Ollama), with the
same keys and settings. **Requires account_invoice_ocr_ai 19.0.1.15.0 or later** (Odoo's
depends cannot say so): it uses that module's background OCR queue, its per-run settings and its
library's provider layer and notes.

- a draft expense that gets its main attachment (Upload, e-mailed expenses, the API) is read by
  a background job within seconds, only when amount or category is still missing;
- the button "Read receipt (OCR)" on the expense reads at once; the list action queues the
  selection.

Only empty fields are filled (amount 0, today's date, no category, empty name; the placeholders of
the Upload button count as empty), the amount in the receipt's currency; everything read and every
guard that fired is written to the chatter, including a printed VAT that the category's tax does
not give. Guards against model guesses: the merchant, the
amount and the date must appear in the OCR text, the date within three days ahead and two years
back, a confidence below 0.6 (0.9 on a photo under one megapixel, or none) leaves amount and date
empty (merchant, description and category may still be filled), the category must be one of the
company's expensable products without a fixed cost (their description is sent to the model as a
hint, so category-specific rules belong in the category's description, not in the module).
English source strings, with a Swedish translation.
    """,
    "author": "Molnkontakt AB",
    "license": "LGPL-3",
    "depends": ["hr_expense", "account_invoice_ocr_ai"],
    # PyPI distribution names (Odoo warns about an import name such as "PIL")
    "external_dependencies": {"python": ["pytesseract", "Pillow"]},
    "data": [
        "views/hr_expense_views.xml",
        "views/res_config_settings_views.xml",
        "data/server_actions.xml",
    ],
    "post_init_hook": "post_init_hook",
    "installable": True,
    "application": False,
}
