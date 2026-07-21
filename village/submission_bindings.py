"""Pure, non-mutating submission binding validation.

Side-effect-free: no file I/O, no network I/O, no state mutation.
Never imports heartbeat, GitHub, review-authority, or terminal-mutation modules.
"""

from __future__ import annotations

import hashlib
from typing import Any

from village.contracts import (
    VillageContract,
    canonical_json_dumps,
    compute_review_policy_hash,
)


def validate_submission_bindings(submission: dict[str, Any], contract: VillageContract) -> list[str]:
    """Pure, non-mutating validation of submission review bindings.

    Returns a list of reason codes. Empty list = valid. Never raises.
    Never performs review or mutation.
    """
    reasons: list[str] = []
    required_str = [
        "submission_id",
        "bounty_id",
        "contract_id",
        "contract_version",
        "work_result_id",
        "execution_id",
        "output_canonical_hash",
        "review_policy_hash",
    ]
    for f in required_str:
        val = submission.get(f)
        if not isinstance(val, str) or not val:
            reasons.append(f"missing_or_invalid:{f}")
            return reasons
    cids = submission.get("criterion_ids")
    chashes = submission.get("criterion_definition_hashes")
    if not isinstance(cids, list) or not isinstance(chashes, list):
        reasons.append("missing_or_invalid:criterion_ids_or_hashes")
        return reasons
    if len(cids) != len(chashes):
        reasons.append("criterion_id_hash_count_mismatch")
        return reasons
    for i, cid in enumerate(cids):
        if not isinstance(cid, str) or not cid:
            reasons.append(f"invalid_criterion_id:{i}")
    for i, ch in enumerate(chashes):
        if not isinstance(ch, str) or len(ch) != 64 or not all(c in "0123456789abcdef" for c in ch):
            reasons.append(f"invalid_criterion_definition_hash:{i}")
    for c in contract.success_criteria:
        if not c.criterion_id or not c.criterion_definition_hash:
            reasons.append("legacy_unbound_criterion")
    if submission["contract_id"] != contract.contract_id:
        reasons.append("contract_id_mismatch")
    if submission["contract_version"] != contract.version:
        reasons.append("contract_version_mismatch")
    expected_cids = [c.criterion_id for c in contract.success_criteria]
    if cids != expected_cids:
        reasons.append("criterion_ids_mismatch")
    expected_hashes = [c.criterion_definition_hash for c in contract.success_criteria]
    if chashes != expected_hashes:
        reasons.append("criterion_definition_hashes_mismatch")
    stored_output = submission.get("output")
    if isinstance(stored_output, dict):
        try:
            computed = hashlib.sha256(canonical_json_dumps(stored_output).encode()).hexdigest()
            if computed != submission.get("output_canonical_hash"):
                reasons.append("output_hash_mismatch")
        except (ValueError, TypeError):
            reasons.append("output_not_canonical")
    else:
        reasons.append("output_hash_missing_or_invalid")
    try:
        expected_policy = compute_review_policy_hash(contract)
        if submission.get("review_policy_hash") != expected_policy:
            reasons.append("review_policy_hash_mismatch")
    except (ValueError, TypeError):
        reasons.append("policy_not_canonical")
    return reasons
