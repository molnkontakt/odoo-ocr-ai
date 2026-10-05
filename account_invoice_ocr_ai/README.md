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
6. Posts a chatter note with everything it read, any regex/AI conflicts and the
   checks below.

### Guards

The buyer's own details are printed on every vendor bill, and the first org
number after a label is often the buyer's. The receiving company (the bill's
company and its branches, read from `res.company` at run time) is therefore
never used as the supplier:

- Its org/VAT numbers are skipped when the supplier's org number is extracted;
  if the regex only finds own numbers, the LLM's value is used instead.
- The vendor is never the own company, a contact under it, or another partner
  (archived ones too) carrying its org/VAT number. A pre-set own company is
  replaced by the vendor from the document.
- The recipient bank account is never one of the own company's accounts,
  including the own clearing+account number truncated to bankgiro length.
- Bankgiro (7–8 digits), plusgiro (2–8) and OCR reference (2–25) must pass the
  length and mod-10 check-digit test. When the value read from the document
  fails, the LLM's value is used if it passes; otherwise the field stays empty.
  Either way the chatter note says so.
- Dates must be real calendar dates. `NN/NN/YYYY` with both parts 12 or less is
  read as day/month unless the LLM read it the other way; the note shows both
  readings.
- **Auto debit**: when the document says the amount is debited automatically
  (autogiro, direct debit, "Dras automatiskt" …) the bill gets **Dras
  automatiskt** (`ocr_auto_debit`), the recipient account is left empty so the
  bill stays out of payment files, and a warning is posted. Matching is per
  sentence, so marketing, conditions ("if you pay by direct debit …") and
  lists of payment methods do not trigger it. A field `l10n_se_auto_debit` on
  `account.move`, if another module provides one, is set as well. A flag set by
  hand is left alone on a re-run.
- **Already booked through the bank**: if a posted bank statement line with the
  invoice number/payment reference, or the same amount near the due date and
  the vendor's name, is already reconciled directly against an expense (not
  the payable), a warning about double booking is posted.
- The payment reference is only stored when it is a valid OCR number
  (modulus 10). A reference that starts with a valid invoice number is cut back
  to the invoice number; references with letters (RF) are kept as they are.

In Odoo these identities travel in each run's own configuration (`own_ids`,
`own_names`, …); the library's module globals are never changed. For standalone
use of `lib/invoice_ocr.py` set `INVOICE_OCR_OWN_COMPANY` and
`INVOICE_OCR_OWN_VAT` (comma-separated), or pass `own_ids`/`own_names` to
`extract_invoice_data` (as arguments or in its `config`).

Long invoices (20+ lines) are aggregated by the model into at most six summary
lines to stay within token limits. **Run OCR again** is available as a header
button on a draft and as a list action for batches; batches commit per bill so
a timeout does not lose finished work. Each bill is read in its own savepoint, so
a failure rolls back only that bill's OCR changes and leaves a chatter note, and
both report how many bills were filled, failed or skipped, and why. On upload a
failed OCR run is noted the same way.

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
values on the form — saved or not — and shows which model actually answered
and how fast. That is the only way to see staik's silent fallback to its
default model. The values are passed to that one call only; nothing is changed
for real extractions until you save.

All providers get the same treatment: JSON-schema structured output where the
endpoint supports it (a `400` on `response_format` falls back to a plain
completion), one retry after 15 s on `429`, and the reasoning-token sanity
check only for models whose name says `thinking`/`reasoning`.

Each run builds its own configuration (system parameters → environment
defaults, plus the bill's company for the own-company guard) and passes it to
the library; the library's module globals are never changed at run time, so
concurrent runs for different companies or providers cannot see each other's
settings.

### LLM context limit

At most **6000 characters** of the extracted text are sent to the LLM (tunable
via `INVOICE_OCR_TEXT_LIMIT`). A longer document is sent as its first two thirds
and its last third of that budget, joined by a marker saying how much was left
out, so the totals, VAT summary and payment details at the end stay in view. The
chatter note then says that the model saw only part of the text (its lines may be
incomplete), and the reliability re-run is skipped, since it would see the same
cut text. The regex extraction of printed amounts runs on the full text, so
totals/dates on late pages still work.

### Environment variables

All of these are read as **defaults** when the corresponding system parameter
(or company field) is not set — system parameters win:

| Env var | Default | Meaning |
|---------|---------|---------|
| `INVOICE_AI_PROVIDER` | `staik` | LLM provider: `staik`, `venice`, `openai`, `openai_compatible`, `ollama` |
| `VENICE_API_KEY` | — | Venice.ai API key |
| `VENICE_MODEL` | `google-gemma-3-27b-it` | Venice.ai model |
| `OPENAI_API_KEY` | — | OpenAI API key |
| `OPENAI_MODEL` | `gpt-4o-mini` | OpenAI model |
| `INVOICE_AI_BASE_URL` | — | `openai_compatible`: base URL up to the API version |
| `INVOICE_AI_API_KEY` | — | `openai_compatible`: API key |
| `INVOICE_AI_MODEL` | — | `openai_compatible`: model |
| `INVOICE_AI_TIMEOUT` | `120` | Hard cap in seconds on one call to any provider except staik |
| `STAIK_URL` | `https://api.staik.se/v1` | staik API base URL |
| `STAIK_API_KEY` | — | staik API key |
| `STAIK_MODEL` | `qwen3.6:35b-a3b-thinking` | staik model (reasoning variant) |
| `STAIK_TIMEOUT` | `120` | Hard cap in seconds on one staik call |
| `STAIK_MIN_COMPLETION_TOKENS` | `1000` | Answers from a reasoning model (name contains `thinking`/`reasoning`) below this completion-token count are treated as suspect (the model skipped its reasoning) and re-run |
| `OLLAMA_URL` | `http://localhost:11434` | Local Ollama base URL |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model |
| `INVOICE_AI_RETRY_SKIP_SECONDS` | `60` | Skip the reliability re-run when the first call already took this long |
| `INVOICE_OCR_OWN_COMPANY` | — | Receiving company name ("Acme AB"), so its name is never taken for the supplier |
| `INVOICE_OCR_OWN_VAT` | — | Comma-separated receiving company VAT/org numbers ("SE5566...,5566..."), same purpose |
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
