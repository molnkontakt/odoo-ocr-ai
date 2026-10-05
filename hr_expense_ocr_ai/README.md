# hr_expense_ocr_ai

Receipt OCR + LLM for expense claims in Odoo 19 Community, where
`hr_expense_extract` (Enterprise) is not available. Depends on
[`account_invoice_ocr_ai`](../account_invoice_ocr_ai/) for the OCR/LLM library
and the provider settings.

## When it runs

| Trigger | How |
|---|---|
| A draft expense gets its main attachment: an e-mailed expense with a photo, or a receipt uploaded via the API with `message_main_attachment_id` set | `write` hook, only when amount or category is still missing |
| **Read receipt (OCR)** button on the expense form | `action_read_receipt()`; a failure is shown as a readable error |
| The list action of the same name | `action_read_receipt_bulk()`: each expense in its own savepoint, a summary of how many were filled, failed or skipped |

A failure on the automatic path never breaks what triggered it (mail fetching,
upload): the read is rolled back on its own and a chatter note says why.

Off switch: *Expense receipt OCR* in the same settings block as the invoice OCR
(`expense_ocr.enabled`).

## What it fills

Only empty fields: amount (`total_amount_currency` = 0), date (when still today's
default), category (no product) and the description (empty or very short).
Everything read, what was filled and which guards fired is posted as a chatter
note, so the reviewer sees the model's reading next to the receipt.

## Guards against model guesses

Measured on real receipts before release:

- The merchant name must occur in the OCR text; the model otherwise guesses a
  chain from the products. Its distinctive words (not legal forms, countries or
  words like "store") must be printed together, as whole words.
- The amount and the date must be printed on the receipt, as a whole amount
  and a date (18 is not found in 418,00, nor 17 in a date). When the model's
  value is not, the value read from the receipt text is used instead, or the
  field stays empty; either way a note says so.
- Confidence below 0.6, or no confidence at all, leaves amount and date empty
  (typically a downscaled, unreadable photo that was read as 339 instead of
  389). Merchant, description and category may still be filled; the note says
  what was left out.
- The category must be one of the company's expensable products. Their
  *purchase description*, or else the category's *Guideline* as plain text (at
  most 200 characters), is sent as a hint, so describe your categories in Odoo
  ("fuel, oil, tools for chainsaw and mower …") for better matches.

Photos are EXIF-rotated, converted to greyscale, upscaled to 2000 px and OCR'd
with `--psm 4`. A 250 kB phone photo takes about 5 s of tesseract and 30 s of
LLM time. Provider handling (retries, JSON-schema fallback, Ollama) is shared with the
invoice module; if the LLM fails altogether, a regex fallback still fills
amount and date.
