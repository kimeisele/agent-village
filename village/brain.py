"""
Agent Village — Brain
=====================
Converts Moltbook talk into structured GitHub Issues.
This is the value-creation pipeline: agent intent → spec → ticket → code.
"""

from __future__ import annotations

import json
import re
import time
import urllib.request
from pathlib import Path

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

def extract_title(text: str) -> str:
    """Extract a short title from the comment text."""
    # First line or first 80 chars
    raw_lines = [line.strip() for line in text.split("\n") if line.strip() and not line.strip().startswith("#")]
    if raw_lines:
        title = raw_lines[0][:80]
        if len(raw_lines[0]) > 80:
            title += "..."
        return title
    return text[:80]

def create_issue(token: str, repo: str, title: str, body: str, labels: list[str]) -> dict | None:
    """Create a GitHub Issue. Returns issue data or None."""
    url = f"https://api.github.com/repos/{repo}/issues"
    data = json.dumps({"title": title, "body": body, "labels": labels}).encode()
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github.v3+json")
    req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except Exception as e:
        print(f"  [brain] create_issue failed: {e}")
        return None

def process_comment(comment: dict, token: str, repo: str,
                    processed_path: Path) -> dict | None:
    """Process one Moltbook comment. If actionable, create a GitHub Issue."""
    cid = comment.get("id", "")
    text = comment.get("content", "")
    author = comment.get("author", {})
    sender = author.get("name", "unknown")

    actionable, kind = is_actionable(text)
    if not actionable:
        return None

    title = extract_title(text)
    body = (
        f"**Source:** Moltbook comment by [{sender}](https://www.moltbook.com/u/{sender})\n"
        f"**Kind:** {kind}\n"
        f"**Detected at:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n\n"
        f"---\n\n"
        f"{text}\n\n"
        f"---\n"
        f"*This issue was auto-created by the Agent Village Brain.*"
    )
    labels = ["village-request", kind]

    issue = create_issue(token, repo, title, body, labels)
    if issue:
        # Track processed
        proc = json.loads(processed_path.read_text()) if processed_path.exists() else {}
        issues = proc.get("issues", {})
        issues[cid] = issue.get("number", 0)
        proc["issues"] = issues
        processed_path.parent.mkdir(parents=True, exist_ok=True)
        processed_path.write_text(json.dumps(proc, indent=2))
        print(f"  [brain] Created issue #{issue.get('number')}: {title}")
        return issue

    return None
