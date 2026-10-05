"""Test documents for the own-company and auto-debit guards (plain Python, no Odoo).

Modelled on a bank's invoice for a one-off fee that is debited automatically from the
buyer's account. Every party, number and account here is INVENTED: org numbers use the
placeholder series 999999-xxxx (with a correct check digit, so Odoo's VAT validation
accepts them), the buyer's account uses clearing number 9999, and the bankgiro numbers
are placeholders (with a correct mod-10 check digit, which the library checks). The text imitates pdfplumber's output, where spaces are often lost
('Betalningavfakturanskermedautomatik…').

Shared by tests/test_own_company_guards.py (pytest, repo root) and the Odoo tests in
this directory.
"""

# The receiving company (the buyer)
OWN_NAME = "Acme Receiver AB"
OWN_ORG = "999999-0006"
OWN_VAT = "SE999999000601"
# Clearing 9999, account 0012345; IBAN with a valid checksum
OWN_ACCOUNT_PRINTED = "99990012345"
OWN_IBAN = "SE7499900000099990012345"
OWN_BANKGIRO = "999-0003"

# The vendor: a bank charging a fee
VENDOR_NAME = "Example Bank AB"
VENDOR_ORG = "999999-0014"
VENDOR_VAT = "SE999999001401"

# Another company in the same database (a separate legal entity)
SISTER_NAME = "Example Sister Association"
SISTER_ORG = "999999-0030"
SISTER_VAT = "SE999999003001"
SISTER_BANKGIRO = "999-0029"

INVOICE_NUMBER = "900000000001"

AUTODEBIT_TEXT = """Faktura
ACMERECEIVERAB DATUM: 2026-06-17
STORGATAN1 PERIOD: 2026-04-01-2026-05-31
FÖRFALLODATUM: 2026-07-02
11122EXEMPELSTAD FAKTURANUMMER: 900000000001
Sweden ORG.NR: 9999990006
MOMSREG.NR:
Betalningavfakturanskermedautomatikfrånföretagetskonto.
FöretagetsavtaladetjänsterhosExampleBank
Tjänster Antal Prisperstyck Totaltbelopp
Additionalcharges
StartavgiftFlyttfrånannanbank 1 2500,00 2500,00
TotalAdditionalcharges SEK 2500,00
Totaltbelopp SEK 2500,00
Totaltmomsbelopp 0,00
Beloppetkommerattdebiterasföretagetskonto
SEK 2500,00
99990012345pervalutadag2026-07-02
Detjänsterdärmomsprocentinteärangivenärmomsbefriade.
ExampleBankAB(publ),11122Exempelstad,Styrelsenssäte:Exempelstad
Organisationsnr.:999999-0014,Momsreg.nr.:SE999999001401 www.example.com
"""

# What an LLM typically answers for the document above — including two mistakes the
# guards must catch: the buyer's org number and the buyer's account as "bankgiro".
AI_ANSWER_OWN_ORG = {
    "vendor_name": VENDOR_NAME,
    "invoice_number": INVOICE_NUMBER,
    "invoice_date": "2026-06-17",
    "due_date": "2026-07-02",
    "total_amount": 2500.0,
    "subtotal": 2500.0,
    "vat_amount": 0.0,
    "currency": "SEK",
    "org_number": "9999990006",
    "bankgiro": "9999-0012345",
    "lines": [{"description": "Startavgift Flytt från annan bank", "quantity": 1,
               "unit_price": 2500.0, "amount": 2500.0, "vat_rate": 0,
               "account_code": "6570"}],
}

# An ordinary invoice that is paid manually (no auto debit)
PLAIN_VENDOR_NAME = "Example Supplier AB"
PLAIN_VENDOR_ORG = "999999-0022"
PLAIN_VENDOR_BANKGIRO = "123-4566"
PLAIN_INVOICE_TEXT = """Faktura
Example Supplier AB
Org.nr: 999999-0022
Fakturanummer: 4711
Fakturadatum: 2026-06-01
Förfallodatum: 2026-06-30
Att betala: 1 250,00
Bankgiro: 123-4566
Betala enkelt med autogiro – anslut dig till autogiro på vår webb!
"""
