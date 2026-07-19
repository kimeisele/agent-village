"""
Tests for the VILLAGE_BRAIN_ENABLED gate on village/heartbeat.py::scan_brain().

Added after Brain fired unintentionally on a real Moltbook comment
(docs/BEFUND.md §12) -- SPEC.md §4 said it should stay disconnected until
explicitly approved, but nothing in code enforced that before this flag,
unlike VILLAGE_NADI_ENABLED which already gated NADI. This mirrors that
pattern.

Note: there is no equivalent dedicated test for VILLAGE_NADI_ENABLED --
that gate lives inline in heartbeat() rather than in a standalone function,
which makes it harder to unit test in isolation (would need mocking
scan_github/scan_moltbook/scan_brain too). Flagging this gap rather than
silently leaving it uncovered.
"""

from __future__ import annotations

import village.heartbeat as hb


def test_disabled_by_default_returns_zero_without_calling_mb(monkeypatch):
    monkeypatch.delenv("VILLAGE_BRAIN_ENABLED", raising=False)
    calls = []
    monkeypatch.setattr(hb, "_mb", lambda *a, **k: calls.append(a) or {"success": True, "comments": []})
    monkeypatch.setattr(hb, "MB", "fake-key")
    monkeypatch.setattr(hb, "REG_POST", "post123")

    result = hb.scan_brain()
    assert result == 0
    assert calls == []  # never even reached the point of fetching comments


def test_enabled_flag_lets_it_proceed_to_fetch_comments(monkeypatch):
    monkeypatch.setenv("VILLAGE_BRAIN_ENABLED", "1")
    calls = []
    monkeypatch.setattr(hb, "_mb", lambda *a, **k: calls.append(a) or {"success": True, "comments": []})
    monkeypatch.setattr(hb, "MB", "fake-key")
    monkeypatch.setattr(hb, "REG_POST", "post123")

    result = hb.scan_brain()
    assert result == 0  # no comments to process, but it DID try
    # 2 calls, not 1: scan_brain() now fetches via
    # _fetch_comments_resilient() (docs/SPEC.md §C.5 -- tolerate the
    # sort=new listing gap documented in MOLTBOOK_CONTRACT_NOTES.md point
    # 7), which queries both sort=new and sort=old and merges by id.
    assert len(calls) == 2


def test_wrong_value_does_not_enable(monkeypatch):
    """Only the exact string "1" enables it -- not "true", not "yes"."""
    monkeypatch.setenv("VILLAGE_BRAIN_ENABLED", "true")
    calls = []
    monkeypatch.setattr(hb, "_mb", lambda *a, **k: calls.append(a) or {"success": True, "comments": []})
    monkeypatch.setattr(hb, "MB", "fake-key")
    monkeypatch.setattr(hb, "REG_POST", "post123")

    result = hb.scan_brain()
    assert result == 0
    assert calls == []
