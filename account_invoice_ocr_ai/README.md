# account_invoice_ocr_ai

OCR + LLM pre-fill of vendor bills in Odoo 19 Community.

## What it does

1. Hooks `account.move._extend_with_attachments`, so it runs whenever a PDF is
   uploaded through the journal's **Upload** button or attached to a draft
   vendor bill (including bills created from an incoming e-mail alias).
2. Extracts text with **pdfplumber**; image-only PDFs fall back to **tesseract**
   (`swe+eng`) via pypdfium2.
3. Regex extraction of the common Swedish fields (dates, amounts, OCR number,
   Bankgiro/Plusgiro, org number, VAT number).
4. Sends the text (never the file) to the configured LLM with a JSON schema and
   lets it fill the gaps and the invoice lines. The answer is sanity checked
   (totals must add up, reasoning models must actually have reasoned); an
   unreliable answer is retried once.
5. Creates or updates the draft bill: partner (auto-created with country from
   the VAT prefix when unknown), dates, references, bank details, lines with
   BAS account and VAT rate. EU suppliers get reverse-charge taxes and the
   account is remapped from the 4000 range to 4515/4535/4545. Marketplace
   invoices use the VAT-declaring entity as vendor.
6. Posts a chatter note with everything it read and any regex/AI conflicts.

Long invoices (20+ lines) are aggregated by the model into at most six summary
lines to stay within token limits. **Run OCR again** is available as a header
button on a draft and as a list action for batches; batches commit per bill so
a timeout does not lose finished work.

## Configuration

*Settings → Invoicing → Invoice OCR*:

| Parameter | Meaning |
|-----------|---------|
| `invoice_ocr.enabled` | on/off |
| `invoice_ocr.provider` | `staik` (default), `venice`, `openai`, `openai_compatible`, `ollama` |
| `invoice_ocr.staik_api_key`, `invoice_ocr.staik_model` | staik; use a reasoning model (default `qwen3.6:35b-a3b-thinking`). An unknown model name silently falls back to staik's default model |
| `invoice_ocr.venice_api_key`, `invoice_ocr.venice_model` | Venice.ai |
| `invoice_ocr.openai_api_key`, `invoice_ocr.openai_model` | OpenAI (default `gpt-4o-mini`) |
| `invoice_ocr.base_url`, `invoice_ocr.api_key`, `invoice_ocr.model` | any other endpoint speaking OpenAI's `/chat/completions`: Mistral, Groq, OpenRouter, Together, DeepSeek, Azure OpenAI, Anthropic's compatibility layer, vLLM, LM Studio … Base URL up to the API version |
| `invoice_ocr.ollama_url`, `invoice_ocr.ollama_model` | local Ollama (native API, JSON-schema `format`) |

**Verify provider** on the settings page does a one-token round-trip with the
values on the form and shows which model actually answered and how fast. That
is the only way to see staik's silent fallback to its default model.

All providers get the same treatment: JSON-schema structured output where the
endpoint supports it (a `400` on `response_format` falls back to a plain
completion), one retry after 15 s on `429`, and the reasoning-token sanity
check only for models whose name says `thinking`/`reasoning`.

Environment variables (`INVOICE_AI_PROVIDER`, `INVOICE_AI_BASE_URL`,
`INVOICE_AI_API_KEY`, `INVOICE_AI_MODEL`, `STAIK_API_KEY`, `OLLAMA_URL`, …) are
read as defaults when no system parameter is set.

## Requirements

`pdfplumber`, `requests`; for scanned PDFs `pytesseract`, `Pillow`, `pypdfium2`
and the `tesseract-ocr` binary with `swe` + `eng` language data.

## Limitations

- Lines are only created when the bill has none yet. Delete the lines and run
  OCR again to re-read.
- The account map is a Swedish BAS default. Adjust `ACCOUNT_FALLBACKS` and the
  remap rules in `models/account_move.py` for another chart of accounts.
- Every pre-filled bill must be reviewed before posting; the model does make
  mistakes, especially on multi-rate invoices.
