"""
Driver for docs/research/INTERNAL_WORKER_PROOF_01.md / .github/workflows/
worker-proof-01.yml. NOT imported by village/*.py, NOT part of the
heartbeat production path.

Runs exactly one bounded worker execution (village/worker.py) against a
fixed local file, using the real DeepSeek provider, and writes a
non-secret evidence JSON file. Never commits, pushes, or writes into
data/village/ -- the Contract used here is constructed in-memory only,
not loaded from or saved to data/village/contracts.json, so this proof
cannot mutate any real bounty/contract state even if run repeatedly.

Exit code is non-zero on any WorkResult status other than SUCCEEDED, so
the calling workflow step fails visibly rather than reporting green on a
failed/skipped run.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from village.contracts import Budget, VillageContract
from village.deepseek_provider import DEEPSEEK_API_KEY_VAR, DeepSeekProvider
from village.worker import WorkOrder, run_work_order


def main() -> int:
    target_file = os.environ.get("WORKER_PROOF_TARGET_FILE", "village/heartbeat.py")
    model = os.environ.get("DEEPSEEK_MODEL", "deepseek-v4-flash")
    evidence_path = Path(os.environ.get("WORKER_PROOF_EVIDENCE_PATH", "worker_proof_evidence.json"))

    if not os.environ.get(DEEPSEEK_API_KEY_VAR):
        print(f"::error::{DEEPSEEK_API_KEY_VAR} not set -- clean skip, not a fake success.")
        evidence_path.write_text(
            json.dumps(
                {
                    "status": "SKIPPED_NO_SECRET",
                    "reason": f"{DEEPSEEK_API_KEY_VAR} not set",
                },
                indent=2,
            )
        )
        return 1

    target_path = Path(target_file)
    if not target_path.is_file():
        print(f"::error::target file not found: {target_file}")
        return 1
    file_content = target_path.read_text(errors="replace")

    # In-memory only -- never loaded from or saved to
    # data/village/contracts.json. This proof cannot mutate any real
    # bounty/contract state.
    contract = VillageContract(
        contract_id="contract:worker-proof-01:1",
        title="Worker Proof 1 — structural gap analysis",
        description=f"Analyze {target_file} for a short, structured list of gaps.",
        # v2 (docs/research/AGENT_LOOP_WORKER_02.md): up to
        # MAX_LLM_CALLS_PER_EXECUTION (4) real calls can happen in one
        # execution now (generate + up to 2 repairs + one interpretation
        # call), each up to DEFAULT_TIMEOUT_SECONDS (30s) -- budget sized
        # with real headroom for that, not for a single call.
        budget=Budget(tokens=40_000, cost_usd=0.05, time_seconds=180),
    )
    contract.activate()

    order = WorkOrder(
        contract_id=contract.contract_id,
        target_file=target_file,
        instruction=(
            f"Analyze {target_file}. List up to 5 concrete gaps (missing "
            "error handling, missing tests, unclear invariants, etc.), "
            "each with a short description and a file reference."
        ),
    )

    provider = DeepSeekProvider(model=model)
    result = run_work_order(contract, order, file_content, provider, execution_id="worker-proof-01")

    evidence = {
        "work_order": {
            "contract_id": order.contract_id,
            "target_file": order.target_file,
            "instruction": order.instruction,
        },
        "contract_id": contract.contract_id,
        "execution_id": result.execution_id,
        "provider": result.provider,
        "model": result.model,
        "status": result.status.value,
        "budget_decision": {
            "budget_limits": contract.budget.to_dict(),
            "exceeded_dimensions": contract.check_budget(),
        },
        "usage": result.usage,
        "work_result": result.to_dict(),
        "error": result.error,
    }
    evidence_path.write_text(json.dumps(evidence, indent=2, sort_keys=True))

    print(f"status={result.status.value} usage={result.usage} error={result.error}")
    return 0 if result.status.value == "succeeded" else 1


if __name__ == "__main__":
    raise SystemExit(main())
