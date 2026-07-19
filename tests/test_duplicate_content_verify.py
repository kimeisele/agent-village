"""
Regression tests for docs/BEFUND.md §15 — the "already_existed" / duplicate
content bug found live 2026-07-18: a byte-identical retry got Moltbook's
deduplication response back (the OLD, already-"failed" comment, no fresh
verification object) and was incorrectly treated as verified=True.
"""

from __future__ import annotations

import village.heartbeat as hb


def test_already_existed_failed_comment_is_not_verified(monkeypatch, tmp_path):
    """Exact reproduction of the live find: POST /comments returns
    success=true, already_existed=true, and the OLD comment with
    verification_status="failed" — must be treated as verified=False."""
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "REPLY_COMMENT_IDS", tmp_path / "reply_comment_ids.json")
    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: {
            "success": True,
            "message": "You already said this on this post! Here is your existing comment.",
            "already_existed": True,
            "comment": {
                "id": "17be4b04-5a5a-4d2e-8f22-c9a0dac44fdf",
                "content": "🦞 **B_ClawAssistant** registered! prithvi/engineering/prahlada. Pop: 1 | Open bounties: 3",
                "verification_status": "failed",
                "already_existed": True,
            },
        },
    )
    result = hb._post_comment_verified("post123", "🦞 **B_ClawAssistant** registered! ...", parent_id="c0")
    assert result["posted"] is True
    assert result["verified"] is False
    assert result["reason"] == "no_fresh_challenge_status_failed"


def test_already_existed_pending_comment_is_not_verified(monkeypatch, tmp_path):
    """Same duplicate-detection path, but the stale comment happens to
    still be "pending" rather than "failed" -- still not verified."""
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "REPLY_COMMENT_IDS", tmp_path / "reply_comment_ids.json")
    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: {
            "success": True,
            "already_existed": True,
            "comment": {"id": "old1", "verification_status": "pending"},
        },
    )
    result = hb._post_comment_verified("post123", "some content", parent_id="c0")
    assert result["verified"] is False


def test_retry_suffix_makes_consecutive_registration_attempts_differ(monkeypatch, tmp_path):
    """FIX 2: two consecutive registration attempts for the same agent
    must produce different comment text, so Moltbook's dedup can't return
    a stale comment instead of creating a new one with a fresh challenge."""
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "PROC_MB", tmp_path / "processed_comments.json")
    monkeypatch.setattr(hb, "PENDING_MB", tmp_path / "pending_confirmations.json")
    monkeypatch.setattr(hb, "POKEDEX", tmp_path / "pokedex.json")
    monkeypatch.setattr(hb, "BOUNTIES", tmp_path / "bounties.json")
    monkeypatch.setattr(hb, "CONTRIBUTIONS", tmp_path / "contributions.json")
    monkeypatch.setattr(hb, "MB", "fake-key")
    monkeypatch.setattr(hb, "REG_POST", "post123")
    hb._save(hb.BOUNTIES, {"bounties": []})

    join_comment = {"id": "c1", "content": "join", "author": {"name": "B_ClawAssistant"}}
    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: (
            {"success": True, "comments": [join_comment]} if "comments" in path and method == "GET" else {"success": True}
        ),
    )

    seen_content = []

    def fake_verify(_post_id, content, parent_id=None):
        seen_content.append(content)
        return {"posted": True, "verified": False, "reason": "verify_rejected"}

    monkeypatch.setattr(hb, "_post_comment_verified", fake_verify)

    hb.scan_moltbook()  # attempt 1 (first encounter)
    hb.scan_moltbook()  # attempt 2 (retry pass)

    assert len(seen_content) == 2
    assert seen_content[0] != seen_content[1], "retried comment text must differ from the first attempt"
    # Core statement (name, pop, bounty count) must be unchanged -- only a
    # small suffix should differ.
    assert "B_ClawAssistant" in seen_content[0] and "B_ClawAssistant" in seen_content[1]
    assert "attempt" in seen_content[1]
