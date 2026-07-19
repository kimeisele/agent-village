"""
Agent Village — Worker (Proof 1)
==================================
Runs exactly one bounded execution of a cognitive provider against ONE
VillageContract's work order, inside its budget, and produces a
WorkResult. Never fulfills the contract, never completes a bounty --
enforced and tested: tests/test_worker_no_write_authority.py proves this
module's own source never mentions `fulfill` or `bounty_complete` by
name, so the boundary can't silently regress.

Scope, hard, per docs/research/INTERNAL_WORKER_PROOF_01.md: exactly one
contract, exactly one provider call (no retry -- see module notes below),
no shell execution of provider output, no repository write, no
autonomous follow-up work, no judgment of the analysis' *quality* (only
its *shape*). SPEC.md §A.5/§A.6.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from village.cognitive_provider import (
    CognitiveProvider,
    ProviderAuthError,
    ProviderError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from village.contracts import ContractState, VillageContract
from village.work_result import WorkResult, WorkResultStatus

# Bounds enforced by construction, not by hoping the provider behaves.
MAX_PROMPT_CHARS = 20_000
MAX_OUTPUT_TOKENS = 2_000
DEFAULT_TIMEOUT_SECONDS = 30.0


@dataclass
class WorkOrder:
    """A single, bounded analysis task -- Proof 1 shape only, deliberately
    minimal: a fixed local file path, a fixed instruction string. No
    internet browsing, no external repo access, no shell/tool access of
    any kind -- the model only ever sees the text handed to it here and
    returns text; nothing it returns is ever executed."""

    contract_id: str
    target_file: str
    instruction: str


def build_prompt(order: WorkOrder, file_content: str) -> str:
    """Bounded, deterministic prompt construction. `file_content` is
    truncated to MAX_PROMPT_CHARS -- input size is bounded here, not left
    to the caller."""
    content = file_content[:MAX_PROMPT_CHARS]
    return (
        f"{order.instruction}\n\n"
        "Respond with ONLY a JSON object of this exact shape, nothing "
        "else, no markdown code fences, no commentary outside the JSON:\n"
        '{"gaps": [{"description": "string", "file": "string", "line": integer-or-null}]}\n\n'
        f"--- {order.target_file} ---\n{content}"
    )


def _validate_output(raw_content: str) -> tuple[dict | None, str | None]:
    """Deterministic STRUCTURAL validation only -- never a judgment of
    whether the analysis is any good, only whether it's the shape asked
    for. Returns (parsed_output, error_message); exactly one is None."""
    text = raw_content.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except ValueError as exc:
        return None, f"output is not valid JSON: {exc}"

    if not isinstance(parsed, dict) or "gaps" not in parsed:
        return None, "output JSON missing required 'gaps' key"
    if not isinstance(parsed["gaps"], list):
        return None, "'gaps' must be a list"
    for i, gap in enumerate(parsed["gaps"]):
        if not isinstance(gap, dict) or "description" not in gap or "file" not in gap:
            return None, f"gaps[{i}] missing required 'description'/'file' fields"
    return parsed, None


def _result(
    *, work_result_id: str, contract_id: str, execution_id: str, provider: str, model: str,
    status: WorkResultStatus, started_at: datetime, output: dict | None = None,
    evidence: dict | None = None, usage: dict | None = None, error: str | None = None,
) -> WorkResult:
    return WorkResult(
        work_result_id=work_result_id,
        contract_id=contract_id,
        execution_id=execution_id,
        provider=provider,
        model=model,
        status=status,
        output=output,
        evidence=evidence or {},
        usage=usage or {},
        started_at=started_at,
        finished_at=datetime.now(timezone.utc),
        error=error,
    )


def run_work_order(
    contract: VillageContract,
    order: WorkOrder,
    file_content: str,
    provider: CognitiveProvider,
    execution_id: str | None = None,
) -> WorkResult:
    """Execute exactly one provider call for `order` against `contract`'s
    budget and return a WorkResult.

    Mutates `contract` ONLY via `record_usage()` (budget bookkeeping,
    itself a data operation with no external effect). Never calls
    `contract.fulfill()`/`contract.violate()`, never touches any bounty,
    never writes to the repository, never executes anything the provider
    returns. The caller (a human, or a future explicitly-separate review
    step -- not this function) decides what happens with the result.

    No retry: only one provider.complete() call is ever made here. A
    transient-error retry was considered and deliberately NOT
    implemented in this proof -- SPEC.md's explicit "max. 1 API-Aufruf"
    constraint for this proof takes precedence; see
    docs/research/INTERNAL_WORKER_PROOF_01.md for the reasoning and the
    note that this is a still-open decision for a later slice."""
    execution_id = execution_id or str(uuid.uuid4())
    work_result_id = f"workresult:{contract.contract_id}:{execution_id}"
    started_at = datetime.now(timezone.utc)
    model_hint = getattr(provider, "model", "?")

    def fail(status: WorkResultStatus, error: str, **kw) -> WorkResult:
        return _result(
            work_result_id=work_result_id, contract_id=contract.contract_id, execution_id=execution_id,
            provider=provider.name, model=model_hint, status=status, started_at=started_at, error=error, **kw,
        )

    if contract.state != ContractState.ACTIVE:
        return fail(WorkResultStatus.FAILED, f"contract not ACTIVE (state={contract.state.value})")

    prompt = build_prompt(order, file_content)
    if len(prompt) > MAX_PROMPT_CHARS + 2_000:  # instruction/template overhead margin
        return fail(WorkResultStatus.FAILED, "prompt exceeds MAX_PROMPT_CHARS bound")

    try:
        response = provider.complete(prompt, max_tokens=MAX_OUTPUT_TOKENS, timeout_seconds=DEFAULT_TIMEOUT_SECONDS)
    except ProviderAuthError as exc:
        return fail(WorkResultStatus.PROVIDER_ERROR, f"auth_error: {exc}")
    except ProviderTimeoutError as exc:
        return fail(WorkResultStatus.PROVIDER_ERROR, f"timeout: {exc}")
    except ProviderRateLimitError as exc:
        return fail(WorkResultStatus.PROVIDER_ERROR, f"rate_limited: {exc}")
    except ProviderHTTPError as exc:
        return fail(WorkResultStatus.PROVIDER_ERROR, f"http_error: {exc}")
    except ProviderResponseError as exc:
        return fail(WorkResultStatus.PROVIDER_ERROR, f"bad_response: {exc}")
    except ProviderError as exc:
        return fail(WorkResultStatus.PROVIDER_ERROR, f"provider_error: {exc}")

    usage_dict = {
        "prompt_tokens": response.usage.prompt_tokens,
        "completion_tokens": response.usage.completion_tokens,
        "total_tokens": response.usage.total_tokens,
        "cost_usd": response.usage.cost_usd,
        "duration_seconds": response.usage.duration_seconds,
    }

    # Real usage checked against the contract's actual budget BEFORE the
    # result is accepted as anything resembling success.
    contract.record_usage(
        tokens=response.usage.total_tokens,
        cost_usd=response.usage.cost_usd,
        time_seconds=response.usage.duration_seconds,
    )
    exceeded = contract.check_budget()
    if exceeded:
        return fail(WorkResultStatus.BUDGET_EXCEEDED, f"budget exceeded in dimensions: {exceeded}", usage=usage_dict)

    parsed_output, validation_error = _validate_output(response.content)
    if validation_error:
        return fail(
            WorkResultStatus.INVALID_OUTPUT, validation_error, usage=usage_dict,
            evidence={"raw_response_excerpt": response.content[:2_000]},
        )

    return _result(
        work_result_id=work_result_id, contract_id=contract.contract_id, execution_id=execution_id,
        provider=provider.name, model=response.model, status=WorkResultStatus.SUCCEEDED,
        started_at=started_at, output=parsed_output,
        evidence={"target_file": order.target_file, "instruction": order.instruction},
        usage=usage_dict,
    )
