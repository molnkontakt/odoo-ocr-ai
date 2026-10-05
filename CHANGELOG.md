# Changelog

## Unreleased

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
