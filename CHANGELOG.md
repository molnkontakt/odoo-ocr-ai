# Changelog

## Unreleased

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
  that reports the model actually served. `account_invoice_ocr_ai` 19.0.1.9.0.

- Initial public release of `account_invoice_ocr_ai` (invoice OCR + LLM) and
  `hr_expense_ocr_ai` (receipt OCR + LLM), extracted from Molnkontakt's private
  Odoo repository.
