"""Agent Village — Bounty Submission & Review Gate.

bounty_review() is the sole terminal authority for deterministic
and manual review.  The automatic path uses an atomic journal commit
followed by idempotent projections (review, contract, bounty).

Journal stages: decided → complete  (or failed_closed on contradiction)
"""

from __future__ import annotations

import hashlib
import os
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
from village.heartbeat import _contract_id_for, _load, _load_contract, _save_contract
from village.submission_bindings import validate_submission_bindings  # noqa: F401
from village.work_result import WorkResult, WorkResultStatus

DIR = Path("data/village")
SUBMISSIONS = DIR / "bounty_submissions.json"
FINALIZATION_JOURNAL = DIR / "finalization_journal.json"

# ── Atomic persistence ────────────────────────────────────────────────────


def _atomic_save_json(path: Path, data: dict[str, Any]) -> None:
    """Write *data* atomically: temp file → fsync → os.replace → fsync dir."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.tmp{os.getpid()}")
    raw = canonical_json_dumps(data).encode("utf-8")
    try:
        with open(tmp, "wb") as f:
            f.write(raw)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except Exception:
        # Clean up temp file on failure where safe
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass
        raise
    # Best-effort directory sync (non-fatal)
    try:
        fd = os.open(str(path.parent), os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass


# ── Manual review request ─────────────────────────────────────────────────


@dataclass(frozen=True)
class ManualReviewRequest:
    bounty_id: str
    submission_id: str
    reviewer_actor_id: str
    decision: ReviewDecision  # ACCEPT or REJECT only
    evidence: dict[str, Any] | None = None


# ── Safe evidence ─────────────────────────────────────────────────────────

_EVIDENCE_BANNED_KEY_SUBSTRINGS = ("api_key", "secret", "authorization", "bearer", "raw", "token")
_EVIDENCE_MAX_STRING_LEN = 4_000
_SECRET_VALUE_PATTERNS = (
    re.compile(r"sk-[A-Za-z0-9_-]{10,}"),
    re.compile(r"[Bb]earer\s+[A-Za-z0-9._-]{10,}"),
    re.compile(r"[Aa]uthorization\s*:\s*\S+(?:\s+\S+){0,3}"),
)
_REDACTED = "[REDACTED]"


def _redact_secret_patterns(value: str) -> str:
    for pattern in _SECRET_VALUE_PATTERNS:
        value = pattern.sub(_REDACTED, value)
    return value


def _safe_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
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


# ── Submission helpers ────────────────────────────────────────────────────


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
    store = _load_submissions()
    submissions = store.get("submissions", {})
    if submission["submission_id"] in submissions:
        raise RuntimeError(f"submission {submission['submission_id']!r} already exists")
    submissions[submission["submission_id"]] = submission
    store["submissions"] = submissions
    _atomic_save_json(SUBMISSIONS, store)


def _attach_review(submission_id: str, review_record: dict[str, Any]) -> dict[str, Any]:
    store = _load_submissions()
    submissions = store.get("submissions", {})
    existing = submissions.get(submission_id)
    if existing is None:
        raise KeyError(f"no submission {submission_id!r} to attach a review to")
    if existing.get("review") is not None:
        raise RuntimeError(f"submission {submission_id!r} was already reviewed")
    updated = dict(existing)
    updated["review"] = review_record
    submissions[submission_id] = updated
    store["submissions"] = submissions
    _atomic_save_json(SUBMISSIONS, store)
    return updated


# ── Bounty submission ─────────────────────────────────────────────────────


def bounty_submit(bounty_id: str, actor_id: str, work_result: WorkResult) -> dict[str, Any] | None:
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

    submission_id = _next_submission_id(bounty_id, work_result.execution_id)
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
    _atomic_save_json(heartbeat.BOUNTIES, board)
    return submission


# ── Journal ───────────────────────────────────────────────────────────────

_JOURNAL_DECIDED = "decided"
_JOURNAL_COMPLETE = "complete"
_JOURNAL_FAILED_CLOSED = "failed_closed"
_VALID_STAGES = frozenset({_JOURNAL_DECIDED, _JOURNAL_COMPLETE, _JOURNAL_FAILED_CLOSED})


def _journal_key(submission_id: str) -> str:
    return f"finalize:{submission_id}"


def _validate_journal_stage(entry: dict[str, Any]) -> bool:
    """Validate that the journal stage is a known value."""
    stage = entry.get("stage")
    return isinstance(stage, str) and stage in _VALID_STAGES


def _validate_journal_bindings(entry: dict[str, Any], evaluation: FinalEvaluation) -> bool:
    """Strictly verify ALL immutable decision bindings.

    Required fields: submission_id, bounty_id, contract_id, contract_version,
    evaluation_hash, decision, evaluator_version.
    """
    return (
        isinstance(entry, dict)
        and entry.get("submission_id") == evaluation.submission_id
        and entry.get("bounty_id") == evaluation.bounty_id
        and entry.get("contract_id") == evaluation.contract_id
        and entry.get("contract_version") == evaluation.contract_version
        and entry.get("evaluation_hash") == evaluation.evaluation_hash
        and entry.get("decision") == evaluation.overall_decision.value
        and entry.get("evaluator_version") == evaluation.evaluator_version
    )


def _write_journal_decided(evaluation: FinalEvaluation) -> dict[str, Any]:
    """Atomically commit a 'decided' journal record.

    Returns {} on conflict (caller must fail closed).  Returns existing
    record on identical replay.
    """
    now = time.time()
    record = {
        "submission_id": evaluation.submission_id,
        "bounty_id": evaluation.bounty_id,
        "contract_id": evaluation.contract_id,
        "contract_version": evaluation.contract_version,
        "evaluation_hash": evaluation.evaluation_hash,
        "decision": evaluation.overall_decision.value,
        "evaluator_version": evaluation.evaluator_version,
        "stage": _JOURNAL_DECIDED,
        "created_at": now,
        "updated_at": now,
    }
    journal = _load(FINALIZATION_JOURNAL)
    jkey = _journal_key(evaluation.submission_id)
    existing: dict[str, Any] | None = journal.get(jkey)

    if existing is not None:
        # Validate stage — malformed stage → failed_closed
        if not _validate_journal_stage(existing):
            existing["stage"] = _JOURNAL_FAILED_CLOSED
            existing["updated_at"] = now
            existing["diagnostic"] = "malformed_stage"
            _atomic_save_json(FINALIZATION_JOURNAL, journal)
            return {}
        # Binding mismatch → failed_closed
        if not _validate_journal_bindings(existing, evaluation):
            existing["stage"] = _JOURNAL_FAILED_CLOSED
            existing["updated_at"] = now
            existing["diagnostic"] = "bindings_mismatch"
            _atomic_save_json(FINALIZATION_JOURNAL, journal)
            return {}
        return existing  # already decided, identical replay

    journal[jkey] = record
    _atomic_save_json(FINALIZATION_JOURNAL, journal)
    return record


def _advance_journal_to(submission_id: str, stage: str, **extra: Any) -> None:
    """Atomically advance journal to *stage*.

    Sets ``completed_at`` exactly once on the transition to complete.
    A duplicate identical call after complete is a no-op.
    """
    journal = _load(FINALIZATION_JOURNAL)
    jkey = _journal_key(submission_id)
    entry = journal.get(jkey)
    if entry is None:
        return
    if entry.get("stage") == _JOURNAL_FAILED_CLOSED:
        return
    if entry.get("stage") == _JOURNAL_COMPLETE and stage == _JOURNAL_COMPLETE:
        return  # duplicate complete — no-op
    entry["stage"] = stage
    entry["updated_at"] = time.time()
    if stage == _JOURNAL_COMPLETE and "completed_at" not in entry:
        entry["completed_at"] = time.time()
    entry.update(extra)
    _atomic_save_json(FINALIZATION_JOURNAL, journal)


def _journal_fail_closed(submission_id: str, reason: str = "") -> None:
    """Atomically transition journal to failed_closed.

    Allowed from any stage including complete — when the completed
    record's own projections have been corrupted, the journal must
    record the failure rather than silently return None.
    """
    journal = _load(FINALIZATION_JOURNAL)
    jkey = _journal_key(submission_id)
    entry = journal.get(jkey)
    if entry is None:
        return
    if entry.get("stage") == _JOURNAL_FAILED_CLOSED:
        return  # already failed
    entry["stage"] = _JOURNAL_FAILED_CLOSED
    entry["updated_at"] = time.time()
    if reason:
        entry["diagnostic"] = reason[:256]
    _atomic_save_json(FINALIZATION_JOURNAL, journal)


# ── Timestamp validation ──────────────────────────────────────────────────


def _is_finite_timestamp(value: Any) -> bool:
    """Return True if *value* is a finite, non-negative, non-boolean numeric timestamp."""
    if not isinstance(value, (int, float)):
        return False
    if isinstance(value, bool):
        return False
    if isinstance(value, float):
        import math

        if math.isnan(value) or math.isinf(value):
            return False
    return value >= 0


# ── Review matching ───────────────────────────────────────────────────────

_REVIEW_CANONICAL_FIELDS = frozenset(
    {
        "review_kind",
        "evaluation_hash",
        "evaluator_version",
        "decision",
        "reason_codes",
        "criteria_results",
        "reviewed_at",
    }
)
_CRITERION_RESULT_FIELDS = frozenset({"criterion_id", "result", "reason_code"})


def _is_matching_deterministic_review(review: dict[str, Any], evaluation: FinalEvaluation) -> bool:
    """Exact canonical field set and value comparison.

    Required top-level fields: review_kind, evaluation_hash, evaluator_version,
    decision, reason_codes, criteria_results, reviewed_at.

    Each criterion-result entry must have exactly: criterion_id, result, reason_code.

    Rejects extra fields, missing fields, NaN, +Inf, -Inf, booleans, negative values.
    """
    if not isinstance(review, dict):
        return False
    # Enforce exact top-level field set
    if set(review.keys()) != _REVIEW_CANONICAL_FIELDS:
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
        if set(s.keys()) != _CRITERION_RESULT_FIELDS:
            return False
        if (
            s.get("criterion_id") != e["criterion_id"]
            or s.get("result") != e["result"]
            or s.get("reason_code") != e["reason_code"]
        ):
            return False
    ra = review.get("reviewed_at")
    if not _is_finite_timestamp(ra):
        return False
    return True


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


# ── Criterion application ─────────────────────────────────────────────────


def _apply_criteria_results(contract: VillageContract, evaluation: FinalEvaluation) -> None:
    for cr in evaluation.criteria_results:
        for sc in contract.success_criteria:
            if sc.criterion_id == cr.criterion_id:
                if cr.result == EvalResult.PASS:
                    sc.met = True
                elif cr.result == EvalResult.FAIL:
                    sc.met = False
                else:
                    sc.met = None
                break


def _criteria_match(contract: VillageContract, evaluation: FinalEvaluation) -> bool:
    if len(contract.success_criteria) != len(evaluation.criteria_results):
        return False
    seen: set[str] = set()
    for cr in evaluation.criteria_results:
        if cr.criterion_id in seen:
            return False
        seen.add(cr.criterion_id)
        found = False
        for sc in contract.success_criteria:
            if sc.criterion_id == cr.criterion_id:
                found = True
                expected = True if cr.result == EvalResult.PASS else False if cr.result == EvalResult.FAIL else None
                if sc.met is not expected:
                    return False
                break
        if not found:
            return False
    return True


# ── Contract evaluation history ───────────────────────────────────────────

_AUTO_EVALS_KEY = "auto_evaluations"
_AUTO_EVAL_ENTRY_FIELDS = frozenset({"submission_id", "evaluation_hash", "decision", "evaluator_version"})


def _get_eval_history(contract: VillageContract) -> dict[str, dict[str, Any]]:
    """Get the submission-keyed evaluation history map."""
    hist = contract.extra.get(_AUTO_EVALS_KEY)
    if isinstance(hist, dict):
        return hist
    return {}


def _record_eval_history(contract: VillageContract, evaluation: FinalEvaluation) -> str:
    """Record an automatic evaluation in contract history.

    Returns:
        ``"recorded"`` — new entry appended.
        ``"matched"`` — exact match, no-op.
        ``"conflict"`` — same submission, different evaluation → caller must fail closed.
    """
    hist = _get_eval_history(contract)
    sid = evaluation.submission_id
    entry = {
        "submission_id": sid,
        "evaluation_hash": evaluation.evaluation_hash,
        "decision": evaluation.overall_decision.value,
        "evaluator_version": evaluation.evaluator_version,
    }
    existing = hist.get(sid)
    if existing is None:
        hist[sid] = entry
        contract.extra[_AUTO_EVALS_KEY] = hist
        return "recorded"
    if not isinstance(existing, dict) or set(existing.keys()) != _AUTO_EVAL_ENTRY_FIELDS:
        return "conflict"
    if (
        existing.get("evaluation_hash") == entry["evaluation_hash"]
        and existing.get("decision") == entry["decision"]
        and existing.get("evaluator_version") == entry["evaluator_version"]
    ):
        return "matched"
    return "conflict"


def _eval_history_has_submission(contract: VillageContract, submission_id: str) -> bool:
    """Check whether *submission_id* has an entry in evaluation history."""
    hist = _get_eval_history(contract)
    return submission_id in hist


def _eval_history_matches(contract: VillageContract, evaluation: FinalEvaluation) -> bool:
    """Check that the evaluation history contains an exact match for this evaluation.

    Requires all four canonical fields: submission_id, evaluation_hash,
    decision, evaluator_version.  The entry must have exactly those fields.
    """
    hist = _get_eval_history(contract)
    entry = hist.get(evaluation.submission_id)
    if entry is None:
        return False
    if not isinstance(entry, dict):
        return False
    if set(entry.keys()) != _AUTO_EVAL_ENTRY_FIELDS:
        return False
    return (
        entry.get("submission_id") == evaluation.submission_id
        and entry.get("evaluation_hash") == evaluation.evaluation_hash
        and entry.get("decision") == evaluation.overall_decision.value
        and entry.get("evaluator_version") == evaluation.evaluator_version
    )


# ── Bounty finalization identity ──────────────────────────────────────────

_BOUNTY_FINALIZED_KEY = "_finalized_by"


def _set_bounty_finalization(bounty: dict[str, Any], evaluation: FinalEvaluation) -> None:
    """Persist finalization identity on the bounty record."""
    bounty[_BOUNTY_FINALIZED_KEY] = {
        "submission_id": evaluation.submission_id,
        "evaluation_hash": evaluation.evaluation_hash,
        "decision": evaluation.overall_decision.value,
    }


def _bounty_finalization_matches(bounty: dict[str, Any], evaluation: FinalEvaluation) -> bool:
    """Check whether the bounty's finalization identity matches *evaluation*."""
    fb = bounty.get(_BOUNTY_FINALIZED_KEY)
    if not isinstance(fb, dict):
        return False
    return (
        fb.get("submission_id") == evaluation.submission_id
        and fb.get("evaluation_hash") == evaluation.evaluation_hash
        and fb.get("decision") == evaluation.overall_decision.value
    )


# ── Complete-record verification ──────────────────────────────────────────


def _verify_complete_projections(
    submission: dict[str, Any] | None,
    contract: VillageContract,
    bounty: dict[str, Any],
    evaluation: FinalEvaluation,
    journal_entry: dict[str, Any],
) -> bool:
    """Verify ALL projections for a journal at 'complete' stage.

    Every contradiction returns False → caller must fail closed.
    """
    if submission is None:
        return False
    # Review must be present and deterministic-matching
    review = submission.get("review")
    if not isinstance(review, dict):
        return False
    if not _is_matching_deterministic_review(review, evaluation):
        return False
    # Contract state
    if evaluation.overall_decision == ReviewDecision.ACCEPT:
        if contract.state != ContractState.FULFILLED:
            return False
    else:
        if contract.state != ContractState.ACTIVE:
            return False
    # Criteria must match
    if not _criteria_match(contract, evaluation):
        return False
    # Evaluation history must contain exact match
    if not _eval_history_matches(contract, evaluation):
        return False
    # Bounty state
    expected_status = "done" if evaluation.overall_decision == ReviewDecision.ACCEPT else "claimed"
    if bounty.get("status") != expected_status:
        return False
    if bounty.get("current_submission_id") != evaluation.submission_id:
        return False
    if not _bounty_finalization_matches(bounty, evaluation):
        return False
    # Timestamp sanity — bounty and review
    if evaluation.overall_decision == ReviewDecision.ACCEPT:
        if not _is_finite_timestamp(bounty.get("completed_at")):
            return False
    if not _is_finite_timestamp(review.get("reviewed_at")):
        return False
    # Journal timestamp validation
    if not isinstance(journal_entry, dict):
        return False
    created_at = journal_entry.get("created_at")
    updated_at = journal_entry.get("updated_at")
    completed_at = journal_entry.get("completed_at")
    if not _is_finite_timestamp(created_at):
        return False
    if not _is_finite_timestamp(updated_at):
        return False
    if not _is_finite_timestamp(completed_at):
        return False
    assert isinstance(created_at, (int, float))
    assert isinstance(updated_at, (int, float))
    assert isinstance(completed_at, (int, float))
    if created_at > updated_at:
        return False
    if created_at > completed_at:
        return False
    return True


# ── Automatic review (commit-and-replay) ──────────────────────────────────


def _bounty_review_automatic(evaluation: FinalEvaluation) -> dict[str, Any] | None:
    if evaluation.overall_decision == ReviewDecision.INDETERMINATE:
        return None

    # ── Load canonical state ──
    board = _load(heartbeat.BOUNTIES)
    bounty = _find_bounty(board, evaluation.bounty_id)
    if bounty is None:
        return None
    contract = _load_contract(_contract_id_for(evaluation.bounty_id))
    if contract is None:
        return None
    submission = _get_submission(evaluation.submission_id)
    if submission is None:
        return None

    # Validate FinalEvaluation against canonical state
    reasons = validate_final_evaluation(evaluation, submission, contract)
    if reasons:
        return None

    current_sid = bounty.get("current_submission_id")
    if not isinstance(current_sid, str) or current_sid != evaluation.submission_id:
        return None

    # ── Check journal ──
    journal = _load(FINALIZATION_JOURNAL)
    jkey = _journal_key(evaluation.submission_id)
    journal_entry = journal.get(jkey)

    if journal_entry is not None:
        # Validate stage
        if not _validate_journal_stage(journal_entry):
            _journal_fail_closed(evaluation.submission_id, "malformed_stage")
            return None
        # Validate bindings — if mismatch against non-complete, fail closed.
        # If complete: the existing record is valid; reject the new evaluation
        # without modifying the journal.
        if not _validate_journal_bindings(journal_entry, evaluation):
            stage = journal_entry.get("stage", "")
            if stage != _JOURNAL_COMPLETE:
                _journal_fail_closed(evaluation.submission_id, "bindings_mismatch")
            return None
        stage = journal_entry.get("stage", "")
        if stage == _JOURNAL_FAILED_CLOSED:
            return None
        if stage == _JOURNAL_COMPLETE:
            # Verify ALL projections; never return cached success on contradiction
            if _verify_complete_projections(submission, contract, bounty, evaluation, journal_entry):
                existing_review = submission.get("review")
                if isinstance(existing_review, dict):
                    return {"bounty": dict(bounty), "review": existing_review}
            # Projections corrupted → fail closed
            _journal_fail_closed(evaluation.submission_id, "complete_contradiction")
            return None
        # decided — resume projections below
    else:
        # Fresh evaluation: validate preconditions
        if bounty.get("status") != "submitted":
            return None
        if contract.state != ContractState.ACTIVE:
            return None

        # Commit decided journal (atomic)
        record = _write_journal_decided(evaluation)
        if not record:
            return None  # conflict during decide → caller already failed closed

    # ── Replay projections ──

    # Review projection
    existing_review = submission.get("review")
    if existing_review is not None:
        if not _is_matching_deterministic_review(existing_review, evaluation):
            _journal_fail_closed(evaluation.submission_id, "conflicting_review")
            return None
        review_record = dict(existing_review)  # reuse, preserve reviewed_at
    else:
        review_record = _build_automatic_review_record(evaluation)
        try:
            _attach_review(evaluation.submission_id, review_record)
        except RuntimeError:
            _journal_fail_closed(evaluation.submission_id, "review_already_attached")
            return None

    # Contract projection
    if evaluation.overall_decision == ReviewDecision.ACCEPT:
        if contract.state == ContractState.FULFILLED:
            # Already fulfilled — verify. Never create a missing history entry.
            if not _eval_history_matches(contract, evaluation):
                _journal_fail_closed(evaluation.submission_id, "contract_fulfilled_different_eval")
                return None
            if not _criteria_match(contract, evaluation):
                _journal_fail_closed(evaluation.submission_id, "contract_criteria_mismatch")
                return None
        elif contract.state == ContractState.ACTIVE:
            hist_result = _record_eval_history(contract, evaluation)
            if hist_result == "conflict":
                _journal_fail_closed(evaluation.submission_id, "eval_history_conflict")
                return None
            if not _criteria_match(contract, evaluation):
                _apply_criteria_results(contract, evaluation)
            try:
                contract.fulfill()
            except ValueError:
                _journal_fail_closed(evaluation.submission_id, "fulfill_refused")
                return None
            _atomic_save_json(
                heartbeat.CONTRACTS,
                _load_contract_store_for_save(contract),
            )
        else:
            _journal_fail_closed(evaluation.submission_id, "contract_incompatible_state")
            return None
    else:  # REJECT
        if contract.state != ContractState.ACTIVE:
            _journal_fail_closed(evaluation.submission_id, "contract_not_active_for_reject")
            return None
        hist_result = _record_eval_history(contract, evaluation)
        if hist_result == "conflict":
            _journal_fail_closed(evaluation.submission_id, "eval_history_conflict")
            return None
        if not _criteria_match(contract, evaluation):
            _apply_criteria_results(contract, evaluation)
        _atomic_save_json(
            heartbeat.CONTRACTS,
            _load_contract_store_for_save(contract),
        )

    # Bounty projection
    if evaluation.overall_decision == ReviewDecision.ACCEPT:
        if bounty.get("status") == "done":
            if not _bounty_finalization_matches(bounty, evaluation):
                _journal_fail_closed(evaluation.submission_id, "bounty_done_different_eval")
                return None
            # Already done with matching identity — no-op
        elif bounty.get("status") == "submitted":
            bounty["status"] = "done"
            bounty["completed_at"] = time.time()
            _set_bounty_finalization(bounty, evaluation)
            _atomic_save_json(heartbeat.BOUNTIES, board)
        else:
            _journal_fail_closed(evaluation.submission_id, "bounty_incompatible_state")
            return None
    else:  # REJECT
        if bounty.get("status") == "claimed":
            if not _bounty_finalization_matches(bounty, evaluation):
                _journal_fail_closed(evaluation.submission_id, "bounty_claimed_different_eval")
                return None
            # Already claimed with matching identity — no-op
        elif bounty.get("status") == "submitted":
            bounty["status"] = "claimed"
            _set_bounty_finalization(bounty, evaluation)
            _atomic_save_json(heartbeat.BOUNTIES, board)
        elif bounty.get("status") == "done":
            _journal_fail_closed(evaluation.submission_id, "bounty_done_on_reject")
            return None
        else:
            _journal_fail_closed(evaluation.submission_id, "bounty_incompatible_state")
            return None

    # Complete (atomic)
    _advance_journal_to(evaluation.submission_id, _JOURNAL_COMPLETE)

    return {"bounty": dict(bounty), "review": review_record}


def _load_contract_store_for_save(contract: VillageContract) -> dict[str, Any]:
    """Load the contract store and merge in *contract*.

    Needed because _save_contract uses _save(CONTRACTS, store) which does
    temp-file replace (also atomic).  We duplicate the merge logic here so
    we can use _atomic_save_json directly.
    """
    store = _load(heartbeat.CONTRACTS)
    contracts = store.get("contracts", {})
    if not isinstance(contracts, dict):
        contracts = {}
    contracts[contract.contract_id] = contract.to_dict()
    store["contracts"] = contracts
    return store


# ── Manual review ─────────────────────────────────────────────────────────


def _bounty_review_manual(request: ManualReviewRequest) -> dict[str, Any] | None:
    if request.decision not in (ReviewDecision.ACCEPT, ReviewDecision.REJECT):
        raise ValueError(f"invalid decision: {request.decision!r}, must be ACCEPT or REJECT")

    board = _load(heartbeat.BOUNTIES)
    bounty = _find_bounty(board, request.bounty_id)
    if bounty is None or bounty.get("status") != "submitted":
        return None
    if bounty.get("current_submission_id") != request.submission_id:
        return None

    contract = _load_contract(_contract_id_for(request.bounty_id))
    if contract is None:
        return None

    submission = _get_submission(request.submission_id)
    if submission is None:
        return None

    review_record = {
        "reviewer_actor_id": request.reviewer_actor_id,
        "decision": request.decision.value,
        "evidence": _safe_evidence(request.evidence or {}),
        "reviewed_at": time.time(),
    }

    if request.decision == ReviewDecision.REJECT:
        _attach_review(request.submission_id, review_record)
        bounty["status"] = "claimed"
        _atomic_save_json(heartbeat.BOUNTIES, board)
        return {"bounty": dict(bounty), "review": review_record}

    # accept
    try:
        contract.fulfill()
    except ValueError:
        return None
    _attach_review(request.submission_id, review_record)
    _save_contract(contract)
    bounty["status"] = "done"
    bounty["completed_at"] = time.time()
    _atomic_save_json(heartbeat.BOUNTIES, board)
    return {"bounty": dict(bounty), "review": review_record}


# ── Public entry point ────────────────────────────────────────────────────


def bounty_review(
    review_input: FinalEvaluation | ManualReviewRequest,
) -> dict[str, Any] | None:
    if isinstance(review_input, ManualReviewRequest):
        return _bounty_review_manual(review_input)
    return _bounty_review_automatic(review_input)
