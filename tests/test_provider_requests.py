"""What is sent to the provider and how its errors are handled (#19, #20, #23, #9).

A fake HTTP layer stands in for the provider; the transfer bound is also checked against a
real local server that trickles its answer.
"""

import http.server
import json
import socketserver
import threading
import time

import invoice_ocr as inv
import pytest


class FakeResponse:
    def __init__(self, status, payload=None, headers=None):
        self.status_code = status
        self._payload = payload if payload is not None else {}
        self.headers = headers or {}

    def json(self):
        return self._payload


def _ok(content='{"ok": true}', model="m", tokens=1234, finish="stop", message=None):
    return FakeResponse(200, {"model": model, "usage": {"completion_tokens": tokens},
                              "choices": [{"finish_reason": finish,
                                           "message": {"content": content, **(message or {})}}]})


def _error(status, message, param=None, code=None):
    return FakeResponse(status, {"error": {"message": message, "type": "invalid_request_error",
                                           "param": param, "code": code}})


@pytest.fixture
def calls(monkeypatch):
    """Records (url, kwargs) per request; `calls.answers` are returned in order, then ok."""

    class Calls(list):
        answers = []

    log = Calls()
    log.answers = []

    def fake_post(url, **kwargs):
        log.append((url, kwargs))
        return log.answers.pop(0) if log.answers else _ok()

    monkeypatch.setattr(inv, "_post", fake_post)
    monkeypatch.setattr(inv.time, "sleep", lambda s: None)
    return log


OPENAI = {"provider": "openai", "openai_api_key": "k", "openai_model": "gpt-4o-mini"}
CUSTOM = {"provider": "openai_compatible", "base_url": "https://llm.example/v1", "api_key": "k",
          "model": "local-model"}


def _chat(cfg, **kwargs):
    return inv.chat_json("P", "text", {"type": "object"}, "x", config=cfg, **kwargs)


# ── #19: OpenAI reasoning models, parameter adaptation, the provider's error text ──

@pytest.mark.parametrize(("model", "reasoning"), [
    ("o1", True), ("o1-mini", True), ("o3", True), ("o3-pro", True), ("o4-mini", True),
    ("gpt-5", True), ("gpt-5-mini", True), ("gpt-5.1", True), ("openai/o3-mini", True),
    ("openai/gpt-5-nano", True), ("gpt-4o-mini", False), ("gpt-4.1", False), ("qwen3", False),
    ("llama-o1", False), ("omni", False), ("ollama", False), ("", False), (None, False),
])
def test_openai_reasoning_model_names(model, reasoning):
    assert inv.is_openai_reasoning_model(model) is reasoning


def test_request_parameters_per_model(calls):
    _chat(OPENAI, max_tokens=500)
    body = calls[-1][1]["json"]
    assert body["max_completion_tokens"] == 500 and "max_tokens" not in body
    assert body["temperature"] == 0, "a non-reasoning OpenAI model keeps temperature 0"
    for model in ("o3-mini", "gpt-5-mini"):
        _chat(dict(OPENAI, openai_model=model), max_tokens=500)
        body = calls[-1][1]["json"]
        assert body["max_completion_tokens"] == 500 and "max_tokens" not in body
        assert "temperature" not in body, model
        assert body["response_format"]["type"] == "json_schema"
    # another endpoint: max_tokens, unless the model is one of OpenAI's reasoning models
    _chat(CUSTOM, max_tokens=500)
    body = calls[-1][1]["json"]
    assert body["max_tokens"] == 500 and body["temperature"] == 0
    _chat(dict(CUSTOM, model="openai/o4-mini"), max_tokens=500)
    body = calls[-1][1]["json"]
    assert body["max_completion_tokens"] == 500 and "temperature" not in body


def test_max_tokens_comes_from_the_config(calls):
    _chat(dict(CUSTOM, max_tokens=12000))
    assert calls[-1][1]["json"]["max_tokens"] == 12000
    _chat(CUSTOM)
    assert calls[-1][1]["json"]["max_tokens"] == inv.AI_MAX_TOKENS == 8000


def test_unsupported_parameters_are_dropped_one_by_one(calls):
    """An endpoint that rejects max_tokens and then temperature (OpenAI's wording): each is
    adapted once, the schema is kept."""
    calls.answers = [
        _error(400, "Unsupported parameter: 'max_tokens' is not supported with this model. "
                    "Use 'max_completion_tokens' instead.", "max_tokens", "unsupported_parameter"),
        _error(400, "Unsupported value: 'temperature' does not support 0 with this model. Only "
                    "the default (1) value is supported.", "temperature", "unsupported_value"),
    ]
    data, _meta = _chat(CUSTOM, max_tokens=700)
    assert data == {"ok": True} and len(calls) == 3
    first, second, third = (kw["json"] for _url, kw in calls)
    assert first["max_tokens"] == 700 and first["temperature"] == 0
    assert second["max_completion_tokens"] == 700 and "max_tokens" not in second
    assert second["temperature"] == 0
    assert "temperature" not in third and third["max_completion_tokens"] == 700
    assert all("response_format" in body for body in (first, second, third))


def test_rejected_parameter_named_only_in_the_message(calls):
    """A pydantic-based server (no OpenAI error fields) that does not know
    max_completion_tokens gets max_tokens."""
    calls.answers = [FakeResponse(400, {"object": "error", "message": (
        "[{'type': 'extra_forbidden', 'loc': ('body', 'max_completion_tokens'), "
        "'msg': 'Extra inputs are not permitted', 'input': 800}]")})]
    _chat(dict(OPENAI, openai_model="o3-mini"), max_tokens=800)
    second = calls[1][1]["json"]
    assert second["max_tokens"] == 800 and "max_completion_tokens" not in second


def test_a_parameter_is_dropped_only_once(calls):
    rejected = _error(400, "Unsupported value: 'temperature' does not support 0.", "temperature",
                      "unsupported_value")
    calls.answers = [rejected, rejected]
    with pytest.raises(inv.ProviderError, match="does not support 0"):
        _chat(CUSTOM)
    assert len(calls) == 2


def test_other_400s_are_not_taken_for_a_schema_problem(calls):
    """Only a 400 about the JSON schema takes the plain-completion fallback; any other fails at
    once, with what the provider said (#19)."""
    calls.answers = [_error(400, "This model's maximum context length is 8192 tokens. However, "
                                 "your messages resulted in 9000 tokens.", "messages",
                            "context_length_exceeded")]
    with pytest.raises(inv.ProviderError) as caught:
        _chat(CUSTOM)
    assert len(calls) == 1, "no schema fallback, no second request"
    assert caught.value.status == 400
    assert "maximum context length is 8192 tokens" in str(caught.value)
    assert str(caught.value).startswith("HTTP 400 from the AI provider: ")


@pytest.mark.parametrize("message", [
    "response_format not supported",
    "Invalid schema for response_format 'invoice': additionalProperties is required",
    "This endpoint does not support structured outputs",
    "json_schema is not available for this model",
    "Failed to parse grammar",
])
def test_schema_errors_fall_back_to_a_plain_completion(calls, message):
    calls.answers = [FakeResponse(400, {"error": message})]
    data, _meta = _chat(CUSTOM)
    assert data == {"ok": True} and len(calls) == 2
    assert "response_format" in calls[0][1]["json"]
    assert "response_format" not in calls[1][1]["json"]
    assert calls[1][1]["json"]["max_tokens"] == calls[0][1]["json"]["max_tokens"]


def test_error_text_is_kept_and_cut(calls):
    calls.answers = [_error(401, "Incorrect API key provided: sk-ab***yz. " + "x" * 1000)]
    with pytest.raises(inv.ProviderError) as caught:
        _chat(OPENAI)
    assert "HTTP 401 from the AI provider: Incorrect API key provided" in str(caught.value)
    assert len(caught.value.detail) <= inv.ERROR_DETAIL_CHARS
    # the body's text when it is not OpenAI-shaped
    calls.answers = [FakeResponse(502, {"detail": "upstream timed out"})]
    with pytest.raises(inv.ProviderError, match="HTTP 502 from the AI provider: upstream timed out"):
        _chat(OPENAI)


def test_verify_shows_the_providers_error(calls):
    calls.answers = [_error(400, "Unsupported parameter: 'foo'")]
    res = inv.verify_provider(OPENAI)
    assert res["ok"] is False
    assert res["error"] == "HTTP 400 from the AI provider: Unsupported parameter: 'foo'"


# ── #9: Retry-After on 429 ───────────────────────────────────────────────────

def test_retry_after_parsing():
    assert inv.retry_after_seconds({"Retry-After": "3"}) == 3.0
    assert inv.retry_after_seconds({"retry-after": " 2.5 "}) == 2.5
    assert inv.retry_after_seconds({"retry-after-ms": "1500", "Retry-After": "9"}) == 1.5
    assert inv.retry_after_seconds({"Retry-After": "Wed, 21 Oct 2015 07:28:00 GMT"}) == 0.0
    assert inv.retry_after_seconds({"Retry-After": "Fri, 01 Jan 2100 00:00:00 GMT"}) > 1e9
    assert inv.retry_after_seconds({"Retry-After": "-4"}) == 0.0
    for nothing in (None, {}, {"Retry-After": "soon"}, {"X-Other": "1"}):
        assert inv.retry_after_seconds(nothing) is None


# ── #20: Ollama ──────────────────────────────────────────────────────────────

OLLAMA = {"provider": "ollama", "ollama_url": "http://gpu.local:11434", "ollama_model": "qwen"}


def _ollama_answer(done="stop", prompt_tokens=900, content='{"ok": true}'):
    return FakeResponse(200, {"model": "qwen", "eval_count": 42, "done_reason": done,
                              "prompt_eval_count": prompt_tokens,
                              "message": {"content": content}})


def test_ollama_gets_context_size_and_output_limit(calls):
    calls.answers = [_ollama_answer()]
    data, meta = _chat(OLLAMA, max_tokens=900)
    body = calls[0][1]["json"]
    assert body["options"] == {"temperature": 0, "num_ctx": 16384, "num_predict": 900}
    assert body["truncate"] is False
    assert data == {"ok": True} and meta["notes"] == []
    calls.answers = [_ollama_answer()]
    _chat(dict(OLLAMA, ollama_num_ctx=32768))
    assert calls[1][1]["json"]["options"]["num_ctx"] == 32768
    assert calls[1][1]["json"]["options"]["num_predict"] == inv.AI_MAX_TOKENS


def test_ollama_truncation_is_noted(calls):
    calls.answers = [_ollama_answer(done="length", content='{"ok": tr')]
    data, meta = _chat(OLLAMA, max_tokens=900)
    assert data == {}
    assert any("cut off" in n and "900" in n and "16384" in n for n in meta["notes"])
    calls.answers = [_ollama_answer(prompt_tokens=16000)]
    _data, meta = _chat(OLLAMA)
    assert any("filled the model's context (16000 of 16384 tokens)" in n for n in meta["notes"])


def test_ollama_error_shows_its_text(calls):
    calls.answers = [FakeResponse(400, {"error": "input length exceeds the context length"})]
    with pytest.raises(inv.ProviderError, match="input length exceeds the context length"):
        _chat(OLLAMA)


def test_a_cut_answer_is_noted_on_the_bill(calls):
    calls.answers = [_ok(content='{"vendor_name": "V', finish="length")] * 2
    out = inv.extract_invoice_data_from_text("Fakturanummer: 4711\n", config=dict(
        CUSTOM, max_tokens=4000))
    assert out["invoice_number"] == "4711"
    assert any("cut off at its token limit (4000 tokens)" in n for n in out["_notes"])


def test_only_the_used_answers_notes_reach_the_bill(calls):
    """A first answer cut off and a sound re-run: the cut is not noted."""
    good = ('{"vendor_name": "V AB", "total_amount": 125, "subtotal": 100, "vat_amount": 25, '
            '"lines": [{"description": "x", "amount": 100, "vat_rate": 25}]}')
    calls.answers = [_ok(content='{"vendor_name": "V', finish="length"), _ok(content=good)]
    out = inv.extract_invoice_data_from_text("Att betala: 125,00\n", config=CUSTOM)
    assert out["vendor_name"] == "V AB" and len(calls) == 2
    assert not any("cut off" in n for n in out.get("_notes", []))


def test_skipped_reasoning_is_noted_when_kept(calls):
    answer = ('{"vendor_name": "V AB", "total_amount": 125, "subtotal": 100, "vat_amount": 25, '
              '"lines": [{"description": "x", "amount": 90, "vat_rate": 25}]}')
    calls.answers = [_ok(content=answer, model="base", tokens=300)] * 2
    out = inv.extract_invoice_data_from_text(
        "Att betala: 125,00\n", config=dict(CUSTOM, model="base-thinking"))
    assert len(calls) == 2, "re-run once"
    assert any("only 300 completion tokens" in n for n in out["_notes"])


# ── #23: which model answered ────────────────────────────────────────────────

@pytest.mark.parametrize(("requested", "served", "shown", "matches"), [
    ("gpt-4o-mini", "gpt-4o-mini", False, True),
    ("GPT-4o-mini", "gpt-4o-mini", False, True),
    ("gpt-4o-mini", "gpt-4o-mini-2024-07-18", False, True),        # dated snapshot
    ("claude-x", "claude-x-20250929", False, True),
    ("gpt-4o", "gpt-4o-mini", False, False),                       # no prefix match …
    ("gpt-4o-mini", "gpt-4o", False, False),                       # … either way
    ("gpt-4o-mini", "gpt-4o-mini-beta", False, False),
    ("qwen3.6:35b-a3b-thinking", "qwen3.6:35b-a3b", True, True),   # base name, reasoned
    ("qwen3.6:35b-a3b-thinking", "qwen3.6:35b-a3b", False, False),  # base name, no reasoning
    ("qwen3.6:35b-a3b-thinkng", "qwen3.6:35b-a3b", True, False),   # a typo is a mismatch
    ("qwen3.6:35b-a3b", "qwen3.6:35b-a3b-thinking", True, False),
    ("model-x", None, False, True),                                # nothing to compare
])
def test_served_model_matches(requested, served, shown, matches):
    assert inv.served_model_matches(requested, served, shown) is matches


def test_verify_reports_tokens_reasoning_and_match(calls):
    staik = {"provider": "staik", "staik_api_key": "k", "staik_model": "qwen3.6:35b-a3b-thinking"}
    calls.answers = [_ok(model="qwen3.6:35b-a3b", tokens=212)]
    res = inv.verify_provider(staik)
    assert res["ok"] and res["model_matches"] and res["reasoning_shown"]
    assert res["completion_tokens"] == 212 and res["finish_reason"] == "stop"
    assert calls[0][1]["json"]["max_tokens"] == inv.PING_MAX_TOKENS == 1000
    # a typo: the provider served its default model without reasoning
    calls.answers = [_ok(model="qwen3.6:35b-a3b", tokens=6)]
    res = inv.verify_provider(dict(staik, staik_model="qwen3.6:35b-a3b-thinkng"))
    assert res["ok"] and not res["model_matches"] and not res["reasoning_shown"]
    # the right name, but no reasoning came back: the fallback is not taken for a match
    calls.answers = [_ok(model="qwen3.6:35b-a3b", tokens=6)]
    assert not inv.verify_provider(staik)["model_matches"]
    # reasoning text in the answer counts, whatever the token count
    calls.answers = [_ok(model="qwen3.6:35b-a3b", tokens=None,
                         message={"reasoning_content": "Let me think."})]
    assert inv.verify_provider(staik)["model_matches"]


def test_verify_reports_a_cut_answer(calls):
    calls.answers = [_ok(content="", finish="length", tokens=1000)]
    res = inv.verify_provider(OPENAI)
    assert res["ok"] is False and res["finish_reason"] == "length"


def test_reasoning_skipped_uses_the_requested_name():
    data = {"_completion_tokens": 400, "_model": "x-thinking", "_served_model": "x"}
    assert inv.reasoning_skipped(data, 1000)
    assert not inv.reasoning_skipped(dict(data, _completion_tokens=1200), 1000)
    assert not inv.reasoning_skipped(dict(data, _model="x"), 1000)
    assert inv.reasoning_skipped({"_completion_tokens": 10, "_served_model": "y-reasoning"}, 50)
    assert not inv.reasoning_skipped({"_model": "x-thinking"}, 1000), "no count, no verdict"


# ── #21: the text limit and the other new limits are settings ───────────────

def test_new_limits_from_settings():
    cfg = inv.config_from_settings({"text_limit": "9000", "max_tokens": "16000",
                                    "ollama_num_ctx": 32768}.get)
    assert (cfg["text_limit"], cfg["max_tokens"], cfg["ollama_num_ctx"]) == (9000, 16000, 32768)
    cfg = inv.config_from_settings({"text_limit": "0", "ollama_num_ctx": ""}.get)
    assert cfg["text_limit"] == inv.TEXT_LIMIT and cfg["ollama_num_ctx"] == inv.OLLAMA_NUM_CTX


# ── #9: the whole transfer ends at the deadline (a real local server) ─────────

class _Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):
        pass

    def do_POST(self):
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        mode = self.path.strip("/")
        body = json.dumps({"ok": True}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        if mode == "chunked":
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            self.wfile.write(b"100000\r\n")  # announces a large chunk, then trickles it
        else:
            self.send_header("Content-Length", "100000" if mode == "trickle" else str(len(body)))
            self.end_headers()
        if mode == "fast":
            self.wfile.write(body)
            return
        try:
            for _byte in range(100):
                self.wfile.write(b" ")
                self.wfile.flush()
                time.sleep(0.1)
        except OSError:
            pass


@pytest.fixture
def server():
    srv = socketserver.ThreadingTCPServer(("127.0.0.1", 0), _Handler)
    srv.daemon_threads = True
    thread = threading.Thread(target=srv.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{srv.server_address[1]}"
    srv.shutdown()
    srv.server_close()


def test_a_fast_answer_is_read_whole(server):
    pytest.importorskip("requests")
    r = inv._post(f"{server}/fast", json={}, timeout=5, deadline=inv._clock() + 5)
    assert r.status_code == 200 and r.json() == {"ok": True}


@pytest.mark.parametrize("mode", ["trickle", "chunked"])
def test_a_trickling_answer_is_cut_at_the_deadline(server, mode):
    """Each byte arrives well within the read timeout, so only the deadline ends it."""
    pytest.importorskip("requests")
    start = time.monotonic()
    with pytest.raises(inv.DeadlineExceeded, match="still arriving"):
        inv._post(f"{server}/{mode}", json={}, timeout=5, deadline=inv._clock() + 0.5)
    assert time.monotonic() - start < 2.0


def test_every_request_carries_the_documents_deadline(calls):
    cfg = inv._cfg(dict(CUSTOM, total_deadline=60))
    run = inv.document_run(cfg)
    inv.chat_json("P", "t", {"type": "object"}, "x", config=cfg)
    assert calls[0][1]["deadline"] == run.deadline
