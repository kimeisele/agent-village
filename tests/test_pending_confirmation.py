"""
Tests for the pending-confirmation retry fix (docs/BEFUND.md §14).

Reproduces the exact bug found live in run 29660128767: a registration
comment's underlying action (dex_register) succeeds and is idempotent, but
its confirmation reply fails verification. On the next cycle,
dex_register() correctly reports "_dup" (already registered) — but that
must NOT be read as "nothing left to do here". The comment must still get
a fresh confirmation attempt, tracked independently in
data/village/pending_confirmations.json rather than inferred from
dex_register()'s idempotent return value.
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
    monkeypatch.setattr(hb, "CONTRIBUTIONS", tmp_path / "contributions.json")
    monkeypatch.setattr(hb, "REPLY_COMMENT_IDS", tmp_path / "reply_comment_ids.json")
    monkeypatch.setattr(hb, "MB", "fake-key")
    monkeypatch.setattr(hb, "REG_POST", "post123")
    hb._save(hb.BOUNTIES, {"bounties": []})


def test_first_run_fails_verify_second_run_retries_not_skips(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    join_comment = {"id": "c1", "content": "join", "author": {"name": "B_ClawAssistant"}}

    # --- Run 1: registration succeeds, confirmation reply fails verification ---
    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: _comments_response(join_comment) if "comments" in path and method == "GET" else {"success": True},
    )
    verify_attempts_run1 = []
    monkeypatch.setattr(
        hb, "_post_comment_verified",
        lambda *a, **k: verify_attempts_run1.append(a) or {"posted": True, "verified": False, "reason": "verify_rejected"},
    )

    result1 = hb.scan_moltbook()
    assert result1 == 0
    assert len(verify_attempts_run1) == 1  # confirmation WAS attempted

    # Registration itself (idempotent) already happened.
    assert hb._load(hb.POKEDEX).get("total") == 1
    # Comment must NOT be in proc yet -- reply never verified.
    proc = hb._load(hb.PROC_MB).get("comment_ids", [])
    assert "c1" not in proc
    # Must be tracked as pending.
    pending = hb._load(hb.PENDING_MB)
    assert "c1" in pending["registration"]
    assert pending["registration"]["c1"]["name"] == "B_ClawAssistant"

    # --- Run 2: dex_register() will now report _dup=True for this name.
    # The retry must still happen, driven by the pending dict, not by
    # re-deciding based on dex_register()'s (idempotent, now "_dup")
    # return value. ---
    verify_attempts_run2 = []

    def fake_verify_run2(*a, **k):
        verify_attempts_run2.append(a)
        return {"posted": True, "verified": True}

    monkeypatch.setattr(hb, "_post_comment_verified", fake_verify_run2)
    # Same comment still returned by the API (as it would be in reality --
    # Moltbook doesn't stop returning old comments).
    monkeypatch.setattr(
        hb, "_mb",
        lambda path, method="GET", body=None: _comments_response(join_comment) if "comments" in path and method == "GET" else {"success": True},
    )

    result2 = hb.scan_moltbook()

    assert len(verify_attempts_run2) == 1, "confirmation retry must be attempted on run 2, not skipped because dex_register() says _dup"
    assert result2 == 1  # now counted as a successful confirmation

    proc2 = hb._load(hb.PROC_MB).get("comment_ids", [])
    assert "c1" in proc2  # now fully done
    pending2 = hb._load(hb.PENDING_MB)
    assert "c1" not in pending2["registration"]  # cleared from pending

    # Population still exactly 1 -- dex_register()'s idempotency prevented
    # a duplicate pokedex entry, as expected.
    assert hb._load(hb.POKEDEX).get("total") == 1


def test_pending_bounty_claim_retries_with_stored_data_not_bounty_claim_again(monkeypatch, tmp_path):
    """bounty_claim() would return None on a second call (status is no
    longer "open"), which must NOT be misread as "not available" on
    retry. The retry pass must use the info captured at first-attempt
    time, never calling bounty_claim() again."""
    _setup(monkeypatch, tmp_path)
    hb._save(hb.PENDING_MB, {
        "registration": {},
        "bounty_claim": {"c2": {"bid": "b001", "sender": "SomeAgent", "title": "Test bounty"}},
        "bounty_reject": {},
        "bounty_done": {},
    })

    bounty_claim_calls = []
    monkeypatch.setattr(hb, "bounty_claim", lambda *a, **k: bounty_claim_calls.append(a) or None)

    verify_calls = []

    def fake_verify(_post_id, content, parent_id=None):
        verify_calls.append((content, parent_id))
        return {"posted": True, "verified": True}

    monkeypatch.setattr(hb, "_post_comment_verified", fake_verify)
    monkeypatch.setattr(hb, "_mb", lambda path, method="GET", body=None: _comments_response())

    result = hb.scan_moltbook()

    assert bounty_claim_calls == []  # never called again during retry
    assert len(verify_calls) == 1
    assert "Test bounty" in verify_calls[0][0]
    assert verify_calls[0][1] == "c2"
    assert result == 1

    pending = hb._load(hb.PENDING_MB)
    assert "c2" not in pending["bounty_claim"]
    proc = hb._load(hb.PROC_MB).get("comment_ids", [])
    assert "c2" in proc
