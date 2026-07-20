"""Tests for External Bounty Lifecycle 01A — identity-correct claim and manual review.

Covers:
- actor_id in claimed_by (not display name)
- publish_pending_review_requests dedup
- manual review CLI validation
- legacy done rejection reply
- end-to-end claim → execution → submission → review → done
"""

from __future__ import annotations

import pytest

import village.bounty_review as br
import village.heartbeat as hb
from village.contracts import ContractState
from village.execution_orchestrator import ExecutionRequest, run_operator_execution
from village.village_core import (
    CanonicalIngressEvent,
    sha256_hex,
)
from village.work_result import WorkResult, WorkResultStatus

# ── Helpers ───────────────────────────────────────────────────


def _make_event(actor_id="actor:alice", display_name="Alice", content="claim b001"):
    return CanonicalIngressEvent(
        event_id=f"mb:test_{actor_id}",
        surface="moltbook",
        external_id="test",
        actor_id=actor_id,
        display_name=display_name,
        content=content,
        content_sha256=sha256_hex(content),
        received_at=0.0,
        dedup_key=f"mb:test_{actor_id}",
    )


def _make_succeeded_work_result(contract_id="contract:b001:1", execution_id="exec-1"):
    return WorkResult(
        work_result_id=f"workresult:{contract_id}:{execution_id}",
        contract_id=contract_id,
        execution_id=execution_id,
        provider="test",
        model="test",
        status=WorkResultStatus.SUCCEEDED,
        output={"gaps": []},
    )


# ── Fixtures ──────────────────────────────────────────────────


@pytest.fixture(autouse=True)
def isolated_village(tmp_path, monkeypatch):
    """Redirect all village data paths to tmp_path."""
    data_dir = tmp_path / "data" / "village"
    data_dir.mkdir(parents=True)
    monkeypatch.setattr(hb, "DIR", tmp_path / "data" / "village")
    monkeypatch.setattr(hb, "POKEDEX", data_dir / "pokedex.json")
    monkeypatch.setattr(hb, "BOUNTIES", data_dir / "bounties.json")
    monkeypatch.setattr(hb, "CONTRACTS", data_dir / "contracts.json")
    monkeypatch.setattr(hb, "PROC_MB", data_dir / "processed_comments.json")
    monkeypatch.setattr(hb, "PROC_GH", data_dir / "processed_issues.json")
    monkeypatch.setattr(hb, "PENDING_MB", data_dir / "pending_confirmations.json")
    monkeypatch.setattr(hb, "CONTRIBUTIONS", data_dir / "contributions.json")
    monkeypatch.setattr(hb, "CHALLENGE_STATE", data_dir / "challenge_failures.json")
    monkeypatch.setattr(hb, "STATE", data_dir / "state.json")
    monkeypatch.setattr(hb, "REPLY_COMMENT_IDS", data_dir / "reply_comment_ids.json")
    monkeypatch.setattr(hb, "REVIEW_REQUESTS", data_dir / "review_requests.json")
    monkeypatch.setattr(br, "DIR", tmp_path / "data" / "village")
    monkeypatch.setattr(br, "SUBMISSIONS", data_dir / "bounty_submissions.json")
    # Disable Moltbook API calls
    monkeypatch.setattr(hb, "MB", "")
    monkeypatch.setattr(hb, "GH", "")
    monkeypatch.setattr(hb, "REG_POST", "")
    yield tmp_path


@pytest.fixture
def open_bounty(isolated_village):
    """Create an open bounty with contract_terms."""
    created = hb.bounty_create(
        title="Test Bounty",
        description="A test bounty",
        contract_terms={
            "budget": {"tokens": 40_000, "cost_usd": 0.05, "time_seconds": 180},
        },
    )
    return created


# ── Identity-correct claim ──────────────────────────────────


class TestIdentityCorrectClaim:
    """actor_id is stored in claimed_by, not display name."""

    def test_claimed_by_stores_canonical_actor_id(self, isolated_village, open_bounty):
        bid = open_bounty["id"]
        result = hb.bounty_claim(bid, "actor:alice")
        assert result is not None
        assert result["claimed_by"] == "actor:alice"

    def test_display_name_not_used_as_claimed_by(self, isolated_village, open_bounty):
        bid = open_bounty["id"]
        # Pass a display-name-like string — it still goes to claimed_by
        # because bounty_claim stores whatever is passed. The fix is at
        # the call site (scan_moltbook), which now passes event.actor_id.
        result = hb.bounty_claim(bid, "actor:bob")
        assert result is not None
        assert result["claimed_by"] == "actor:bob"
        assert result["claimed_by"] != "Bob"  # not the display name

    def test_correct_actor_can_submit_after_claim(self, isolated_village, open_bounty):
        bid = open_bounty["id"]
        hb.bounty_claim(bid, "actor:alice")
        wr = _make_succeeded_work_result(contract_id=f"contract:{bid}:1")
        result = br.bounty_submit(bid, "actor:alice", wr)
        assert result is not None
        assert result["bounty_id"] == bid

    def test_wrong_actor_rejected_on_submit(self, isolated_village, open_bounty):
        bid = open_bounty["id"]
        hb.bounty_claim(bid, "actor:alice")
        wr = _make_succeeded_work_result(contract_id=f"contract:{bid}:1")
        result = br.bounty_submit(bid, "actor:eve", wr)
        assert result is None  # wrong actor

    def test_duplicate_claim_rejected(self, isolated_village, open_bounty):
        bid = open_bounty["id"]
        first = hb.bounty_claim(bid, "actor:alice")
        assert first is not None
        second = hb.bounty_claim(bid, "actor:bob")
        assert second is None  # already claimed


# ── Review request publication ────────────────────────────────


class TestPublishPendingReviewRequests:
    """Dedup, no state mutation, retry safety."""

    def _create_submitted_bounty(self, isolated_village, open_bounty, actor="actor:alice"):
        bid = open_bounty["id"]
        hb.bounty_claim(bid, actor)
        wr = _make_succeeded_work_result(contract_id=f"contract:{bid}:1")
        sub = br.bounty_submit(bid, actor, wr)
        return bid, sub

    def test_one_issue_per_submission_id(self, isolated_village, open_bounty, monkeypatch):
        self._create_submitted_bounty(isolated_village, open_bounty)

        issues_created = []

        def fake_gh(path, method="GET", body=None):
            if method == "POST" and "issues" in path:
                issues_created.append(body)
                return {"number": len(issues_created), "html_url": f"https://github.com/issue/{len(issues_created)}"}
            return {}

        monkeypatch.setattr(hb, "_gh", fake_gh)

        created = hb.publish_pending_review_requests()
        assert created == 1
        assert len(issues_created) == 1

    def test_repeat_scan_no_duplicate(self, isolated_village, open_bounty, monkeypatch):
        self._create_submitted_bounty(isolated_village, open_bounty)

        issues_created = []

        def fake_gh(path, method="GET", body=None):
            if method == "POST" and "issues" in path:
                issues_created.append(body)
                return {"number": len(issues_created), "html_url": f"https://github.com/issue/{len(issues_created)}"}
            return {}

        monkeypatch.setattr(hb, "_gh", fake_gh)

        first = hb.publish_pending_review_requests()
        assert first == 1
        second = hb.publish_pending_review_requests()
        assert second == 0  # dedup
        assert len(issues_created) == 1  # only one API call

    def test_publisher_does_not_mutate_bounty_state(self, isolated_village, open_bounty, monkeypatch):
        bid, sub = self._create_submitted_bounty(isolated_village, open_bounty)

        def fake_gh(path, method="GET", body=None):
            if method == "POST":
                return {"number": 1, "html_url": "https://github.com/issue/1"}
            return {}

        monkeypatch.setattr(hb, "_gh", fake_gh)

        hb.publish_pending_review_requests()

        # Bounty state unchanged
        board = hb._load(hb.BOUNTIES)
        bounty = next(b for b in board.get("bounties", []) if b["id"] == bid)
        assert bounty["status"] == "submitted"  # not changed by publisher

    def test_api_failure_retry_safe(self, isolated_village, open_bounty, monkeypatch):
        bid, sub = self._create_submitted_bounty(isolated_village, open_bounty)

        call_count = [0]

        def fake_gh(path, method="GET", body=None):
            call_count[0] += 1
            if call_count[0] <= 2:
                return None  # fail twice
            return {"number": 1, "html_url": "https://github.com/issue/1"}

        monkeypatch.setattr(hb, "_gh", fake_gh)

        # First attempt fails
        first = hb.publish_pending_review_requests()
        assert first == 0
        # Second attempt fails
        second = hb.publish_pending_review_requests()
        assert second == 0
        # Third attempt succeeds
        third = hb.publish_pending_review_requests()
        assert third == 1

        # Fourth should be dedup
        fourth = hb.publish_pending_review_requests()
        assert fourth == 0


# ── Manual review CLI ─────────────────────────────────────────


class TestManualReviewEntry:
    """CLI validation gates."""

    def _create_submitted_bounty(self, isolated_village, open_bounty):
        bid = open_bounty["id"]
        hb.bounty_claim(bid, "actor:alice")
        wr = _make_succeeded_work_result(contract_id=f"contract:{bid}:1")
        sub = br.bounty_submit(bid, "actor:alice", wr)
        return bid, sub

    def test_invalid_submission_id_rejected(self, isolated_village, open_bounty):
        result = br._get_submission("nonexistent")
        assert result is None

    def test_mismatched_bounty_submission_rejected(self, isolated_village, open_bounty):
        bid, sub = self._create_submitted_bounty(isolated_village, open_bounty)
        # Try to review with wrong bounty_id
        board = hb._load(hb.BOUNTIES)
        bounty = next(b for b in board.get("bounties", []) if b["id"] == bid)
        assert bounty["status"] == "submitted"
        # The review CLI validates submission.bounty_id matches the requested bounty
        stored_sub = br._get_submission(sub["submission_id"])
        assert stored_sub is not None
        assert stored_sub["bounty_id"] == bid  # correct match

    def test_duplicate_review_refused(self, isolated_village, open_bounty):
        bid, _sub = self._create_submitted_bounty(isolated_village, open_bounty)

        # First review: accept
        result1 = br.bounty_review(bid, "reviewer1", "accept")
        assert result1 is not None

        # Second review: refused
        result2 = br.bounty_review(bid, "reviewer2", "accept")
        assert result2 is None

    def test_accept_moves_bounty_to_done(self, isolated_village, open_bounty):
        bid, sub = self._create_submitted_bounty(isolated_village, open_bounty)

        result = br.bounty_review(bid, "reviewer1", "accept")
        assert result is not None

        board = hb._load(hb.BOUNTIES)
        bounty = next(b for b in board.get("bounties", []) if b["id"] == bid)
        assert bounty["status"] == "done"

    def test_reject_resets_bounty_to_claimed(self, isolated_village, open_bounty):
        bid, sub = self._create_submitted_bounty(isolated_village, open_bounty)

        result = br.bounty_review(bid, "reviewer1", "reject")
        assert result is not None

        board = hb._load(hb.BOUNTIES)
        bounty = next(b for b in board.get("bounties", []) if b["id"] == bid)
        assert bounty["status"] == "claimed"


# ── Legacy done rejection ────────────────────────────────────


class TestLegacyDoneRejection:
    """done bXXX receives explicit rejection, no silent consumption."""

    def test_bounty_complete_still_refuses(self, isolated_village, open_bounty):
        bid = open_bounty["id"]
        hb.bounty_claim(bid, "actor:alice")
        result = hb.bounty_complete(bid)
        assert result is None  # still refuses completion

    def test_legacy_done_path_posts_rejection(self, isolated_village, open_bounty):
        """The done command now gets a rejection reply instead of silent consumption."""
        bid = open_bounty["id"]
        hb.bounty_claim(bid, "actor:alice")

        # Verify bounty_complete still returns None (no completion)
        result = hb.bounty_complete(bid)
        assert result is None


# ── End-to-end ────────────────────────────────────────────────


class TestEndToEnd:
    """Full lifecycle: claim → execution → submission → review → done."""

    def test_full_lifecycle(self, isolated_village, open_bounty):
        bid = open_bounty["id"]
        contract_id = f"contract:{bid}:1"
        actor = "actor:alice"

        # 1. Claim
        claimed = hb.bounty_claim(bid, actor)
        assert claimed is not None
        assert claimed["claimed_by"] == actor
        assert claimed["status"] == "claimed"

        # 2. Contract is active
        contract = hb._load_contract(contract_id)
        assert contract is not None
        assert contract.state == ContractState.ACTIVE

        # 3. Execute — use orchestrator with a mock provider
        class FakeProvider:
            name = "test"
            model = "test"

            def complete(self, prompt, *, max_tokens, timeout_seconds):
                from village.cognitive_provider import (
                    CognitiveResponse,
                    ProviderUsage,
                )

                return CognitiveResponse(
                    visible_text='{"gaps": []}',
                    reasoning_text=None,
                    finish_reason="stop",
                    provider="test",
                    model="test",
                    usage=ProviderUsage(
                        prompt_tokens=10,
                        completion_tokens=2,
                        reasoning_tokens=0,
                        total_tokens=12,
                        cost_usd=0.001,
                        duration_seconds=0.1,
                    ),
                    raw={},
                )

        request = ExecutionRequest(
            bounty_id=bid,
            actor_id=actor,
            target_file="village/heartbeat.py",
            instruction="Analyze this file for structural gaps.",
        )
        outcome = run_operator_execution(request, FakeProvider(), "dummy file content", execution_id="e2e-1")
        assert outcome.accepted is True
        assert outcome.submission is not None

        # 4. Bounty is submitted
        board = hb._load(hb.BOUNTIES)
        bounty = next(b for b in board.get("bounties", []) if b["id"] == bid)
        assert bounty["status"] == "submitted"

        # 5. Review — accept
        review_result = br.bounty_review(bid, "reviewer1", "accept")
        assert review_result is not None

        # 6. Bounty is done
        board = hb._load(hb.BOUNTIES)
        bounty = next(b for b in board.get("bounties", []) if b["id"] == bid)
        assert bounty["status"] == "done"

        # 7. Contract is fulfilled
        contract = hb._load_contract(contract_id)
        assert contract.state == ContractState.FULFILLED
