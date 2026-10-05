{
    "name": "Invoice OCR + AI",
    "version": "19.0.1.13.0",
    "category": "Accounting",
    "summary": "Read uploaded vendor bill PDFs with OCR + an LLM and pre-fill partner, dates, references and lines",
    "depends": ["account"],
    "description": """
Runs when a PDF is uploaded through the journal's *Upload* button or attached to a draft
vendor bill: text via pdfplumber (tesseract fallback for scans), regex extraction, then an LLM
(staik by default, Swedish data residency; Venice, OpenAI or a local Ollama also supported)
fills partner (auto-created if unknown), invoice/due dates, invoice number, OCR/Bankgiro/Plusgiro
references, the currency and invoice lines with a BAS account from the company's chart and a
purchase tax per line: Swedish VAT, EU and non-EU reverse charge for goods and services, import
of goods, foreign VAT as cost. Handles marketplace VAT declarers (Amazon, eBay). Everything is
a draft for human review.
    """,
    "external_dependencies": {"python": ["pdfplumber", "requests"]},
    "author": "Molnkontakt AB",
    "license": "LGPL-3",
    "data": [
        "views/res_config_settings_views.xml",
        "views/account_move_views.xml",
        "data/server_actions.xml",
    ],
    "installable": True,
}
