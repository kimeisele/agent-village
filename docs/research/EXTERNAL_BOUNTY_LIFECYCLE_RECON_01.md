# External Bounty Lifecycle Recon 01 — inventory, state machines, authority model

Status: Phase 1 recon per Issue #21. Read-only — no production activation,
no implementation of the full lifecycle.

## 1. Complete function inventory

### 1.1 `bounty_create()` — `village/heartbeat.py:419`

| Aspect | Detail |
|---|---|
| Signature | `bounty_create(title, description, reward="reputation", contract_terms=None) -> dict[str, Any]` |
| Reads | `data/village/bounties.json` |
| Writes | `data/village/bounties.json` |
| State transition | none (creates at `"open"`) |
| Authority | none — any caller. NOT called in production; only `scripts/operator_execute.py` for disposable proof bounties |
| Idempotent | no — each call appends a new record |
| Error | never returns None |

### 1.2 `bounty_list()` — `village/heartbeat.py:453`

| Aspect | Detail |
|---|---|
| Signature | `bounty_list(status="open") -> list[dict[str, Any]]` |
| Reads | `data/village/bounties.json` |
| Writes | none |
| State transition | none (read-only) |
| Idempotent | yes |

### 1.3 `bounty_claim()` — `village/heartbeat.py:494`

| Aspect | Detail |
|---|---|
| Signature | `bounty_claim(bid, agent) -> dict[str, Any] \| None` |
| Reads | `bounties.json`, `contracts.json` |
| Writes | `bounties.json` (status="claimed"), `contracts.json` (create-or-load, activate) |
| State transition | `"open"` → `"claimed"` |
| Authority | none — any agent name can claim any open bounty. No actor_id check. |
| Idempotent | partially — second claim returns None (status no longer "open"). Contract path is idempotent (refuses reactivation). |
| Error | Returns None for: not found, not open, or malformed `contract_terms` (atomic rejection before any write). Raises ValueError if record is not a dict. |

**Notable:** The `agent` parameter is a plain string set directly as `claimed_by`. There is no cryptographic identity binding.

### 1.4 `bounty_complete()` — `village/heartbeat.py:545`

| Aspect | Detail |
|---|---|
| Signature | `bounty_complete(bid) -> dict[str, Any] \| None` |
| Reads | `bounties.json` |
| Writes | **none** — unconditionally refuses all `claimed` → `done` transitions |
| State transition | **none.** Deliberately narrowed (LEGACY). |
| Authority | none (always refuses) |
| Idempotent | yes — always returns None |
| Bypass risk | **None.** The function is dead code. Only caller is `scan_moltbook()` (line 922), which marks the comment processed and moves on. |

### 1.5 `bounty_submit()` — `village/bounty_review.py:192`

| Aspect | Detail |
|---|---|
| Signature | `bounty_submit(bounty_id, actor_id, work_result) -> dict[str, Any] \| None` |
| Reads | `bounties.json`, `contracts.json`, `bounty_submissions.json` |
| Writes | `bounty_submissions.json` (immutable append), `bounties.json` (status="submitted") |
| State transition | `"claimed"` → `"submitted"` |
| Authority checks | (1) bounty status == "claimed", (2) `claimed_by` matches `actor_id`, (3) `work_result.status == SUCCEEDED`, (4) `work_result.contract_id` matches deterministic contract ID, (5) ACTIVE contract exists |
| Idempotent | no — second submit on same bounty returns None |
| Atomicity | all validation is read-only; first write only after all gates pass. Invalid submissions modify no files. |

### 1.6 `bounty_review()` — `village/bounty_review.py:266`

| Aspect | Detail |
|---|---|
| Signature | `bounty_review(bounty_id, reviewer_actor_id, decision, evidence=None) -> dict[str, Any] \| None` |
| Reads | `bounties.json`, `contracts.json`, `bounty_submissions.json` |
| Writes | `bounty_submissions.json` (immutable review attachment), `bounties.json`, `contracts.json` (accept only) |
| State transitions | accept: `"submitted"` → `"done"` + `ACTIVE` → `FULFILLED`. reject: `"submitted"` → `"claimed"` (contract stays ACTIVE) |
| Authority checks | (1) decision must be "accept"/"reject", (2) bounty status == "submitted", (3) contract exists, (4) `current_submission_id` is valid non-empty string, (5) submission exists and is not yet reviewed, (6) for accept: `contract.fulfill()` validates all required criteria are met |
| Idempotent | no — duplicate review returns None |
| **This is the only function that can mark a bounty "done" or call `contract.fulfill()`.** |

### 1.7 `run_operator_execution()` — `village/execution_orchestrator.py:72`

| Aspect | Detail |
|---|---|
| Reads | `bounties.json`, `contracts.json` |
| Writes | `contracts.json` (budget usage only), then via `bounty_submit()`: `bounty_submissions.json`, `bounties.json` |
| State transition | delegates to `bounty_submit()`: `"claimed"` → `"submitted"` (SUCCEEDED only) |
| Authority checks | (1) bounty exists and status == "claimed", (2) `actor_id` matches `claimed_by`, (3) ACTIVE contract exists |
| Never calls | `bounty_review()`, `bounty_complete()`, `contract.fulfill()` — AST-verified |

### 1.8 `run_work_order()` — `village/worker.py:155`

| Aspect | Detail |
|---|---|
| Reads/Writes | no files — in-memory only. Mutates contract via `record_usage()`. |
| State transition | none — produces `WorkResult` only |
| Authority | none (pure execution). Never imports `village.heartbeat`, `village.bounty_review`, or `village.execution_orchestrator` — AST-verified. |
| Hard limits | `MAX_REPAIR_ATTEMPTS=2`, `MAX_LLM_CALLS_PER_EXECUTION=4`. No shell/subprocess/exec. |

## 2. State transition table

### Bounty states

| Transition | Function | Precondition | Postcondition | File writes |
|---|---|---|---|---|
| (none) → open | `bounty_create()` | none | `status="open"` | `bounties.json` |
| open → claimed | `bounty_claim()` | `status=="open"` | `status="claimed"`, `claimed_by`, `claimed_at` | `bounties.json`, `contracts.json` |
| claimed → submitted | `bounty_submit()` | `status=="claimed"`, actor match, SUCCEEDED, contract ACTIVE | `status="submitted"`, `current_submission_id` | `bounties.json`, `bounty_submissions.json` |
| submitted → done | `bounty_review("accept")` | `status=="submitted"`, all required criteria met | `status="done"`, `completed_at` | `bounties.json`, `contracts.json`, `bounty_submissions.json` |
| submitted → claimed | `bounty_review("reject")` | `status=="submitted"` | `status="claimed"` (same claimed_by) | `bounties.json`, `bounty_submissions.json` |
| claimed → done | `bounty_complete()` | **REFUSED** | none | none |

### Contract states (parallel lifecycle)

| Transition | Function | Precondition |
|---|---|---|
| DRAFTED → ACTIVE | `contract.activate()` | must be DRAFTED |
| ACTIVE → FULFILLED | `contract.fulfill()` | all required criteria `met=True` |
| ACTIVE → VIOLATED | `contract.violate()` | not terminal |
| ACTIVE → EXPIRED | `contract.expire()` | not terminal |
| ACTIVE → TERMINATED | `contract.terminate()` | not terminal |
| ACTIVE → FAILED | `contract.fail()` | not terminal |

**Note:** `violate()`, `expire()`, `terminate()`, and `fail()` are defined but never called in production code. Only `activate()` and `fulfill()` have production call sites.

## 3. Authority / ownership matrix

| Operation | Who can call it | Authority mechanism | AST-verified |
|---|---|---|---|
| `bounty_create()` | anyone (no production path) | none | — |
| `bounty_claim()` | any Moltbook commenter, any script caller | none (plain string agent name) | — |
| `bounty_submit()` | `run_operator_execution()` only | actor_id matches claimed_by; contract ACTIVE; WorkResult SUCCEEDED | yes |
| `bounty_review()` | **no production caller** (reserved for human/separate automation) | decision validation; submission state check; criteria enforcement via `contract.fulfill()` | — |
| `contract.fulfill()` | **only** `bounty_review(accept)` | all required criteria met | yes |
| `contract.record_usage()` | `run_work_order()` (worker) | budget dimension validation | yes |
| direct `_save(BOUNTIES, ...)` | `bounty_create`, `bounty_claim`, `bounty_submit`, `bounty_review` | call-site state validation before write | — |
| `bounty_complete()` | `scan_moltbook()` only | **always refuses** | — |

## 4. Bypass and legacy path inventory

### 4.1 `bounty_complete()` — DELIBERATELY DISABLED

Previously allowed `claimed → done` without review. Now unconditionally refuses. The `"done bXXX"` Moltbook comment path still calls it but it is a silent no-op. Comment is marked processed and never retried.

**Bypass risk: NONE.** Verified by `test_bounty_contracts.py:62-101`.

### 4.2 Worker → fulfill/complete — STRUCTURALLY BLOCKED

Worker never imports `village.heartbeat` or `village.bounty_review`. Never calls `fulfill()`, `bounty_complete()`, `bounty_submit()`, or `bounty_review()`. Enforced by 17 AST-level tests in `test_worker_no_write_authority.py`.

**Bypass risk: NONE.**

### 4.3 Orchestrator → review/complete — STRUCTURALLY BLOCKED

Orchestrator never calls `bounty_review()`, `bounty_complete()`, or `contract.fulfill()`. Enforced by AST-level tests.

**Bypass risk: NONE.**

### 4.4 Contract terminal states reachable without review — FORWARD-LOOKING CODE

`violate()`, `expire()`, `terminate()`, `fail()` are defined but have **no production call sites**. If a future caller invokes them on an ACTIVE contract, the contract becomes terminal without review. This is currently not exploitable because no code path reaches them.

**Bypass risk: DORMANT.** The primitives exist but are unreachable. Any future integration must route through the review gate or explicitly document the bypass.

### 4.5 Missing deadline enforcement

`contract.is_past_deadline()` exists and is tested, but no production code checks it. A contract past its deadline remains ACTIVE indefinitely.

**Bypass risk: DORMANT.** Not a bypass today, but deadline enforcement should be added to the operator execution path.

## 5. Real connections vs. isolated components

### Connected (real, tested)

| Path | Status |
|---|---|
| Moltbook comment → `bounty_claim()` via `scan_moltbook()` | **Connected.** Gated behind `VILLAGE_BOUNTIES_ENABLED=1`. |
| `operator_execute.py` → `run_operator_execution()` → `bounty_submit()` | **Connected.** Manual workflow_dispatch only. |
| `bounty_review()` accept/reject | **Connected.** Callable but has no automated production caller. |
| Worker AST-level authority boundary | **Connected.** 17 tests enforce the structural invariants. |

### Isolated (defined, tested, but not wired into the full chain)

| Component | Status |
|---|---|
| Automatic execution after claim | **Isolated.** No heartbeat path calls `run_operator_execution()`. |
| Automatic review of submissions | **Isolated.** No code calls `bounty_review()` programmatically. |
| Deadline enforcement | **Isolated.** `is_past_deadline()` is defined but never called in production. |
| `violate()`/`expire()`/`terminate()`/`fail()` | **Isolated.** Defined but unreachable. |
| `bounty_create()` in production | **Isolated.** Only used for disposable proof bounties in `operator_execute.py`. |
| Full lifecycle from external claim to `done` | **Isolated.** The pieces exist but no single path connects them end-to-end. |

## 6. Moltbook confirmation/listing anomaly

Documented in `docs/BEFUND.md` §15: Moltbook deduplicates identical comment content. A byte-identical retry returns the OLD (already-failed) comment instead of creating a new one with a fresh CAPTCHA challenge.

**Impact on lifecycle:** Reply confirmation (via `_post_comment_verified()`) cannot assume immediate visibility. The `_retry_suffix()` mechanism appends `(attempt N)` to make each retry byte-unique. Pending confirmations are tracked in `data/village/pending_confirmations.json` across heartbeat cycles.

**Design constraint:** No lifecycle transition may depend on a just-posted comment being immediately visible in a listing API. The current code already handles this via the pending-confirmation retry mechanism.

## 7. Recommended single authoritative completion boundary

The code already has the correct boundary: **only `bounty_review(decision="accept")` may set a bounty to `"done"` and call `contract.fulfill()`.**

What is missing is the **caller** of `bounty_review()` — it has no production integration. The smallest change to close the lifecycle is adding a caller.

### Recommended architecture

```
external claim (Moltbook)
    → scan_moltbook() → bounty_claim()
    → [operator_execute.py or automated heartbeat step]
    → run_operator_execution() → bounty_submit()
    → [review step: human or deterministic automation]
    → bounty_review(accept|reject)
```

The review step is the critical gap. Two options:

**Option A — Human review via GitHub Issue.** `bounty_submit()` creates a GitHub Issue with submission details. A human reviews and triggers `bounty_review()` via a workflow_dispatch step.

**Option B — Deterministic automated review.** A separate review function checks contract success criteria against the WorkResult evidence and calls `bounty_review()` if all criteria are objectively met. Uses the existing `SuccessCriterion` data model.

Both options preserve the invariant that the worker never completes a bounty.

## 8. Recommended smallest implementation PR

**Title:** `village: external bounty lifecycle — wired end-to-end with review gate`

**Scope:**
1. Add `scan_submitted()` to `village/heartbeat.py` — checks `bounty_submissions.json` for unreviewed submissions and creates a GitHub Issue for each (Option A) or runs deterministic review (Option B).
2. Add `bounty_review` caller — either a `workflow_dispatch` step reading the Issue, or an automated function.
3. Wire deadline enforcement into `run_operator_execution()` — reject execution if `contract.is_past_deadline()`.
4. Tests for the full end-to-end path: external claim → ingress → identity → claim → execution → submission → review → done.
5. Tests for all negative paths: wrong actor, non-SUCCEEDED, deadline exceeded, duplicate claim, duplicate review, corrupt persistence.

**Not in scope:** automatic execution triggering, `violate()`/`expire()` activation, reputation, token economy.

## 9. File inventory

| File | Role |
|---|---|
| `village/heartbeat.py` | Bounty create/claim/list/complete, Moltbook scan, persistence |
| `village/bounty_review.py` | Submission, review gate, evidence sanitization, audit history |
| `village/contracts.py` | VillageContract state machine, budget, success criteria |
| `village/worker.py` | Bounded LLM execution loop (read-only for bounty state) |
| `village/execution_orchestrator.py` | Operator execution pipeline (worker → submit) |
| `village/village_core.py` | CanonicalIngressEvent, actor identity, contributions |
| `scripts/operator_execute.py` | Manual operator entry point (workflow_dispatch) |
| `data/village/bounties.json` | Bounty records (3 open bounties: b001, b002, b003) |
| `data/village/contracts.json` | Contract records |
| `data/village/bounty_submissions.json` | Submission and review audit trail |
| `tests/test_bounty_review.py` | 30 tests: submission/review lifecycle |
| `tests/test_bounty_contracts.py` | 13 tests: claim/contract wiring |
| `tests/test_execution_orchestrator.py` | 11 tests: operator execution pipeline |
| `tests/test_bounty_gate.py` | 3 tests: VILLAGE_BOUNTIES_ENABLED gating |
| `tests/test_worker_no_write_authority.py` | 17 tests: AST-level authority boundaries |

## 10. Untested paths (known gaps)

1. Concurrent state modifications (multi-file atomicity is documented as a known limitation)
2. Contract expiry/violation interaction with bounty states
3. Production bounties (b001-b003) have no `contract_terms` — contract-terms testing uses synthetic data only
4. No automated execution triggering (only manual workflow_dispatch)
5. No automated review (no caller for `bounty_review()`)
6. No deadline enforcement in production paths
