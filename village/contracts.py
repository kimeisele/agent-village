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

import hashlib
import json
import re as _re
import uuid
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
        limit: float | None = getattr(self, dimension)
        if limit is None:
            return None
        used: float = getattr(self, f"used_{dimension}")
        return limit - used

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


class EvaluatorType(str, Enum):
    """Allowlisted deterministic evaluator kinds."""

    FIELD_PRESENT = "field_present"
    FIELD_VALUE = "field_value"
    FIELD_COUNT = "field_count"


def canonical_json_dumps(obj: object) -> str:
    """Deterministic JSON serialization for hashing.

    Sorted keys, no trailing whitespace, UTF-8, compact separators.
    Uses allow_nan=False to reject NaN and Infinity at the serialization
    layer (not via string scanning — ordinary strings containing those
    words are unaffected).
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=True, allow_nan=False)


def compute_criterion_definition_hash(evaluator: EvaluatorType | None, params: dict[str, Any]) -> str:
    """Canonical hash of a criterion's evaluator configuration."""
    if evaluator is None:
        projection: dict[str, object] = {"evaluator": None}
    else:
        projection = {"evaluator": evaluator.value, "evaluator_params": params}
    return hashlib.sha256(canonical_json_dumps(projection).encode()).hexdigest()


MAX_FIELD_LEN = 128
MAX_SEGMENTS = 4
MAX_SEGMENT_LEN = 32
MAX_VALUE_STRLEN = 256
MAX_MIN_COUNT = 1_000_000

FIELD_SEGMENT_RE = _re.compile(r"^[a-zA-Z0-9_]+$")


def validate_evaluator_config(evaluator: EvaluatorType, params: dict[str, Any]) -> None:
    """Validate evaluator configuration against the Issue #34 schemas.

    Raises ValueError for any violation. Used by both from_untrusted_terms
    and persisted-state validation.
    """
    if evaluator == EvaluatorType.FIELD_PRESENT:
        allowed = {"field"}
        for k in params:
            if k not in allowed:
                raise ValueError(f"unknown parameter for FIELD_PRESENT: {k!r}")
        field = params.get("field")
        if not isinstance(field, str):
            raise ValueError("FIELD_PRESENT: field must be a string")
        validate_field_path(field)

    elif evaluator == EvaluatorType.FIELD_VALUE:
        allowed = {"field", "value"}
        for k in params:
            if k not in allowed:
                raise ValueError(f"unknown parameter for FIELD_VALUE: {k!r}")
        field = params.get("field")
        if not isinstance(field, str):
            raise ValueError("FIELD_VALUE: field must be a string")
        validate_field_path(field)
        value = params.get("value")
        if value is None:
            raise ValueError("FIELD_VALUE: value is required")
        if isinstance(value, str):
            if len(value) > MAX_VALUE_STRLEN:
                raise ValueError(f"FIELD_VALUE: string value too long ({len(value)} > {MAX_VALUE_STRLEN})")
        elif isinstance(value, bool):
            pass  # strict bool
        elif isinstance(value, (int, float)):
            import math as _math

            if isinstance(value, float) and (_math.isnan(value) or _math.isinf(value)):
                raise ValueError("FIELD_VALUE: numeric value must be finite")
        else:
            raise ValueError(f"FIELD_VALUE: unsupported value type {type(value).__name__}")

    elif evaluator == EvaluatorType.FIELD_COUNT:
        allowed = {"field", "min_count"}
        for k in params:
            if k not in allowed:
                raise ValueError(f"unknown parameter for FIELD_COUNT: {k!r}")
        field = params.get("field")
        if not isinstance(field, str):
            raise ValueError("FIELD_COUNT: field must be a string")
        validate_field_path(field)
        min_count = params.get("min_count")
        if isinstance(min_count, bool) or not isinstance(min_count, int):
            raise ValueError("FIELD_COUNT: min_count must be a strict integer")
        if min_count < 0 or min_count > MAX_MIN_COUNT:
            raise ValueError(f"FIELD_COUNT: min_count out of range ({min_count})")

    else:
        raise ValueError(f"unknown evaluator type: {evaluator!r}")


def validate_field_path(field: str) -> None:
    """Validate a dotted field path. Raises ValueError on violation."""
    if not field or len(field) > MAX_FIELD_LEN:
        raise ValueError(f"field path too long ({len(field)})")
    segments = field.split(".")
    if len(segments) > MAX_SEGMENTS:
        raise ValueError(f"too many path segments ({len(segments)})")
    for seg in segments:
        if not seg or len(seg) > MAX_SEGMENT_LEN:
            raise ValueError("path segment too long")
        if not FIELD_SEGMENT_RE.match(seg):
            raise ValueError(f"invalid path segment characters: {seg!r}")


def compute_review_policy_hash(contract: "VillageContract") -> str:
    """Canonical hash of all decision-relevant review-policy fields.

    Includes: auto_review_enabled, criterion IDs, definition hashes,
    required flags, evaluator types, evaluator params, schema version.
    Excludes: met, name, description, weight (mutable display/result fields).
    """
    projection: dict[str, object] = {
        "auto_review_enabled": contract.auto_review_enabled,
        "criteria": [
            {
                "criterion_id": c.criterion_id,
                "criterion_definition_hash": c.criterion_definition_hash,
                "required": c.required,
                "evaluator": c.evaluator.value if c.evaluator else None,
                "evaluator_params": c.evaluator_params,
            }
            for c in sorted(contract.success_criteria, key=lambda c: c.criterion_id)
        ],
        "policy_schema_version": 1,
    }
    return hashlib.sha256(canonical_json_dumps(projection).encode()).hexdigest()


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

    criterion_id: str = ""  # system-assigned, opaque, stable, unique per contract
    criterion_definition_hash: str = ""  # system-computed from evaluator config
    name: str = ""
    description: str = ""
    required: bool = False
    weight: float = 1.0
    met: bool | None = None  # None = not yet evaluated
    evaluator: EvaluatorType | None = None
    evaluator_params: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not 0.0 <= self.weight <= 1.0:
            raise ValueError(f"weight must be in [0, 1], got {self.weight}")
        # Enforce ID invariant: evaluator-bearing criteria must have ID + hash
        has_evaluator = self.evaluator is not None
        has_id = bool(self.criterion_id)
        has_hash = bool(self.criterion_definition_hash)
        if has_evaluator and (not has_id or not has_hash):
            raise ValueError("evaluator-bearing criterion must have criterion_id and criterion_definition_hash")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def create(
        cls,
        name: str,
        *,
        description: str = "",
        required: bool = False,
        weight: float = 1.0,
        evaluator: EvaluatorType | None = None,
        evaluator_params: dict[str, Any] | None = None,
    ) -> "SuccessCriterion":
        """Trusted creation factory for new canonical criteria.

        Validates evaluator configuration via the shared validator,
        generates a system-controlled criterion ID, computes the
        definition hash, and initializes met=None.
        """
        params = dict(evaluator_params) if evaluator_params else {}
        if evaluator is not None:
            validate_evaluator_config(evaluator, params)
        criterion_id = hashlib.sha256(f"{name}:{uuid.uuid4()}".encode()).hexdigest()[:16]
        definition_hash = compute_criterion_definition_hash(evaluator, params)
        return cls(
            criterion_id=criterion_id,
            criterion_definition_hash=definition_hash,
            name=name,
            description=description,
            required=required,
            weight=weight,
            met=None,
            evaluator=evaluator,
            evaluator_params=params,
        )

    @classmethod
    def from_untrusted_terms(cls, d: dict[str, Any]) -> "SuccessCriterion":
        """Create from external contract_terms. Ignores any supplied
        criterion_id or criterion_definition_hash — the system owns identity.
        Always creates met=None — external data may not pre-set results.
        Validates evaluator configuration via the shared validator."""
        known: dict[str, Any] = {f: d[f] for f in ("name", "description", "required", "weight") if f in d}
        # External data may NEVER pre-set evaluation results
        known["met"] = None
        # Parse and validate evaluator via shared validator
        evaluator_raw = d.get("evaluator")
        params = d.get("evaluator_params", {})
        if not isinstance(params, dict):
            raise ValueError("evaluator_params must be a dict")
        if evaluator_raw is not None:
            if not isinstance(evaluator_raw, str):
                raise ValueError(f"evaluator must be a string or null, got {type(evaluator_raw).__name__}")
            try:
                evaluator = EvaluatorType(evaluator_raw)
            except ValueError:
                raise ValueError(f"unknown evaluator type: {evaluator_raw!r}")
            validate_evaluator_config(evaluator, params)
            known["evaluator"] = evaluator
            known["evaluator_params"] = params
        else:
            known["evaluator"] = None
            known["evaluator_params"] = {}
        # System-generated identity (never from external input)
        known["criterion_id"] = hashlib.sha256(f"{known.get('name', '')}:{uuid.uuid4()}".encode()).hexdigest()[:16]
        known["criterion_definition_hash"] = compute_criterion_definition_hash(
            known.get("evaluator"), known.get("evaluator_params", {})
        )
        return cls(**known)

    @classmethod
    def from_persisted_dict(cls, d: dict[str, Any]) -> "SuccessCriterion":
        """Create from canonical persistence. Preserves stored criterion_id
        for new-schema criteria. Legacy criteria without identity fields
        load with empty ID — deterministic, stable across repeated loads,
        ineligible for automatic evaluation.

        Partially bound criteria (ID without hash, hash without ID) fail
        closed. Duplicate IDs within a contract must be caught by the
        caller (VillageContract.from_dict).
        """
        known: dict[str, Any] = {f: d[f] for f in cls.__dataclass_fields__ if f in d}
        stored_id = known.get("criterion_id", "")
        stored_hash = known.get("criterion_definition_hash", "")
        has_id = bool(stored_id and isinstance(stored_id, str))
        has_hash = bool(stored_hash and isinstance(stored_hash, str))

        # Partially bound → fail closed
        if has_id != has_hash:
            raise ValueError(f"partially bound criterion identity: id={bool(has_id)} hash={bool(has_hash)}")
        if has_id and (len(stored_id) < 8 or not all(c in "0123456789abcdef" for c in stored_id)):
            raise ValueError(f"malformed criterion_id: {stored_id!r}")
        if has_hash and len(stored_hash) != 64:
            raise ValueError(f"malformed criterion_definition_hash length: {len(stored_hash)}")

        # Parse evaluator
        evaluator_raw = known.get("evaluator")
        if evaluator_raw is None or evaluator_raw == "":
            known["evaluator"] = None
            known["evaluator_params"] = {}
        elif isinstance(evaluator_raw, str):
            try:
                known["evaluator"] = EvaluatorType(evaluator_raw)
            except ValueError:
                known["evaluator"] = None
                known["evaluator_params"] = {}
        params = known.get("evaluator_params", {})
        if not isinstance(params, dict):
            params = {}
        known["evaluator_params"] = params

        # Legacy: no ID → keep empty, never mint during read
        if not has_id:
            known["criterion_id"] = ""
        else:
            # New-schema with evaluator: validate config via shared validator
            # even when the stored hash matches (defense in depth)
            if known.get("evaluator") is not None:
                validate_evaluator_config(known["evaluator"], params)

        computed_hash = compute_criterion_definition_hash(known.get("evaluator"), params)
        if has_hash and stored_hash != computed_hash:
            raise ValueError(
                f"criterion_definition_hash mismatch: " f"stored={stored_hash[:16]}... computed={computed_hash[:16]}..."
            )
        known["criterion_definition_hash"] = computed_hash if has_id else ""
        return cls(**known)

    # Backward-compatible alias used by legacy callers and tests
    from_dict = from_untrusted_terms


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
    auto_review_enabled: bool = False
    state: ContractState = ContractState.DRAFTED
    termination_reason: str | None = None
    created_at: datetime = field(default_factory=_now)
    extra: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.deadline = normalize_datetime(self.deadline)
        self.created_at = normalize_datetime(self.created_at) or _now()
        # Enforce unique non-empty criterion IDs
        seen_ids: set[str] = set()
        for c in self.success_criteria:
            if c.criterion_id:
                if c.criterion_id in seen_ids:
                    raise ValueError(f"duplicate criterion_id in contract: {c.criterion_id!r}")
                seen_ids.add(c.criterion_id)

    # ── Resource whitelist ────────────────────────────────────────
    def is_resource_permitted(self, resource: str) -> bool:
        return resource in self.allowed_resources

    # ── Temporal ──────────────────────────────────────────────────
    def is_past_deadline(self, now: datetime | None = None) -> bool:
        if self.deadline is None:
            return False
        current = _now() if now is None else normalize_datetime(now) or _now()
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
            "auto_review_enabled": self.auto_review_enabled,
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
            "auto_review_enabled",
            "state",
            "termination_reason",
            "created_at",
        }
        extra = {k: v for k, v in d.items() if k not in known_fields}

        deadline = d.get("deadline")
        deadline_dt = datetime.fromisoformat(deadline) if deadline else None
        created_at = d.get("created_at")
        created_at_dt = datetime.fromisoformat(created_at) if created_at else _now()

        auto_review_raw = d.get("auto_review_enabled")
        if auto_review_raw is None:
            auto_review = False
        elif isinstance(auto_review_raw, bool):
            auto_review = auto_review_raw
        else:
            raise ValueError(f"auto_review_enabled must be bool, got {type(auto_review_raw).__name__}")

        criteria = [SuccessCriterion.from_persisted_dict(c) for c in d.get("success_criteria", [])]
        # Enforce unique criterion IDs within the contract
        seen_ids: set[str] = set()
        for c in criteria:
            if c.criterion_id:
                if c.criterion_id in seen_ids:
                    raise ValueError(f"duplicate criterion_id in contract: {c.criterion_id!r}")
                seen_ids.add(c.criterion_id)

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
            success_criteria=criteria,
            auto_review_enabled=auto_review,
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
