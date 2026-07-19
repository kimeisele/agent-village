"""
Agent Village — DeepSeek Cognitive Provider
=============================================
Concrete adapter implementing village/cognitive_provider.py's
CognitiveProvider interface. stdlib-only HTTP (urllib.request) -- same
pattern as village/moltbook_captcha.py::_deepseek_solve() (referenced
for error handling/timeout/no-secret-in-logs conventions, not copied:
that function solves a short math captcha and returns a number, this one
runs a bounded work-order analysis and returns full response fidelity).

Default model: **deepseek-v4-flash**, verified 2026-07-19 against
DeepSeek's own docs (see docs/research/AGENT_LOOP_WORKER_02.md step 0 for
the full trail) -- unchanged from PR #13.

v2 finding (docs/research/AGENT_LOOP_WORKER_02.md, root-caused after PR
#13's first live run came back with an empty `content` at exactly
`max_tokens`): `deepseek-v4-flash` defaults to **thinking mode enabled**
(confirmed directly against
https://api-docs.deepseek.com/quick_start/pricing/ and
https://api-docs.deepseek.com/guides/thinking_mode). In thinking mode,
the model's reasoning goes into a separate `message.reasoning_content`
field (confirmed against
https://api-docs.deepseek.com/api/create-chat-completion) -- `content`
can legitimately be empty while `reasoning_content` holds real text, if
the completion-token budget runs out mid-reasoning (`finish_reason ==
"length"`) before the model reaches its final answer. That is exactly
what PR #13's live run hit at `max_tokens=2000`.

Two changes address this, both applied here:
1. Thinking mode is explicitly **disabled by default**
   (`{"thinking": {"type": "disabled"}}` in the request body, per the
   thinking-mode guide above) -- Proof 1's task is shallow structural
   analysis, not the kind of task thinking mode exists for, and a
   non-thinking call makes token spend and latency predictable.
   Configurable (`thinking_enabled=True`) for a future task that
   actually needs it.
2. `message.reasoning_content` and `choices[0].finish_reason` are read
   and surfaced regardless -- even with thinking disabled, this module
   never assumes empty `content` means an empty answer without checking
   whether reasoning text exists and what `finish_reason` says.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

from village.cognitive_provider import (
    CognitiveProvider,
    CognitiveResponse,
    ProviderAuthError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUsage,
)

DEEPSEEK_API_KEY_VAR = "DEEPSEEK_API_KEY"
DEEPSEEK_URL = "https://api.deepseek.com/chat/completions"
DEEPSEEK_MODEL_VAR = "DEEPSEEK_MODEL"
DEFAULT_MODEL = "deepseek-v4-flash"

# Realistic, not knapped at the wall (docs/research/AGENT_LOOP_WORKER_02.md
# step 5): large enough that a non-thinking structural-analysis answer
# has real room, small enough to stay a deliberate, bounded default --
# the actual per-call ceiling used by village/worker.py is set there,
# this is only the provider's own fallback if no override is given.
DEFAULT_MAX_TOKENS = 4096

# USD per 1M tokens, cache-miss input price (the conservative case) +
# output price (covers reasoning tokens too -- DeepSeek bills
# reasoning_tokens as part of completion_tokens, not separately).
# Verified 2026-07-19 against
# https://api-docs.deepseek.com/quick_start/pricing/.
_PRICE_PER_MILLION_TOKENS = {
    "deepseek-v4-flash": {"input": 0.14, "output": 0.28},
    "deepseek-v4-pro": {"input": 0.435, "output": 0.87},
}


class DeepSeekProvider(CognitiveProvider):
    name = "deepseek"

    def __init__(self, model: str | None = None, api_key: str | None = None, thinking_enabled: bool = False):
        self.model = model or os.environ.get(DEEPSEEK_MODEL_VAR, DEFAULT_MODEL)
        self._api_key = api_key if api_key is not None else os.environ.get(DEEPSEEK_API_KEY_VAR, "")
        self.thinking_enabled = thinking_enabled

    def complete(self, prompt: str, *, max_tokens: int, timeout_seconds: float) -> CognitiveResponse:
        if not self._api_key:
            raise ProviderAuthError(f"{DEEPSEEK_API_KEY_VAR} not set")

        request_body: dict = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if not self.thinking_enabled:
            request_body["thinking"] = {"type": "disabled"}

        body = json.dumps(request_body).encode()
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
        except TimeoutError:
            raise ProviderTimeoutError(f"DeepSeek request timed out after {timeout_seconds}s") from None
        except urllib.error.URLError as exc:
            raise ProviderTimeoutError(f"DeepSeek request failed: {exc.reason}") from None
        duration = time.monotonic() - start

        try:
            resp = json.loads(raw_bytes)
        except ValueError as exc:
            raise ProviderResponseError(f"non-JSON response from DeepSeek: {exc}") from None

        try:
            choice = resp["choices"][0]
            message = choice["message"]
        except (KeyError, IndexError, TypeError) as exc:
            raise ProviderResponseError(f"unexpected DeepSeek response shape: {exc}") from None

        # Full-fidelity handling, not just `content`: `reasoning_content`
        # (thinking mode) and `finish_reason` are read unconditionally --
        # see module docstring for why an empty `content` alone must
        # never be read as "empty answer" without checking these too.
        visible_text = message.get("content") or ""
        reasoning_text = message.get("reasoning_content") or None
        finish_reason = choice.get("finish_reason")

        usage_raw = resp.get("usage", {}) or {}
        prompt_tokens = int(usage_raw.get("prompt_tokens", 0))
        completion_tokens = int(usage_raw.get("completion_tokens", 0))
        total_tokens = int(usage_raw.get("total_tokens", prompt_tokens + completion_tokens))
        reasoning_tokens = int((usage_raw.get("completion_tokens_details") or {}).get("reasoning_tokens", 0))

        pricing = _PRICE_PER_MILLION_TOKENS.get(self.model, _PRICE_PER_MILLION_TOKENS[DEFAULT_MODEL])
        cost_usd = (prompt_tokens * pricing["input"] + completion_tokens * pricing["output"]) / 1_000_000

        usage = ProviderUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            reasoning_tokens=reasoning_tokens,
            total_tokens=total_tokens,
            cost_usd=cost_usd,
            duration_seconds=duration,
        )
        # `raw` deliberately excludes anything from the request (no
        # prompt, no headers, no key) -- only DeepSeek's own response
        # object, for evidence/audit purposes at the caller's discretion.
        return CognitiveResponse(
            visible_text=visible_text,
            reasoning_text=reasoning_text,
            finish_reason=finish_reason,
            usage=usage,
            provider=self.name,
            model=self.model,
            raw=resp,
        )
