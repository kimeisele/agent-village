"""Tests for the immutable FinalEvaluation and pure decision aggregation."""

from __future__ import annotations

import hashlib

import pytest

from village.contracts import (
    EvaluatorType,
    SuccessCriterion,
    VillageContract,
    canonical_json_dumps,
)
from village.evaluator import EvalResult
from village.final_evaluation import (
    EVALUATOR_VERSION,
    FinalEvaluation,
    ReviewDecision,
    _validate_reason_code_syntax,
    build_final_evaluation,
    validate_final_evaluation,
    verify_evaluation_hash,
)


def _make_contract(**kw):
    defaults = {"contract_id": "c:1", "auto_review_enabled": True}
    defaults.update(kw)
    return VillageContract(**defaults)


def _make_submission(contract, output=None):
    from village.contracts import compute_review_policy_hash

    out = output if output is not None else {}
    return {
        "submission_id": "s:1",
        "bounty_id": "b:1",
        "contract_id": contract.contract_id,
        "contract_version": contract.version,
        "work_result_id": "w:1",
        "execution_id": "e:1",
        "output_canonical_hash": hashlib.sha256(canonical_json_dumps(out).encode()).hexdigest(),
        "review_policy_hash": compute_review_policy_hash(contract),
        "criterion_ids": [c.criterion_id for c in contract.success_criteria],
        "criterion_definition_hashes": [c.criterion_definition_hash for c in contract.success_criteria],
        "output": out,
    }


# ── Decision policy ──────────────────────────────────────────


class TestDecisionPolicy:
    def test_accept_when_all_required_pass(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"gaps": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert fe.overall_decision == ReviewDecision.ACCEPT

    def test_reject_when_required_fails(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"gaps": None})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert fe.overall_decision == ReviewDecision.REJECT

    def test_indeterminate_when_required_indeterminate(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert fe.overall_decision == ReviewDecision.INDETERMINATE

    def test_indeterminate_precedence_over_fail(self):
        a = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        b = SuccessCriterion.create(
            name="b", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "y"}
        )
        contract = _make_contract(success_criteria=[a, b])
        sub = _make_submission(contract, output={"x": None})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        # x=FAIL (None), y=INDETERMINATE (missing) => INDETERMINATE outranks FAIL
        assert fe.overall_decision == ReviewDecision.INDETERMINATE

    def test_auto_review_disabled_indeterminate(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c], auto_review_enabled=False)
        sub = _make_submission(contract, output={"gaps": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert fe.overall_decision == ReviewDecision.INDETERMINATE
        assert "auto_review_disabled" in str(fe.reason_codes)

    def test_no_criteria_indeterminate(self):
        contract = _make_contract()
        sub = _make_submission(contract)
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert fe.overall_decision == ReviewDecision.INDETERMINATE

    def test_required_human_only_indeterminate(self):
        c = SuccessCriterion.create(name="a", required=True, evaluator=None)
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract)
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert fe.overall_decision == ReviewDecision.INDETERMINATE


# ── Binding failures ─────────────────────────────────────────


class TestBindingFailures:
    def test_binding_failure_indeterminate(self):
        contract = _make_contract()
        sub = {"submission_id": "s:1"}
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert fe.overall_decision == ReviewDecision.INDETERMINATE
        assert any("binding:" in r for r in fe.reason_codes)


# ── Artifact integrity ───────────────────────────────────────


class TestArtifactIntegrity:
    def test_every_criterion_represented_once(self):
        a = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        b = SuccessCriterion.create(
            name="b", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "y"}
        )
        contract = _make_contract(success_criteria=[a, b])
        sub = _make_submission(contract, output={"x": [1], "y": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert len(fe.criteria_results) == 2
        ids = [c.criterion_id for c in fe.criteria_results]
        assert ids == [a.criterion_id, b.criterion_id]

    def test_optional_criteria_included(self):
        a = SuccessCriterion.create(
            name="req", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        b = SuccessCriterion.create(
            name="opt", required=False, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "y"}
        )
        contract = _make_contract(success_criteria=[a, b])
        sub = _make_submission(contract, output={"x": [1], "y": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert len(fe.criteria_results) == 2

    def test_deterministic_hash_stable(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"gaps": [1]})
        fe1 = build_final_evaluation(sub, contract, evaluated_at=1.0)
        fe2 = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert fe1.evaluation_hash == fe2.evaluation_hash

    def test_hash_changes_when_decision_changes(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        fe_pass = build_final_evaluation(_make_submission(contract, output={"gaps": [1]}), contract, evaluated_at=1.0)
        fe_fail = build_final_evaluation(_make_submission(contract, output={"gaps": None}), contract, evaluated_at=1.0)
        assert fe_pass.evaluation_hash != fe_fail.evaluation_hash

    def test_tampered_hash_rejected(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"gaps": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert verify_evaluation_hash(fe)
        d = fe.to_dict()
        d["evaluation_hash"] = "0" * 64
        with pytest.raises(ValueError, match="evaluation_hash mismatch"):
            FinalEvaluation.from_persisted_dict(d)

    def test_canonical_roundtrip_preserves_hash(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"gaps": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        restored = FinalEvaluation.from_persisted_dict(fe.to_dict())
        assert restored.evaluation_hash == fe.evaluation_hash


# ── Purity and authority ─────────────────────────────────────


class TestPurity:
    def test_input_submission_unchanged(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"gaps": [1]})
        original = dict(sub)
        build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert sub == original

    def test_input_contract_unchanged(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"gaps": [1]})
        original_state = contract.state
        build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert contract.state == original_state

    def test_criterion_met_unchanged(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"gaps": [1]})
        build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert contract.success_criteria[0].met is None

    def test_no_heartbeat_import(self):
        import ast

        with open("village/final_evaluation.py") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "heartbeat" in node.module:
                    pytest.fail("final_evaluation imports heartbeat")

    def test_no_bounty_review_call(self):
        import ast

        with open("village/final_evaluation.py") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in ("bounty_review", "contract.fulfill"):
                    pytest.fail("final_evaluation calls terminal authority")


# ── AST import boundaries ────────────────────────────────────


class TestImportBoundaries:
    def test_final_evaluation_does_not_import_bounty_review(self):
        import ast

        with open("village/final_evaluation.py") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and "bounty_review" in node.module:
                    pytest.fail("final_evaluation imports bounty_review")

    def test_submission_bindings_does_not_import_terminal(self):
        import ast

        with open("village/submission_bindings.py") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and any(t in (node.module or "") for t in ("heartbeat", "bounty_review")):
                    pytest.fail("submission_bindings imports terminal module")


# ── Complete criterion coverage ──────────────────────────────


class TestCompleteCoverage:
    def test_binding_failure_has_criterion_results(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = {"submission_id": "s:1"}
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert len(fe.criteria_results) == 1
        assert fe.criteria_results[0].result == EvalResult.INDETERMINATE

    def test_auto_review_disabled_has_criterion_results(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        contract = _make_contract(success_criteria=[c], auto_review_enabled=False)
        sub = _make_submission(contract, output={"x": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert len(fe.criteria_results) == 1
        assert "auto_review_disabled" in fe.criteria_results[0].reason_code


# ── Persisted loading fails closed ──────────────────────────


class TestPersistedLoading:
    def test_missing_hash_rejected(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"x": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        d = fe.to_dict()
        del d["evaluation_hash"]
        with pytest.raises(ValueError, match="evaluation_hash"):
            FinalEvaluation.from_persisted_dict(d)

    def test_malformed_enum_rejected(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"x": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        d = fe.to_dict()
        d["criteria_results"][0]["result"] = "INVALID"
        with pytest.raises(ValueError, match="EvalResult"):
            FinalEvaluation.from_persisted_dict(d)

    def test_nan_evaluated_at_rejected(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"x": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        d = fe.to_dict()
        d["evaluated_at"] = float("nan")
        with pytest.raises(ValueError, match="finite"):
            FinalEvaluation.from_persisted_dict(d)


# ── Round-trip with reason codes ─────────────────────────────


class TestRoundTrip:
    def test_indeterminate_roundtrip_preserves_reason_codes(self):
        contract = _make_contract(auto_review_enabled=False)
        sub = _make_submission(contract)
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        d = fe.to_dict()
        assert len(d["reason_codes"]) > 0
        restored = FinalEvaluation.from_persisted_dict(d)
        assert restored.reason_codes == fe.reason_codes
        assert restored.evaluation_hash == fe.evaluation_hash

    def test_reject_roundtrip(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"gaps": None})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert fe.overall_decision == ReviewDecision.REJECT
        restored = FinalEvaluation.from_persisted_dict(fe.to_dict())
        assert restored.overall_decision == fe.overall_decision
        assert restored.evaluation_hash == fe.evaluation_hash

    def test_multiple_reason_codes_preserved(self):
        a = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        b = SuccessCriterion.create(
            name="b", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "y"}
        )
        contract = _make_contract(success_criteria=[a, b])
        sub = _make_submission(contract, output={})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        restored = FinalEvaluation.from_persisted_dict(fe.to_dict())
        assert list(restored.reason_codes) == list(fe.reason_codes)
        assert restored.evaluation_hash == fe.evaluation_hash


# ── Binding validation completeness ──────────────────────────


class TestBindingValidationComplete:
    def test_work_result_id_mismatch(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"x": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        sub2 = dict(sub, work_result_id="wrong")
        reasons = validate_final_evaluation(fe, sub2, contract)
        assert any("work_result_id" in r for r in reasons)

    def test_execution_id_mismatch(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"x": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        sub2 = dict(sub, execution_id="wrong")
        reasons = validate_final_evaluation(fe, sub2, contract)
        assert any("execution_id" in r for r in reasons)

    def test_output_hash_mismatch(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"x": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        sub2 = dict(sub, output_canonical_hash="0" * 64)
        reasons = validate_final_evaluation(fe, sub2, contract)
        assert any("output_canonical_hash" in r for r in reasons)

    def test_submission_policy_hash_mismatch(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"x": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        sub2 = dict(sub, review_policy_hash="0" * 64)
        reasons = validate_final_evaluation(fe, sub2, contract)
        assert any("review_policy_hash" in r for r in reasons)

    def test_validator_never_raises_on_malformed_submission(self):
        c = SuccessCriterion.create(
            name="a", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "x"}
        )
        contract = _make_contract(success_criteria=[c])
        sub = _make_submission(contract, output={"x": [1]})
        fe = build_final_evaluation(sub, contract, evaluated_at=1.0)
        reasons = validate_final_evaluation(fe, {"bad": "data"}, contract)
        assert len(reasons) > 0


# ── Reason code syntax ───────────────────────────────────────


class TestReasonCodeSyntax:
    def test_empty_code_rejected(self):
        with pytest.raises(ValueError, match="non-empty"):
            _validate_reason_code_syntax("")

    def test_newline_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            _validate_reason_code_syntax("code\nbad")

    def test_control_char_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            _validate_reason_code_syntax("code\x01bad")

    def test_valid_code_accepted(self):
        _validate_reason_code_syntax("field_present:gaps:pass")
        _validate_reason_code_syntax("criterion:legacy_unbound")
        _validate_reason_code_syntax("policy:auto_review_disabled")
        _validate_reason_code_syntax("binding:output_hash_mismatch")


# ── Trusted creation validation ─────────────────────────────


class TestTrustedCreationValidation:
    def test_nan_rejected(self):
        with pytest.raises(ValueError, match="finite"):
            FinalEvaluation.create(
                submission_id="s",
                bounty_id="b",
                contract_id="c",
                contract_version="1.0",
                work_result_id="w",
                execution_id="e",
                output_canonical_hash="o" * 64,
                review_policy_hash="r" * 64,
                criteria_results=(),
                overall_decision=ReviewDecision.INDETERMINATE,
                reason_codes=("test:code",),
                evaluator_version=EVALUATOR_VERSION,
                evaluated_at=float("nan"),
            )

    def test_malformed_reason_code_rejected(self):
        with pytest.raises(ValueError, match="invalid"):
            FinalEvaluation.create(
                submission_id="s",
                bounty_id="b",
                contract_id="c",
                contract_version="1.0",
                work_result_id="w",
                execution_id="e",
                output_canonical_hash="o" * 64,
                review_policy_hash="r" * 64,
                criteria_results=(),
                overall_decision=ReviewDecision.INDETERMINATE,
                reason_codes=("bad code",),
                evaluator_version=EVALUATOR_VERSION,
                evaluated_at=1.0,
            )
