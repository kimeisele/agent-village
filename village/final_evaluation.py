"""Immutable FinalEvaluation artifact and pure decision aggregation.

Side-effect-free: no file I/O, no network I/O, no state mutation.
Never imports heartbeat, GitHub, review, or terminal-mutation modules.
Never calls bounty_review(), contract.fulfill(), or _attach_review().
"""

from __future__ import annotations

import hashlib
import time
from dataclasses import dataclass
from enum import Enum
from typing import Any

from village.contracts import (
    SuccessCriterion,
    VillageContract,
    canonical_json_dumps,
    compute_review_policy_hash,
)
from village.evaluator import EvalResult, evaluate_criterion

EVALUATOR_VERSION = "02c-1"


class ReviewDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class CriterionEvaluation:
    """Immutable result of evaluating one criterion against submitted output."""

    criterion_id: str
    criterion_definition_hash: str
    result: EvalResult
    reason_code: str


@dataclass(frozen=True)
class FinalEvaluation:
    """Immutable, self-hashed evaluation artifact for one submission.

    Produced by build_final_evaluation(). Carries all fields needed
    for a later review authority to apply the decision without
    trusting a caller-mutated contract or re-evaluating.
    """

    submission_id: str
    bounty_id: str
    contract_id: str
    contract_version: str
    work_result_id: str
    execution_id: str
    output_canonical_hash: str
    review_policy_hash: str
    criteria_results: tuple[CriterionEvaluation, ...]
    overall_decision: ReviewDecision
    reason_codes: tuple[str, ...]
    evaluator_version: str
    evaluated_at: float
    evaluation_hash: str = ""

    def __post_init__(self) -> None:
        if not self.evaluation_hash:
            h = compute_evaluation_hash(self)
            object.__setattr__(self, "evaluation_hash", h)

    def to_dict(self) -> dict[str, Any]:
        return {
            "submission_id": self.submission_id,
            "bounty_id": self.bounty_id,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "work_result_id": self.work_result_id,
            "execution_id": self.execution_id,
            "output_canonical_hash": self.output_canonical_hash,
            "review_policy_hash": self.review_policy_hash,
            "criteria_results": [
                {
                    "criterion_id": c.criterion_id,
                    "criterion_definition_hash": c.criterion_definition_hash,
                    "result": c.result.value,
                    "reason_code": c.reason_code,
                }
                for c in self.criteria_results
            ],
            "overall_decision": self.overall_decision.value,
            "reason_codes": list(self.reason_codes),
            "evaluator_version": self.evaluator_version,
            "evaluated_at": self.evaluated_at,
            "evaluation_hash": self.evaluation_hash,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "FinalEvaluation":
        results = tuple(
            CriterionEvaluation(
                criterion_id=c["criterion_id"],
                criterion_definition_hash=c["criterion_definition_hash"],
                result=EvalResult(c["result"]),
                reason_code=c["reason_code"],
            )
            for c in d["criteria_results"]
        )
        return cls(
            submission_id=d["submission_id"],
            bounty_id=d["bounty_id"],
            contract_id=d["contract_id"],
            contract_version=d["contract_version"],
            work_result_id=d["work_result_id"],
            execution_id=d["execution_id"],
            output_canonical_hash=d["output_canonical_hash"],
            review_policy_hash=d["review_policy_hash"],
            criteria_results=results,
            overall_decision=ReviewDecision(d["overall_decision"]),
            reason_codes=tuple(d["reason_codes"]),
            evaluator_version=d["evaluator_version"],
            evaluated_at=d["evaluated_at"],
            evaluation_hash=d.get("evaluation_hash", ""),
        )


def compute_evaluation_hash(evaluation: FinalEvaluation) -> str:
    """Canonical hash over every authoritative field except evaluation_hash."""
    projection: dict[str, object] = {
        "submission_id": evaluation.submission_id,
        "bounty_id": evaluation.bounty_id,
        "contract_id": evaluation.contract_id,
        "contract_version": evaluation.contract_version,
        "work_result_id": evaluation.work_result_id,
        "execution_id": evaluation.execution_id,
        "output_canonical_hash": evaluation.output_canonical_hash,
        "review_policy_hash": evaluation.review_policy_hash,
        "criteria_results": [
            {
                "criterion_id": c.criterion_id,
                "criterion_definition_hash": c.criterion_definition_hash,
                "result": c.result.value,
                "reason_code": c.reason_code,
            }
            for c in evaluation.criteria_results
        ],
        "overall_decision": evaluation.overall_decision.value,
        "reason_codes": list(evaluation.reason_codes),
        "evaluator_version": evaluation.evaluator_version,
        "evaluated_at": evaluation.evaluated_at,
    }
    return hashlib.sha256(canonical_json_dumps(projection).encode()).hexdigest()


def verify_evaluation_hash(evaluation: FinalEvaluation) -> bool:
    """Recompute and compare the evaluation hash. Returns True if valid."""
    return compute_evaluation_hash(evaluation) == evaluation.evaluation_hash


def _criterion_reason_code(criterion: SuccessCriterion, result: EvalResult) -> str:
    """Build a bounded, deterministic reason code for one criterion evaluation."""
    if not criterion.criterion_id or not criterion.criterion_definition_hash:
        return "criterion:legacy_unbound"
    if criterion.evaluator is None:
        return f"criterion:{criterion.criterion_id[:8]}:human_only"
    ev_type = criterion.evaluator.value
    field = str(criterion.evaluator_params.get("field", "?"))[:64]
    if result == EvalResult.PASS:
        return f"{ev_type}:{field}:pass"
    elif result == EvalResult.FAIL:
        return f"{ev_type}:{field}:fail"
    else:
        return f"{ev_type}:{field}:indeterminate"


def build_final_evaluation(
    submission: dict[str, Any],
    contract: VillageContract,
    *,
    evaluated_at: float = 0.0,
) -> FinalEvaluation:
    """Pure, non-authoritative aggregation of per-criterion evaluations.

    1. Validates submission bindings (reuses validate_submission_bindings).
    2. Evaluates every criterion via evaluate_criterion().
    3. Applies the required-criterion decision policy.
    4. Returns an immutable, self-hashed FinalEvaluation.

    Never mutates state. Never performs I/O. Never calls terminal authority.
    """
    from village.bounty_review import validate_submission_bindings

    if evaluated_at == 0.0:
        evaluated_at = time.time()

    reason_codes: list[str] = []

    # Validate bindings first — any failure → INDETERMINATE
    binding_errors = validate_submission_bindings(submission, contract)
    if binding_errors:
        return FinalEvaluation(
            submission_id=str(submission.get("submission_id", "")),
            bounty_id=str(submission.get("bounty_id", "")),
            contract_id=contract.contract_id,
            contract_version=contract.version,
            work_result_id=str(submission.get("work_result_id", "")),
            execution_id=str(submission.get("execution_id", "")),
            output_canonical_hash=str(submission.get("output_canonical_hash", "")),
            review_policy_hash=compute_review_policy_hash(contract),
            criteria_results=(),
            overall_decision=ReviewDecision.INDETERMINATE,
            reason_codes=tuple(f"binding:{e}" for e in binding_errors),
            evaluator_version=EVALUATOR_VERSION,
            evaluated_at=evaluated_at,
        )

    # Check auto-review policy
    if not contract.auto_review_enabled:
        return FinalEvaluation(
            submission_id=submission["submission_id"],
            bounty_id=submission["bounty_id"],
            contract_id=contract.contract_id,
            contract_version=contract.version,
            work_result_id=submission["work_result_id"],
            execution_id=submission["execution_id"],
            output_canonical_hash=submission["output_canonical_hash"],
            review_policy_hash=compute_review_policy_hash(contract),
            criteria_results=(),
            overall_decision=ReviewDecision.INDETERMINATE,
            reason_codes=("policy:auto_review_disabled",),
            evaluator_version=EVALUATOR_VERSION,
            evaluated_at=evaluated_at,
        )

    # Evaluate every criterion
    has_required_machine = False
    results: list[CriterionEvaluation] = []
    output = submission.get("output")
    if not isinstance(output, dict):
        output = {}

    for criterion in contract.success_criteria:
        result = evaluate_criterion(criterion, output)
        reason = _criterion_reason_code(criterion, result)
        results.append(
            CriterionEvaluation(
                criterion_id=criterion.criterion_id,
                criterion_definition_hash=criterion.criterion_definition_hash,
                result=result,
                reason_code=reason,
            )
        )
        if result != EvalResult.PASS:
            reason_codes.append(reason)
        if criterion.required and criterion.evaluator is not None:
            has_required_machine = True

    # Decision policy
    if not has_required_machine:
        decision = ReviewDecision.INDETERMINATE
        if not any(r == "policy:auto_review_disabled" for r in reason_codes):
            reason_codes.append("policy:no_required_machine_criterion")
    else:
        has_indeterminate = any(
            c.result == EvalResult.INDETERMINATE for c in results if _criterion_is_required(contract, c.criterion_id)
        )
        has_fail = any(c.result == EvalResult.FAIL for c in results if _criterion_is_required(contract, c.criterion_id))
        if has_indeterminate:
            decision = ReviewDecision.INDETERMINATE
        elif has_fail:
            decision = ReviewDecision.REJECT
        else:
            decision = ReviewDecision.ACCEPT

    return FinalEvaluation(
        submission_id=submission["submission_id"],
        bounty_id=submission["bounty_id"],
        contract_id=contract.contract_id,
        contract_version=contract.version,
        work_result_id=submission["work_result_id"],
        execution_id=submission["execution_id"],
        output_canonical_hash=submission["output_canonical_hash"],
        review_policy_hash=compute_review_policy_hash(contract),
        criteria_results=tuple(results),
        overall_decision=decision,
        reason_codes=tuple(reason_codes),
        evaluator_version=EVALUATOR_VERSION,
        evaluated_at=evaluated_at,
    )


def _criterion_is_required(contract: VillageContract, criterion_id: str) -> bool:
    for c in contract.success_criteria:
        if c.criterion_id == criterion_id:
            return c.required
    return False
