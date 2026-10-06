"""One deadline per document across every provider call (#9).

The text extraction, the first call, the 429 wait and retry, the schema fallback and the
reliability re-run all fit in the config's total_deadline: each request gets at most the
time that is left and none is started without enough of it. A fake clock and a fake HTTP
layer stand in for time and the provider.
"""

import invoice_ocr as inv
import pytest


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload or {}
        self.headers = headers or {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _ok(content='{"ok": true}', tokens=1234):
    return FakeResponse(200, {"model": "m", "usage": {"completion_tokens": tokens},
                              "choices": [{"finish_reason": "stop", "message": {"content": content}}]})


class Clock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


@pytest.fixture
def clock(monkeypatch):
    c = Clock()
    monkeypatch.setattr(inv, "_clock", c)
    monkeypatch.setattr(inv.time, "sleep", c.advance)
    return c


@pytest.fixture
def http(monkeypatch, clock):
    """Each queued step is (seconds the request takes, response); the timeouts are recorded."""

    class Http:
        steps = []
        timeouts = []

    def fake_post(url, **kwargs):
        Http.timeouts.append(kwargs["timeout"])
        seconds, response = Http.steps.pop(0) if Http.steps else (1, _ok())
        clock.advance(seconds)
        if isinstance(response, Exception):
            raise response
        return response

    Http.steps, Http.timeouts = [], []
    monkeypatch.setattr(inv, "_post", fake_post)
    return Http


CFG = {"provider": "openai_compatible", "base_url": "https://llm.example/v1", "api_key": "k",
       "model": "m", "timeout": 120}


def test_each_call_gets_only_the_time_left(http, clock):
    http.steps = [(30, FakeResponse(400, {"error": "response_format unsupported"})), (1, _ok())]
    data, _meta = inv.chat_json("P", "t", {"type": "object"}, "x",
                                config=dict(CFG, total_deadline=50))
    assert data == {"ok": True}
    # the per-call cap (120 s) is cut to the deadline: 50 s, then the 20 s that are left
    assert http.timeouts == [50, 20]


def test_429_wait_counts_against_the_deadline(http, clock):
    http.steps = [(10, FakeResponse(429)), (1, _ok())]
    inv.chat_json("P", "t", {"type": "object"}, "x", config=dict(CFG, total_deadline=60))
    # 10 s for the first call, 15 s of waiting: 35 s left for the retry
    assert http.timeouts == [60, 35]


def test_429_honours_retry_after(http, clock):
    http.steps = [(10, FakeResponse(429, headers={"Retry-After": "3"})), (1, _ok())]
    inv.chat_json("P", "t", {"type": "object"}, "x", config=dict(CFG, total_deadline=60))
    # 10 s for the first call, the 3 s the provider asked for: 47 s left for the retry
    assert http.timeouts == [60, 47]
    http.timeouts.clear()
    http.steps = [(1, FakeResponse(429, headers={"retry-after-ms": "500"})), (1, _ok())]
    inv.chat_json("P", "t", {"type": "object"}, "x", config=dict(CFG, total_deadline=60))
    assert http.timeouts == [60, 58.5]


def test_429_asking_for_more_than_the_deadline_gives_up(http, clock):
    http.steps = [(5, FakeResponse(429, headers={"Retry-After": "120"}))]
    with pytest.raises(inv.DeadlineExceeded, match="asks to wait 120 s"):
        inv.chat_json("P", "t", {"type": "object"}, "x", config=dict(CFG, total_deadline=60))
    assert len(http.timeouts) == 1 and clock.now == 1005.0, "no wait, no second request"


def test_a_second_429_is_an_error(http, clock):
    http.steps = [(1, FakeResponse(429, headers={"Retry-After": "1"})),
                  (1, FakeResponse(429, {"error": {"message": "rate limit reached"}}))]
    with pytest.raises(inv.ProviderError, match="HTTP 429 from the AI provider: rate limit"):
        inv.chat_json("P", "t", {"type": "object"}, "x", config=dict(CFG, total_deadline=60))
    assert len(http.timeouts) == 2


def test_429_without_time_to_wait_gives_up(http, clock):
    http.steps = [(5, FakeResponse(429))]
    with pytest.raises(inv.DeadlineExceeded, match="429"):
        inv.chat_json("P", "t", {"type": "object"}, "x", config=dict(CFG, total_deadline=20))
    assert len(http.timeouts) == 1, "no wait and no second request"
    assert clock.now == 1005.0


def test_no_call_is_started_without_time_left(http, clock):
    cfg = inv._cfg(dict(CFG, total_deadline=30))
    run = inv.document_run(cfg)
    clock.advance(30 - inv.MIN_CALL_SECONDS + 1)
    with pytest.raises(inv.DeadlineExceeded, match="time limit per document 30 s"):
        inv.chat_json("P", "t", {"type": "object"}, "x", config=cfg)
    assert http.timeouts == []
    assert run.remaining() < inv.MIN_CALL_SECONDS


def test_call_timeout_setting_caps_every_provider(http, clock):
    for provider in ({"provider": "staik", "staik_api_key": "k"}, CFG):
        http.timeouts.clear()
        inv.chat_json("P", "t", {"type": "object"}, "x",
                      config=dict(provider, call_timeout=30, total_deadline=90))
        assert http.timeouts == [30]


def test_the_text_extraction_counts_against_the_deadline(http, clock, monkeypatch):
    def slow_extract(pdf, config=None):
        clock.advance(25)
        return "Fakturanummer: 4711\nAtt betala: 125,00\n"

    monkeypatch.setattr(inv, "extract_text", slow_extract)
    answer = ('{"vendor_name": "V AB", "total_amount": 125, "subtotal": 100, "vat_amount": 25, '
              '"lines": [{"description": "x", "amount": 100, "vat_rate": 25}]}')
    http.steps = [(5, _ok(answer))]
    out = inv.extract_invoice_data(b"%PDF", config=dict(CFG, total_deadline=90))
    assert out["vendor_name"] == "V AB"
    assert http.timeouts == [65], "90 s for the document, 25 s of them spent on reading"


def test_reliability_rerun_only_when_it_fits(http, clock):
    suspect = ('{"vendor_name": "V AB", "total_amount": 125, "subtotal": 90, "vat_amount": 25, '
               '"lines": [{"amount": 90}]}')
    ref = {"total_amount": 125.0, "subtotal": 100.0, "vat_amount": 25.0}
    # the first call takes 40 s of 70: 30 s left, less than the 40 s a re-run would need
    http.steps = [(40, _ok(suspect))]
    out = inv._extract_fields_ai("text", reference=ref,
                                 config=dict(CFG, total_deadline=70, retry_skip_seconds=60))
    assert len(http.timeouts) == 1 and out["subtotal"] == 90
    # with 90 s there is room: the re-run is made, with the time that is left
    http.timeouts.clear()
    http.steps = [(40, _ok(suspect)), (10, _ok(suspect))]
    inv._extract_fields_ai("text", reference=ref,
                           config=dict(CFG, total_deadline=90, retry_skip_seconds=60))
    assert http.timeouts == [90, 50]


def test_worst_case_is_bounded_by_the_deadline(http, clock, monkeypatch):
    """Every request hangs until its timeout: the document still ends at the deadline."""
    def hang(url, **kwargs):
        http.timeouts.append(kwargs["timeout"])
        clock.advance(kwargs["timeout"])
        raise TimeoutError("read timed out")

    monkeypatch.setattr(inv, "_post", hang)
    start = clock.now
    out = inv.extract_invoice_data_from_text("Fakturanummer: 4711\nAtt betala: 125,00\n",
                                             config=dict(CFG, total_deadline=90))
    assert clock.now - start <= 90
    assert out["invoice_number"] == "4711", "the regex fields survive"
    assert "TimeoutError" in out["_ai_error"]
    assert any("the AI step failed (TimeoutError" in n for n in out["_notes"])


def test_provider_failure_is_reported_not_swallowed(http, clock):
    http.steps = [(1, FakeResponse(503, {"error": {"message": "the model is overloaded"}}))]
    out = inv.extract_invoice_data_from_text("Fakturanummer: 4711\nAtt betala: 125,00\n",
                                             config=CFG)
    assert out["_ai_error"] == "HTTP 503 from the AI provider: the model is overloaded"
    assert out["invoice_number"] == "4711"


def test_limits_from_settings():
    cfg = inv.config_from_settings({"call_timeout": "45", "total_deadline": 75}.get)
    assert cfg["call_timeout"] == 45 and cfg["total_deadline"] == 75
    for empty in ("", "0", 0, None, "abc", "-5", False):
        cfg = inv.config_from_settings({"call_timeout": empty, "total_deadline": empty}.get)
        assert cfg["call_timeout"] is None and cfg["total_deadline"] == inv.TOTAL_DEADLINE, empty


def test_receipt_ai_failure_is_reported(http, clock, monkeypatch):
    import receipt_ocr as r

    monkeypatch.setattr(r, "extract_text", lambda raw, mimetype=None, filename=None, config=None:
                        "Kvitto 2026-09-17\nTotalt 418,00\n")
    http.steps = [(1, FakeResponse(503))]
    res = r.extract_receipt_data(b"img", "image/jpeg", config=CFG)
    assert res["ai_error"] == "HTTP 503 from the AI provider"
    assert res["source"] == "regex" and res["fields"]["total"] == 418.0
    assert any("HTTP 503" in n for n in res["notes"])
