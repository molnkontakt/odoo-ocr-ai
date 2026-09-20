# odoo-ocr-ai

Odoo 19 Community modules that read supplier invoices and expense receipts
with OCR + an LLM and pre-fill the accounting record. Built for Swedish
bookkeeping (BAS accounts, Swedish VAT rates, Bankgiro/Plusgiro/OCR references,
Swedish receipt layouts) but the pipeline itself is generic.

Sister repositories: [odoo-l10n-se](https://github.com/molnkontakt/odoo-l10n-se)
(bank statement imports, payment reminders) and
[odoo-l10n-se-skv](https://github.com/molnkontakt/odoo-l10n-se-skv) (VAT return).

## Modules

| Module | Description |
|--------|-------------|
| [`account_invoice_ocr_ai`](account_invoice_ocr_ai/) | Vendor bill PDFs uploaded through *Upload* or attached to a draft bill: text via pdfplumber (tesseract fallback for scans), regex field extraction, then an LLM fills partner, dates, references, bank details and invoice lines with BAS account and VAT rate. Auto-creates the vendor, handles EU reverse charge and marketplace VAT declarers |
| [`hr_expense_ocr_ai`](hr_expense_ocr_ai/) | Receipt photos and PDFs on expense claims: EXIF-rotated, upscaled and OCR'd with tesseract, then the LLM fills amount, date, merchant and expense category. Runs when a claim arrives by e-mail or gets its main attachment, and on demand. Guards against model guesses: merchant and amount must appear in the OCR text, low confidence fills nothing |

`hr_expense_ocr_ai` depends on `account_invoice_ocr_ai` (shared OCR/LLM library
and settings).

## AI providers

Configured under *Settings → Invoicing → Invoice OCR*: **staik** (Swedish data
residency, default), Venice.ai, OpenAI or a local Ollama. The text of the
document, never the file, is sent to the provider — and only the first
6000 characters of it (`INVOICE_OCR_TEXT_LIMIT`); see the
[module README](account_invoice_ocr_ai/) for the full environment-variable
list. Keys live in Odoo system parameters. A reasoning-capable model is
strongly recommended; the defaults were tuned with `qwen3.6:35b-a3b-thinking`.

### Known limitation: synchronous LLM call on upload

The upload path (journal *Upload* button, chatter attachment, e-mail alias) runs
OCR + the LLM call **synchronously inside the create transaction**. One call is
capped at `STAIK_TIMEOUT` seconds (default 120, env-tunable) and the reliability
re-run is skipped when the first call already took `INVOICE_AI_RETRY_SKIP_SECONDS`
seconds (default 60), so a single upload can block a worker for roughly that long
— it can never hang indefinitely. Avoid this on high-volume setups or with
slow/offline providers; the intended long-term fix is async processing
(queue_job / server action), and the bulk path already exists as a list-view
server action, *Kör OCR igen*, which commits per move.


## Requirements

- Odoo 19 Community
- Python: `pdfplumber`, `requests`; for scans and photos `pytesseract` + `Pillow`
  (+ `pypdfium2` for image-only PDFs) and the `tesseract-ocr` binary with the
  `swe` and `eng` language packs

## Disclaimer

Modules in this repository are under active development. **Use at your own
risk.** OCR and language models make mistakes: every pre-filled bill and expense
must be reviewed by a human before it is posted. Molnkontakt AB disclaims all
liability for incorrect bookkeeping or any other consequence, direct or indirect,
arising from use of these modules.

## License

LGPL-3.0-only. See [LICENSE](LICENSE).
