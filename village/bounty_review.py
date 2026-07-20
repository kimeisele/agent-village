"""
Agent Village — Bounty Submission & Review Gate
=================================================
Closes the loop opened by village/worker.py's WorkResult (docs/research/
AGENT_LOOP_WORKER_02.md): a worker execution's output is submitted
evidence, never an authoritative completion. This module is the ONLY
place a bounty may move `claimed -> submitted -> done`, and the ONLY
place a `VillageContract` may be `fulfill()`ed as a result of worker
output -- neither `village/worker.py` nor `village/interpreter.py` can
reach this module (SPEC.md §A.5, enforced via AST inspection,
tests/test_worker_no_write_authority.py).

Lifecycle (minimal review state adapted from docs/research/
NIGHTFORGE_DESIGN_NOTE_01.md's ticket state machine, per docs/BEFUND.md
§32): `open -> claimed -> submitted -> done`. `village/heartbeat.py::
bounty_complete()` can no longer move a bounty directly from `claimed`
to `done` -- see its own docstring.

No reputation tiers, no automatic LLM reviewer, no multi-reviewer
quorum, no appeals -- a review decision here is made by an explicit,
human-authorized caller (or a future, separately-designed automation),
never by the cognitive worker itself.
"""

from __future__ import annotations

import hashlib
import re
import time
from pathlib import Path
from typing import Any

import village.heartbeat as heartbeat
from village.contracts import (
    ContractState,
    VillageContract,
    canonical_json_dumps,
    compute_review_policy_hash,
)
from village.heartbeat import _contract_id_for, _load, _load_contract, _save, _save_contract
from village.work_result import WorkResult, WorkResultStatus


def validate_submission_bindings(submission: dict[str, Any], contract: VillageContract) -> list[str]:
    """Pure, non-mutating validation of submission review bindings.

    Returns a list of reason codes for violations. Empty list = valid.
    Never performs review or mutation.
    """
    reasons: list[str] = []
    required_str = [
        "submission_id",
        "bounty_id",
        "contract_id",
        "contract_version",
        "work_result_id",
        "execution_id",
        "output_canonical_hash",
        "review_policy_hash",
    ]
    for f in required_str:
        val = submission.get(f)
        if not isinstance(val, str) or not val:
            reasons.append(f"missing_or_invalid:{f}")
            return reasons

    cids = submission.get("criterion_ids")
    chashes = submission.get("criterion_definition_hashes")
    if not isinstance(cids, list) or not isinstance(chashes, list):
        reasons.append("missing_or_invalid:criterion_ids_or_hashes")
        return reasons
    if len(cids) != len(chashes):
        reasons.append("criterion_id_hash_count_mismatch")
        return reasons

    if submission["contract_id"] != contract.contract_id:
        reasons.append("contract_id_mismatch")
    if submission["contract_version"] != contract.version:
        reasons.append("contract_version_mismatch")
    expected_cids = [c.criterion_id for c in contract.success_criteria]
    if cids != expected_cids:
        reasons.append("criterion_ids_mismatch")
    expected_hashes = [c.criterion_definition_hash for c in contract.success_criteria]
    if chashes != expected_hashes:
        reasons.append("criterion_definition_hashes_mismatch")

    stored_output = submission.get("output")
    if isinstance(stored_output, dict):
        computed = hashlib.sha256(canonical_json_dumps(stored_output).encode()).hexdigest()
        if computed != submission.get("output_canonical_hash"):
            reasons.append("output_hash_mismatch")
    else:
        reasons.append("output_hash_missing_or_invalid")

    expected_policy = compute_review_policy_hash(contract)
    if submission.get("review_policy_hash") != expected_policy:
        reasons.append("review_policy_hash_mismatch")

    return reasons


# NOTE: `heartbeat.BOUNTIES` is accessed via qualified attribute lookup
# throughout this module, deliberately NOT `from village.heartbeat import
# BOUNTIES` -- a plain value import would snapshot the path at import
# time, so a test's `monkeypatch.setattr(heartbeat, "BOUNTIES", ...)`
# would silently not apply here (a real bug caught while writing
# tests/test_bounty_review.py -- every test failed with a mysterious
# None until this was traced back to the stale binding). `_load`/`_save`/
# `_load_contract`/`_save_contract`/`_contract_id_for` are plain
# functions, not values -- importing them by name is safe, they resolve
# `heartbeat.BOUNTIES`/`heartbeat.CONTRACTS` dynamically inside
# heartbeat.py's own namespace every time they run.

DIR = Path("data/village")
SUBMISSIONS = DIR / "bounty_submissions.json"

_EVIDENCE_BANNED_KEY_SUBSTRINGS = ("api_key", "secret", "authorization", "bearer", "raw", "token")
_EVIDENCE_MAX_STRING_LEN = 4_000

# Kim's PR #15 review, Blocker 2: key-based filtering alone doesn't stop
# a model from echoing a secret-shaped VALUE back inside an otherwise
# innocuously-named field (e.g. evidence["notes"] containing a stray
# "sk-..." token). These patterns are checked against every string
# value, not just ones under a suspicious key, and redacted in place --
# never dropped silently, so a reviewer can see redaction happened.
_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"[Bb]earer\s+[A-Za-z0-9._-]{10,}"),
    # "Authorization: <scheme> <token>" -- consumes up to a few
    # whitespace-separated tokens after the colon (covers "Basic
    # <base64>", "Bearer <token>", etc.), not just the first one (a real
    # bug found in testing: `\S+` alone left the actual credential
    # token exposed right after a redacted scheme word).
    re.compile(r"[Aa]uthorization\s*:\s*\S+(?:\s+\S+){0,3}"),
)
_REDACTED = "[REDACTED]"


def _redact_secret_patterns(value: str) -> str:
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    return value


def _safe_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Defense in depth before persisting evidence as part of a bounty
    submission: strips any KEY that looks credential-shaped, redacts any
    string VALUE that matches a known secret pattern regardless of its
    key, and caps string length. `WorkResult.evidence` for a SUCCEEDED
    result only ever contains `{target_file, instruction, phase_log}`
    today (no raw provider payload is ever attached on success -- see
    village/worker.py), but this normalizer does not trust that by
    convention alone; it re-checks structurally every time."""

    def _clean(value: Any) -> Any:
        if isinstance(value, dict):
            return {
                k: _clean(v)
                for k, v in value.items()
                if not any(banned in k.lower() for banned in _EVIDENCE_BANNED_KEY_SUBSTRINGS)
            }
        if isinstance(value, list):
            return [_clean(v) for v in value]
        if isinstance(value, str):
            cleaned = _redact_secret_patterns(value)
            if len(cleaned) > _EVIDENCE_MAX_STRING_LEN:
                cleaned = cleaned[:_EVIDENCE_MAX_STRING_LEN] + "...[truncated]"
            return cleaned
        return value

    result = _clean(evidence)
    if not isinstance(result, dict):
        raise ValueError(f"_safe_evidence: expected dict result, got {type(result).__name__}")
    return result


def _find_bounty(board: dict[str, Any], bounty_id: str) -> dict[str, Any] | None:
    for b in board.get("bounties", []):
        if b["id"] == bounty_id:
            if not isinstance(b, dict):
                raise ValueError(f"bounty record is not a dict: {type(b).__name__}")
            return b
    return None


def _load_submissions() -> dict[str, Any]:
    return _load(SUBMISSIONS)


def _get_submission(submission_id: str) -> dict[str, Any] | None:
    submissions_raw = _load_submissions().get("submissions", {})
    if not isinstance(submissions_raw, dict):
        return None
    sub = submissions_raw.get(submission_id)
    return sub if isinstance(sub, dict) else None


def _next_submission_id(bounty_id: str, execution_id: str) -> str:
    """Always returns a fresh, never-before-used submission id (docs/
    research/BOUNTY_REVIEW_GATE_01.md Blocker 1). The common case (an
    execution submitted for the first time) keeps the plain
    `submission:<bounty_id>:<execution_id>` form; if that id is somehow
    already taken (the same execution resubmitted after a reject, or a
    defensive edge case), a numbered revision suffix is used instead --
    the previous record, and its review if any, is never overwritten."""
    store = _load_submissions()
    submissions = store.get("submissions", {})
    base = f"submission:{bounty_id}:{execution_id}"
    if base not in submissions:
        return base
    revision = 2
    while f"{base}:r{revision}" in submissions:
        revision += 1
    return f"{base}:r{revision}"


def _insert_submission(submission: dict[str, Any]) -> None:
    """Immutable insert: refuses to overwrite an existing submission_id.
    The ONLY way a new submission record enters storage -- callers must
    obtain a guaranteed-fresh id from `_next_submission_id()` first, and
    this function double-checks it at the storage layer, so a caller bug
    elsewhere can't silently destroy audit history."""
    store = _load_submissions()
    submissions = store.get("submissions", {})
    if submission["submission_id"] in submissions:
        raise RuntimeError(
            f"submission {submission['submission_id']!r} already exists -- "
            "audit records are immutable; this should be unreachable"
        )
    submissions[submission["submission_id"]] = submission
    store["submissions"] = submissions
    _save(SUBMISSIONS, store)


def _attach_review(submission_id: str, review_record: dict[str, Any]) -> dict[str, Any]:
    """Attach a review verdict to an existing submission -- the one
    legitimate in-place update this module makes, and only once: refuses
    if the submission already has a review (defense in depth on top of
    the status-based gating in `bounty_review()`, which should already
    make a double-review unreachable)."""
    store = _load_submissions()
    submissions = store.get("submissions", {})
    existing = submissions.get(submission_id)
    if existing is None:
        raise KeyError(f"no submission {submission_id!r} to attach a review to")
    if existing.get("review") is not None:
        raise RuntimeError(f"submission {submission_id!r} was already reviewed -- refusing to overwrite the review")
    updated = dict(existing)
    updated["review"] = review_record
    submissions[submission_id] = updated
    store["submissions"] = submissions
    _save(SUBMISSIONS, store)
    return updated


# ── Submission ────────────────────────────────────────────────────────────
def bounty_submit(bounty_id: str, actor_id: str, work_result: WorkResult) -> dict[str, Any] | None:
    """Submit a worker's WorkResult as evidence for a claimed bounty.

    Never marks the bounty `done`, never calls `contract.fulfill()` --
    only `bounty_review(..., decision="accept")` may do either. All
    validation happens BEFORE any file write (atomicity: an invalid
    submission leaves `bounties.json`/`contracts.json`/
    `bounty_submissions.json` completely untouched, not partially
    updated).

    Rejected (returns `None`, same semantics as "not found") when:
    - the bounty doesn't exist or isn't `claimed`,
    - `actor_id` doesn't match the bounty's `claimed_by`,
    - `work_result.status` isn't `SUCCEEDED` (a FAILED/INVALID_OUTPUT/
      PROVIDER_ERROR/BUDGET_EXCEEDED result is never submittable -- it
      isn't a real work result, per docs/research/
      AGENT_LOOP_WORKER_02.md's own "not just a successful HTTP call"
      criterion),
    - `work_result.contract_id` doesn't match this bounty's own
      deterministic contract id (wrong/foreign execution),
    - no ACTIVE `VillageContract` exists for this bounty.

    A duplicate submit call while the bounty is no longer `claimed`
    (already `submitted`/`done`/`open`) is explicitly, deterministically
    rejected -- not silently treated as idempotent success, per docs/
    research/BOUNTY_REVIEW_GATE_01.md's atomicity note.
    """
    board = _load(heartbeat.BOUNTIES)
    bounty = _find_bounty(board, bounty_id)
    if bounty is None or bounty.get("status") != "claimed":
        return None
    if bounty.get("claimed_by") != actor_id:
        return None
    if work_result.status != WorkResultStatus.SUCCEEDED:
        return None

    contract_id = _contract_id_for(bounty_id)
    if work_result.contract_id != contract_id:
        return None

    contract = _load_contract(contract_id)
    if contract is None or contract.state != ContractState.ACTIVE:
        return None

    # All validation above is read-only. First write happens here.
    submission_id = _next_submission_id(bounty_id, work_result.execution_id)
    # Immutable bindings for deterministic review (Issue #34)
    output_hash = hashlib.sha256(
        canonical_json_dumps(work_result.output if work_result.output else {}).encode()
    ).hexdigest()
    policy_hash = compute_review_policy_hash(contract)
    criterion_ids = [c.criterion_id for c in contract.success_criteria]
    criterion_hashes = [c.criterion_definition_hash for c in contract.success_criteria]
    submission = {
        "submission_id": submission_id,
        "bounty_id": bounty_id,
        "work_result_id": work_result.work_result_id,
        "contract_id": work_result.contract_id,
        "contract_version": contract.version,
        "execution_id": work_result.execution_id,
        "actor_id": actor_id,
        "provider": work_result.provider,
        "model": work_result.model,
        "status": work_result.status.value,
        "output": work_result.output,
        "evidence": _safe_evidence(work_result.evidence),
        "submitted_at": time.time(),
        "review": None,
        "output_canonical_hash": output_hash,
        "review_policy_hash": policy_hash,
        "criterion_ids": criterion_ids,
        "criterion_definition_hashes": criterion_hashes,
    }
    _insert_submission(submission)

    bounty["status"] = "submitted"
    bounty["current_submission_id"] = submission_id
    _save(heartbeat.BOUNTIES, board)

    return submission


# ── Review ────────────────────────────────────────────────────────────────
_VALID_DECISIONS = ("accept", "reject")


def bounty_review(
    bounty_id: str, reviewer_actor_id: str, decision: str, evidence: dict[str, Any] | None = None
) -> dict[str, Any] | None:
    """Record a review decision for a `submitted` bounty. The ONLY
    function in this codebase that may set a bounty to `done` or call
    `contract.fulfill()`.

    `decision` must be exactly `"accept"` or `"reject"` (raises
    `ValueError` otherwise -- a programming error, not a data-driven
    rejection, so it's raised rather than returning `None`).

    **accept**: only proceeds if `contract.fulfill()` itself succeeds
    (it raises internally if any `required` success criterion isn't
    `met is True` -- see village/contracts.py -- which is exactly "don't
    invent a pass for a non-automatically-checkable criterion": nothing
    here ever sets `criterion.met`, so a required criterion that was
    never explicitly marked `met=True` by some other, separate mechanism
    blocks acceptance, deterministically, not a judgment call made here).
    On success: review recorded on the submission (audit trail, not
    overwritten), contract `FULFILLED`, bounty `done`.

    **reject**: review recorded on the submission (still not
    overwritten/deleted -- it remains as audit evidence). Bounty reset
    to `claimed` (same `claimed_by`, so the same actor can resubmit).
    Contract is NOT touched -- stays `ACTIVE`.

    Rejected (returns `None`) when: the bounty doesn't exist or isn't
    `submitted`, no contract exists, or no submission record is
    associated with the bounty's current submission. A duplicate review
    call after the bounty has already left `submitted` is explicitly,
    deterministically rejected.
    """
    if decision not in _VALID_DECISIONS:
        raise ValueError(f"invalid decision: {decision!r}, must be one of {_VALID_DECISIONS}")

    board = _load(heartbeat.BOUNTIES)
    bounty = _find_bounty(board, bounty_id)
    if bounty is None or bounty.get("status") != "submitted":
        return None

    contract_id = _contract_id_for(bounty_id)
    contract = _load_contract(contract_id)
    if contract is None:
        return None

    submission_id_raw = bounty.get("current_submission_id")
    if not isinstance(submission_id_raw, str) or not submission_id_raw:
        return None
    submission_id: str = submission_id_raw
    submission = _get_submission(submission_id)
    if submission is None:
        return None

    review_record = {
        "reviewer_actor_id": reviewer_actor_id,
        "decision": decision,
        "evidence": _safe_evidence(evidence or {}),
        "reviewed_at": time.time(),
    }

    if decision == "reject":
        # Contract untouched -- validation above is the only
        # precondition. Submission (with its review outcome attached via
        # _attach_review(), an in-place update of THIS record only) is
        # preserved, not deleted or overwritten by a later resubmit --
        # _next_submission_id() guarantees the next submit() gets its own
        # fresh id.
        _attach_review(str(submission_id), review_record)

        bounty["status"] = "claimed"
        _save(heartbeat.BOUNTIES, board)
        return {"bounty": dict(bounty), "review": review_record}

    # accept: contract.fulfill() itself enforces "no unmet/unknown
    # required criterion" -- see docstring. Raises ValueError, no
    # mutation, if it can't honestly fulfill.
    try:
        contract.fulfill()
    except ValueError:
        return None

    _attach_review(str(submission_id), review_record)
    _save_contract(contract)

    bounty["status"] = "done"
    bounty["completed_at"] = time.time()
    _save(heartbeat.BOUNTIES, board)

    return {"bounty": dict(bounty), "review": review_record}
