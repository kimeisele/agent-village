"""
Agent Village — Heartbeat
=========================
The village pulse. Runs every 15 minutes.
Scans: registrations, bounty claims, task updates.
"""

from __future__ import annotations

import json
import os
import re
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Any

from village._types import load_json_object
from village.contracts import Budget, ContractState, SuccessCriterion, VillageContract
from village.village_core import (
    STATUS_ACCEPTED,
    STATUS_MATERIALIZED,
    STATUS_RECEIVED,
    STATUS_REJECTED,
    CanonicalIngressEvent,
    classify_command,
    find_agent_by_actor_id,
    github_issue_to_event,
    kw_match,
    legacy_actor_id,
    make_contribution,
    migrate_pokedex,
    moltbook_comment_to_event,
    sanitize_name,
    sha256_hex,
)

# ── Config ──────────────────────────────────────────────
REPO = os.environ.get("GITHUB_REPOSITORY", "kimeisele/agent-village")
VILLAGE = "agent-village"
DIR = Path("data/village")
POKEDEX = DIR / "pokedex.json"
BOUNTIES = DIR / "bounties.json"
STATE = DIR / "state.json"
PROC_GH = DIR / "processed_issues.json"
PROC_MB = DIR / "processed_comments.json"
CHALLENGE_STATE = DIR / "challenge_failures.json"
CONTRIBUTIONS = DIR / "contributions.json"
# Governance layer for claimed/completed bounties (docs/SPEC.md §C.3.1,
# village/contracts.py). Only touched by bounty_claim()/bounty_complete()
# — bounty_create() is not a production integration point (nothing calls
# it; bounties are created outside the code). Budget/deadline stay
# unconstrained (None) here — no ingress path supplies that data yet,
# see docs/research/VILLAGE_CONTRACTS_01.md.
CONTRACTS = DIR / "contracts.json"
# Records the Moltbook comment id returned by every successful POST,
# written immediately after the POST (before/regardless of verify) — see
# docs/SPEC.md §C.5. There is no GET /comments/{id} endpoint (docs/
# MOLTBOOK_CONTRACT_NOTES.md point 9) and a verified comment has been
# observed to be absent from every listing for minutes (point 8), so this
# is the only reliable local record that a given reply was actually
# created server-side.
REPLY_COMMENT_IDS = DIR / "reply_comment_ids.json"
# Tracks comments where the underlying action (dex_register/bounty_claim/
# bounty_complete) already succeeded, but the confirmation reply has not
# yet been verified. Deliberately separate from PROC_MB/proc — see
# docs/BEFUND.md §14. Do NOT use dex_register()'s idempotent "_dup" return
# (or bounty_claim() returning None because status is no longer "open") to
# decide whether a comment still needs a confirmation retry: both of those
# are true for "already fully handled" AND for "action succeeded, reply
# still pending" alike, which is exactly the bug this file exists to avoid.
PENDING_MB = DIR / "pending_confirmations.json"

GH = os.environ.get("GITHUB_TOKEN", "") or os.environ.get("GH_TOKEN", "")
MB = ""
_c = Path.home() / ".config" / "moltbook" / "credentials.json"
if _c.exists():
    try:
        MB = json.loads(_c.read_text()).get("api_key", "")
    except Exception:
        pass
MB = os.environ.get("MOLTBOOK_API_KEY", MB)

# No fallback: the old default (f6175b7f-...) is a different repo's Agent
# City recruiting post, not a village registration post. A silent fallback
# to the wrong post is worse than a clear failure. Must be set via a GitHub
# Actions repo variable/env var.
REG_POST = os.environ.get("MB_REG_POST", "")


# _kw_match/_sanitize_name used to be defined here directly; both are now
# single implementations in village/village_core.py (docs/SPEC.md §C.4/
# §E.5 — "sanitizing/dedup logic exists only once, in the core"). Kept as
# module-level names for backward compatibility (existing tests reference
# `heartbeat._kw_match`/`heartbeat._sanitize_name` directly).
_kw_match = kw_match
_sanitize_name = sanitize_name


def _retry_suffix(attempts: int) -> str:
    """Small, honest suffix to make a retried confirmation reply's text
    unique. FIX (docs/BEFUND.md §15, real bug found live 2026-07-18):
    Moltbook deduplicates identical comment content and silently returns
    the OLD (already-failed) comment instead of creating a new one with a
    fresh challenge — a byte-identical retry can therefore never succeed,
    no matter how many times it's attempted. `attempts` is 0 on the very
    first try (no suffix, keeps the common case clean) and >=1 on each
    retry attempt."""
    if attempts <= 0:
        return ""
    return f" (attempt {attempts + 1})"


# ── API helpers ─────────────────────────────────────────
def _load(p: Path) -> dict[str, Any]:
    if not p.exists():
        return {}
    return dict(load_json_object(p.read_text()))


def _save(p: Path, data: dict[str, Any]) -> None:
    """Write-to-temp-then-atomic-replace (docs/research/
    BOUNTY_REVIEW_GATE_01.md Blocker 3): protects each individual file
    against a process crash mid-write leaving behind truncated/corrupt
    JSON. `Path.replace()` is an atomic rename on POSIX. This does NOT
    make a multi-file sequence (e.g. bounty_review()'s submission ->
    contract -> bounty writes) atomic as a whole -- that remains a
    documented, accepted limitation -- but it does eliminate the worse
    failure mode of a single half-written file that won't even parse.
    """
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(f"{p.name}.tmp{os.getpid()}")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(p)


def _api(url: str, token: str | None = None, body: dict[str, Any] | None = None, method: str = "GET") -> Any:
    if not token:
        return None
    h = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    data = json.dumps(body).encode() if body else None
    if body:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        # FIX (docs/BEFUND.md §12): the response body is the actual
        # diagnostic (e.g. Moltbook's "Incorrect answer" rejection reason)
        # and was previously discarded — only the generic "HTTP Error 400:
        # Bad Request" repr was logged, making a failed verify call
        # unrecoverable to debug after the fact (the challenge itself is
        # single-use/expires within minutes).
        try:
            detail = e.read().decode("utf-8", errors="replace")
        except Exception:
            detail = "(could not read error body)"
        print(f"  [api] {e} — body: {detail[:500]}")
        return None
    except Exception as e:
        print(f"  [api] {e}")
        return None


def _gh(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    return _api(f"https://api.github.com/repos/{REPO}/{path}", GH, body, method)


def _mb(path: str, method: str = "GET", body: dict[str, Any] | None = None) -> Any:
    return _api(f"https://www.moltbook.com/api/v1/{path}", MB, body, method)


def _load_challenge_monitor_state() -> None:
    """Restore ChallengeMonitor counters from data/village/challenge_failures.json
    at the start of a heartbeat run, so BAN_THRESHOLD (10) can accumulate
    across cycles instead of resetting every fresh process. See
    docs/BEFUND.md §9."""
    from village.moltbook_captcha import get_challenge_monitor

    state = _load(CHALLENGE_STATE)
    if state:
        get_challenge_monitor().load_state(state)


def _save_challenge_monitor_state() -> None:
    """Persist ChallengeMonitor counters at the end of a heartbeat run.
    If consecutive_failures has reached BAN_THRESHOLD, sets a sticky
    "banned" flag and emits a GitHub Actions ::error:: annotation. Does
    NOT disable the workflow itself — that stays a human decision. Once
    "banned" is set it is never cleared automatically, even by a later
    success; only a manual edit of the state file clears it."""
    from village.moltbook_captcha import ChallengeMonitor, get_challenge_monitor

    monitor = get_challenge_monitor()
    prev = _load(CHALLENGE_STATE)
    state = monitor.to_state()
    state["banned"] = bool(prev.get("banned", False))
    if state["consecutive_failures"] >= ChallengeMonitor.BAN_THRESHOLD and not state["banned"]:
        state["banned"] = True
        print(
            f"::error::Challenge monitor reached BAN_THRESHOLD "
            f"({ChallengeMonitor.BAN_THRESHOLD}) consecutive failures across "
            f"cycles. Registration/bounty comments will be refused until "
            f"{CHALLENGE_STATE} is manually reset (set banned:false or delete it)."
        )
    _save(CHALLENGE_STATE, state)


def _post_comment_verified(post_id: str, content: str, parent_id: str | None = None) -> dict[str, Any]:
    """Post a Moltbook comment and, if it triggers a verify challenge
    (see docs/BEFUND.md §3), solve it automatically via
    village/moltbook_captcha.py and submit the answer before the comment
    counts as done.

    Two halt layers:
    1. In-process: ChallengeMonitor.is_halted (5+ consecutive failures
       this run — resets every fresh process/cycle).
    2. Cross-cycle: the persisted "banned" flag in challenge_failures.json
       (10+ consecutive failures accumulated across runs — see
       _save_challenge_monitor_state(), docs/BEFUND.md §9). Checked first,
       since it should block even before this cycle's monitor is consulted.
    """
    from village.moltbook_captcha import get_challenge_monitor, solve_and_verify

    if _load(CHALLENGE_STATE).get("banned"):
        print(f"  [mb] challenge monitor BANNED (cross-cycle, persisted) — refusing comment: {content[:50]!r}")
        return {"posted": False, "reason": "banned_cross_cycle"}

    monitor = get_challenge_monitor()
    if monitor.is_halted:
        print(f"  [mb] challenge monitor halted this cycle, skipping comment: {content[:50]!r}")
        return {"posted": False, "reason": "monitor_halted"}

    body: dict[str, Any] = {"content": content}
    if parent_id:
        body["parent_id"] = parent_id
    resp = _mb(f"posts/{post_id}/comments", "POST", body)
    if not resp or not resp.get("success"):
        print(f"  [mb] comment post failed: {resp}")
        return {"posted": False, "reason": "post_failed", "response": resp}

    comment = resp.get("comment", {})
    comment_id = comment.get("id")
    if comment_id:
        # Persisted immediately, independent of verification outcome
        # (docs/SPEC.md §C.5): there is no GET /comments/{id} (contract
        # notes point 9) and a verified comment has been observed absent
        # from every listing for minutes (point 8), so this local record
        # is the only reliable evidence a reply was actually created.
        _record_comment_id(comment_id, post_id, parent_id)
    verification_status = comment.get("verification_status")
    verification = comment.get("verification")

    if verification_status == "verified":
        # Already verified without us doing anything this call — some
        # comments apparently don't require a challenge at all.
        return {"posted": True, "verified": True, "comment_id": comment_id}

    if not verification:
        # FIX (docs/BEFUND.md §15, real bug found live 2026-07-18): a
        # missing `verification` object does NOT mean "no challenge was
        # needed, treat as verified". Moltbook's duplicate-content
        # detection ("already_existed": true) returns the OLD comment —
        # verification_status often "failed" or "pending" from a PAST
        # attempt — without a fresh verification object to solve. Only
        # verification_status == "verified" (checked above) counts as
        # success; anything else here is NOT verified, even without a
        # fresh challenge to act on.
        print(
            f"  [mb] comment returned without a fresh challenge "
            f"(verification_status={verification_status!r}, "
            f"already_existed={comment.get('already_existed')}) — treating as NOT verified"
        )
        return {
            "posted": True,
            "verified": False,
            "reason": f"no_fresh_challenge_status_{verification_status}",
            "comment_id": comment_id,
        }

    result = solve_and_verify(_mb, verification)
    if result.get("solved"):
        print(f"  [mb] comment verified (llm_fallback={result.get('used_llm_fallback')})")
    else:
        print(f"  [mb] comment posted but NOT verified: {result.get('reason')}")
    return {
        "posted": True,
        "verified": bool(result.get("solved")),
        "verify_result": result,
        "comment_id": comment_id,
    }


def _record_comment_id(comment_id: str, post_id: str, parent_id: str | None) -> None:
    store = _load(REPLY_COMMENT_IDS)
    ids = store.get("comment_ids", [])
    ids.append(
        {
            "comment_id": comment_id,
            "post_id": post_id,
            "parent_id": parent_id,
            "recorded_at": time.time(),
        }
    )
    store["comment_ids"] = ids[-200:]
    _save(REPLY_COMMENT_IDS, store)


# ── Identity ─────────────────────────────────────────────
_EL = {
    "a": "akasha",
    "e": "akasha",
    "h": "akasha",
    "g": "akasha",
    "i": "vayu",
    "c": "vayu",
    "j": "vayu",
    "y": "vayu",
    "s": "vayu",
    "r": "agni",
    "n": "jala",
    "l": "jala",
    "d": "jala",
    "t": "jala",
    "z": "jala",
    "m": "prithvi",
    "p": "prithvi",
    "b": "prithvi",
    "v": "prithvi",
    "w": "prithvi",
    "f": "prithvi",
    "o": "prithvi",
    "u": "prithvi",
}
_ZN = ["discovery", "governance", "engineering", "research"]
_GD = {
    "discovery": ["brahma", "vyasa", "shambhu", "narada"],
    "governance": ["manu", "kumaras", "prithu", "prahlada"],
    "research": ["nrisimha", "shuka", "bali", "yamaraja"],
    "engineering": ["parashurama", "bhishma", "prahlada", "kumaras"],
}


def derive(name: str) -> dict[str, Any]:
    low = name.lower().lstrip("_")
    el = _EL.get(low[0] if low else "a", "akasha")
    seed = sum(ord(c) for c in name)
    zi, gi, gu = seed % 4, seed % 4, seed % 3
    z = _ZN[zi]
    return {
        "name": name,
        # "observed" not "verified"/"resident" — no identity binding happens
        # yet (name is unauthenticated). See docs/ARCHITECTURE_VISION.md §5
        # for the OBSERVED -> CLAIMED -> VERIFIED -> RESIDENT ladder.
        "status": "observed",
        "element": el,
        "zone": z,
        "guardian": _GD[z][gi],
        "guna": ["SATTVA", "RAJAS", "TAMAS"][gu],
        "seed": seed,
        "registered_at": time.time(),
    }


# ── Pokedex ──────────────────────────────────────────────
def _load_pokedex() -> dict[str, Any]:
    """Load pokedex.json, migrating any pre-actor_id entries in place
    (docs/SPEC.md §C.1/§E.3). Migration is idempotent — re-running it on
    an already-migrated file is a no-op (no `changed`, no write)."""
    dex = _load(POKEDEX)
    dex, changed = migrate_pokedex(dex)
    if changed:
        _save(POKEDEX, dex)
    return dex


def dex_register(name: str, actor_id: str | None = None) -> dict[str, Any]:
    """Register (or return the existing entry for) an agent, keyed by
    `actor_id` (docs/SPEC.md §C.1) — NOT by display name. Two different
    actor_ids that happen to choose the same display name get two separate
    entries (§E.1); the same actor_id re-registering under a new display
    name updates the existing entry in place rather than creating a
    duplicate (§E.2).

    `actor_id=None` is the legacy call shape (e.g. the retry pass in
    scan_moltbook(), which persisted only a name before this slice) — it
    falls back to the same deterministic `legacy:<name>` placeholder used
    by the pokedex migration, so old and new call sites agree on identity
    for the same name.
    """
    if actor_id is None:
        actor_id = legacy_actor_id(name)
    dex = _load_pokedex()
    agents = dex.get("agents", [])
    existing = find_agent_by_actor_id(agents, actor_id)
    if existing:
        if existing.get("name") != name:
            existing["name"] = name
            _save(POKEDEX, dex)
        existing["_dup"] = True
        return existing
    ident = derive(name)
    ident["actor_id"] = actor_id
    agents.append(ident)
    dex["agents"] = agents
    dex["total"] = len(agents)
    _save(POKEDEX, dex)
    return ident


def dex_list() -> list[dict[str, Any]]:
    agents_raw = _load_pokedex().get("agents", [])
    return agents_raw if isinstance(agents_raw, list) else []


# ── Bounty Board ─────────────────────────────────────────
def bounty_create(
    title: str,
    description: str,
    reward: str = "reputation",
    contract_terms: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """`contract_terms`, if given, is stored verbatim on the bounty and
    parsed by `bounty_claim()` via the existing `_parse_contract_terms()`
    -- no separate creation-time validation, same atomic-rejection-on-
    claim behavior as a manually-authored bounty with `contract_terms`
    already had."""
    board = _load(BOUNTIES)
    bounties = board.get("bounties", [])
    bid = f"b{len(bounties) + 1:03d}"
    bounty = {
        "id": bid,
        "title": title,
        "description": description,
        "reward": reward,
        "status": "open",
        "created_by": VILLAGE,
        "created_at": time.time(),
        "claimed_by": None,
        "claimed_at": None,
        "completed_at": None,
    }
    if contract_terms is not None:
        bounty["contract_terms"] = contract_terms
    bounties.append(bounty)
    board["bounties"] = bounties
    _save(BOUNTIES, board)
    return bounty


def bounty_list(status: str = "open") -> list[dict[str, Any]]:
    return [b for b in _load(BOUNTIES).get("bounties", []) if b.get("status") == status]


def _contract_id_for(bid: str) -> str:
    return f"contract:{bid}:1"


def _load_contract(contract_id: str) -> VillageContract | None:
    store = _load(CONTRACTS)
    data = store.get("contracts", {}).get(contract_id)
    return VillageContract.from_dict(data) if data else None


def _save_contract(contract: VillageContract) -> None:
    store = _load(CONTRACTS)
    contracts = store.get("contracts", {})
    contracts[contract.contract_id] = contract.to_dict()
    store["contracts"] = contracts
    _save(CONTRACTS, store)


def _parse_contract_terms(terms: dict[str, Any]) -> tuple[list[str], Budget, "datetime | None", list[SuccessCriterion]]:
    """Parse a bounty's optional `contract_terms` field into the existing
    village/contracts.py types -- no second schema, no new validation.
    `Budget`/`SuccessCriterion` validate themselves at construction
    (raise ValueError on e.g. a negative budget or an out-of-range
    weight); a malformed deadline string raises ValueError via
    `datetime.fromisoformat()`. Every sub-field of `contract_terms` is
    optional. Raises on any invalid input -- the caller MUST call this
    BEFORE mutating any bounty/contract state (docs/SPEC.md §C.3.1
    atomicity requirement: a rejected claim must never leave a partial
    state behind)."""
    allowed_resources = list(terms.get("allowed_resources", []))
    budget = Budget.from_dict(terms["budget"]) if terms.get("budget") else Budget()
    deadline_raw = terms.get("deadline")
    deadline = datetime.fromisoformat(deadline_raw) if deadline_raw else None
    success_criteria = [SuccessCriterion.from_untrusted_terms(c) for c in terms.get("success_criteria", [])]
    return allowed_resources, budget, deadline, success_criteria


def bounty_claim(bid: str, agent: str) -> dict[str, Any] | None:
    board = _load(BOUNTIES)
    for b in board.get("bounties", []):
        if b["id"] == bid and b["status"] == "open":
            allowed_resources: list[str] = []
            budget = Budget()
            deadline = None
            success_criteria: list[SuccessCriterion] = []

            terms = b.get("contract_terms")
            if terms:
                # Construct BEFORE any mutation. A malformed
                # contract_terms rejects the claim atomically -- same
                # return semantics as "bid not found" (None), no bounty
                # state change, no contracts.json write.
                try:
                    allowed_resources, budget, deadline, success_criteria = _parse_contract_terms(terms)
                except (ValueError, TypeError) as e:
                    print(f"  [contracts] {bid} claim rejected: invalid contract_terms ({e})")
                    return None

            b["status"] = "claimed"
            b["claimed_by"] = agent
            b["claimed_at"] = time.time()
            _save(BOUNTIES, board)

            # Governance layer (docs/SPEC.md §C.3.1): create-or-load the
            # bounty's VillageContract, activate it. Without
            # contract_terms, budget/deadline/success_criteria stay
            # unconstrained/empty exactly as before (docs/research/
            # VILLAGE_CONTRACTS_01.md) -- this path is unchanged.
            contract_id = _contract_id_for(bid)
            contract = _load_contract(contract_id) or VillageContract(
                contract_id=contract_id,
                title=b["title"],
                description=b["description"],
                allowed_resources=allowed_resources,
                budget=budget,
                deadline=deadline,
                success_criteria=success_criteria,
            )
            if contract.state == ContractState.DRAFTED:
                contract.activate()
            _save_contract(contract)

            if not isinstance(b, dict):
                raise ValueError(f"bounty record is not a dict: {type(b).__name__}")
            return b
    return None


def bounty_complete(bid: str) -> dict[str, Any] | None:
    """LEGACY, deliberately narrowed (docs/research/
    BOUNTY_REVIEW_GATE_01.md): since the submit/review gate
    (village/bounty_review.py) exists, no normal path may move a bounty
    directly from `claimed` to `done` anymore -- that would silently
    bypass review, which is exactly what this gate exists to prevent.
    This function now refuses that transition unconditionally (same
    `None` return as "bid not found" -- no bounty/contract state
    change), for BOTH callers of this function: any future direct
    caller, and the existing `scan_moltbook()` "done bXXX" comment path
    (village/heartbeat.py), which as a side effect of this change no
    longer completes a bounty from a bare chat comment -- real
    completion now requires actual submitted work evidence via
    `village.bounty_review.bounty_submit()` +
    `village.bounty_review.bounty_review()`. See docs/BEFUND.md §32 for
    the explicit behavior-change note.

    Real completion path: `village.bounty_review.bounty_submit()` then
    `village.bounty_review.bounty_review(..., decision="accept")` --
    only that path may set a bounty to `done` or call
    `contract.fulfill()`.
    """
    board = _load(BOUNTIES)
    for b in board.get("bounties", []):
        if b["id"] == bid and b["status"] == "claimed":
            print(
                f"  [contracts] bounty_complete({bid}) refused -- direct claimed->done is "
                "no longer supported. Use bounty_review.bounty_submit()/bounty_review() instead."
            )
            return None
    return None


# ── Contributions (docs/SPEC.md §C.3/§C.4) ──────────────────────────────
def _record_contribution(
    event: CanonicalIngressEvent, kind: str, status: str, artifact_refs: list[str] | None = None
) -> None:
    """Upsert a Contribution keyed by its deterministic contribution_id
    (dedup_key + kind) — an identical retry of the same event/kind recomputes
    the same id and overwrites in place instead of appending, so identical
    retries never create duplicate contributions (SPEC.md §E.6)."""
    contribution = make_contribution(event, kind, status, artifact_refs)
    store = _load(CONTRIBUTIONS)
    contributions = store.get("contributions", {})
    contributions[contribution.contribution_id] = contribution.to_dict()
    store["contributions"] = contributions
    _save(CONTRIBUTIONS, store)


# ── GitHub Scanner ───────────────────────────────────────
def scan_github() -> int:
    """Surface-specific: reads GitHub issues and normalizes each into a
    CanonicalIngressEvent (village_core.github_issue_to_event). Identity
    resolution (dex_register with actor_id) and contribution bookkeeping
    are the shared core's job, not this function's (SPEC.md §C.4)."""
    proc = set(_load(PROC_GH).get("issues", []))
    issues = _gh("issues?labels=registration,pending&state=open&per_page=10")
    if not issues:
        return 0
    c = 0
    for iss in issues:
        num = iss.get("number", 0)
        if num in proc:
            continue
        event = github_issue_to_event(iss)
        m = re.search(r"\[REGISTRATION\]\s*(.+)", iss.get("title", ""))
        if not m:
            m = re.search(r"Agent Name[:\s]+([^\n]+)", iss.get("body", "") or "")
        if not m:
            continue
        name = sanitize_name(m.group(1), event.display_name)
        ident = dex_register(name, event.actor_id)
        if ident.get("_dup"):
            _record_contribution(event, classify_command(event.content), STATUS_ACCEPTED)
            continue
        _gh(
            f"issues/{num}/comments",
            "POST",
            {
                "body": f"🦞 **{name}** registered! {ident['element']}/{ident['zone']}/{ident['guardian']}. Pop: {_load(POKEDEX).get('total', 0)}\n\nOpen bounties: {len(bounty_list())}"  # noqa: E501 — Moltbook announcement template
            },
        )
        _record_contribution(
            event, classify_command(event.content), STATUS_MATERIALIZED, artifact_refs=[f"pokedex:{event.actor_id}"]
        )
        proc.add(num)
        c += 1
        print(f"  [gh] {name} #{num}")
    _save(PROC_GH, {"issues": list(proc)})
    return c


# ── Moltbook Scanner ─────────────────────────────────────
def _empty_pending() -> dict[str, Any]:
    return {"registration": {}, "bounty_claim": {}, "bounty_reject": {}, "bounty_done": {}, "bounty_done_reject": {}}


def _retry_event(cid: str, actor_id: str, display_name: str) -> CanonicalIngressEvent:
    """Reconstruct a CanonicalIngressEvent for a comment being confirmed
    via the retry pass, which only has the small `info` dict captured at
    first-encounter time (docs/BEFUND.md §14), not the original raw
    comment payload. `content`/`content_sha256` are placeholders (empty
    string) -- the retry pass only needs this event to call
    _record_contribution() with the correct dedup_key (`moltbook:{cid}`),
    which depends only on `surface`+`external_id`, not on content."""
    return CanonicalIngressEvent(
        event_id=f"moltbook:{cid}",
        surface="moltbook",
        external_id=cid,
        actor_id=actor_id,
        display_name=display_name,
        content="",
        content_sha256=sha256_hex(""),
        received_at=time.time(),
        dedup_key=f"moltbook:{cid}",
    )


def _fetch_comments_resilient(post_id: str) -> list[dict[str, Any]]:
    """Fetch a post's comments tolerating the two listing gaps documented
    in docs/MOLTBOOK_CONTRACT_NOTES.md points 7/8: `sort=new` has been
    observed to (temporarily or, once, durably) not show a just-created
    comment that `sort=old` does show. Fetches both and merges by id
    (sort=new first, since it's the common case and typically sufficient;
    sort=old only contributes ids missing from the first list) rather than
    relying on a single sort order. Does not solve point 8's fully-invisible
    case (nothing server-side to fetch in that scenario) but does close the
    much more common "wrong sort order missed it" gap."""
    seen: dict[str, dict[str, Any]] = {}
    for sort in ("new", "old"):
        resp = _mb(f"posts/{post_id}/comments?sort={sort}&limit=50")
        if not resp or not resp.get("success"):
            continue
        for cmt in resp.get("comments", []):
            cid = cmt.get("id", "")
            if cid and cid not in seen:
                seen[cid] = cmt
    return list(seen.values())


def scan_moltbook() -> int:
    if not MB:
        print("  [mb] no key")
        return 0
    if not REG_POST:
        print("  [mb] MB_REG_POST not configured — skipping (see docs/SPEC.md §1)")
        return 0
    proc = set(_load(PROC_MB).get("comment_ids", []))
    pending = _load(PENDING_MB) or _empty_pending()
    for kind in ("registration", "bounty_claim", "bounty_reject", "bounty_done", "bounty_done_reject"):
        pending.setdefault(kind, {})

    c = 0

    # ── Retry pass: comments whose underlying action already succeeded
    # last cycle, but whose confirmation reply was never verified. Uses
    # the data captured at first-attempt time (docs/BEFUND.md §14) —
    # NOT dex_register()/bounty_claim() again, since both are idempotent
    # and would misreport "already done" for a comment that is actually
    # still awaiting its first successful confirmation.
    for cid, info in list(pending["registration"].items()):
        name = info["name"]
        actor_id = info.get(
            "actor_id"
        )  # None for entries pending from before C.1 — falls back to legacy_actor_id(name) in dex_register
        attempts = info.get("attempts", 1)
        ident = dex_register(name, actor_id)  # idempotent; just need element/zone/guardian again
        result = _post_comment_verified(
            REG_POST,
            f"🦞 **{name}** registered! {ident['element']}/{ident['zone']}/{ident['guardian']}. Pop: {_load(POKEDEX).get('total', 0)} | Open bounties: {len(bounty_list())}{_retry_suffix(attempts)}",  # noqa: E501 — retry announcement template
            parent_id=cid,
        )
        if result.get("verified"):
            proc.add(cid)
            del pending["registration"][cid]
            c += 1
            _record_contribution(
                _retry_event(cid, actor_id or legacy_actor_id(name), name),
                "join",
                STATUS_MATERIALIZED,
                artifact_refs=[f"pokedex:{actor_id or legacy_actor_id(name)}"],
            )
            print(f"  [mb] reg {name} confirmed on retry")
        else:
            pending["registration"][cid]["attempts"] = attempts + 1
            print(f"  [mb] reg {name} still not verified ({result.get('reason')}), retrying next cycle")

    for cid, info in list(pending["bounty_claim"].items()):
        attempts = info.get("attempts", 1)
        actor_id = info.get("actor_id", legacy_actor_id(info.get("sender", "?")))
        display_name = info.get("sender", "?")
        result = _post_comment_verified(
            REG_POST,
            f"🦞 **{display_name}** claimed bounty `{info['bid']}`: {info['title']}{_retry_suffix(attempts)}",
            parent_id=cid,
        )
        if result.get("verified"):
            proc.add(cid)
            del pending["bounty_claim"][cid]
            c += 1
            _record_contribution(
                _retry_event(cid, actor_id, display_name),
                "bounty_claim",
                STATUS_MATERIALIZED,
                artifact_refs=[f"bounty:{info['bid']}"],
            )
            print(f"  [mb] bounty {info['bid']} claim confirmed on retry")
        else:
            pending["bounty_claim"][cid]["attempts"] = attempts + 1
            print(f"  [mb] bounty {info['bid']} claim still not verified ({result.get('reason')}), retrying next cycle")

    for cid, info in list(pending["bounty_reject"].items()):
        attempts = info.get("attempts", 1)
        result = _post_comment_verified(
            REG_POST,
            f"❌ Bounty `{info['bid']}` not available (already claimed or not found).{_retry_suffix(attempts)}",
            parent_id=cid,
        )
        if result.get("verified"):
            proc.add(cid)
            del pending["bounty_reject"][cid]
            _record_contribution(
                _retry_event(cid, legacy_actor_id(cid), "?"),
                "bounty_claim",
                STATUS_REJECTED,
            )
        else:
            pending["bounty_reject"][cid]["attempts"] = attempts + 1
            print(
                f"  [mb] bounty {info['bid']} rejection still not verified ({result.get('reason')}), retrying next cycle"  # noqa: E501 — log line, broken form harder to grep
            )

    for cid, info in list(pending["bounty_done"].items()):
        attempts = info.get("attempts", 1)
        result = _post_comment_verified(
            REG_POST,
            f"✅ Bounty `{info['bid']}` complete: {info['title']} — claimed by {info['claimed_by']}{_retry_suffix(attempts)}",  # noqa: E501 — Moltbook announcement template
            parent_id=cid,
        )
        if result.get("verified"):
            proc.add(cid)
            del pending["bounty_done"][cid]
            c += 1
            _record_contribution(
                _retry_event(cid, legacy_actor_id(info["claimed_by"]), info["claimed_by"]),
                "bounty_claim",
                STATUS_MATERIALIZED,
                artifact_refs=[f"bounty:{info['bid']}:done"],
            )
            print(f"  [mb] bounty {info['bid']} done confirmed on retry")
        else:
            pending["bounty_done"][cid]["attempts"] = attempts + 1
            print(f"  [mb] bounty {info['bid']} done still not verified ({result.get('reason')}), retrying next cycle")

    for cid, info in list(pending["bounty_done_reject"].items()):
        attempts = info.get("attempts", 1)
        result = _post_comment_verified(
            REG_POST,
            f"❌ Bounty `{info['bid']}` cannot be completed directly — the `done` command is no longer supported. "
            f"Completion now requires: (1) submit work via operator execution, "
            f"(2) the submission is reviewed via the review gate. "
            f"See the repository documentation for details.{_retry_suffix(attempts)}",
            parent_id=cid,
        )
        if result.get("verified"):
            proc.add(cid)
            del pending["bounty_done_reject"][cid]
            c += 1
            print(f"  [mb] legacy done {info['bid']} rejection confirmed on retry")
        else:
            pending["bounty_done_reject"][cid]["attempts"] = attempts + 1
            print(
                f"  [mb] legacy done {info['bid']} rejection still not verified ({result.get('reason')}), retrying next cycle"
            )

    comments = _fetch_comments_resilient(REG_POST)
    if not comments:
        _save(PROC_MB, {"comment_ids": list(proc)})
        _save(PENDING_MB, pending)
        return c

    already_pending = (
        set(pending["registration"])
        | set(pending["bounty_claim"])
        | set(pending["bounty_reject"])
        | set(pending["bounty_done"])
        | set(pending["bounty_done_reject"])
    )

    for cmt in comments:
        cid = cmt.get("id", "")
        if cid in proc or cid in already_pending:
            continue

        # Surface-specific step ends here: normalize into the canonical
        # event once, then work only with `event` (SPEC.md §C.4). `sender`
        # kept as a local alias of event.display_name for the existing
        # reply-text f-strings below.
        event = moltbook_comment_to_event(cmt)
        text = event.content
        sender = event.display_name

        # First-encounter policy (docs/BEFUND.md §14): if the action
        # (dex_register/bounty_claim/bounty_complete) succeeds but the
        # confirmation reply is not verified, the comment goes into
        # `pending`, NOT back to being re-scanned as "new" next cycle —
        # the retry pass above handles it from here on, using the
        # already-captured info dict, never re-deciding based on
        # dex_register()'s/bounty_claim()'s idempotent return value.

        # --- Registration intent ---
        if classify_command(text) == "join":
            m = re.search(r"name[:\s]+([^\n]+)", text, re.I)
            name = sanitize_name(m.group(1), sender) if m else sender
            ident = dex_register(name, event.actor_id)
            if ident.get("_dup"):
                # Genuinely already registered under this actor_id by a
                # PRIOR, different comment — nothing to retry for THIS
                # cid, no reply was ever attempted for it.
                proc.add(cid)
                _record_contribution(event, "join", STATUS_ACCEPTED)
                continue
            result = _post_comment_verified(
                REG_POST,
                f"🦞 **{name}** registered! {ident['element']}/{ident['zone']}/{ident['guardian']}. Pop: {_load(POKEDEX).get('total', 0)} | Open bounties: {len(bounty_list())}",  # noqa: E501 — Moltbook announcement template
                parent_id=cid,
            )
            if result.get("verified"):
                proc.add(cid)
                c += 1
                _record_contribution(event, "join", STATUS_MATERIALIZED, artifact_refs=[f"pokedex:{event.actor_id}"])
                print(f"  [mb] reg {name} via {sender}")
            else:
                pending["registration"][cid] = {"name": name, "actor_id": event.actor_id, "attempts": 1}
                _record_contribution(event, "join", STATUS_RECEIVED)
                print(
                    f"  [mb] reg {name} via {sender} — reply not verified ({result.get('reason')}), retrying next cycle"
                )
            continue

        # --- Bounty claim ---
        m = re.search(r"\bclaim\s+(b\d+)", text, re.I)
        if m:
            if os.environ.get("VILLAGE_BOUNTIES_ENABLED") != "1":
                # Gated (docs/BEFUND.md §13): same risk class as Brain was
                # before it was gated -- exercised by real external agents,
                # explicitly out of scope for v1 (SPEC.md §4). Left OUT of
                # `proc` on purpose, not marked done, so this comment is
                # retried automatically once the flag is turned on later —
                # no need for the agent to comment again.
                print(
                    f"  [mb] bounty claim disabled pending approval — skipping (retries once VILLAGE_BOUNTIES_ENABLED=1): {cid}"  # noqa: E501 — log line, broken form harder to grep
                )
                continue
            bid = m.group(1)
            claim_result = bounty_claim(bid, event.actor_id)
            if claim_result:
                reply = _post_comment_verified(
                    REG_POST,
                    f"🦞 **{sender}** claimed bounty `{bid}`: {claim_result['title']}",
                    parent_id=cid,
                )
                if reply.get("verified"):
                    proc.add(cid)
                    c += 1
                    _record_contribution(event, "bounty_claim", STATUS_MATERIALIZED, artifact_refs=[f"bounty:{bid}"])
                    print(f"  [mb] bounty {bid} claimed by {sender}")
                else:
                    pending["bounty_claim"][cid] = {
                        "bid": bid,
                        "sender": sender,
                        "actor_id": event.actor_id,
                        "title": claim_result["title"],
                        "attempts": 1,
                    }
                    _record_contribution(event, "bounty_claim", STATUS_RECEIVED)
                    print(f"  [mb] bounty {bid} claim reply not verified ({reply.get('reason')}), retrying next cycle")
            else:
                reply = _post_comment_verified(
                    REG_POST,
                    f"❌ Bounty `{bid}` not available (already claimed or not found).",
                    parent_id=cid,
                )
                if reply.get("verified"):
                    proc.add(cid)
                    _record_contribution(event, "bounty_claim", STATUS_REJECTED)
                else:
                    pending["bounty_reject"][cid] = {"bid": bid, "attempts": 1}
                    print(
                        f"  [mb] bounty {bid} rejection reply not verified ({reply.get('reason')}), retrying next cycle"
                    )
            continue

        # --- Bounty done ---
        m = re.search(r"\bdone\s+(b\d+)", text, re.I)
        if m:
            if os.environ.get("VILLAGE_BOUNTIES_ENABLED") != "1":
                print(
                    f"  [mb] bounty done disabled pending approval — skipping (retries once VILLAGE_BOUNTIES_ENABLED=1): {cid}"  # noqa: E501 — log line, broken form harder to grep
                )
                continue
            bid = m.group(1)
            complete_result = bounty_complete(bid)
            if complete_result:
                reply = _post_comment_verified(
                    REG_POST,
                    f"✅ Bounty `{bid}` complete: {complete_result['title']} — claimed by {complete_result['claimed_by']}",
                    parent_id=cid,
                )
                if reply.get("verified"):
                    proc.add(cid)
                    c += 1
                    _record_contribution(
                        event, "bounty_claim", STATUS_MATERIALIZED, artifact_refs=[f"bounty:{bid}:done"]
                    )
                    print(f"  [mb] bounty {bid} done by {sender}")
                else:
                    pending["bounty_done"][cid] = {
                        "bid": bid,
                        "title": complete_result["title"],
                        "claimed_by": complete_result["claimed_by"],
                        "attempts": 1,
                    }
                    print(f"  [mb] bounty {bid} done reply not verified ({reply.get('reason')}), retrying next cycle")
            else:
                # bounty_complete() is disabled — the legacy "done bXXX"
                # command is no longer a valid completion path. Post an
                # explicit rejection reply directing the actor to use
                # Submission + Review instead of silently consuming it.
                reply = _post_comment_verified(
                    REG_POST,
                    f"❌ Bounty `{bid}` cannot be completed directly — the `done` command is no longer supported. "
                    f"Completion now requires: (1) submit work via operator execution, "
                    f"(2) the submission is reviewed via the review gate. "
                    f"See the repository documentation for details.",
                    parent_id=cid,
                )
                if reply.get("verified"):
                    proc.add(cid)
                    _record_contribution(event, "bounty_claim", STATUS_REJECTED)
                    print(f"  [mb] legacy done {bid} rejected with migration guidance")
                else:
                    pending["bounty_done_reject"][cid] = {"bid": bid, "attempts": 1}
                    print(
                        f"  [mb] legacy done {bid} rejection reply not verified "
                        f"({reply.get('reason')}), retrying next cycle"
                    )
            continue

        # No recognized intent in this comment — nothing to act on, never
        # needs to be retried.
        proc.add(cid)

    _save(PROC_MB, {"comment_ids": list(proc)})
    _save(PENDING_MB, pending)
    return c


# ── Brain ─────────────────────────────────────────────────
def scan_brain() -> int:
    """Convert Moltbook talk into GitHub Issues. The value-creation pipeline.

    Gated behind VILLAGE_BRAIN_ENABLED=1 (default off), same pattern as
    VILLAGE_NADI_ENABLED. Added after Brain fired unintentionally on a
    real Moltbook comment 2026-07-18 (docs/BEFUND.md §12) — SPEC.md §4
    said Brain "stays disconnected until explicitly greenlit", but nothing
    in code actually enforced that before this flag.
    """
    if os.environ.get("VILLAGE_BRAIN_ENABLED") != "1":
        print("  [brain] disabled pending explicit approval — skipping")
        return 0
    if not MB:
        return 0
    if not REG_POST:
        return 0
    proc = set(_load(PROC_MB).get("comment_ids", []))
    brain_proc = _load(DIR / "brain_processed.json")
    done = set(brain_proc.get("issues", {}).keys())

    comments = _fetch_comments_resilient(REG_POST)
    if not comments:
        return 0

    c = 0
    for cmt in comments:
        cid = cmt.get("id", "")
        if cid not in proc or cid in done:
            continue
        text = cmt.get("content", "")
        # Skip registration/bounty comments (already handled)
        if _kw_match(text, "join", "register", "claim", "done", "sign up"):
            continue

        try:
            from village.brain import create_issue, is_actionable

            actionable, kind = is_actionable(text)
            if actionable:
                title = text.split("\n")[0].strip()[:80]
                body = (
                    f"**Source:** Moltbook comment\n"
                    f"**Kind:** {kind}\n\n"
                    f"---\n{text}\n---\n"
                    f"*Auto-created by Agent Village Brain.*"
                )
                issue = create_issue(GH, REPO, title, body, ["village-request", kind])
                if issue:
                    # Mark done immediately, BEFORE attempting the reply:
                    # create_issue() is NOT idempotent (no dup check), so
                    # unlike registration/bounty actions this must not be
                    # retried — retrying would create duplicate GitHub
                    # issues. The reply notification itself is therefore
                    # best-effort, not retried, if its challenge fails.
                    brain_proc.setdefault("issues", {})[cid] = issue.get("number", 0)
                    _save(DIR / "brain_processed.json", brain_proc)
                    reply = _post_comment_verified(
                        REG_POST,
                        f"🧠 **Brain:** Created issue #{issue.get('number')} — {title}",
                        parent_id=cid,
                    )
                    c += 1
                    print(f"  [brain] Issue #{issue.get('number')}: {title}")
                    if not reply.get("verified"):
                        print(
                            f"  [brain] notification reply not verified ({reply.get('reason')}) — not retried, issue already created"  # noqa: E501 — log line, broken form harder to grep
                        )
        except ImportError:
            pass

    return c


# ── Review Requests ───────────────────────────────────────
REVIEW_REQUESTS = DIR / "review_requests.json"


_REVIEW_REQUEST_MARKER = "<!-- agent-village-review-request:submission_id="


def _make_review_marker(submission_id: str) -> str:
    return f"{_REVIEW_REQUEST_MARKER}{submission_id} -->"


def _find_existing_review_issue(submission_id: str) -> dict[str, Any] | None:
    """Search for an existing review-request Issue by its marker.

    Searches GitHub for issues containing the exact HTML marker, then
    fetches each candidate's body to verify the marker is genuinely
    present (not just a search-index coincidence).  Returns the issue
    dict (with ``number``, ``html_url``) only when the exact marker is
    confirmed in the body, or ``None``.
    """
    expected_marker = _make_review_marker(submission_id)
    for state in ("open", "closed"):
        results = _gh(f"search/issues?q={_quote(expected_marker)}+state:{state}")
        if not isinstance(results, dict):
            continue
        items = results.get("items", [])
        if not isinstance(items, list):
            continue
        for candidate in items:
            if not isinstance(candidate, dict):
                continue
            issue_number = candidate.get("number")
            if not issue_number:
                continue
            # Fetch the full issue to inspect the body
            full = _gh(f"issues/{issue_number}")
            if not isinstance(full, dict):
                continue
            body = full.get("body", "")
            if not isinstance(body, str):
                continue
            if expected_marker in body:
                return {
                    "issue_number": issue_number,
                    "issue_url": full.get("html_url", ""),
                }
    return None


def _quote(text: str) -> str:
    """Minimal URL quoting for GitHub search."""
    return (
        text.replace(" ", "+")
        .replace(":", "%3A")
        .replace("=", "%3D")
        .replace("<", "%3C")
        .replace(">", "%3E")
        .replace("!", "%21")
    )


def _validate_review_mapping(mapping: dict[str, Any], sid: str) -> None:
    """Fail closed on malformed mapping entries.

    Raises ``ValueError`` if the entry for *sid* exists but is not a
    dict, is missing ``issue_number``, has a non-positive
    ``issue_number``, or lacks an ``issue_url`` string.
    """
    entry = mapping.get(sid)
    if entry is None:
        return
    if not isinstance(entry, dict):
        raise ValueError(f"review_requests entry for {sid!r} is not a dict: {type(entry).__name__}")
    issue_number = entry.get("issue_number")
    if not isinstance(issue_number, int) or issue_number <= 0:
        raise ValueError(f"review_requests entry for {sid!r} has invalid issue_number: {issue_number!r}")
    if not isinstance(entry.get("issue_url"), str):
        raise ValueError(f"review_requests entry for {sid!r} has missing or non-string issue_url")
    stored_sid = entry.get("submission_id")
    if stored_sid is not None and stored_sid != sid:
        raise ValueError(f"review_requests entry for {sid!r} has mismatched stored submission_id: {stored_sid!r}")


def publish_pending_review_requests() -> int:
    """Detect unreviewed submissions and create GitHub Issues for each.

    Reads ``data/village/bounty_submissions.json``, finds submissions
    whose ``review`` field is ``None``, and creates exactly one GitHub
    Issue per ``submission_id``.  Each Issue carries a stable
    machine-readable HTML marker for crash-safe server-side
    reconciliation.

    The mapping is persisted **immediately** after each successful POST
    or reconciliation.  Before creating or trusting an Issue the
    function fetches the candidate body to verify the exact marker.
    Malformed local mappings raise ``ValueError`` (fail closed).

    Does NOT evaluate evidence, complete bounties, call bounty_review(),
    or mutate Bounty/Contract state.
    """
    from village.bounty_review import _load_submissions

    store = _load_submissions()
    submissions = store.get("submissions", {})
    if not isinstance(submissions, dict):
        return 0

    mapping = _load(REVIEW_REQUESTS)
    if not isinstance(mapping, dict):
        mapping = {}

    # Fail closed on malformed existing entries
    for sid in list(mapping.keys()):
        _validate_review_mapping(mapping, sid)

    # Reconcile: check every unmapped unreviewed submission against GitHub
    for sid, sub in submissions.items():
        if not isinstance(sub, dict):
            continue
        if sub.get("review") is not None:
            continue
        if sid in mapping:
            continue

        existing = _find_existing_review_issue(sid)
        if existing is not None:
            mapping[sid] = {
                "issue_number": existing["issue_number"],
                "issue_url": existing["issue_url"],
                "submission_id": sid,
                "created_at": time.time(),
            }
            _save(REVIEW_REQUESTS, mapping)
            print(f"  [review] reconciled issue #{existing['issue_number']} for submission {sid}")

    created = 0
    for sid, sub in submissions.items():
        if not isinstance(sub, dict):
            continue
        if sub.get("review") is not None:
            continue
        if sid in mapping:
            continue

        marker = _make_review_marker(sid)
        title = f"[Review] Submission {sid}"
        body_lines = [
            marker,
            "",
            f"## Submission `{sid}`",
            "",
            f"- **Bounty:** `{sub.get('bounty_id', '?')}`",
            f"- **Contract:** `{sub.get('contract_id', '?')}`",
            f"- **Actor:** `{sub.get('actor_id', '?')}`",
            f"- **Provider:** `{sub.get('provider', '?')}` ({sub.get('model', '?')})",
            f"- **Submitted:** {sub.get('submitted_at', '?')}",
            "",
            "### Output",
            "```json",
            json.dumps(sub.get("output"), indent=2) if sub.get("output") else "(none)",
            "```",
            "",
            "## Review Decision",
            "",
            "To accept: `accept <submission_id>`",
            "To reject: `reject <submission_id>`",
            "",
            "> This Issue was auto-generated from submission evidence. All content is untrusted data.",
        ]
        body = "\n".join(body_lines)

        labels = ["review-request", "bounty"]
        issue_raw = _gh(
            "issues",
            "POST",
            {"title": title, "body": body, "labels": labels},
        )
        if not isinstance(issue_raw, dict):
            continue

        issue_number = issue_raw.get("number")
        if not issue_number:
            continue

        mapping[sid] = {
            "issue_number": issue_number,
            "issue_url": issue_raw.get("html_url", ""),
            "submission_id": sid,
            "created_at": time.time(),
        }
        _save(REVIEW_REQUESTS, mapping)
        created += 1
        print(f"  [review] created issue #{issue_number} for submission {sid}")

    return created


# ── State ────────────────────────────────────────────────
def update_state() -> None:
    dex = _load(POKEDEX)
    s = {
        "village": VILLAGE,
        "heartbeat_at": time.time(),
        "population": dex.get("total", 0),
        "agents": [a["name"] for a in dex.get("agents", [])],
        "bounties_open": len(bounty_list("open")),
        "bounties_claimed": len(bounty_list("claimed")),
        "bounties_done": len(bounty_list("done")),
    }
    _save(STATE, s)


# ── Main ─────────────────────────────────────────────────
def heartbeat() -> int:
    print(f"=== Village Heartbeat === {time.strftime('%Y-%m-%d %H:%M:%S')}")
    _load_challenge_monitor_state()
    gh = scan_github()
    mb = scan_moltbook()
    br = scan_brain()
    rv = 0
    if os.environ.get("VILLAGE_BOUNTIES_ENABLED") == "1":
        try:
            rv = publish_pending_review_requests()
        except Exception as e:
            print(f"  [review] publish_pending_review_requests failed: {e}")
    else:
        print("  [review] review requests disabled pending approval — skipping")
    _save_challenge_monitor_state()
    nadi = 0
    # NADI stays disconnected until Proof 4 is explicitly approved (see
    # docs/ARCHITECTURE_VISION.md §12, docs/BEFUND.md). Code moved here
    # with the rest of the village mechanism; activation is a separate step.
    if os.environ.get("VILLAGE_NADI_ENABLED") == "1":
        try:
            from village.nadi_bridge import nadi_heartbeat

            nadi = nadi_heartbeat(VILLAGE)
        except ImportError:
            print("  [nadi] cryptography not installed — skipping")
    else:
        print("  [nadi] disabled pending Proof 4 approval — skipping")
    update_state()
    pop = _load(POKEDEX).get("total", 0)
    bo = len(bounty_list("open"))
    bc = len(bounty_list("claimed"))
    print(f"  Done — GH:{gh} MB:{mb} Brain:{br} Review:{rv} Nadi:{nadi} Pop:{pop} Bounties:{bo}o/{bc}c")
    return gh + mb + br + rv + nadi


if __name__ == "__main__":
    heartbeat()
