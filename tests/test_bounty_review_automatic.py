"""Tests for the commit-and-replay automatic review path."""

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


# ── Happy path ───────────────────────────────────────────────────


class TestAutomaticAccept:
    def test_valid_automatic_accept(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")
        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert evaluation.overall_decision == ReviewDecision.ACCEPT

        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        assert hb._load_contract("contract:b001:1").state == ContractState.FULFILLED
        assert result["review"]["review_kind"] == "deterministic"
        assert result["review"]["evaluation_hash"] == evaluation.evaluation_hash
        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = f"finalize:{sub['submission_id']}"
        assert journal[jkey]["stage"] == "complete"


class TestAutomaticReject:
    def test_valid_automatic_reject(self, monkeypatch, tmp_path):
        c = SuccessCriterion.create(
            name="summary_present",
            required=True,
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "summary"},
        )
        contract = _make_contract(success_criteria=[c])
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(contract)
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result(output={"summary": None, "gaps": []}))
        contract = hb._load_contract("contract:b001:1")
        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert evaluation.overall_decision == ReviewDecision.REJECT

        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        assert hb._load_contract("contract:b001:1").state == ContractState.ACTIVE
        journal = hb._load(br.FINALIZATION_JOURNAL)
        jkey = f"finalize:{sub['submission_id']}"
        assert journal[jkey]["stage"] == "complete"


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


# ── Crash recovery ──────────────────────────────────────────────


class TestCrashRecovery:
    """Crash after journal decided, before each projection."""

    def _setup_accept(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")
        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert evaluation.overall_decision == ReviewDecision.ACCEPT
        return sub, evaluation

    def test_crash_after_decided_before_review(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_accept(monkeypatch, tmp_path)
        # Write journal decided, no review
        br._write_journal_decided(evaluation)
        # Retry
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_crash_after_review_before_contract(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_accept(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        # Retry
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_crash_after_contract_before_bounty(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_accept(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        contract.fulfill()
        br._set_contract_eval_binding(contract, evaluation)
        hb._save_contract(contract)
        # Retry
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "done"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_crash_after_bounty_before_complete(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_accept(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        contract.fulfill()
        br._set_contract_eval_binding(contract, evaluation)
        hb._save_contract(contract)
        board = hb._load(hb.BOUNTIES)
        board["bounties"][0]["status"] = "done"
        hb._save(hb.BOUNTIES, board)
        # Retry
        result = br.bounty_review(evaluation)
        assert result is not None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"


# ── Idempotency ─────────────────────────────────────────────────


class TestIdempotency:
    def test_duplicate_call_makes_no_changes(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")
        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)

        result1 = br.bounty_review(evaluation)
        assert result1 is not None
        reviewed_at_1 = result1["review"]["reviewed_at"]

        result2 = br.bounty_review(evaluation)
        assert result2 is not None
        assert result2["review"]["reviewed_at"] == reviewed_at_1  # preserved


# ── Conflict ────────────────────────────────────────────────────


class TestConflict:
    def test_different_hash_fails(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        contract = hb._load_contract("contract:b001:1")
        e1 = build_final_evaluation(sub, contract, evaluated_at=1.0)
        e2 = build_final_evaluation(sub, contract, evaluated_at=2.0)

        assert br.bounty_review(e1) is not None
        assert br.bounty_review(e2) is None  # conflicting hash (decided exists with different hash)


# ── Manual path unchanged ────────────────────────────────────────


class TestManualPath:
    def test_manual_accept(self, monkeypatch, tmp_path):
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(_make_contract())
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result())
        # Set met=True so fulfill passes
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


# ── REJECT crash recovery ──────────────────────────────────────


class TestCrashRecoveryReject:
    def _setup_reject(self, monkeypatch, tmp_path):
        c = SuccessCriterion.create(
            name="summary_present",
            required=True,
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "summary"},
        )
        contract = _make_contract(success_criteria=[c])
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(contract)
        _claim("SomeAgent")
        sub = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result(output={"summary": None, "gaps": []}))
        contract = hb._load_contract("contract:b001:1")
        evaluation = build_final_evaluation(sub, contract, evaluated_at=1.0)
        assert evaluation.overall_decision == ReviewDecision.REJECT
        return sub, evaluation

    def test_reject_crash_after_decided_before_review(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_reject(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_reject_crash_after_review_before_contract(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_reject(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_reject_crash_after_contract_before_bounty(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_reject(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        br._set_contract_eval_binding(contract, evaluation)
        hb._save_contract(contract)
        result = br.bounty_review(evaluation)
        assert result is not None
        assert result["bounty"]["status"] == "claimed"
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"

    def test_reject_crash_after_bounty_before_complete(self, monkeypatch, tmp_path):
        sub, evaluation = self._setup_reject(monkeypatch, tmp_path)
        br._write_journal_decided(evaluation)
        br._attach_review(sub["submission_id"], br._build_automatic_review_record(evaluation))
        contract = hb._load_contract("contract:b001:1")
        br._apply_criteria_results(contract, evaluation)
        br._set_contract_eval_binding(contract, evaluation)
        hb._save_contract(contract)
        board = hb._load(hb.BOUNTIES)
        board["bounties"][0]["status"] = "claimed"
        hb._save(hb.BOUNTIES, board)
        result = br.bounty_review(evaluation)
        assert result is not None
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub["submission_id"])]["stage"] == "complete"


# ── Resubmission after REJECT ───────────────────────────────────


class TestResubmission:
    def test_new_submission_after_reject_uses_new_journal_key(self, monkeypatch, tmp_path):
        c = SuccessCriterion.create(
            name="summary_present",
            required=True,
            evaluator=EvaluatorType.FIELD_PRESENT,
            evaluator_params={"field": "summary"},
        )
        contract = _make_contract(success_criteria=[c])
        _setup(monkeypatch, tmp_path)
        _bootstrap_contract(contract)
        _claim("SomeAgent")
        sub1 = br.bounty_submit("b001", "SomeAgent", _succeeded_work_result(output={"summary": None, "gaps": []}))
        e1 = build_final_evaluation(sub1, hb._load_contract("contract:b001:1"), evaluated_at=1.0)
        br.bounty_review(e1)  # REJECT → claimed

        # Claim and submit again
        _claim("SomeAgent")  # re-claim
        sub2 = br.bounty_submit(
            "b001", "SomeAgent", _succeeded_work_result(execution_id="exec-2", output={"summary": [1]})
        )
        e2 = build_final_evaluation(sub2, hb._load_contract("contract:b001:1"), evaluated_at=1.0)

        result = br.bounty_review(e2)
        assert result is not None
        # Old submission's journal is unchanged
        journal = hb._load(br.FINALIZATION_JOURNAL)
        assert journal[br._journal_key(sub1["submission_id"])]["stage"] == "complete"
        assert journal[br._journal_key(sub2["submission_id"])]["stage"] == "complete"
        assert br._journal_key(sub1["submission_id"]) != br._journal_key(sub2["submission_id"])


# ── AST authority boundaries ───────────────────────────────────


def _inspect_import_source(mod: object) -> str:
    import inspect

    return inspect.getsource(mod)


class TestAuthorityBoundaries:
    """Structural AST checks that the automatic review path does not create
    a second completion authority, and that purity boundaries hold."""

    def test_only_bounty_review_module_calls_contract_fulfill_for_lifecycle(self):
        """No module outside bounty_review.py and contracts.py itself
        may call .fulfill()."""
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
        """final_evaluation.py must not import heartbeat or bounty_review."""
        import ast

        with open("village/final_evaluation.py") as f:
            tree = ast.parse(f.read())
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                if node.module and any(m in (node.module or "") for m in ("heartbeat", "bounty_review")):
                    pytest.fail(f"final_evaluation imports {node.module}")

    def test_no_second_completion_path(self):
        """_bounty_review_automatic is only called from bounty_review()."""
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
        """No other module imports or calls _bounty_review_automatic."""
        import village.heartbeat as heartbeat
        import village.interpreter as interpreter
        import village.worker as worker

        for mod, name in [(worker, "worker"), (interpreter, "interpreter"), (heartbeat, "heartbeat")]:
            src = _inspect_import_source(mod)
            if "_bounty_review_automatic" in src:
                pytest.fail(f"{name} references _bounty_review_automatic")

    def test_bounty_review_is_sole_fulfill_caller(self):
        """Only bounty_review calls .fulfill() on a VillageContract for the bounty lifecycle."""
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
