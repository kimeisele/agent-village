"""
Tests for village/village_core.py's canonical ingress event model
(docs/SPEC.md §C.2, §E.4-§E.5).
"""

from __future__ import annotations

import village.heartbeat as hb
from village.village_core import (
    CanonicalIngressEvent,
    github_issue_to_event,
    moltbook_comment_to_event,
    sanitize_name,
)

# =============================================================================
# §E.4 — a Moltbook comment and a GitHub issue produce the same event schema
# =============================================================================


def test_both_surfaces_produce_the_same_event_field_set():
    mb_event = moltbook_comment_to_event({
        "id": "c1", "content": "join name: Alice", "author": {"name": "Alice"},
    })
    gh_event = github_issue_to_event({
        "number": 7, "title": "[REGISTRATION] Bob", "body": "",
        "user": {"login": "Bob"},
    })

    assert isinstance(mb_event, CanonicalIngressEvent)
    assert isinstance(gh_event, CanonicalIngressEvent)
    assert set(mb_event.to_dict().keys()) == set(gh_event.to_dict().keys()) == {
        "event_id", "surface", "external_id", "actor_id", "display_name",
        "content", "content_sha256", "received_at", "dedup_key",
    }


def test_moltbook_comment_to_event_fields():
    event = moltbook_comment_to_event({
        "id": "c1", "content": "join name: Alice", "author": {"name": "Alice"},
    })
    assert event.surface == "moltbook"
    assert event.external_id == "c1"
    assert event.actor_id == "Alice"
    assert event.display_name == "Alice"
    assert event.content == "join name: Alice"
    assert event.dedup_key == "moltbook:c1"


def test_github_issue_to_event_fields():
    event = github_issue_to_event({
        "number": 7, "title": "[REGISTRATION] Bob", "body": "Agent Name: Bob",
        "user": {"login": "Bob"},
    })
    assert event.surface == "github"
    assert event.external_id == "7"
    assert event.actor_id == "Bob"
    assert event.display_name == "Bob"
    assert "[REGISTRATION] Bob" in event.content
    assert "Agent Name: Bob" in event.content
    assert event.dedup_key == "github:7"


def test_moltbook_actor_id_prefers_platform_id_field_over_name():
    event = moltbook_comment_to_event({
        "id": "c1", "content": "hi", "author": {"id": "u_999", "name": "Alice"},
    })
    assert event.actor_id == "u_999"
    assert event.display_name == "Alice"


# =============================================================================
# §E.5 — sanitizing/dedup logic exists exactly once, in the shared core
# =============================================================================


def test_heartbeat_sanitize_name_is_the_core_implementation():
    """heartbeat._sanitize_name must be the SAME function object as
    village_core.sanitize_name, not a reimplementation — this is what
    makes "only one implementation" mechanically checkable, not just
    a documentation claim."""
    assert hb._sanitize_name is sanitize_name


def test_heartbeat_kw_match_is_the_core_implementation():
    from village.village_core import kw_match
    assert hb._kw_match is kw_match
