"""
Six required test cases (docs/research/AGENT_CONTRACTS_EXPERIMENT_01.md
step 4) against the real, pinned `ai-agent-contracts==0.3.2` library,
modeling the real b001 bounty. No LLM calls, no network access, fully
offline/deterministic. Not part of tests/ (Agent Village's real suite) --
lives entirely under experiments/agent_contracts_01/, own venv, own
requirements.txt.
"""

from __future__ import annotations

import pytest
from agent_contracts import ContractState
from contract_experiment import (
    build_b001_contract,
    evaluate_success,
    is_tool_permitted,
    make_enforcer,
)


def test_valid_contract_is_accepted():
    """A correctly-specified Contract for b001 builds without error and
    starts in the expected initial state."""
    contract = build_b001_contract()
    assert contract.id == "b001"
    assert contract.state == ContractState.DRAFTED
    assert contract.resources.tokens == 20_000
    assert contract.temporal.deadline is not None


def test_budget_overrun_is_detected():
    """Recording usage beyond the contract's resource budget must be
    caught by check_constraints() and flip the contract to VIOLATED.

    Real finding while writing this test: `Contract.violate()` raises
    `ValueError` unless the contract is already ACTIVE -- so
    `enforcer.start()` (which calls `contract.activate()`) must run
    before the first `check_constraints()` call, or a violation on the
    very first check crashes instead of being recorded. Not documented
    in the README's quick-start example, found only by running it."""
    contract = build_b001_contract()
    enforcer, _ = make_enforcer(contract, strict_mode=True)
    enforcer.start()

    # Well within budget: no violation yet.
    enforcer.monitor.usage.add_tokens(5_000)
    is_violated, violations = enforcer.check_constraints()
    assert is_violated is False
    assert violations == []

    # Blow through the 20_000-token budget.
    enforcer.monitor.usage.add_tokens(20_000)  # total now 25_000
    is_violated, violations = enforcer.check_constraints()
    assert is_violated is True
    assert any(v.resource == "tokens" for v in violations)
    assert contract.state == ContractState.VIOLATED


def test_disallowed_resource_is_rejected():
    """b001 is a review-only bounty: capabilities.tools = ["Read", "Grep"].
    A tool outside that whitelist (e.g. "Write", "Bash") must be rejected
    -- this is the "unerlaubte Ressource" case; the library's data model
    supports declaring the whitelist, enforcement is a one-line check
    against it (see contract_experiment.is_tool_permitted docstring for
    why this can't come from the library's own runtime without an LLM
    loop)."""
    contract = build_b001_contract()
    assert is_tool_permitted(contract, "Read") is True
    assert is_tool_permitted(contract, "Grep") is True
    assert is_tool_permitted(contract, "Write") is False
    assert is_tool_permitted(contract, "Bash") is False


def test_deadline_overrun_raises_on_timezone_aware_deadline():
    """REAL BUG FOUND (not synthetic), installed version 0.3.2: building
    the contract the "correct" modern way -- a timezone-aware UTC
    deadline, `tz_aware_deadline=True` (the default) -- makes
    `TemporalMonitor.is_past_deadline()` raise `TypeError: can't compare
    offset-naive and offset-aware datetimes`. Root cause (verified by
    reading agent_contracts/core/monitor.py): `TemporalMonitor` compares
    the deadline against a *naive* `datetime.now()` internally at every
    call site (is_past_deadline, get_remaining_seconds, is_over_duration,
    elapsed time tracking) -- the type hint on `TemporalConstraints.deadline`
    accepts a tz-aware `datetime`, but the monitor code was never updated
    to match. This is exactly the "library error / unexpected behavior"
    case the experiment plan asked to cover, caught cleanly here rather
    than crashing the caller."""
    contract = build_b001_contract(deadline_hours=-1, tz_aware_deadline=True)
    _, temporal_monitor = make_enforcer(contract)
    temporal_monitor.start()
    with pytest.raises(TypeError, match="offset-naive and offset-aware"):
        temporal_monitor.is_past_deadline()


def test_deadline_overrun_is_detected_with_naive_datetime_workaround():
    """Same scenario as above, but built with `tz_aware_deadline=False`
    (naive datetimes throughout) to match what the library's monitor code
    actually expects -- this is the workaround, not a fix (see docs/
    research/AGENT_CONTRACTS_EXPERIMENT_01.md). With the workaround
    applied, the deadline-overrun detection itself works correctly."""
    contract = build_b001_contract(deadline_hours=-1, tz_aware_deadline=False)
    _, temporal_monitor = make_enforcer(contract)
    temporal_monitor.start()
    assert temporal_monitor.is_past_deadline() is True


def test_deadline_not_yet_overrun_when_future_with_naive_datetime_workaround():
    """Symmetric case: a future (naive) deadline must NOT be reported as
    passed."""
    contract = build_b001_contract(deadline_hours=24, tz_aware_deadline=False)
    _, temporal_monitor = make_enforcer(contract)
    temporal_monitor.start()
    assert temporal_monitor.is_past_deadline() is False


def test_success_criterion_is_deterministically_checkable():
    """The library stores SuccessCriterion.condition as a plain callable
    (or string) -- it does NOT evaluate it itself (verified: no
    `evaluate`-style method exists anywhere in the installed package, see
    contract_experiment.evaluate_success docstring). This test proves the
    round-trip: store a callable, retrieve it from the Contract, invoke it
    ourselves, get a deterministic true/false."""
    contract = build_b001_contract()

    ok, failed = evaluate_success(contract, {"review_text": ""})
    assert ok is False
    assert failed == ["review_comment_posted"]

    ok, failed = evaluate_success(contract, {"review_text": "Looks good, one nit."})
    assert ok is True
    assert failed == []


def test_library_error_on_invalid_input_is_caught_cleanly():
    """Malformed contract data must raise a specific, catchable exception
    from the library itself, not something opaque -- and our code must be
    able to catch it cleanly rather than crash."""
    from agent_contracts import ResourceConstraints, SuccessCriterion

    with pytest.raises(ValueError, match="non-negative"):
        ResourceConstraints(tokens=-1)

    with pytest.raises(ValueError, match=r"weight must be in \[0, 1\]"):
        SuccessCriterion(name="bad", condition=lambda r: True, weight=1.5)


# ---------------------------------------------------------------------------
# Exploratory (not one of the required 6, but directly informs step 5's
# "multi-agent work orders" question) -- kept separate and clearly labeled.
# ---------------------------------------------------------------------------


def test_multi_agent_delegation_conservation_exists():
    """Step 5 asks whether the library is single-agent-focused or fit for
    multi-agent work orders. Verified by direct API inspection
    (agent_contracts.core.delegation): ConservationViolationError,
    ResourceAllocation, and plan_resource_allocation exist specifically to
    prevent a parent contract's budget being over-allocated across
    multiple delegated sub-agents/sub-bounties -- this is real multi-agent
    support in the data model, not just single-agent budget tracking."""
    from agent_contracts import ConservationViolationError, ResourceAllocation, plan_resource_allocation

    assert issubclass(ConservationViolationError, Exception)
    assert ResourceAllocation is not None
    assert callable(plan_resource_allocation)
