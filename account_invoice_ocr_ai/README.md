# account_invoice_ocr_ai

OCR + LLM pre-fill of vendor bills in Odoo 19 Community.

## What it does

1. A vendor bill created from a PDF — the journal's **Upload** button or its
   e-mail alias — is queued in `account.move._extend_with_attachments`, and a
   background job reads it within seconds (see *When OCR runs*). The form
   button reads a draft bill at once. A PDF attached later to an existing bill
   is not read automatically.
2. Extracts text with **pdfplumber**; image-only PDFs fall back to **tesseract**
   (`swe+eng`) via pypdfium2, within page, pixel and time limits.
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

## When OCR runs

Reading a document takes OCR plus a call to an LLM, often half a minute. That no
longer happens inside the request that brought the document in, where it could
make Odoo stop the worker (`limit_time_real`, 120 s by default) and lose the
whole upload or hold up the mail fetch:

| Trigger | What happens |
|---|---|
| **Upload** in a purchase journal, a PDF to the journal's mail alias | The new draft bill is queued (*OCR: Queued*); the background job reads it within seconds |
| List action **Run OCR again** | The selected draft bills are queued; a notification says how many were queued or skipped, and why |
| Header button **Run OCR again** on a draft | Reads the bill at once (within the time limit per document) and reports the result |
| A PDF attached to an existing bill (the chatter's attachment box, a reply to the bill) | **Not read automatically**, so a supporting document never overwrites a bill someone filled in; use the header button, which reads the bill's newest PDF |

A bill Odoo already imported electronically (UBL/Peppol, embedded
Factur-X/ZUGFeRD) is left alone, as is everything when *Invoice OCR on upload*
is off. A queued bill is reported to Odoo as imported, so the upload no longer
posts "There was an error while importing the bill": Odoo only uses that value
for this message, and the bill's OCR state and chatter say how the reading went.

**The background job** (*OCR: read queued bills and receipts*, shared with
`hr_expense_ocr_ai`) is woken at once by every upload and reads the queued
documents oldest first, one at a time, each committed on its own:

- **States**: *Queued* → *Reading* → *Read*, or *Failed* with the error. The
  form shows a banner while a bill is queued or failed; the search has *OCR
  pending* and *OCR failed* filters and the list an optional *OCR* column. The
  chatter gets the usual fill note, or a note saying why OCR gave up.
- **Time budget per run** (*Background OCR time per run*,
  `invoice_ocr.cron_time_budget`): by default three quarters of Odoo's cron time
  limit (`limit_time_real_cron`, else `limit_time_real`: 90 s with Odoo's
  defaults). A document is only started when its whole time limit still fits in
  what is left of the run; the rest stays queued and the next run starts at once.
  Odoo runs all ready cron jobs of a database under one time limit, so keep the
  budget well under it.
- **Retries**: a failed attempt (an error, the provider down, the time limit) is
  tried again after 1 and 5 minutes; after `invoice_ocr.max_attempts` attempts
  (default 3) the bill is *Failed* and a note says why. While the AI step fails,
  nothing is written; the last attempt fills in what the text gave and notes
  that the AI failed. An attempt that kills the worker still counts, so a file
  that cannot be read cannot block the queue.
- **Your changes win**: a bill that is no longer a draft is taken off the queue.
  A bill someone changed after it was queued (a vendor, a line, a date — any
  save) is not read at all, so nothing entered by hand is overwritten; a note
  says so, and the form button still reads it on request (filling only what is
  empty).
- The job reads the PDF the bill was queued with, else the bill's main
  attachment.
- **As whom**: the job reads a bill as the user who queued it (*OCR requested
  by*, `ocr_requested_by`): with that user's access rights — a vendor is only
  created by a user who may create contacts, as with the form button —, in that
  user's language, with the bill's company; the notes are theirs. Only the
  queue's own bookkeeping (state, attempts) runs with superuser rights. A bill
  that came in by e-mail is read as the sender when the sender is a user (an
  employee forwarding it); from an unknown sender it is read as OdooBot, as
  before, in the company's language. A bill whose user was archived or lost the
  company is not read with other rights: it fails with that reason, and the
  form button reads it as whoever clicks it.

### Time and size limits

| Setting / system parameter | Default | Limits |
|---|---|---|
| *Time limit per document* `invoice_ocr.total_deadline` | 80 s | Reading the text and every call to the provider for one document — the 429 wait and retry, the schema fallback, the reliability re-run — end within it; never more than three quarters of Odoo's request and cron time limits, so the form button returns in time |
| *AI call timeout* `invoice_ocr.call_timeout` | 0 = 120 s | One call to the provider, any provider (`INVOICE_AI_TIMEOUT`, `STAIK_TIMEOUT`); also cut to what the document has left |
| *Background OCR time per run* `invoice_ocr.cron_time_budget` | 0 = automatic | See above |
| `invoice_ocr.max_attempts` | 3 | Attempts per queued document |
| `invoice_ocr.extract_time_budget` | 30 s | Reading the text (pdfplumber, tesseract) in all |
| `invoice_ocr.max_text_pages` | 20 | Pages pdfplumber reads: the first ones and the last |
| `invoice_ocr.max_ocr_pages` | 10 | Pages rendered for tesseract: the first ones and the last |
| `invoice_ocr.max_page_pixels` | 12000000 | Pixels of a rendered page or a receipt photo; larger ones are scaled down |
| `invoice_ocr.tesseract_timeout` | 20 s | One tesseract run (a page or a photo); that page is skipped |
| `invoice_ocr.max_image_bytes` | 20971520 | Receipt images larger than this are not read |

A limit that cut the reading (pages left out, a page scaled down or skipped,
the time used up) is noted in the chatter.

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
  (autogiro, direct debit, "dras automatiskt" …) the bill gets **Debited
  automatically** (`ocr_auto_debit`), the recipient account is left empty so the
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
lines to stay within token limits. Each bill is read in its own savepoint, so a
failure rolls back only that bill's OCR changes and leaves a chatter note; the
form button reports how many bills were filled, failed or skipped, and why.

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
| `invoice_ocr.ollama_num_ctx` | *Ollama context size*: the context window Ollama runs the model with (`num_ctx`, default 16384 tokens), see below |
| `invoice_ocr.account_list` | *Accounts for invoice lines*: the account codes the model may choose, one per line as `code: hint`; empty = the built-in list (see below) |
| `invoice_ocr.call_timeout`, `invoice_ocr.total_deadline`, `invoice_ocr.cron_time_budget` | *Limits*: time, see *When OCR runs* |
| `invoice_ocr.text_limit` | *Limits*: *Text sent to the AI*, characters (default 6000), see *LLM context limit* |
| `invoice_ocr.max_tokens` | System parameter: the answer's token limit for every provider (default 8000; `max_tokens`, `max_completion_tokens`, Ollama's `num_predict`). Reasoning models count their reasoning in it |

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

**Verify provider** on the settings page sends one short request with the values
on the form — saved or not — asking for `{"ok": true}` (a handful of tokens on a
plain model, a few hundred on a reasoning model, at most 1000), and shows which
model actually answered, how many completion tokens it used and how fast. The
requested and the answering model must have the same name; two exceptions only:
a dated snapshot (`gpt-4o-mini` answered as `gpt-4o-mini-2024-07-18`), and a
reasoning variant that the provider reports under its base name (staik answers
`qwen3.6:35b-a3b` for `qwen3.6:35b-a3b-thinking`) — the latter only when the
answer shows reasoning, because the same provider serves an unknown model name
(a typo) with its default model, without reasoning and under that same base name.
Anything else is shown as a warning. The values are passed to that one call
only; nothing is changed for real extractions until you save.

All providers get the same treatment, within the time limit per document:

- JSON-schema structured output where the endpoint supports it. Only a `400`
  that is about the schema (`response_format`, `json_schema`, structured
  output …) falls back to a plain completion.
- A `400` that names a parameter the endpoint does not accept is sent once more
  without it: `max_tokens` becomes `max_completion_tokens` (or back), a rejected
  `temperature` is left out. OpenAI's reasoning models (o-series, `gpt-5…`) get
  `max_completion_tokens` and the default temperature up front, on any endpoint;
  the `openai` preset always sends `max_completion_tokens`.
- Any other error is raised with the first 300 characters of what the provider
  said (`HTTP 400 from the AI provider: …`), in the chatter and on the Verify
  button, not just "400 Bad Request".
- A `429` is retried once, after the wait the provider asks for (`Retry-After`,
  else 15 s) — when that wait still fits in the document's time limit.
- The whole answer must have arrived by the document's deadline: a provider that
  trickles bytes is cut off then, not only one that stays silent.
- An answer cut off at its token limit is noted. The reasoning-token sanity check
  applies to models whose name says `thinking`/`reasoning` — the requested name
  or the answering one — and a kept answer from a model that skipped its
  reasoning is noted.

A provider that fails or times out is noted on the bill and, in the background
job, tried again later.

**Ollama** gets the context window (`num_ctx`, *Ollama context size*, default
16384 tokens) and the answer's limit (`num_predict`) with every request.
Ollama's own default context is small (4096 tokens on hosts with less than about
24 GB of VRAM, CPU-only included) and it then drops the beginning of a longer
prompt — the instructions — without an error. `truncate: false` asks Ollama to
fail instead (versions without that option ignore it), and an answer cut off at
its limit, or a prompt that filled the context, is noted. A larger context needs
more RAM or VRAM; `OLLAMA_CONTEXT_LENGTH` on the Ollama server is an alternative.

Each run builds its own configuration (system parameters → environment
defaults, plus the bill's company for the own-company guard) and passes it to
the library; the library's module globals are never changed at run time, so
concurrent runs for different companies or providers cannot see each other's
settings.

### LLM context limit

At most **6000 characters** of the extracted text are sent to the LLM (*Text
sent to the AI* in the settings, `invoice_ocr.text_limit`; environment default
`INVOICE_OCR_TEXT_LIMIT`). A longer document is sent as its first two thirds
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
| `INVOICE_AI_TIMEOUT` | `120` | Cap in seconds on one call to any provider except staik (the *AI call timeout* setting wins) |
| `STAIK_URL` | `https://api.staik.se/v1` | staik API base URL |
| `STAIK_API_KEY` | — | staik API key |
| `STAIK_MODEL` | `qwen3.6:35b-a3b-thinking` | staik model (reasoning variant) |
| `STAIK_TIMEOUT` | `120` | Cap in seconds on one staik call (the *AI call timeout* setting wins) |
| `STAIK_MIN_COMPLETION_TOKENS` | `1000` | Answers from a reasoning model (the requested or the answering model's name contains `thinking`/`reasoning`) below this completion-token count are treated as suspect (the model skipped its reasoning) and re-run |
| `RECEIPT_MIN_COMPLETION_TOKENS` | `300` | The same check for receipts (`hr_expense_ocr_ai`): a receipt answer is far shorter than an invoice |
| `INVOICE_AI_MAX_TOKENS` | `8000` | The answer's token limit, every provider (`invoice_ocr.max_tokens` wins) |
| `OLLAMA_URL` | `http://localhost:11434` | Local Ollama base URL |
| `OLLAMA_MODEL` | `qwen2.5:7b` | Ollama model |
| `OLLAMA_NUM_CTX` | `16384` | Ollama context window in tokens (*Ollama context size* wins) |
| `INVOICE_AI_RETRY_SKIP_SECONDS` | `60` | Skip the reliability re-run when the first call already took this long |
| `INVOICE_OCR_OWN_COMPANY` | — | Receiving company name ("Acme AB"), so its name is never taken for the supplier |
| `INVOICE_OCR_OWN_VAT` | — | Comma-separated receiving company VAT/org numbers ("SE5566...,5566..."), same purpose |
| `INVOICE_OCR_TEXT_LIMIT` | `6000` | Characters of invoice text sent to the LLM |
| `INVOICE_OCR_MAX_PAGES` | `10` | Max PDF pages rendered for tesseract OCR (the first ones and the last) |
| `INVOICE_OCR_SCALE` | `2` | Render scale for tesseract OCR |
| `INVOICE_OCR_DEADLINE` | `80` | Seconds for one document: text extraction and every provider call |
| `INVOICE_OCR_EXTRACT_BUDGET` | `30` | Seconds for the text extraction of one document |
| `INVOICE_OCR_MAX_TEXT_PAGES` | `20` | Max PDF pages read by pdfplumber (the first ones and the last) |
| `INVOICE_OCR_MAX_PIXELS` | `12000000` | Max pixels of a rendered page or a receipt photo |
| `INVOICE_OCR_TESSERACT_TIMEOUT` | `20` | Seconds for one tesseract run |
| `INVOICE_OCR_MAX_IMAGE_BYTES` | `20971520` | Largest receipt image read |

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
