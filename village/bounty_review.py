"""Agent Village — Bounty Submission & Review Gate.

bounty_review() is the sole terminal authority for deterministic
and manual review.  The automatic path uses an atomic journal commit
followed by idempotent projections (review, contract, bounty).
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
from village.heartbeat import _contract_id_for, _load, _load_contract, _save, _save_contract
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
    with open(tmp, "wb") as f:
        f.write(raw)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
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
    _save(SUBMISSIONS, store)


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
    _save(SUBMISSIONS, store)
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
    _save(heartbeat.BOUNTIES, board)
    return submission


# ── Journal ───────────────────────────────────────────────────────────────

_JOURNAL_DECIDED = "decided"
_JOURNAL_COMPLETE = "complete"
_JOURNAL_FAILED_CLOSED = "failed_closed"


def _journal_key(submission_id: str) -> str:
    return f"finalize:{submission_id}"


def _validate_journal_bindings(entry: dict[str, Any], evaluation: FinalEvaluation) -> bool:
    """Validate that a journal entry's immutable bindings match the evaluation."""
    return (
        isinstance(entry, dict)
        and entry.get("submission_id") == evaluation.submission_id
        and entry.get("bounty_id") == evaluation.bounty_id
        and entry.get("evaluation_hash") == evaluation.evaluation_hash
        and entry.get("decision") == evaluation.overall_decision.value
    )


def _write_journal_decided(evaluation: FinalEvaluation) -> dict[str, Any]:
    """Atomically commit a 'decided' journal record. Returns the record."""
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
        if not _validate_journal_bindings(existing, evaluation):
            return {}  # conflict — caller fails closed
        return existing  # already decided, return existing
    journal[jkey] = record
    _atomic_save_json(FINALIZATION_JOURNAL, journal)
    return record


def _advance_journal_to(submission_id: str, stage: str, **extra: Any) -> None:
    journal = _load(FINALIZATION_JOURNAL)
    jkey = _journal_key(submission_id)
    entry = journal.get(jkey)
    if entry is None:
        return
    if entry.get("stage") == _JOURNAL_FAILED_CLOSED:
        return
    entry["stage"] = stage
    entry["updated_at"] = time.time()
    entry.update(extra)
    _save(FINALIZATION_JOURNAL, journal)


def _journal_fail_closed(submission_id: str, reason: str = "") -> None:
    journal = _load(FINALIZATION_JOURNAL)
    jkey = _journal_key(submission_id)
    entry = journal.get(jkey)
    if entry is None or entry.get("stage") == _JOURNAL_COMPLETE:
        return
    entry["stage"] = _JOURNAL_FAILED_CLOSED
    entry["updated_at"] = time.time()
    if reason:
        entry["diagnostic"] = reason[:256]
    _save(FINALIZATION_JOURNAL, journal)


# ── Review matching ───────────────────────────────────────────────────────


def _is_matching_deterministic_review(review: dict[str, Any], evaluation: FinalEvaluation) -> bool:
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


# ── Contract evaluation binding ───────────────────────────────────────────

_AUTO_EVAL_BINDING_KEY = "auto_evaluation"


def _get_contract_eval_binding(contract: VillageContract) -> dict[str, Any] | None:
    binding = contract.extra.get(_AUTO_EVAL_BINDING_KEY)
    return binding if isinstance(binding, dict) else None


def _set_contract_eval_binding(contract: VillageContract, evaluation: FinalEvaluation) -> None:
    contract.extra[_AUTO_EVAL_BINDING_KEY] = {
        "submission_id": evaluation.submission_id,
        "evaluation_hash": evaluation.evaluation_hash,
        "decision": evaluation.overall_decision.value,
        "evaluator_version": evaluation.evaluator_version,
    }


def _contract_binding_matches(contract: VillageContract, evaluation: FinalEvaluation) -> bool:
    binding = _get_contract_eval_binding(contract)
    if binding is None:
        return False
    return (
        binding.get("submission_id") == evaluation.submission_id
        and binding.get("evaluation_hash") == evaluation.evaluation_hash
        and binding.get("decision") == evaluation.overall_decision.value
    )


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
        if not _validate_journal_bindings(journal_entry, evaluation):
            return None  # conflicting bindings
        stage = journal_entry.get("stage", "")
        if stage == _JOURNAL_FAILED_CLOSED:
            return None
        if stage == _JOURNAL_COMPLETE:
            # Already complete — verify projections match
            existing_review = submission.get("review")
            if existing_review and _is_matching_deterministic_review(existing_review, evaluation):
                return {"bounty": dict(bounty), "review": existing_review}
            return None
        # decided — resume projections below
    else:
        # Fresh evaluation: validate preconditions
        if bounty.get("status") != "submitted":
            return None
        if contract.state != ContractState.ACTIVE:
            return None

        # Commit decided journal
        record = _write_journal_decided(evaluation)
        if not record:
            return None  # conflict during decide

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
            # Already fulfilled — verify binding matches
            if not _contract_binding_matches(contract, evaluation):
                _journal_fail_closed(evaluation.submission_id, "contract_fulfilled_different_eval")
                return None
            if not _criteria_match(contract, evaluation):
                _journal_fail_closed(evaluation.submission_id, "contract_criteria_mismatch")
                return None
        else:
            if not _criteria_match(contract, evaluation):
                _apply_criteria_results(contract, evaluation)
            try:
                contract.fulfill()
            except ValueError:
                _journal_fail_closed(evaluation.submission_id, "fulfill_refused")
                return None
            _set_contract_eval_binding(contract, evaluation)
            _save_contract(contract)
    else:  # REJECT
        if contract.state != ContractState.ACTIVE:
            _journal_fail_closed(evaluation.submission_id, "contract_not_active_for_reject")
            return None
        if not _criteria_match(contract, evaluation):
            _apply_criteria_results(contract, evaluation)
            _set_contract_eval_binding(contract, evaluation)
            _save_contract(contract)
        elif not _contract_binding_matches(contract, evaluation):
            _set_contract_eval_binding(contract, evaluation)
            _save_contract(contract)

    # Bounty projection
    if evaluation.overall_decision == ReviewDecision.ACCEPT:
        if bounty.get("status") != "done":
            bounty["status"] = "done"
            bounty["completed_at"] = time.time()
            _save(heartbeat.BOUNTIES, board)
    else:  # REJECT
        if bounty.get("status") != "claimed":
            bounty["status"] = "claimed"
            _save(heartbeat.BOUNTIES, board)

    # Complete
    _advance_journal_to(evaluation.submission_id, _JOURNAL_COMPLETE)

    return {"bounty": dict(bounty), "review": review_record}


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
        _save(heartbeat.BOUNTIES, board)
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
    _save(heartbeat.BOUNTIES, board)
    return {"bounty": dict(bounty), "review": review_record}


# ── Public entry point ────────────────────────────────────────────────────


def bounty_review(
    review_input: FinalEvaluation | ManualReviewRequest,
) -> dict[str, Any] | None:
    if isinstance(review_input, ManualReviewRequest):
        return _bounty_review_manual(review_input)
    return _bounty_review_automatic(review_input)
