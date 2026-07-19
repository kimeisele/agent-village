"""
Tests for the bounty_claim()/bounty_complete() <-> village/contracts.py
wiring (docs/SPEC.md §C.3.1, docs/research/VILLAGE_CONTRACTS_01.md
follow-up slice). bounty_create() is deliberately NOT wired -- nothing
calls it in production, so it's out of scope here.
"""

from __future__ import annotations

import village.heartbeat as hb
from village.contracts import ContractState, VillageContract


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "BOUNTIES", tmp_path / "bounties.json")
    monkeypatch.setattr(hb, "CONTRACTS", tmp_path / "contracts.json")
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


def test_claim_creates_and_activates_contract(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)

    result = hb.bounty_claim("b001", "SomeAgent")
    assert result is not None
    assert result["status"] == "claimed"

    contract = hb._load_contract("contract:b001:1")
    assert contract is not None
    assert contract.contract_id == "contract:b001:1"
    assert contract.title == "Review village/heartbeat.py"
    assert contract.description == "Read and review the heartbeat scanner."
    assert contract.state == ContractState.ACTIVE
    # No ingress path supplies budget/deadline data -- must stay unconstrained.
    assert contract.budget.tokens is None
    assert contract.deadline is None


def test_complete_fulfills_the_contract(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    hb.bounty_claim("b001", "SomeAgent")

    result = hb.bounty_complete("b001")
    assert result is not None
    assert result["status"] == "done"

    contract = hb._load_contract("contract:b001:1")
    assert contract.state == ContractState.FULFILLED


def test_complete_with_no_matching_contract_is_skipped_cleanly(monkeypatch, tmp_path, capsys):
    """Simulates a bounty claimed before this wiring existed: status is
    "claimed" but no contracts.json entry exists for it. Must not crash."""
    _setup(monkeypatch, tmp_path)
    board = hb._load(hb.BOUNTIES)
    board["bounties"][0]["status"] = "claimed"
    board["bounties"][0]["claimed_by"] = "LegacyAgent"
    hb._save(hb.BOUNTIES, board)
    assert hb._load_contract("contract:b001:1") is None  # no contract exists

    result = hb.bounty_complete("b001")

    assert result is not None
    assert result["status"] == "done"
    captured = capsys.readouterr()
    assert "no contract for b001" in captured.out


def test_claim_reuses_existing_contract_instead_of_recreating(monkeypatch, tmp_path):
    """If a contract already exists for this bid (e.g. re-running a
    retried claim path) and is already ACTIVE, bounty_claim() must not
    try to re-activate it (VillageContract.activate() raises on a
    non-DRAFTED contract)."""
    _setup(monkeypatch, tmp_path)
    pre_existing = VillageContract(contract_id="contract:b001:1", title="custom title")
    pre_existing.activate()
    hb._save_contract(pre_existing)

    result = hb.bounty_claim("b001", "SomeAgent")

    assert result is not None
    contract = hb._load_contract("contract:b001:1")
    assert contract.state == ContractState.ACTIVE
    assert contract.title == "custom title"  # not overwritten/recreated


def test_claim_on_nonexistent_bounty_never_touches_contracts(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    result = hb.bounty_claim("b999", "SomeAgent")
    assert result is None
    assert hb._load(hb.CONTRACTS) == {}


def test_complete_on_non_claimed_bounty_never_touches_contracts(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)  # b001 is still "open", not "claimed"
    result = hb.bounty_complete("b001")
    assert result is None
    assert hb._load(hb.CONTRACTS) == {}
