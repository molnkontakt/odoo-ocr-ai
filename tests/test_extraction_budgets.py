"""Bounded text extraction (#26): a page cap for pdfplumber and tesseract (the first pages
and the last), a pixel budget per rendered page and receipt photo, a tesseract timeout, one
time budget for the whole reading and a size cap for receipt images. A budget that cut the
reading leaves a note for the reviewer.

The tesseract path runs with a fake pytesseract (the binary is not needed); pdfplumber and
pypdfium2 run for real on small generated PDFs.
"""

import io
import types

import invoice_ocr as inv
import pytest
import receipt_ocr as r
from PIL import Image, ImageOps


def make_pdf(texts, size=(595, 842)):
    """A minimal valid PDF with one page per text (empty text: a page without a text layer)."""
    width, height = size
    objs = {1: b"<< /Type /Catalog /Pages 2 0 R >>",
            3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>"}
    kids = []
    for i, text in enumerate(texts):
        page, content = 4 + 2 * i, 5 + 2 * i
        kids.append(b"%d 0 R" % page)
        stream = b"BT /F1 12 Tf 72 720 Td (%s) Tj ET" % text.encode() if text else b""
        objs[page] = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 %d %d] "
                      b"/Resources << /Font << /F1 3 0 R >> >> /Contents %d 0 R >>"
                      % (width, height, content))
        objs[content] = b"<< /Length %d >>\nstream\n%s\nendstream" % (len(stream), stream)
    objs[2] = b"<< /Type /Pages /Kids [%s] /Count %d >>" % (b" ".join(kids), len(texts))
    out, offsets = b"%PDF-1.4\n", {}
    for key in sorted(objs):
        offsets[key] = len(out)
        out += b"%d 0 obj\n%s\nendobj\n" % (key, objs[key])
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1)
    out += b"".join(b"%010d 00000 n \n" % offsets[key] for key in sorted(objs))
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objs) + 1, xref)
    return out


def page_text(n):
    return f"Page {n} of the test document with enough words to count"


def run_cfg(**limits):
    cfg = inv._cfg(limits)
    return cfg, inv.document_run(cfg)


class FakeTesseract:
    """Stands in for pytesseract: records (page size, timeout) and answers with text."""

    def __init__(self, timeout_on=()):
        self.calls = []
        self.timeout_on = set(timeout_on)

    def image_to_string(self, image, lang=None, config="", timeout=0):
        self.calls.append((image.size, timeout))
        if len(self.calls) in self.timeout_on:
            raise RuntimeError("Tesseract process timeout")
        return f"scanned text number {len(self.calls)} long enough to be used as the reading"


@pytest.fixture
def tesseract(monkeypatch):
    fake = FakeTesseract()
    monkeypatch.setattr(inv, "HAS_TESSERACT", True)
    monkeypatch.setattr(inv, "pytesseract", fake, raising=False)
    monkeypatch.setattr(r, "HAS_TESSERACT", True)
    monkeypatch.setattr(r, "pytesseract", fake, raising=False)
    monkeypatch.setattr(r, "Image", Image, raising=False)
    monkeypatch.setattr(r, "ImageOps", ImageOps, raising=False)
    return fake


def test_pages_to_read_keeps_the_first_pages_and_the_last():
    assert inv.pages_to_read(5, 20) == [1, 2, 3, 4, 5]
    assert inv.pages_to_read(200, 5) == [1, 2, 3, 4, 200]
    assert inv.pages_to_read(200, 1) == [200]
    assert inv.pages_to_read(None, 3) == [1, 2, 3], "unknown count: the first pages"
    assert inv.pages_to_read(4, 0) == [1, 2, 3, 4], "no cap: every page"


def test_pdfplumber_reads_the_capped_pages_and_says_so():
    pdf = make_pdf([page_text(n) for n in range(1, 31)])
    cfg, run = run_cfg(max_text_pages=5)
    text = inv.extract_text(pdf, cfg)
    assert [n for n in range(1, 31) if f"Page {n} of" in text] == [1, 2, 3, 4, 30]
    assert run.notes == ["the PDF has 30 pages; the text was read from only pages 1–4 and 30 "
                         "(page limit 5)"]
    # a short PDF: every page, no note
    cfg, run = run_cfg(max_text_pages=5)
    assert "Page 3 of" in inv.extract_text(make_pdf([page_text(n) for n in (1, 2, 3)]), cfg)
    assert run.notes == []


def test_reading_stops_when_the_time_budget_is_spent(monkeypatch):
    ticks = types.SimpleNamespace(now=0.0)

    def clock():  # every look at the clock costs 7 s
        ticks.now += 7
        return ticks.now

    monkeypatch.setattr(inv, "_clock", clock)
    pdf = make_pdf([page_text(n) for n in range(1, 11)])
    cfg, run = run_cfg(extract_time_budget=30, max_text_pages=50)
    text = inv.extract_text(pdf, cfg)
    read = [n for n in range(1, 11) if f"Page {n} of" in text]
    assert read and len(read) < 10
    assert any(n.startswith(f"reading the document text stopped after {len(read)} of 10 pages")
               and "(30 s)" in n for n in run.notes), run.notes


def test_extraction_never_runs_past_the_document_deadline(monkeypatch):
    cfg, run = run_cfg(extract_time_budget=30, total_deadline=90)
    ticks = types.SimpleNamespace(now=run.deadline + 1)  # the document's time is up
    monkeypatch.setattr(inv, "_clock", lambda: ticks.now)
    text = inv.extract_text(make_pdf([page_text(1)]), cfg)
    assert text == "" and any("stopped after 0 of 1 pages" in n for n in run.notes)


def test_tesseract_page_cap_timeout_and_budget_per_run(tesseract):
    pdf = make_pdf([""] * 12)  # a scan: no text layer
    cfg, run = run_cfg(max_ocr_pages=3, tesseract_timeout=20)
    text = inv.extract_text(pdf, cfg)
    assert len(tesseract.calls) == 3, "pages 1, 2 and 12"
    assert all(0 < timeout <= 20 for _size, timeout in tesseract.calls)
    assert "scanned text number 3" in text
    assert "the PDF has 12 pages; OCR read only pages 1–2 and 12 (page limit 3)" in run.notes


def test_tesseract_timeout_skips_the_page(tesseract):
    tesseract.timeout_on = {2}
    cfg, run = run_cfg(max_ocr_pages=3, tesseract_timeout=20)
    text = inv.extract_text(make_pdf([""] * 3), cfg)
    assert len(tesseract.calls) == 3
    assert "number 1" in text and "number 3" in text and "number 2" not in text
    assert any(n.startswith("tesseract took longer than 20 s on page 2") for n in run.notes)


def test_huge_page_is_rendered_within_the_pixel_budget(tesseract):
    pdf = make_pdf([""], size=(14400, 14400))  # the PDF maximum: 2.6 GB at scale 2
    cfg, run = run_cfg(max_page_pixels=1_000_000, ocr_scale=2)
    inv.extract_text(pdf, cfg)
    (width, height), _timeout = tesseract.calls[0]
    assert width * height <= 1_000_000 * 1.01
    assert any("page 1 is very large" in n for n in run.notes)


def test_receipt_photo_is_scaled_to_the_pixel_budget(tesseract):
    buffer = io.BytesIO()
    Image.new("L", (3000, 2000), 255).save(buffer, format="PNG")
    cfg, run = run_cfg(max_page_pixels=1_000_000)
    r.extract_text(buffer.getvalue(), "image/png", "receipt.png", cfg)
    (width, height), timeout = tesseract.calls[0]
    assert width * height <= 1_000_000 * 1.01
    assert 0 < timeout <= inv.TESSERACT_TIMEOUT
    assert run.notes == ["the image (3000×2000) was scaled down to 1 megapixels for OCR"]


def test_receipt_image_over_the_size_cap_is_not_read(tesseract):
    buffer = io.BytesIO()
    Image.new("L", (300, 200), 255).save(buffer, format="PNG")
    raw = buffer.getvalue()
    with pytest.raises(r.ReceiptReadError, match="more than the 0 MB a receipt image may have"):
        r.read_text(raw, "image/png", "receipt.png", {"max_image_bytes": len(raw) - 1})
    assert tesseract.calls == []


def test_budget_notes_reach_the_results(tesseract, monkeypatch):
    """The notes end up where the Odoo modules show them: _notes and notes."""
    monkeypatch.setattr(inv, "_extract_fields_ai", lambda text, reference=None, config=None: {})
    pdf = make_pdf([page_text(n) for n in range(1, 31)])
    out = inv.extract_invoice_data(pdf, config={"max_text_pages": 5})
    assert any("page limit 5" in n for n in out["_notes"])

    monkeypatch.setattr(r, "_chat_json", lambda prompt, text, config=None: {})
    buffer = io.BytesIO()
    Image.new("L", (3000, 2000), 255).save(buffer, format="PNG")
    res = r.extract_receipt_data(buffer.getvalue(), "image/png", "receipt.png",
                                 config={"max_page_pixels": 1_000_000})
    assert any("scaled down to 1 megapixels" in n for n in res["notes"])


def test_limits_are_read_from_the_settings():
    cfg = inv.config_from_settings({"max_text_pages": "8", "tesseract_timeout": "12.5",
                                    "max_image_bytes": "0"}.get)
    assert cfg["max_text_pages"] == 8 and cfg["tesseract_timeout"] == 12.5
    assert cfg["max_image_bytes"] == inv.MAX_IMAGE_BYTES
