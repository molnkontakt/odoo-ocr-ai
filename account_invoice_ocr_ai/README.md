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
| `invoice_ocr.provider` | `staik` (default), `venice`, `openai`, `ollama` |
| `invoice_ocr.staik_api_key`, `invoice_ocr.staik_model` | staik credentials; use a reasoning model (default `qwen3.6:35b-a3b-thinking`). An unknown model name silently falls back to staik's default model |
| `invoice_ocr.venice_api_key`, `invoice_ocr.venice_model` | Venice.ai credentials |
| `invoice_ocr.openai_api_key`, `invoice_ocr.openai_model` | OpenAI credentials (default model `gpt-4o-mini`) |

### LLM context limit

Only the first **6000 characters** of the extracted text are sent to the LLM
(`text[:6000]`, tunable via `INVOICE_OCR_TEXT_LIMIT`). Fields printed further
down a very long document are never seen by the model; the regex extraction of
printed amounts runs on the full text, so totals/dates on late pages still work.

### Environment variables

All of these are read as **defaults** when the corresponding system parameter
(or company field) is not set — system parameters win:

| Env var | Default | Meaning |
|---------|---------|---------|
| `INVOICE_AI_PROVIDER` | `staik` | LLM provider: `staik`, `venice`, `openai`, `ollama` |
| `VENICE_API_KEY` | — | Venice.ai API key |
| `VENICE_MODEL` | `google-gemma-3-27b-it` | Venice.ai model |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model |
| `STAIK_URL` | `https://api.staik.se/v1` | staik API base URL |
| `STAIK_API_KEY` | — | staik API key |
| `STAIK_MODEL` | `qwen3.6:35b-a3b-thinking` | staik model (reasoning variant) |
| `STAIK_TIMEOUT` | `120` | Hard cap in seconds on one staik call |
| `STAIK_MIN_COMPLETION_TOKENS` | `1000` | Answers below this completion-token count are treated as suspect (the model skipped its reasoning) and re-run |
| `OLLAMA_URL` | `http://localhost:11434` | Local Ollama base URL |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model |
| `INVOICE_AI_RETRY_SKIP_SECONDS` | `60` | Skip the reliability re-run when the first call already took this long |
| `INVOICE_OCR_OWN_COMPANY` | — | Receiving company name ("Acme AB"), so its name is never taken for the supplier |
| `INVOICE_OCR_OWN_VAT` | — | Comma-separated receiving company VAT numbers ("SE5566...,SE5566..."), same purpose |
| `INVOICE_OCR_TEXT_LIMIT` | `6000` | Characters of invoice text sent to the LLM |
| `INVOICE_OCR_MAX_PAGES` | `10` | Max PDF pages rendered for tesseract OCR |
| `INVOICE_OCR_SCALE` | `2` | Render scale for tesseract OCR |

## Requirements

`pdfplumber`, `requests`; for scanned PDFs `pytesseract`, `Pillow`, `pypdfium2`
and the `tesseract-ocr` binary with `swe` + `eng` language data.

## Limitations

- Lines are only created when the bill has none yet. Delete the lines and run
  OCR again to re-read.
- The account map is a Swedish BAS default. Adjust `ACCOUNT_FALLBACKS` in
  `models/account_move.py` and `remap_account_code` in `lib/invoice_ocr.py`
  for another chart of accounts.
- Every pre-filled bill must be reviewed before posting; the model does make
  mistakes, especially on multi-rate invoices.
