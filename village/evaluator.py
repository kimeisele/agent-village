"""Pure deterministic evaluator for bounty success criteria.

Side-effect-free: no file I/O, no network I/O, no state mutation.
Never imports heartbeat, GitHub, review, or terminal-mutation modules.
Never invokes an LLM or executes free text.
"""

from __future__ import annotations

import math
import re
from enum import Enum
from typing import Any

from village.contracts import EvaluatorType, SuccessCriterion

_SENTINEL = object()

_MAX_FIELD_LEN = 128
_MAX_SEGMENTS = 4
_MAX_SEGMENT_LEN = 32
_FIELD_SEGMENT_RE = re.compile(r"^[a-zA-Z0-9_]+$")
_MAX_VALUE_STRLEN = 256
_MAX_MIN_COUNT = 1_000_000


class EvalResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"


def _resolve_path(output: dict[str, object], field: str) -> object:
    """Resolve a dot-separated dictionary path. Returns _SENTINEL if the
    path cannot be traversed (missing key, non-dict intermediate).
    """
    segments = field.split(".")
    current: object = output
    for seg in segments:
        if not isinstance(current, dict):
            return _SENTINEL
        if seg not in current:
            return _SENTINEL
        current = current[seg]
    return current


def _validate_field_path(field: str) -> str | None:
    """Validate the field path against the bounded rules.

    Returns an error message string on failure, or None on success.
    """
    if not field or len(field) > _MAX_FIELD_LEN:
        return f"field path too long ({len(field)} > {_MAX_FIELD_LEN})"
    segments = field.split(".")
    if len(segments) > _MAX_SEGMENTS:
        return f"too many path segments ({len(segments)} > {_MAX_SEGMENTS})"
    for seg in segments:
        if not seg or len(seg) > _MAX_SEGMENT_LEN:
            return f"segment too long (max {_MAX_SEGMENT_LEN})"
        if not _FIELD_SEGMENT_RE.match(seg):
            return f"invalid segment characters: {seg!r}"
    return None


def _make_reason(evaluator_type: str, field: str, code: str) -> EvalResult:
    """Build a bounded, sanitized reason code and return INDETERMINATE."""
    return EvalResult.INDETERMINATE


def evaluate_criterion(criterion: SuccessCriterion, output: dict[str, Any]) -> EvalResult:
    """Pure, side-effect-free evaluation of one criterion.

    Returns PASS, FAIL, or INDETERMINATE. Never raises.
    Never mutates state. Never performs I/O.
    """
    evaluator = criterion.evaluator
    params = criterion.evaluator_params

    if not isinstance(output, dict):
        return EvalResult.INDETERMINATE
    if not isinstance(params, dict):
        return EvalResult.INDETERMINATE
    if evaluator is None:
        return EvalResult.INDETERMINATE

    field = params.get("field")
    if not isinstance(field, str):
        return _make_reason("unknown", str(field)[:32], "indeterminate_params")

    path_err = _validate_field_path(field)
    if path_err is not None:
        return EvalResult.INDETERMINATE

    if evaluator == EvaluatorType.FIELD_PRESENT:
        # Check for unknown keys
        for k in params:
            if k != "field":
                return EvalResult.INDETERMINATE
        val = _resolve_path(output, field)
        if val is _SENTINEL:
            return EvalResult.INDETERMINATE
        if val is None:
            return EvalResult.FAIL
        return EvalResult.PASS

    elif evaluator == EvaluatorType.FIELD_VALUE:
        expected = params.get("value", _SENTINEL)
        if expected is _SENTINEL:
            return EvalResult.INDETERMINATE
        if not isinstance(expected, (str, int, float, bool)):
            return EvalResult.INDETERMINATE
        if isinstance(expected, str) and len(expected) > _MAX_VALUE_STRLEN:
            return EvalResult.INDETERMINATE
        if isinstance(expected, float) and (math.isnan(expected) or math.isinf(expected)):
            return EvalResult.INDETERMINATE
        # Check for unknown keys
        for k in params:
            if k not in ("field", "value"):
                return EvalResult.INDETERMINATE

        val = _resolve_path(output, field)
        if val is _SENTINEL:
            return EvalResult.INDETERMINATE
        # Strict type equality
        if type(val) is not type(expected):
            return EvalResult.INDETERMINATE
        if val == expected:
            return EvalResult.PASS
        return EvalResult.FAIL

    elif evaluator == EvaluatorType.FIELD_COUNT:
        min_count = params.get("min_count")
        if not isinstance(min_count, int) or isinstance(min_count, bool):
            return EvalResult.INDETERMINATE
        if min_count < 0 or min_count > _MAX_MIN_COUNT:
            return EvalResult.INDETERMINATE
        # Check for unknown keys
        for k in params:
            if k not in ("field", "min_count"):
                return EvalResult.INDETERMINATE

        val = _resolve_path(output, field)
        if val is _SENTINEL:
            return EvalResult.INDETERMINATE
        if not isinstance(val, list):
            return EvalResult.INDETERMINATE
        if len(val) >= min_count:
            return EvalResult.PASS
        return EvalResult.FAIL

    # Unknown evaluator type
    return EvalResult.INDETERMINATE
