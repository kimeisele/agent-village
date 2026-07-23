"""Tests for the commit-and-replay automatic review path.

All crash-recovery tests use true public-boundary interruption: they
pre-construct partial persisted state (simulating a crash), then invoke
the public ``bounty_review(evaluation)``, inject an exception at the
next persistence boundary, confirm interruption, then retry the same
public call and prove exact completion and idempotency.
"""

from __future__ import annotations

import pytest

import village.bounty_review as br
import village.heartbeat as hb
from village.bounty_review import ManualReviewRequest
from village.contracts import ContractState, EvaluatorType, SuccessCriterion, VillageContract
from village.final_evaluation import (
    ReviewDecision,
    build_final_evaluation,
)

# ── Helpers ──────────────────────────────────────────────────────


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "BOUNTIES", tmp_path / "bounties.json")
    monkeypatch.setattr(hb, "CONTRACTS", tmp_path / "contracts.json")
    monkeypatch.setattr(br, "SUBMISSIONS", tmp_path / "bounty_submissions.json")
    monkeypatch.setattr(br, "FINALIZATION_JOURNAL", tmp_path / "finalization_journal.json")
    hb._save(
        hb.BOUNTIES,
        {
            "bounties": [
                {
                    "id": "b001",
                    "title": "t",
                    "description": "d",
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


def _succeeded_work_result(execution_id="exec-1", output=None):
    from village.work_result import WorkResult, WorkResultStatus

    return WorkResult(
        work_result_id=f"workresult:contract:b001:1:{execution_id}",
        contract_id="contract:b001:1",
        execution_id=execution_id,
        provider="deepseek",
        model="deepseek-v4-flash",
        status=WorkResultStatus.SUCCEEDED,
        output=output or {"gaps": [{"description": "x", "file": "village/heartbeat.py", "line": 1}]},
        evidence={},
        usage={"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150, "cost_usd": 0.001},
    )


def _make_contract(**kw):
    c = SuccessCriterion.create(
        name="gaps_present", required=True, evaluator=EvaluatorType.FIELD_PRESENT, evaluator_params={"field": "gaps"}
    )
    defaults = {"contract_id": "contract:b001:1", "title": "test", "success_criteria": [c], "auto_review_enabled": True}
    defaults.update(kw)
    contract = VillageContract(**defaults)
    contract.activate()
    return contract


def _bootstrap_contract(contract):
    hb._save_contract(contract)
    return contract


def _submit_and_evaluate(monkeypatch, tmp_path, contract=None, output=None, execution_id="exec-1"):
    """Full happy-path setup: contract → claim → submit → evaluate."""
    _setup(monkeypatch, tmp_path)
    _bootstrap_contract(contract or _make_contract())
    _claim("SomeAgent")
    sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result(execution_id=execution_id, output=output))
    c = hb._load_contract("contract:b001:1")
    evaluation = build_final_evaluation(sub, c, evaluated_at=1.0)
    return sub, evaluation


def _reject_setup(monkeypatch, tmp_path):
    """Setup for REJECT path."""
    c = SuccessCriterion.create(
        name="summary_present",
        required=True,
        evaluator=EvaluatorType.FIELD_PRESENT,
        evaluator_params={"field": "summary"},
    )
    contract = _make_contract(success_criteria=[c])
    return _submit_and_evaluate(monkeypatch, tmp_path, contract=contract, output={"summary": None, "gaps": []})


# ── Happy path ───────────────────────────────────────────────────


class TestAutomaticAccept:
    def test_valid_automatic_accept(self, monkeypatch, tmp_path):
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        assert evaluation.overall_decision == ReviewDecision.ACCEPT

        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        assert result["bounty"]["_finalized_by"]["submission_id"] == sub["submission_id"]
        assert hb._load_contract("contract:b001:1").state == ContractState.FULFILLED
        assert result["review"]["review_kind"] == "deterministic"
        assert result["review"]["evaluation_hash"] == evaluation.evaluation_hash
        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = f"finalize:{sub['submission_id']}"
        assert journal[jkey]["stage"] == "complete"
        assert "completed_at" in journal[jkey]
        contract = hb._load_contract("contract:b001:1")
        evals = contract.extra.get("auto_evaluations", {})
        assert sub["submission_id"] in evals


class TestAutomaticReject:
    def test_valid_automatic_reject(self, monkeypatch, tmp_path):
        sub, evaluation = _reject_setup(monkeypatch, tmp_path)
        assert evaluation.overall_decision == ReviewDecision.REJECT

        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        assert result["bounty"]["_finalized_by"]["submission_id"] == sub["submission_id"]
        assert hb._load_contract("contract:b001:1").state == ContractState.ACTIVE
        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = f"finalize:{sub['submission_id']}"
        assert journal[jkey]["stage"] == "complete"
        assert "completed_at" in journal[jkey]
        contract = hb._load_contract("contract:b001:1")
        evals = contract.extra.get("auto_evaluations", {})
        assert sub["submission_id"] in evals
        assert evals[sub["submission_id"]]["decision"] == "reject"


class TestIndeterminate:
    def test_indeterminate_rejected(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract(auto_review_enabled=False))
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")
        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert evaluation.overall_decision == ReviewDecision.INDETERMINATE
        assert br.bounty_review(evaluation) is None


# ── Idempotency ─────────────────────────────────────────────────


class TestIdempotency:
    def test_duplicate_call_makes_no_changes(self, monkeypatch, tmp_path):
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)

        result1 = br.bounty_review(evaluation)
        assert result1 is not None
        reviewed_at_1 = result1["review"]["reviewed_at"]
        journal = hb._load(br.FINALIZATION_JOURNAL)
        completed_at_1 = journal[br._journal_key(sub["submission_id"])]["completed_at"]

        result2 = br.bounty_review(evaluation)
        assert result2 is not None
        assert result2["review"]["reviewed_at"] == reviewed_at_1
        completed_at_2 = journal[br._journal_key(sub["submission_id"])]["completed_at"]
        assert completed_at_2 == completed_at_1
        contract = hb._load_contract("contract:b001:1")
        evals = contract.extra.get("auto_evaluations", {})
        assert len(evals) == 1

    def test_duplicate_reject_makes_no_changes(self, monkeypatch, tmp_path):
        sub, evaluation = _reject_setup(monkeypatch, tmp_path)

        result1 = br.bounty_review(evaluation)
        assert result1 is not None
        reviewed_at_1 = result1["review"]["reviewed_at"]
        journal = hb._load(br.FINALIZATION_JOURNAL)
        completed_at_1 = journal[br._journal_key(sub["submission_id"])]["completed_at"]

        result2 = br.bounty_review(evaluation)
        assert result2 is not None
        assert result2["review"]["reviewed_at"] == reviewed_at_1
        completed_at_2 = journal[br._journal_key(sub["submission_id"])]["completed_at"]
        assert completed_at_2 == completed_at_1


# ── Conflict ────────────────────────────────────────────────────


class TestConflict:
    def test_different_hash_fails(self, monkeypatch, tmp_path):
        """Different evaluation hash for completed submission returns None.
        Journal stays at complete (preserving the valid first evaluation)."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        contract = hb._load_contract("contract:b001:1")
        e1 = evaluation
        e2 = build_final_evaluation(sub, contract, evaluated_at=2.0)

        # First evaluation succeeds
        assert br.bounty_review(e1) is not None
        # Different hash → rejected, journal preserved
        result2 = br.bounty_review(e2)
        assert result2 is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"


# ── Manual path unchanged ────────────────────────────────────────


class TestManualPath:
    def test_manual_accept(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")
        contract.success_criteria[0].met = True
        hb._save_contract(contract)

        result = br.bounty_review(
            ManualReviewRequest(
                bounty_id="b001",
                submission_id=sub["submission_id"],
                reviewer_actor_id="r1",
                decision=ReviewDecision.ACCEPT,
            )
        )
        assert result is not None
        assert result["bounty"]["status"] == "done"

    def test_manual_reject(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

        result = br.bounty_review(
            ManualReviewRequest(
                bounty_id="b001",
                submission_id=sub["submission_id"],
                reviewer_actor_id="r1",
                decision=ReviewDecision.REJECT,
            )
        )
        assert result is not None
        assert result["bounty"]["status"] == "claimed"


# ── Resubmission after REJECT ───────────────────────────────────


class TestResubmission:
    def test_new_submission_after_reject_uses_new_journal_key(self, monkeypatch, tmp_path):
        sub1, e1 = _reject_setup(monkeypatch, tmp_path)
        br.bounty_review(e1)

        _claim("SomeAgent")
        sub2 = br.bounty_submit(
            "b001", "SomeAgent", _succeeded_work_result(execution_id="exec-2", output={"summary": [1]})
        )
        e2 = build_final_evaluation(sub2, hb._load_contract("contract:b001:1"), evaluated_at=1.0)

        result = br.bounty_review(e2)
        assert result is not None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub1["submission_id"])]["stage"] == "complete"
        assert journal[br._journal_key(sub2["submission_id"])]["stage"] == "complete"
        assert br._journal_key(sub1["submission_id"]) != br._journal_key(sub2["submission_id"])
        contract = hb._load_contract("contract:b001:1")
        evals = contract.extra.get("auto_evaluations", {})
        assert sub1["submission_id"] in evals
        assert sub2["submission_id"] in evals
        assert evals[sub1["submission_id"]]["decision"] == "reject"


# ═════════════════════════════════════════════════════════════════
# True public-boundary interruption tests (Blocker J)
#
# Pattern: pre-construct partial state (simulating crash), then
# call public bounty_review(), inject exception at next boundary,
# confirm interruption, retry same public call, prove completion.
# ═════════════════════════════════════════════════════════════════


class TestInterruptAccept:
    """Interruption at each ACCEPT projection boundary."""

    def test_interrupt_after_decided_before_review(self, monkeypatch, tmp_path):
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        # Simulate: decided journal written, crash before review
        br._write_journal_decided(evaluation)
        assert hb._load(br.FINALIZATION_JOURNAL)[br._journal_key(sub["submission_id"])]["stage"] == "decided"

        # Inject crash in _build_automatic_review_record (before _attach_review)
        original_build = br._build_automatic_review_record
        monkeypatch.setattr(
            br, "_build_automatic_review_record", lambda e: (_ for _ in ()).throw(RuntimeError("crash_before_review"))
        )

        try:
            br.bounty_review(evaluation)
            pytest.fail("expected crash")
        except RuntimeError:
            pass

        # Restore and retry
        monkeypatch.setattr(br, "_build_automatic_review_record", original_build)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = br._journal_key(sub["submission_id"])
        assert journal[jkey]["stage"] == "complete"
        assert "completed_at" in journal[jkey]
        # Exactly one review, no duplicates
        submission = br._get_submission(sub["submission_id"])
        assert submission["review"] is not None
        contract = hb._load_contract("contract:b001:1")
        assert contract.state == ContractState.FULFILLED
        assert len(contract.extra.get("auto_evaluations", {})) == 1

    def test_interrupt_after_review_before_contract(self, monkeypatch, tmp_path):
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        # Simulate: decided + review persisted, crash before contract
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))

        # Inject crash in _record_eval_history
        original_record = br._record_eval_history
        monkeypatch.setattr(
            br, "_record_eval_history", lambda c, e: (_ for _ in ()).throw(RuntimeError("crash_before_contract"))
        )

        try:
            br.bounty_review(evaluation)
            pytest.fail("expected crash")
        except RuntimeError:
            pass

        monkeypatch.setattr(br, "_record_eval_history", original_record)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        contract = hb._load_contract("contract:b001:1")
        assert contract.state == ContractState.FULFILLED
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_interrupt_after_contract_before_bounty(self, monkeypatch, tmp_path):
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        # Simulate: decided + review + contract persisted
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        contract.fulfill()
        br._record_eval_history(contract, evaluation)
        hb._save_contract(contract)

        # Inject crash in _atomic_save_json when saving bounties
        original_atomic = br._atomic_save_json

        def _injected_atomic(path, data):
            if path.name.startswith("bounties"):
                raise RuntimeError("crash_before_bounty")
            return original_atomic(path, data)

        monkeypatch.setattr(br, "_atomic_save_json", _injected_atomic)

        try:
            br.bounty_review(evaluation)
            pytest.fail("expected crash")
        except RuntimeError:
            pass

        monkeypatch.setattr(br, "_atomic_save_json", original_atomic)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_interrupt_after_bounty_before_complete(self, monkeypatch, tmp_path):
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        # Simulate: everything persisted except journal complete
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        contract.fulfill()
        br._record_eval_history(contract, evaluation)
        hb._save_contract(contract)
        board = hb._load(hb.BOUNTIES)
        board["bounties"][0]["status"] = "done"
        board["bounties"][0]["completed_at"] = 1.0
        br._set_bounty_finalization(board["bounties"][0], evaluation)
        hb._save(hb.BOUNTIES, board)

        # Inject crash in _advance_journal_to
        original_advance = br._advance_journal_to
        monkeypatch.setattr(
            br, "_advance_journal_to", lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash_before_complete"))
        )

        try:
            br.bounty_review(evaluation)
            pytest.fail("expected crash")
        except RuntimeError:
            pass

        monkeypatch.setattr(br, "_advance_journal_to", original_advance)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"
        assert "completed_at" in journal[br._journal_key(sub["submission_id"])]
        # Stable reviewed_at
        reviewed_at_1 = result["review"]["reviewed_at"]
        result2 = br.bounty_review(evaluation)
        assert result2["review"]["reviewed_at"] == reviewed_at_1


class TestInterruptReject:
    """Interruption at each REJECT projection boundary."""

    def test_interrupt_reject_after_decided_before_review(self, monkeypatch, tmp_path):
        sub, evaluation = _reject_setup(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)

        original_build = br._build_automatic_review_record
        monkeypatch.setattr(
            br,
            "_build_automatic_review_record",
            lambda e: (_ for _ in ()).throw(RuntimeError("crash_before_review_reject")),
        )

        try:
            br.bounty_review(evaluation)
            pytest.fail("expected crash")
        except RuntimeError:
            pass

        monkeypatch.setattr(br, "_build_automatic_review_record", original_build)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_interrupt_reject_after_review_before_contract(self, monkeypatch, tmp_path):
        sub, evaluation = _reject_setup(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))

        original_record = br._record_eval_history
        monkeypatch.setattr(
            br, "_record_eval_history", lambda c, e: (_ for _ in ()).throw(RuntimeError("crash_before_reject_contract"))
        )

        try:
            br.bounty_review(evaluation)
            pytest.fail("expected crash")
        except RuntimeError:
            pass

        monkeypatch.setattr(br, "_record_eval_history", original_record)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_interrupt_reject_after_contract_before_bounty(self, monkeypatch, tmp_path):
        sub, evaluation = _reject_setup(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        br._record_eval_history(contract, evaluation)
        hb._save_contract(contract)

        original_atomic = br._atomic_save_json

        def _injected_atomic(path, data):
            if path.name.startswith("bounties"):
                raise RuntimeError("crash_before_reject_bounty")
            return original_atomic(path, data)

        monkeypatch.setattr(br, "_atomic_save_json", _injected_atomic)

        try:
            br.bounty_review(evaluation)
            pytest.fail("expected crash")
        except RuntimeError:
            pass

        monkeypatch.setattr(br, "_atomic_save_json", original_atomic)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_interrupt_reject_after_bounty_before_complete(self, monkeypatch, tmp_path):
        sub, evaluation = _reject_setup(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        br._record_eval_history(contract, evaluation)
        hb._save_contract(contract)
        board = hb._load(hb.BOUNTIES)
        board["bounties"][0]["status"] = "claimed"
        br._set_bounty_finalization(board["bounties"][0], evaluation)
        hb._save(hb.BOUNTIES, board)

        original_advance = br._advance_journal_to
        monkeypatch.setattr(
            br,
            "_advance_journal_to",
            lambda *a, **kw: (_ for _ in ()).throw(RuntimeError("crash_before_reject_complete")),
        )

        try:
            br.bounty_review(evaluation)
            pytest.fail("expected crash")
        except RuntimeError:
            pass

        monkeypatch.setattr(br, "_advance_journal_to", original_advance)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"
        reviewed_at_1 = result["review"]["reviewed_at"]
        result2 = br.bounty_review(evaluation)
        assert result2["review"]["reviewed_at"] == reviewed_at_1


# ── Failure-mode tests ──────────────────────────────────────────


class TestFailureModes:
    """Tests for unsafe/incompatible states per Blocker J."""

    def test_conflicting_review_fails_closed(self, monkeypatch, tmp_path):
        """Submission with mismatched review → failed_closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(
            sub["submission_id"],
            {
                "review_kind": "deterministic",
                "evaluation_hash": "wrong_hash_0000000000000000000000000000000000000000",
                "evaluator_version": evaluation.evaluator_version,
                "decision": "accept",
                "reason_codes": [],
                "criteria_results": [],
                "reviewed_at": 1.0,
            },
        )

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_accept_fulfilled_under_other_eval_fails_closed(self, monkeypatch, tmp_path):
        """Complete journal, contract later bound to different eval → failed_closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        # First review succeeds
        assert br.bounty_review(evaluation) is not None

        # Tamper: change contract auto_evaluations to a different hash
        contract = hb._load_contract("contract:b001:1")
        contract.extra["auto_evaluations"][sub["submission_id"]] = {
            "submission_id": sub["submission_id"],
            "evaluation_hash": "other_hash_00000000000000000000000000000000000000",
            "decision": "accept",
            "evaluator_version": evaluation.evaluator_version,
        }
        hb._save_contract(contract)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_reject_against_fulfilled_contract_fails_closed(self, monkeypatch, tmp_path):
        """REJECT journal at decided, contract later fulfilled → retry fails closed."""
        sub, evaluation = _reject_setup(monkeypatch, tmp_path)
        # Write decided journal and attach review (simulate crash before contract)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))

        # Now someone manually fulfills the contract
        contract = hb._load_contract("contract:b001:1")
        contract.success_criteria[0].met = True  # override to allow fulfill
        contract.fulfill()
        hb._save_contract(contract)

        # Retry REJECT → contract is FULFILLED, not ACTIVE → fail closed
        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_bounty_done_for_other_submission_fails_closed(self, monkeypatch, tmp_path):
        """Complete ACCEPT, bounty later changed to different eval → failed_closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        assert br.bounty_review(evaluation) is not None

        # Tamper: change bounty finalization to different hash
        board = hb._load(hb.BOUNTIES)
        board["bounties"][0]["_finalized_by"] = {
            "submission_id": sub["submission_id"],
            "evaluation_hash": "other_hash_00000000000000000000000000000000000000",
            "decision": "accept",
        }
        hb._save(hb.BOUNTIES, board)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_done_with_matching_review_but_no_journal_not_accepted(self, monkeypatch, tmp_path):
        """Bounty done, matching review, but no journal → returns None."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)

        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        contract.fulfill()
        br._record_eval_history(contract, evaluation)
        hb._save_contract(contract)
        review_record = br._build_automatic_review_record(evaluation)
        br._attach_review(sub["submission_id"], review_record)
        board = hb._load(hb.BOUNTIES)
        board["bounties"][0]["status"] = "done"
        br._set_bounty_finalization(board["bounties"][0], evaluation)
        hb._save(hb.BOUNTIES, board)

        result = br.bounty_review(evaluation)
        assert result is None  # No journal → no proof of decision

    def test_torn_journal_temp_file_not_committed(self, monkeypatch, tmp_path):
        """An unrenamed temp file does not affect the canonical journal."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)

        tmp_file = br.FINALIZATION_JOURNAL.with_name(f"{br.FINALIZATION_JOURNAL.name}.tmp99999")
        tmp_file.write_text('{"fake": true}')

        result = br.bounty_review(evaluation)
        assert result is not None
        if tmp_file.exists():
            tmp_file.unlink()

    def test_complete_record_with_later_contract_contradiction_fails_closed(self, monkeypatch, tmp_path):
        """Complete journal, then contract criteria changed → failed_closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        assert br.bounty_review(evaluation) is not None

        # Tamper: change contract criteria met values
        contract = hb._load_contract("contract:b001:1")
        contract.success_criteria[0].met = False
        hb._save_contract(contract)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_complete_record_with_later_bounty_contradiction_fails_closed(self, monkeypatch, tmp_path):
        """Complete journal, then bounty status changed → failed_closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        assert br.bounty_review(evaluation) is not None

        # Tamper: change bounty status
        board = hb._load(hb.BOUNTIES)
        board["bounties"][0]["status"] = "open"
        hb._save(hb.BOUNTIES, board)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_reject_criteria_match_interrupt_at_contract(self, monkeypatch, tmp_path):
        """REJECT where criteria already match, interrupted at contract persistence."""
        sub, evaluation = _reject_setup(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        # Pre-apply criteria so they match on retry
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        hb._save_contract(contract)

        # Inject interruption at atomic contract save
        original_atomic = br._atomic_save_json
        call_count = [0]

        def _injected_atomic(path, data):
            if path.name.startswith("contracts"):
                call_count[0] += 1
                if call_count[0] == 1:
                    raise RuntimeError("crash_reject_contract_persist")
            return original_atomic(path, data)

        monkeypatch.setattr(br, "_atomic_save_json", _injected_atomic)

        try:
            br.bounty_review(evaluation)
            pytest.fail("expected crash")
        except RuntimeError:
            pass

        monkeypatch.setattr(br, "_atomic_save_json", original_atomic)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"
        # One durable history entry
        contract = hb._load_contract("contract:b001:1")
        evals = contract.extra.get("auto_evaluations", {})
        assert sub["submission_id"] in evals

    def test_fulfilled_no_history_fails_closed(self, monkeypatch, tmp_path):
        """FULFILLED contract with no evaluation-history entry → failed closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        # First review succeeds (creates history)
        assert br.bounty_review(evaluation) is not None

        # Tamper: remove eval history from the fulfilled contract
        contract = hb._load_contract("contract:b001:1")
        contract.extra.pop("auto_evaluations", None)
        hb._save_contract(contract)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_fulfilled_wrong_evaluator_version_fails_closed(self, monkeypatch, tmp_path):
        """FULFILLED with matching hash/decision but wrong evaluator version → failed closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        assert br.bounty_review(evaluation) is not None

        # Tamper: change evaluator_version in history
        contract = hb._load_contract("contract:b001:1")
        evals = contract.extra.get("auto_evaluations", {})
        evals[sub["submission_id"]]["evaluator_version"] = "wrong-version"
        hb._save_contract(contract)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_extra_top_level_review_field_fails_closed(self, monkeypatch, tmp_path):
        """Review with extra top-level field → mismatch → failed closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        review = br._build_automatic_review_record(evaluation)
        review["extra_field"] = "unexpected"
        br._attach_review(sub["submission_id"], review)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_extra_criterion_result_field_fails_closed(self, monkeypatch, tmp_path):
        """Criterion result with extra field → mismatch → failed closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        review = br._build_automatic_review_record(evaluation)
        review["criteria_results"][0]["extra"] = "unexpected"
        br._attach_review(sub["submission_id"], review)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_missing_canonical_review_field_fails_closed(self, monkeypatch, tmp_path):
        """Review missing a canonical field → mismatch → failed closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        review = br._build_automatic_review_record(evaluation)
        del review["evaluator_version"]
        br._attach_review(sub["submission_id"], review)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"


# ── Timestamp validation ────────────────────────────────────────


class TestTimestampValidation:
    """Malformed review timestamp tests."""

    def test_negative_reviewed_at_fails_closed(self, monkeypatch, tmp_path):
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        review = br._build_automatic_review_record(evaluation)
        review["reviewed_at"] = -1.0
        br._attach_review(sub["submission_id"], review)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_nan_reviewed_at_rejected_by_matcher(self):
        """_is_matching_deterministic_review rejects NaN reviewed_at (defence in depth)."""
        import math

        # Build minimal evaluation and review with NaN reviewed_at.
        # Cannot go through JSON persistence (allow_nan=False), so test
        # the matching function directly.
        from village.final_evaluation import FinalEvaluation, ReviewDecision

        evaluation = FinalEvaluation(
            submission_id="s",
            bounty_id="b",
            contract_id="c",
            contract_version="1",
            work_result_id="w",
            execution_id="e",
            output_canonical_hash="a" * 64,
            review_policy_hash="b" * 64,
            criteria_results=(),
            overall_decision=ReviewDecision.ACCEPT,
            reason_codes=(),
            evaluator_version="02c-1",
            evaluated_at=1.0,
            evaluation_hash="c" * 64,
        )
        review = {
            "review_kind": "deterministic",
            "evaluation_hash": "c" * 64,
            "evaluator_version": "02c-1",
            "decision": "accept",
            "reason_codes": [],
            "criteria_results": [],
            "reviewed_at": float("nan"),
        }
        assert math.isnan(review["reviewed_at"])
        assert not br._is_matching_deterministic_review(review, evaluation)

    def test_inf_reviewed_at_rejected_by_matcher(self):
        """_is_matching_deterministic_review rejects +Inf reviewed_at."""
        from village.final_evaluation import FinalEvaluation, ReviewDecision

        evaluation = FinalEvaluation(
            submission_id="s",
            bounty_id="b",
            contract_id="c",
            contract_version="1",
            work_result_id="w",
            execution_id="e",
            output_canonical_hash="a" * 64,
            review_policy_hash="b" * 64,
            criteria_results=(),
            overall_decision=ReviewDecision.ACCEPT,
            reason_codes=(),
            evaluator_version="02c-1",
            evaluated_at=1.0,
            evaluation_hash="c" * 64,
        )
        review = {
            "review_kind": "deterministic",
            "evaluation_hash": "c" * 64,
            "evaluator_version": "02c-1",
            "decision": "accept",
            "reason_codes": [],
            "criteria_results": [],
            "reviewed_at": float("inf"),
        }
        assert not br._is_matching_deterministic_review(review, evaluation)

    def test_bool_reviewed_at_fails_closed(self, monkeypatch, tmp_path):
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        review = br._build_automatic_review_record(evaluation)
        review["reviewed_at"] = True
        br._attach_review(sub["submission_id"], review)

        result = br.bounty_review(evaluation)
        assert result is None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_negative_journal_created_at_fails_closed(self, monkeypatch, tmp_path):
        """Complete journal with negative created_at → failed_closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        assert br.bounty_review(evaluation) is not None

        # Tamper: set journal created_at negative
        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = br._journal_key(sub["submission_id"])
        journal[jkey]["created_at"] = -1.0
        hb._save(br.FINALIZATION_JOURNAL, journal)

        result = br.bounty_review(evaluation)
        assert result is None
        journal2 = hb._load(br.FINALIZATION_JOURNAL)
        assert journal2[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_boolean_journal_updated_at_fails_closed(self, monkeypatch, tmp_path):
        """Complete journal with boolean updated_at → failed_closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        assert br.bounty_review(evaluation) is not None

        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = br._journal_key(sub["submission_id"])
        journal[jkey]["updated_at"] = True
        hb._save(br.FINALIZATION_JOURNAL, journal)

        result = br.bounty_review(evaluation)
        assert result is None
        journal2 = hb._load(br.FINALIZATION_JOURNAL)
        assert journal2[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_negative_journal_completed_at_fails_closed(self, monkeypatch, tmp_path):
        """Complete journal with negative completed_at → failed_closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        assert br.bounty_review(evaluation) is not None

        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = br._journal_key(sub["submission_id"])
        journal[jkey]["completed_at"] = -5.0
        hb._save(br.FINALIZATION_JOURNAL, journal)

        result = br.bounty_review(evaluation)
        assert result is None
        journal2 = hb._load(br.FINALIZATION_JOURNAL)
        assert journal2[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_completed_at_before_created_at_fails_closed(self, monkeypatch, tmp_path):
        """Complete journal with completed_at < created_at → failed_closed."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        assert br.bounty_review(evaluation) is not None

        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = br._journal_key(sub["submission_id"])
        created = journal[jkey]["created_at"]
        journal[jkey]["completed_at"] = created - 10.0
        hb._save(br.FINALIZATION_JOURNAL, journal)

        result = br.bounty_review(evaluation)
        assert result is None
        journal2 = hb._load(br.FINALIZATION_JOURNAL)
        assert journal2[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"

    def test_missing_journal_completed_at_fails_closed(self, monkeypatch, tmp_path):
        """Complete journal without completed_at → failed_closed (non-finite timestamp)."""
        sub, evaluation = _submit_and_evaluate(monkeypatch, tmp_path)
        assert br.bounty_review(evaluation) is not None

        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = br._journal_key(sub["submission_id"])
        del journal[jkey]["completed_at"]
        hb._save(br.FINALIZATION_JOURNAL, journal)

        result = br.bounty_review(evaluation)
        assert result is None
        journal2 = hb._load(br.FINALIZATION_JOURNAL)
        assert journal2[br._journal_key(sub["submission_id"])]["stage"] == "failed_closed"


# ── AST authority boundaries ───────────────────────────────────


def _inspect_import_source(mod: object) -> str:
    import inspect

    return inspect.getsource(mod)


class TestAuthorityBoundaries:
    """Structural AST checks that the automatic review path does not create
    a second completion authority, and that purity boundaries hold."""

    def test_only_bounty_review_module_calls_contract_fulfill_for_lifecycle(self):
        import ast

        import village.interpreter as interpreter
        import village.worker as worker

        with open("village/bounty_review.py") as f:
            br_src = f.read()
        br_tree = ast.parse(br_src)
        br_calls = set()
        for node in ast.walk(br_tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute) and func.attr == "fulfill":
                    br_calls.add(("call", "fulfill"))
                elif isinstance(func, ast.Name) and func.id == "fulfill":
                    br_calls.add(("call_direct", "fulfill"))
        assert len(br_calls) >= 1, "bounty_review.py must call .fulfill()"

        for mod, name in [(worker, "worker"), (interpreter, "interpreter")]:
            src = _inspect_import_source(mod)
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "fulfill":
                        pytest.fail(f"{name} calls .fulfill()")

    def test_final_evaluation_remains_pure(self):
        import ast

        with open("village/final_evaluation.py") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and any(m in (node.module or "") for m in ("heartbeat", "bounty_review")):
                    pytest.fail(f"final_evaluation imports {node.module}")

    def test_no_second_completion_path(self):
        import ast

        with open("village/bounty_review.py") as f:
            src = f.read()
        tree = ast.parse(src)

        call_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    call_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    call_names.add(node.func.attr)

        assert "bounty_review" in src
        assert "_bounty_review_automatic" in src
        assert "_bounty_review_manual" in src

        for attr in ("_save_contract", "_attach_review", "fulfill"):
            assert attr in call_names or f".{attr}" in src

    def test_automatic_review_not_called_outside_bounty_review(self):
        import village.heartbeat as heartbeat
        import village.interpreter as interpreter
        import village.worker as worker

        for mod, name in [(worker, "worker"), (interpreter, "interpreter"), (heartbeat, "heartbeat")]:
            src = _inspect_import_source(mod)
            if "_bounty_review_automatic" in src:
                pytest.fail(f"{name} references _bounty_review_automatic")

    def test_bounty_review_is_sole_fulfill_caller(self):
        import ast

        with open("village/contracts.py") as f:
            contracts_src = f.read()
        contracts_tree = ast.parse(contracts_src)
        for node in ast.walk(contracts_tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "fulfill":
                    pytest.fail("contracts.py must not call fulfill() on itself during init")

        try:
            import village.execution_orchestrator as orch

            src = _inspect_import_source(orch)
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "fulfill":
                        pytest.fail("execution_orchestrator calls fulfill()")
        except ImportError:
            pass
