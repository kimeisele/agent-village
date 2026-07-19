"""
Tests for village/heartbeat.py::_sanitize_name and its wiring into
scan_moltbook()'s registration path. Added per docs/BEFUND.md §21:
registration names come from unauthenticated free text (SPEC.md §2.1
"Known limitation"), so a hostile or malformed "name:" value must not be
stored/posted back raw -- unbounded length or embedded control characters
were both previously possible.

The existing regression case (a normal short name via the "join"-only,
no-"name:"-field fallback path) is NOT reinvented here -- it's already
covered by tests/test_pending_confirmation.py::
test_first_run_fails_verify_second_run_retries_not_skips (uses author
name "B_ClawAssistant" with no "name:" field, i.e. the sender-fallback
path). That test still passes unchanged after this fix (see the full
suite run in docs/BEFUND.md §21).
"""

from __future__ import annotations

import village.heartbeat as hb


def _comments_response(*comments):
    return {"success": True, "comments": list(comments)}


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "PROC_MB", tmp_path / "processed_comments.json")
    monkeypatch.setattr(hb, "PENDING_MB", tmp_path / "pending_confirmations.json")
    monkeypatch.setattr(hb, "POKEDEX", tmp_path / "pokedex.json")
    monkeypatch.setattr(hb, "BOUNTIES", tmp_path / "bounties.json")
    monkeypatch.setattr(hb, "MB", "fake-key")
    monkeypatch.setattr(hb, "REG_POST", "post123")
    hb._save(hb.BOUNTIES, {"bounties": []})
    monkeypatch.setattr(hb, "_post_comment_verified", lambda *a, **k: {"posted": True, "verified": True})


# =============================================================================
# Unit tests: _sanitize_name directly
# =============================================================================


def test_truncates_to_40_chars():
    raw = "Lorem ipsum dolor sit amet " * 10  # way over 40 chars
    result = hb._sanitize_name(raw, "fallback")
    assert len(result) == 40


def test_strips_control_characters():
    result = hb._sanitize_name("Bo\nb\t\x00", "fallback")
    assert "\n" not in result
    assert "\t" not in result
    assert "\x00" not in result
    assert result == "Bob"


def test_only_control_characters_falls_back_to_sender():
    result = hb._sanitize_name("\x00\x01\x02", "SomeSender")
    assert result == "SomeSender"


def test_unicode_letters_pass_through_unmodified():
    """Control-char stripping must not become an ASCII filter -- accented
    and non-Latin letters are ordinary Unicode categories, not control
    chars, and must survive."""
    assert hb._sanitize_name("Jörg", "fallback") == "Jörg"
    assert hb._sanitize_name("北京", "fallback") == "北京"
    assert hb._sanitize_name("Müller-Ω", "fallback") == "Müller-Ω"


def test_normal_short_name_unchanged():
    assert hb._sanitize_name("Alice", "fallback") == "Alice"


# =============================================================================
# End-to-end: wired into scan_moltbook()'s registration path
# =============================================================================


def test_long_name_registers_truncated_to_40_chars(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    lorem = "Lorem ipsum dolor sit amet consectetur adipiscing elit sed do " * 3  # 200+ chars
    comment = {"id": "c1", "content": f"join name: {lorem}", "author": {"name": "sender1"}}
    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: _comments_response(comment) if "comments" in path and method == "GET" else {"success": True},
    )
    hb.scan_moltbook()
    agents = hb._load(hb.POKEDEX).get("agents", [])
    assert len(agents) == 1
    assert len(agents[0]["name"]) == 40


def test_control_chars_in_name_are_cleaned_before_storing(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    # NOTE: the pre-existing extraction regex `name[:\s]+([^\n]+)` already
    # stops at the first newline (unrelated to this fix), so a literal
    # "\n" in the raw comment truncates the captured group before
    # _sanitize_name ever sees it. Using tab + NUL (no newline) here so
    # the control characters actually reach _sanitize_name, proving ITS
    # cleaning rather than the regex's incidental truncation. The direct
    # unit test above (test_strips_control_characters) covers the "\n"
    # case against _sanitize_name() itself, bypassing the regex.
    comment = {"id": "c1", "content": "join name: Bo\tb\x00", "author": {"name": "sender1"}}
    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: _comments_response(comment) if "comments" in path and method == "GET" else {"success": True},
    )
    hb.scan_moltbook()
    agents = hb._load(hb.POKEDEX).get("agents", [])
    assert len(agents) == 1
    assert agents[0]["name"] == "Bob"
    assert all(ord(ch) >= 0x20 and ord(ch) != 0x7F for ch in agents[0]["name"])


def test_name_field_with_only_control_chars_falls_back_to_sender(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    comment = {"id": "c1", "content": "join name: \x00\x01\x02", "author": {"name": "RealSender"}}
    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: _comments_response(comment) if "comments" in path and method == "GET" else {"success": True},
    )
    hb.scan_moltbook()
    agents = hb._load(hb.POKEDEX).get("agents", [])
    assert len(agents) == 1
    assert agents[0]["name"] == "RealSender"
