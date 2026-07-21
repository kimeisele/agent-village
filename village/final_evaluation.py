"""Immutable FinalEvaluation artifact and pure decision aggregation.

Side-effect-free: no file I/O, no network I/O, no state mutation.
Never imports bounty_review, heartbeat, GitHub, or terminal-mutation modules.
Never calls bounty_review(), contract.fulfill(), or _attach_review().
"""

from __future__ import annotations

import hashlib
import math
import re as _re
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
from village.submission_bindings import validate_submission_bindings

EVALUATOR_VERSION = "02c-1"
MAX_REASON_CODE_LEN = 128
_REASON_CODE_RE = _re.compile(r"^[a-zA-Z0-9_:.=-]+$")


def _validate_reason_code_syntax(code: str) -> None:
    """Validate reason-code syntax. Raises ValueError on violation."""
    if not code:
        raise ValueError("reason_code must be non-empty")
    if len(code) > MAX_REASON_CODE_LEN:
        raise ValueError(f"reason_code too long ({len(code)} > {MAX_REASON_CODE_LEN})")
    if not _REASON_CODE_RE.match(code):
        raise ValueError(f"reason_code contains invalid characters: {code[:64]!r}")


def _validate_finite_float(value: float) -> None:
    if not isinstance(value, (int, float)) or math.isnan(value) or math.isinf(value):
        raise ValueError("evaluated_at must be a finite number")


class ReviewDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    INDETERMINATE = "indeterminate"


@dataclass(frozen=True)
class CriterionEvaluation:
    criterion_id: str
    criterion_definition_hash: str
    result: EvalResult
    reason_code: str


@dataclass(frozen=True)
class FinalEvaluation:
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

    @classmethod
    def create(
        cls,
        submission_id: str,
        bounty_id: str,
        contract_id: str,
        contract_version: str,
        work_result_id: str,
        execution_id: str,
        output_canonical_hash: str,
        review_policy_hash: str,
        criteria_results: tuple[CriterionEvaluation, ...],
        overall_decision: ReviewDecision,
        reason_codes: tuple[str, ...],
        evaluator_version: str,
        evaluated_at: float,
    ) -> "FinalEvaluation":
        """Trusted creation — computes and assigns the evaluation hash."""
        # Trusted creation must reject what the loader/validator would reject
        _validate_finite_float(evaluated_at)
        for rc in reason_codes:
            _validate_reason_code_syntax(rc)
        for cr in criteria_results:
            _validate_reason_code_syntax(cr.reason_code)
        if evaluator_version != EVALUATOR_VERSION:
            raise ValueError(f"unsupported evaluator_version: {evaluator_version}")

        inst = cls(
            submission_id=submission_id,
            bounty_id=bounty_id,
            contract_id=contract_id,
            contract_version=contract_version,
            work_result_id=work_result_id,
            execution_id=execution_id,
            output_canonical_hash=output_canonical_hash,
            review_policy_hash=review_policy_hash,
            criteria_results=criteria_results,
            overall_decision=overall_decision,
            reason_codes=reason_codes,
            evaluator_version=evaluator_version,
            evaluated_at=evaluated_at,
            evaluation_hash="",
        )
        h = compute_evaluation_hash(inst)
        object.__setattr__(inst, "evaluation_hash", h)
        return inst

    @classmethod
    def from_persisted_dict(cls, d: dict[str, Any]) -> "FinalEvaluation":
        """Load from persisted data. Fails closed on any structural or hash
        mismatch. Never trusts a supplied hash without recomputation."""
        # Validate field types
        for f in (
            "submission_id",
            "bounty_id",
            "contract_id",
            "contract_version",
            "work_result_id",
            "execution_id",
            "output_canonical_hash",
            "review_policy_hash",
            "evaluator_version",
        ):
            if not isinstance(d.get(f), str):
                raise ValueError(f"FinalEvaluation: {f} must be a string")
        if not isinstance(d.get("evaluated_at"), (int, float)):
            raise ValueError("FinalEvaluation: evaluated_at must be a number")
        if math.isnan(d["evaluated_at"]) or math.isinf(d["evaluated_at"]):
            raise ValueError("FinalEvaluation: evaluated_at must be finite")
        overall = d.get("overall_decision")
        try:
            decision = ReviewDecision(overall)
        except (ValueError, TypeError):
            raise ValueError(f"FinalEvaluation: invalid overall_decision {overall!r}")
        supplied_hash = d.get("evaluation_hash", "")
        if not isinstance(supplied_hash, str) or not supplied_hash:
            raise ValueError("FinalEvaluation: missing or invalid evaluation_hash")

        rc = d.get("reason_codes", [])
        if not isinstance(rc, list):
            raise ValueError("FinalEvaluation: reason_codes must be a list")
        reason_codes: list[str] = []
        for r in rc:
            if not isinstance(r, str) or len(r) > MAX_REASON_CODE_LEN:
                raise ValueError("FinalEvaluation: reason_code too long or non-string")
            _validate_reason_code_syntax(r)
            reason_codes.append(r)

        cr_list = d.get("criteria_results", [])
        if not isinstance(cr_list, list):
            raise ValueError("FinalEvaluation: criteria_results must be a list")
        criteria_results: list[CriterionEvaluation] = []
        seen_ids: set[str] = set()
        for cr in cr_list:
            if not isinstance(cr, dict):
                raise ValueError("FinalEvaluation: criterion result must be a dict")
            cid = cr.get("criterion_id", "")
            if not isinstance(cid, str) or not cid:
                raise ValueError("FinalEvaluation: invalid criterion_id")
            if cid in seen_ids:
                raise ValueError(f"FinalEvaluation: duplicate criterion_id {cid}")
            seen_ids.add(cid)
            ch = cr.get("criterion_definition_hash", "")
            if not isinstance(ch, str) or len(ch) != 64:
                raise ValueError("FinalEvaluation: invalid criterion_definition_hash")
            try:
                result = EvalResult(cr.get("result"))
            except (ValueError, TypeError):
                raise ValueError("FinalEvaluation: invalid EvalResult")
            rcode = cr.get("reason_code", "")
            if not isinstance(rcode, str) or len(rcode) > MAX_REASON_CODE_LEN:
                raise ValueError("FinalEvaluation: reason_code too long or non-string")
            _validate_reason_code_syntax(rcode)
            criteria_results.append(
                CriterionEvaluation(
                    criterion_id=cid,
                    criterion_definition_hash=ch,
                    result=result,
                    reason_code=rcode,
                )
            )

        inst = cls(
            submission_id=d["submission_id"],
            bounty_id=d["bounty_id"],
            contract_id=d["contract_id"],
            contract_version=d["contract_version"],
            work_result_id=d["work_result_id"],
            execution_id=d["execution_id"],
            output_canonical_hash=d["output_canonical_hash"],
            review_policy_hash=d["review_policy_hash"],
            criteria_results=tuple(criteria_results),
            overall_decision=decision,
            reason_codes=tuple(reason_codes),
            evaluator_version=d["evaluator_version"],
            evaluated_at=d["evaluated_at"],
            evaluation_hash=supplied_hash,
        )
        recomputed = compute_evaluation_hash(inst)
        if recomputed != supplied_hash:
            raise ValueError(
                f"FinalEvaluation: evaluation_hash mismatch "
                f"(supplied={supplied_hash[:16]}..., computed={recomputed[:16]}...)"
            )
        return inst

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


def compute_evaluation_hash(evaluation: FinalEvaluation) -> str:
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
    return compute_evaluation_hash(evaluation) == evaluation.evaluation_hash


def _criterion_reason_code(criterion: SuccessCriterion, result: EvalResult) -> str:
    """Build a bounded, deterministic reason code."""
    if not criterion.criterion_id or not criterion.criterion_definition_hash:
        return "criterion:legacy_unbound"
    if criterion.evaluator is None:
        return f"criterion:{criterion.criterion_id[:8]}:human_only"
    ev_type = criterion.evaluator.value
    try:
        from village.contracts import validate_evaluator_config

        validate_evaluator_config(criterion.evaluator, criterion.evaluator_params)
        field = str(criterion.evaluator_params.get("field", "?"))[:64]
    except ValueError:
        return "criterion:invalid_configuration"
    if result == EvalResult.PASS:
        return f"{ev_type}:{field}:pass"
    elif result == EvalResult.FAIL:
        return f"{ev_type}:{field}:fail"
    else:
        return f"{ev_type}:{field}:indeterminate"


def _indeterminate_criterion_result(criterion: SuccessCriterion, reason: str) -> CriterionEvaluation:
    """Create an INDETERMINATE result for a criterion that cannot be evaluated."""
    return CriterionEvaluation(
        criterion_id=criterion.criterion_id or "",
        criterion_definition_hash=criterion.criterion_definition_hash or "",
        result=EvalResult.INDETERMINATE,
        reason_code=reason[:MAX_REASON_CODE_LEN],
    )


def _criterion_is_required(contract: VillageContract, criterion_id: str) -> bool:
    for c in contract.success_criteria:
        if c.criterion_id == criterion_id:
            return c.required
    return False


def build_final_evaluation(
    submission: dict[str, Any],
    contract: VillageContract,
    *,
    evaluated_at: float = 0.0,
) -> FinalEvaluation:
    """Pure, non-authoritative aggregation of per-criterion evaluations."""
    if evaluated_at == 0.0:
        evaluated_at = time.time()

    reason_codes: list[str] = []
    criteria = list(contract.success_criteria)

    # Validate bindings
    binding_errors = validate_submission_bindings(submission, contract)
    if binding_errors:
        # Complete criterion coverage even on binding failure
        results = tuple(_indeterminate_criterion_result(c, "criterion:evaluation_not_authorized") for c in criteria)
        return FinalEvaluation.create(
            submission_id=str(submission.get("submission_id", "")),
            bounty_id=str(submission.get("bounty_id", "")),
            contract_id=contract.contract_id,
            contract_version=contract.version,
            work_result_id=str(submission.get("work_result_id", "")),
            execution_id=str(submission.get("execution_id", "")),
            output_canonical_hash=str(submission.get("output_canonical_hash", "")),
            review_policy_hash=compute_review_policy_hash(contract),
            criteria_results=results,
            overall_decision=ReviewDecision.INDETERMINATE,
            reason_codes=tuple(f"binding:{e}" for e in binding_errors),
            evaluator_version=EVALUATOR_VERSION,
            evaluated_at=evaluated_at,
        )

    # Check auto-review policy
    if not contract.auto_review_enabled:
        results = tuple(_indeterminate_criterion_result(c, "policy:auto_review_disabled") for c in criteria)
        return FinalEvaluation.create(
            submission_id=submission["submission_id"],
            bounty_id=submission["bounty_id"],
            contract_id=contract.contract_id,
            contract_version=contract.version,
            work_result_id=submission["work_result_id"],
            execution_id=submission["execution_id"],
            output_canonical_hash=submission["output_canonical_hash"],
            review_policy_hash=compute_review_policy_hash(contract),
            criteria_results=results,
            overall_decision=ReviewDecision.INDETERMINATE,
            reason_codes=("policy:auto_review_disabled",),
            evaluator_version=EVALUATOR_VERSION,
            evaluated_at=evaluated_at,
        )

    # Evaluate every criterion
    has_required_machine = False
    eval_results: list[CriterionEvaluation] = []
    output = submission.get("output")
    if not isinstance(output, dict):
        output = {}

    for criterion in criteria:
        result = evaluate_criterion(criterion, output)
        reason = _criterion_reason_code(criterion, result)
        eval_results.append(
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
        reason_codes.append("policy:no_required_machine_criterion")
    else:
        has_indeterminate = any(
            c.result == EvalResult.INDETERMINATE
            for c in eval_results
            if _criterion_is_required(contract, c.criterion_id)
        )
        has_fail = any(
            c.result == EvalResult.FAIL for c in eval_results if _criterion_is_required(contract, c.criterion_id)
        )
        if has_indeterminate:
            decision = ReviewDecision.INDETERMINATE
        elif has_fail:
            decision = ReviewDecision.REJECT
        else:
            decision = ReviewDecision.ACCEPT

    return FinalEvaluation.create(
        submission_id=submission["submission_id"],
        bounty_id=submission["bounty_id"],
        contract_id=contract.contract_id,
        contract_version=contract.version,
        work_result_id=submission["work_result_id"],
        execution_id=submission["execution_id"],
        output_canonical_hash=submission["output_canonical_hash"],
        review_policy_hash=compute_review_policy_hash(contract),
        criteria_results=tuple(eval_results),
        overall_decision=decision,
        reason_codes=tuple(reason_codes),
        evaluator_version=EVALUATOR_VERSION,
        evaluated_at=evaluated_at,
    )


def validate_final_evaluation(
    evaluation: FinalEvaluation,
    submission: dict[str, Any],
    contract: VillageContract,
) -> list[str]:
    """Pure structural validator. Returns reason codes, never raises."""
    reasons: list[str] = []
    if not verify_evaluation_hash(evaluation):
        reasons.append("invalid_evaluation_hash")
    if evaluation.submission_id != submission.get("submission_id"):
        reasons.append("submission_id_mismatch")
    if evaluation.bounty_id != submission.get("bounty_id"):
        reasons.append("bounty_id_mismatch")
    if evaluation.contract_id != contract.contract_id:
        reasons.append("contract_id_mismatch")
    if evaluation.contract_version != contract.version:
        reasons.append("contract_version_mismatch")
    if evaluation.work_result_id != submission.get("work_result_id"):
        reasons.append("work_result_id_mismatch")
    if evaluation.execution_id != submission.get("execution_id"):
        reasons.append("execution_id_mismatch")
    if evaluation.output_canonical_hash != submission.get("output_canonical_hash"):
        reasons.append("output_canonical_hash_mismatch")
    if evaluation.review_policy_hash != submission.get("review_policy_hash"):
        reasons.append("review_policy_hash_mismatch")
    try:
        expected_policy = compute_review_policy_hash(contract)
        if evaluation.review_policy_hash != expected_policy:
            reasons.append("review_policy_hash_mismatch_contract")
    except (ValueError, TypeError):
        reasons.append("review_policy_hash_not_canonical")
    if evaluation.evaluator_version != EVALUATOR_VERSION:
        reasons.append(f"unsupported_evaluator_version:{evaluation.evaluator_version[:32]}")

    # Validate reason code syntax in both top-level and criterion results
    for i, rc in enumerate(evaluation.reason_codes):
        try:
            _validate_reason_code_syntax(rc)
        except ValueError:
            reasons.append(f"invalid_reason_code:{i}")
    for i, cr in enumerate(evaluation.criteria_results):
        try:
            _validate_reason_code_syntax(cr.reason_code)
        except ValueError:
            reasons.append(f"invalid_criterion_reason_code:{i}")

    seen: set[str] = set()
    contract_ids = [c.criterion_id for c in contract.success_criteria]
    for i, cr in enumerate(evaluation.criteria_results):
        if cr.criterion_id in seen:
            reasons.append(f"duplicate_criterion_id:{cr.criterion_id[:16]}")
        seen.add(cr.criterion_id)
        if i >= len(contract_ids) or cr.criterion_id != contract_ids[i]:
            reasons.append("criterion_order_or_count_mismatch")
            break
        expected_ch = contract.success_criteria[i].criterion_definition_hash
        if cr.criterion_definition_hash != expected_ch:
            reasons.append(f"criterion_definition_hash_mismatch:{cr.criterion_id[:16]}")

    if len(evaluation.criteria_results) != len(contract.success_criteria):
        reasons.append("criterion_count_mismatch")

    # Decision consistency: recompute and compare
    has_indeterminate = False
    has_fail = False
    has_required_machine = False
    for c in contract.success_criteria:
        if c.required and c.evaluator is not None:
            has_required_machine = True
    for cr in evaluation.criteria_results:
        if _criterion_is_required(contract, cr.criterion_id):
            if cr.result == EvalResult.INDETERMINATE:
                has_indeterminate = True
            elif cr.result == EvalResult.FAIL:
                has_fail = True
    if not contract.auto_review_enabled or not has_required_machine:
        expected = ReviewDecision.INDETERMINATE
    elif has_indeterminate:
        expected = ReviewDecision.INDETERMINATE
    elif has_fail:
        expected = ReviewDecision.REJECT
    else:
        expected = ReviewDecision.ACCEPT
    if evaluation.overall_decision != expected:
        reasons.append(f"decision_inconsistent: got={evaluation.overall_decision.value} expected={expected.value}")

    return reasons
