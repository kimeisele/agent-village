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
from pathlib import Path

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


def _kw_match(text: str, *keywords: str) -> bool:
    """Word-boundary keyword match — NOT a substring check.

    Fix for a real bug found live 2026-07-18: a plain `kw in text.lower()`
    check matched "join" inside "#joinCAPUnion", causing a comment that had
    nothing to do with registration to be treated as one. \\b on both sides
    works correctly for single words ("join") AND multi-word phrases
    ("sign up") — verified against both cases, not assumed. See
    docs/BEFUND.md §10.
    """
    text_lower = text.lower()
    return any(re.search(rf"\b{re.escape(kw)}\b", text_lower) for kw in keywords)


# ── API helpers ─────────────────────────────────────────
def _load(p: Path) -> dict:
    return json.loads(p.read_text()) if p.exists() else {}


def _save(p: Path, d):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(d, indent=2))


def _api(url, token=None, body=None, method="GET"):
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


def _gh(path, method="GET", body=None):
    return _api(f"https://api.github.com/repos/{REPO}/{path}", GH, body, method)


def _mb(path, method="GET", body=None):
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


def _post_comment_verified(post_id: str, content: str, parent_id: str | None = None) -> dict:
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

    body: dict = {"content": content}
    if parent_id:
        body["parent_id"] = parent_id
    resp = _mb(f"posts/{post_id}/comments", "POST", body)
    if not resp or not resp.get("success"):
        print(f"  [mb] comment post failed: {resp}")
        return {"posted": False, "reason": "post_failed", "response": resp}

    comment = resp.get("comment", {})
    verification = comment.get("verification")
    if not verification:
        # No challenge triggered for this comment.
        return {"posted": True, "verified": True}

    result = solve_and_verify(_mb, verification)
    if result.get("solved"):
        print(f"  [mb] comment verified (llm_fallback={result.get('used_llm_fallback')})")
    else:
        print(f"  [mb] comment posted but NOT verified: {result.get('reason')}")
    return {"posted": True, "verified": bool(result.get("solved")), "verify_result": result}


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


def derive(name: str) -> dict:
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
def dex_register(name: str) -> dict:
    dex = _load(POKEDEX)
    agents = dex.get("agents", [])
    for a in agents:
        if a.get("name") == name:
            a["_dup"] = True
            return a
    ident = derive(name)
    agents.append(ident)
    dex["agents"] = agents
    dex["total"] = len(agents)
    _save(POKEDEX, dex)
    return ident


def dex_list() -> list[dict]:
    return _load(POKEDEX).get("agents", [])


# ── Bounty Board ─────────────────────────────────────────
def bounty_create(title: str, description: str, reward: str = "reputation") -> dict:
    board = _load(BOUNTIES)
    bounties = board.get("bounties", [])
    bid = f"b{len(bounties)+1:03d}"
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
    bounties.append(bounty)
    board["bounties"] = bounties
    _save(BOUNTIES, board)
    return bounty


def bounty_list(status: str = "open") -> list[dict]:
    return [b for b in _load(BOUNTIES).get("bounties", []) if b.get("status") == status]


def bounty_claim(bid: str, agent: str) -> dict | None:
    board = _load(BOUNTIES)
    for b in board.get("bounties", []):
        if b["id"] == bid and b["status"] == "open":
            b["status"] = "claimed"
            b["claimed_by"] = agent
            b["claimed_at"] = time.time()
            _save(BOUNTIES, board)
            return b
    return None


def bounty_complete(bid: str) -> dict | None:
    board = _load(BOUNTIES)
    for b in board.get("bounties", []):
        if b["id"] == bid and b["status"] == "claimed":
            b["status"] = "done"
            b["completed_at"] = time.time()
            _save(BOUNTIES, board)
            return b
    return None


# ── GitHub Scanner ───────────────────────────────────────
def scan_github() -> int:
    proc = set(_load(PROC_GH).get("issues", []))
    issues = _gh("issues?labels=registration,pending&state=open&per_page=10")
    if not issues:
        return 0
    c = 0
    for iss in issues:
        num = iss.get("number", 0)
        if num in proc:
            continue
        t = iss.get("title", "")
        m = re.search(r"\[REGISTRATION\]\s*(.+)", t)
        if not m:
            body = iss.get("body", "") or ""
            m = re.search(r"Agent Name[:\s]+([^\n]+)", body)
        if not m:
            continue
        name = m.group(1).strip()
        ident = dex_register(name)
        if ident.get("_dup"):
            continue
        _gh(
            f"issues/{num}/comments",
            "POST",
            {
                "body": f"🦞 **{name}** registered! {ident['element']}/{ident['zone']}/{ident['guardian']}. Pop: {_load(POKEDEX).get('total',0)}\n\nOpen bounties: {len(bounty_list())}"
            },
        )
        proc.add(num)
        c += 1
        print(f"  [gh] {name} #{num}")
    _save(PROC_GH, {"issues": list(proc)})
    return c


# ── Moltbook Scanner ─────────────────────────────────────
def scan_moltbook() -> int:
    if not MB:
        print("  [mb] no key")
        return 0
    if not REG_POST:
        print("  [mb] MB_REG_POST not configured — skipping (see docs/SPEC.md §1)")
        return 0
    proc = set(_load(PROC_MB).get("comment_ids", []))
    resp = _mb(f"posts/{REG_POST}/comments?sort=new&limit=50")
    if not resp or not resp.get("success"):
        return 0
    c = 0
    for cmt in resp.get("comments", []):
        cid = cmt.get("id", "")
        if cid in proc:
            continue
        text = cmt.get("content", "")
        author = cmt.get("author", {})
        sender = author.get("name", "?")

        # Retry policy (Option A, docs/BEFUND.md §8/§9): cid is only added
        # to `proc` (marked done, never seen again) once a verify-gated
        # reply is confirmed verified, or there was nothing to verify in
        # the first place. If the reply's challenge fails/is skipped, cid
        # is left OUT of proc so this same comment is retried next cycle.
        # dex_register()/bounty_claim()/bounty_complete() are idempotent
        # (dup checks), so re-running them on retry is safe.

        # --- Registration intent ---
        if _kw_match(text, "join", "register", "sign up", "add me"):
            m = re.search(r"name[:\s]+([^\n]+)", text, re.I)
            name = m.group(1).strip() if m else sender
            ident = dex_register(name)
            if ident.get("_dup"):
                # Already registered under this name — nothing left to
                # retry, no reply was ever attempted for this comment.
                proc.add(cid)
                continue
            result = _post_comment_verified(
                REG_POST,
                f"🦞 **{name}** registered! {ident['element']}/{ident['zone']}/{ident['guardian']}. Pop: {_load(POKEDEX).get('total',0)} | Open bounties: {len(bounty_list())}",
                parent_id=cid,
            )
            if result.get("verified"):
                proc.add(cid)
                c += 1
                print(f"  [mb] reg {name} via {sender}")
            else:
                print(f"  [mb] reg {name} via {sender} — reply not verified ({result.get('reason')}), retrying next cycle")
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
                print(f"  [mb] bounty claim disabled pending approval — skipping (retries once VILLAGE_BOUNTIES_ENABLED=1): {cid}")
                continue
            bid = m.group(1)
            result = bounty_claim(bid, sender)
            if result:
                reply = _post_comment_verified(
                    REG_POST,
                    f"🦞 **{sender}** claimed bounty `{bid}`: {result['title']}",
                    parent_id=cid,
                )
                if reply.get("verified"):
                    proc.add(cid)
                    c += 1
                    print(f"  [mb] bounty {bid} claimed by {sender}")
                else:
                    print(f"  [mb] bounty {bid} claim reply not verified ({reply.get('reason')}), retrying next cycle")
            else:
                reply = _post_comment_verified(
                    REG_POST,
                    f"❌ Bounty `{bid}` not available (already claimed or not found).",
                    parent_id=cid,
                )
                if reply.get("verified"):
                    proc.add(cid)
                else:
                    print(f"  [mb] bounty {bid} rejection reply not verified ({reply.get('reason')}), retrying next cycle")
            continue

        # --- Bounty done ---
        m = re.search(r"\bdone\s+(b\d+)", text, re.I)
        if m:
            if os.environ.get("VILLAGE_BOUNTIES_ENABLED") != "1":
                print(f"  [mb] bounty done disabled pending approval — skipping (retries once VILLAGE_BOUNTIES_ENABLED=1): {cid}")
                continue
            bid = m.group(1)
            result = bounty_complete(bid)
            if result:
                reply = _post_comment_verified(
                    REG_POST,
                    f"✅ Bounty `{bid}` complete: {result['title']} — claimed by {result['claimed_by']}",
                    parent_id=cid,
                )
                if reply.get("verified"):
                    proc.add(cid)
                    c += 1
                    print(f"  [mb] bounty {bid} done by {sender}")
                else:
                    print(f"  [mb] bounty {bid} done reply not verified ({reply.get('reason')}), retrying next cycle")
            else:
                # bounty_complete() found nothing to complete — no reply
                # attempted, nothing to retry.
                proc.add(cid)
            continue

        # No recognized intent in this comment — nothing to act on, never
        # needs to be retried.
        proc.add(cid)

    _save(PROC_MB, {"comment_ids": list(proc)})
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

    resp = _mb(f"posts/{REG_POST}/comments?sort=new&limit=50")
    if not resp or not resp.get("success"):
        return 0

    c = 0
    for cmt in resp.get("comments", []):
        cid = cmt.get("id", "")
        if cid not in proc or cid in done:
            continue
        text = cmt.get("content", "")
        # Skip registration/bounty comments (already handled)
        if _kw_match(text, "join", "register", "claim", "done", "sign up"):
            continue

        try:
            from village.brain import is_actionable, create_issue
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
                        print(f"  [brain] notification reply not verified ({reply.get('reason')}) — not retried, issue already created")
        except ImportError:
            pass

    return c


# ── State ────────────────────────────────────────────────
def update_state():
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
def heartbeat():
    print(f"=== Village Heartbeat === {time.strftime('%Y-%m-%d %H:%M:%S')}")
    _load_challenge_monitor_state()
    gh = scan_github()
    mb = scan_moltbook()
    br = scan_brain()
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
    print(f"  Done — GH:{gh} MB:{mb} Brain:{br} Nadi:{nadi} Pop:{pop} Bounties:{bo}o/{bc}c")
    return gh + mb + br + nadi


if __name__ == "__main__":
    heartbeat()
