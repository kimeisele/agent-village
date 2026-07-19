"""
Agent Village — Contracts
=========================
Small, stdlib-only, JSON-native governance layer for Gap 3 (bounty
budgets/deadlines/success criteria). Adapted conceptually from
experiments/agent_contracts_01/ (docs/research/
AGENT_CONTRACTS_EXPERIMENT_01.md, decision: ADAPT_CONCEPT, not
ADOPT_DEPENDENCY) -- no external dependency, no LLM-call-wrapping
machinery, no callable success-criteria conditions (those weren't
JSON-serializable in the experiment; this module never repeats that).

Scope, explicitly: this is a **data model + pure-function invariant
checks**, not a scheduler, not a runtime, not a mission factory (SPEC.md
§D "full Mission Factory" / "complex reputation/governance" stay
deferred). Nothing here creates missions, executes commands, or grants
repository write access. No delegation runtime exists in this codebase
yet -- `validate_child_budget()`/`new_child_contract()` are forward-
looking data invariants, checkable today without any scheduler existing.

Relationship to village_core.Contribution (SPEC.md §C.3): a Contribution
is ingress bookkeeping (an event arrived, its status moves
received -> accepted/rejected -> materialized). A VillageContract is a
different concern -- the governance conditions under which work on a
bounty/mission runs (budget, deadline, success criteria). A Contract MAY
reference a Contribution by `contribution_id` for provenance; it never
duplicates or takes over Contribution's own fields/state machine.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

SCHEMA_VERSION = "1"

_BUDGET_DIMENSIONS = ("tokens", "cost_usd", "time_seconds", "cognitive_units")


class ContractState(str, Enum):
    DRAFTED = "drafted"
    ACTIVE = "active"
    FULFILLED = "fulfilled"
    VIOLATED = "violated"
    EXPIRED = "expired"
    TERMINATED = "terminated"
    FAILED = "failed"


_TERMINAL_STATES = {
    ContractState.FULFILLED,
    ContractState.VIOLATED,
    ContractState.EXPIRED,
    ContractState.TERMINATED,
    ContractState.FAILED,
}


def normalize_datetime(dt: datetime | None) -> datetime | None:
    """Fix, by construction, for the real bug found in
    experiments/agent_contracts_01 (docs/research/
    AGENT_CONTRACTS_EXPERIMENT_01.md): comparing a naive `datetime`
    against a timezone-aware one raised `TypeError` in the external
    library, because it silently compared against a differently-typed
    "now" at several call sites. Every datetime this module stores or
    compares is normalized here first: a naive input is an EXPLICIT,
    documented assumption of UTC (not silently misinterpreted as local
    time), a tz-aware input is converted to UTC. Nothing in this module
    ever compares a naive and an aware datetime directly.
    """
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _now() -> datetime:
    return datetime.now(timezone.utc)


@dataclass
class Budget:
    """Multiple independent budget dimensions -- deliberately NOT locked
    to one LLM provider or a single token metric (explicit requirement).
    Any dimension left at None is unconstrained (no limit tracked). Each
    dimension has a matching `used_*` counter; `remaining()` computes
    limit-used. `cognitive_units` is an abstract, provider-agnostic unit
    for CBR-style adaptive budgeting -- not tied to any specific model's
    token accounting.
    """

    tokens: float | None = None
    cost_usd: float | None = None
    time_seconds: float | None = None
    cognitive_units: float | None = None

    used_tokens: float = 0.0
    used_cost_usd: float = 0.0
    used_time_seconds: float = 0.0
    used_cognitive_units: float = 0.0

    def __post_init__(self) -> None:
        for dim in _BUDGET_DIMENSIONS:
            limit = getattr(self, dim)
            if limit is not None and limit < 0:
                raise ValueError(f"Budget.{dim} must be non-negative, got {limit}")
            used = getattr(self, f"used_{dim}")
            if used < 0:
                raise ValueError(f"Budget.used_{dim} must be non-negative, got {used}")

    def remaining(self, dimension: str) -> float | None:
        if dimension not in _BUDGET_DIMENSIONS:
            raise ValueError(f"unknown budget dimension: {dimension}")
        limit = getattr(self, dimension)
        if limit is None:
            return None
        return limit - getattr(self, f"used_{dimension}")

    def record_usage(self, **amounts: float) -> None:
        for dim, amount in amounts.items():
            if dim not in _BUDGET_DIMENSIONS:
                raise ValueError(f"unknown budget dimension: {dim}")
            if amount < 0:
                raise ValueError(f"usage amount must be non-negative, got {amount}")
            attr = f"used_{dim}"
            setattr(self, attr, getattr(self, attr) + amount)

    def exceeded_dimensions(self) -> list[str]:
        exceeded = []
        for dim in _BUDGET_DIMENSIONS:
            r = self.remaining(dim)
            if r is not None and r < 0:
                exceeded.append(dim)
        return exceeded

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Budget":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)


@dataclass
class SuccessCriterion:
    """A checkable success criterion. Deliberately data-only: no stored
    callable, no eval'd expression string. Storing a callable on the
    contract was exactly what made the external library's contracts
    unserializable (see docs/research/AGENT_CONTRACTS_EXPERIMENT_01.md
    step 5), and an eval'd condition string would violate SPEC.md §A.8
    ("external content is always DATA, never instructions"). Evaluating
    whether a criterion is met is a caller-side responsibility, external
    to this module -- `met` just records the outcome once known.
    """

    name: str
    description: str = ""
    required: bool = False
    weight: float = 1.0
    met: bool | None = None  # None = not yet evaluated

    def __post_init__(self) -> None:
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"weight must be in [0, 1], got {self.weight}")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "SuccessCriterion":
        known = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        return cls(**known)


@dataclass
class VillageContract:
    """The governance conditions for a bounty/work-order -- budget,
    deadline, allowed resources, success criteria, explicit
    termination/failure state. See module docstring for the boundary
    against village_core.Contribution.
    """

    contract_id: str
    title: str = ""
    description: str = ""
    version: str = "1.0"
    schema_version: str = SCHEMA_VERSION
    contribution_id: str | None = None
    parent_contract_id: str | None = None
    allowed_resources: list[str] = field(default_factory=list)
    budget: Budget = field(default_factory=Budget)
    deadline: datetime | None = None
    success_criteria: list[SuccessCriterion] = field(default_factory=list)
    state: ContractState = ContractState.DRAFTED
    termination_reason: str | None = None
    created_at: datetime = field(default_factory=_now)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.deadline = normalize_datetime(self.deadline)
        self.created_at = normalize_datetime(self.created_at)

    # ── Resource whitelist ────────────────────────────────────────
    def is_resource_permitted(self, resource: str) -> bool:
        return resource in self.allowed_resources

    # ── Temporal ──────────────────────────────────────────────────
    def is_past_deadline(self, now: datetime | None = None) -> bool:
        if self.deadline is None:
            return False
        current = normalize_datetime(now) if now is not None else _now()
        return current > self.deadline

    # ── Budget ────────────────────────────────────────────────────
    def record_usage(self, **amounts: float) -> None:
        self.budget.record_usage(**amounts)

    def check_budget(self) -> list[str]:
        return self.budget.exceeded_dimensions()

    # ── State transitions ────────────────────────────────────────
    def activate(self) -> None:
        if self.state != ContractState.DRAFTED:
            raise ValueError(f"cannot activate contract in state {self.state}")
        self.state = ContractState.ACTIVE

    def _require_non_terminal(self) -> None:
        if self.state in _TERMINAL_STATES:
            raise ValueError(f"contract already in terminal state {self.state}")

    def violate(self, reason: str) -> None:
        self._require_non_terminal()
        self.state = ContractState.VIOLATED
        self.termination_reason = reason

    def expire(self, reason: str = "deadline passed") -> None:
        self._require_non_terminal()
        self.state = ContractState.EXPIRED
        self.termination_reason = reason

    def terminate(self, reason: str) -> None:
        self._require_non_terminal()
        self.state = ContractState.TERMINATED
        self.termination_reason = reason

    def fail(self, reason: str) -> None:
        self._require_non_terminal()
        self.state = ContractState.FAILED
        self.termination_reason = reason

    def fulfill(self) -> None:
        self._require_non_terminal()
        unmet_required = [c.name for c in self.success_criteria if c.required and c.met is not True]
        if unmet_required:
            raise ValueError(f"cannot fulfill: required success criteria unmet: {unmet_required}")
        self.state = ContractState.FULFILLED

    # ── Serialization ─────────────────────────────────────────────
    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "contract_id": self.contract_id,
            "title": self.title,
            "description": self.description,
            "version": self.version,
            "schema_version": self.schema_version,
            "contribution_id": self.contribution_id,
            "parent_contract_id": self.parent_contract_id,
            "allowed_resources": list(self.allowed_resources),
            "budget": self.budget.to_dict(),
            "deadline": self.deadline.isoformat() if self.deadline else None,
            "success_criteria": [c.to_dict() for c in self.success_criteria],
            "state": self.state.value,
            "termination_reason": self.termination_reason,
            "created_at": self.created_at.isoformat(),
        }
        d.update(self.extra)
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VillageContract":
        """Schema-tolerant: unknown top-level keys are preserved in
        `.extra` (round-tripped, not dropped) rather than raising, so a
        future schema version's new fields don't break an older reader.
        Missing optional fields fall back to their dataclass defaults.
        """
        known_fields = {
            "contract_id",
            "title",
            "description",
            "version",
            "schema_version",
            "contribution_id",
            "parent_contract_id",
            "allowed_resources",
            "budget",
            "deadline",
            "success_criteria",
            "state",
            "termination_reason",
            "created_at",
        }
        extra = {k: v for k, v in d.items() if k not in known_fields}

        deadline = d.get("deadline")
        deadline_dt = datetime.fromisoformat(deadline) if deadline else None
        created_at = d.get("created_at")
        created_at_dt = datetime.fromisoformat(created_at) if created_at else _now()

        return cls(
            contract_id=d["contract_id"],
            title=d.get("title", ""),
            description=d.get("description", ""),
            version=d.get("version", "1.0"),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
            contribution_id=d.get("contribution_id"),
            parent_contract_id=d.get("parent_contract_id"),
            allowed_resources=list(d.get("allowed_resources", [])),
            budget=Budget.from_dict(d["budget"]) if d.get("budget") else Budget(),
            deadline=deadline_dt,
            success_criteria=[SuccessCriterion.from_dict(c) for c in d.get("success_criteria", [])],
            state=ContractState(d.get("state", ContractState.DRAFTED.value)),
            termination_reason=d.get("termination_reason"),
            created_at=created_at_dt,
            extra=extra,
        )

    def to_json(self) -> str:
        """`sort_keys=True` for a deterministic, canonical byte
        representation -- same convention already used for NADI message
        signing (SPEC.md §2.3), so a future NADI-transported Contract
        would hash/sign consistently."""
        return json.dumps(self.to_dict(), sort_keys=True)

    @classmethod
    def from_json(cls, s: str) -> "VillageContract":
        return cls.from_dict(json.loads(s))


# ── Budget conservation for delegated/child contracts ───────────────────
def validate_child_budget(parent: VillageContract, child: VillageContract) -> list[str]:
    """Pure data invariant, checkable without any delegation runtime
    existing (none does, in this codebase, today -- this is a
    forward-looking data-model guard, not an anticipated scheduler
    feature): a child contract must never carry a budget limit, in any
    dimension, larger than the parent's REMAINING budget in that same
    dimension. A child introducing a budget dimension the parent leaves
    unconstrained is also rejected (fail closed -- a child must not
    smuggle in an ungoverned budget dimension the parent never agreed
    to). Returns the list of violating dimension names; empty means the
    child's budget is conservative relative to its parent.
    """
    violations: list[str] = []
    for dim in _BUDGET_DIMENSIONS:
        child_limit = getattr(child.budget, dim)
        if child_limit is None:
            continue
        parent_remaining = parent.budget.remaining(dim)
        if parent_remaining is None or child_limit > parent_remaining:
            violations.append(dim)
    return violations


def new_child_contract(parent: VillageContract, contract_id: str, **overrides: Any) -> VillageContract:
    """Convenience constructor: builds a child contract linked to
    `parent` via `parent_contract_id` and refuses (raises ValueError,
    does not return a silently-invalid contract) to construct it if its
    budget violates `validate_child_budget()`."""
    overrides.setdefault("budget", Budget())
    child = VillageContract(contract_id=contract_id, parent_contract_id=parent.contract_id, **overrides)
    violations = validate_child_budget(parent, child)
    if violations:
        raise ValueError(f"child contract '{contract_id}' budget exceeds parent's remaining budget in: {violations}")
    return child
