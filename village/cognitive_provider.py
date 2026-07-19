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
    time_seconds). No provider-specific fields."""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    cost_usd: float = 0.0
    duration_seconds: float = 0.0


@dataclass
class ProviderResponse:
    content: str
    provider: str
    model: str
    usage: ProviderUsage
    raw: dict[str, Any] = field(default_factory=dict)


class CognitiveProvider(ABC):
    """Neutral cognition provider interface."""

    name: str

    @abstractmethod
    def complete(self, prompt: str, *, max_tokens: int, timeout_seconds: float) -> ProviderResponse:
        """Run exactly one completion call. Must raise a ProviderError
        subclass on any failure (auth/timeout/rate-limit/HTTP/malformed
        response) -- never return a fabricated success."""
        raise NotImplementedError
