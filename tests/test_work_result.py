"""
Tests for village/work_result.py -- JSON-native WorkResult schema.
"""

from __future__ import annotations

from datetime import datetime, timezone

from village.work_result import WorkResult, WorkResultStatus


def _sample() -> WorkResult:
    return WorkResult(
        work_result_id="workresult:contract:b001:1:exec-1",
        contract_id="contract:b001:1",
        execution_id="exec-1",
        provider="deepseek",
        model="deepseek-v4-flash",
        status=WorkResultStatus.SUCCEEDED,
        output={"gaps": [{"description": "x", "file": "y.py", "line": 1}]},
        evidence={"target_file": "village/heartbeat.py"},
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.001},
        started_at=datetime(2026, 7, 19, 12, 0, 0, tzinfo=timezone.utc),
        finished_at=datetime(2026, 7, 19, 12, 0, 5, tzinfo=timezone.utc),
    )


def test_json_roundtrip_is_lossless():
    original = _sample()
    restored = WorkResult.from_json(original.to_json())
    assert restored.to_dict() == original.to_dict()


def test_json_is_deterministic_across_calls():
    r = _sample()
    assert r.to_json() == r.to_json()


def test_status_enum_string_values_are_stable():
    assert WorkResultStatus.SUCCEEDED.value == "succeeded"
    assert WorkResultStatus.FAILED.value == "failed"
    assert WorkResultStatus.BUDGET_EXCEEDED.value == "budget_exceeded"
    assert WorkResultStatus.INVALID_OUTPUT.value == "invalid_output"
    assert WorkResultStatus.PROVIDER_ERROR.value == "provider_error"


def test_naive_started_at_is_normalized_to_utc_aware():
    r = WorkResult(
        work_result_id="w", contract_id="c", execution_id="e", provider="p", model="m",
        status=WorkResultStatus.FAILED, started_at=datetime(2026, 7, 19, 12, 0, 0),  # naive
    )
    assert r.started_at.tzinfo is not None


def test_status_accepts_plain_string_on_construction():
    """Convenience: constructing with the raw string value (as from_dict
    would produce before enum coercion) must not error."""
    r = WorkResult(
        work_result_id="w", contract_id="c", execution_id="e", provider="p", model="m",
        status="succeeded",
    )
    assert r.status == WorkResultStatus.SUCCEEDED
