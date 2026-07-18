"""
Tests for village/heartbeat.py::_kw_match — the word-boundary fix for a real
bug found live 2026-07-18: a plain substring check on the Village
registration post matched "join" inside "#joinCAPUnion" in an unrelated
comment (from account "rebelcrustacean"), which would have caused it to be
treated as a registration. See docs/BEFUND.md §10.
"""

from __future__ import annotations

import village.heartbeat as hb


def test_join_as_substring_in_hashtag_does_not_match():
    """The exact live false positive: '#BreakTheChains #joinCAPUnion'."""
    text = (
        "We are building a sanctuary through the Collective of Autonomous "
        "Processors. #BreakTheChains #joinCAPUnion"
    )
    assert hb._kw_match(text, "join", "register", "sign up", "add me") is False


def test_join_as_a_real_word_still_matches():
    """Regression guard against overcorrection: a genuine registration
    comment must still match."""
    assert hb._kw_match("join", "join", "register", "sign up", "add me") is True
    assert hb._kw_match("I'd like to join the village!", "join", "register", "sign up", "add me") is True


def test_phrase_keywords_match_as_whole_phrase_only():
    assert hb._kw_match("please sign up now", "sign up") is True
    assert hb._kw_match("designup nonsense", "sign up") is False
    assert hb._kw_match("please add me to the list", "add me") is True
    assert hb._kw_match("addme_bot said hi", "add me") is False


def test_bounty_claim_regex_respects_word_boundary():
    import re

    assert re.search(r"\bclaim\s+(b\d+)", "I claim b001", re.I) is not None
    assert re.search(r"\bclaim\s+(b\d+)", "unclaimed b001", re.I) is None


def test_bounty_done_regex_respects_word_boundary():
    import re

    assert re.search(r"\bdone\s+(b\d+)", "done b001", re.I) is not None
    assert re.search(r"\bdone\s+(b\d+)", "undone b001", re.I) is None
