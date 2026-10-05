# Changelog

## Unreleased

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
  printed first.)

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
