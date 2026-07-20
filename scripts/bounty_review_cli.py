"""Manual review entry point for submitted bounties.

This is the SOLE authorized production caller of ``bounty_review()``
per Issue #23. It validates that the specified submission belongs to the
specified bounty, then delegates to the existing review gate.

Usage:
    python scripts/bounty_review_cli.py <bounty_id> <submission_id> accept|reject <reviewer_actor_id>
"""

from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT))  # noqa: E402 — standalone script

import village.bounty_review as br  # noqa: E402
import village.heartbeat as hb  # noqa: E402


def main() -> int:
    if len(sys.argv) != 5:
        print(f"Usage: {sys.argv[0]} <bounty_id> <submission_id> accept|reject <reviewer_actor_id>")
        return 1

    bounty_id = sys.argv[1]
    submission_id = sys.argv[2]
    decision = sys.argv[3].lower()
    reviewer_actor_id = sys.argv[4]

    if decision not in ("accept", "reject"):
        print(f"Error: decision must be 'accept' or 'reject', got {decision!r}")
        return 1

    # Validate submission belongs to bounty
    submission = br._get_submission(submission_id)
    if submission is None:
        print(f"Error: submission {submission_id!r} not found")
        return 1
    if submission.get("bounty_id") != bounty_id:
        print(
            f"Error: submission {submission_id!r} belongs to bounty "
            f"{submission.get('bounty_id')!r}, not {bounty_id!r}"
        )
        return 1
    if submission.get("review") is not None:
        print(f"Error: submission {submission_id!r} has already been reviewed")
        return 1

    # Bounty must be in submitted state
    board = hb._load(hb.BOUNTIES)
    bounty = None
    for b in board.get("bounties", []):
        if isinstance(b, dict) and b.get("id") == bounty_id:
            bounty = b
            break
    if bounty is None:
        print(f"Error: bounty {bounty_id!r} not found")
        return 1
    if bounty.get("status") != "submitted":
        print(f"Error: bounty {bounty_id!r} is not in 'submitted' state (current: {bounty.get('status')!r})")
        return 1

    # Call the sole authorized completion boundary
    result = br.bounty_review(bounty_id, reviewer_actor_id, decision)
    if result is None:
        print("Error: bounty_review returned None — review could not be completed")
        return 1

    print(f"Review complete: bounty={bounty_id} submission={submission_id} decision={decision}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
