"""
Tests for village/bounty_review.py -- the submission/review gate that
closes the loop opened by village/worker.py's WorkResult (docs/research/
BOUNTY_REVIEW_GATE_01.md). Covers the full open -> claimed -> submitted
-> done lifecycle plus the reject -> claimed -> resubmit cycle.
"""

from __future__ import annotations

import json

import pytest

import village.bounty_review as br
import village.heartbeat as hb
from village.bounty_review import ManualReviewRequest
from village.contracts import ContractState
from village.final_evaluation import ReviewDecision
from village.work_result import WorkResult, WorkResultStatus


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "BOUNTIES", tmp_path / "bounties.json")
    monkeypatch.setattr(hb, "CONTRACTS", tmp_path / "contracts.json")
    monkeypatch.setattr(br, "SUBMISSIONS", tmp_path / "bounty_submissions.json")
    hb._save(
        hb.BOUNTIES,
        {
            "bounties": [
                {
                    "id": "b001",
                    "title": "Review village/heartbeat.py",
                    "description": "Read and review the heartbeat scanner.",
                    "reward": "reputation",
                    "status": "open",
                    "created_by": "agent-village",
                    "created_at": 0.0,
                    "claimed_by": None,
                    "claimed_at": None,
                    "completed_at": None,
                }
            ]
        },
    )


def _claim(actor_id="SomeAgent"):
    return hb.bounty_claim("b001", actor_id)


def _succeeded_work_result(execution_id="exec-1", output=None) -> WorkResult:
    return WorkResult(
        work_result_id=f"workresult:contract:b001:1:{execution_id}",
        contract_id="contract:b001:1",
        execution_id=execution_id,
        provider="deepseek",
        model="deepseek-v4-flash",
        status=WorkResultStatus.SUCCEEDED,
        output=output or {"gaps": [{"description": "x", "file": "village/heartbeat.py", "line": 1}]},
        evidence={"target_file": "village/heartbeat.py", "instruction": "analyze"},
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.001},
    )


# ── Submission: happy path ────────────────────────────────────────────────


def test_valid_submission_moves_bounty_to_submitted(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")

    result = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    assert result is not None
    assert result["status"] == "succeeded"
    board = hb._load(hb.BOUNTIES)
    assert board["bounties"][0]["status"] == "submitted"
    assert board["bounties"][0]["current_submission_id"] == result["submission_id"]


def test_submission_leaves_contract_active(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")

    br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    contract = hb._load_contract("contract:b001:1")
    assert contract.state == ContractState.ACTIVE


def test_submission_persists_required_fields(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()

    result = br.bounty_submit("b001", "SomeAgent", wr)

    for field in (
        "work_result_id",
        "contract_id",
        "execution_id",
        "actor_id",
        "provider",
        "model",
        "status",
        "evidence",
        "submitted_at",
    ):
        assert field in result, f"missing field: {field}"
    assert result["work_result_id"] == wr.work_result_id
    assert result["contract_id"] == wr.contract_id
    assert result["execution_id"] == wr.execution_id
    assert result["actor_id"] == "SomeAgent"
    assert result["provider"] == "deepseek"
    assert result["model"] == "deepseek-v4-flash"
    assert result["status"] == "succeeded"


# ── Submission: only SUCCEEDED is submittable ──────────────────────────────


def test_failed_status_not_submittable(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.status = WorkResultStatus.FAILED

    result = br.bounty_submit("b001", "SomeAgent", wr)

    assert result is None
    assert hb._load(hb.BOUNTIES)["bounties"][0]["status"] == "claimed"


def test_invalid_output_status_not_submittable(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.status = WorkResultStatus.INVALID_OUTPUT

    result = br.bounty_submit("b001", "SomeAgent", wr)
    assert result is None


def test_provider_error_status_not_submittable(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.status = WorkResultStatus.PROVIDER_ERROR

    result = br.bounty_submit("b001", "SomeAgent", wr)
    assert result is None


def test_budget_exceeded_status_not_submittable(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.status = WorkResultStatus.BUDGET_EXCEEDED

    result = br.bounty_submit("b001", "SomeAgent", wr)
    assert result is None


# ── Submission: authority checks ───────────────────────────────────────────


def test_wrong_actor_is_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("RealClaimant")

    result = br.bounty_submit("b001", "ImposterAgent", _succeeded_work_result())

    assert result is None
    assert hb._load(hb.BOUNTIES)["bounties"][0]["status"] == "claimed"


def test_foreign_execution_contract_id_is_rejected(monkeypatch, tmp_path):
    """A WorkResult whose contract_id doesn't match THIS bounty's own
    deterministic contract id -- e.g. copy-pasted from a different
    bounty's execution -- must be rejected."""
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.contract_id = "contract:some-other-bounty:1"

    result = br.bounty_submit("b001", "SomeAgent", wr)
    assert result is None


def test_missing_contract_is_rejected(monkeypatch, tmp_path):
    """Simulates a bounty claimed via the legacy pre-contract path (no
    contracts.json entry)."""
    _setup(monkeypatch, tmp_path)
    board = hb._load(hb.BOUNTIES)
    board["bounties"][0]["status"] = "claimed"
    board["bounties"][0]["claimed_by"] = "SomeAgent"
    hb._save(hb.BOUNTIES, board)
    assert hb._load_contract("contract:b001:1") is None

    result = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
    assert result is None


# ── Submission: status-gate (open/submitted/done not (re)submittable) ────


def test_open_bounty_is_not_submittable(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)  # still "open", never claimed
    result = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
    assert result is None


def test_already_submitted_bounty_rejects_a_second_submit(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    br.bounty_submit("b001", "SomeAgent", _succeeded_work_result(execution_id="exec-1"))

    # A second, different submission attempt while still "submitted" --
    # explicitly, deterministically rejected (not silently idempotent).
    result = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result(execution_id="exec-2"))
    assert result is None


def test_done_bounty_is_not_submittable(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
    br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=sub["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.ACCEPT,
        )
    )
    assert hb._load(hb.BOUNTIES)["bounties"][0]["status"] == "done"

    result = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result(execution_id="exec-late"))
    assert result is None


# ── Submission: atomicity ──────────────────────────────────────────────────


def test_invalid_submission_does_not_partially_mutate_any_file(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("RealClaimant")
    bounties_before = hb._load(hb.BOUNTIES)
    contracts_before = hb._load(hb.CONTRACTS)

    result = br.bounty_submit("b001", "WrongActor", _succeeded_work_result())

    assert result is None
    assert hb._load(hb.BOUNTIES) == bounties_before
    assert hb._load(hb.CONTRACTS) == contracts_before
    assert hb._load(br.SUBMISSIONS) == {}


# ── Submission: no secrets in evidence ─────────────────────────────────────


def test_evidence_never_contains_secret_shaped_keys(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.evidence["api_key"] = "sk-should-never-appear"
    wr.evidence["nested"] = {"authorization_header": "Bearer sk-also-should-not-appear"}

    result = br.bounty_submit("b001", "SomeAgent", wr)

    serialized = json.dumps(result)
    assert "sk-should-never-appear" not in serialized
    assert "sk-also-should-not-appear" not in serialized
    assert "api_key" not in result["evidence"]
    assert "nested" not in result["evidence"] or "authorization_header" not in result["evidence"].get("nested", {})


# ── Submission: audit data survives JSON roundtrip ────────────────────────


def test_submission_survives_json_roundtrip(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    result = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    reloaded = json.loads((tmp_path / "bounty_submissions.json").read_text())
    stored = reloaded["submissions"][result["submission_id"]]
    assert stored == result


# ── Review: accept ──────────────────────────────────────────────────────────


def test_accept_moves_submitted_to_done(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    result = br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=sub["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.ACCEPT,
        )
    )

    assert result is not None
    assert result["bounty"]["status"] == "done"


def test_accept_fulfills_the_contract_only_now(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
    assert hb._load_contract("contract:b001:1").state == ContractState.ACTIVE  # not yet

    br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=sub["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.ACCEPT,
        )
    )

    assert hb._load_contract("contract:b001:1").state == ContractState.FULFILLED


def test_accept_persists_reviewer_and_decision(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    submission = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    result = br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=submission["submission_id"],
            reviewer_actor_id="reviewer-42",
            decision=ReviewDecision.ACCEPT,
        )
    )

    assert result["review"]["reviewer_actor_id"] == "reviewer-42"
    assert result["review"]["decision"] == "accept"
    assert "reviewed_at" in result["review"]
    stored = br._get_submission(submission["submission_id"])
    assert stored["review"]["reviewer_actor_id"] == "reviewer-42"


def test_accept_with_no_success_criteria_fulfills_trivially(monkeypatch, tmp_path):
    """No success_criteria set at all -- contract.fulfill()'s own rule
    (no REQUIRED criteria means nothing to check) applies, matching the
    pre-review-gate bounty_complete() semantics exactly for this case."""
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    result = br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=sub["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.ACCEPT,
        )
    )
    assert result is not None


def test_accept_with_unmet_required_criterion_is_refused(monkeypatch, tmp_path):
    """The core "don't invent a pass" guarantee: a required criterion
    that was never explicitly marked met=True blocks acceptance --
    deterministically, via contract.fulfill()'s own existing rule, not a
    judgment call made in this module."""
    _setup(monkeypatch, tmp_path)
    board = hb._load(hb.BOUNTIES)
    board["bounties"][0]["contract_terms"] = {
        "success_criteria": [{"name": "review_posted", "required": True}],
    }
    hb._save(hb.BOUNTIES, board)
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    result = br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=sub["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.ACCEPT,
        )
    )

    assert result is None
    assert hb._load(hb.BOUNTIES)["bounties"][0]["status"] == "submitted"  # unchanged
    assert hb._load_contract("contract:b001:1").state == ContractState.ACTIVE  # not fulfilled


def test_accept_with_met_required_criterion_fulfills_contract(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    board = hb._load(hb.BOUNTIES)
    board["bounties"][0]["contract_terms"] = {
        "success_criteria": [{"name": "review_posted", "required": True}],
    }
    hb._save(hb.BOUNTIES, board)
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    contract = hb._load_contract("contract:b001:1")
    contract.success_criteria[0].met = True
    hb._save_contract(contract)

    result = br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=sub["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.ACCEPT,
        )
    )

    assert result is not None
    assert hb._load_contract("contract:b001:1").state == ContractState.FULFILLED


# ── Review: reject ──────────────────────────────────────────────────────────


def test_reject_resets_bounty_to_claimed(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    result = br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=sub["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.REJECT,
            evidence={"reason": "missing coverage"},
        )
    )

    assert result is not None
    assert result["bounty"]["status"] == "claimed"
    assert hb._load(hb.BOUNTIES)["bounties"][0]["claimed_by"] == "SomeAgent"


def test_reject_leaves_contract_active(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=sub["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.REJECT,
        )
    )

    assert hb._load_contract("contract:b001:1").state == ContractState.ACTIVE


def test_rejected_work_result_stays_in_audit_history(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    submission = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=submission["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.REJECT,
            evidence={"reason": "not good enough"},
        )
    )

    stored = br._get_submission(submission["submission_id"])
    assert stored is not None  # not deleted
    assert stored["output"] == submission["output"]  # not overwritten
    assert stored["review"]["decision"] == "reject"


def test_resubmit_possible_after_reject(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    s1 = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result(execution_id="exec-1"))
    br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=s1["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.REJECT,
        )
    )

    result = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result(execution_id="exec-2"))

    assert result is not None
    assert hb._load(hb.BOUNTIES)["bounties"][0]["status"] == "submitted"
    # Both submissions preserved -- the rejected one AND the new one.
    all_submissions = hb._load(br.SUBMISSIONS)["submissions"]
    assert len(all_submissions) == 2


# ── Review: invalid decision / status gate ─────────────────────────────────


def test_invalid_decision_string_raises_value_error(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

    with pytest.raises(ValueError):
        br.bounty_review(
            ManualReviewRequest(
                bounty_id="b001", submission_id=sub["submission_id"], reviewer_actor_id="reviewer-1", decision="maybe"
            )
        )


def test_review_on_non_submitted_bounty_is_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")  # only "claimed", never submitted

    result = br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001", submission_id="no-op", reviewer_actor_id="reviewer-1", decision=ReviewDecision.ACCEPT
        )
    )
    assert result is None


def test_duplicate_review_after_already_done_is_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
    br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=sub["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.ACCEPT,
        )
    )

    result = br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=sub["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.ACCEPT,
        )
    )  # duplicate call
    assert result is None


# ── Authority: worker/interpreter cannot reach this module ────────────────
# (Covered structurally in tests/test_worker_no_write_authority.py --
# not duplicated here.)


# =============================================================================
# Regression tests, Kim's PR #15 review (docs/research/
# BOUNTY_REVIEW_GATE_01.md "Corrections" section)
# =============================================================================


# ── Blocker 1: submissions are immutable, never overwritten ──────────────


def test_resubmit_of_the_same_execution_after_reject_does_not_overwrite_the_first_record(monkeypatch, tmp_path):
    """The exact scenario from the review: submit -> reject -> claimed ->
    submit (SAME execution_id). Both records must exist afterwards, and
    the first one's reject review must still be intact -- not silently
    overwritten by the second submit call."""
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result(execution_id="exec-1")

    first = br.bounty_submit("b001", "SomeAgent", wr)
    br.bounty_review(
        ManualReviewRequest(
            bounty_id="b001",
            submission_id=first["submission_id"],
            reviewer_actor_id="reviewer-1",
            decision=ReviewDecision.REJECT,
            evidence={"reason": "not good enough"},
        )
    )

    # Resubmit the SAME execution_id (e.g. a naive retry of the same
    # work_result object) after the reject.
    second = br.bounty_submit("b001", "SomeAgent", wr)

    assert second is not None
    assert second["submission_id"] != first["submission_id"]  # distinct record

    all_submissions = hb._load(br.SUBMISSIONS)["submissions"]
    assert len(all_submissions) == 2

    first_stored = all_submissions[first["submission_id"]]
    assert first_stored["review"]["decision"] == "reject"  # preserved, not lost
    assert first_stored["review"]["evidence"]["reason"] == "not good enough"

    second_stored = all_submissions[second["submission_id"]]
    assert second_stored["review"] is None  # fresh record, not yet reviewed


def test_insert_submission_refuses_to_overwrite_an_existing_id(monkeypatch, tmp_path):
    """Storage-layer guarantee, independent of bounty_submit()'s own id
    generation: _insert_submission() itself refuses a duplicate key."""
    _setup(monkeypatch, tmp_path)
    submission = {
        "submission_id": "submission:b001:exec-1",
        "bounty_id": "b001",
        "work_result_id": "w",
        "contract_id": "c",
        "execution_id": "exec-1",
        "actor_id": "a",
        "provider": "p",
        "model": "m",
        "status": "succeeded",
        "output": {},
        "evidence": {},
        "submitted_at": 0.0,
        "review": None,
    }
    br._insert_submission(submission)

    with pytest.raises(RuntimeError, match="already exists"):
        br._insert_submission(submission)


def test_attach_review_refuses_to_overwrite_an_existing_review(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    submission = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
    br._attach_review(submission["submission_id"], {"decision": "reject", "reviewer_actor_id": "r1"})

    with pytest.raises(RuntimeError, match="already reviewed"):
        br._attach_review(submission["submission_id"], {"decision": "accept", "reviewer_actor_id": "r2"})


def test_multiple_reject_resubmit_cycles_preserve_full_history(monkeypatch, tmp_path):
    """Not just one reject/resubmit cycle -- three in a row, all three
    prior records must survive untouched."""
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result(execution_id="exec-1")

    for i in range(3):
        s = br.bounty_submit("b001", "SomeAgent", wr)
        br.bounty_review(
            ManualReviewRequest(
                bounty_id="b001",
                submission_id=s["submission_id"],
                reviewer_actor_id="reviewer-1",
                decision=ReviewDecision.REJECT,
                evidence={"attempt": i},
            )
        )

    br.bounty_submit("b001", "SomeAgent", wr)  # final submission, left un-reviewed

    all_submissions = hb._load(br.SUBMISSIONS)["submissions"]
    assert len(all_submissions) == 4  # 3 rejected + 1 pending
    rejected = [s for s in all_submissions.values() if s["review"] is not None]
    assert len(rejected) == 3
    assert {s["review"]["evidence"]["attempt"] for s in rejected} == {0, 1, 2}


# ── Blocker 2: evidence value scanning, not just key filtering ───────────


def test_evidence_value_containing_sk_token_is_redacted(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.evidence["notes"] = "debug info: sk-abcdefghij1234567890 was used somewhere"

    result = br.bounty_submit("b001", "SomeAgent", wr)

    assert "sk-abcdefghij1234567890" not in result["evidence"]["notes"]
    assert "[REDACTED]" in result["evidence"]["notes"]


def test_evidence_value_containing_bearer_token_is_redacted(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.evidence["notes"] = "header was Bearer abcdefghij1234567890XYZ"

    result = br.bounty_submit("b001", "SomeAgent", wr)

    assert "abcdefghij1234567890XYZ" not in result["evidence"]["notes"]
    assert "[REDACTED]" in result["evidence"]["notes"]


def test_evidence_value_containing_authorization_header_is_redacted(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.evidence["notes"] = "saw this in a log: Authorization: Basic dXNlcjpwYXNz"

    result = br.bounty_submit("b001", "SomeAgent", wr)

    assert "dXNlcjpwYXNz" not in result["evidence"]["notes"]
    assert "[REDACTED]" in result["evidence"]["notes"]


def test_evidence_value_redaction_applies_inside_nested_structures(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.evidence["phase_log"] = [{"note": "leaked sk-nestedvalue1234567890 here"}]

    result = br.bounty_submit("b001", "SomeAgent", wr)

    assert "sk-nestedvalue1234567890" not in str(result["evidence"])
    assert "[REDACTED]" in result["evidence"]["phase_log"][0]["note"]


def test_evidence_value_without_secret_pattern_is_left_unchanged(monkeypatch, tmp_path):
    """The redaction must not be so aggressive it mangles ordinary text."""
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    wr = _succeeded_work_result()
    wr.evidence["notes"] = "This is a perfectly ordinary analysis note about heartbeat.py."

    result = br.bounty_submit("b001", "SomeAgent", wr)

    assert result["evidence"]["notes"] == "This is a perfectly ordinary analysis note about heartbeat.py."
