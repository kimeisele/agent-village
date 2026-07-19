"""
Tests for village/worker.py::run_work_order(). No real API calls -- a
FakeProvider test double implements village.cognitive_provider's
interface, injecting recorded/synthetic responses.
"""

from __future__ import annotations

from village.cognitive_provider import (
    CognitiveProvider,
    ProviderError,
    ProviderHTTPError,
    ProviderResponse,
    ProviderResponseError,
    ProviderTimeoutError,
    ProviderUsage,
)
from village.contracts import Budget, VillageContract
from village.work_result import WorkResultStatus
from village.worker import WorkOrder, _validate_output, build_prompt, run_work_order


class FakeProvider(CognitiveProvider):
    name = "fake"

    def __init__(self, model="fake-model-01", response: ProviderResponse | None = None, error: Exception | None = None):
        self.model = model
        self._response = response
        self._error = error
        self.calls = 0

    def complete(self, prompt: str, *, max_tokens: int, timeout_seconds: float) -> ProviderResponse:
        self.calls += 1
        if self._error is not None:
            raise self._error
        return self._response


def _active_contract(**budget_kwargs) -> VillageContract:
    c = VillageContract(contract_id="contract:test:1", title="t", budget=Budget(**budget_kwargs))
    c.activate()
    return c


def _order() -> WorkOrder:
    return WorkOrder(contract_id="contract:test:1", target_file="village/heartbeat.py", instruction="Find gaps.")


def _usage(prompt_tokens=100, completion_tokens=50, cost_usd=0.001, duration=0.5) -> ProviderUsage:
    return ProviderUsage(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens, cost_usd=cost_usd, duration_seconds=duration,
    )


# ── Successful output ────────────────────────────────────────────────────


def test_successful_output_produces_succeeded_result():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    valid_json = '{"gaps": [{"description": "no tests for X", "file": "village/heartbeat.py", "line": 42}]}'
    provider = FakeProvider(response=ProviderResponse(
        content=valid_json, provider="fake", model="fake-model-01", usage=_usage(),
    ))

    result = run_work_order(contract, _order(), "file content here", provider, execution_id="exec-1")

    assert result.status == WorkResultStatus.SUCCEEDED
    assert result.contract_id == "contract:test:1"
    assert result.execution_id == "exec-1"
    assert result.provider == "fake"
    assert result.output == {"gaps": [{"description": "no tests for X", "file": "village/heartbeat.py", "line": 42}]}
    assert result.usage["total_tokens"] == 150
    assert result.error is None
    assert provider.calls == 1  # exactly one call


# ── Invalid model JSON ────────────────────────────────────────────────────


def test_invalid_json_output_produces_invalid_output_status():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(response=ProviderResponse(
        content="this is not json at all", provider="fake", model="fake-model-01", usage=_usage(),
    ))

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.INVALID_OUTPUT
    assert "not valid JSON" in result.error
    assert result.output is None


def test_json_missing_required_key_is_invalid_output():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(response=ProviderResponse(
        content='{"something_else": true}', provider="fake", model="fake-model-01", usage=_usage(),
    ))

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.INVALID_OUTPUT
    assert "gaps" in result.error


# ── Provider HTTP error ───────────────────────────────────────────────────


def test_provider_http_error_produces_provider_error_status():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(error=ProviderHTTPError(500, "internal server error"))

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.PROVIDER_ERROR
    assert "http_error" in result.error


def test_provider_malformed_response_produces_provider_error_status():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(error=ProviderResponseError("unexpected shape"))

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.PROVIDER_ERROR
    assert "bad_response" in result.error


# ── Timeout ───────────────────────────────────────────────────────────────


def test_provider_timeout_produces_provider_error_status():
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(error=ProviderTimeoutError("timed out after 30s"))

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.PROVIDER_ERROR
    assert "timeout" in result.error


def test_generic_provider_error_is_caught_not_propagated():
    """The catch-all ProviderError branch -- any future ProviderError
    subclass must still be handled cleanly, not crash the worker."""
    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(error=ProviderError("something unforeseen"))

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.PROVIDER_ERROR


# ── Missing secret (via a provider that raises ProviderAuthError) ────────


def test_missing_secret_never_produces_a_fake_success():
    from village.cognitive_provider import ProviderAuthError

    contract = _active_contract(tokens=100_000, cost_usd=1.0)
    provider = FakeProvider(error=ProviderAuthError("DEEPSEEK_API_KEY not set"))

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.PROVIDER_ERROR
    assert result.status != WorkResultStatus.SUCCEEDED


# ── Budget exceeded ────────────────────────────────────────────────────────


def test_budget_exceeded_rejects_even_a_structurally_valid_result():
    contract = _active_contract(cost_usd=0.0001)  # tiny budget, easily blown
    valid_json = '{"gaps": []}'
    provider = FakeProvider(response=ProviderResponse(
        content=valid_json, provider="fake", model="fake-model-01",
        usage=_usage(prompt_tokens=10_000, completion_tokens=5_000, cost_usd=0.5),
    ))

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.BUDGET_EXCEEDED
    assert "cost_usd" in result.error
    assert result.output is None  # a structurally valid result is still rejected once over budget


def test_budget_exceeded_still_records_real_usage_on_the_contract():
    """The contract's own usage counters must reflect the real API
    usage even when the result is rejected -- no silent discard of
    what was actually spent."""
    contract = _active_contract(cost_usd=0.0001)
    provider = FakeProvider(response=ProviderResponse(
        content='{"gaps": []}', provider="fake", model="fake-model-01",
        usage=_usage(cost_usd=0.5),
    ))

    run_work_order(contract, _order(), "file content", provider)

    assert contract.budget.used_cost_usd == 0.5


# ── Reproducibility ────────────────────────────────────────────────────────


def test_identical_fixture_input_yields_the_same_validation_decision():
    fixture_content = '{"gaps": [{"description": "x", "file": "y.py"}]}'
    results = []
    for _ in range(3):
        contract = _active_contract(tokens=100_000, cost_usd=1.0)
        provider = FakeProvider(response=ProviderResponse(
            content=fixture_content, provider="fake", model="fake-model-01", usage=_usage(),
        ))
        results.append(run_work_order(contract, _order(), "file content", provider))

    statuses = {r.status for r in results}
    outputs = {str(r.output) for r in results}
    assert statuses == {WorkResultStatus.SUCCEEDED}
    assert len(outputs) == 1  # identical parsed output every time


# ── Contract state guard ──────────────────────────────────────────────────


def test_non_active_contract_is_rejected_without_calling_the_provider():
    contract = VillageContract(contract_id="contract:test:2", title="t")  # still DRAFTED
    provider = FakeProvider(response=None)

    result = run_work_order(contract, _order(), "file content", provider)

    assert result.status == WorkResultStatus.FAILED
    assert "not ACTIVE" in result.error
    assert provider.calls == 0  # never even attempted


# ── build_prompt / _validate_output unit-level ────────────────────────────


def test_build_prompt_truncates_to_max_prompt_chars():
    order = _order()
    huge_content = "x" * 50_000
    prompt = build_prompt(order, huge_content)
    from village.worker import MAX_PROMPT_CHARS
    assert len(prompt) < MAX_PROMPT_CHARS + 1_000  # instruction/template overhead only


def test_validate_output_tolerates_markdown_fence():
    parsed, err = _validate_output('```json\n{"gaps": []}\n```')
    assert err is None
    assert parsed == {"gaps": []}
