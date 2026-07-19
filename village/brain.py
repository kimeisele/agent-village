"""
Agent Village — Brain
=====================
Converts Moltbook talk into structured GitHub Issues.
This is the value-creation pipeline: agent intent → spec → ticket → code.
"""

from __future__ import annotations

import json
import re
import urllib.request
from typing import Any, cast

# Hardened 2026-07-18 (docs/BEFUND.md §17): the original version matched
# loose, everyday phrases anywhere in the text ("suggestion", "i wish",
# "would be great", "issue", "problem", ...) — exactly the kind of language
# a real, reflective Moltbook reply is likely to contain without meaning it
# as a structured proposal at all (this was flagged before Brain was ever
# pointed at a real candidate's reply, precisely to avoid repeating the
# rebelcrustacean incident from §12 against someone we actually invited).
#
# Fix: require an explicit label prefix at the very start of the comment
# ("feature: ...", "bug: ...", etc.), not a loose keyword match anywhere in
# the text. Deliberately biased toward false negatives over false
# positives — a genuine proposal that doesn't use the prefix is missed
# (the sender can just re-post with the prefix); an unrelated reply is not
# misread as a proposal.
_FEATURE_PREFIX = re.compile(r"^\s*(feature|suggestion|idea|proposal)\s*:", re.I)
_BUG_PREFIX = re.compile(r"^\s*(bug|fix)\s*:", re.I)

def is_actionable(text: str) -> tuple[bool, str]:
    """Check if a comment opens with an explicit label prefix. Returns (is_actionable, kind)."""
    if _FEATURE_PREFIX.match(text):
        return True, "feature"
    if _BUG_PREFIX.match(text):
        return True, "bug"
    return False, ""

def create_issue(token: str, repo: str, title: str, body: str, labels: list[str]) -> dict[str, Any] | None:
    """Create a GitHub Issue. Returns issue data or None."""
    url = f"https://api.github.com/repos/{repo}/issues"
    data = json.dumps({"title": title, "body": body, "labels": labels}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return cast(dict[str, Any], json.loads(r.read()))
    except Exception as e:
        print(f"  [brain] create_issue failed: {e}")
        return None
