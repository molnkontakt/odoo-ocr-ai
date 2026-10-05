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
   the VAT prefix when unknown; `EL` is Greece, `XI` the UK), dates,
   references, bank details, lines with BAS account and a purchase tax per line
   (see *VAT treatment* below). Marketplace invoices use the VAT-declaring
   entity as vendor.
6. Posts a chatter note with everything it read, any regex/AI conflicts and the
   checks below.

### Currency

The bill gets the document's currency before any line is created, when that
currency is active and has an exchange rate on or before the invoice date (rates
of the company or shared ones, as Odoo converts with). Otherwise a warning is
posted and **no lines are created**: the bill stays in the company's currency,
since amounts in EUR booked as SEK would be wrong by the exchange rate. Activate
the currency, add a rate and run OCR again, or enter the lines by hand. The
totals check also warns when the bill's and the document's currencies differ.

### VAT treatment

Every line gets its own tax, chosen once its final account is known. Accounts,
taxes, partners and bank accounts are always those of the bill's company, also
when another company is active. Taxes are l10n_se's, found by their template id
for the bill's company; on another chart a domestic tax is found by rate, and a
reverse-charge tax only by its l10n_se-style name ("25% EU G"), never guessed.

- **Goods or services** follows the line's account (BAS): goods are 4000–4499,
  4510–4529 (EU goods, 4515–4517) and 4540–4549 (import, 4545–4547); everything
  else is a service, including 4530–4539 (4531–4533 from outside the EU,
  4535–4537 from the EU) and the 5xxx–7xxx cost accounts.
- **Swedish vendor** (or no country): Swedish input VAT at the line's rate,
  `25% G` or `25% S` etc. A 0 % line gets no tax.
- **Foreign vendor, no VAT on the document**: reverse charge per line, at the
  line's Swedish rate or 25 %: EU goods `EU G` (box 20), EU services `EU S`
  (box 21), import of goods `EX G` (box 50), services from outside the EU
  `EX S` (box 22). A domestic goods account (4000–4099) becomes 4515–4517 or
  4545–4547, and a 45xx account of the wrong region or rate is moved to the
  right one; other cost accounts stay. Import of goods gets a note that the VAT
  base is the customs value on the customs bill, not the invoice amount. A
  0 % line on an account for exempt or out-of-scope costs (insurance 63xx, bank
  charges 657x, fees 699x, statutory premiums 75xx, financial items 8xxx) gets
  no tax: a reminder fee is never reverse-charged at 25 %.
- **Foreign vendor charging Swedish VAT** (it shows a Swedish VAT number, e.g. a
  marketplace's "VAT declared by"): Swedish input VAT, no reverse charge, and a
  note.
- **Foreign vendor charging foreign VAT** (a hotel abroad): foreign VAT is not
  deductible in Sweden, so the printed VAT is added to the cost of the lines
  that carry it, the lines get no tax and no reverse charge, and a note says
  so (ask for a corrected invoice if it should have been reverse-charged).

### Guards

The buyer's own details are printed on every vendor bill, and the first org
number after a label is often the buyer's. The receiving company (the bill's
company and its branches, read from `res.company` at run time) is therefore
never used as the supplier:

- Its org/VAT numbers are skipped when the supplier's org number is extracted;
  if the regex only finds own numbers, the LLM's value is used instead.
- **Vendor matching**, in this order: the VAT number, the Swedish org number,
  the bankgiro/plusgiro (the same digits as an account the bill's company may
  use, never part of a longer number), then the name, only among the company's
  vendors: the same name apart from legal form and punctuation, or every
  distinctive word of it (not "AB", "Sverige" …). More than one partner on a rule
  is no match. The commercial partner is used, a vendor already on the bill is
  kept (nothing is looked up or created), and the chatter note says which rule
  matched; a match on the name alone is flagged for checking.
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
| `invoice_ocr.account_list` | *Accounts for invoice lines*: the account codes the model may choose, one per line as `code: hint`; empty = the built-in list (see below) |

### Accounts for invoice lines

The model picks each line's account from a list of codes with a short hint each
("6540: IT-tjänster (IT consulting, managed services)"). The default is a
Swedish BAS list (`DEFAULT_ACCOUNTS` in `lib/invoice_ocr.py`); replace it in the
settings with your own, one `code: hint` per line. Only the codes that exist in
the bill company's chart (or have expense sub-accounts there, e.g. 65400 for
6540) are sent, the answer's schema allows only those codes, and an answer with
another code is noted and gets the fallback account: the purchase journal's
default account, else the company's default expense account. For a reverse-charge
purchase the BAS foreign-purchase account is used when the chart has it (see
*VAT treatment*).

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
- The account list and the VAT rules follow Swedish BAS and l10n_se. For another
  chart, set your own account list in the settings; goods and services are told
  apart by BAS account ranges (`GOODS_ACCOUNT_RANGES` in `lib/invoice_ocr.py`).
- Every pre-filled bill must be reviewed before posting; the model does make
  mistakes, especially on multi-rate invoices.
