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
