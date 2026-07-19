"""
Tests for docs/SPEC.md §E.6 — identical retries never create duplicate
contributions/artifacts.
"""

from __future__ import annotations

import village.heartbeat as hb
from village.village_core import moltbook_comment_to_event


def test_record_contribution_upserts_not_appends(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "CONTRIBUTIONS", tmp_path / "contributions.json")
    event = moltbook_comment_to_event(
        {
            "id": "c1",
            "content": "join",
            "author": {"name": "Alice"},
        }
    )

    hb._record_contribution(event, "join", hb.STATUS_RECEIVED)
    hb._record_contribution(event, "join", hb.STATUS_RECEIVED)  # identical retry
    hb._record_contribution(event, "join", hb.STATUS_MATERIALIZED)  # later, same event+kind

    store = hb._load(hb.CONTRIBUTIONS)
    contributions = store["contributions"]
    assert len(contributions) == 1  # never duplicated
    only = list(contributions.values())[0]
    assert only["status"] == "materialized"  # last write wins, in place


def test_different_events_produce_different_contribution_ids(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "CONTRIBUTIONS", tmp_path / "contributions.json")
    e1 = moltbook_comment_to_event({"id": "c1", "content": "join", "author": {"name": "Alice"}})
    e2 = moltbook_comment_to_event({"id": "c2", "content": "join", "author": {"name": "Bob"}})

    hb._record_contribution(e1, "join", hb.STATUS_RECEIVED)
    hb._record_contribution(e2, "join", hb.STATUS_RECEIVED)

    store = hb._load(hb.CONTRIBUTIONS)
    assert len(store["contributions"]) == 2


def test_identical_registration_retry_via_scan_moltbook_yields_one_contribution(monkeypatch, tmp_path):
    """End-to-end: the same unverified registration comment scanned twice
    (first-encounter, then a retry cycle) must not produce two
    contributions for the same underlying event -- whether because the
    retry pass doesn't re-record it, or (see
    test_record_contribution_upserts_not_appends) because a re-record
    with the same contribution_id would upsert in place either way."""
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
        hb,
        "_mb",
        lambda path, method="GET", body=None: (
            {"success": True, "comments": [join_comment]}
            if "comments" in path and method == "GET"
            else {"success": True}
        ),
    )
    monkeypatch.setattr(
        hb,
        "_post_comment_verified",
        lambda *a, **k: {"posted": True, "verified": False, "reason": "verify_rejected"},
    )

    hb.scan_moltbook()  # first encounter -> pending, contribution "received"
    hb.scan_moltbook()  # retry pass -- must NOT create a second contribution

    store = hb._load(hb.CONTRIBUTIONS)
    assert len(store["contributions"]) == 1


def test_registration_confirmed_on_retry_reaches_materialized(monkeypatch, tmp_path):
    """Fund 1 (Kim's independent review of PR #3): the first-encounter
    path calls _record_contribution(..., STATUS_MATERIALIZED) on success,
    but the retry pass previously didn't touch the contribution record at
    all -- a comment whose confirmation reply failed on first attempt and
    succeeded on retry (the exact, common BEFUND §15 scenario) left its
    Contribution stuck on "received" forever, even though the underlying
    action was fully done. Must now reach "materialized"."""
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
        hb,
        "_mb",
        lambda path, method="GET", body=None: (
            {"success": True, "comments": [join_comment]}
            if "comments" in path and method == "GET"
            else {"success": True}
        ),
    )

    # Run 1: reply not verified -> pending, contribution "received".
    monkeypatch.setattr(
        hb,
        "_post_comment_verified",
        lambda *a, **k: {"posted": True, "verified": False, "reason": "verify_rejected"},
    )
    hb.scan_moltbook()

    store = hb._load(hb.CONTRIBUTIONS)
    assert len(store["contributions"]) == 1
    contribution_id = list(store["contributions"].keys())[0]
    assert store["contributions"][contribution_id]["status"] == "received"

    # Run 2 (retry pass): reply now verifies.
    monkeypatch.setattr(hb, "_post_comment_verified", lambda *a, **k: {"posted": True, "verified": True})
    result = hb.scan_moltbook()
    assert result == 1

    store = hb._load(hb.CONTRIBUTIONS)
    assert len(store["contributions"]) == 1  # same contribution, not a new one
    assert store["contributions"][contribution_id]["status"] == "materialized"
    assert store["contributions"][contribution_id]["artifact_refs"]  # non-empty


def test_bounty_claim_confirmed_on_retry_reaches_materialized(monkeypatch, tmp_path):
    """Same bug class as above, for the bounty_claim retry branch."""
    monkeypatch.setattr(hb, "CHALLENGE_STATE", tmp_path / "challenge_failures.json")
    monkeypatch.setattr(hb, "PROC_MB", tmp_path / "processed_comments.json")
    monkeypatch.setattr(hb, "PENDING_MB", tmp_path / "pending_confirmations.json")
    monkeypatch.setattr(hb, "CONTRIBUTIONS", tmp_path / "contributions.json")
    monkeypatch.setattr(hb, "MB", "fake-key")
    monkeypatch.setattr(hb, "REG_POST", "post123")
    hb._save(
        hb.PENDING_MB,
        {
            "registration": {},
            "bounty_claim": {"c2": {"bid": "b001", "sender": "SomeAgent", "title": "Test bounty", "attempts": 1}},
            "bounty_reject": {},
            "bounty_done": {},
        },
    )
    monkeypatch.setattr(hb, "_mb", lambda path, method="GET", body=None: {"success": True, "comments": []})
    monkeypatch.setattr(hb, "_post_comment_verified", lambda *a, **k: {"posted": True, "verified": True})

    result = hb.scan_moltbook()
    assert result == 1

    store = hb._load(hb.CONTRIBUTIONS)
    assert len(store["contributions"]) == 1
    only = list(store["contributions"].values())[0]
    assert only["status"] == "materialized"
    assert only["kind"] == "bounty_claim"
