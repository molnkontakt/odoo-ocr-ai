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
| [`account_invoice_ocr_ai`](account_invoice_ocr_ai/) | Vendor bill PDFs uploaded through *Upload* or received by a purchase journal's mail alias, read by a background job within seconds: text via pdfplumber (tesseract fallback for scans), regex field extraction, then an LLM fills partner, dates, references, bank details and invoice lines with BAS account and VAT rate. Auto-creates the vendor, handles EU reverse charge and marketplace VAT declarers |
| [`hr_expense_ocr_ai`](hr_expense_ocr_ai/) | Receipt photos and PDFs on expense claims: EXIF-rotated, scaled and OCR'd with tesseract, then the LLM fills amount, date, merchant and expense category. Read by the same background job when a claim arrives by e-mail or gets its main attachment, and at once from the form. Guards against model guesses: merchant, amount and date must appear in the OCR text, low confidence leaves amount and date empty |

`hr_expense_ocr_ai` depends on `account_invoice_ocr_ai` (shared OCR/LLM library
and settings).

## AI providers

Configured under *Settings → Invoicing → Invoice OCR*: **staik** (Swedish data
residency, default), Venice.ai, OpenAI, **any OpenAI-compatible endpoint**
(Mistral, Groq, OpenRouter, Together, DeepSeek, Azure OpenAI, Anthropic's
compatibility layer, a local vLLM or LM Studio: base URL + key + model) or a
local **Ollama**. A *Verify provider* button shows which model actually answers.
The text of the document, never the file, is sent to the provider — at most 6000
characters of it, the beginning and the end of a longer one (`INVOICE_OCR_TEXT_LIMIT`); see the
[module README](account_invoice_ocr_ai/) for the full environment-variable
list. Keys live in Odoo system parameters. A reasoning-capable model is
strongly recommended; the defaults were tuned with `qwen3.6:35b-a3b-thinking`.

### When OCR runs, and how long it may take

OCR and the LLM call no longer run inside the request that brought the document
in. An upload, a mail to the alias or the list action only **queues** the bill
or receipt; a background job (one `ir.cron`, no extra dependency) reads it
**within seconds**, one document at a time, with its own time budget per run,
three attempts and a state on the document (*Queued*, *Reading*, *Read*,
*Failed*) with filters and a banner on the form. The **form buttons read at
once**. A document someone changed after it was queued is left alone. See the
[module README](account_invoice_ocr_ai/#when-ocr-runs) for the details.

Every document has one **time limit** (*Time limit per document*, default
80 s): text extraction and every provider call — retries, the 429 wait, the
schema fallback, the reliability re-run — end within it, and it never exceeds
three quarters of Odoo's request and cron time limits (`limit_time_real`,
`limit_time_real_cron`, 120 s by default), so neither the form button nor the
background job gets its worker killed. Text extraction is bounded too: pages,
pixels per page, a tesseract timeout and a time budget, with a note when a limit
cut the reading.

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
