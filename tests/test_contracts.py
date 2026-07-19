"""
Tests for village/contracts.py -- the JSON-native governance layer for
Gap 3 (bounty budgets/deadlines/success criteria), adapted conceptually
from experiments/agent_contracts_01/ (docs/research/
AGENT_CONTRACTS_EXPERIMENT_01.md, decision: ADAPT_CONCEPT).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from village.contracts import (
    Budget,
    ContractState,
    SuccessCriterion,
    VillageContract,
    new_child_contract,
    normalize_datetime,
    validate_child_budget,
)

FIXTURES = Path(__file__).parent / "fixtures" / "contracts"


def _b001_contract(**overrides) -> VillageContract:
    defaults = dict(
        contract_id="contract:b001:1",
        title="Review village/heartbeat.py",
        description="Read and review the heartbeat scanner.",
        contribution_id="moltbook:c1:bounty_claim",
        allowed_resources=["Read", "Grep"],
        budget=Budget(tokens=20_000, cost_usd=0.50, time_seconds=3600),
    )
    defaults.update(overrides)
    return VillageContract(**defaults)


# ── Identity, version, description ──────────────────────────────────────


def test_unique_id_and_version_and_description():
    c = _b001_contract()
    assert c.contract_id == "contract:b001:1"
    assert c.version == "1.0"
    assert c.schema_version == "1"
    assert "review" in c.description.lower()
    assert c.state == ContractState.DRAFTED


# ── Typed allowed resources ──────────────────────────────────────────────


def test_disallowed_resource_is_rejected():
    c = _b001_contract()
    assert c.is_resource_permitted("Read") is True
    assert c.is_resource_permitted("Grep") is True
    assert c.is_resource_permitted("Write") is False
    assert c.is_resource_permitted("Bash") is False


# ── Budget: multiple dimensions, not locked to one provider/metric ──────


def test_budget_expresses_multiple_independent_dimensions():
    b = Budget(tokens=1000, cost_usd=2.5, time_seconds=60, cognitive_units=10)
    assert b.remaining("tokens") == 1000
    assert b.remaining("cost_usd") == 2.5
    assert b.remaining("time_seconds") == 60
    assert b.remaining("cognitive_units") == 10


def test_budget_dimension_left_none_is_unconstrained():
    b = Budget(tokens=1000)
    assert b.remaining("cost_usd") is None
    b.record_usage(cost_usd=1_000_000)  # no limit set -> no error, no violation
    assert b.exceeded_dimensions() == []


def test_budget_overrun_is_detected_per_dimension():
    c = _b001_contract()
    c.record_usage(tokens=5_000)
    assert c.check_budget() == []

    c.record_usage(tokens=20_000)  # total 25_000 > 20_000 limit
    assert c.check_budget() == ["tokens"]

    c.record_usage(cost_usd=1.0)  # total 1.0 > 0.50 limit
    assert set(c.check_budget()) == {"tokens", "cost_usd"}


def test_negative_budget_is_rejected():
    with pytest.raises(ValueError, match="non-negative"):
        Budget(tokens=-1)


def test_negative_usage_amount_is_rejected():
    b = Budget(tokens=1000)
    with pytest.raises(ValueError, match="non-negative"):
        b.record_usage(tokens=-5)


def test_unknown_budget_dimension_is_rejected():
    b = Budget(tokens=1000)
    with pytest.raises(ValueError, match="unknown budget dimension"):
        b.record_usage(gpu_hours=1)


# ── Deadline: naive AND timezone-aware handled unambiguously ────────────
# Real bug found in experiments/agent_contracts_01 (docs/research/
# AGENT_CONTRACTS_EXPERIMENT_01.md): the external library crashed
# comparing a tz-aware deadline against a naive `datetime.now()`. Fixed
# here by construction (normalize_datetime), not repeated.


def test_naive_deadline_is_normalized_to_utc_aware():
    naive = datetime(2026, 7, 20, 12, 0, 0)
    assert naive.tzinfo is None
    normalized = normalize_datetime(naive)
    assert normalized.tzinfo is not None
    assert normalized.utcoffset() == timedelta(0)


def test_aware_deadline_is_converted_to_utc():
    aware = datetime(2026, 7, 20, 14, 0, 0, tzinfo=timezone(timedelta(hours=2)))
    normalized = normalize_datetime(aware)
    assert normalized == datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc)


def test_contract_never_mixes_naive_and_aware_datetimes():
    """Construct with a naive deadline, check with a naive `now` -- must
    not raise, unlike the library bug this module was built to avoid."""
    c = _b001_contract(deadline=datetime(2020, 1, 1))  # naive, far in the past
    assert c.deadline.tzinfo is not None  # normalized on construction
    assert c.is_past_deadline(now=datetime(2026, 7, 19)) is True  # naive `now` too -- must not crash
    assert c.is_past_deadline(now=datetime.now(timezone.utc)) is True  # aware `now` -- must also work


def test_deadline_overrun_is_detected():
    c = _b001_contract(deadline=datetime.now(timezone.utc) - timedelta(hours=1))
    assert c.is_past_deadline() is True


def test_deadline_not_yet_overrun_when_future():
    c = _b001_contract(deadline=datetime.now(timezone.utc) + timedelta(hours=24))
    assert c.is_past_deadline() is False


def test_no_deadline_is_never_past_due():
    c = _b001_contract(deadline=None)
    assert c.is_past_deadline() is False


# ── Success criteria: checkable, data-only (no stored callable/eval) ────


def test_success_criterion_is_data_only_and_checkable():
    crit = SuccessCriterion(name="review_posted", required=True)
    assert crit.met is None  # not yet evaluated
    crit.met = True  # caller-side evaluation sets this; module never evaluates itself
    assert crit.met is True


def test_success_criterion_weight_out_of_range_rejected():
    with pytest.raises(ValueError, match=r"weight must be in \[0, 1\]"):
        SuccessCriterion(name="bad", weight=1.5)


def test_fulfill_requires_all_required_criteria_met():
    c = _b001_contract(success_criteria=[SuccessCriterion(name="review_posted", required=True)])
    with pytest.raises(ValueError, match="required success criteria unmet"):
        c.fulfill()

    c.success_criteria[0].met = True
    c.fulfill()
    assert c.state == ContractState.FULFILLED


def test_fulfill_ignores_unmet_optional_criteria():
    c = _b001_contract(
        success_criteria=[
            SuccessCriterion(name="required_one", required=True, met=True),
            SuccessCriterion(name="optional_one", required=False, met=None),
        ]
    )
    c.fulfill()
    assert c.state == ContractState.FULFILLED


# ── Explicit termination/failure states ──────────────────────────────────


def test_explicit_termination_states_are_distinct_and_terminal():
    for method, expected_state in [
        ("violate", ContractState.VIOLATED),
        ("expire", ContractState.EXPIRED),
        ("terminate", ContractState.TERMINATED),
        ("fail", ContractState.FAILED),
    ]:
        c = _b001_contract()
        getattr(c, method)("test reason")
        assert c.state == expected_state
        assert c.termination_reason == "test reason"
        with pytest.raises(ValueError, match="terminal state"):
            c.violate("cannot re-terminate")


def test_activate_requires_drafted_state():
    c = _b001_contract()
    c.activate()
    assert c.state == ContractState.ACTIVE
    with pytest.raises(ValueError, match="cannot activate"):
        c.activate()


# ── Budget conservation for delegated/child contracts ────────────────────
# Pure data invariant -- no delegation runtime exists in this codebase
# today; this is a forward-looking data-model guard only.


def test_child_budget_within_parent_remaining_is_accepted():
    parent = _b001_contract()
    child = new_child_contract(parent, "contract:b001:1:sub-a", budget=Budget(tokens=5_000, cost_usd=0.10))
    assert child.parent_contract_id == parent.contract_id
    assert validate_child_budget(parent, child) == []


def test_child_budget_exceeding_parent_remaining_is_rejected():
    parent = _b001_contract()  # tokens=20_000
    with pytest.raises(ValueError, match="exceeds parent's remaining budget"):
        new_child_contract(parent, "contract:b001:1:sub-a", budget=Budget(tokens=25_000))


def test_child_budget_accounts_for_parent_usage_already_consumed():
    parent = _b001_contract()  # tokens=20_000
    parent.record_usage(tokens=18_000)  # only 2_000 remaining
    violations = validate_child_budget(parent, VillageContract(contract_id="c", budget=Budget(tokens=5_000)))
    assert violations == ["tokens"]


def test_child_budget_dimension_parent_leaves_unconstrained_is_rejected():
    """Fail closed: a child must not introduce a budget dimension the
    parent never agreed to govern."""
    parent = _b001_contract()  # no cognitive_units limit set
    violations = validate_child_budget(parent, VillageContract(contract_id="c", budget=Budget(cognitive_units=10)))
    assert violations == ["cognitive_units"]


def test_child_budget_dimension_left_none_is_fine():
    """A child that simply doesn't declare a dimension at all is not a
    violation -- only declaring MORE than the parent allows is."""
    parent = _b001_contract()
    child = VillageContract(contract_id="c", budget=Budget())  # no dimensions declared
    assert validate_child_budget(parent, child) == []


# ── JSON serialization: deterministic roundtrip ───────────────────────────


def test_json_roundtrip_is_lossless():
    original = _b001_contract(
        success_criteria=[SuccessCriterion(name="review_posted", required=True, met=True)],
        deadline=datetime(2026, 7, 20, 12, 0, 0, tzinfo=timezone.utc),
    )
    restored = VillageContract.from_json(original.to_json())
    assert restored.to_dict() == original.to_dict()


def test_json_serialization_is_deterministic_across_calls():
    c = _b001_contract()
    assert c.to_json() == c.to_json()  # sort_keys=True -> stable byte-for-byte output


def test_json_roundtrip_against_real_fixture_files():
    """Not just an in-memory dict -- load real committed JSON files
    (tests/fixtures/contracts/) and prove the module round-trips them
    losslessly, the same files a future NADI transport layer or CLI tool
    would read/write."""
    parent_path = FIXTURES / "b001_contract.json"
    parent_data = json.loads(parent_path.read_text())
    parent = VillageContract.from_dict(parent_data)
    assert parent.to_dict() == parent_data

    child_path = FIXTURES / "b001_child_contract.json"
    child_data = json.loads(child_path.read_text())
    child = VillageContract.from_dict(child_data)
    assert child.to_dict() == child_data

    # And the fixture pair genuinely satisfies the conservation invariant.
    assert validate_child_budget(parent, child) == []


# ── Unknown fields / future schema versions ───────────────────────────────


def test_unknown_top_level_fields_are_preserved_not_dropped():
    data = _b001_contract().to_dict()
    data["future_field_v2"] = "some-value-a-newer-schema-version-added"
    restored = VillageContract.from_dict(data)
    assert restored.extra == {"future_field_v2": "some-value-a-newer-schema-version-added"}
    # And round-tripping again keeps it -- not silently dropped on the next write.
    assert restored.to_dict()["future_field_v2"] == "some-value-a-newer-schema-version-added"


def test_missing_optional_fields_fall_back_to_defaults():
    minimal = {"contract_id": "contract:minimal:1"}
    c = VillageContract.from_dict(minimal)
    assert c.contract_id == "contract:minimal:1"
    assert c.title == ""
    assert c.state == ContractState.DRAFTED
    assert c.budget.tokens is None
    assert c.deadline is None
