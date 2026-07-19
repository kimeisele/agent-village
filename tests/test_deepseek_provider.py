"""
Tests for village/deepseek_provider.py. No real API calls -- urllib's
urlopen is monkeypatched with recorded/synthetic responses. Includes the
explicit secret-redaction test required for this slice.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

import village.deepseek_provider as dsp
from village.cognitive_provider import (
    ProviderAuthError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)

FAKE_SECRET = "sk-THIS-IS-A-FAKE-SECRET-VALUE-1234567890"


class _FakeHTTPResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self) -> bytes:
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _mock_success(monkeypatch, body: dict):
    def fake_urlopen(req, timeout=None):
        return _FakeHTTPResponse(json.dumps(body).encode())

    monkeypatch.setattr(dsp.urllib.request, "urlopen", fake_urlopen)


def _mock_http_error(monkeypatch, status: int, error_body: dict | None = None):
    def fake_urlopen(req, timeout=None):
        fp = io.BytesIO(json.dumps(error_body or {}).encode())
        raise urllib.error.HTTPError(dsp.DEEPSEEK_URL, status, "error", {}, fp)

    monkeypatch.setattr(dsp.urllib.request, "urlopen", fake_urlopen)


# ── Missing secret ────────────────────────────────────────────────────────


def test_missing_api_key_raises_auth_error_without_calling_network(monkeypatch):
    monkeypatch.delenv(dsp.DEEPSEEK_API_KEY_VAR, raising=False)
    calls = []
    monkeypatch.setattr(dsp.urllib.request, "urlopen", lambda *a, **k: calls.append(1))

    provider = dsp.DeepSeekProvider(api_key="")
    with pytest.raises(ProviderAuthError):
        provider.complete("hello", max_tokens=100, timeout_seconds=5)
    assert calls == []  # never even attempted a network call


# ── Successful call ───────────────────────────────────────────────────────


def test_successful_call_returns_parsed_response(monkeypatch):
    _mock_success(monkeypatch, {
        "choices": [{"message": {"content": '{"gaps": []}'}}],
        "usage": {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120},
    })
    provider = dsp.DeepSeekProvider(model="deepseek-v4-flash", api_key=FAKE_SECRET)

    resp = provider.complete("analyze this", max_tokens=100, timeout_seconds=5)

    assert resp.content == '{"gaps": []}'
    assert resp.provider == "deepseek"
    assert resp.model == "deepseek-v4-flash"
    assert resp.usage.prompt_tokens == 100
    assert resp.usage.completion_tokens == 20
    assert resp.usage.total_tokens == 120
    assert resp.usage.cost_usd > 0


def test_default_model_is_deepseek_v4_flash(monkeypatch):
    monkeypatch.delenv(dsp.DEEPSEEK_MODEL_VAR, raising=False)
    provider = dsp.DeepSeekProvider(api_key=FAKE_SECRET)
    assert provider.model == "deepseek-v4-flash"


# ── HTTP errors ───────────────────────────────────────────────────────────


def test_401_raises_provider_auth_error(monkeypatch):
    _mock_http_error(monkeypatch, 401, {"error": {"message": "invalid key"}})
    provider = dsp.DeepSeekProvider(api_key=FAKE_SECRET)
    with pytest.raises(ProviderAuthError):
        provider.complete("x", max_tokens=10, timeout_seconds=5)


def test_429_raises_provider_rate_limit_error(monkeypatch):
    _mock_http_error(monkeypatch, 429, {"error": {"message": "rate limited"}})
    provider = dsp.DeepSeekProvider(api_key=FAKE_SECRET)
    with pytest.raises(ProviderRateLimitError):
        provider.complete("x", max_tokens=10, timeout_seconds=5)


def test_500_raises_provider_http_error(monkeypatch):
    _mock_http_error(monkeypatch, 500, {"error": {"message": "server error"}})
    provider = dsp.DeepSeekProvider(api_key=FAKE_SECRET)
    with pytest.raises(ProviderHTTPError) as exc_info:
        provider.complete("x", max_tokens=10, timeout_seconds=5)
    assert exc_info.value.status == 500


# ── Timeout ───────────────────────────────────────────────────────────────


def test_timeout_raises_provider_timeout_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise TimeoutError("timed out")

    monkeypatch.setattr(dsp.urllib.request, "urlopen", fake_urlopen)
    provider = dsp.DeepSeekProvider(api_key=FAKE_SECRET)
    with pytest.raises(ProviderTimeoutError):
        provider.complete("x", max_tokens=10, timeout_seconds=5)


def test_url_error_raises_provider_timeout_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        raise urllib.error.URLError("connection refused")

    monkeypatch.setattr(dsp.urllib.request, "urlopen", fake_urlopen)
    provider = dsp.DeepSeekProvider(api_key=FAKE_SECRET)
    with pytest.raises(ProviderTimeoutError):
        provider.complete("x", max_tokens=10, timeout_seconds=5)


# ── Malformed JSON / unexpected shape ──────────────────────────────────────


def test_non_json_response_raises_provider_response_error(monkeypatch):
    def fake_urlopen(req, timeout=None):
        return _FakeHTTPResponse(b"not json at all")

    monkeypatch.setattr(dsp.urllib.request, "urlopen", fake_urlopen)
    provider = dsp.DeepSeekProvider(api_key=FAKE_SECRET)
    with pytest.raises(ProviderResponseError):
        provider.complete("x", max_tokens=10, timeout_seconds=5)


def test_unexpected_response_shape_raises_provider_response_error(monkeypatch):
    _mock_success(monkeypatch, {"unexpected": "shape"})
    provider = dsp.DeepSeekProvider(api_key=FAKE_SECRET)
    with pytest.raises(ProviderResponseError):
        provider.complete("x", max_tokens=10, timeout_seconds=5)


# ── Secret redaction (explicit, required test) ────────────────────────────


def test_api_key_never_appears_in_any_raised_exception(monkeypatch, capsys):
    """The single most important test in this file: whatever goes wrong,
    the credential must never leak into an exception message, and
    therefore never into a log line derived from str(exception)."""
    scenarios = []

    # HTTP error path
    _mock_http_error(monkeypatch, 500, {"error": {"message": "server error"}})
    provider = dsp.DeepSeekProvider(api_key=FAKE_SECRET)
    try:
        provider.complete("x", max_tokens=10, timeout_seconds=5)
    except Exception as e:
        scenarios.append(str(e))
        scenarios.append(repr(e))

    # Auth error path (401)
    _mock_http_error(monkeypatch, 401, {"error": {"message": "invalid key"}})
    try:
        provider.complete("x", max_tokens=10, timeout_seconds=5)
    except Exception as e:
        scenarios.append(str(e))
        scenarios.append(repr(e))

    # Malformed response path
    def fake_urlopen(req, timeout=None):
        return _FakeHTTPResponse(b"not json")
    monkeypatch.setattr(dsp.urllib.request, "urlopen", fake_urlopen)
    try:
        provider.complete("x", max_tokens=10, timeout_seconds=5)
    except Exception as e:
        scenarios.append(str(e))
        scenarios.append(repr(e))

    assert len(scenarios) >= 3
    for text in scenarios:
        assert FAKE_SECRET not in text

    captured = capsys.readouterr()
    assert FAKE_SECRET not in captured.out
    assert FAKE_SECRET not in captured.err


def test_api_key_never_appears_in_the_provider_response_object(monkeypatch):
    """The successful-path ProviderResponse (including `.raw`, which
    might get persisted as evidence) must not contain the key either --
    it only ever appears in the outgoing request's Authorization header,
    never in anything DeepSeek echoes back."""
    _mock_success(monkeypatch, {
        "choices": [{"message": {"content": "ok"}}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    })
    provider = dsp.DeepSeekProvider(api_key=FAKE_SECRET)
    resp = provider.complete("x", max_tokens=10, timeout_seconds=5)

    assert FAKE_SECRET not in json.dumps(resp.raw)
    assert FAKE_SECRET not in resp.content
