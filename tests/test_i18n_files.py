"""The shipped translations match the code (#35.3, #36.12): every _() string of a module —
its models and its library — is in the module's .pot, and i18n/sv.po translates every msgid
of the .pot with the same placeholders. Regenerate the .pot with Odoo's exporter
(odoo-bin i18n export) after changing a string."""

import ast
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
MODULES = ("account_invoice_ocr_ai", "hr_expense_ocr_ai")
PLACEHOLDER = re.compile(r"%(?:\(\w+\))?[sdr]|%(?:\(\w+\))?\.\d+f")


ESCAPES = {"n": "\n", "t": "\t", '"': '"', "\\": "\\"}


def _unquote(chunk):
    """The text of the quoted PO strings in `chunk`, joined, with \\n, \\t, \\" and \\\\ undone."""
    text = "".join(part[1:-1] for part in re.findall(r'"(?:[^"\\]|\\.)*"', chunk))
    return re.sub(r"\\(.)", lambda m: ESCAPES.get(m.group(1), m.group(1)), text)


def read_po(path):
    """{msgid: msgstr} of a .po/.pot file (no plurals or contexts in these files)."""
    entries = {}
    for block in path.read_text(encoding="utf-8").split("\n\n"):
        m = re.search(r'^msgid ((?:".*"\n?)+)^msgstr ((?:".*"\n?)+)', block + "\n", re.M)
        if m and _unquote(m.group(1)):
            entries[_unquote(m.group(1))] = _unquote(m.group(2))
    return entries


def code_strings(module):
    """The literal first arguments of _() / env._() calls in the module's Python code."""
    found = set()
    for path in (ROOT / module).rglob("*.py"):
        if "tests" in path.parts:
            continue
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call) or not node.args:
                continue
            func = node.func
            name = func.id if isinstance(func, ast.Name) else (
                func.attr if isinstance(func, ast.Attribute) else None)
            if name == "_" and isinstance(node.args[0], ast.Constant) \
                    and isinstance(node.args[0].value, str):
                found.add(node.args[0].value)
    return found


@pytest.mark.parametrize("module", MODULES)
def test_every_code_string_is_in_the_pot(module):
    pot = read_po(ROOT / module / "i18n" / f"{module}.pot")
    missing = sorted(code_strings(module) - set(pot))
    assert not missing, f"not in {module}.pot (run odoo-bin i18n export): {missing}"


@pytest.mark.parametrize("module", MODULES)
def test_swedish_translates_every_msgid(module):
    pot = read_po(ROOT / module / "i18n" / f"{module}.pot")
    sv = read_po(ROOT / module / "i18n" / "sv.po")
    assert set(sv) == set(pot), set(sv) ^ set(pot)
    untranslated = [msgid for msgid, msgstr in sv.items() if not msgstr]
    assert not untranslated
    for msgid, msgstr in sv.items():
        assert sorted(PLACEHOLDER.findall(msgid)) == sorted(PLACEHOLDER.findall(msgstr)), msgid


def test_swedish_terms():
    sv = read_po(ROOT / "account_invoice_ocr_ai" / "i18n" / "sv.po")
    assert sv["Run OCR again"] == "Kör OCR igen"
    assert sv["Debited automatically"] == "Dras automatiskt"
    assert "omvänd skattskyldighet" in sv[
        "The supplier is abroad but charged Swedish VAT (%(number)s): booked as Swedish input "
        "VAT, without reverse charge."]
    hr = read_po(ROOT / "hr_expense_ocr_ai" / "i18n" / "sv.po")
    assert hr["Read receipt (OCR)"] == "Läs kvitto (OCR)"
