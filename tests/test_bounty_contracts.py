"""
Tests for the bounty_claim()/bounty_complete() <-> village/contracts.py
wiring (docs/SPEC.md §C.3.1, docs/research/VILLAGE_CONTRACTS_01.md
follow-up slice). bounty_create() is deliberately NOT wired -- nothing
calls it in production, so it's out of scope here.

bounty_complete()'s behavior was narrowed by docs/research/
BOUNTY_REVIEW_GATE_01.md: it no longer moves a bounty from claimed to
done under any circumstance (see test_complete_no_longer_moves_claimed_
to_done below) -- that authority moved to village/bounty_review.py,
tested in tests/test_bounty_review.py.
"""

from __future__ import annotations

import village.heartbeat as hb
from village.contracts import ContractState, VillageContract


def _setup(monkeypatch, tmp_path):
    monkeypatch.setattr(hb, "BOUNTIES", tmp_path / "bounties.json")
    monkeypatch.setattr(hb, "CONTRACTS", tmp_path / "contracts.json")
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


def test_complete_no_longer_moves_claimed_to_done(monkeypatch, tmp_path, capsys):
    """Superseded by docs/research/BOUNTY_REVIEW_GATE_01.md: since the
    submit/review gate (village/bounty_review.py) exists, no normal
    path -- including this legacy function -- may move a bounty
    directly from claimed to done anymore. Real completion now goes
    through bounty_submit()+bounty_review(..., decision="accept"),
    tested in tests/test_bounty_review.py."""
    _setup(monkeypatch, tmp_path)
    hb.bounty_claim("b001", "SomeAgent")

    result = hb.bounty_complete("b001")

    assert result is None
    board = hb._load(hb.BOUNTIES)
    assert board["bounties"][0]["status"] == "claimed"  # unchanged
    contract = hb._load_contract("contract:b001:1")
    assert contract.state == ContractState.ACTIVE  # unchanged, not fulfilled
    captured = capsys.readouterr()
    assert "refused" in captured.out


def test_complete_on_bounty_claimed_before_this_wiring_existed_also_refused(monkeypatch, tmp_path):
    """Simulates a bounty claimed before any contract wiring existed:
    status is "claimed" but no contracts.json entry exists for it.
    Still refused (not a crash, not a completion) -- the legacy path is
    closed unconditionally now, independent of whether a contract
    exists."""
    _setup(monkeypatch, tmp_path)
    board = hb._load(hb.BOUNTIES)
    board["bounties"][0]["status"] = "claimed"
    board["bounties"][0]["claimed_by"] = "LegacyAgent"
    hb._save(hb.BOUNTIES, board)
    assert hb._load_contract("contract:b001:1") is None  # no contract exists

    result = hb.bounty_complete("b001")

    assert result is None
    board = hb._load(hb.BOUNTIES)
    assert board["bounties"][0]["status"] == "claimed"  # unchanged


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


# =============================================================================
# contract_terms ingress (bounty record optional field) -- follow-up slice
# to PR #11, docs/research/VILLAGE_CONTRACTS_01.md.
# =============================================================================


def _set_contract_terms(monkeypatch, tmp_path, terms: dict) -> None:
    board = hb._load(hb.BOUNTIES)
    board["bounties"][0]["contract_terms"] = terms
    hb._save(hb.BOUNTIES, board)


_VALID_TERMS = {
    "allowed_resources": ["Read", "Grep"],
    "budget": {"tokens": 20_000, "cost_usd": 0.5, "time_seconds": 3600, "cognitive_units": None},
    "deadline": "2026-07-25T12:00:00+00:00",
    "success_criteria": [
        {"name": "review_posted", "description": "A review comment was posted", "required": True, "weight": 1.0}
    ],
}


def test_legacy_bounty_without_contract_terms_is_unchanged(monkeypatch, tmp_path):
    """No contract_terms field at all -- byte-for-byte the PR #11
    behavior, not a new code path."""
    _setup(monkeypatch, tmp_path)
    assert "contract_terms" not in hb._load(hb.BOUNTIES)["bounties"][0]

    result = hb.bounty_claim("b001", "SomeAgent")

    assert result is not None
    contract = hb._load_contract("contract:b001:1")
    assert contract.allowed_resources == []
    assert contract.budget.tokens is None
    assert contract.deadline is None
    assert contract.success_criteria == []


def test_valid_contract_terms_reach_the_contract(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_contract_terms(monkeypatch, tmp_path, _VALID_TERMS)

    result = hb.bounty_claim("b001", "SomeAgent")

    assert result is not None
    assert result["status"] == "claimed"
    contract = hb._load_contract("contract:b001:1")
    assert contract.state == ContractState.ACTIVE
    assert contract.allowed_resources == ["Read", "Grep"]
    assert contract.budget.tokens == 20_000
    assert contract.budget.cost_usd == 0.5
    assert contract.deadline is not None
    assert len(contract.success_criteria) == 1
    assert contract.success_criteria[0].name == "review_posted"
    assert contract.success_criteria[0].required is True


def test_invalid_contract_terms_reject_claim_atomically(monkeypatch, tmp_path, capsys):
    """Negative budget -> Budget.__post_init__ raises ValueError. The
    claim must be rejected with the SAME return semantics as "bid not
    found" (None), leaving NO partial state behind: bounty stays "open",
    no contracts.json entry created."""
    _setup(monkeypatch, tmp_path)
    _set_contract_terms(monkeypatch, tmp_path, {"budget": {"tokens": -5}})

    result = hb.bounty_claim("b001", "SomeAgent")

    assert result is None
    captured = capsys.readouterr()
    assert "claim rejected" in captured.out
    assert "invalid contract_terms" in captured.out

    # No partial state: bounty untouched, no contract written.
    board = hb._load(hb.BOUNTIES)
    assert board["bounties"][0]["status"] == "open"
    assert board["bounties"][0]["claimed_by"] is None
    assert hb._load(hb.CONTRACTS) == {}


def test_invalid_deadline_string_rejects_claim_atomically(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_contract_terms(monkeypatch, tmp_path, {"deadline": "not-a-real-date"})

    result = hb.bounty_claim("b001", "SomeAgent")

    assert result is None
    board = hb._load(hb.BOUNTIES)
    assert board["bounties"][0]["status"] == "open"
    assert hb._load(hb.CONTRACTS) == {}


def test_invalid_success_criterion_weight_rejects_claim_atomically(monkeypatch, tmp_path):
    _setup(monkeypatch, tmp_path)
    _set_contract_terms(
        monkeypatch,
        tmp_path,
        {
            "success_criteria": [{"name": "bad", "weight": 5.0}],
        },
    )

    result = hb.bounty_claim("b001", "SomeAgent")

    assert result is None
    board = hb._load(hb.BOUNTIES)
    assert board["bounties"][0]["status"] == "open"
    assert hb._load(hb.CONTRACTS) == {}


def test_partial_contract_terms_subfields_are_all_optional(monkeypatch, tmp_path):
    """Only allowed_resources set -- budget/deadline/success_criteria
    missing entirely must not error, each falls back to its own default
    (unconstrained Budget, no deadline, no criteria)."""
    _setup(monkeypatch, tmp_path)
    _set_contract_terms(monkeypatch, tmp_path, {"allowed_resources": ["Read"]})

    result = hb.bounty_claim("b001", "SomeAgent")

    assert result is not None
    contract = hb._load_contract("contract:b001:1")
    assert contract.allowed_resources == ["Read"]
    assert contract.budget.tokens is None
    assert contract.deadline is None
    assert contract.success_criteria == []


# Success-criteria-vs-fulfillment behavior (unmet required criterion
# blocks fulfillment; a met one allows it) moved to the review gate --
# see tests/test_bounty_review.py::
# test_accept_with_unmet_required_criterion_is_refused and
# ::test_accept_with_met_required_criterion_fulfills_contract. Not
# re-tested here since bounty_complete() no longer reaches that logic
# at all (test_complete_no_longer_moves_claimed_to_done above covers
# its new, narrow behavior completely).


def test_contract_terms_json_roundtrip_on_the_bounty_record(monkeypatch, tmp_path):
    """The bounty record's contract_terms field itself, not just the
    resulting VillageContract, must survive a save/load cycle
    identically -- proves data/village/bounties.json isn't silently
    mutating/dropping the field anywhere in the claim path."""
    _setup(monkeypatch, tmp_path)
    _set_contract_terms(monkeypatch, tmp_path, _VALID_TERMS)

    before = hb._load(hb.BOUNTIES)["bounties"][0]["contract_terms"]
    hb.bounty_claim("b001", "SomeAgent")
    after = hb._load(hb.BOUNTIES)["bounties"][0]["contract_terms"]

    assert before == after == _VALID_TERMS
