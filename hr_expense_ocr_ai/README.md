# hr_expense_ocr_ai

Receipt OCR + LLM for expense claims in Odoo 19 Community, where
`hr_expense_extract` (Enterprise) is not available. Depends on
[`account_invoice_ocr_ai`](../account_invoice_ocr_ai/) for the OCR/LLM library
and the provider settings: every provider it supports (staik, Venice, OpenAI, any
OpenAI-compatible endpoint, a local Ollama) reads receipts too, with the same keys.

**Version requirement:** `hr_expense_ocr_ai` 19.0.1.6.0 needs
`account_invoice_ocr_ai` **19.0.1.16.0 or later**. It uses that module's
background queue (`ocr.queue.mixin`, its form banner and states), its per-run
settings (`account.move._invoice_ocr_config`, `_ocr_currency`,
`_ocr_notification`, the company's *Not VAT-registered* flag) and its library's
provider layer (`chat_json` with the answer's diagnostics, the reasoning check,
translatable notes). Odoo's `depends`
cannot pin a version, so install both from the same release of this repository.

## When it runs

| Trigger | How |
|---|---|
| A draft expense gets its main attachment: the **Upload** button, an e-mailed expense with a photo, or a receipt set via the API with `message_main_attachment_id` (in `create` or a later `write`) | Queued (`create`/`write` hooks), only when amount or category is still missing; the background job reads it within seconds |
| **Read receipt (OCR)** button on the expense form | `action_read_receipt()`: reads at once; a failure is shown as a readable error |
| The list action of the same name | `action_read_receipt_bulk()`: queues the selection and says how many were queued or skipped, and why |

The background job is the one of `account_invoice_ocr_ai` (*OCR: read queued
bills and receipts*), with the same time budget per run, retries, *OCR* state,
*OCR pending/failed* filters and form banner, and the same rule: an expense
someone changed after it was queued is not read (see that module's *When OCR
runs*). The job reads an expense as the user who queued it, with their rights
and language; an e-mailed expense (queued by the mail gateway) as the user of
its employee. The upload, the mail fetch or the API call returns at once, and a failed
read is rolled back on its own and noted in the chatter.

Off switch: *Expense receipt OCR* in the same settings block as the invoice OCR
(`expense_ocr.enabled`).

## What it fills

Only empty fields: amount (`total_amount_currency` = 0), date (when still today's
default), category (no product) and the description (empty or very short).
The placeholders of the standard **Upload** button count as empty: the generic
`EXP_GEN` category (or the first expensable product Upload falls back to) and the
"Untitled Expense <date>" description are replaced by what the receipt says.

A description that starts with one of the prefixes in the system parameter
`expense_ocr.placeholder_name_prefixes` (comma-separated, empty by default; also
*Placeholder name prefixes* in the settings) gets the receipt's description
appended, e.g. the subject a mail alias gives expenses from unknown senders.

The amount is written in the receipt's currency: the expense's currency is set
in the same write and the amount is rounded like that currency. A currency that
is unknown, inactive or has no exchange rate on or before the receipt date
leaves the amount empty with a note, and so does a category with a fixed cost set
by hand (its amount is quantity × cost, in the company's currency); such a
category (mileage) is never offered to the model, so a receipt is never put on it. Without the AI, a
total printed in a foreign currency (`EUR 12,50`, `€ 12,50`) is not read.
Everything read, what was filled, which guards fired and which model answered
(with its completion tokens) is posted as a chatter note, so the reviewer sees
the model's reading next to the receipt.

## Guards against model guesses

Measured on real receipts before release:

- The merchant name must occur in the OCR text; the model otherwise guesses a
  chain from the products. Its distinctive words (not legal forms, countries or
  words like "store") must be printed together, as whole words.
- The amount and the date must be printed on the receipt, as a whole amount
  and a date (18 is not found in 418,00, nor 17 in a date). When the model's
  value is not, the value read from the receipt text is used instead, or the
  field stays empty; either way a note says so.
- The date must lie in a plausible window: at most 3 days after today and at
  most 2 years before it (`RECEIPT_MAX_FUTURE_DAYS`, `RECEIPT_MAX_AGE_DAYS` in
  `lib/receipt_ocr.py`). A misread year ("2075") is never used: the first
  printed date in the window is taken instead, or the date stays empty.
- Confidence below 0.6, or no confidence at all, leaves amount and date empty
  (typically a downscaled, unreadable photo that was read as 339 instead of
  389). On a photo under one megapixel (`SMALL_IMAGE_PIXELS`) tesseract misreads
  digits even when the model is fairly sure, so there amount and date need a
  confidence of 0.9 (`SMALL_IMAGE_MIN_CONFIDENCE`). Merchant, description and
  category may still be filled; the note says what was left out.
- The VAT printed on the receipt is compared with what the expense's tax gives
  (when the VAT amount is printed, the expense has a tax and its amount is the
  receipt's total, also for a category set by hand): a difference of more than 1
  is noted, e.g. a 12 % restaurant receipt in a 25 % category. The tax itself is
  never changed.
- A company marked *Not VAT-registered* in the invoice OCR settings gets no tax
  on a receipt it reads: the total, VAT included, is the cost.
- The category must be one of the company's expensable products. Their
  *purchase description*, or else the category's *Guideline* as plain text (at
  most 200 characters), is sent as a hint, and the model is told to follow it.

### Steering the categories

The prompt has no rules of its own about which purchase belongs to which
category: it only asks for a specific category before the generic `EXP_GEN`
("Expenses"). Rules that depend on your organisation belong in the description
of the category (the expensable product's *purchase description*). For example,
if fuel, oil and spare parts for your machines should go to a *Machinery*
category rather than to *Travel*, describe it so:

| Category (product) | Purchase description |
|---|---|
| Machinery | Fuel, oil, chain lubricant, spare parts and tools for our chainsaws, mowers and other machines |
| Travel | Train and bus tickets, taxi, fuel for cars on business trips |

Each description goes to the model next to the category's code and name.

Photos are EXIF-rotated, converted to greyscale, upscaled to 2000 px and OCR'd
with `--psm 4`. A 250 kB phone photo takes about 5 s of tesseract and 30 s of
LLM time. A photo above 12 megapixels is scaled down to that, one larger than
20 MB is not read, and tesseract and the whole receipt have time limits (the
invoice module's *Time limits*); a limit that cut the reading is noted. Provider handling (retries, JSON-schema fallback, parameter adaptation, Ollama)
and the answer's token limit (`invoice_ocr.max_tokens`) are shared with the
invoice module. An answer cut off at its token limit, or one from a reasoning
model that skipped its reasoning (fewer than 300 completion tokens,
`RECEIPT_MIN_COMPLETION_TOKENS`), is read once more when there is time, and noted
when it stays so. If the LLM fails altogether, a regex fallback still fills amount and
date.
