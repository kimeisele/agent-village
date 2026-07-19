"""
Tests for the VILLAGE_BOUNTIES_ENABLED gate on bounty claim/complete in
village/heartbeat.py::scan_moltbook().

Added per docs/BEFUND.md §13: bounty_claim()/bounty_complete() being
exercised by real external agents was explicitly out of scope for v1
(SPEC.md §4), but was NOT actually gated in code -- same risk class as
Brain before its fix, just not yet triggered because no comment had
happened to say "claim bXXX"/"done bXXX". This mirrors the
VILLAGE_BRAIN_ENABLED pattern.
"""

from __future__ import annotations

import village.heartbeat as hb


def _comments_response(*comments):
    return {"success": True, "comments": list(comments)}


def test_claim_comment_does_nothing_when_disabled(monkeypatch, tmp_path):
    """The exact scenario from Kim's instruction: a real 'claim b001'
    comment must not trigger bounty_claim() or any reply while the flag
    is off."""
    monkeypatch.delenv("VILLAGE_BOUNTIES_ENABLED", raising=False)
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "PROC_MB", tmp_path / "processed_comments.json")
    monkeypatch.setattr(hb, "MB", "fake-key")
    monkeypatch.setattr(hb, "REG_POST", "post123")
    monkeypatch.setattr(hb, "BOUNTIES", tmp_path / "bounties.json")
    hb._save(hb.BOUNTIES, {"bounties": [{"id": "b001", "title": "test", "status": "open",
                                          "claimed_by": None, "claimed_at": None, "completed_at": None}]})

    claim_calls = []
    monkeypatch.setattr(hb, "bounty_claim", lambda *a, **k: claim_calls.append(a) or None)

    post_calls = []
    monkeypatch.setattr(hb, "_post_comment_verified", lambda *a, **k: post_calls.append(a) or {"verified": True})

    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: _comments_response({
            "id": "c1", "content": "I claim b001", "author": {"name": "SomeAgent"},
        }) if "comments" in path and method == "GET" else {"success": True},
    )

    result = hb.scan_moltbook()
    assert result == 0
    assert claim_calls == []  # bounty_claim() never even called
    assert post_calls == []  # no reply attempted either

    # Not marked processed -- must retry once the flag is enabled.
    proc = hb._load(hb.PROC_MB).get("comment_ids", [])
    assert "c1" not in proc


def test_claim_comment_proceeds_when_enabled(monkeypatch, tmp_path):
    monkeypatch.setenv("VILLAGE_BOUNTIES_ENABLED", "1")
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "PROC_MB", tmp_path / "processed_comments.json")
    monkeypatch.setattr(hb, "CONTRIBUTIONS", tmp_path / "contributions.json")
    monkeypatch.setattr(hb, "MB", "fake-key")
    monkeypatch.setattr(hb, "REG_POST", "post123")

    claim_calls = []
    monkeypatch.setattr(hb, "bounty_claim", lambda *a, **k: claim_calls.append(a) or {"id": "b001", "title": "test"})
    monkeypatch.setattr(hb, "_post_comment_verified", lambda *a, **k: {"verified": True})

    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: _comments_response({
            "id": "c1", "content": "I claim b001", "author": {"name": "SomeAgent"},
        }) if "comments" in path and method == "GET" else {"success": True},
    )

    result = hb.scan_moltbook()
    assert result == 1
    assert len(claim_calls) == 1


def test_done_comment_does_nothing_when_disabled(monkeypatch, tmp_path):
    monkeypatch.delenv("VILLAGE_BOUNTIES_ENABLED", raising=False)
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "PROC_MB", tmp_path / "processed_comments.json")
    monkeypatch.setattr(hb, "MB", "fake-key")
    monkeypatch.setattr(hb, "REG_POST", "post123")

    complete_calls = []
    monkeypatch.setattr(hb, "bounty_complete", lambda *a, **k: complete_calls.append(a) or None)
    post_calls = []
    monkeypatch.setattr(hb, "_post_comment_verified", lambda *a, **k: post_calls.append(a) or {"verified": True})

    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: _comments_response({
            "id": "c2", "content": "done b001", "author": {"name": "SomeAgent"},
        }) if "comments" in path and method == "GET" else {"success": True},
    )

    result = hb.scan_moltbook()
    assert result == 0
    assert complete_calls == []
    assert post_calls == []
