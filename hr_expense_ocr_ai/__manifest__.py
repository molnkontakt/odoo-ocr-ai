{
    "name": "Expense receipt OCR + AI",
    "version": "19.0.1.3.0",
    "category": "Human Resources/Expenses",
    "summary": "Read receipt photos on expense claims with tesseract + an LLM and fill amount, date, merchant and category",
    "description": """
Odoo Community has no receipt scanning (hr_expense_extract is Enterprise). This module reuses the
OCR/LLM pipeline of account_invoice_ocr_ai (tesseract, staik/Venice/OpenAI/Ollama, same keys and
settings) on hr.expense:

- runs when a draft expense gets its main attachment (e-mailed expenses included) and only when
  amount or category is still missing;
- the button "Read receipt (OCR)" on the expense and the list action re-run it.

Only empty fields are filled (amount 0, today's date, no category, empty name; the placeholders of
the Upload button count as empty), the amount in the receipt's currency; everything read and every
guard that fired is written to the chatter, including a printed VAT that the category's tax does
not give. Guards against model guesses: the merchant, the
amount and the date must appear in the OCR text, a confidence below 0.6 (or none) leaves amount and
date empty (merchant, description and category may still be filled), the category must be one of
the company's expensable products (their description is sent to the model as a hint).
    """,
    "author": "Molnkontakt AB",
    "license": "LGPL-3",
    "depends": ["hr_expense", "account_invoice_ocr_ai"],
    "external_dependencies": {"python": ["pytesseract", "PIL"]},
    "data": [
        "views/hr_expense_views.xml",
        "views/res_config_settings_views.xml",
        "data/server_actions.xml",
    ],
    "installable": True,
    "application": False,
}
