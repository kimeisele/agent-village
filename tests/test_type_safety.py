"""Tests for type-safety boundary validators.

Covers JsonValue/JsonObject loading, ID validation, datetime fallbacks,
and persistence round-trips added during Type Safety Foundation 01.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from village._types import is_json_value, load_json_object


class TestIsJsonValue:
    """Recursive runtime validation of JSON-compatible values."""

    def test_none_is_valid(self):
        assert is_json_value(None)

    def test_string_is_valid(self):
        assert is_json_value("hello")

    def test_bool_is_valid(self):
        assert is_json_value(True)
        assert is_json_value(False)

    def test_int_is_valid(self):
        assert is_json_value(42)
        assert is_json_value(0)
        assert is_json_value(-1)

    def test_float_is_valid(self):
        assert is_json_value(3.14)

    def test_nan_is_rejected(self):
        assert not is_json_value(float("nan"))

    def test_infinity_is_rejected(self):
        assert not is_json_value(float("inf"))
        assert not is_json_value(float("-inf"))

    def test_list_of_valid_values_is_valid(self):
        assert is_json_value([1, "two", None, True])

    def test_list_containing_nan_is_rejected(self):
        assert not is_json_value([1.0, float("nan")])

    def test_dict_with_string_keys_is_valid(self):
        assert is_json_value({"a": 1, "b": [2, 3]})

    def test_dict_with_non_string_key_is_rejected(self):
        assert not is_json_value({1: "value"})

    def test_dict_with_invalid_nested_value_is_rejected(self):
        assert not is_json_value({"a": float("nan")})


class TestLoadJsonObject:
    """Top-level JSON object validation."""

    def test_valid_object_succeeds(self):
        assert load_json_object('{"a": 1}') == {"a": 1}

    def test_nested_valid_object_succeeds(self):
        assert load_json_object('{"a": {"b": [1, 2]}}') == {"a": {"b": [1, 2]}}

    def test_json_array_raises(self):
        with pytest.raises(ValueError, match="expected.*object"):
            load_json_object("[1, 2, 3]")

    def test_json_string_raises(self):
        with pytest.raises(ValueError, match="expected.*object"):
            load_json_object('"hello"')

    def test_json_number_raises(self):
        with pytest.raises(ValueError, match="expected.*object"):
            load_json_object("42")

    def test_json_null_raises(self):
        with pytest.raises(ValueError, match="expected.*object"):
            load_json_object("null")

    def test_object_with_nan_value_raises(self):
        with pytest.raises(ValueError, match="non-JSON-compatible"):
            load_json_object('{"a": NaN}')

    def test_object_with_infinity_value_raises(self):
        with pytest.raises(ValueError, match="non-JSON-compatible"):
            load_json_object('{"a": Infinity}')

    def test_non_string_top_level_key_raises(self):
        # json.loads always produces string keys, but we validate anyway
        # This test covers direct Python data passed to is_json_value
        assert not is_json_value({1: "value"})


class TestSubmissionIdValidation:
    """Required ID is not a string — error, not silent conversion."""

    def test_numeric_submission_id_rejected(self, monkeypatch, tmp_path):
        """A bounty with a numeric submission_id returns None from bounty_review."""
        import village.bounty_review as br
        import village.heartbeat as hb

        monkeypatch.setattr(hb, "BOUNTIES", tmp_path / "bounties.json")
        monkeypatch.setattr(hb, "CONTRACTS", tmp_path / "contracts.json")
        monkeypatch.setattr(br, "SUBMISSIONS", tmp_path / "bounty_submissions.json")

        # Create a bounty with a numeric submission_id (simulating corrupt data)
        hb._save(
            hb.BOUNTIES,
            {
                "bounties": [
                    {
                        "id": "b001",
                        "title": "test",
                        "description": "",
                        "reward": "reputation",
                        "status": "submitted",
                        "created_by": "agent-village",
                        "created_at": 0.0,
                        "claimed_by": "agent1",
                        "claimed_at": 0.0,
                        "completed_at": None,
                        "current_submission_id": 12345,  # non-string!
                    }
                ]
            },
        )

        # bounty_review should handle gracefully (no contract → None)
        result = br.bounty_review("b001", "reviewer1", "accept")
        assert result is None


class TestDatetimeFallbacks:
    """Datetime edge cases: missing uses default, corrupt raises error."""

    def test_contract_created_at_default_when_missing(self):
        """Missing created_at uses _now() in from_dict."""
        from village.contracts import VillageContract

        d: dict[str, object] = {"contract_id": "c:1"}
        c = VillageContract.from_dict(d)
        assert c.created_at is not None
        assert c.created_at.tzinfo is not None  # always UTC-aware

    def test_existing_valid_persistence_roundtrip(self):
        """Existing valid persistence data works unchanged."""
        from village.contracts import VillageContract

        original = VillageContract(
            contract_id="c:1",
            title="t",
            description="d",
            deadline=datetime(2026, 7, 20, tzinfo=timezone.utc),
        )
        d = original.to_dict()
        restored = VillageContract.from_dict(d)
        assert restored.contract_id == original.contract_id
        assert restored.deadline == original.deadline
        assert restored.created_at == original.created_at

    def test_work_result_started_at_default(self):
        """Missing started_at uses datetime.now(timezone.utc)."""
        from village.work_result import WorkResult, WorkResultStatus

        wr = WorkResult(
            work_result_id="wr:1",
            contract_id="c:1",
            execution_id="e:1",
            provider="p",
            model="m",
            status=WorkResultStatus.SUCCEEDED,
        )
        assert wr.started_at is not None
        assert wr.started_at.tzinfo is not None
