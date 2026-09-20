# hr_expense_ocr_ai

Receipt OCR + LLM for expense claims in Odoo 19 Community, where
`hr_expense_extract` (Enterprise) is not available. Depends on
[`account_invoice_ocr_ai`](../account_invoice_ocr_ai/) for the OCR/LLM library
and the provider settings.

## When it runs

| Trigger | How |
|---|---|
| A draft expense gets its main attachment: an e-mailed expense with a photo, or a receipt uploaded via the API with `message_main_attachment_id` set | `write` hook, only when amount or category is still missing |
| **Read receipt (OCR)** button on the expense form, or the list action of the same name | `action_read_receipt()` |

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
  chain from the products.
- The amount must occur in the text.
- Confidence below 0.6 fills nothing (typically a downscaled, unreadable photo
  that was read as 339 instead of 389).
- The category must be one of the company's expensable products. Their
  *purchase description* is sent as a hint, so describe your categories in Odoo
  ("fuel, oil, tools for chainsaw and mower …") for better matches.

Photos are EXIF-rotated, converted to greyscale, upscaled to 2000 px and OCR'd
with `--psm 4`. A 250 kB phone photo takes about 5 s of tesseract and 30 s of
LLM time. A `429` from the provider is retried once after 15 s; if the LLM
fails altogether, a regex fallback still fills amount and date.
