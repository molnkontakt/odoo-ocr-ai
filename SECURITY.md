# Security Policy

## Reporting a Vulnerability

If you discover a security issue, please report it **privately** via
[GitHub's private vulnerability reporting](https://github.com/molnkontakt/odoo-ocr-ai/security/advisories/new).

We aim to acknowledge reports within 5 business days and provide a status
update within 14 days.

## Scope

In scope:

- Code in this repository
- Python imports declared in module manifests

Out of scope:

- Vulnerabilities in upstream Odoo (report to Odoo SA directly)
- The AI providers themselves (staik, Venice, OpenAI, Ollama)

## Data handling

These modules send the **text** of uploaded invoices and receipts to the AI
provider configured in Odoo's settings. Choose the provider with the data
residency your bookkeeping requires; the default is staik (Sweden). API keys
are stored as Odoo system parameters and are never written to the repository.
