"""
Agent Village — Worker (Proof 1, v2: bounded Agent Loop)
==========================================================
Runs a small, tightly-bounded agent loop -- GENERATE -> INTERPRET ->
EVALUATE -> optional REPAIR (hard cap) -> FINISHED -- for ONE
VillageContract's work order, inside its budget, and produces a
WorkResult. Never fulfills the contract, never completes a bounty --
enforced and tested: tests/test_worker_no_write_authority.py proves this
module's own source (via AST inspection, not a substring grep, which its
own explanatory docstrings would false-positive) never mentions `.fulfill(`
or `bounty_complete(` and never imports `village.heartbeat`.

v2 vs. the PR #13 one-shot version: see docs/research/
AGENT_LOOP_WORKER_02.md for the full rationale, including which
kimeisele/steward concepts were used as a design reference (loop phase
structure, provider-response normalization, outcome-driven evaluation,
cumulative cognitive budgeting) and which were deliberately NOT ported
(tool system, provider failover, full autonomy, Sankhya naming).

Hard limits, unchanged from PR #13 and NOT weakened by this rewrite:
exactly one Contract per execution, no shell execution of any model
output, no repository write, no autonomous follow-up work, no judgment
of analysis *quality* (only *shape*). SPEC.md §A.5/§A.6.

New hard limit, this slice: MAX_LLM_CALLS_PER_EXECUTION caps the total
number of provider calls (generate + repair-regenerates + the single
optional interpretation-only call) at a fixed, named constant -- a
repair loop with an unbounded ceiling would defeat the entire point of
"controlled resource use," budget headroom or not.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import datetime, timezone

from village.cognitive_provider import (
    CognitiveProvider,
    CognitiveResponse,
    ProviderAuthError,
    ProviderError,
    ProviderHTTPError,
    ProviderRateLimitError,
    ProviderResponseError,
    ProviderTimeoutError,
)
from village.contracts import ContractState, VillageContract
from village.interpreter import (
    RESULT_BEGIN,
    RESULT_END,
    build_interpretation_prompt,
    extract_marked_block,
    tolerant_parse,
)
from village.work_result import WorkResult, WorkResultStatus

# Bounds enforced by construction, not by hoping the provider behaves.
MAX_PROMPT_CHARS = 20_000
DEFAULT_CALL_MAX_TOKENS = 4_096  # realistic for a non-thinking structural-analysis answer, not knapped at the wall
DEFAULT_TIMEOUT_SECONDS = 30.0

# The single hard cap on total provider calls per execution. Exactly:
# 1 GENERATE + up to 2 REPAIR-regenerates + up to 1 interpretation-only
# call (village/interpreter.py stage c) = 4. Named here, not buried in
# loop logic, and reported in every PR/BEFUND entry for this slice.
MAX_REPAIR_ATTEMPTS = 2
MAX_LLM_CALLS_PER_EXECUTION = 1 + MAX_REPAIR_ATTEMPTS + 1  # == 4


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
    truncated to MAX_PROMPT_CHARS. Instructs the model to wrap its final
    answer in explicit markers -- this is what makes stage (a) of the
    interpretation layer (deterministic extraction) usually sufficient,
    without requiring a second LLM call."""
    content = file_content[:MAX_PROMPT_CHARS]
    return (
        f"{order.instruction}\n\n"
        "Think as needed, then give your final answer. When ready, "
        f"output your final answer between the exact markers "
        f"{RESULT_BEGIN} and {RESULT_END}, containing ONLY a JSON object "
        'of this exact shape inside the markers: {"gaps": '
        '[{"description": "string", "file": "string", "line": '
        "integer-or-null}]}. Nothing outside the markers is read.\n\n"
        f"--- {order.target_file} ---\n{content}"
    )


def build_repair_prompt(order: WorkOrder, file_content: str, reason: str) -> str:
    """Names the concrete failure -- never a blind repeat of the
    original prompt. `reason` comes from the EVALUATE phase and is one
    of a small, specific set (empty response / truncated output / no
    usable result block / interpretation failed)."""
    base = build_prompt(order, file_content)
    return (
        f"{base}\n\n"
        "--- RETRY NOTICE ---\n"
        f"Your previous answer could not be used. Concrete reason: "
        f"{reason}. Please answer again, and make sure to: (1) actually "
        f"include the {RESULT_BEGIN}/{RESULT_END} markers, (2) keep your "
        "reasoning concise so the final answer fits within the token "
        "budget, (3) ensure the JSON inside the markers is syntactically "
        "valid and matches the required shape exactly."
    )


def _empty_usage() -> dict:
    return {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "reasoning_tokens": 0,
        "total_tokens": 0,
        "cost_usd": 0.0,
        "duration_seconds": 0.0,
    }


def _accumulate(total: dict, response: CognitiveResponse) -> None:
    total["prompt_tokens"] += response.usage.prompt_tokens
    total["completion_tokens"] += response.usage.completion_tokens
    total["reasoning_tokens"] += response.usage.reasoning_tokens
    total["total_tokens"] += response.usage.total_tokens
    total["cost_usd"] += response.usage.cost_usd
    total["duration_seconds"] += response.usage.duration_seconds


def _evaluate_failure_reason(response: CognitiveResponse, interpret_error: str | None) -> str:
    """Outcome-driven decision, not a hardcoded round counter: inspects
    the actual signal (finish_reason, presence of text, the
    interpretation stage's own error) to name a SPECIFIC reason, fed
    verbatim into the next repair prompt."""
    candidate_text = response.visible_text or response.reasoning_text or ""
    if response.finish_reason == "length":
        return "truncated_output (finish_reason=length, ran out of tokens before finishing)"
    if not candidate_text.strip():
        return "empty_response (no visible or reasoning text returned)"
    return f"no_usable_result_block_or_interpretation_failed ({interpret_error})"


def run_work_order(
    contract: VillageContract,
    order: WorkOrder,
    file_content: str,
    provider: CognitiveProvider,
    execution_id: str | None = None,
) -> WorkResult:
    """Execute the bounded agent loop for `order` against `contract`'s
    budget and return a WorkResult.

    Mutates `contract` ONLY via `record_usage()` (budget bookkeeping,
    itself a data operation with no external effect), called after EVERY
    provider call so cumulative spend across the whole execution is
    always reflected, not just the final one. Never calls
    `contract.fulfill()`/`contract.violate()`, never touches any bounty,
    never writes to the repository, never executes anything a provider
    returns."""
    execution_id = execution_id or str(uuid.uuid4())
    work_result_id = f"workresult:{contract.contract_id}:{execution_id}"
    started_at = datetime.now(timezone.utc)
    model_hint = getattr(provider, "model", "?")

    phase_log: list[dict] = []
    cumulative_usage = _empty_usage()
    calls_made = 0
    interpretation_call_used = False

    def finish(status: WorkResultStatus, error: str | None = None, output: dict | None = None) -> WorkResult:
        return WorkResult(
            work_result_id=work_result_id,
            contract_id=contract.contract_id,
            execution_id=execution_id,
            provider=provider.name,
            model=model_hint,
            status=status,
            output=output,
            evidence={"phase_log": phase_log, "target_file": order.target_file, "instruction": order.instruction},
            usage=dict(cumulative_usage),
            started_at=started_at,
            finished_at=datetime.now(timezone.utc),
            error=error,
        )

    def call_provider(prompt: str) -> CognitiveResponse | WorkResult:
        """Returns a CognitiveResponse on success, or a terminal
        WorkResult if the call itself must end the execution (provider
        error or budget exceeded)."""
        nonlocal calls_made
        try:
            response = provider.complete(
                prompt, max_tokens=DEFAULT_CALL_MAX_TOKENS, timeout_seconds=DEFAULT_TIMEOUT_SECONDS
            )
        except ProviderAuthError as exc:
            return finish(WorkResultStatus.PROVIDER_ERROR, f"auth_error: {exc}")
        except ProviderTimeoutError as exc:
            return finish(WorkResultStatus.PROVIDER_ERROR, f"timeout: {exc}")
        except ProviderRateLimitError as exc:
            return finish(WorkResultStatus.PROVIDER_ERROR, f"rate_limited: {exc}")
        except ProviderHTTPError as exc:
            return finish(WorkResultStatus.PROVIDER_ERROR, f"http_error: {exc}")
        except ProviderResponseError as exc:
            return finish(WorkResultStatus.PROVIDER_ERROR, f"bad_response: {exc}")
        except ProviderError as exc:
            return finish(WorkResultStatus.PROVIDER_ERROR, f"provider_error: {exc}")

        calls_made += 1
        _accumulate(cumulative_usage, response)
        contract.record_usage(
            tokens=response.usage.total_tokens,
            cost_usd=response.usage.cost_usd,
            time_seconds=response.usage.duration_seconds,
        )
        exceeded = contract.check_budget()
        if exceeded:
            return finish(WorkResultStatus.BUDGET_EXCEEDED, f"budget exceeded in dimensions: {exceeded}")
        return response

    if contract.state != ContractState.ACTIVE:
        return finish(WorkResultStatus.FAILED, f"contract not ACTIVE (state={contract.state.value})")

    prompt = build_prompt(order, file_content)
    repair_attempts = 0

    while True:
        if calls_made >= MAX_LLM_CALLS_PER_EXECUTION:
            return finish(
                WorkResultStatus.INVALID_OUTPUT,
                f"reached MAX_LLM_CALLS_PER_EXECUTION ({MAX_LLM_CALLS_PER_EXECUTION}) without a valid result",
            )

        # ── GENERATE ──────────────────────────────────────────────────
        result = call_provider(prompt)
        if isinstance(result, WorkResult):
            return result
        response = result
        phase_log.append(
            {
                "phase": "generate",
                "attempt": repair_attempts,
                "finish_reason": response.finish_reason,
                "has_visible_text": bool(response.visible_text.strip()),
                "has_reasoning_text": bool(response.reasoning_text),
            }
        )

        # ── INTERPRET ────────────────────────────────────────────────
        candidate_text = response.visible_text or response.reasoning_text or ""
        parsed, interpret_error = extract_marked_block(candidate_text)
        if parsed is None:
            parsed, interpret_error = tolerant_parse(candidate_text)

        # The interpretation call is a REFORMATTING call -- it needs
        # actual substantive content to reformat. An empty response or a
        # truncated one (finish_reason == "length") has nothing worth
        # spending that single precious call on; those go straight to
        # REPAIR (regenerate) instead. Reserved for the one case where
        # it earns its keep: real content, wrong/missing structure.
        candidate_is_substantive = bool(candidate_text.strip()) and response.finish_reason != "length"
        if (
            parsed is None
            and candidate_is_substantive
            and not interpretation_call_used
            and calls_made < MAX_LLM_CALLS_PER_EXECUTION
        ):
            interpretation_call_used = True
            interp_result = call_provider(build_interpretation_prompt(candidate_text))
            if isinstance(interp_result, WorkResult):
                return interp_result
            interp_response = interp_result
            phase_log.append(
                {
                    "phase": "interpret_call",
                    "finish_reason": interp_response.finish_reason,
                }
            )
            parsed, interpret_error = tolerant_parse(interp_response.visible_text or "")

        # ── EVALUATE ─────────────────────────────────────────────────
        if parsed is not None:
            phase_log.append({"phase": "evaluate", "result": "accepted"})
            return finish(WorkResultStatus.SUCCEEDED, output=parsed)

        reason = _evaluate_failure_reason(response, interpret_error)
        phase_log.append({"phase": "evaluate", "result": "rejected", "reason": reason})

        # ── REPAIR (bounded) or FINISHED ────────────────────────────
        repair_attempts += 1
        if repair_attempts > MAX_REPAIR_ATTEMPTS:
            phase_log.append({"phase": "finished", "result": "repair_attempts_exhausted"})
            return finish(
                WorkResultStatus.INVALID_OUTPUT,
                f"exhausted {MAX_REPAIR_ATTEMPTS} repair attempts; last reason: {reason}",
            )

        phase_log.append({"phase": "repair", "attempt": repair_attempts, "reason": reason})
        prompt = build_repair_prompt(order, file_content, reason)
