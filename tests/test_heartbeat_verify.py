"""
Tests for the verify-flow wiring in village/heartbeat.py::_post_comment_verified.

Not a live test — mocks village.heartbeat._mb and
village.moltbook_captcha.get_challenge_monitor/solve_and_verify. Covers:
comment with no challenge, comment that gets verified, comment where the
monitor is already halted (must not even attempt the post), and a failed
post (no comment in response).
"""

from __future__ import annotations

import village.heartbeat as hb
import village.moltbook_captcha as mc


def test_no_challenge_triggered(monkeypatch):
    monkeypatch.setattr(hb, "_mb", lambda path, method="GET", body=None: {
        "success": True,
        "comment": {"id": "c1", "content": "hi"},
    })
    result = hb._post_comment_verified("post123", "hello", parent_id="c0")
    assert result == {"posted": True, "verified": True}


def test_challenge_solved(monkeypatch):
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


def test_halted_monitor_skips_post_entirely(monkeypatch):
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


def test_post_failure_returns_not_posted(monkeypatch):
    monkeypatch.setattr(hb, "_mb", lambda path, method="GET", body=None: None)
    result = hb._post_comment_verified("post123", "x", parent_id="c0")
    assert result["posted"] is False
    assert result["reason"] == "post_failed"
