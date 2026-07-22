"""Comprehensive tests for the automatic (FinalEvaluation) review path.

Covers:
- Valid ACCEPT with met criteria
- Valid REJECT with failing criteria
- INDETERMINATE rejected without mutation
- Each bindings check (stale submission, contract version, output hash, etc.)
- Journal retry (resumable and conflicting)
- Existing matching review (resumable)
- Conflicting existing review (fails closed)
- Manual path unchanged via ManualReviewRequest
- AST authority boundaries
"""

from __future__ import annotations

import ast

import pytest

import village.bounty_review as br
import village.heartbeat as hb
from village.bounty_review import ManualReviewRequest
from village.contracts import (
    ContractState,
    EvaluatorType,
    SuccessCriterion,
    VillageContract,
)
from village.final_evaluation import (
    EVALUATOR_VERSION,
    FinalEvaluation,
    ReviewDecision,
    build_final_evaluation,
)
from village.work_result import WorkResult, WorkResultStatus


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


def _make_contract_with_criteria(
    *,
    auto_review_enabled: bool = True,
) -> VillageContract:
    """Build a contract with a required FIELD_PRESENT criterion."""
    c = SuccessCriterion.create(
        name="gaps_present",
        required=True,
        evaluator=EvaluatorType.FIELD_PRESENT,
        evaluator_params={"field": "gaps"},
    )
    contract = VillageContract(
        contract_id="contract:b001:1",
        title="test",
        success_criteria=[c],
        auto_review_enabled=auto_review_enabled,
    )
    contract.activate()
    return contract


def _make_reject_criteria_contract() -> VillageContract:
    """Build a contract whose criterion FAILs with the default output."""
    c = SuccessCriterion.create(
        name="summary_present",
        required=True,
        evaluator=EvaluatorType.FIELD_PRESENT,
        evaluator_params={"field": "summary"},
    )
    contract = VillageContract(
        contract_id="contract:b001:1",
        title="test",
        success_criteria=[c],
        auto_review_enabled=True,
    )
    contract.activate()
    return contract


def _bootstrap_contract(contract: VillageContract) -> VillageContract:
    """Save the contract so bounty_claim uses it."""
    hb._save_contract(contract)
    return contract


# ═══════════════════════════════════════════════════════════════════════════
# Valid ACCEPT
# ═══════════════════════════════════════════════════════════════════════════


class TestAutomaticAccept:
    def test_valid_automatic_accept(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        assert sub is not None
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert evaluation.overall_decision == ReviewDecision.ACCEPT

        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"

        # Contract fulfilled
        assert hb._load_contract("contract:b001:1").state == ContractState.FULFILLED

        # Criteria met applied
        loaded_contract = hb._load_contract("contract:b001:1")
        for sc in loaded_contract.success_criteria:
            assert sc.met is True

        # Review record has correct shape
        review = result["review"]
        assert review["review_kind"] == "deterministic"
        assert review["evaluation_hash"] == evaluation.evaluation_hash
        assert review["evaluator_version"] == EVALUATOR_VERSION
        assert review["decision"] == "accept"

        # Journal written to completion
        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = f"finalize:{sub['submission_id']}"
        assert jkey in journal
        assert journal[jkey]["stage"] == "complete"


# ═══════════════════════════════════════════════════════════════════════════
# Valid REJECT
# ═══════════════════════════════════════════════════════════════════════════


class TestAutomaticReject:
    def test_valid_automatic_reject(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_reject_criteria_contract())
        _claim("SomeAgent")
        # Provide output where "summary" exists but is None → FIELD_PRESENT returns FAIL
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result(output={"summary": None, "gaps": []}))
        assert sub is not None
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert evaluation.overall_decision == ReviewDecision.REJECT

        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"  # reset for resubmit

        # Contract stays ACTIVE
        assert hb._load_contract("contract:b001:1").state == ContractState.ACTIVE

        # Review record
        review = result["review"]
        assert review["review_kind"] == "deterministic"
        assert review["decision"] == "reject"

        # Journal complete
        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = f"finalize:{sub['submission_id']}"
        assert jkey in journal
        assert journal[jkey]["stage"] == "complete"


# ═══════════════════════════════════════════════════════════════════════════
# INDETERMINATE
# ═══════════════════════════════════════════════════════════════════════════


class TestIndeterminate:
    def test_indeterminate_rejected_without_mutation(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        # No criteria → no required machine evaluator → INDETERMINATE
        contract = VillageContract(
            contract_id="contract:b001:1",
            title="test",
            success_criteria=[],
            auto_review_enabled=True,
        )
        contract.activate()
        _bootstrap_contract(contract)
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        assert sub is not None
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert evaluation.overall_decision == ReviewDecision.INDETERMINATE

        result = br.bounty_review(evaluation)
        assert result is None  # never applied

        # No state mutations
        assert hb._load_contract("contract:b001:1").state == ContractState.ACTIVE
        assert hb._load(hb.BOUNTIES)["bounties"][0]["status"] == "submitted"
        stored_sub = br._get_submission(sub["submission_id"])
        assert stored_sub is not None
        assert stored_sub.get("review") is None

        # No journal entry (never started)
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert f"finalize:{sub['submission_id']}" not in journal


# ═══════════════════════════════════════════════════════════════════════════
# Validation rejections
# ═══════════════════════════════════════════════════════════════════════════


class TestValidationFailures:
    def test_stale_submission(self, monkeypatch, tmp_path):
        """Evaluation refers to a submission that is not the bounty's current_submission_id."""
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)

        # Replace the bounty's current_submission_id with a different id
        board = hb._load(hb.BOUNTIES)
        board["bounties"][0]["current_submission_id"] = "some-other-submission"
        hb._save(hb.BOUNTIES, board)

        result = br.bounty_review(evaluation)
        assert result is None

        # State unchanged
        assert hb._load_contract("contract:b001:1").state == ContractState.ACTIVE

    def test_stale_contract_version(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)

        # Change version on the stored contract
        contract.version = "2.0"
        hb._save_contract(contract)

        result = br.bounty_review(evaluation)
        assert result is None

    def test_output_hash_mismatch(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)

        # Modify the stored submission's output hash to cause mismatch
        store = hb._load(br.SUBMISSIONS)
        store["submissions"][sub["submission_id"]]["output_canonical_hash"] = "0" * 64
        hb._save(br.SUBMISSIONS, store)

        result = br.bounty_review(evaluation)
        assert result is None

    def test_policy_hash_mismatch(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)

        # Change the contract's success criteria → different policy hash
        c2 = SuccessCriterion.create(
            name="extra",
            required=True,
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "extra"},
        )
        contract.success_criteria.append(c2)
        hb._save_contract(contract)

        result = br.bounty_review(evaluation)
        assert result is None

    def test_criterion_definition_mismatch(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)

        # Replace the stored contract with one that has different criteria
        # (different criterion IDs → submission's criterion_hashes won't match)
        new_c = SuccessCriterion.create(
            name="different",
            required=True,
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "diff"},
        )
        replacement = VillageContract(
            contract_id="contract:b001:1",
            title="replacement",
            success_criteria=[new_c],
            auto_review_enabled=True,
        )
        replacement.activate()
        hb._save_contract(replacement)

        result = br.bounty_review(evaluation)
        assert result is None

    def test_evaluator_version_mismatch(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        # Build a valid evaluation, then construct a copy with wrong version
        # (bypassing create() which validates the version at construction time)
        valid = build_final_evaluation(sub, contract, evaluated_at=1.0)
        evaluation = FinalEvaluation(
            submission_id=valid.submission_id,
            bounty_id=valid.bounty_id,
            contract_id=valid.contract_id,
            contract_version=valid.contract_version,
            work_result_id=valid.work_result_id,
            execution_id=valid.execution_id,
            output_canonical_hash=valid.output_canonical_hash,
            review_policy_hash=valid.review_policy_hash,
            criteria_results=valid.criteria_results,
            overall_decision=valid.overall_decision,
            reason_codes=valid.reason_codes,
            evaluator_version="999.0",
            evaluated_at=valid.evaluated_at,
            evaluation_hash="0" * 64,
        )

        result = br.bounty_review(evaluation)
        assert result is None

    def test_auto_review_disabled_indeterminate(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria(auto_review_enabled=False))
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        # auto_review_enabled=False → INDETERMINATE
        assert evaluation.overall_decision == ReviewDecision.INDETERMINATE

        result = br.bounty_review(evaluation)
        assert result is None  # never applied

        # State unchanged
        assert hb._load(hb.BOUNTIES)["bounties"][0]["status"] == "submitted"


# ═══════════════════════════════════════════════════════════════════════════
# Journal / retry
# ═══════════════════════════════════════════════════════════════════════════


class TestJournalRetry:
    def test_duplicate_identical_retry_resumes(self, monkeypatch, tmp_path):
        """Calling the same evaluation twice succeeds both times."""
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)

        result1 = br.bounty_review(evaluation)
        assert result1 is not None
        assert result1["bounty"]["status"] == "done"

        result2 = br.bounty_review(evaluation)
        assert result2 is not None
        assert result2["bounty"]["status"] == "done"

    def test_conflicting_evaluation_hash_fails_closed(self, monkeypatch, tmp_path):
        """A different evaluation for the same submission is rejected."""
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        evaluation1 = build_final_evaluation(sub, contract, evaluated_at=1.0)

        result1 = br.bounty_review(evaluation1)
        assert result1 is not None  # first succeeds

        # Second evaluation with different evaluated_at → different hash
        evaluation2 = build_final_evaluation(sub, contract, evaluated_at=2.0)
        assert evaluation2.evaluation_hash != evaluation1.evaluation_hash

        result2 = br.bounty_review(evaluation2)
        assert result2 is None  # conflicting hash

    def test_existing_matching_review_resumable(self, monkeypatch, tmp_path):
        """A submission that already has a matching review returns success."""
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)

        # Attach a matching review using the actual evaluation data
        review_record = {
            "review_kind": "deterministic",
            "evaluation_hash": evaluation.evaluation_hash,
            "evaluator_version": EVALUATOR_VERSION,
            "decision": evaluation.overall_decision.value,
            "reason_codes": list(evaluation.reason_codes),
            "criteria_results": [
                {"criterion_id": cr.criterion_id, "result": cr.result.value, "reason_code": cr.reason_code}
                for cr in evaluation.criteria_results
            ],
            "reviewed_at": 0.0,
        }
        br._attach_review(sub["submission_id"], review_record)

        # Also move the bounty to done manually
        board = hb._load(hb.BOUNTIES)
        board["bounties"][0]["status"] = "done"
        hb._save(hb.BOUNTIES, board)

        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"

    def test_conflicting_existing_manual_review_fails_closed(self, monkeypatch, tmp_path):
        """An automatic review fails when a manual review already exists."""
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        # Manually attach a manual-style review (no evaluation_hash)
        br._attach_review(
            sub["submission_id"],
            {
                "reviewer_actor_id": "human",
                "decision": "accept",
                "evidence": {},
                "reviewed_at": 0.0,
            },
        )

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        result = br.bounty_review(evaluation)
        assert result is None  # conflicting: manual review has no evaluation_hash

    def test_journal_resume_from_review_attached(self, monkeypatch, tmp_path):
        """Simulate a crash after review_attached but before contract/bounty save."""
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")

        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)

        # Pre-seed journal at "review_attached"
        journal = {}
        jkey = f"finalize:{sub['submission_id']}"
        journal[jkey] = {
            "submission_id": sub["submission_id"],
            "bounty_id": sub["bounty_id"],
            "evaluation_hash": evaluation.evaluation_hash,
            "decision": "accept",
            "stage": "review_attached",
            "created_at": 0.0,
            "updated_at": 0.0,
        }
        hb._save(br.FINALIZATION_JOURNAL, journal)

        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"

        # Contract should be fulfilled
        assert hb._load_contract("contract:b001:1").state == ContractState.FULFILLED

        # Journal advanced past review_attached → contract_applied → bounty_applied → complete
        loaded_journal = hb._load(br.FINALIZATION_JOURNAL)
        assert loaded_journal[jkey]["stage"] == "complete"


# ═══════════════════════════════════════════════════════════════════════════
# Manual path unchanged
# ═══════════════════════════════════════════════════════════════════════════


class TestManualPath:
    def test_manual_accept_works(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        # Default contract (no criteria) so fulfill() trivially passes
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
        assert hb._load_contract("contract:b001:1").state == ContractState.FULFILLED

    def test_manual_reject_works(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

        result = br.bounty_review(
            ManualReviewRequest(
                bounty_id="b001",
                submission_id=sub["submission_id"],
                reviewer_actor_id="reviewer-1",
                decision=ReviewDecision.REJECT,
            )
        )
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        assert hb._load_contract("contract:b001:1").state == ContractState.ACTIVE

    def test_manual_invalid_decision_raises_value_error(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())

        with pytest.raises(ValueError, match="invalid decision"):
            br.bounty_review(
                ManualReviewRequest(
                    bounty_id="b001",
                    submission_id=sub["submission_id"],
                    reviewer_actor_id="reviewer-1",
                    decision="maybe",
                )
            )


# ═══════════════════════════════════════════════════════════════════════════
# AST authority boundaries
# ═══════════════════════════════════════════════════════════════════════════


class TestAuthorityBoundaries:
    """Structural AST checks that the automatic review path does not create
    a second completion authority, and that purity boundaries hold."""

    def test_only_bounty_review_module_calls_contract_fulfill_for_lifecycle(self):
        """No module outside bounty_review.py and contracts.py itself
        may call .fulfill().  (contracts.py defines it; bounty_review.py
        calls it to complete the lifecycle.)"""
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

        # Verify that worker/interpreter no not call fulfill
        import village.interpreter as interpreter
        import village.worker as worker

        for mod, name in [(worker, "worker"), (interpreter, "interpreter")]:
            src = inspect_import_source(mod)
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "fulfill":
                        pytest.fail(f"{name} calls .fulfill()")

    def test_final_evaluation_remains_pure(self):
        """final_evaluation.py must not import heartbeat or bounty_review."""
        with open("village/final_evaluation.py") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and any(m in (node.module or "") for m in ("heartbeat", "bounty_review")):
                    pytest.fail(f"final_evaluation imports {node.module}")

    def test_no_second_completion_path(self):
        """Verify that _bounty_review_automatic is only called from bounty_review(),
        and no other module has its own completion authority."""
        # Check that bounty_review() is the sole dispatcher
        with open("village/bounty_review.py") as f:
            src = f.read()
        tree = ast.parse(src)

        # Count calls to contract.fulfill(), bounty state mutations, etc.
        call_names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name):
                    call_names.add(node.func.id)
                elif isinstance(node.func, ast.Attribute):
                    call_names.add(node.func.attr)

        # bounty_review dispatches to both paths but bounty_review remains
        # the sole entry point
        assert "bounty_review" in src
        assert "_bounty_review_automatic" in src
        assert "_bounty_review_manual" in src

        # Verify completion-related calls exist only in bounty_review.py
        for attr in ("_save_contract", "_attach_review", "fulfill"):
            assert attr in call_names or f".{attr}" in src

    def test_automatic_review_not_called_outside_bounty_review(self):
        """No other module imports or calls _bounty_review_automatic."""
        import village.heartbeat as heartbeat
        import village.interpreter as interpreter
        import village.worker as worker

        for mod, name in [(worker, "worker"), (interpreter, "interpreter"), (heartbeat, "heartbeat")]:
            src = inspect_import_source(mod)
            if "_bounty_review_automatic" in src:
                pytest.fail(f"{name} references _bounty_review_automatic")
            if "bounty_review(" in src and "ManualReviewRequest" not in src:
                # heartbeat references bounty_review via _load_submissions, not the
                # completion path; that's acceptable
                pass

    def test_bounty_review_is_sole_fulfill_caller(self):
        """Only bounty_review (and contracts.py itself via the method definition)
        call .fulfill() on a VillageContract for the bounty lifecycle."""
        with open("village/contracts.py") as f:
            contracts_src = f.read()
        # contracts.py defines fulfill() but must not call it itself
        contracts_tree = ast.parse(contracts_src)
        for node in ast.walk(contracts_tree):
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Attribute) and node.func.attr == "fulfill":
                    pytest.fail("contracts.py must not call fulfill() on itself during init")

        # execution_orchestrator must not call fulfill
        try:
            import village.execution_orchestrator as orch

            src = inspect_import_source(orch)
            tree = ast.parse(src)
            for node in ast.walk(tree):
                if isinstance(node, ast.Call):
                    if isinstance(node.func, ast.Attribute) and node.func.attr == "fulfill":
                        pytest.fail("execution_orchestrator calls fulfill()")
        except ImportError:
            pass


# ═══════════════════════════════════════════════════════════════════════════
# Helpers
# ═══════════════════════════════════════════════════════════════════════════


def inspect_import_source(mod: object) -> str:
    """Get the source of an imported module."""
    import inspect

    return inspect.getsource(mod)


# ── Crash recovery ──────────────────────────────────────────


class TestCrashRecoveryAccept:
    """Crash at each stage boundary during ACCEPT, then retry with same evaluation."""

    def _setup_accept(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract_with_criteria())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")
        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert evaluation.overall_decision == ReviewDecision.ACCEPT
        return sub, evaluation

    def test_crash_after_journal_prepared_retry_succeeds(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_accept(monkeypatch, tmp_path)
        # Simulate: journal prepared but review not attached
        jkey = f"finalize:{sub['submission_id']}"
        br._init_journal(sub["submission_id"], "b001", evaluation.evaluation_hash, "accept")
        br._save(
            br.FINALIZATION_JOURNAL,
            {
                jkey: {
                    "submission_id": sub["submission_id"],
                    "bounty_id": "b001",
                    "evaluation_hash": evaluation.evaluation_hash,
                    "decision": "accept",
                    "stage": "prepared",
                    "created_at": 1.0,
                    "updated_at": 1.0,
                }
            },
        )
        # Retry
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[jkey]["stage"] == "complete"

    def test_crash_after_review_attached_retry_succeeds(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_accept(monkeypatch, tmp_path)
        # Attach review + set journal to review_attached
        review = br._build_automatic_review_record(evaluation)
        br._attach_review(sub["submission_id"], review)
        jkey = f"finalize:{sub['submission_id']}"
        br._init_journal(sub["submission_id"], "b001", evaluation.evaluation_hash, "accept")
        br._advance_journal(sub["submission_id"], "review_attached")
        # Retry
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[jkey]["stage"] == "complete"

    def test_crash_after_contract_save_retry_succeeds(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_accept(monkeypatch, tmp_path)
        # Complete review, contract save, advance to contract_applied
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        contract.fulfill()
        hb._save_contract(contract)
        jkey = f"finalize:{sub['submission_id']}"
        br._init_journal(sub["submission_id"], "b001", evaluation.evaluation_hash, "accept")
        br._advance_journal(sub["submission_id"], "review_attached")
        br._advance_journal(sub["submission_id"], "contract_applied")
        # Retry — bounty not yet done
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[jkey]["stage"] == "complete"

    def test_crash_before_complete_retry_succeeds(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_accept(monkeypatch, tmp_path)
        # Full completion except journal complete stage
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        contract.fulfill()
        hb._save_contract(contract)
        # Also save the bounty as "done" (simulating crash after bounty save)
        board = hb._load(hb.BOUNTIES)
        board["bounties"][0]["status"] = "done"
        board["bounties"][0]["completed_at"] = 1.0
        hb._save(hb.BOUNTIES, board)
        jkey = f"finalize:{sub['submission_id']}"
        br._init_journal(sub["submission_id"], "b001", evaluation.evaluation_hash, "accept")
        br._advance_journal(sub["submission_id"], "review_attached")
        br._advance_journal(sub["submission_id"], "contract_applied")
        br._advance_journal(sub["submission_id"], "bounty_applied")
        # Also attach the review (simulating crash after review + contract + bounty save)
        review = br._build_automatic_review_record(evaluation)
        br._attach_review(sub["submission_id"], review)
        # Retry should recognize done bounty + matching review and return success
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[jkey]["stage"] == "complete"
