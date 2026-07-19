"""
Agent Village — village core
=============================
Transport-agnostic village logic, shared by every ingress surface
(village/heartbeat.py::scan_moltbook / scan_github). See docs/SPEC.md §C.

Per SPEC.md §A: surface-specific code (scan_moltbook/scan_github) only
reads a platform payload and normalizes it into a CanonicalIngressEvent.
Everything below this line — name sanitizing, actor-identity resolution,
command classification, dedup, contribution bookkeeping — exists exactly
once, here, called identically from both surfaces (§E.5).
"""

from __future__ import annotations

import hashlib
import re
import time
import unicodedata
from dataclasses import asdict, dataclass, field
from typing import Any

_NAME_MAX_LEN = 40


def sanitize_name(raw: str, fallback: str) -> str:
    """Clean a user-supplied agent name before it's stored or posted back.

    `raw` comes from unauthenticated free text on Moltbook/GitHub (see
    SPEC.md §2.1 "Known limitation"). Strips Unicode control/format
    characters (category "Cc"/"Cf"), then truncates to _NAME_MAX_LEN. Falls
    back to `fallback` (the platform-verified sender/author) if nothing
    usable remains. See docs/BEFUND.md §21 — moved here unchanged from
    village/heartbeat.py per SPEC.md §C.4/§E.5 (was duplicated logic risk,
    now a single implementation).
    """
    cleaned = "".join(ch for ch in raw if unicodedata.category(ch) not in ("Cc", "Cf"))
    cleaned = cleaned.strip()[:_NAME_MAX_LEN].strip()
    return cleaned if cleaned else fallback


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


# ── Canonical ingress event (SPEC.md §C.2) ──────────────────────────────
@dataclass
class CanonicalIngressEvent:
    event_id: str
    surface: str
    external_id: str
    actor_id: str
    display_name: str
    content: str
    content_sha256: str
    received_at: float
    dedup_key: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def moltbook_comment_to_event(comment: dict[str, Any]) -> CanonicalIngressEvent:
    """Normalize a raw Moltbook comment payload (docs/SPEC.md §2.4 shape)
    into a CanonicalIngressEvent. `actor_id` prefers a platform-stable
    author id field if present; Moltbook's observed payloads only carry
    `author.name` today (no numeric/opaque author id has been seen in
    practice), so this falls back to the display name — still stable
    across a single author's repeated comments even though it is not a
    cryptographic identity (see SPEC.md §2.1 "Known limitation", unchanged
    by this slice)."""
    cid = comment.get("id", "")
    author = comment.get("author", {}) or {}
    content = comment.get("content", "") or ""
    actor_id = str(author.get("id") or author.get("user_id") or author.get("name") or "?")
    display_name = author.get("name", "?")
    return CanonicalIngressEvent(
        event_id=f"moltbook:{cid}",
        surface="moltbook",
        external_id=str(cid),
        actor_id=actor_id,
        display_name=display_name,
        content=content,
        content_sha256=sha256_hex(content),
        received_at=time.time(),
        dedup_key=f"moltbook:{cid}",
    )


def github_issue_to_event(issue: dict) -> CanonicalIngressEvent:
    """Normalize a raw GitHub issue payload into a CanonicalIngressEvent.
    `content` combines title + body so the same downstream regexes
    (`[REGISTRATION] ...` / `Agent Name: ...`) that scan_github() already
    applies to title-or-body keep working unchanged."""
    num = issue.get("number", 0)
    user = issue.get("user", {}) or {}
    body = issue.get("body", "") or ""
    title = issue.get("title", "") or ""
    content = f"{title}\n{body}"
    actor_id = str(user.get("id") or user.get("login") or "?")
    display_name = user.get("login", "?")
    return CanonicalIngressEvent(
        event_id=f"github:{num}",
        surface="github",
        external_id=str(num),
        actor_id=actor_id,
        display_name=display_name,
        content=content,
        content_sha256=sha256_hex(content),
        received_at=time.time(),
        dedup_key=f"github:{num}",
    )


# ── Contribution (SPEC.md §C.3) ─────────────────────────────────────────
KIND_JOIN = "join"
KIND_FEATURE = "feature"
KIND_BUG = "bug"
KIND_BOUNTY_CLAIM = "bounty_claim"
KIND_OTHER = "other"

STATUS_RECEIVED = "received"
STATUS_ACCEPTED = "accepted"
STATUS_REJECTED = "rejected"
STATUS_MATERIALIZED = "materialized"


@dataclass
class Contribution:
    contribution_id: str
    source_event_id: str
    kind: str
    status: str = STATUS_RECEIVED
    artifact_refs: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


_FEATURE_PREFIX = re.compile(r"^\s*(feature|suggestion|idea|proposal)\s*:", re.I)
_BUG_PREFIX = re.compile(r"^\s*(bug|fix)\s*:", re.I)


def kw_match(text: str, *keywords: str) -> bool:
    """Word-boundary keyword match — NOT a substring check. Moved here
    unchanged from village/heartbeat.py (docs/BEFUND.md §10)."""
    text_lower = text.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", text_lower) for kw in keywords)


def classify_command(text: str) -> str:
    """Classify raw event content into a Contribution `kind`. The single
    place this decision is made (SPEC.md §E.5) — mirrors the keyword/regex
    logic that used to live inline in scan_moltbook()/scan_github() (join
    keywords, `claim`/`done` bounty regex) plus brain.py's feature:/bug:
    label prefixes."""
    if kw_match(text, "join", "register", "sign up", "add me"):
        return KIND_JOIN
    if re.search(r"\bclaim\s+b\d+", text, re.I) or re.search(r"\bdone\s+b\d+", text, re.I):
        return KIND_BOUNTY_CLAIM
    if _FEATURE_PREFIX.match(text):
        return KIND_FEATURE
    if _BUG_PREFIX.match(text):
        return KIND_BUG
    return KIND_OTHER


def make_contribution(event: CanonicalIngressEvent, kind: str, status: str = STATUS_RECEIVED,
                       artifact_refs: list | None = None) -> Contribution:
    """contribution_id is deterministic (dedup_key + kind), NOT random —
    this is what makes SPEC.md §E.6 ("identical retries never create
    duplicate contributions") mechanically true: a retried event always
    recomputes the same contribution_id, so callers upsert instead of
    append. See heartbeat.py::_record_contribution()."""
    return Contribution(
        contribution_id=f"{event.dedup_key}:{kind}",
        source_event_id=event.event_id,
        kind=kind,
        status=status,
        artifact_refs=list(artifact_refs or []),
    )


# ── Actor identity (SPEC.md §C.1) ───────────────────────────────────────
def legacy_actor_id(name: str) -> str:
    """Deterministic placeholder actor_id for pokedex entries that predate
    actor_id (name-keyed only). Documented decision (SPEC.md §C.1, PR
    description): rather than inventing a random id (which would make the
    migration non-idempotent and non-reviewable in a diff), legacy entries
    get `legacy:<name>` — stable, reproducible, and obviously distinguishable
    from a real platform-sourced actor_id so it can be linked/replaced later
    if the same agent is ever seen again with a real one."""
    return f"legacy:{name}"


def migrate_pokedex(dex: dict) -> tuple[dict, bool]:
    """Ensure every agent entry has an actor_id. Returns (dex, changed).
    Pre-existing entries (e.g. the real B_ClawAssistant registration, which
    predates actor_id entirely) are read and given a deterministic
    `legacy:<name>` id in place — no entry is ever dropped or duplicated.
    See SPEC.md §E.3, tests/test_actor_identity.py."""
    agents = dex.get("agents", [])
    changed = False
    for a in agents:
        if not a.get("actor_id"):
            a["actor_id"] = legacy_actor_id(a.get("name", "?"))
            changed = True
    if changed:
        dex["agents"] = agents
    return dex, changed


def find_agent_by_actor_id(agents: list[dict], actor_id: str) -> dict | None:
    for a in agents:
        if a.get("actor_id") == actor_id:
            return a
    return None
