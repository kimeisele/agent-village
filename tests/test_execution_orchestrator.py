"""
Tests for village/execution_orchestrator.py -- the operator entry point
claimed bounty -> ACTIVE contract -> worker.run_work_order() ->
(SUCCEEDED only) bounty_submit() -> submitted. No real API calls -- a
FakeProvider test double, same pattern as tests/test_worker.py.
"""

from __future__ import annotations

import village.bounty_review as br
import village.execution_orchestrator as eo
import village.heartbeat as hb
from village.cognitive_provider import (
    CognitiveProvider,
    CognitiveResponse,
    ProviderHTTPError,
    ProviderUsage,
)
from village.contracts import ContractState
from village.interpreter import RESULT_BEGIN, RESULT_END
from village.work_result import WorkResultStatus


class FakeProvider(CognitiveProvider):
    name = "fake"

    def __init__(self, model="fake-model-01", script: list | None = None):
        self.model = model
        self._script = list(script or [])
        self.calls = 0

    def complete(self, prompt: str, *, max_tokens: int, timeout_seconds: float) -> CognitiveResponse:
        if self.calls >= len(self._script):
            raise AssertionError(f"FakeProvider script exhausted at call {self.calls + 1}")
        item = self._script[self.calls]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "BOUNTIES", tmp_path / "bounties.json")
    monkeypatch.setattr(hb, "CONTRACTS", tmp_path / "contracts.json")
    monkeypatch.setattr(br, "SUBMISSIONS", tmp_path / "bounty_submissions.json")
    hb._save(hb.BOUNTIES, {
        "bounties": [{
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
        }]
    })


def _claim(actor_id="SomeAgent"):
    return hb.bounty_claim("b001", actor_id)


def _usage(prompt_tokens=100, completion_tokens=50, cost_usd=0.001, duration=0.5) -> ProviderUsage:
    return ProviderUsage(
        prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens, cost_usd=cost_usd, duration_seconds=duration,
    )


VALID_MARKED = f'{RESULT_BEGIN}\n{{"gaps": [{{"description": "no tests for X", "file": "village/heartbeat.py", "line": 42}}]}}\n{RESULT_END}'


def _success_response() -> CognitiveResponse:
    return CognitiveResponse(
        visible_text=VALID_MARKED, reasoning_text=None, finish_reason="stop",
        usage=_usage(), provider="fake", model="fake-model-01",
    )


def _request(bounty_id="b001", actor_id="SomeAgent") -> eo.ExecutionRequest:
    return eo.ExecutionRequest(
        bounty_id=bounty_id, actor_id=actor_id,
        target_file="village/heartbeat.py", instruction="Find gaps.",
    )


# ── 1) claimed + ACTIVE contract + SUCCEEDED WorkResult -> submitted ──────


def test_successful_execution_moves_bounty_to_submitted(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    provider = FakeProvider(script=[_success_response()])

    outcome = eo.run_operator_execution(_request(), provider, "file content", execution_id="exec-1")

    assert outcome.accepted is True
    assert outcome.work_result.status == WorkResultStatus.SUCCEEDED
    assert outcome.submission is not None
    board = hb._load(hb.BOUNTIES)
    assert board["bounties"][0]["status"] == "submitted"


# ── 2) Contract stays ACTIVE ────────────────────────────────────────────


def test_contract_stays_active_after_successful_submission(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    provider = FakeProvider(script=[_success_response()])

    eo.run_operator_execution(_request(), provider, "file content")

    contract = hb._load_contract("contract:b001:1")
    assert contract.state == ContractState.ACTIVE  # not fulfilled -- only bounty_review() may do that


# ── 3) INVALID_OUTPUT -> claimed, no submission ────────────────────────


def test_invalid_output_leaves_bounty_claimed_no_submission(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    bad = CognitiveResponse(
        visible_text="never valid, no markers, no json", reasoning_text=None, finish_reason="stop",
        usage=_usage(), provider="fake", model="fake-model-01",
    )
    provider = FakeProvider(script=[bad, bad, bad, bad])  # exhausts MAX_LLM_CALLS_PER_EXECUTION

    outcome = eo.run_operator_execution(_request(), provider, "file content")

    assert outcome.accepted is False
    assert outcome.work_result.status == WorkResultStatus.INVALID_OUTPUT
    assert outcome.submission is None
    board = hb._load(hb.BOUNTIES)
    assert board["bounties"][0]["status"] == "claimed"  # unchanged
    assert hb._load(br.SUBMISSIONS) == {}


# ── 4) PROVIDER_ERROR -> claimed, no submission ─────────────────────────


def test_provider_error_leaves_bounty_claimed_no_submission(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    provider = FakeProvider(script=[ProviderHTTPError(500, "server error")])

    outcome = eo.run_operator_execution(_request(), provider, "file content")

    assert outcome.accepted is False
    assert outcome.work_result.status == WorkResultStatus.PROVIDER_ERROR
    assert outcome.submission is None
    assert hb._load(hb.BOUNTIES)["bounties"][0]["status"] == "claimed"


# ── 5) wrong actor rejected ─────────────────────────────────────────────


def test_wrong_actor_is_rejected_without_calling_the_worker(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("RealClaimant")
    provider = FakeProvider(script=[])  # must never be called

    outcome = eo.run_operator_execution(_request(actor_id="Imposter"), provider, "file content")

    assert outcome.accepted is False
    assert "actor_id" in outcome.reason
    assert provider.calls == 0
    assert hb._load(hb.BOUNTIES)["bounties"][0]["status"] == "claimed"


# ── 6) missing contract rejected ────────────────────────────────────────


def test_missing_contract_is_rejected_without_calling_the_worker(monkeypatch, tmp_path):
    """Simulates a bounty claimed via a path that predates contract
    wiring -- no contracts.json entry exists."""
    _setup(monkeypatch, tmp_path)
    board = hb._load(hb.BOUNTIES)
    board["bounties"][0]["status"] = "claimed"
    board["bounties"][0]["claimed_by"] = "SomeAgent"
    hb._save(hb.BOUNTIES, board)
    assert hb._load_contract("contract:b001:1") is None
    provider = FakeProvider(script=[])

    outcome = eo.run_operator_execution(_request(), provider, "file content")

    assert outcome.accepted is False
    assert "contract" in outcome.reason
    assert provider.calls == 0


# ── 7) wrong contract state rejected ────────────────────────────────────


def test_non_active_contract_state_is_rejected(monkeypatch, tmp_path):
    """A contract that somehow reached a terminal state (e.g. a previous
    execution already fulfilled/violated it) must reject a new
    execution attempt rather than run against stale/closed governance."""
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    contract = hb._load_contract("contract:b001:1")
    contract.terminate("simulated prior closure")
    hb._save_contract(contract)
    provider = FakeProvider(script=[])

    outcome = eo.run_operator_execution(_request(), provider, "file content")

    assert outcome.accepted is False
    assert "ACTIVE" in outcome.reason
    assert provider.calls == 0


def test_bounty_not_claimed_is_rejected(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)  # still "open"
    provider = FakeProvider(script=[])

    outcome = eo.run_operator_execution(_request(), provider, "file content")

    assert outcome.accepted is False
    assert provider.calls == 0


# ── 8) submit failure is propagated honestly ────────────────────────────


def test_submit_failure_is_reported_honestly_not_as_success(monkeypatch, tmp_path):
    """Simulates bounty_submit() refusing the result (e.g. the bounty
    state changed between the worker run and the submit call -- here
    forced by flipping status away from "claimed" mid-flight via a
    monkeypatched bounty_submit)."""
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    provider = FakeProvider(script=[_success_response()])
    monkeypatch.setattr(eo, "bounty_submit", lambda *a, **k: None)

    outcome = eo.run_operator_execution(_request(), provider, "file content")

    assert outcome.accepted is False
    assert outcome.work_result.status == WorkResultStatus.SUCCEEDED  # the worker DID succeed
    assert outcome.submission is None  # but submission was refused
    assert "bounty_submit" in outcome.reason


# ── 9) worker cannot import the orchestrator ────────────────────────────
# (Covered structurally in tests/test_worker_no_write_authority.py --
# not duplicated here.)


# ── 10) orchestrator contains no review/fulfill/complete call ──────────
# (Covered structurally in tests/test_worker_no_write_authority.py --
# not duplicated here.)


# ── 11) no real API call anywhere in this file ──────────────────────────
# (Structural: every test above uses FakeProvider, never
# village.deepseek_provider.DeepSeekProvider.)


# ── 12) evidence/secret invariants preserved through the full path ─────


def test_evidence_secret_redaction_still_applies_through_the_full_path(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    response = CognitiveResponse(
        visible_text=VALID_MARKED, reasoning_text=None, finish_reason="stop",
        usage=_usage(), provider="fake", model="fake-model-01",
    )
    provider = FakeProvider(script=[response])

    outcome = eo.run_operator_execution(_request(), provider, "file content")

    # Evidence never contains a raw provider payload or secret-shaped key.
    assert "raw" not in outcome.submission["evidence"]
    assert outcome.submission["evidence"] == {
        "phase_log": outcome.submission["evidence"]["phase_log"],
        "target_file": "village/heartbeat.py",
        "instruction": "Find gaps.",
    }


# ── Additional: real usage accounting reaches the persisted contract ────


def test_real_usage_is_persisted_to_the_contract_even_on_failure(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    bad = CognitiveResponse(
        visible_text="", reasoning_text=None, finish_reason="length",
        usage=_usage(prompt_tokens=500, completion_tokens=500), provider="fake", model="fake-model-01",
    )
    provider = FakeProvider(script=[bad, bad, bad, bad])

    eo.run_operator_execution(_request(), provider, "file content")

    contract = hb._load_contract("contract:b001:1")
    assert contract.budget.used_tokens > 0  # real spend recorded despite the failure


def test_no_automatic_retry_of_a_failed_mission(monkeypatch, tmp_path):
    """run_operator_execution() is called once, does exactly one worker
    execution, and returns -- it never loops or retries the whole
    mission on its own."""
    _setup(monkeypatch, tmp_path)
    _claim("SomeAgent")
    bad = CognitiveResponse(
        visible_text="not usable", reasoning_text=None, finish_reason="stop",
        usage=_usage(), provider="fake", model="fake-model-01",
    )
    provider = FakeProvider(script=[bad, bad, bad, bad])  # worker's own internal cap, not the orchestrator's

    eo.run_operator_execution(_request(), provider, "file content")

    assert provider.calls == 4  # exactly the worker's own MAX_LLM_CALLS_PER_EXECUTION, no orchestrator-level retry on top
