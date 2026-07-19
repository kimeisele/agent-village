"""
Tests for the Repository Fortress 01 operator-proof-budget fix:
scripts/operator_execute.py's auto-created disposable proof bounty must
get an explicit, conservative contract (tokens/cost_usd/time_seconds),
reusing the existing contract_terms/Budget structure -- not a parallel
one. Before this fix, the auto-created proof contract had every Budget
field `null` (see docs/research/OPERATOR_EXECUTION_01.md's live-proof
section, run 29696150575: `"tokens": null, "cost_usd": null,
"time_seconds": null`).

No real API calls anywhere -- village.execution_orchestrator's
FakeProvider pattern (tests/test_execution_orchestrator.py), same as
the rest of this file's siblings.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import village.bounty_review as br
import village.execution_orchestrator as eo
import village.heartbeat as hb
from village.cognitive_provider import CognitiveProvider, CognitiveResponse, ProviderUsage
from village.contracts import Budget
from village.work_result import WorkResultStatus

SCRIPT_PATH = Path(__file__).resolve().parent.parent / "scripts" / "operator_execute.py"


def _load_operator_execute_module():
    spec = importlib.util.spec_from_file_location("operator_execute", SCRIPT_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules.setdefault("operator_execute", module)
    spec.loader.exec_module(module)
    return module


oe = _load_operator_execute_module()


class FakeProvider(CognitiveProvider):
    name = "fake"

    def __init__(self, script: list):
        self.model = "fake-model-01"
        self._script = list(script)
        self.calls = 0

    def complete(self, prompt: str, *, max_tokens: int, timeout_seconds: float) -> CognitiveResponse:
        item = self._script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "BOUNTIES", tmp_path / "bounties.json")
    monkeypatch.setattr(hb, "CONTRACTS", tmp_path / "contracts.json")
    monkeypatch.setattr(br, "SUBMISSIONS", tmp_path / "bounty_submissions.json")
    hb._save(hb.BOUNTIES, {"bounties": []})


def _usage(prompt_tokens, completion_tokens, cost_usd, duration) -> ProviderUsage:
    return ProviderUsage(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens, cost_usd=cost_usd, duration_seconds=duration,
    )


# 1) the constant itself carries all three limits, matching Kim's spec
def test_proof_contract_terms_has_all_three_limits():
    budget = oe.PROOF_CONTRACT_TERMS["budget"]
    assert budget["tokens"] == 40_000
    assert budget["cost_usd"] == 0.05
    assert budget["time_seconds"] == 180


# 2) bounty_create() accepts and stores contract_terms verbatim
def test_bounty_create_stores_contract_terms(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    created = hb.bounty_create("t", "d", contract_terms=oe.PROOF_CONTRACT_TERMS)
    assert created["contract_terms"] == oe.PROOF_CONTRACT_TERMS
    board = hb._load(hb.BOUNTIES)
    assert board["bounties"][0]["contract_terms"] == oe.PROOF_CONTRACT_TERMS


# 3) claiming the auto-created proof bounty (production path, same as
#    scripts/operator_execute.py::main()) produces a contract whose
#    Budget has all three limits set -- not null, as it was before.
def test_claimed_proof_bounty_contract_has_all_three_limits(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    created = hb.bounty_create(
        title="[OPERATOR PROOF -- safe to ignore] test",
        description="proof",
        contract_terms=oe.PROOF_CONTRACT_TERMS,
    )
    claimed = hb.bounty_claim(created["id"], "operator-proof-01")
    assert claimed is not None

    contract = hb._load_contract(hb._contract_id_for(created["id"]))
    assert contract.budget.tokens == 40_000
    assert contract.budget.cost_usd == 0.05
    assert contract.budget.time_seconds == 180


# 4) run_operator_execution() -- the exact function scripts/
#    operator_execute.py::main() calls -- uses this contract, and real
#    usage is booked against it (well inside budget here).
def test_operator_execution_books_usage_against_the_proof_contract(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    created = hb.bounty_create(
        title="[OPERATOR PROOF -- safe to ignore] test",
        description="proof",
        contract_terms=oe.PROOF_CONTRACT_TERMS,
    )
    hb.bounty_claim(created["id"], "operator-proof-01")
    from village.interpreter import RESULT_BEGIN, RESULT_END
    marked = f'{RESULT_BEGIN}\n{{"gaps": []}}\n{RESULT_END}'
    response = CognitiveResponse(
        visible_text=marked, reasoning_text=None, finish_reason="stop",
        usage=_usage(100, 50, 0.001, 0.5), provider="fake", model="fake-model-01",
    )
    provider = FakeProvider(script=[response])
    request = eo.ExecutionRequest(
        bounty_id=created["id"], actor_id="operator-proof-01",
        target_file="village/heartbeat.py", instruction="Find gaps.",
    )

    outcome = eo.run_operator_execution(request, provider, "file content")

    assert outcome.accepted is True
    contract = hb._load_contract(hb._contract_id_for(created["id"]))
    assert contract.budget.used_tokens == 150
    assert contract.budget.used_cost_usd == 0.001
    # limits are still the conservative ones, untouched by real usage
    assert contract.budget.tokens == 40_000


# 5) overrun of the proof budget is controlled by the existing worker/
#    contract logic (BUDGET_EXCEEDED), not a new mechanism -- usage far
#    beyond the 40,000-token limit on this exact contract.
def test_exceeding_the_proof_budget_is_controlled_by_existing_logic(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    created = hb.bounty_create(
        title="[OPERATOR PROOF -- safe to ignore] test",
        description="proof",
        contract_terms=oe.PROOF_CONTRACT_TERMS,
    )
    hb.bounty_claim(created["id"], "operator-proof-01")
    huge = CognitiveResponse(
        visible_text="not usable, no markers", reasoning_text=None, finish_reason="stop",
        usage=_usage(30_000, 20_000, 0.001, 0.5),  # 50,000 tokens > 40,000 limit
        provider="fake", model="fake-model-01",
    )
    provider = FakeProvider(script=[huge])
    request = eo.ExecutionRequest(
        bounty_id=created["id"], actor_id="operator-proof-01",
        target_file="village/heartbeat.py", instruction="Find gaps.",
    )

    outcome = eo.run_operator_execution(request, provider, "file content")

    assert outcome.accepted is False
    assert outcome.work_result.status == WorkResultStatus.BUDGET_EXCEEDED
    board = hb._load(hb.BOUNTIES)
    assert board["bounties"][0]["status"] == "claimed"  # never submitted
