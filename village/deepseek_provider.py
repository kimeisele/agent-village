"""
Agent Village — DeepSeek Cognitive Provider
=============================================
Concrete adapter implementing village/cognitive_provider.py's
CognitiveProvider interface. stdlib-only HTTP (urllib.request) -- same
pattern as village/moltbook_captcha.py::_deepseek_solve() (referenced
for error handling/timeout/no-secret-in-logs conventions, not copied:
that function solves a short math captcha and returns a number, this one
runs a bounded work-order analysis prompt and returns structured text).

Default model: **deepseek-v4-flash**. Verified directly (not just taken
from a secondary source) against DeepSeek's own docs before writing this
file, 2026-07-19:
- https://api-docs.deepseek.com/quick_start/pricing/ : two current
  models, `deepseek-v4-flash` (cheaper, non-thinking-by-default) and
  `deepseek-v4-pro`. The legacy names `deepseek-chat`/`deepseek-reasoner`
  (still used in village/moltbook_captcha.py::_deepseek_solve(), out of
  scope for this slice) are deprecated 2026-07-24 15:59 UTC -- 5 days
  after this file was written.
- https://api-docs.deepseek.com/updates/ : confirms the same deprecation
  date and the two new model names, both reachable via the same base
  URL/OpenAI-compatible ChatCompletions shape already used by
  _deepseek_solve(), so no request-shape change was needed here.

`deepseek-v4-flash` (not `-pro`) is the deliberate choice for Proof 1:
shallow structural analysis, no deep reasoning required, and it is the
cheaper of the two current models.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from village.cognitive_provider import (
    CognitiveProvider,
    ProviderAuthError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUsage,
)

DEEPSEEK_API_KEY_VAR = "DEEPSEEK_API_KEY"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL_VAR = "DEEPSEEK_MODEL"
DEFAULT_MODEL = "deepseek-v4-flash"

# USD per 1M tokens, cache-miss input price (the conservative case) +
# output price. Verified 2026-07-19 against
# https://api-docs.deepseek.com/quick_start/pricing/. A cache hit would
# make the real cost lower, never higher -- so a budget check against
# this estimate never passes on an optimistic guess, only ever the
# reverse.
_PRICE_PER_MILLION_TOKENS = {
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87},
}


class DeepSeekProvider(CognitiveProvider):
    name = "deepseek"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or os.environ.get(DEEPSEEK_MODEL_VAR, DEFAULT_MODEL)
        self._api_key = api_key if api_key is not None else os.environ.get(DEEPSEEK_API_KEY_VAR, "")

    def complete(self, prompt: str, *, max_tokens: int, timeout_seconds: float) -> ProviderResponse:
        if not self._api_key:
            raise ProviderAuthError(f"{DEEPSEEK_API_KEY_VAR} not set")

        body = json.dumps(
            {
                "model": self.model,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0,
                "max_tokens": max_tokens,
            }
        ).encode()

        req = urllib.request.Request(
            DEEPSEEK_URL,
            data=body,
            headers={"Authorization": f"Bearer {self._api_key}", "Content-Type": "application/json"},
            method="POST",
        )

        start = time.monotonic()
        try:
            with urllib.request.urlopen(req, timeout=timeout_seconds) as r:
                raw_bytes = r.read()
        except urllib.error.HTTPError as exc:
            status = exc.code
            # Only DeepSeek's own structured error message field is ever
            # surfaced -- never the raw response body verbatim (it could
            # in principle echo request data back; we don't rely on it
            # not doing so).
            try:
                detail = json.loads(exc.read()).get("error", {}).get("message", "")
            except Exception:
                detail = ""
            if status == 401:
                raise ProviderAuthError("DeepSeek rejected the API key (401)") from None
            if status == 429:
                raise ProviderRateLimitError(detail or "rate limited (429)") from None
            raise ProviderHTTPError(status, detail or "DeepSeek API error") from None
        except TimeoutError as exc:
            raise ProviderTimeoutError(f"DeepSeek request timed out after {timeout_seconds}s") from None
        except urllib.error.URLError as exc:
            raise ProviderTimeoutError(f"DeepSeek request failed: {exc.reason}") from None
        duration = time.monotonic() - start

        try:
            resp = json.loads(raw_bytes)
        except ValueError as exc:
            raise ProviderResponseError(f"non-JSON response from DeepSeek: {exc}") from None

        try:
            content = resp["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(f"unexpected DeepSeek response shape: {exc}") from None

        usage_raw = resp.get("usage", {}) or {}
        prompt_tokens = int(usage_raw.get("prompt_tokens", 0))
        completion_tokens = int(usage_raw.get("completion_tokens", 0))
        total_tokens = int(usage_raw.get("total_tokens", prompt_tokens + completion_tokens))
        pricing = _PRICE_PER_MILLION_TOKENS.get(self.model, _PRICE_PER_MILLION_TOKENS[DEFAULT_MODEL])
        cost_usd = (prompt_tokens * pricing["input"] + completion_tokens * pricing["output"]) / 1_000_000

        usage = ProviderUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            duration_seconds=duration,
        )
        # `raw` deliberately excludes anything from the request (no
        # prompt, no headers, no key) -- only DeepSeek's own response
        # object, for evidence/audit purposes at the caller's discretion.
        return ProviderResponse(content=content, provider=self.name, model=self.model, usage=usage, raw=resp)
