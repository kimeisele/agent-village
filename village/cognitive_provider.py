"""
Agent Village — Cognitive Provider Interface
=============================================
Neutral interface any LLM/cognition provider implements. No
provider-specific detail lives here (no DeepSeek model names, no
DeepSeek pricing) -- see village/deepseek_provider.py for the concrete
adapter. SPEC.md §A.6: the cognitive-kernel port belongs to Village, not
to any one provider; this module IS that port.

SPEC.md §A.5: cognition never gets write authority. Nothing in this
module (or any implementation of it) writes to a Contract, a bounty, or
the repository -- see village/worker.py for where that boundary is
enforced and tested on the caller side.

v2 (docs/research/AGENT_LOOP_WORKER_02.md): `CognitiveResponse` replaces
the v1 `ProviderResponse` -- no JSON-shape assumption at this layer
(that belongs to the interpretation layer, village/interpreter.py). A
provider hands back whatever the model actually said: visible text,
separate reasoning/thinking text if the model produced one, the
provider's own `finish_reason`, and full usage accounting including a
reasoning-token count where available.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


class ProviderError(Exception):
    """Base class for all cognitive-provider failures. An implementation
    must never raise anything outside this hierarchy, and must never
    include a credential value in any exception message."""


class ProviderAuthError(ProviderError):
    """Missing or rejected credential."""


class ProviderTimeoutError(ProviderError):
    pass


class ProviderRateLimitError(ProviderError):
    pass


class ProviderHTTPError(ProviderError):
    def __init__(self, status: int, message: str):
        super().__init__(f"HTTP {status}: {message}")
        self.status = status


class ProviderResponseError(ProviderError):
    """Response received but not parseable into the expected shape."""


@dataclass
class ProviderUsage:
    """Neutral usage accounting -- maps directly onto
    village/contracts.py::Budget's dimensions (tokens, cost_usd,
    time_seconds). `reasoning_tokens` is a sub-count of
    `completion_tokens` (not additional to it), reported separately
    where the provider exposes it, so a caller can tell "the model spent
    its whole budget thinking and never got to an answer" apart from "the
    model just wrote a long answer"."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    reasoning_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0


@dataclass
class CognitiveResponse:
    """What a provider actually said, fully -- no JSON-shape assumption,
    no premature "empty response" judgment. `visible_text` is the
    model's final-answer text (may be empty even on a technically
    successful call -- e.g. a thinking model that used its whole budget
    on `reasoning_text` and never reached a final answer; `finish_reason
    == "length"` is the signal for that case, not an empty string on its
    own)."""

    visible_text: str
    reasoning_text: str | None
    finish_reason: str | None
    usage: ProviderUsage
    provider: str
    model: str
    raw: dict[str, Any] = field(default_factory=dict)


class CognitiveProvider(ABC):
    """Neutral cognition provider interface."""

    name: str

    @abstractmethod
    def complete(self, prompt: str, *, max_tokens: int, timeout_seconds: float) -> CognitiveResponse:
        """Run exactly one completion call. Must raise a ProviderError
        subclass on any failure (auth/timeout/rate-limit/HTTP/malformed
        response) -- never return a fabricated success."""
        raise NotImplementedError
