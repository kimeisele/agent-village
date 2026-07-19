"""
Tests for the verify-flow wiring in village/heartbeat.py::_post_comment_verified
and the cross-cycle ban persistence added in BEFUND.md §9.

Not a live test — mocks village.heartbeat._mb and
village.moltbook_captcha.get_challenge_monitor/solve_and_verify. Every test
redirects CHALLENGE_STATE to a tmp_path file so no test depends on or
pollutes the real data/village/challenge_failures.json.
"""

from __future__ import annotations

import village.heartbeat as hb
import village.moltbook_captcha as mc


def test_already_verified_without_fresh_challenge(monkeypatch, tmp_path):
    """Some comments could in principle come back already
    verification_status="verified" with no fresh `verification` object —
    that's the one case where "no verification object" legitimately means
    success."""
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "REPLY_COMMENT_IDS", tmp_path / "reply_comment_ids.json")
    monkeypatch.setattr(hb, "_mb", lambda path, method="GET", body=None: {
        "success": True,
        "comment": {"id": "c1", "content": "hi", "verification_status": "verified"},
    })
    result = hb._post_comment_verified("post123", "hello", parent_id="c0")
    # comment_id now always included (docs/SPEC.md §C.5 — persist the
    # Moltbook POST result id immediately, regardless of verify outcome).
    assert result == {"posted": True, "verified": True, "comment_id": "c1"}


def test_no_verification_status_at_all_is_not_verified(monkeypatch, tmp_path):
    """FIX (docs/BEFUND.md §15): a response with neither a fresh
    `verification` object NOR a verification_status must NOT default to
    verified=True. Only verification_status == "verified" counts as
    success — this was the old (wrong) fallback behavior."""
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "REPLY_COMMENT_IDS", tmp_path / "reply_comment_ids.json")
    monkeypatch.setattr(hb, "_mb", lambda path, method="GET", body=None: {
        "success": True,
        "comment": {"id": "c1", "content": "hi"},
    })
    result = hb._post_comment_verified("post123", "hello", parent_id="c0")
    assert result["posted"] is True
    assert result["verified"] is False


def test_challenge_solved(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "REPLY_COMMENT_IDS", tmp_path / "reply_comment_ids.json")
    calls = []

    def fake_mb(path, method="GET", body=None):
        calls.append((path, method, body))
        if path == "posts/post123/comments":
            return {
                "success": True,
                "comment": {
                    "id": "c1",
                    "verification_status": "pending",
                    "verification": {
                        "verification_code": "moltbook_verify_x",
                        "challenge_text": "What is seven + 3?",
                    },
                },
            }
        if path == "verify":
            assert body["verification_code"] == "moltbook_verify_x"
            assert body["answer"] == "10.00"
            return {"success": True}
        raise AssertionError(f"unexpected call: {path}")

    monkeypatch.setattr(hb, "_mb", fake_mb)
    monkeypatch.setattr(mc, "_challenge_monitor", mc.ChallengeMonitor())  # fresh, not halted

    result = hb._post_comment_verified("post123", "registration reply", parent_id="c0")
    assert result["posted"] is True
    assert result["verified"] is True
    assert any(c[0] == "verify" for c in calls)


def test_halted_monitor_skips_post_entirely(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    calls = []

    def fake_mb(path, method="GET", body=None):
        calls.append(path)
        return {"success": True, "comment": {}}

    monkeypatch.setattr(hb, "_mb", fake_mb)

    halted_monitor = mc.ChallengeMonitor()
    halted_monitor._halted = True
    monkeypatch.setattr(mc, "_challenge_monitor", halted_monitor)

    result = hb._post_comment_verified("post123", "should not be posted", parent_id="c0")
    assert result == {"posted": False, "reason": "monitor_halted"}
    assert calls == []  # _mb never called — no comment attempt at all


def test_post_failure_returns_not_posted(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "_mb", lambda path, method="GET", body=None: None)
    result = hb._post_comment_verified("post123", "x", parent_id="c0")
    assert result["posted"] is False
    assert result["reason"] == "post_failed"


# =============================================================================
# Cross-cycle ban persistence (BEFUND.md §9)
# =============================================================================


def test_banned_state_blocks_post_without_calling_mb(monkeypatch, tmp_path):
    state_path = tmp_path / "challenge_failures.json"
    state_path.write_text('{"banned": true, "consecutive_failures": 10}')
    monkeypatch.setattr(hb, "CHALLENGE_STATE", state_path)

    calls = []
    monkeypatch.setattr(hb, "_mb", lambda *a, **k: calls.append(a) or {"success": True})

    result = hb._post_comment_verified("post123", "x", parent_id="c0")
    assert result == {"posted": False, "reason": "banned_cross_cycle"}
    assert calls == []


def test_save_sets_banned_and_logs_error_at_threshold(monkeypatch, tmp_path, capsys):
    state_path = tmp_path / "challenge_failures.json"
    monkeypatch.setattr(hb, "CHALLENGE_STATE", state_path)

    monitor = mc.ChallengeMonitor()
    monitor._consecutive_failures = mc.ChallengeMonitor.BAN_THRESHOLD
    monitor._total_attempts = 10
    monitor._total_failures = 10
    monkeypatch.setattr(mc, "_challenge_monitor", monitor)

    hb._save_challenge_monitor_state()

    saved = hb._load(state_path)
    assert saved["banned"] is True
    assert saved["consecutive_failures"] == mc.ChallengeMonitor.BAN_THRESHOLD
    captured = capsys.readouterr()
    assert "::error::" in captured.out
    assert "BAN_THRESHOLD" in captured.out


def test_banned_flag_sticky_across_a_later_success(monkeypatch, tmp_path):
    """Once banned, a subsequent save with fewer consecutive_failures
    (e.g. after one success reset the in-process counter) must NOT clear
    it automatically — only a manual edit of the file should."""
    state_path = tmp_path / "challenge_failures.json"
    state_path.write_text('{"banned": true, "consecutive_failures": 10}')
    monkeypatch.setattr(hb, "CHALLENGE_STATE", state_path)

    monitor = mc.ChallengeMonitor()
    monitor._consecutive_failures = 0  # as if a success just reset it
    monkeypatch.setattr(mc, "_challenge_monitor", monitor)

    hb._save_challenge_monitor_state()

    saved = hb._load(state_path)
    assert saved["banned"] is True  # still banned — sticky


def test_load_state_restores_monitor_counters(monkeypatch, tmp_path):
    state_path = tmp_path / "challenge_failures.json"
    state_path.write_text('{"consecutive_failures": 3, "total_attempts": 7, "total_successes": 4, "total_failures": 3, "halted": false}')
    monkeypatch.setattr(hb, "CHALLENGE_STATE", state_path)

    fresh_monitor = mc.ChallengeMonitor()
    monkeypatch.setattr(mc, "_challenge_monitor", fresh_monitor)

    hb._load_challenge_monitor_state()

    stats = mc.get_challenge_monitor().get_stats()
    assert stats["consecutive_failures"] == 3
    assert stats["total_attempts"] == 7
