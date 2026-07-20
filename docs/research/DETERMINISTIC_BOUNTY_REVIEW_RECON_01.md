# Deterministic Bounty Review Recon 01 — evaluator architecture and typed criteria

Status: Phase 1 recon per Issue #27. Read-only specification — no evaluator
implementation, no automatic `bounty_review()` caller.

## 1. Current contract between criteria, evidence, and review

### 1.1 `VillageContract.success_criteria` (village/contracts.py:181-201)

| Field | Type | Meaning |
|---|---|---|
| `name` | `str` | Human-readable label |
| `description` | `str` (default `""`) | Free-text explanation — NOT an executable predicate |
| `required` | `bool` (default `False`) | If `True`, MUST pass for `fulfill()` to succeed |
| `weight` | `float` (default `1.0`) | Must be in `[0, 1]`. Currently unused. |
| `met` | `bool \| None` (default `None`) | `None` = not yet evaluated; `True` = passed; `False` = failed |

**Current state:** `met` is NEVER set by any production code path. Only
test code assigns to `.met`. Without an evaluator, `contract.fulfill()`
always fails for contracts with `required=True` criteria.

### 1.2 `contract.fulfill()` (village/contracts.py:256-261)

```python
def fulfill(self) -> None:
    self._require_non_terminal()
    unmet_required = [c.name for c in self.success_criteria
                      if c.required and c.met is not True]
    if unmet_required:
        raise ValueError(f"required success criteria unmet: {unmet_required}")
    self.state = ContractState.FULFILLED
```

### 1.3 `WorkResult` structure (village/work_result.py)

| Field | Type | Purpose |
|---|---|---|
| `output` | `dict[str, Any] \| None` | Worker-produced structured result |
| `evidence` | `dict[str, Any]` | Sanitized work evidence |
| `usage` | `dict[str, Any]` | Token/cost/time accounting |

### 1.4 Submission and review data flow

```
WorkResult (worker output)
    → bounty_submit() → bounty_submissions.json
    → publish_pending_review_requests() → GitHub Issue
    → [GAP: no evaluator, no review authority]
    → bounty_review() → contract.fulfill() → bounty done
```

## 2. Complete mutation path inventory (Issue #27 requirement)

### 2.1 Production code

| Location | Sets `criterion.met`? | Calls `_attach_review`? | Calls `bounty_review`? | Calls `contract.fulfill`? |
|---|---|---|---|---|
| `village/contracts.py` | NO | — | — | `fulfill()` is defined but only called by `bounty_review()` |
| `village/bounty_review.py` | NO | YES (line 333, 347) | YES (line 266, definition) | NO (calls `contract.fulfill()` indirectly via accept path) |
| `village/worker.py` | NO | NO | NO | NO (AST-verified) |
| `village/execution_orchestrator.py` | NO | NO | NO | NO (AST-verified) |
| `village/heartbeat.py` | NO | NO | NO | NO |
| `scripts/operator_execute.py` | NO | NO | NO | NO |
| `scripts/bounty_review_cli.py` | NO | NO | YES (sole production caller of `bounty_review()`) | NO |

### 2.2 Test code

| Location | Sets `.met`? |
|---|---|
| `tests/test_contracts.py:162-164` | YES — `crit.met = True` |
| `tests/test_contracts.py:185` | YES — `SuccessCriterion(..., met=True)` |
| `tests/test_contracts.py:265` | YES — `SuccessCriterion(..., met=True)` |
| `tests/test_bounty_review.py:365` | YES — `contract.success_criteria[0].met = True` |

### 2.3 Other contract terminal-state transitions

| Function | Production calls | Notes |
|---|---|---|
| `contract.violate(reason)` | **NONE** | Defined but unreachable |
| `contract.expire(reason)` | **NONE** | Defined but unreachable |
| `contract.terminate(reason)` | **NONE** | Defined but unreachable |
| `contract.fail(reason)` | **NONE** | Defined but unreachable |
| `contract.fulfill()` | Only via `bounty_review(accept)` | Sole terminal path |

## 3. Typed success-criterion protocol

### 3.1 Evaluator result type

```python
from enum import Enum

class EvalResult(str, Enum):
    PASS = "PASS"                   # criterion definitively satisfied
    FAIL = "FAIL"                   # criterion definitively not satisfied
    INDETERMINATE = "INDETERMINATE" # cannot determine — missing data, schema
                                    # mismatch, unknown evaluator type,
                                    # evaluator=None on a required criterion,
                                    # or any other inability to evaluate
```

### 3.2 Evaluator types (allowlisted)

```python
class EvaluatorType(str, Enum):
    FIELD_PRESENT = "field_present"
    FIELD_VALUE = "field_value"
    FIELD_COUNT = "field_count"
```

### 3.3 Exact parameter schemas with validation contracts

#### FIELD_PRESENT

Checks that `output` contains a specific key AND the value is not `None`.

```python
# Parameter schema (validated before use):
#   field: str (required) — dot-separated path, e.g. "gaps" or "result.summary"
# Unknown keys in evaluator_params → INDETERMINATE
# Missing required key → INDETERMINATE
# Wrong type for a known key → INDETERMINATE

# Result logic:
#   output.get("field") is not None → PASS
#   output.get("field") is None     → FAIL
#   KeyError / path resolution error → INDETERMINATE
```

#### FIELD_VALUE

Checks that a field equals an expected value.

```python
# Parameter schema:
#   field: str (required)
#   value: str | int | float | bool (required)
# Unknown keys → INDETERMINATE

# Result logic:
#   output.get("field") == value → PASS
#   output.get("field") != value → FAIL  (valid comparison, predicate false)
#   field missing or wrong type for comparison → INDETERMINATE
```

#### FIELD_COUNT

Checks that a list field has at least N elements.

```python
# Parameter schema:
#   field: str (required)
#   min_count: int (required, must be >= 0)
# Unknown keys → INDETERMINATE

# Result logic:
#   isinstance(val, list) and len(val) >= min_count → PASS
#   isinstance(val, list) and len(val) < min_count  → FAIL
#   field missing or val is not a list → INDETERMINATE
```

### 3.4 Extended SuccessCriterion

```python
@dataclass
class SuccessCriterion:
    criterion_id: str  # stable unique ID, e.g. sha256(name + evaluator + params)
    name: str
    description: str = ""
    required: bool = False
    weight: float = 1.0
    met: bool | None = None  # set only by the review authority, not the evaluator

    evaluator: EvaluatorType | None = None  # None = human-only
    evaluator_params: dict[str, Any] = field(default_factory=dict)
```

### 3.5 Evaluator function contract

```python
def evaluate_criterion(
    criterion: SuccessCriterion,
    output: dict[str, Any],
) -> EvalResult:
    """Pure, side-effect-free evaluation of one criterion.

    Returns PASS, FAIL, or INDETERMINATE. Never raises — all error
    conditions produce INDETERMINATE with a machine-readable reason code.

    Does NOT mutate criterion.met. Does NOT persist anything.
    Does NOT import or call bounty_review, contract.fulfill, or heartbeat.
    """
```

**Reason code format:** `"<evaluator_type>:<field>:<code>"` where codes are
`pass`, `fail_value`, `fail_count`, `indeterminate_missing_field`,
`indeterminate_wrong_type`, `indeterminate_unknown_evaluator`,
`indeterminate_invalid_params`, `indeterminate_human_only_required`.

## 4. Evaluator-to-authority handoff

```
┌──────────┐     ┌─────────────────┐     ┌──────────────────────┐
│ Evaluator│────▶│ EvalResult per  │────▶│ Review Authority     │
│ (pure)   │     │ criterion       │     │ (separate module)    │
│          │     │ (immutable)     │     │                      │
│ NEVER    │     │ NEVER persisted │     │ Loads submission +   │
│ mutates  │     │ by evaluator    │     │ contract fresh       │
│ state    │     └─────────────────┘     │ Validates bindings   │
└──────────┘                            │ Applies results to   │
                                        │ contract copy        │
                                        │ Calls bounty_review  │
                                        │ ONLY if accept/reject│
                                        └──────────────────────┘
```

**Review Authority** (proposed: `village/review_authority.py`):

- Loads the exact current submission and contract from persistence.
- Validates all immutable bindings (submission_id, contract_id, output_hash,
  contract_version, criterion_ids).
- Runs the evaluator against the freshly loaded data.
- Applies validated `EvalResult` values to `criterion.met` on an in-memory
  contract copy (PASS → `met=True`, FAIL → `met=False`, INDETERMINATE →
  `met=None`).
- For accept/reject: calls `bounty_review()`.
- For indeterminate: persists the evaluation attempt, updates the
  review-request Issue, does NOT call `bounty_review()`.
- Is the ONLY component permitted to set `criterion.met` in production.
- Does NOT live in `village/heartbeat.py` (that is an ingress/scheduling
  module, not a review authority). Heartbeat may invoke it.

**Heartbeat may invoke the review authority but external ingress and
platform adapters may not define evaluator configuration or verdicts.**

## 5. Automatic-accept policy

Automatic acceptance requires ALL of:

1. An explicit owner-authorized auto-review policy on the contract
   (`auto_review_enabled: bool = False`).
2. At least one required criterion with a machine evaluator
   (`required=True` AND `evaluator is not None`).
3. Every required criterion returning `PASS`.

Contracts with **no criteria**, **only optional criteria**, **required
criteria with `evaluator=None`**, **legacy contracts** (no auto-review
policy field), or **missing auto-review policy** MUST result in
`INDETERMINATE`.

This ensures that a contract with no machine-checkable criteria can never
be auto-accepted — a human must explicitly configure the contract for
autonomous review.

## 6. Tamper-evident evidence binding at submission time

Captured during `bounty_submit()` and stored in the submission record:

| Binding | How computed | When captured |
|---|---|---|
| `output_canonical_hash` | `sha256(canonical_json_dumps(output))` | `bounty_submit()` |
| `contract_policy_hash` | `sha256(canonical_json_dumps(criteria_definitions))` | `bounty_submit()` |
| `contract_id` | From `WorkResult.contract_id` | `bounty_submit()` |
| `contract_version` | `VillageContract.version` | `bounty_submit()` |
| `criterion_ids` | `[c.criterion_id for c in contract.success_criteria]` | `bounty_submit()` |
| `work_result_id` | `WorkResult.work_result_id` | `bounty_submit()` |
| `execution_id` | `WorkResult.execution_id` | `bounty_submit()` |
| `submission_id` | Generated by `_next_submission_id()` | `bounty_submit()` |

**`canonical_json_dumps`**: sorted keys, no trailing whitespace, UTF-8,
no `NaN`/`Infinity` (rejected by existing `load_json_object`). This ensures
deterministic hashing across runs.

A hash computed only during evaluation is insufficient — it cannot detect
modification of the submission record between submit and review.

## 7. Separate evaluation attempts from final review

### 7.1 `evaluation_attempts` (append-only history)

```python
# Stored in bounty_submissions.json, keyed by submission_id:
{
    "submission_id": "...",
    ...
    "evaluation_attempts": [
        {
            "attempt_id": "eval:<submission_id>:<uuid>",
            "evaluated_at": 1234567890.0,
            "evaluator_version": "abc1234",
            "criteria_results": {
                "<criterion_id>": "PASS" | "FAIL" | "INDETERMINATE"
            },
            "reason_codes": ["field_present:gaps:indeterminate_missing_field"],
            "overall_outcome": "INDETERMINATE"
        }
    ]
}
```

Evaluation attempts may contain `INDETERMINATE` outcomes — they are
diagnostic records, not authoritative decisions.

### 7.2 Final `review` record (at most one per submission)

```python
# Existing _attach_review record, unchanged structure:
{
    "reviewer_actor_id": "review-authority-v1",
    "decision": "accept" | "reject",
    "evidence": {...},
    "reviewed_at": 1234567890.0
}
```

The final `review` record is attached via `_attach_review()` exactly once.
Non-final evaluation attempts use a separate persistence path — they must
NOT call `_attach_review()`.

### 7.3 stable `criterion_id`

```python
criterion_id = sha256(f"{criterion.name}:{criterion.evaluator.value if criterion.evaluator else 'none'}:" +
                       json.dumps(criterion.evaluator_params, sort_keys=True))
```

Criterion results in evaluation attempts and review evidence are keyed by
`criterion_id`, not by human-readable `name`. This ensures that renaming a
criterion does not silently break result lookup.

## 8. Crash recovery and reconciliation

| Crash point | Detectable partial state | Reconciliation |
|---|---|---|
| Before evaluation persistence | No record of any attempt. | Retry from scratch. |
| After evaluation attempt persisted, before `bounty_review()` | `evaluation_attempts` non-empty, no `review` record, bounty still `submitted`. | Re-apply evaluation: load contract, run evaluator, check for existing final review. If previous attempt was INDETERMINATE, may re-evaluate (evaluator may have been updated). |
| After `bounty_review()` accept, before contract save | `review` record exists, contract not yet FULFILLED. | `bounty_review()` already mutated in-memory state; the remaining `_save_contract` call is idempotent. |
| After contract save, before bounty save | Contract FULFILLED, bounty still `submitted`. | Detect inconsistency: if contract is FULFILLED for a `submitted` bounty, replay the bounty status update to `done`. |
| After canonical completion, before GitHub Issue update | Bounty `done`, contract FULFILLED, Issue still open. | Re-run `publish_pending_review_requests()` which now sees a reviewed submission; update Issue with verdict comment and close/open based on outcome. |

**GitHub Issue comments, labels, and closure are non-authoritative downstream
effects.** They MUST be delivered idempotently and MAY be reconciled on the
next heartbeat cycle. GitHub API failure must not repeat, reverse, or
invalidate canonical completion stored in `bounties.json` and
`bounty_submissions.json`.

## 9. Durable review record schema

```python
@dataclass
class ReviewVerdict:
    submission_id: str
    work_result_id: str
    execution_id: str
    contract_id: str
    bounty_id: str
    contract_version: str
    output_canonical_hash: str       # from submission (not recomputed)
    contract_policy_hash: str        # from submission (not recomputed)
    criterion_ids: list[str]         # from submission (not recomputed)
    decision: str                    # "accept" | "reject" | "indeterminate"
    reason_codes: list[str]
    criteria_results: dict[str, str] # criterion_id → "PASS" | "FAIL" | "INDETERMINATE"
    evaluated_at: float
    evaluator_version: str           # git SHA of evaluator code
```

## 10. GitHub review-request Issue transition

| Role | Trigger | Behavior |
|---|---|---|
| **Audit surface** | Every evaluation attempt | Issue comment with collapsed verdict (non-authoritative) |
| **Exception surface** | `INDETERMINATE` overall outcome | Issue stays open, label `needs-human-review` |
| **Completion surface** | `accept` | Issue comment with verdict, Issue closed |
| **Rejection surface** | `reject` | Issue comment with verdict, Issue closed |
| **Break-glass surface** | Manual CLI | `scripts/bounty_review_cli.py` overrides evaluator |

The current manual CLI (`scripts/bounty_review_cli.py`) is an **interim
human review entry point.** It is neither authenticated nor audit-complete
break-glass unless a separate authorization mechanism is specified.
Future work should add reviewer identity binding and audit logging.

## 11. Authority and ownership matrix (review subsystem)

| Operation | Who | Authority mechanism |
|---|---|---|
| Define criteria with evaluator config | Contract creator | `contract_terms` in bounty record |
| Run evaluator | `village/evaluator.py` (pure, no I/O) | Deterministic rule code |
| Set `criterion.met` on contract | `village/review_authority.py` only | Loads fresh contract, applies validated results |
| Call `bounty_review()` | `village/review_authority.py` only | Separate from worker/orchestrator/evaluator |
| Call `contract.fulfill()` | `bounty_review()` only | Existing guard, unchanged |
| Human override | `scripts/bounty_review_cli.py` | Interim; future auth required |
| Publish review-request Issues | `publish_pending_review_requests()` | Idempotent, non-authoritative |

## 12. Decision table

| Condition | Outcome |
|---|---|
| Auto-review policy absent or `auto_review_enabled=False` | **INDETERMINATE** |
| No criteria on contract | **INDETERMINATE** |
| Only optional criteria | **INDETERMINATE** |
| Any required criterion has `evaluator=None` | **INDETERMINATE** |
| All required machine criteria → `PASS` | **accept** (if auto-review enabled) |
| Any required machine criterion → `FAIL` | **reject** |
| Any required machine criterion → `INDETERMINATE` | **INDETERMINATE** |
| Unknown evaluator type | **INDETERMINATE** (per-criterion) |
| Invalid evaluator params | **INDETERMINATE** (per-criterion) |
| Output field missing for evaluator | **INDETERMINATE** (per-criterion) |
| Output field wrong type for evaluator | **INDETERMINATE** (per-criterion) |
| Contract version mismatch (binding vs current) | **INDETERMINATE** |
| Schema mismatch (criterion_ids changed) | **INDETERMINATE** |
| Stale submission (reviewed elsewhere) | Existing `bounty_review()` guard → `None` |

## 13. Recommended smallest implementation Issue

**Title:** `village: deterministic review authority — evaluator + typed criteria + auto-review policy`

**Scope:**

1. Add `EvalResult` enum and typed evaluator parameter models to new
   `village/evaluator.py`.
2. Add `criterion_id` to `SuccessCriterion`, computed in `__post_init__`.
3. Extend `VillageContract` with `auto_review_enabled: bool = False`.
4. Implement `evaluate_criterion()` for FIELD_PRESENT, FIELD_VALUE,
   FIELD_COUNT with the exact contracts from §3.3.
5. Capture tamper-evident bindings in `bounty_submit()` (§6).
6. Add `evaluation_attempts` persistence path in `bounty_submissions.json`
   (separate from `_attach_review`).
7. Implement `village/review_authority.py` — loads submission + contract,
   runs evaluator, applies results, calls `bounty_review()` for
   accept/reject, persists evaluation attempts for indeterminate.
8. Wire review authority into `heartbeat()` under `VILLAGE_BOUNTIES_ENABLED`
   gate (separate from `publish_pending_review_requests`).
9. Add crash-recovery reconciliation in review authority.
10. Tests: every evaluator type × every outcome, decision table exhaustively,
    crash recovery at every point, authority boundary (evaluator never calls
    fulfill/bounty_review), tamper-evident binding mismatches, auto-review
    policy gating.

**Explicitly NOT in scope:** automatic execution, deadline enforcement,
contract expiry/failure, production Moltbook activation, new evaluator
types beyond initial three.

**Affected files (estimated):**
- `village/evaluator.py` (new)
- `village/review_authority.py` (new)
- `village/contracts.py` (SuccessCriterion.criterion_id, auto_review_enabled)
- `village/bounty_review.py` (binding capture in bounty_submit)
- `village/heartbeat.py` (wire review authority invocation)
- `tests/test_evaluator.py` (new)
- `tests/test_review_authority.py` (new)
- `docs/BEFUND.md`
