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

No reputation tiers, no multi-reviewer quorum, no appeals -- a review
decision here is made by an explicit human-authorized caller or by a
deterministic FinalEvaluation (docs/BEFUND.md §41), never by the cognitive
worker itself.

Accepting a `FinalEvaluation` (automatic review path) requires the
evaluation to pass `validate_final_evaluation()`, have a concrete
ACCEPT or REJECT decision (INDETERMINATE is never applied), and match
the submission's identity bindings.  A crash-safe finalization journal
guarantees that retries with the same evaluation_hash resume from the
last known-good stage.
"""

from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import village.heartbeat as heartbeat
from village.contracts import (
    ContractState,
    VillageContract,
    canonical_json_dumps,
    compute_review_policy_hash,
)
from village.evaluator import EvalResult
from village.final_evaluation import FinalEvaluation, ReviewDecision, validate_final_evaluation
from village.heartbeat import _contract_id_for, _load, _load_contract, _save, _save_contract
from village.submission_bindings import validate_submission_bindings  # noqa: F401 — re-exported
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
FINALIZATION_JOURNAL = DIR / "finalization_journal.json"


@dataclass(frozen=True)
class ManualReviewRequest:
    """Discriminated input for the manual (human-authorized) review path.

    ``bounty_review()`` accepts either this or a ``FinalEvaluation``.
    The decision string must be ``"accept"`` or ``"reject"``.
    """

    bounty_id: str
    submission_id: str
    reviewer_actor_id: str
    decision: ReviewDecision  # ACCEPT or REJECT only
    evidence: dict[str, Any] | None = None


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


# ── Finalization Journal ─────────────────────────────────────────────────
_JOURNAL_STAGES = ("prepared", "review_attached", "contract_applied", "bounty_applied", "complete")
_JOURNAL_FAILED_CLOSED = "failed_closed"


def _journal_key(submission_id: str) -> str:
    return f"finalize:{submission_id}"


def _init_journal(submission_id: str, bounty_id: str, evaluation_hash: str, decision: str) -> None:
    journal = _load(FINALIZATION_JOURNAL)
    jkey = _journal_key(submission_id)
    if jkey not in journal:
        journal[jkey] = {
            "submission_id": submission_id,
            "bounty_id": bounty_id,
            "evaluation_hash": evaluation_hash,
            "decision": decision,
            "stage": "prepared",
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        _save(FINALIZATION_JOURNAL, journal)


def _advance_journal(submission_id: str, target_stage: str) -> None:
    """Advance the journal stage. Sets completed_at exactly once at 'complete'."""
    journal = _load(FINALIZATION_JOURNAL)
    jkey = _journal_key(submission_id)
    entry = journal.get(jkey)
    if entry is None:
        return
    cur = entry.get("stage", "prepared")
    if cur in (_JOURNAL_FAILED_CLOSED, "complete"):
        return
    try:
        cur_idx = _JOURNAL_STAGES.index(cur)
        tgt_idx = _JOURNAL_STAGES.index(target_stage)
    except ValueError:
        return
    if tgt_idx <= cur_idx:
        return
    entry["stage"] = target_stage
    entry["updated_at"] = time.time()
    if target_stage == "complete" and "completed_at" not in entry:
        entry["completed_at"] = time.time()
    _save(FINALIZATION_JOURNAL, journal)


def _journal_fail_closed(submission_id: str) -> None:
    """Set the journal stage to failed_closed. Never transitions from complete."""
    journal = _load(FINALIZATION_JOURNAL)
    jkey = _journal_key(submission_id)
    entry = journal.get(jkey)
    if entry is None or entry.get("stage") == "complete":
        return
    entry["stage"] = _JOURNAL_FAILED_CLOSED
    entry["updated_at"] = time.time()
    _save(FINALIZATION_JOURNAL, journal)


# ── Automatic review helpers ─────────────────────────────────────────────
def _build_automatic_review_record(evaluation: FinalEvaluation) -> dict[str, Any]:
    return {
        "review_kind": "deterministic",
        "evaluation_hash": evaluation.evaluation_hash,
        "evaluator_version": evaluation.evaluator_version,
        "decision": evaluation.overall_decision.value,
        "reason_codes": list(evaluation.reason_codes),
        "criteria_results": [
            {"criterion_id": cr.criterion_id, "result": cr.result.value, "reason_code": cr.reason_code}
            for cr in evaluation.criteria_results
        ],
        "reviewed_at": time.time(),
    }


def _apply_criteria_results(contract: VillageContract, evaluation: FinalEvaluation) -> None:
    """Apply evaluation results by exact criterion_id.
    PASS→met=True, FAIL→met=False, INDETERMINATE→met=None (explicit)."""
    for cr in evaluation.criteria_results:
        for sc in contract.success_criteria:
            if sc.criterion_id == cr.criterion_id:
                if cr.result == EvalResult.PASS:
                    sc.met = True
                elif cr.result == EvalResult.FAIL:
                    sc.met = False
                else:
                    sc.met = None  # INDETERMINATE explicitly cleared
                break


def _validate_journal_record(entry: object, jkey: str, evaluation: FinalEvaluation) -> dict[str, Any] | None:
    """Validate journal record against evaluation. Returns dict or None (fail closed)."""
    if not isinstance(entry, dict):
        return None
    sid = entry.get("submission_id")
    if not isinstance(sid, str) or not sid or sid != evaluation.submission_id:
        return None
    if jkey != f"finalize:{sid}":
        return None
    bid = entry.get("bounty_id")
    if not isinstance(bid, str) or not bid or bid != evaluation.bounty_id:
        return None
    eh = entry.get("evaluation_hash")
    if not isinstance(eh, str) or not eh or eh != evaluation.evaluation_hash:
        return None
    dec = entry.get("decision")
    if not isinstance(dec, str) or dec != evaluation.overall_decision.value:
        return None
    stage = entry.get("stage", "")
    if stage not in ("prepared", "review_attached", "contract_applied", "bounty_applied", "complete", "failed_closed"):
        return None
    for ts_field in ("created_at", "updated_at"):
        ts = entry.get(ts_field)
        if not isinstance(ts, (int, float)):
            return None
        if isinstance(ts, float) and (ts != ts or ts == float("inf")):
            return None
    if entry["updated_at"] < entry["created_at"]:
        return None
    if stage == "complete":
        ca = entry.get("completed_at")
        if not isinstance(ca, (int, float)):
            return None
        if isinstance(ca, float) and (ca != ca or ca == float("inf")):
            return None
    return entry


def _is_matching_deterministic_review(review: dict[str, Any], evaluation: FinalEvaluation) -> bool:
    """Exact canonical matching. Every required field must exist with exact type/value."""
    if not isinstance(review, dict):
        return False
    if review.get("review_kind") != "deterministic":
        return False
    eh = review.get("evaluation_hash")
    if not isinstance(eh, str) or not eh or eh != evaluation.evaluation_hash:
        return False
    if review.get("evaluator_version") != evaluation.evaluator_version:
        return False
    if review.get("decision") != evaluation.overall_decision.value:
        return False
    rc_stored = review.get("reason_codes")
    if not isinstance(rc_stored, list) or list(evaluation.reason_codes) != rc_stored:
        return False
    cr_stored = review.get("criteria_results")
    if not isinstance(cr_stored, list):
        return False
    cr_eval = [
        {"criterion_id": cr.criterion_id, "result": cr.result.value, "reason_code": cr.reason_code}
        for cr in evaluation.criteria_results
    ]
    if len(cr_stored) != len(cr_eval):
        return False
    for s, e in zip(cr_stored, cr_eval):
        if not isinstance(s, dict):
            return False
        if (
            s.get("criterion_id") != e["criterion_id"]
            or s.get("result") != e["result"]
            or s.get("reason_code") != e["reason_code"]
        ):
            return False
    ra = review.get("reviewed_at")
    if not isinstance(ra, (int, float)):
        return False
    if isinstance(ra, float) and (ra != ra or ra == float("inf")):
        return False
    return True


# ── Review ────────────────────────────────────────────────────────────────
def _bounty_review_manual(request: ManualReviewRequest) -> dict[str, Any] | None:
    """Manual (human-authorized) review path.  Preserves the exact behavior
    of the original ``bounty_review()`` before the discriminated-union
    refactor (Issue #128 / BEFUND.md §41)."""
    if request.decision not in (ReviewDecision.ACCEPT, ReviewDecision.REJECT):
        raise ValueError(f"invalid decision: {request.decision!r}, must be ACCEPT or REJECT")

    board = _load(heartbeat.BOUNTIES)
    bounty = _find_bounty(board, request.bounty_id)
    if bounty is None or bounty.get("status") != "submitted":
        return None

    # Validate submission_id matches the bounty's current authoritative submission
    if bounty.get("current_submission_id") != request.submission_id:
        return None

    contract_id = _contract_id_for(request.bounty_id)
    contract = _load_contract(contract_id)
    if contract is None:
        return None

    submission_id: str = request.submission_id
    submission = _get_submission(submission_id)
    if submission is None:
        return None

    review_record = {
        "reviewer_actor_id": request.reviewer_actor_id,
        "decision": request.decision.value,
        "evidence": _safe_evidence(request.evidence or {}),
        "reviewed_at": time.time(),
    }

    if request.decision == ReviewDecision.REJECT:
        _attach_review(submission_id, review_record)
        bounty["status"] = "claimed"
        _save(heartbeat.BOUNTIES, board)
        return {"bounty": dict(bounty), "review": review_record}

    # accept
    try:
        contract.fulfill()
    except ValueError:
        return None

    _attach_review(submission_id, review_record)
    _save_contract(contract)

    bounty["status"] = "done"
    bounty["completed_at"] = time.time()
    _save(heartbeat.BOUNTIES, board)

    return {"bounty": dict(bounty), "review": review_record}


def _bounty_review_automatic(evaluation: FinalEvaluation) -> dict[str, Any] | None:
    """Automatic (deterministic) review path.  Called when
    ``bounty_review()`` receives a ``FinalEvaluation``.

    Validates bindings, checks for INDETERMINATE (never applied), applies
    criterion results, and uses the finalization journal for crash-safe
    retry with identical ``evaluation_hash``.  Every retry reconciles full
    canonical state — there is no fast-path early matching-hash return."""
    # Pre-load submission for existing-review check (used later for matching).
    pre_submission = _get_submission(evaluation.submission_id)

    # Freshly load from persistence
    board = _load(heartbeat.BOUNTIES)
    bounty = _find_bounty(board, evaluation.bounty_id)
    if bounty is None:
        return None
    bounty_status = bounty.get("status")
    # Retries after completion: check journal and return cached result
    if bounty_status not in ("submitted", "done"):
        return None
    if bounty_status == "done":
        sub = _get_submission(evaluation.submission_id)
        if sub and sub.get("review"):
            existing = sub["review"]
            if _is_matching_deterministic_review(existing, evaluation):
                _advance_journal(evaluation.submission_id, "complete")
                return {"bounty": dict(bounty), "review": existing}
            # Conflicting evaluation on an already-done bounty
            return None
        return None

    submission_id: str | None = bounty.get("current_submission_id")
    if not isinstance(submission_id, str) or not submission_id:
        return None
    if submission_id != evaluation.submission_id:
        _log_review_rejection("stale_submission", submission_id)
        return None

    contract = _load_contract(_contract_id_for(evaluation.bounty_id))
    if contract is None:
        _log_review_rejection("missing_contract", evaluation.bounty_id)
        return None

    # Reuse pre-loaded submission if it matches; otherwise load fresh
    submission = (
        pre_submission
        if pre_submission is not None and pre_submission.get("submission_id") == submission_id
        else _get_submission(submission_id)
    )
    if submission is None:
        _log_review_rejection("missing_submission", submission_id)
        return None

    # Structural validation
    reasons = validate_final_evaluation(evaluation, submission, contract)
    if reasons:
        _log_review_rejection("validation_failed", reasons[:3])
        return None

    # INDETERMINATE is never applied
    if evaluation.overall_decision == ReviewDecision.INDETERMINATE:
        _log_review_rejection("indeterminate_not_applied", submission_id)
        return None

    # Check for existing review — matching resumes, conflicting fails closed
    existing_review = pre_submission.get("review") if pre_submission is not None else None
    if existing_review is not None:
        if not _is_matching_deterministic_review(existing_review, evaluation):
            _log_review_rejection("conflicting_review", evaluation.submission_id)
            return None
        # Matching review: resume from journal, don't re-attach

    # Check / initialise journal
    journal = _load(FINALIZATION_JOURNAL)
    jkey = _journal_key(submission_id)
    journal_entry = journal.get(jkey)

    if journal_entry is not None:
        validated = _validate_journal_record(journal_entry, jkey, evaluation)
        if validated is None:
            _journal_fail_closed(submission_id)
            return None
        if validated.get("evaluation_hash") != evaluation.evaluation_hash:
            _log_review_rejection("conflicting_journal_hash", submission_id)
            return None
        stage = validated.get("stage", "prepared")
        if stage == "complete":
            # Already fully finalized — return cached result
            sub = _get_submission(submission_id)
            if sub and sub.get("review"):
                return {"bounty": dict(bounty), "review": sub["review"]}
            return None
        if stage == "failed_closed":
            return None
    else:
        # First attempt — create journal entry
        _init_journal(
            submission_id, evaluation.bounty_id, evaluation.evaluation_hash, evaluation.overall_decision.value
        )

    # Determine resume skip flags from journal AND canonical review state
    # If matching review already persisted, never re-attach (journal may lag)
    review_already_attached = existing_review is not None and _is_matching_deterministic_review(
        existing_review, evaluation
    )
    skip_attach = review_already_attached
    skip_fulfill_save = False
    skip_bounty_save = False
    if journal_entry is not None:
        rs = journal_entry.get("stage", "prepared")
        if rs in ("review_attached", "contract_applied", "bounty_applied"):
            skip_attach = True
        if rs in ("contract_applied", "bounty_applied"):
            skip_fulfill_save = True
        if rs == "bounty_applied":
            skip_bounty_save = True

    # Reuse existing review record if matching; otherwise build new
    review_record: dict[str, Any] = (
        existing_review
        if review_already_attached and isinstance(existing_review, dict)
        else _build_automatic_review_record(evaluation)
    )

    # Apply criterion results to contract (in-memory, always applied on fresh load)
    _apply_criteria_results(contract, evaluation)

    # Stage: review_attached
    if not skip_attach:
        _attach_review(submission_id, review_record)
    _advance_journal(submission_id, "review_attached")

    # Stage: contract_applied (ACCEPT fulfills, REJECT saves results)
    if not skip_fulfill_save:
        if evaluation.overall_decision == ReviewDecision.ACCEPT:
            try:
                contract.fulfill()
            except ValueError:
                _journal_fail_closed(submission_id)
                return None
        # Save contract for BOTH decisions (criterion results persisted)
        _save_contract(contract)
    _advance_journal(submission_id, "contract_applied")

    # Stage: bounty_applied
    if not skip_bounty_save:
        if evaluation.overall_decision == ReviewDecision.ACCEPT:
            bounty["status"] = "done"
            bounty["completed_at"] = time.time()
        else:  # REJECT
            bounty["status"] = "claimed"
        _save(heartbeat.BOUNTIES, board)
    _advance_journal(submission_id, "bounty_applied")

    # Stage: complete
    _advance_journal(submission_id, "complete")

    return {"bounty": dict(bounty), "review": review_record}


def _log_review_rejection(reason: str, detail: object) -> None:
    """Quiet diagnostic for review rejections (noiseless in production,
    readable in test output)."""
    print(f"  [review] automatic rejection — {reason}: {detail}")


def bounty_review(
    review_input: FinalEvaluation | ManualReviewRequest,
) -> dict[str, Any] | None:
    """Record a review decision for a ``submitted`` bounty.  The ONLY
    function in this codebase that may set a bounty to ``done`` or call
    ``contract.fulfill()``.

    Accepts a discriminated union of ``FinalEvaluation`` (automatic,
    deterministic review) or ``ManualReviewRequest`` (human-authorized
    review).  See the respective private helpers for per-path semantics.

    For ``ManualReviewRequest``:
        ``decision`` must be exactly ``"accept"`` or ``"reject"`` (raises
        ``ValueError`` otherwise -- a programming error, not a data-driven
        rejection).

        **accept**: only proceeds if ``contract.fulfill()`` itself
        succeeds (it raises internally if any ``required`` success
        criterion isn't ``met is True``).  Submission review recorded,
        contract ``FULFILLED``, bounty ``done``.

        **reject**: review recorded, bounty reset to ``claimed``,
        contract stays ``ACTIVE``.

    For ``FinalEvaluation``:
        Validated via ``validate_final_evaluation()``.  INDETERMINATE is
        never applied.  A crash-safe finalization journal enables safe
        retries with identical ``evaluation_hash``.
    """
    if isinstance(review_input, ManualReviewRequest):
        return _bounty_review_manual(review_input)
    return _bounty_review_automatic(review_input)
