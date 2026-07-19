"""
Tests for village/worker.py::run_work_order() -- the bounded Agent Loop
(v2, docs/research/AGENT_LOOP_WORKER_02.md). No real API calls -- a
FakeProvider test double implements village.cognitive_provider's
interface, injecting a scripted sequence of CognitiveResponses (or
errors) per call, so multi-call loop behavior (repair, interpretation
call, call-cap) can be exercised deterministically.
"""

from __future__ import annotations

from village.cognitive_provider import (
    CognitiveProvider,
    CognitiveResponse,
    ProviderError,
    ProviderHTTPError,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUsage,
)
from village.contracts import Budget, VillageContract
from village.interpreter import RESULT_BEGIN, RESULT_END
from village.work_result import WorkResultStatus
from village.worker import (
    MAX_LLM_CALLS_PER_EXECUTION,
    MAX_REPAIR_ATTEMPTS,
    MAX_PROMPT_CHARS,
    WorkOrder,
    build_prompt,
    run_work_order,
)


class FakeProvider(CognitiveProvider):
    """Scripted sequence: each element is either a CognitiveResponse to
    return, or an Exception instance to raise, for the Nth call. If the
    script runs out, raises AssertionError -- a test asserting on a call
    count that doesn't match its own script is a bug in the test, not
    something to paper over with a default."""

    name = "fake"

    def __init__(self, model="fake-model-01", script: list | None = None):
        self.model = model
        self._script = list(script or [])
        self.calls = 0
        self.prompts_seen: list[str] = []

    def complete(self, prompt: str, *, max_tokens: int, timeout_seconds: float) -> CognitiveResponse:
        self.prompts_seen.append(prompt)
        if self.calls >= len(self._script):
            raise AssertionError(f"FakeProvider script exhausted at call {self.calls + 1}")
        item = self._script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _active_contract(**budget_kwargs) -> VillageContract:
    c = VillageContract(contract_id="contract:test:1", title="t", budget=Budget(**budget_kwargs))
    c.activate()
    return c


def _order() -> WorkOrder:
    return WorkOrder(contract_id="contract:test:1", target_file="village/heartbeat.py", instruction="Find gaps.")


def _usage(prompt_tokens=100, completion_tokens=50, reasoning_tokens=0, cost_usd=0.001, duration=0.5) -> ProviderUsage:
    return ProviderUsage(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens, reasoning_tokens=reasoning_tokens,
        total_tokens=prompt_tokens + completion_tokens, cost_usd=cost_usd, duration_seconds=duration,
    )


def _response(visible_text="", reasoning_text=None, finish_reason="stop", usage=None) -> CognitiveResponse:
    return CognitiveResponse(
        visible_text=visible_text, reasoning_text=reasoning_text, finish_reason=finish_reason,
        usage=usage or _usage(), provider="fake", model="fake-model-01",
    )


VALID_MARKED = f'{RESULT_BEGIN}\n{{"gaps": [{{"description": "no tests for X", "file": "village/heartbeat.py", "line": 42}}]}}\n{RESULT_END}'


# ── Free model answer shapes: content-only, reasoning-only, mixed ────────


def test_content_only_success():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[_response(visible_text=VALID_MARKED)])

    result = run_work_order(contract, _order(), "file content", provider, execution_id="exec-1")

    assert result.status == WorkResultStatus.SUCCEEDED
    assert result.output == {"gaps": [{"description": "no tests for X", "file": "village/heartbeat.py", "line": 42}]}
    assert provider.calls == 1


def test_reasoning_only_with_empty_content_still_succeeds_if_result_marker_is_in_reasoning():
    """The real PR #13 finding: thinking mode can put everything in
    reasoning_content with content empty. If the model still followed
    the marker instructions inside its reasoning text, this must NOT be
    prematurely treated as an empty/failed answer."""
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[_response(visible_text="", reasoning_text=VALID_MARKED, finish_reason="stop")])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.SUCCEEDED


def test_mixed_content_and_reasoning_uses_visible_text_first():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[_response(visible_text=VALID_MARKED, reasoning_text="unrelated internal notes")])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.SUCCEEDED


# ── Truncated response (finish_reason == "length") triggers repair ───────


def test_truncated_response_triggers_repair_then_succeeds():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[
        _response(visible_text="", reasoning_text="ran out of room", finish_reason="length"),
        _response(visible_text=VALID_MARKED, finish_reason="stop"),
    ])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.SUCCEEDED
    assert provider.calls == 2
    repair_prompts = [p for p in provider.prompts_seen if "RETRY NOTICE" in p]
    assert len(repair_prompts) == 1
    assert "truncated_output" in repair_prompts[0]


def test_empty_response_triggers_repair_with_specific_reason():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[
        _response(visible_text="", reasoning_text=None, finish_reason="stop"),
        _response(visible_text=VALID_MARKED, finish_reason="stop"),
    ])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.SUCCEEDED
    repair_prompts = [p for p in provider.prompts_seen if "RETRY NOTICE" in p]
    assert "empty_response" in repair_prompts[0]


# ── Interpretation failure / interpretation-call repair path ─────────────


def test_no_usable_result_block_falls_back_to_interpretation_call():
    """visible_text has real content but no markers and no bare JSON --
    stage (c), the interpretation-only call, should be used before
    giving up."""
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[
        _response(visible_text="I found that heartbeat.py lacks tests for X, in file village/heartbeat.py.", finish_reason="stop"),
        _response(visible_text='{"gaps": [{"description": "lacks tests for X", "file": "village/heartbeat.py"}]}', finish_reason="stop"),
    ])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.SUCCEEDED
    assert provider.calls == 2
    interp_prompts = [p for p in provider.prompts_seen if "strict reformatting tool" in p]
    assert len(interp_prompts) == 1


def test_interpretation_call_used_at_most_once_per_execution():
    """If the interpretation call ALSO fails to produce usable output,
    the loop must repair-regenerate rather than spend a second
    interpretation call."""
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[
        _response(visible_text="unstructured answer one", finish_reason="stop"),
        _response(visible_text="still not JSON", finish_reason="stop"),  # interpretation call, also fails
        _response(visible_text=VALID_MARKED, finish_reason="stop"),  # repair-regenerate succeeds
    ])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.SUCCEEDED
    assert provider.calls == 3
    interp_prompts = [p for p in provider.prompts_seen if "strict reformatting tool" in p]
    assert len(interp_prompts) == 1  # never a second interpretation call


# ── Repair ceiling is enforced, execution ends controlled not endlessly ──


def test_repair_ceiling_stops_the_execution_deterministically():
    contract = _active_contract(tokens=1_000_000, cost_usd=1000.0)  # budget is NOT the limiting factor here
    # Script: generate (fails), interpretation call (fails), repair#1 generate (fails),
    # repair#2 generate (fails) -- exactly MAX_LLM_CALLS_PER_EXECUTION calls, all useless.
    bad = _response(visible_text="never valid, no markers, no json", finish_reason="stop")
    provider = FakeProvider(script=[bad, bad, bad, bad])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.INVALID_OUTPUT
    assert provider.calls == MAX_LLM_CALLS_PER_EXECUTION
    assert "repair attempts" in result.error or "MAX_LLM_CALLS_PER_EXECUTION" in result.error
    # Controlled stop, not an exception, not a hang.


def test_max_llm_calls_and_max_repair_attempts_are_named_constants_with_expected_values():
    assert MAX_REPAIR_ATTEMPTS == 2
    assert MAX_LLM_CALLS_PER_EXECUTION == 4  # 1 generate + 2 repair + 1 interpretation call


# ── Multiple calls cumulate against ONE contract budget ───────────────────


def test_multiple_calls_cumulate_against_the_same_budget():
    contract = _active_contract(tokens=1_000, cost_usd=1.0)  # 1000 tokens total across the WHOLE execution
    provider = FakeProvider(script=[
        _response(visible_text="", finish_reason="length", usage=_usage(prompt_tokens=200, completion_tokens=200)),  # 400 tokens
        _response(visible_text=VALID_MARKED, finish_reason="stop", usage=_usage(prompt_tokens=200, completion_tokens=200)),  # +400 = 800
    ])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.SUCCEEDED
    assert result.usage["total_tokens"] == 800  # cumulative across both calls
    assert contract.budget.used_tokens == 800  # contract itself reflects cumulative usage


def test_budget_exceeded_mid_loop_stops_immediately_no_further_calls():
    contract = _active_contract(tokens=500)  # first call alone (400 tokens) is fine, but leaves only 100 remaining
    provider = FakeProvider(script=[
        _response(visible_text="", finish_reason="length", usage=_usage(prompt_tokens=200, completion_tokens=200)),  # 400, ok
        _response(visible_text=VALID_MARKED, usage=_usage(prompt_tokens=200, completion_tokens=200)),  # would be +400 = 800 > 500
    ])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.BUDGET_EXCEEDED
    assert provider.calls == 2  # the second (over-budget) call still happened -- usage is checked AFTER the call, not predicted before
    assert "tokens" in result.error


# ── Provider error paths still stop the whole execution cleanly ──────────


def test_provider_error_on_first_call_stops_immediately():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[ProviderHTTPError(500, "server error")])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.PROVIDER_ERROR
    assert provider.calls == 1  # complete() was invoked once; it raised, worker's own calls_made counter (checked via no repair happening) does not advance past it


def test_provider_error_during_repair_stops_immediately():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[
        _response(visible_text="", finish_reason="length"),
        ProviderTimeoutError("timed out"),
    ])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.PROVIDER_ERROR
    assert "timeout" in result.error


def test_generic_provider_error_is_caught_not_propagated():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[ProviderError("something unforeseen")])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.PROVIDER_ERROR


def test_provider_response_error_is_caught():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[ProviderResponseError("bad shape")])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.PROVIDER_ERROR
    assert "bad_response" in result.error


# ── Missing secret (via a provider that raises ProviderAuthError) ────────


def test_missing_secret_never_produces_a_fake_success():
    from village.cognitive_provider import ProviderAuthError

    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(script=[ProviderAuthError("DEEPSEEK_API_KEY not set")])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.PROVIDER_ERROR
    assert result.status != WorkResultStatus.SUCCEEDED


# ── Contract state guard ──────────────────────────────────────────────────


def test_non_active_contract_is_rejected_without_calling_the_provider():
    contract = VillageContract(contract_id="contract:test:2", title="t")  # still DRAFTED
    provider = FakeProvider(script=[])

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.FAILED
    assert "not ACTIVE" in result.error
    assert provider.calls == 0


# ── Reproducibility ────────────────────────────────────────────────────────


def test_identical_fixture_input_yields_the_same_validation_decision():
    results = []
    for _ in range(3):
        contract = _active_contract(tokens=100_000, cost_usd=1.0)
        provider = FakeProvider(script=[_response(visible_text=VALID_MARKED)])
        results.append(run_work_order(contract, _order(), "file content", provider))

    statuses = {r.status for r in results}
    outputs = {str(r.output) for r in results}
    assert statuses == {WorkResultStatus.SUCCEEDED}
    assert len(outputs) == 1


# ── build_prompt ───────────────────────────────────────────────────────────


def test_build_prompt_truncates_to_max_prompt_chars():
    order = _order()
    huge_content = "x" * 50_000
    prompt = build_prompt(order, huge_content)
    assert len(prompt) < MAX_PROMPT_CHARS + 1_000


def test_build_prompt_includes_result_markers_instruction():
    prompt = build_prompt(_order(), "content")
    assert RESULT_BEGIN in prompt
    assert RESULT_END in prompt
