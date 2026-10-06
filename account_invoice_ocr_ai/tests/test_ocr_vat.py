"""Lines on the Swedish chart (l10n_se "se"): one tax per line, chosen from the line's final
account, the vendor's country and the printed VAT (#8, #22). All parties are invented."""
from odoo.tests import tagged

from .common import OcrBillCase

TEXT = "Invoice\nExample Supplier\nInvoice no: 4711\nInvoice date: 2026-06-01\n"


@tagged("post_install", "-at_install", "invoice_ocr")
class TestOcrVat(OcrBillCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls._se_company("Example Buyer", vat="SE999999000601")
        Partner = cls.env["res.partner"]

        def vendor(name, country):
            return Partner.create({"name": name, "is_company": True, "supplier_rank": 1,
                                   "country_id": cls.env.ref(f"base.{country}").id})
        cls.se_vendor = vendor("Example Leverantör", "se")
        cls.de_vendor = vendor("Example GmbH", "de")
        cls.us_vendor = vendor("Example Inc", "us")

    def _bill(self, partner=None, lines=(), text=TEXT, **ai):
        move = self.env["account.move"].with_company(self.company).create(
            {"move_type": "in_invoice", "partner_id": partner.id if partner else False})
        answer = {"vendor_name": partner.name if partner else None, "invoice_number": "4711",
                  "lines": list(lines), **ai}
        return self._run_ocr(move, text=text, ai=answer)

    def _line(self, move, name):
        return move.invoice_line_ids.filtered(lambda line: line.name == name)

    def _assert_tax(self, line, xmlid):
        self.assertEqual(line.tax_ids, self._tax(self.company, xmlid), line.name)

    def test_domestic_rates_goods_and_services(self):
        move = self._bill(self.se_vendor, [
            {"description": "Hardware", "amount": 100.0, "vat_rate": 25, "account_code": "4000"},
            {"description": "Support", "amount": 200.0, "vat_rate": 25, "account_code": "6540"},
            {"description": "Hotel", "amount": 300.0, "vat_rate": 12, "account_code": "5831"},
            {"description": "Train", "amount": 400.0, "vat_rate": 6, "account_code": "5810"},
        ])
        self._assert_tax(self._line(move, "Hardware"), "purchase_tax_25_goods")
        self._assert_tax(self._line(move, "Support"), "purchase_tax_25_services")
        self._assert_tax(self._line(move, "Hotel"), "purchase_tax_12_services")
        self._assert_tax(self._line(move, "Train"), "purchase_tax_6_services")
        self.assertEqual(self._line(move, "Hardware").account_id.code, "4000")
        self.assertAlmostEqual(move.amount_tax, 25 + 50 + 36 + 24)

    def test_eu_goods_and_services_on_one_bill(self):
        move = self._bill(self.de_vendor, [
            {"description": "Hardware", "amount": 1000.0, "vat_rate": 0, "account_code": "4000"},
            {"description": "Support", "amount": 500.0, "vat_rate": 0, "account_code": "6540"},
        ], vat_amount=0.0, subtotal=1500.0, total_amount=1500.0)
        goods, services = self._line(move, "Hardware"), self._line(move, "Support")
        self.assertEqual(goods.account_id.code, "4515")
        self._assert_tax(goods, "purchase_goods_tax_25_EC")
        self.assertIn("se_20", goods.tax_tag_ids.mapped("name"))
        self.assertEqual(services.account_id.code, "6540")
        self._assert_tax(services, "purchase_services_tax_25_EC")
        self.assertIn("se_21", services.tax_tag_ids.mapped("name"))
        self.assertEqual(move.amount_total, 1500.0, "reverse charge nets to zero")

    def test_import_of_goods_and_services_from_outside_the_eu(self):
        move = self._bill(self.us_vendor, [
            {"description": "Hardware", "amount": 800.0, "vat_rate": 0, "account_code": "4000"},
            {"description": "Cloud", "amount": 300.0, "vat_rate": 0, "account_code": "6540"},
        ], vat_amount=0.0)
        goods, services = self._line(move, "Hardware"), self._line(move, "Cloud")
        self.assertEqual(goods.account_id.code, "4545")
        self._assert_tax(goods, "purchase_goods_tax_25_NEC")
        self.assertIn("se_50", goods.tax_tag_ids.mapped("name"))
        self._assert_tax(services, "purchase_services_tax_25_NEC")
        self.assertIn("se_22", services.tax_tag_ids.mapped("name"))
        self.assertEqual(move.amount_total, 1100.0)
        self.assertIn("customs value", self._bodies(move))

    def test_out_of_scope_fee_line_is_not_reverse_charged(self):
        move = self._bill(self.de_vendor, [
            {"description": "Hosting", "amount": 1000.0, "vat_rate": 0, "account_code": "6540"},
            {"description": "Reminder fee", "amount": 60.0, "vat_rate": 0, "account_code": "6570"},
        ], vat_amount=0.0)
        self._assert_tax(self._line(move, "Hosting"), "purchase_services_tax_25_EC")
        fee = self._line(move, "Reminder fee")
        self.assertFalse(fee.tax_ids)
        self.assertEqual(fee.account_id.code, "6570")
        self.assertEqual(move.amount_total, 1060.0)

    def test_foreign_vat_is_part_of_the_cost(self):
        text = TEXT + "Subtotal 100,00\nVAT 7,00\nTotal amount 107,00\n"
        move = self._bill(self.de_vendor, [
            {"description": "Hotel room", "quantity": 2, "unit_price": 50.0, "amount": 100.0,
             "vat_rate": 6, "account_code": "5832"},
        ], text=text, vat_amount=7.0, subtotal=100.0, total_amount=107.0)
        line = self._line(move, "Hotel room")
        self.assertFalse(line.tax_ids)
        self.assertEqual(line.account_id.code, "5832")
        self.assertEqual(line.price_subtotal, 107.0)
        self.assertEqual(line.quantity, 2)
        self.assertEqual(move.amount_tax, 0.0)
        self.assertEqual(move.amount_total, 107.0)
        bodies = self._bodies(move)
        self.assertIn("foreign VAT (7.00)", bodies)
        self.assertNotIn("the lines do not match the bill", bodies,
                         "the totals check knows about it")

    def test_foreign_vat_spread_over_the_taxed_lines(self):
        move = self._bill(self.us_vendor, [
            {"description": "Room", "amount": 300.0, "vat_rate": 12, "account_code": "5832"},
            {"description": "Dinner", "amount": 100.0, "vat_rate": 12, "account_code": "5832"},
            {"description": "Card fee", "amount": 10.0, "vat_rate": 0, "account_code": "6570"},
        ], vat_amount=40.0)
        self.assertEqual(self._line(move, "Room").price_subtotal, 330.0)
        self.assertEqual(self._line(move, "Dinner").price_subtotal, 110.0)
        self.assertEqual(self._line(move, "Card fee").price_subtotal, 10.0)
        self.assertFalse(move.invoice_line_ids.tax_ids)
        self.assertEqual(move.amount_total, 450.0)

    def test_foreign_supplier_charging_swedish_vat(self):
        text = TEXT + "Moms deklarerat av Example Marketplace S.a.r.l. Moms # SE999999001401\n"
        move = self._bill(self.de_vendor, [
            {"description": "Cable", "amount": 1000.0, "vat_rate": 25, "account_code": "4000"},
        ], text=text, vat_amount=250.0, subtotal=1000.0, total_amount=1250.0)
        line = move.invoice_line_ids
        self.assertEqual(line.account_id.code, "4000")
        self._assert_tax(line, "purchase_tax_25_goods")
        self.assertEqual(move.amount_tax, 250.0)
        self.assertIn("charged Swedish VAT (SE999999001401)", self._bodies(move))

    def test_greek_el_prefix_is_an_eu_supplier(self):
        text = TEXT + "VAT Reg. No.: EL999999993\n"
        move = self._bill(None, [
            {"description": "Olive oil", "amount": 400.0, "vat_rate": 0, "account_code": "4000"},
        ], text=text, vendor_name="Hellenic Olive Traders", vat_amount=0.0)
        self.assertEqual(move.partner_id.country_id.code, "GR")
        line = move.invoice_line_ids
        self.assertEqual(line.account_id.code, "4515")
        self._assert_tax(line, "purchase_goods_tax_25_EC")

    def test_single_line_from_the_totals(self):
        """No AI lines: one line on the totals, its rate read off the printed amounts."""
        move = self._bill(self.se_vendor, [], subtotal=89.62, vat_amount=5.38,
                          total_amount=95.0)
        line = move.invoice_line_ids
        self.assertEqual(len(line), 1)
        self._assert_tax(line, "purchase_tax_6_goods")
        move = self._bill(self.de_vendor, [], subtotal=500.0, vat_amount=0.0, total_amount=500.0)
        self._assert_tax(move.invoice_line_ids, "purchase_goods_tax_25_EC")
        self.assertEqual(move.invoice_line_ids.account_id.code, "4515")

    def test_equipment_for_own_use_is_goods_on_its_cost_account(self):
        """A computer on 5410 (förbrukningsinventarier) is goods: Swedish 25 % G, and from
        another EU country an EU purchase of goods (box 20) that stays on 5410 (#39)."""
        move = self._bill(self.se_vendor, [
            {"description": "Laptop", "amount": 8000.0, "vat_rate": 25, "account_code": "5410"},
        ])
        self.assertEqual(move.invoice_line_ids.account_id.code, "5410")
        self._assert_tax(move.invoice_line_ids, "purchase_tax_25_goods")
        move = self._bill(self.de_vendor, [
            {"description": "Router", "amount": 2000.0, "vat_rate": 0, "account_code": "5410"},
        ], vat_amount=0.0)
        line = move.invoice_line_ids
        self.assertEqual(line.account_id.code, "5410")
        self._assert_tax(line, "purchase_goods_tax_25_EC")
        self.assertIn("se_20", line.tax_tag_ids.mapped("name"))

    def test_a_line_without_an_account_is_noted(self):
        move = self._bill(self.se_vendor, [
            {"description": "Something", "amount": 100.0, "vat_rate": 25},
        ])
        self.assertEqual(move.invoice_line_ids.account_id, move.journal_id.default_account_id)
        self.assertIn("line 'Something': the AI gave no account", self._bodies(move))
        move = self._bill(self.se_vendor, [], subtotal=100.0, vat_amount=25.0,
                          total_amount=125.0)
        self.assertIn("one line was made from the totals", self._bodies(move))

    # -- the account list (#24) --------------------------------------------------------

    def test_account_list_only_has_the_charts_accounts(self):
        Move = self.env["account.move"]
        codes = [code for code, _hint in Move._ocr_account_list(self.company)]
        self.assertIn("4000", codes)
        self.assertIn("6540", codes)
        self.assertNotIn("6231", codes, "not in l10n_se's chart")
        self.assertNotIn("6990", codes)
        self.env["res.config.settings"].create({
            "invoice_ocr_account_list": "6540: IT services\n9999: not in the chart\n5010: Rent",
        }).set_values()
        self.assertEqual(Move._ocr_account_list(self.company),
                         [("6540", "IT services"), ("5010", "Rent")])

    def test_account_not_in_the_chart_gets_the_fallback(self):
        move = self._bill(self.se_vendor, [
            {"description": "Cloud", "amount": 100.0, "vat_rate": 25, "account_code": "6231"},
        ])
        line = move.invoice_line_ids
        self.assertEqual(line.account_id, move.journal_id.default_account_id)
        self.assertEqual(line.account_id.code, "4000")
        self._assert_tax(line, "purchase_tax_25_goods")
        self.assertIn("account 6231 is not in the account list", self._bodies(move))

    # -- currency (#5) -----------------------------------------------------------------

    def _currency(self, name, active=True, rate_date=None, rate=0.1):
        currency = self.env["res.currency"].with_context(active_test=False).search(
            [("name", "=", name)], limit=1)
        currency.active = active
        self.env["res.currency.rate"].search([("currency_id", "=", currency.id)]).unlink()
        if rate_date:
            self.env["res.currency.rate"].create({
                "currency_id": currency.id, "name": rate_date, "rate": rate,
                "company_id": self.company.id})
        return currency

    LINE = {"description": "Hosting", "amount": 100.0, "vat_rate": 0, "account_code": "6540"}

    def test_bill_in_the_documents_currency(self):
        eur = self._currency("EUR", rate_date="2026-01-01", rate=0.1)  # 1 EUR = 10 SEK
        move = self._bill(self.de_vendor, [self.LINE], currency="EUR", vat_amount=0.0)
        self.assertEqual(move.currency_id, eur)
        line = move.invoice_line_ids
        self.assertEqual(line.price_subtotal, 100.0)
        self.assertAlmostEqual(line.balance, 1000.0)
        self.assertNotIn("no lines were created", self._bodies(move))

    def test_inactive_currency_gives_a_warning_and_no_lines(self):
        self._currency("NOK", active=False, rate_date="2026-01-01")
        move = self._bill(self.se_vendor, [self.LINE], currency="NOK")
        self.assertFalse(move.invoice_line_ids)
        self.assertEqual(move.currency_id, self.company.currency_id)
        bodies = self._bodies(move)
        self.assertIn("no lines were created", bodies)
        self.assertIn("NOK is not active", bodies)
        self.assertEqual(move.ref, "4711", "the rest of the fill is kept")

    def test_currency_without_a_rate_gives_a_warning_and_no_lines(self):
        self._currency("DKK", rate_date="2026-12-01")  # only a rate after the invoice date
        move = self._bill(self.se_vendor, [self.LINE], currency="DKK")
        self.assertFalse(move.invoice_line_ids)
        self.assertEqual(move.currency_id, self.company.currency_id)
        self.assertIn("DKK has no exchange rate on or before 2026-06-01", self._bodies(move))

    def test_unknown_or_local_currency(self):
        move = self._bill(self.se_vendor, [self.LINE], currency="XYZ")
        self.assertFalse(move.invoice_line_ids)
        self.assertIn("XYZ is not known", self._bodies(move))
        move = self._bill(self.se_vendor, [self.LINE], currency="kr")
        self.assertTrue(move.invoice_line_ids)
        self.assertEqual(move.currency_id, self.company.currency_id)

    def test_totals_check_warns_on_another_currency(self):
        move = self._bill(self.se_vendor, [self.LINE])
        self.env["account.move"]._check_ocr_totals(move, {"currency": "EUR"})
        self.assertIn("the bill is in SEK, the document in EUR", self._bodies(move))

    def test_bill_with_lines_keeps_its_currency(self):
        self._currency("EUR", rate_date="2026-01-01")
        move = self._bill(self.se_vendor, [self.LINE])
        self.assertEqual(move.currency_id, self.company.currency_id)
        self._run_ocr(move, text=TEXT, ai={"vendor_name": "x", "invoice_number": "4711",
                                           "currency": "EUR", "lines": [self.LINE]})
        self.assertEqual(move.currency_id, self.company.currency_id)
        self.assertEqual(len(move.invoice_line_ids), 1)
        self.assertIn("the bill in SEK: it already has lines", self._bodies(move))

    def test_rounding_difference_is_not_foreign_vat(self):
        move = self._bill(self.us_vendor, [
            {"description": "Cloud", "amount": 1000.0, "vat_rate": 0, "account_code": "6540"},
        ], subtotal=1000.0, total_amount=1000.4)
        self._assert_tax(move.invoice_line_ids, "purchase_services_tax_25_NEC")

    # -- the totals check (#25) ----------------------------------------------------------

    PRINTED = TEXT + "Netto 1 000,00\nMoms 250,00\nAtt betala 1 250,00\n"

    def test_lines_that_do_not_add_up_are_flagged(self):
        """A line the AI dropped: net, VAT and total are compared with the printed amounts."""
        move = self._bill(self.se_vendor, [
            {"description": "Support", "amount": 800.0, "vat_rate": 25, "account_code": "6540"},
        ], text=self.PRINTED)
        self.assertAlmostEqual(move.amount_total, 1000.0)
        bodies = self._bodies(move)
        self.assertIn("OCR: the lines do not match the bill", bodies)
        self.assertRegex(bodies, r"net [^ ]*800\.00[^ ]* against the bill's [^ ]*1,000\.00")
        self.assertRegex(bodies, r"VAT [^ ]*200\.00[^ ]* against the bill's [^ ]*250\.00")
        self.assertRegex(bodies, r"total [^ ]*1,000\.00[^ ]* against the bill's [^ ]*1,250\.00")

    def test_lines_that_add_up_are_not_flagged(self):
        move = self._bill(self.se_vendor, [
            {"description": "Support", "amount": 600.0, "vat_rate": 25, "account_code": "6540"},
            {"description": "Hardware", "amount": 400.0, "vat_rate": 25, "account_code": "4000"},
        ], text=self.PRINTED)
        self.assertAlmostEqual(move.amount_total, 1250.0)
        self.assertNotIn("the lines do not match the bill", self._bodies(move))
