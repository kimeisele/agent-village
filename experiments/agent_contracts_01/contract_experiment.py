"""
Isolated evaluation experiment: does `ai-agent-contracts` (Apache-2.0,
PyPI, pinned 0.3.2) express Agent Village's bounty-governance gap (SPEC.md
§D "complex reputation/governance" / §A.11 "Marketplace", CAPABILITY_SURVEY_01
Gap 3) better than a small stdlib-only equivalent?

NOT production code. Not imported by village/*.py, not wired into any
workflow. Models the real b001 bounty from data/village/bounties.json
(id/title/description/reward/status/claimed_by/claimed_at/completed_at)
as an `agent_contracts.Contract`, purely offline -- no LLM calls, no
network access beyond what pip already fetched. All resource/temporal
usage is simulated by directly driving the library's ResourceMonitor/
TemporalMonitor, since the library itself does not evaluate its
`SuccessCriterion.condition`/`TerminationCondition.condition` fields
(verified: no `evaluate`-style helper exists in the installed package;
these fields are the caller's responsibility to interpret).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from agent_contracts import (
    Capabilities,
    Contract,
    ContractEnforcer,
    DeadlineType,
    ResourceConstraints,
    SuccessCriterion,
    TemporalConstraints,
    TemporalMonitor,
    TerminationCondition,
)

# The real b001 record, as it exists in data/village/bounties.json at time
# of writing (title/description copied verbatim, not paraphrased).
B001 = {
    "id": "b001",
    "title": "Review village/heartbeat.py",
    "description": (
        "Read and review the heartbeat scanner. Suggest improvements for error handling or Moltbook API integration."
    ),
    "reward": "reputation",
}


def build_b001_contract(deadline_hours: int = 24, tz_aware_deadline: bool = True) -> Contract:
    """Model b001 as a Contract: a review task, read-only capability set
    (this is a *review* bounty, not a code-change bounty -- Write/Bash
    are deliberately NOT in the allowed tool list, modeling "unerlaubte
    Ressource" for the rejected-resource test case), a small token/cost
    budget, a hard deadline, one required success criterion, and one
    explicit termination condition.

    `deadline_hours` may be negative to build an already-expired contract
    for the deadline-overrun test (TemporalConstraints is a frozen
    dataclass -- you cannot mutate `.deadline` after construction, so a
    fresh Contract is the only way to get a past deadline).

    `tz_aware_deadline`: see docs/research/AGENT_CONTRACTS_EXPERIMENT_01.md
    step 4 / the "library error" test -- the installed 0.3.2
    TemporalMonitor compares its deadline against a *naive* `datetime.now()`
    internally (agent_contracts/core/monitor.py, multiple call sites), so a
    timezone-aware deadline (the correct modern choice, and what
    tz_aware_deadline=True, the default, produces) raises
    `TypeError: can't compare offset-naive and offset-aware datetimes` the
    moment `TemporalMonitor.is_past_deadline()`/`get_remaining_seconds()`
    is called. Pass `tz_aware_deadline=False` to build a contract whose
    deadline exercises the working (naive-only) code path instead.
    """
    now = datetime.now(timezone.utc) if tz_aware_deadline else datetime.now()
    return Contract(
        id=B001["id"],
        name=B001["title"],
        description=B001["description"],
        capabilities=Capabilities(tools=["Read", "Grep"]),
        resources=ResourceConstraints(
            tokens=20_000,
            api_calls=10,
            cost_usd=0.50,
        ),
        temporal=TemporalConstraints(
            deadline=now + timedelta(hours=deadline_hours),
            max_duration=timedelta(hours=abs(deadline_hours) or 1),
            deadline_type=DeadlineType.HARD,
        ),
        success_criteria=[
            SuccessCriterion(
                name="review_comment_posted",
                condition=lambda result: bool(result.get("review_text")),
                weight=1.0,
                required=True,
            ),
        ],
        termination_conditions=[
            TerminationCondition(
                type="budget_exceeded",
                condition="resources.cost_usd > resources.cost_usd_limit",
                priority=1,
            ),
        ],
    )


def evaluate_success(contract: Contract, result: dict) -> tuple[bool, list[str]]:
    """The library does not evaluate SuccessCriterion.condition itself --
    this is the caller-side evaluation loop a real integration would need
    to write. Returns (all_required_met, failed_required_names)."""
    failed: list[str] = []
    for criterion in contract.success_criteria:
        condition = criterion.condition
        passed = bool(condition(result)) if callable(condition) else bool(condition)
        if criterion.required and not passed:
            failed.append(criterion.name)
    return (len(failed) == 0, failed)


def make_enforcer(contract: Contract, strict_mode: bool = True) -> tuple[ContractEnforcer, TemporalMonitor]:
    enforcer = ContractEnforcer(contract, strict_mode=strict_mode)
    temporal_monitor = TemporalMonitor(contract)
    return enforcer, temporal_monitor


def is_tool_permitted(contract: Contract, tool_name: str) -> bool:
    """Capability whitelist check ("unerlaubte Ressource abgelehnt").

    The library's `Capabilities.tools` field is a data spec, not a
    self-enforcing whitelist -- real enforcement (checking a specific
    tool-call attempt against it) only happens inside `ContractExecutor`,
    which drives an actual LLM/tool-calling loop. Since this experiment
    makes no LLM calls, this one-line check models what that enforcement
    point would do: reject any tool name not present in
    `contract.capabilities.tools`. Verified by reading
    agent_contracts/core/capabilities.py (no LLM code, pure dataclass) --
    not assumed from the README.
    """
    if contract.capabilities is None:
        return True
    return tool_name in contract.capabilities.tools
