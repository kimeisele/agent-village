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

import time
from pathlib import Path
from typing import Any

import village.heartbeat as heartbeat
from village.contracts import ContractState
from village.heartbeat import _contract_id_for, _load, _load_contract, _save, _save_contract
from village.work_result import WorkResult, WorkResultStatus

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


def _safe_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    """Defense in depth before persisting evidence as part of a bounty
    submission: strips any key that looks credential-shaped and caps
    string length. `WorkResult.evidence` for a SUCCEEDED result only
    ever contains `{target_file, instruction, phase_log}` today (no raw
    provider payload is ever attached on success -- see
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
        if isinstance(value, str) and len(value) > _EVIDENCE_MAX_STRING_LEN:
            return value[:_EVIDENCE_MAX_STRING_LEN] + "...[truncated]"
        return value

    return _clean(evidence)


def _find_bounty(board: dict, bounty_id: str) -> dict | None:
    for b in board.get("bounties", []):
        if b["id"] == bounty_id:
            return b
    return None


def _load_submissions() -> dict:
    return _load(SUBMISSIONS)


def _save_submission(submission: dict) -> None:
    store = _load_submissions()
    submissions = store.get("submissions", {})
    submissions[submission["submission_id"]] = submission
    store["submissions"] = submissions
    _save(SUBMISSIONS, store)


def _get_submission(submission_id: str) -> dict | None:
    return _load_submissions().get("submissions", {}).get(submission_id)


# ── Submission ────────────────────────────────────────────────────────────
def bounty_submit(bounty_id: str, actor_id: str, work_result: WorkResult) -> dict | None:
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
    submission_id = f"submission:{bounty_id}:{work_result.execution_id}"
    submission = {
        "submission_id": submission_id,
        "bounty_id": bounty_id,
        "work_result_id": work_result.work_result_id,
        "contract_id": work_result.contract_id,
        "execution_id": work_result.execution_id,
        "actor_id": actor_id,
        "provider": work_result.provider,
        "model": work_result.model,
        "status": work_result.status.value,
        "output": work_result.output,
        "evidence": _safe_evidence(work_result.evidence),
        "submitted_at": time.time(),
        "review": None,
    }
    _save_submission(submission)

    bounty["status"] = "submitted"
    bounty["current_submission_id"] = submission_id
    _save(heartbeat.BOUNTIES, board)

    return submission


# ── Review ────────────────────────────────────────────────────────────────
_VALID_DECISIONS = ("accept", "reject")


def bounty_review(
    bounty_id: str, reviewer_actor_id: str, decision: str, evidence: dict[str, Any] | None = None
) -> dict | None:
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

    submission_id = bounty.get("current_submission_id")
    submission = _get_submission(submission_id) if submission_id else None
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
        # precondition. Submission (with its review outcome attached)
        # is preserved, not deleted or overwritten by a later resubmit
        # (each execution gets its own submission_id).
        submission["review"] = review_record
        _save_submission(submission)

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

    submission["review"] = review_record
    _save_submission(submission)
    _save_contract(contract)

    bounty["status"] = "done"
    bounty["completed_at"] = time.time()
    _save(heartbeat.BOUNTIES, board)

    return {"bounty": dict(bounty), "review": review_record}
