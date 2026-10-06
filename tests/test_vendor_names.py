"""Vendor names and VAT numbers that are near without matching (#39): a document's vendor
is never created next to an existing vendor that may be it. All names are invented."""

import invoice_ocr as inv


def test_name_is_near():
    assert inv.name_is_near("Example Market EU S.à r.l.", "Example Market")
    assert inv.name_is_near("Example Market EU S.a.r.l.", "Example Market Business")
    assert not inv.name_is_near("Example Market EU", "Other Market")
    assert not inv.name_is_near("Abc Consulting AB", "Abc Bygg AB"), "first word too short"
    assert not inv.name_is_near("Sverige AB", "Sverige Konsult AB"), "nothing distinctive"
    assert not inv.name_is_near("", "Example")


def test_vat_numbers_apart_from_formatting():
    key = inv.vat_number_key
    assert key("SE 999999-0014 01") == ("SE", "9999990014")
    assert key("999999-0014") == (None, "9999990014")
    assert key("el 123456783") == ("GR", "123456783")
    assert key("NL123456789B01") == ("NL", "123456789B01")
    assert key("12") == (None, "")
    same = inv.same_vat_number
    assert same("SE999999001401", "SE 999999-0014 01")
    assert same("999999-0014", "SE999999001401")
    assert same("LU12345613", "1234 5613")
    assert same("EL123456783", "GR 123456783")
    assert not same("LU12345613", "DE12345613")
    assert not same("SE999999001401", "SE999999002201")
    assert not same("", "")
