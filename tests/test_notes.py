"""The libraries' notes are translatable in Odoo but plain English text everywhere else
(#35.3, #36.12): a Note is a str with its msgid, parameters and Odoo module."""

import ast
import pickle
import re
from pathlib import Path

import invoice_ocr as inv
import pytest
import receipt_ocr as r

ROOT = Path(__file__).resolve().parent.parent
LIBS = [ROOT / "account_invoice_ocr_ai/lib/invoice_ocr.py",
        ROOT / "hr_expense_ocr_ai/lib/receipt_ocr.py"]


def test_a_note_is_its_english_text():
    note = inv._("the PDF has %(total)s pages", total=30)
    assert note == "the PDF has 30 pages" and isinstance(note, str)
    assert note.msgid == "the PDF has %(total)s pages" and note.params == {"total": 30}
    assert note.addon == "account_invoice_ocr_ai"
    assert "; ".join([note, "x"]) == "the PDF has 30 pages; x"
    copy = pickle.loads(pickle.dumps(note))
    assert copy == note and copy.msgid == note.msgid and copy.params == note.params
    plain = inv._("100 %")  # no parameters: no formatting
    assert plain == "100 %" and plain.params == {}


def test_receipt_notes_belong_to_the_expense_module():
    note = r._("the date %(date)s is not printed on the receipt — ignored", date="2026-09-17")
    assert isinstance(note, inv.Note) and note.addon == "hr_expense_ocr_ai"


def test_error_messages_keep_their_note():
    error = inv.ProviderError(400, "bad parameter")
    message = inv.error_message(error)
    assert isinstance(message, inv.Note)
    assert message.msgid == "HTTP %(status)s from the AI provider: %(detail)s"
    assert inv.error_message(TimeoutError("read timed out")) == "TimeoutError: read timed out"
    with pytest.raises(ValueError) as caught:
        inv.resolve_endpoint({"provider": "nope"})
    assert isinstance(inv.error_message(caught.value), inv.Note)


def _note_calls(path):
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "_":
            yield node


@pytest.mark.parametrize("path", LIBS, ids=lambda p: p.name)
def test_every_note_is_a_literal_with_matching_parameters(path):
    """Odoo's exporter only finds literal strings in _(); the placeholders must be exactly
    the parameters passed, or the note (or its translation) cannot be formatted."""
    calls = list(_note_calls(path))
    assert len(calls) > 10
    for call in calls:
        assert call.args and isinstance(call.args[0], ast.Constant) \
            and isinstance(call.args[0].value, str), f"line {call.lineno}: not a literal"
        msgid = call.args[0].value
        placeholders = set(re.findall(r"%\((\w+)\)", msgid))
        keywords = {kw.arg for kw in call.keywords}
        if None in keywords:  # **params: checked where it is built
            continue
        assert placeholders == keywords, f"line {call.lineno}: {msgid!r}"
        assert not re.search(r"%(?!\(\w+\)[sdr.0-9]|%)", msgid) or not keywords, \
            f"line {call.lineno}: a bare % in {msgid!r}"
