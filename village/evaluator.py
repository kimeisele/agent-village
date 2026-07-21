"""Pure deterministic evaluator for bounty success criteria.

Side-effect-free: no file I/O, no network I/O, no state mutation.
Never imports heartbeat, GitHub, review, or terminal-mutation modules.
Uses the shared config validator from village.contracts.
"""

from __future__ import annotations

from enum import Enum
from typing import Any

from village.contracts import (
    EvaluatorType,
    SuccessCriterion,
    validate_evaluator_config,
)

_SENTINEL = object()


class EvalResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


def _resolve_path(output: dict[str, object], field: str) -> object:
    segments = field.split(".")
    current: object = output
    for seg in segments:
        if not isinstance(current, dict):
            return _SENTINEL
        if seg not in current:
            return _SENTINEL
        current = current[seg]
    return current


def evaluate_criterion(criterion: SuccessCriterion, output: dict[str, Any]) -> EvalResult:
    """Pure, side-effect-free evaluation of one criterion.

    Returns PASS, FAIL, or INDETERMINATE. Never raises.
    Never mutates state. Never performs I/O.
    Uses the shared config validator for parameter schemas.
    """
    evaluator = criterion.evaluator
    params = criterion.evaluator_params

    if not isinstance(output, dict):
        return EvalResult.INDETERMINATE
    if not isinstance(params, dict):
        return EvalResult.INDETERMINATE
    if evaluator is None:
        return EvalResult.INDETERMINATE

    # Validate config via shared boundary before evaluation
    try:
        validate_evaluator_config(evaluator, params)
    except ValueError:
        return EvalResult.INDETERMINATE

    field = params.get("field")
    if not isinstance(field, str):
        return EvalResult.INDETERMINATE

    if evaluator == EvaluatorType.FIELD_PRESENT:
        val = _resolve_path(output, field)
        if val is _SENTINEL:
            return EvalResult.INDETERMINATE
        if val is None:
            return EvalResult.FAIL
        return EvalResult.PASS

    elif evaluator == EvaluatorType.FIELD_VALUE:
        expected = params.get("value")
        val = _resolve_path(output, field)
        if val is _SENTINEL:
            return EvalResult.INDETERMINATE
        if type(val) is not type(expected):
            return EvalResult.INDETERMINATE
        if val == expected:
            return EvalResult.PASS
        return EvalResult.FAIL

    elif evaluator == EvaluatorType.FIELD_COUNT:
        mc = params.get("min_count")
        if not isinstance(mc, int) or isinstance(mc, bool):
            return EvalResult.INDETERMINATE
        val = _resolve_path(output, field)
        if val is _SENTINEL:
            return EvalResult.INDETERMINATE
        if not isinstance(val, list):
            return EvalResult.INDETERMINATE
        if len(val) >= mc:
            return EvalResult.PASS
        return EvalResult.FAIL

    return EvalResult.INDETERMINATE
