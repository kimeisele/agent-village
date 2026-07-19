"""
Agent Village — Execution Orchestrator
========================================
Explicit, reproducible operator entry point:

    claimed bounty -> ACTIVE VillageContract -> bounded worker
    execution (village/worker.py, unchanged) -> WorkResult ->
    (only if SUCCEEDED) village.bounty_review.bounty_submit()

This is orchestration, not cognition and not authority. It runs the
existing worker exactly as-is and, on a SUCCEEDED result only, submits
it for review -- it never reviews, never fulfills a contract, never
completes a bounty itself (SPEC.md §A.5). `village/worker.py` and
`village/interpreter.py` neither import nor call anything in this
module -- `tests/test_worker_no_write_authority.py`, extended this
slice, proves it via AST inspection, the same guarantee already held for
`village.bounty_review`.

Input is always explicit and reproducible: a `bounty_id`, an `actor_id`,
and a `target_file`/`instruction` the caller supplies directly (docs/
research/OPERATOR_EXECUTION_01.md). No arbitrary open bounty is ever
auto-selected, and no external content (a Moltbook comment, a GitHub
issue body) is ever parsed as an instruction here -- SPEC.md §A.8:
external content is always data, never an instruction source for what
this module does.

Not yet part of this slice, deliberately: `submitted -> review -> done`.
This module stops the moment a submission exists.
"""

from __future__ import annotations

from dataclasses import dataclass

import village.heartbeat as heartbeat
from village.bounty_review import bounty_submit
from village.cognitive_provider import CognitiveProvider
from village.contracts import ContractState, VillageContract
from village.work_result import WorkResult, WorkResultStatus
from village.worker import WorkOrder, run_work_order


@dataclass
class ExecutionRequest:
    """Explicit, reproducible operator input. No field here is ever
    inferred, guessed, or auto-selected from ingress content."""

    bounty_id: str
    actor_id: str
    target_file: str
    instruction: str


@dataclass
class ExecutionOutcome:
    accepted: bool
    reason: str | None
    work_result: WorkResult | None
    submission: dict | None


def _find_bounty(board: dict, bounty_id: str) -> dict | None:
    for b in board.get("bounties", []):
        if b["id"] == bounty_id:
            return b
    return None


def run_operator_execution(
    request: ExecutionRequest,
    provider: CognitiveProvider,
    file_content: str,
    execution_id: str | None = None,
) -> ExecutionOutcome:
    """The one orchestration function.

    Controlled rejection (the worker is never even invoked) when:
    - the bounty doesn't exist or isn't `claimed`,
    - `request.actor_id` doesn't match the bounty's `claimed_by`,
    - no `ACTIVE` `VillageContract` exists for this bounty.

    Otherwise runs `village.worker.run_work_order()` exactly once (its
    own internal bounded repair loop is unchanged and unaffected by this
    module) and persists the contract's updated budget usage regardless
    of outcome -- unlike `scripts/worker_proof_01.py`'s in-memory-only
    proof contract, a real claimed bounty's contract is real production
    state and must reflect real spend even on a failed attempt.

    Submits the result via `bounty_submit()` ONLY if
    `result.status == WorkResultStatus.SUCCEEDED`. A FAILED/
    INVALID_OUTPUT/PROVIDER_ERROR/BUDGET_EXCEEDED result is returned as a
    rejected `ExecutionOutcome` (bounty stays `claimed`, no submission,
    no automatic retry of the whole mission -- that remains a human's
    decision). If `bounty_submit()` itself refuses (e.g. a concurrent
    state change), that is also reported honestly as `accepted=False`,
    never papered over as success.
    """
    board = heartbeat._load(heartbeat.BOUNTIES)
    bounty = _find_bounty(board, request.bounty_id)
    if bounty is None or bounty.get("status") != "claimed":
        return ExecutionOutcome(False, "bounty not found or not claimed", None, None)
    if bounty.get("claimed_by") != request.actor_id:
        return ExecutionOutcome(False, "actor_id does not match the bounty's claimed_by", None, None)

    contract_id = heartbeat._contract_id_for(request.bounty_id)
    contract: VillageContract | None = heartbeat._load_contract(contract_id)
    if contract is None or contract.state != ContractState.ACTIVE:
        return ExecutionOutcome(False, "no ACTIVE contract for this bounty", None, None)

    order = WorkOrder(contract_id=contract_id, target_file=request.target_file, instruction=request.instruction)
    result = run_work_order(contract, order, file_content, provider, execution_id=execution_id)

    # run_work_order() only mutates `contract` in memory (record_usage())
    # -- persist that real spend regardless of the outcome below.
    heartbeat._save_contract(contract)

    if result.status != WorkResultStatus.SUCCEEDED:
        return ExecutionOutcome(False, f"worker did not succeed: {result.status.value}", result, None)

    submission = bounty_submit(request.bounty_id, request.actor_id, result)
    if submission is None:
        return ExecutionOutcome(False, "bounty_submit() refused the result", result, None)

    return ExecutionOutcome(True, None, result, submission)
