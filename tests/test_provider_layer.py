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


# ── Per-run config: the provider layer never reads or writes run-time globals ─

def _globals_snapshot():
    """Every module-level setting of the library (upper-case names), copied."""
    return {k: (set(v) if isinstance(v, set) else v) for k, v in vars(inv).items() if k.isupper()}


@pytest.fixture
def env_defaults(monkeypatch):
    """Env-derived defaults that differ from anything a per-run config sets."""
    monkeypatch.setattr(inv, "AI_PROVIDER", "staik")
    monkeypatch.setattr(inv, "STAIK_API_KEY", "global-staik-key")
    monkeypatch.setattr(inv, "AI_BASE_URL", "")
    monkeypatch.setattr(inv, "AI_API_KEY", "")
    monkeypatch.setattr(inv, "AI_MODEL", "")


def test_per_run_config_is_used_without_touching_globals(calls, env_defaults):
    before = _globals_snapshot()
    cfg = {"provider": "openai_compatible", "base_url": "https://llm.example/v1/",
           "api_key": "K-company-a", "model": "model-x"}
    data, meta = inv.chat_json("P", "text", {"type": "object"}, "x", config=cfg)
    url, kw = calls[0]
    assert url == "https://llm.example/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer K-company-a"
    assert kw["json"]["model"] == "model-x" and meta["model"] == "model-x"
    assert kw["timeout"] == inv.AI_TIMEOUT, "non-staik calls are capped by INVOICE_AI_TIMEOUT"
    assert data == {"ok": True}
    assert _globals_snapshot() == before, "chat_json must not write module globals"

    # A call without config still goes to the env-derived default (staik), not to the
    # endpoint or key of the previous run.
    inv.chat_json("P", "text", {"type": "object"}, "x")
    url, kw = calls[1]
    assert url == "https://api.staik.se/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer global-staik-key"
    assert kw["timeout"] == inv.STAIK_TIMEOUT, "staik keeps its own cap (STAIK_TIMEOUT)"


def test_two_runs_with_different_configs_do_not_leak(calls, env_defaults):
    """Two companies in one worker: each run sees only its own provider and key."""
    before = _globals_snapshot()
    answer = '{"vendor_name": "V AB", "total_amount": 125, "subtotal": 100, "vat_amount": 25, ' \
             '"lines": [{"description": "x", "amount": 100, "vat_rate": 25, "account_code": "6540"}]}'
    calls.responders.extend([lambda u, k: FakeResponse(200, _openai_payload(answer, model="gpt-4o-mini"))] * 2)
    a = {"provider": "openai", "openai_api_key": "K-a", "openai_model": "gpt-4o-mini"}
    b = {"provider": "venice", "venice_api_key": "K-b"}
    ref = {"total_amount": 125.0, "subtotal": 100.0, "vat_amount": 25.0}
    assert inv._extract_fields_ai("text", reference=ref, config=a)["vendor_name"] == "V AB"
    assert inv._extract_fields_ai("text", reference=ref, config=b)["vendor_name"] == "V AB"
    assert calls[0][0].startswith(inv.OPENAI_URL) and calls[0][1]["headers"]["Authorization"] == "Bearer K-a"
    assert calls[1][0].startswith(inv.VENICE_URL) and calls[1][1]["headers"]["Authorization"] == "Bearer K-b"
    assert _globals_snapshot() == before


def test_ollama_reads_url_and_model_from_config(calls, env_defaults):
    before = _globals_snapshot()
    calls.responders.append(lambda u, k: FakeResponse(200, {"model": "llama3", "eval_count": 7,
                                                            "message": {"content": '{"ok": true}'}}))
    data, meta = inv.chat_json("P", "t", {"type": "object"}, "x",
                               config={"provider": "ollama", "ollama_url": "http://gpu.local:11434/",
                                       "ollama_model": "llama3"})
    url, kw = calls[0]
    assert url == "http://gpu.local:11434/api/chat" and kw["json"]["model"] == "llama3"
    assert data == {"ok": True} and meta["model"] == "llama3"
    assert _globals_snapshot() == before


def test_text_limit_from_config_caps_the_prompt(calls, env_defaults):
    inv._call_provider("abcdefghij", config={"text_limit": 3})
    assert calls[0][1]["json"]["messages"][0]["content"].endswith(inv.EXTRACTION_PROMPT[-10:] + "abc")


# ── Verify provider with the form's unsaved values ───────────────────────────

def test_verify_with_unsaved_form_values_leaves_globals_unchanged(calls, env_defaults):
    """The settings page's Verify builds a config from the form; nothing reaches the globals.

    Before: the button wrote the form's values into the module globals, so an unsaved key
    was used for real extractions and a cleared key kept working until a restart.
    """
    before = _globals_snapshot()
    form = {"provider": "openai_compatible", "base_url": "https://unsaved.example/v1",
            "api_key": "UNSAVED-KEY", "model": "unsaved-model", "staik_api_key": False}
    cfg = inv.config_from_settings(form.get)
    res = inv.verify_provider(cfg)
    url, kw = calls[0]
    assert url == "https://unsaved.example/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer UNSAVED-KEY"
    assert res["ok"] is True and res["provider"] == "openai_compatible"
    assert res["model_requested"] == "unsaved-model"
    assert _globals_snapshot() == before, "Verify must not write module globals"

    # The next real extraction (no Save in between) still uses the saved/env settings.
    inv._call_provider("invoice text")
    url, kw = calls[1]
    assert url == "https://api.staik.se/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer global-staik-key"
    assert "UNSAVED-KEY" not in str(calls[1])


def test_verify_failure_reports_config_provider_and_leaves_globals(calls, env_defaults):
    before = _globals_snapshot()
    calls.responders.append(lambda u, k: FakeResponse(401, {"error": "bad key"}))
    res = inv.verify_provider(inv.config_from_settings(
        {"provider": "venice", "venice_api_key": "wrong"}.get))
    assert res["ok"] is False and res["provider"] == "venice" and "401" in res["error"]
    assert _globals_snapshot() == before


def test_config_from_settings_empty_values_keep_defaults(env_defaults):
    """An empty form field / system parameter falls back to the env default, as a saved run does."""
    cfg = inv.config_from_settings({"provider": "staik", "staik_api_key": "", "staik_model": None}.get)
    assert cfg["staik_api_key"] == "global-staik-key"
    assert cfg["staik_model"] == inv.STAIK_MODEL
    cfg = inv.config_from_settings({"staik_api_key": " form-key "}.get)
    assert cfg["staik_api_key"] == "form-key"


def test_receipt_chat_uses_per_run_config(calls, env_defaults, monkeypatch):
    import receipt_ocr as r

    before = _globals_snapshot()
    answer = '{"merchant": "Kvitto", "date": "2026-09-17", "total": 418.0, "items": "x", ' \
             '"category_code": null, "confidence": 0.9}'
    calls.responders.append(lambda u, k: FakeResponse(200, _openai_payload(answer)))
    monkeypatch.setattr(r, "extract_text", lambda raw, mimetype=None, filename=None, config=None:
                        "Kvitto 3270143 2026-09-17\nTotalt 418,00\n")
    res = r.extract_receipt_data(b"img", "image/jpeg", config={
        "provider": "openai_compatible", "base_url": "https://receipts.example/v1",
        "api_key": "K-receipt", "model": "m"})
    url, kw = calls[0]
    assert url == "https://receipts.example/v1/chat/completions"
    assert kw["headers"]["Authorization"] == "Bearer K-receipt" and kw["timeout"] == 180
    assert res["source"] == "ai" and res["fields"]["total"] == 418.0
    assert _globals_snapshot() == before
