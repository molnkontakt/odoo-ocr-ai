"""The generic provider layer: presets, custom endpoint, Ollama, schema fallback, 429 retry, verify."""

import invoice_ocr as inv
import pytest


class FakeResponse:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def _openai_payload(content, model="served-model", tokens=1234):
    return {"model": model, "usage": {"completion_tokens": tokens},
            "choices": [{"finish_reason": "stop", "message": {"content": content}}]}


class CallLog(list):
    """Recorded (url, kwargs) per call; `responders` are consumed in order, default answers {"ok": true}."""

    def __init__(self):
        super().__init__()
        self.responders = []


@pytest.fixture
def calls(monkeypatch):
    log = CallLog()

    def fake_post(url, **kwargs):
        log.append((url, kwargs))
        responder = log.responders.pop(0) if log.responders else (lambda u, k: FakeResponse(200, _openai_payload('{"ok": true}')))
        return responder(url, kwargs)

    monkeypatch.setattr(inv, "_post", fake_post)
    monkeypatch.setattr(inv.time, "sleep", lambda s: None)
    return log


def test_presets_and_custom_endpoint(monkeypatch):
    monkeypatch.setattr(inv, "AI_PROVIDER", "staik")
    monkeypatch.setattr(inv, "STAIK_API_KEY", "k1")
    assert inv.resolve_endpoint() == ("https://api.staik.se/v1", "k1", inv.STAIK_MODEL)
    monkeypatch.setattr(inv, "AI_PROVIDER", "openai_compatible")
    monkeypatch.setattr(inv, "AI_BASE_URL", "https://api.mistral.ai/v1/")
    monkeypatch.setattr(inv, "AI_API_KEY", "k2")
    monkeypatch.setattr(inv, "AI_MODEL", "mistral-large-latest")
    assert inv.resolve_endpoint() == ("https://api.mistral.ai/v1", "k2", "mistral-large-latest")
    monkeypatch.setattr(inv, "AI_MODEL", "")
    with pytest.raises(ValueError, match="no model"):
        inv.resolve_endpoint()
    monkeypatch.setattr(inv, "AI_PROVIDER", "nope")
    with pytest.raises(ValueError, match="unknown AI provider"):
        inv.resolve_endpoint()


def test_chat_json_sends_schema_and_returns_meta(calls, monkeypatch):
    monkeypatch.setattr(inv, "AI_PROVIDER", "openai_compatible")
    monkeypatch.setattr(inv, "AI_BASE_URL", "http://vllm.local:8000/v1")
    monkeypatch.setattr(inv, "AI_API_KEY", "")
    monkeypatch.setattr(inv, "AI_MODEL", "local-model")
    data, meta = inv.chat_json("P", "text", {"type": "object"}, "x", max_tokens=50, max_chars=2)
    url, kw = calls[0]
    assert url == "http://vllm.local:8000/v1/chat/completions"
    assert "Authorization" not in kw["headers"], "no key → no header"
    assert kw["json"]["response_format"]["json_schema"]["name"] == "x"
    assert kw["json"]["messages"][0]["content"] == "Pte", "text is capped at max_chars"
    assert data == {"ok": True} and meta["served_model"] == "served-model" and meta["completion_tokens"] == 1234


def test_schema_unsupported_falls_back_to_plain_completion(calls, monkeypatch):
    monkeypatch.setattr(inv, "AI_PROVIDER", "openai")
    monkeypatch.setattr(inv, "OPENAI_API_KEY", "k")
    calls.responders.append(lambda u, k: FakeResponse(400, {"error": "response_format not supported"}))
    data, _ = inv.chat_json("P", "t", {"type": "object"}, "x")
    assert len(calls) == 2
    assert "response_format" in calls[0][1]["json"] and "response_format" not in calls[1][1]["json"]
    assert data == {"ok": True}


def test_429_is_retried_once(calls, monkeypatch):
    monkeypatch.setattr(inv, "AI_PROVIDER", "venice")
    monkeypatch.setattr(inv, "VENICE_API_KEY", "k")
    calls.responders.append(lambda u, k: FakeResponse(429, {}))
    data, _ = inv.chat_json("P", "t", {"type": "object"}, "x")
    assert len(calls) == 2 and data == {"ok": True}
    assert calls[0][0].startswith("https://api.venice.ai/api/v1/")


def test_ollama_uses_native_api(calls, monkeypatch):
    monkeypatch.setattr(inv, "AI_PROVIDER", "ollama")
    monkeypatch.setattr(inv, "OLLAMA_URL", "http://localhost:11434/")
    monkeypatch.setattr(inv, "OLLAMA_MODEL", "qwen2.5:7b")
    calls.responders.append(lambda u, k: FakeResponse(200, {"model": "qwen2.5:7b", "eval_count": 42, "done_reason": "stop",
                                                            "message": {"content": '{"ok": true}'}}))
    data, meta = inv.chat_json("P", "t", {"type": "object"}, "x")
    url, kw = calls[0]
    assert url == "http://localhost:11434/api/chat"
    assert kw["json"]["format"] == {"type": "object"} and kw["json"]["stream"] is False
    assert data == {"ok": True} and meta["completion_tokens"] == 42


def test_verify_provider_reports_served_model_and_failure(calls, monkeypatch):
    monkeypatch.setattr(inv, "AI_PROVIDER", "staik")
    monkeypatch.setattr(inv, "STAIK_API_KEY", "k")
    calls.responders.append(lambda u, k: FakeResponse(200, _openai_payload('{"ok": true}', model="other-model", tokens=5)))
    res = inv.verify_provider()
    assert res["ok"] is True and res["model_served"] == "other-model" and res["model_requested"] == inv.STAIK_MODEL
    calls.responders.append(lambda u, k: FakeResponse(401, {"error": "bad key"}))
    res = inv.verify_provider()
    assert res["ok"] is False and "401" in res["error"]


def test_reasoning_token_check_only_for_thinking_models():
    plain = {"total_amount": 100.0, "subtotal": 80.0, "vat_amount": 20.0, "lines": [{"amount": 80.0}],
             "_completion_tokens": 300, "_served_model": "gpt-4o-mini"}
    assert not [p for p in inv._ai_answer_problems(plain) if "completion-tokens" in p]
    thinking = dict(plain, _served_model="qwen3.6:35b-a3b-thinking")
    assert [p for p in inv._ai_answer_problems(thinking) if "completion-tokens" in p]
