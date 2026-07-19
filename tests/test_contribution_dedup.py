"""
Tests for docs/SPEC.md §E.6 — identical retries never create duplicate
contributions/artifacts.
"""

from __future__ import annotations

import village.heartbeat as hb
from village.village_core import moltbook_comment_to_event


def test_record_contribution_upserts_not_appends(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "CONTRIBUTIONS", tmp_path / "contributions.json")
    event = moltbook_comment_to_event({
        "id": "c1", "content": "join", "author": {"name": "Alice"},
    })

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
        hb, "_mb",
        lambda path, method="GET", body=None: (
            {"success": True, "comments": [join_comment]} if "comments" in path and method == "GET" else {"success": True}
        ),
    )
    monkeypatch.setattr(
        hb, "_post_comment_verified",
        lambda *a, **k: {"posted": True, "verified": False, "reason": "verify_rejected"},
    )

    hb.scan_moltbook()  # first encounter -> pending, contribution "received"
    hb.scan_moltbook()  # retry pass -- must NOT create a second contribution

    store = hb._load(hb.CONTRIBUTIONS)
    assert len(store["contributions"]) == 1
