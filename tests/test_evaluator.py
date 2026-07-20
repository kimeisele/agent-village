"""Tests for the pure deterministic evaluator (village/evaluator.py)."""

from __future__ import annotations

import pytest

from village.contracts import EvaluatorType, SuccessCriterion
from village.evaluator import EvalResult, evaluate_criterion

# ── FIELD_PRESENT ────────────────────────────────────────────


class TestFieldPresent:
    def test_pass_when_key_exists_and_value_not_none(self):
        c = SuccessCriterion(name="test", evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"})
        assert evaluate_criterion(c, {"gaps": []}) == EvalResult.PASS

    def test_fail_when_key_exists_but_value_is_none(self):
        c = SuccessCriterion(name="test", evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"})
        assert evaluate_criterion(c, {"gaps": None}) == EvalResult.FAIL

    def test_indeterminate_when_key_missing(self):
        c = SuccessCriterion(name="test", evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "missing"})
        assert evaluate_criterion(c, {"gaps": []}) == EvalResult.INDETERMINATE

    def test_nested_dict_path(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "result.summary"},
        )
        assert evaluate_criterion(c, {"result": {"summary": "ok"}}) == EvalResult.PASS

    def test_intermediate_not_dict(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "result.summary"},
        )
        assert evaluate_criterion(c, {"result": "not_a_dict"}) == EvalResult.INDETERMINATE


# ── FIELD_VALUE ──────────────────────────────────────────────


class TestFieldValue:
    def test_pass_exact_string_match(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_VALUE,
            evaluator_params={"field": "status", "value": "ok"},
        )
        assert evaluate_criterion(c, {"status": "ok"}) == EvalResult.PASS

    def test_fail_string_mismatch(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_VALUE,
            evaluator_params={"field": "status", "value": "ok"},
        )
        assert evaluate_criterion(c, {"status": "error"}) == EvalResult.FAIL

    def test_indeterminate_type_mismatch(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_VALUE,
            evaluator_params={"field": "status", "value": "ok"},
        )
        assert evaluate_criterion(c, {"status": 42}) == EvalResult.INDETERMINATE

    def test_bool_not_equal_to_int(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_VALUE,
            evaluator_params={"field": "flag", "value": True},
        )
        assert evaluate_criterion(c, {"flag": 1}) == EvalResult.INDETERMINATE

    def test_indeterminate_missing_key(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_VALUE,
            evaluator_params={"field": "x", "value": 1},
        )
        assert evaluate_criterion(c, {}) == EvalResult.INDETERMINATE


# ── FIELD_COUNT ──────────────────────────────────────────────


class TestFieldCount:
    def test_pass_meets_min_count(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_COUNT,
            evaluator_params={"field": "gaps", "min_count": 1},
        )
        assert evaluate_criterion(c, {"gaps": [1, 2]}) == EvalResult.PASS

    def test_fail_below_min_count(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_COUNT,
            evaluator_params={"field": "gaps", "min_count": 1},
        )
        assert evaluate_criterion(c, {"gaps": []}) == EvalResult.FAIL

    def test_indeterminate_not_a_list(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_COUNT,
            evaluator_params={"field": "gaps", "min_count": 1},
        )
        assert evaluate_criterion(c, {"gaps": "not_a_list"}) == EvalResult.INDETERMINATE

    def test_indeterminate_missing_key(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_COUNT,
            evaluator_params={"field": "gaps", "min_count": 1},
        )
        assert evaluate_criterion(c, {}) == EvalResult.INDETERMINATE

    def test_bool_not_accepted_as_min_count(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_COUNT,
            evaluator_params={"field": "gaps", "min_count": True},  # bool, not int
        )
        assert evaluate_criterion(c, {"gaps": [1]}) == EvalResult.INDETERMINATE


# ── Evaluator purity and boundaries ──────────────────────────


class TestEvaluatorPurity:
    def test_unknown_evaluator_type_indeterminate(self):
        c = SuccessCriterion(name="test", evaluator_params={"field": "x"})
        c.evaluator = None  # explicitly None
        assert evaluate_criterion(c, {"x": 1}) == EvalResult.INDETERMINATE

    def test_malformed_params_indeterminate(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": 123},  # not a string
        )
        assert evaluate_criterion(c, {"x": 1}) == EvalResult.INDETERMINATE

    def test_unknown_param_keys_indeterminate(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "x", "unknown_extra": True},
        )
        # Unknown keys → INDETERMINATE
        assert evaluate_criterion(c, {"x": 1}) == EvalResult.INDETERMINATE

    def test_field_path_too_long(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "a" * 129},
        )
        assert evaluate_criterion(c, {}) == EvalResult.INDETERMINATE

    def test_field_path_too_many_segments(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "a.b.c.d.e"},
        )
        assert evaluate_criterion(c, {}) == EvalResult.INDETERMINATE

    def test_field_segment_invalid_chars(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "hello*world"},
        )
        assert evaluate_criterion(c, {}) == EvalResult.INDETERMINATE


# ── Criterion identity ───────────────────────────────────────


class TestCriterionIdentity:
    def test_duplicate_definitions_get_distinct_ids(self):
        a = SuccessCriterion.from_untrusted_terms({"name": "same", "required": True})
        b = SuccessCriterion.from_untrusted_terms({"name": "same", "required": True})
        assert a.criterion_id != b.criterion_id

    def test_externally_supplied_id_discarded(self):
        c = SuccessCriterion.from_untrusted_terms({"criterion_id": "external", "name": "test", "description": ""})
        assert c.criterion_id != "external"

    def test_definition_hash_computed(self):
        c = SuccessCriterion.from_untrusted_terms(
            {"name": "test", "evaluator": "field_present", "evaluator_params": {"field": "gaps"}}
        )
        assert c.criterion_definition_hash
        assert len(c.criterion_definition_hash) == 64  # sha256 hex

    def test_persisted_id_survives_roundtrip(self):
        """criterion_id is preserved through canonical save/load."""
        c = SuccessCriterion.from_untrusted_terms(
            {"name": "test", "evaluator": "field_present", "evaluator_params": {"field": "x"}}
        )
        d = c.to_dict()
        restored = SuccessCriterion.from_persisted_dict(d)
        assert restored.criterion_id == c.criterion_id
        assert restored.criterion_definition_hash == c.criterion_definition_hash

    def test_persisted_hash_mismatch_fails_closed(self):
        """A corrupted definition_hash in persisted data fails closed."""
        c = SuccessCriterion.from_untrusted_terms(
            {"name": "test", "evaluator": "field_present", "evaluator_params": {"field": "x"}}
        )
        d = c.to_dict()
        d["criterion_definition_hash"] = "0" * 64
        with pytest.raises(ValueError, match="criterion_definition_hash mismatch"):
            SuccessCriterion.from_persisted_dict(d)


# ── Evaluator does NOT mutate input ───────────────────────────


class TestEvaluatorDoesNotMutate:
    def test_output_dict_unchanged_after_evaluation(self):
        output = {"gaps": [1, 2, 3]}
        original = dict(output)
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_COUNT,
            evaluator_params={"field": "gaps", "min_count": 1},
        )
        evaluate_criterion(c, output)
        assert output == original

    def test_criterion_met_unchanged_after_evaluation(self):
        c = SuccessCriterion(
            name="test",
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "gaps"},
        )
        before = c.met
        evaluate_criterion(c, {"gaps": []})
        assert c.met == before  # evaluator never sets met
