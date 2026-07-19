"""
Driver for docs/research/OPERATOR_EXECUTION_01.md / .github/workflows/
operator-execute-01.yml. NOT imported by village/*.py, NOT part of the
heartbeat production path -- the actual operator entry point used for
this slice's live proof (not a unit test calling the function directly).

Exercises the real claimed-bounty -> real ACTIVE-contract ->
village.execution_orchestrator.run_operator_execution() ->
(SUCCEEDED only) village.bounty_review.bounty_submit() path against
real data/village/*.json files in the runner's own checkout.

Safe by construction, not just by convention: the calling workflow sets
`permissions: contents: read` (no write), so nothing this script does to
its local checkout of data/village/*.json can ever be committed or
pushed back to the repository -- the same guarantee
.github/workflows/worker-proof-01.yml already relies on. Unlike that
script's in-memory-only Contract, this one deliberately DOES exercise
real file I/O (that's the point of this slice), confined entirely to the
ephemeral runner.

If OPERATOR_BOUNTY_ID is not set, creates and claims a clearly-labeled,
dedicated proof bounty first (title/description say so explicitly) --
per Kim's instruction to use either an isolated proof data path or an
explicitly-created proof bounty; this script chooses the latter so the
same real bounty_submit()/bounty_review.py code path that would run
against production data is actually exercised, not a parallel one.

Exit code is non-zero unless the execution was accepted (worker
SUCCEEDED AND bounty_submit() accepted it), so the calling workflow step
fails visibly rather than reporting green on a failed/rejected run.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import village.bounty_review as br
import village.heartbeat as hb
from village.deepseek_provider import DEEPSEEK_API_KEY_VAR, DeepSeekProvider
from village.execution_orchestrator import ExecutionRequest, run_operator_execution


def _snapshot(bounty_id: str, contract_id: str) -> dict:
    board = hb._load(hb.BOUNTIES)
    bounty = next((b for b in board.get("bounties", []) if b["id"] == bounty_id), None)
    contract = hb._load_contract(contract_id)
    return {
        "bounty": bounty,
        "contract": contract.to_dict() if contract else None,
    }


def main() -> int:
    target_file = os.environ.get("OPERATOR_TARGET_FILE", "village/heartbeat.py")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    actor_id = os.environ.get("OPERATOR_ACTOR_ID", "operator-proof-01")
    bounty_id = os.environ.get("OPERATOR_BOUNTY_ID", "").strip() or None
    evidence_path = Path(os.environ.get("OPERATOR_EVIDENCE_PATH", "operator_execute_evidence.json"))

    if not os.environ.get(DEEPSEEK_API_KEY_VAR):
        print(f"::error::{DEEPSEEK_API_KEY_VAR} not set -- clean skip, not a fake success.")
        evidence_path.write_text(json.dumps({
            "status": "SKIPPED_NO_SECRET",
            "reason": f"{DEEPSEEK_API_KEY_VAR} not set",
        }, indent=2))
        return 1

    target_path = Path(target_file)
    if not target_path.is_file():
        print(f"::error::target file not found: {target_file}")
        return 1
    file_content = target_path.read_text(errors="replace")

    if bounty_id is None:
        # Explicitly-created, clearly-labeled proof bounty -- never
        # touches any pre-existing real bounty. Confined to the runner's
        # ephemeral checkout (permissions: contents: read means this can
        # never be committed/pushed back).
        created = hb.bounty_create(
            title="[OPERATOR PROOF -- safe to ignore] Structural gap analysis of a target file",
            description=(
                "Dedicated, disposable proof bounty created by "
                "scripts/operator_execute.py for docs/research/"
                "OPERATOR_EXECUTION_01.md. Not real work, not for "
                "production use. Confined to the ephemeral CI runner --"
                "this workflow has no write permission to the repo."
            ),
        )
        bounty_id = created["id"]
        claimed = hb.bounty_claim(bounty_id, actor_id)
        if claimed is None:
            print(f"::error::failed to claim freshly-created proof bounty {bounty_id}")
            return 1
        print(f"Created and claimed dedicated proof bounty: {bounty_id}")
    else:
        board = hb._load(hb.BOUNTIES)
        bounty = next((b for b in board.get("bounties", []) if b["id"] == bounty_id), None)
        if bounty is None:
            print(f"::error::bounty {bounty_id} not found")
            return 1
        if bounty.get("status") != "claimed" or bounty.get("claimed_by") != actor_id:
            print(f"::error::bounty {bounty_id} is not claimed by {actor_id!r} (status={bounty.get('status')!r}, claimed_by={bounty.get('claimed_by')!r})")
            return 1

    contract_id = hb._contract_id_for(bounty_id)
    before = _snapshot(bounty_id, contract_id)

    request = ExecutionRequest(
        bounty_id=bounty_id,
        actor_id=actor_id,
        target_file=target_file,
        instruction=(
            f"Analyze {target_file}. List up to 5 concrete gaps (missing "
            "error handling, missing tests, unclear invariants, etc.), "
            "each with a short description and a file reference."
        ),
    )
    provider = DeepSeekProvider(model=model)
    outcome = run_operator_execution(request, provider, file_content, execution_id="operator-execute-01")

    after = _snapshot(bounty_id, contract_id)

    evidence = {
        "bounty_id": bounty_id,
        "actor_id": actor_id,
        "accepted": outcome.accepted,
        "reason": outcome.reason,
        "before": before,
        "after": after,
        "work_result": outcome.work_result.to_dict() if outcome.work_result else None,
        "submission": outcome.submission,
    }
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True))

    usage = outcome.work_result.usage if outcome.work_result else {}
    print(f"accepted={outcome.accepted} reason={outcome.reason} usage={usage}")
    return 0 if outcome.accepted else 1


if __name__ == "__main__":
    raise SystemExit(main())
