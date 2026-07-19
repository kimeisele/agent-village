"""Shared type aliases for JSON persistence boundaries.

Python 3.11 compatible — uses TypeAlias + string-forwarding for
recursive types instead of the 3.12 ``type`` statement.
"""

from __future__ import annotations

import json
import math
from typing import TypeAlias, TypeGuard

JsonScalar: TypeAlias = str | int | float | bool | None
JsonValue: TypeAlias = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
JsonObject: TypeAlias = dict[str, JsonValue]


def is_json_value(obj: object) -> TypeGuard[JsonValue]:
    """Recursive runtime validator for JSON-compatible values.

    Returns ``True`` only if *obj* is a valid :class:`JsonValue`:
    strings, integers, floats (excluding NaN and Infinity), booleans,
    ``None``, lists of valid ``JsonValue``, and dicts with string keys
    and valid ``JsonValue`` values.

    Rejects ``NaN``, ``Infinity``, and ``-Infinity`` — these are not
    valid JSON per :rfc:`8259` and are not part of agent-village's
    persistence specification.
    """
    if obj is None:
        return True
    if isinstance(obj, (str, bool)):
        return True
    if isinstance(obj, int):
        return True
    if isinstance(obj, float):
        if math.isnan(obj) or math.isinf(obj):
            return False
        return True
    if isinstance(obj, list):
        return all(is_json_value(item) for item in obj)
    if isinstance(obj, dict):
        return all(isinstance(k, str) and is_json_value(v) for k, v in obj.items())
    return False


def load_json_object(data: str | bytes) -> JsonObject:
    """Parse JSON and validate the top-level value is a JSON object.

    The validation is recursive: every key must be a string and every
    value must be a valid :class:`JsonValue` (no ``NaN``, no
    ``Infinity``, no non-standard types).

    Raises:
        ValueError: if *data* is not valid JSON, the top-level value
            is not a mapping, any key is not a string, or any value is
            not a valid ``JsonValue``.
    """
    parsed: object = json.loads(data)
    if not isinstance(parsed, dict):
        raise ValueError(f"expected JSON object at top level, got {type(parsed).__name__}")
    if not all(isinstance(k, str) for k in parsed):
        raise ValueError("all keys in the top-level JSON object must be strings")
    if not is_json_value(parsed):
        raise ValueError("top-level JSON object contains non-JSON-compatible values")
    assert isinstance(parsed, dict)  # re-narrow after TypeGuard erases isinstance(parsed, dict)
    return parsed
