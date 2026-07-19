"""
Minimal stdlib-only equivalent of contract_experiment.py, for comparison
only (docs/research/AGENT_CONTRACTS_EXPERIMENT_01.md step 5). Deliberately
just enough code to cover the same 6 test cases as the ai-agent-contracts
experiment -- not a second framework, no ambition beyond "what would we
write ourselves for b001's governance needs, in plain Python."

Not production code. Not imported by village/*.py.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable


@dataclass
class BountyBudget:
    """Everything the b001 experiment actually needed: a token/cost cap, a
    hard deadline, an allowed-tool whitelist, one required success check."""

    tokens_limit: int
    cost_usd_limit: float
    api_calls_limit: int
    deadline: datetime
    allowed_tools: list[str] = field(default_factory=list)
    success_check: Callable[[dict], bool] | None = None

    tokens_used: int = 0
    cost_usd_used: float = 0.0
    api_calls_used: int = 0

    def __post_init__(self) -> None:
        if self.tokens_limit < 0 or self.cost_usd_limit < 0 or self.api_calls_limit < 0:
            raise ValueError("budget limits must be non-negative")


def build_b001_budget(deadline_hours: int = 24) -> BountyBudget:
    return BountyBudget(
        tokens_limit=20_000,
        cost_usd_limit=0.50,
        api_calls_limit=10,
        deadline=datetime.now(timezone.utc) + timedelta(hours=deadline_hours),
        allowed_tools=["Read", "Grep"],
        success_check=lambda result: bool(result.get("review_text")),
    )


def record_usage(budget: BountyBudget, tokens: int = 0, cost_usd: float = 0.0, api_calls: int = 0) -> None:
    budget.tokens_used += tokens
    budget.cost_usd_used += cost_usd
    budget.api_calls_used += api_calls


def is_over_budget(budget: BountyBudget) -> list[str]:
    violations = []
    if budget.tokens_used > budget.tokens_limit:
        violations.append("tokens")
    if budget.cost_usd_used > budget.cost_usd_limit:
        violations.append("cost_usd")
    if budget.api_calls_used > budget.api_calls_limit:
        violations.append("api_calls")
    return violations


def is_tool_permitted(budget: BountyBudget, tool_name: str) -> bool:
    return tool_name in budget.allowed_tools


def is_past_deadline(budget: BountyBudget, now: datetime | None = None) -> bool:
    return (now or datetime.now(timezone.utc)) > budget.deadline


def evaluate_success(budget: BountyBudget, result: dict) -> bool:
    if budget.success_check is None:
        return False
    return bool(budget.success_check(result))
