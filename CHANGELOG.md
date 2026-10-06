# Changelog

## Unreleased

- `account_invoice_ocr_ai` 19.0.1.15.0, `hr_expense_ocr_ai` 19.0.1.5.0: the provider layer,
  English source strings with a Swedish translation, and the background job reads as the
  user who queued the document. **Behaviour changes** — see the points marked so.
  `hr_expense_ocr_ai` 19.0.1.5.0 needs `account_invoice_ocr_ai` 19.0.1.15.0 or later
  (#36.7); install both from the same release.
  - **OpenAI reasoning models work (#19).** o-series and `gpt-5…` models get
    `max_completion_tokens` and the default temperature, on any endpoint; the `openai`
    preset always sends `max_completion_tokens` (**behaviour change** for that preset). A
    `400` that names a parameter the endpoint does not accept is sent once more without it
    (`max_tokens` ↔ `max_completion_tokens`, `temperature`), so other OpenAI-compatible
    endpoints benefit too. Only a `400` about the JSON schema still falls back to a plain
    completion; any other error is shown with the first 300 characters of what the
    provider said (`HTTP 400 from the AI provider: …`), in the chatter and on *Verify
    provider*, instead of "400 Bad Request".
  - **Rate limits and slow answers (#9).** A `429` waits as long as `Retry-After`
    (or `retry-after-ms`) asks, within the document's time limit, else 15 s. The whole
    answer must have arrived by the document's deadline: a provider that trickles bytes is
    cut off then, not only one that stays silent.
  - **Ollama (#20) — behaviour change.** Every request sends the context size (`num_ctx`,
    new setting *Ollama context size*, `invoice_ocr.ollama_num_ctx`, default 16384 tokens —
    more than Ollama's own default of 4096 on most hosts, so it needs more RAM/VRAM), the
    answer's limit (`num_predict`) and `truncate: false`. An answer cut off at its limit, or
    a prompt that filled the context, is noted.
  - **Verify provider (#23).** The requested and the answering model must have the same
    name, apart from a dated snapshot (`gpt-4o-mini-2024-07-18`) and a reasoning variant
    reported under its base name — the latter only when the answer shows reasoning, so a
    typo in `…-thinking` that the provider silently serves with its default model is now a
    warning. The notification shows the completion tokens and the latency, and a cut-off
    answer is reported as such. The reasoning-token check decides "reasoning model" from
    the requested name too.
  - **Receipts (#36.9) — behaviour change.** The AI diagnostics are kept; the answer's
    token limit is the shared `invoice_ocr.max_tokens` (default 8000, was a fixed 4000);
    an answer cut off at its limit, or one from a reasoning model that skipped its
    reasoning (fewer than 300 completion tokens, `RECEIPT_MIN_COMPLETION_TOKENS`), is read
    once more when there is time, and noted when it stays so. The chatter note names the
    model that answered.
  - **New settings and parameters.** *Text sent to the AI* (`invoice_ocr.text_limit`,
    6000 characters, #21) is shown with the other limits; *Ollama context size*
    (`invoice_ocr.ollama_num_ctx`, 16384); system parameter `invoice_ocr.max_tokens`
    (8000). Environment defaults `INVOICE_AI_MAX_TOKENS`, `OLLAMA_NUM_CTX`,
    `RECEIPT_MIN_COMPLETION_TOKENS`.
  - **The background job reads as the user who queued the document — behaviour change.** In
    19.0.1.14.0 the job read everything as OdooBot, past the uploader's access rights. Now a
    queued bill or receipt stores who queued it (*OCR requested by*, `ocr_requested_by`) and
    is read as that user, in their language and with the document's company: a user who may
    not create contacts gets a note instead of a new vendor (as with the form button), and
    the notes are theirs. Documents from the mail alias are read as the sender when the
    sender is a user (an expense: as its employee's user), else as OdooBot as before. A user
    who was archived or lost the company is not replaced by OdooBot: the document fails with
    that reason.
  - **English source strings and a Swedish translation (#35.3, #36.12) — behaviour
    change.** Every label, button, list action, setting, notification and chatter note is
    English in the code and goes through `_()` with its arguments; `i18n/sv.po` of both
    modules translates all of it ("Kör OCR igen", "Dras automatiskt", "Läs kvitto (OCR)",
    "omvänd betalningsskyldighet" …), so a Swedish user sees Swedish and an English user
    English. Amounts in notes are formatted for the user's language and currency. The
    notes of the plain-Python libraries are translatable too: they are `invoice_ocr.Note`
    objects (English text plus msgid, parameters and module), marked with `_()` so Odoo's
    exporter finds them, and shown through the module's translations.
  - **Docs (#35.2, #36.7, #36.10) — behaviour change for receipts.** A PDF attached later
    to an existing bill is not read automatically (the form button reads it); both
    manifests list every provider. The receipt prompt no longer sends fuel, oil and tools
    to a machinery category: put such rules in the category's description (an example is
    in the module README). **If you relied on that rule, add it to your category.**
  - Tests: OpenAI-style and other 400s, parameter adaptation, Retry-After, a trickling
    local server, Ollama options and notes, model matching, receipt re-reads, translated
    notes, the .pot/.po against the code, reading as the user who queued, the Swedish
    translation in Odoo, and the totals check (#25).

- `account_invoice_ocr_ai` 19.0.1.14.0, `hr_expense_ocr_ai` 19.0.1.4.0: OCR runs in the
  background, and every document has a time limit. **Behaviour change** — uploads are no
  longer read inside the request.
  - **Uploads are read by a background job within seconds (#9, #29).** The journal's
    *Upload* button, a PDF to a purchase journal's mail alias, a receipt that becomes an
    expense's main attachment (Upload, e-mail, the API — now also when it is set in
    `create`, #36.3) and the list actions *Kör OCR igen* / *Läs kvitto (OCR)* only queue
    the document; one `ir.cron`, *OCR: read queued bills and receipts*, is woken at once
    and reads the queue oldest first. The upload, the mail fetch or the API call returns
    at once, so a slow provider no longer gets a worker killed, rolls back an upload of
    several files or holds up the mail intake. The **form buttons still read at once**.
    No new dependency (no queue_job).
  - **What users see.** An *OCR* state on bills and expenses: *Queued*, *Reading*,
    *Read*, *Failed* (with the error). A banner on the form while a document is queued or
    failed, *OCR pending* and *OCR failed* search filters, an optional *OCR* list column.
    The list actions answer "N queued for OCR …, M skipped" with the reasons (#35.1,
    #36.4); the chatter gets the fill note, or a note saying why OCR gave up.
  - **The job.** One document at a time, each committed on its own. Its own time budget
    per run (*Background OCR time per run*, default three quarters of Odoo's cron time
    limit: 90 s with Odoo's default 120 s): a document is only started when its whole time
    limit still fits, the rest is left to a run that starts at once. A failed attempt is
    tried again after 1 and 5 minutes; after three (`invoice_ocr.max_attempts`) the
    document is *Failed* with a note. While the AI step fails nothing is written; the last
    attempt fills in what the text gave and notes that the AI failed. An attempt that
    kills the worker still counts. Documents that are no longer drafts leave the queue.
  - **Your changes win.** A document someone changed after it was queued (a vendor, a
    line, a date — any save) is not read at all, so nothing entered by hand is
    overwritten; a note says so, and the form button still reads it on request (filling
    only what is empty, as before).
  - **A time limit per document (#9).** Text extraction and every provider call — the
    429 wait and retry, the schema fallback, the reliability re-run — end within *Time
    limit per document* (default 80 s, `INVOICE_OCR_DEADLINE`), never more than three
    quarters of Odoo's request and cron time limits; before, one call could take about
    375 s. *AI call timeout* caps a single call for every provider (empty: 120 s as
    before). The receipt call no longer has a fixed 180 s.
  - **Bounded text extraction (#26).** pdfplumber reads at most 20 pages and tesseract
    10 (the first ones and the last), a rendered page or photo is scaled down to 12
    megapixels, one tesseract run stops after 20 s, the whole reading after 30 s, and a
    receipt image over 20 MB is not read. A limit that cut the reading is noted in the
    chatter. The limits are system parameters `invoice_ocr.<key>` (see the module
    README).
  - **A failing provider is reported.** A failed AI call is no longer swallowed: the
    chatter says that only the regex values were used, and the job tries again.
  - **Upload no longer posts "There was an error while importing the bill" (#17).** A
    queued bill is reported to Odoo as imported: core only uses that value for this
    message, and the OCR state and chatter tell how the reading went. The decoder
    refactor is not needed with OCR in the job. The per-record commits of the list
    actions are gone.
  - New settings: *Time limit per document*, *AI call timeout*, *Background OCR time per
    run*. `hr_expense_ocr_ai` 19.0.1.4.0 needs `account_invoice_ocr_ai` 19.0.1.14.0
    (`ocr.queue.mixin`).

- `account_invoice_ocr_ai` 19.0.1.13.0, `hr_expense_ocr_ai` 19.0.1.3.0: accounting
  correctness. **Several behaviour changes** — review the first bills and receipts after
  upgrading.
  - **The bill's company (#7).** Accounts, taxes, partners and bank accounts are looked up
    in the bill's company, also when another company is active or several are ticked.
    Before, the lines got another company's account or tax, Odoo refused them and the
    bill was left without lines. Taxes are l10n_se's, found by template id for the bill's
    company; other charts fall back to a search by rate (a reverse-charge tax only by its
    l10n_se-style name, never guessed).
  - **A purchase tax per line (#8, #22) — behaviour change.** Each line gets its own tax
    once its account is known. Goods or services follows the account (BAS: goods
    4000–4499, 4510–4529, 4540–4549; services 4530–4539 and the 5xxx–7xxx cost
    accounts). Foreign vendor without VAT on the document: reverse charge per line — EU
    goods `EU G` (box 20) instead of `EU S`, import of goods `EX G` (box 50/60, with a
    note that the customs value is the VAT base), EU services `EU S`, services from
    outside the EU `EX S`. Swedish vendors get `G` or `S` taxes per line. A 0 % line on
    an account for exempt or out-of-scope costs (63xx, 657x, 699x, 75xx, 8xxx — reminder
    fees, bank charges) is never reverse-charged at 25 %. A foreign vendor charging
    Swedish VAT (a Swedish VAT number on the document or the partner) gets Swedish input
    VAT. **Foreign VAT** printed on the document (a hotel abroad) is added to the cost of
    the lines, which get no tax and no reverse charge, and a note says so. Greek (`EL`)
    and Northern Irish (`XI`) VAT numbers give the countries GR and GB. The account remap
    keeps EU goods on 4515–4517 (it was swallowed into 4535) and picks the BAS account
    for the region and rate (#35).
  - **Account list setting (#24) — behaviour change.** The model only gets accounts that
    exist in the bill company's chart (6231 is in none of l10n_se's charts, nine more
    codes of the built-in list are not in its base chart), the answer's schema allows
    only those codes, and
    another code is noted and gets the fallback account: the purchase journal's default
    account instead of a hard-coded 4000/4515/4545. The list is the new setting *Accounts
    for invoice lines* (`invoice_ocr.account_list`, one `code: hint` per line); empty
    means the built-in Swedish BAS list. Dead code removed and the README no longer
    points to `ACCOUNT_FALLBACKS` (#35).
  - **Currency (#5, #28) — behaviour change.** A bill gets the document's currency before
    its lines are created, when that currency is active and has an exchange rate on or
    before the invoice date; otherwise a warning is posted and **no lines are created**
    (the bill stays in the company's currency). The totals check warns when the
    currencies differ. A receipt's amount is written in the receipt's currency, rounded
    like it; a currency that cannot be used leaves the amount empty with a note, and a
    category with a fixed cost keeps its quantity × cost amount. Without the AI a total
    printed in a foreign currency is not read. Before, EUR 104.64 became 104.64 SEK.
  - **Stricter vendor matching (#13) — behaviour change.** Bankgiro/plusgiro must equal
    an account's number digit for digit (no substring of an IBAN or another account).
    Names match only among the company's vendors (`supplier_rank` > 0), on the whole name
    apart from legal form or on every distinctive word, so "Acme Sverige AB" no longer
    picks "Other Sverige AB". Several partners on a rule: none. The commercial partner is
    used, the chatter note says which rule matched, a name-only match is flagged for
    checking, and a vendor already on the bill is kept without a lookup (no partner is
    created for it).
  - **Lock dates (#35).** The accounting date follows the invoice date unless Odoo's own
    lock rules forbid it: purchase lock date, parent company locks, the hard lock and
    the user's lock exceptions now count. A locked date is noted.
  - **Receipt VAT (#33).** A note when the VAT printed on the receipt and the category's
    tax differ by more than 1 (e.g. a 12 % restaurant receipt in a 25 % category). The
    tax is never changed.
  - **Upload placeholders (#34) — behaviour change.** Odoo's *Upload* button puts the
    generic `EXP_GEN` category and "Untitled Expense <date>" on every expense; these now
    count as empty, so the receipt fills category and description too. The hard-coded
    `OKÄND AVSÄNDARE` name prefix is replaced by the system parameter
    `expense_ocr.placeholder_name_prefixes` (comma-separated, empty by default; also in
    the settings): a description starting with one of them gets the receipt's
    description appended.
  - The Odoo tests run on l10n_se's Swedish chart (CI installs `l10n_se`), with a case
    per VAT treatment and their tax report tags, two companies, currencies, lock dates
    and vendor matching. `hr_expense_ocr_ai` 19.0.1.3.0 needs `account_invoice_ocr_ai`
    19.0.1.13.0 (`account.move._ocr_currency`).

- `account_invoice_ocr_ai` 19.0.1.12.0, `hr_expense_ocr_ai` 19.0.1.2.0: failures are
  contained and visible, and the parsing is stricter.
  - **Failures.** OCR on upload runs in a savepoint: a failure rolls back only its own
    writes (no half-created partner, no aborted transaction) and leaves a chatter note;
    core's "There was an error while importing the bill" no longer appears on a bill OCR
    did fill (#17). *Run OCR again* (button and list action) and the receipt list action
    read each record in its own savepoint and show a notification with how many records
    were filled, failed or skipped, and why; the list actions no longer write an
    `ir.logging` row their own rollback discarded (#35, #36). *Read receipt* shows a
    readable error instead of a server error, and the automatic receipt read on a new
    attachment runs in a savepoint and notes a failure (#36).
  - **Amounts.** `1,234` and `1,234,567` are thousands, not decimals (#15). The receipt
    amount check compares whole amounts, so 18 no longer passes on `418,00`, 25 on
    `Moms 25%` or 17 on a date, and `1 234,50` / `1 000 kr` are accepted; when the
    model's total is not printed, the printed total is used. The regex total fallback
    reads `1234,50` and `Totalt (2 Artiklar) 418,00` and picks the labelled amount to
    pay instead of the largest amount (#32, #36).
  - **Dates.** Only real calendar dates are read, merged and written; English month
    names are understood; `Datum` no longer matches Leveransdatum/Förfallodatum/
    Orderdatum; `NN/NN/YYYY` with both parts ≤ 12 is day/month unless the LLM's date is
    the other reading (#16). A receipt date the model gives in another format, or that
    is not printed on the receipt, is not used; the printed date is used instead (#30,
    #31).
  - **LLM answers.** Malformed `lines` and locale-formatted numbers no longer lose the
    whole pre-fill; the regex fields survive any malformed answer (#10). Invoice lines
    come to the line amount the answer was checked against (#11).
  - **Bankgiro, plusgiro, OCR.** Values stay on their line, labels are whole words, a
    label row with the values on the next row is read (the OCR number too), and every
    value must pass the length and mod-10 check digit, with the LLM's value as the
    fallback (#14).
  - **Receipts.** The merchant check needs the name's distinctive words printed
    together (#36); the category hint is plain text, not the Guideline's HTML (#36). A
    missing confidence counts as low; the docs now say what low confidence does: amount
    and date stay empty, merchant, description and category may be filled (#36).
  - **Long invoices.** A text over the limit is sent as head and tail with a marker, the
    chatter says the model saw only part of it, and the re-run is skipped. The
    marketplace VAT declarer is found in the full text (#21).
  - CI runs the modules' Odoo tests on Odoo 19 with PostgreSQL (#25, #36).

- `account_invoice_ocr_ai` 19.0.1.11.1, `hr_expense_ocr_ai` 19.0.1.1.1: the *Invoice OCR on
  upload* and *Expense receipt OCR* switches can be turned off. An unticked Boolean
  `config_parameter` deletes the parameter and a missing parameter read as on, so the
  switch never stuck; `set_values` now stores "True"/"False" explicitly (#6, #27). OCR no
  longer runs on bills Odoo already imported electronically (UBL/Peppol, embedded
  Factur-X/ZUGFeRD: `_extend_with_attachments` returned a truthy result), so it no longer
  overwrites their lines and due date with a reading of the PDF (#18).

- `account_invoice_ocr_ai` 19.0.1.11.0: guards against booking a vendor bill with the
  buyer as vendor. The receiving company's org/VAT numbers, names, partners and bank
  accounts are read from `res.company` (the bill's company and its branches) instead of
  only the first company's name/VAT: own numbers are skipped when extracting the
  supplier's org number (the LLM's value wins if the regex only finds own ones), the
  vendor is never the own company, its contacts or duplicates carrying its numbers, and
  the recipient account is never an own account (also when truncated to bankgiro
  length); a "bankgiro" that is not 7–8 digits is dropped. New **Dras automatiskt**
  flag (`ocr_auto_debit`, `ocr_auto_debit_phrase`): auto-debited bills (autogiro, direct
  debit) are detected per sentence, the recipient account is left empty and a warning
  is posted; `l10n_se_auto_debit` is set too when that field exists. A warning is
  posted when a bank statement line is already reconciled directly against an expense
  (double booking). The payment reference is only stored when it is a valid OCR number
  (modulus 10). (Before: a bank's invoice was booked with the buyer's own company as
  vendor and the buyer's own account as recipient, because the buyer's org number was
  printed first.) The receiving company's identities travel in the per-run config
  (`own_ids`, `own_names`, plus `own_partner_ids`/`own_bank_keys`/`own_account_keys`
  for the Odoo-side guards; built by `account.move._invoice_ocr_config`); the
  library's `OWN_COMPANY`/`OWN_VAT_NUMBERS` globals are never written and only serve
  as the environment fallback for standalone use. `own_ids`/`own_names` replace the
  19.0.1.8.2 config keys `own_vat_numbers`/`own_company`, and the 19.0.1.8.2
  normalized VAT comparison is covered by the org/VAT key matching (`SE…01` ↔ org.nr,
  spaces/dashes ignored).

- `account_invoice_ocr_ai` 19.0.1.10.0: rounding and adjustments outside VAT are fixed
  against the invoice's printed amounts. When the printed VAT shows a different taxable
  base than the lines (one rate only, at most 2 kr) the base is moved and the opposite
  amount is booked outside VAT on 3740; a remaining difference to the amount due (at most
  2 kr) becomes a rounding line on 3740. A note is posted. Larger differences are left
  alone and flagged as before. (Before: a telecom invoice with a credit outside VAT and
  rounding was booked 0.94 too high, under the check's 1 kr tolerance.)

- Provider layer generalised: any OpenAI-compatible endpoint (`openai_compatible`:
  base URL + key + model), Ollama for both modules, OpenAI settings in the UI,
  JSON-schema fallback and 429 retry for every provider, *Verify provider* button
  that reports the model actually served. `account_invoice_ocr_ai` 19.0.1.9.0,
  `hr_expense_ocr_ai` 19.0.1.1.0. Built on the 19.0.1.8.2 per-run config: the
  provider layer (`resolve_endpoint`, `chat_json`, Ollama, `verify_provider`)
  reads provider, keys, URLs and limits only from the config passed in and never
  writes the module globals, which stay env-derived defaults for standalone use.
  *Verify provider* builds a config from the form's (possibly unsaved) values for
  that one call, so an unsaved key is never used by real extractions and a
  cleared key stops working once saved. `hr_expense_ocr_ai` builds the same
  per-run config for each expense's company. `INVOICE_AI_TIMEOUT` (every provider
  except staik) defaults to 120 s like `STAIK_TIMEOUT`, so every call on the
  synchronous upload path stays bounded.

### Fixed (review, PR #1 medium findings — `account_invoice_ocr_ai` 19.0.1.8.2)

- Own-company guard: the VAT comparison is now normalized (spaces/dashes
  stripped, upper-cased) — `company.vat` ("SE556 000-0001") and
  `company_registry` ("556000-0001") both match candidates printed on the
  invoice. When no non-SE candidate remains (only Swedish VAT numbers, which
  may be the customer block on a foreign invoice), `org_number` is no longer
  overwritten with a guess; the regex-extracted value stands.
- Removed per-run mutation of the module globals (`AI_PROVIDER`, provider API
  keys, `OWN_COMPANY`, `OWN_VAT_NUMBERS`): concurrent moves in one worker could
  read another company's VAT or another provider's credentials mid-run. The
  Odoo model now builds a per-run config dict (`invoice_ocr.default_config()`)
  and passes it to `extract_invoice_data`; the globals remain as env-derived
  defaults for standalone use.
- `_parse_amount`: a dot with no comma and exactly three digits after the last
  dot is now treated as a thousands separator ("1.234" → 1234, "SEK 3.020" →
  3020); two-decimal amounts ("539.00") still parse as decimals.
- `_extract_text_tesseract` caps the number of rendered pages
  (`INVOICE_OCR_MAX_PAGES`, default 10) and makes the render scale
  configurable (`INVOICE_OCR_SCALE`, default 2), so a very long scanned PDF
  cannot pin a worker for minutes.
- OpenAI provider can now be configured from the settings UI
  (`invoice_ocr.openai_api_key`, `invoice_ocr.openai_model` system parameters;
  key stored as a password field).
- Documented the 6000-character LLM truncation limit and the complete
  environment-variable list in the module README.
- Added unit tests (no Odoo) for `_ai_answer_problems` (tolerance, token limit,
  reference comparison), the regex/AI merge (REGEX_WINS + conflicts), and the
  EU/export account-code remapping (now a testable lib function).

### Fixed (review, PR #1 — `account_invoice_ocr_ai` 19.0.1.8.1)

- Removed the premature `cr.commit()` in `_extend_with_attachments`; committing
  mid-create also committed `super()`'s work and the create itself. The bulk
  server action keeps its intentional commit-per-move.
- Bounded the synchronous upload path: staik timeout is now capped at
  `STAIK_TIMEOUT` (default 120 s, was 300 s) and the reliability re-run is
  skipped when the first AI call already took `INVOICE_AI_RETRY_SKIP_SECONDS`
  (default 60 s). Limitation documented in the README; async (queue_job)
  remains future work.
- Escaped OCR/LLM values with `markupsafe.escape` in the chatter note to prevent
  HTML injection.

### Initial release

- Initial public release of `account_invoice_ocr_ai` (invoice OCR + LLM) and
  `hr_expense_ocr_ai` (receipt OCR + LLM), extracted from Molnkontakt's private
  Odoo repository.
