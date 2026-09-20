# Changelog

## Unreleased

- Provider layer generalised: any OpenAI-compatible endpoint (`openai_compatible`:
  base URL + key + model), Ollama for both modules, OpenAI settings in the UI,
  JSON-schema fallback and 429 retry for every provider, *Verify provider* button
  that reports the model actually served. `account_invoice_ocr_ai` 19.0.1.9.0.

- Initial public release of `account_invoice_ocr_ai` (invoice OCR + LLM) and
  `hr_expense_ocr_ai` (receipt OCR + LLM), extracted from Molnkontakt's private
  Odoo repository.
