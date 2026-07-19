"""
The same 6 required test cases, against stdlib_baseline.py -- for direct
comparison with test_agent_contracts_experiment.py. See docs/research/
AGENT_CONTRACTS_EXPERIMENT_01.md step 5.
"""

from __future__ import annotations

import pytest
from stdlib_baseline import (
    build_b001_budget,
    evaluate_success,
    is_over_budget,
    is_past_deadline,
    is_tool_permitted,
    record_usage,
)


def test_valid_contract_is_accepted():
    budget = build_b001_budget()
    assert budget.tokens_limit == 20_000
    assert budget.deadline is not None


def test_budget_overrun_is_detected():
    budget = build_b001_budget()
    record_usage(budget, tokens=5_000)
    assert is_over_budget(budget) == []

    record_usage(budget, tokens=20_000)  # total now 25_000
    assert is_over_budget(budget) == ["tokens"]


def test_disallowed_resource_is_rejected():
    budget = build_b001_budget()
    assert is_tool_permitted(budget, "Read") is True
    assert is_tool_permitted(budget, "Grep") is True
    assert is_tool_permitted(budget, "Write") is False
    assert is_tool_permitted(budget, "Bash") is False


def test_deadline_overrun_is_detected():
    budget = build_b001_budget(deadline_hours=-1)
    assert is_past_deadline(budget) is True


def test_deadline_not_yet_overrun_when_future():
    budget = build_b001_budget(deadline_hours=24)
    assert is_past_deadline(budget) is False


def test_success_criterion_is_deterministically_checkable():
    budget = build_b001_budget()
    assert evaluate_success(budget, {"review_text": ""}) is False
    assert evaluate_success(budget, {"review_text": "Looks good."}) is True


def test_library_error_on_invalid_input_is_caught_cleanly():
    from datetime import datetime, timezone

    from stdlib_baseline import BountyBudget

    with pytest.raises(ValueError, match="non-negative"):
        BountyBudget(
            tokens_limit=-1,
            cost_usd_limit=0.5,
            api_calls_limit=10,
            deadline=datetime.now(timezone.utc),
        )
