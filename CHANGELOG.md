# Changelog

## Unreleased

- Initial public release of `account_invoice_ocr_ai` (invoice OCR + LLM) and
  `hr_expense_ocr_ai` (receipt OCR + LLM), extracted from Molnkontakt's private
  Odoo repository.

### Fixed (review, PR #1 — `account_invoice_ocr_ai`)

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

### Fixed (review, PR #1 medium findings — `account_invoice_ocr_ai`)

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
