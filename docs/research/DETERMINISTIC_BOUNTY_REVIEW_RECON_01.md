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

**Current state:** `met` is NEVER set by any production code path.

### 1.2 `contract.fulfill()` (village/contracts.py:256-261)

Checks `c.required and c.met is not True`, raises `ValueError` on unmet required criteria.

### 1.3 Submission and review data flow

```
WorkResult → bounty_submit() → bounty_submissions.json
    → publish_pending_review_requests() → GitHub Issue
    → [GAP: no evaluator, no review authority, no finalization protocol]
    → bounty_review() → contract.fulfill() → bounty done
```

## 2. Complete mutation path inventory (Issue #27 requirement)

### 2.1 Every caller and mutation path

| Location | Sets `.met`? | Calls `_attach_review`? | Calls `bounty_review`? | Calls `contract.fulfill`? |
|---|---|---|---|---|
| `village/bounty_review.py:343` | NO | YES (333, 347) | YES (266, definition) | **YES — direct call** in accept path |
| `village/worker.py` | NO | NO | NO | NO (AST-verified) |
| `village/execution_orchestrator.py` | NO | NO | NO | NO (AST-verified) |
| `village/heartbeat.py` | NO | NO | NO | NO |
| `scripts/bounty_review_cli.py` | NO | NO | YES (sole production caller) | NO |
| `scripts/operator_execute.py` | NO | NO | NO | NO |

### 2.2 Other contract terminal-state transitions

| Function | Production calls |
|---|---|
| `contract.violate()`, `contract.expire()`, `contract.terminate()`, `contract.fail()` | **NONE** — defined but unreachable |
| `contract.fulfill()` | **Directly called by `bounty_review()` accept path** (`bounty_review.py:343`) |

### 2.3 Test-only `.met` assignments

`tests/test_contracts.py:162-164,185,265`; `tests/test_bounty_review.py:365`.

## 3. Typed success-criterion protocol

### 3.1 Evaluator result type

```python
class EvalResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"
```

### 3.2 Evaluator types and exact parameter bounds

#### FIELD_PRESENT

```python
# Parameter schema (validated before use):
#   field: str (required, max 128 chars, max 4 segments, each segment
#          max 32 chars, only [a-zA-Z0-9_], dict-only traversal,
#          no array indexing, no wildcards)
# Unknown keys → INDETERMINATE. Missing required key → INDETERMINATE.
# Wrong type for a known key → INDETERMINATE.

# Semantics: checks key existence AND value is not None.
#   output.get("field", _SENTINEL) is not _SENTINEL
#     and output.get("field") is not None → PASS
#   key present, value is None → FAIL
#   key absent → INDETERMINATE (cannot distinguish "intentionally absent"
#     from "worker failed to produce the field")
```

_SENTINEL is a unique module-level object, distinct from `None`.

#### FIELD_VALUE

```python
# Parameter schema:
#   field: str (required, same path rules as FIELD_PRESENT)
#   value: str | int | float | bool (required)
#     - str: max 256 chars
#     - int | float: must be finite (no NaN, no Inf, no -Inf)
#     - bool: strict — True/False only, not 0/1
# Unknown keys → INDETERMINATE.

# Semantics:
#   output.get("field", _SENTINEL) is _SENTINEL → INDETERMINATE
#   type(output_val) is not type(value) → INDETERMINATE (strict type equality)
#   output_val == value → PASS
#   output_val != value → FAIL
```

#### FIELD_COUNT

```python
# Parameter schema:
#   field: str (required, same path rules)
#   min_count: int (required, 0 <= min_count <= 1_000_000)
# Unknown keys → INDETERMINATE.

# Semantics:
#   output.get("field", _SENTINEL) is _SENTINEL → INDETERMINATE
#   not isinstance(val, list) → INDETERMINATE
#   len(val) >= min_count → PASS
#   len(val) < min_count → FAIL
```

#### Reason-code format

```python
# "<evaluator_type>:<field>:<code>"
# codes: pass, fail_absent, fail_value, fail_count,
#   indeterminate_missing, indeterminate_type, indeterminate_params,
#   indeterminate_unknown_eval, indeterminate_human_only
#
# Every reason code is bounded (max 128 chars), constructed from
# validated evaluator type and field name, never from raw output data.
```

### 3.3 Extended SuccessCriterion

```python
@dataclass
class SuccessCriterion:
    criterion_id: str              # opaque stable ID, assigned once at creation
    criterion_definition_hash: str # sha256(canonical_json_dumps(def))
                                   # recomputed on definition change only
    name: str
    description: str = ""
    required: bool = False
    weight: float = 1.0
    met: bool | None = None        # set ONLY by the final review function

    evaluator: EvaluatorType | None = None
    evaluator_params: dict[str, Any] = field(default_factory=dict)
```

**`criterion_id`:** assigned once when the criterion is first created
(e.g., `sha256(name + evaluator + params)` at creation, stored opaquely).
Never recomputed from mutable `name` or `evaluator` fields. Display-name
changes do not change the ID.

**`criterion_definition_hash`:** recomputed whenever the evaluator type
or its parameters change. Used in the review-policy hash to detect
definition changes between submission and review.

### 3.4 Evaluator function contract

```python
def evaluate_criterion(
    criterion: SuccessCriterion,
    output: dict[str, Any],
) -> EvalResult:
    """Pure, side-effect-free. Returns PASS/FAIL/INDETERMINATE.
    Never raises. Never mutates state. Never imports review/contract modules."""
```

## 4. Exact evaluator-to-review API

### 4.1 Immutable final-evaluation artifact

```python
@dataclass(frozen=True)
class FinalEvaluation:
    """Immutable artifact produced by the evaluator, consumed by the review
    authority. Carries everything needed to apply criterion outcomes without
    trusting a caller-mutated contract object."""
    submission_id: str
    bounty_id: str
    contract_id: str
    contract_version: str
    output_canonical_hash: str       # from submission binding (not recomputed)
    review_policy_hash: str          # canonical projection (see §5)
    criterion_ids: tuple[str, ...]   # from submission binding
    criterion_definition_hashes: tuple[str, ...]  # one per criterion
    criteria_results: tuple[tuple[str, EvalResult], ...]  # (criterion_id, result)
    overall_decision: str            # "accept" | "reject" | "indeterminate"
    reason_codes: tuple[str, ...]
    evaluator_version: str           # git SHA
    evaluated_at: float
```

`criteria_results` maps `criterion_id` → `EvalResult` for required criteria
only (optional criteria are evaluated for audit but do not affect the
decision).

### 4.2 final review function contract

```python
def finalize_review(evaluation: FinalEvaluation) -> bool:
    """The sole function permitted to apply criterion outcomes and call
    bounty_review().

    1. Freshly loads submission, bounty, and contract from persistence.
    2. Validates all bindings in `evaluation` against loaded state.
    3. Validates review_policy_hash against current contract state.
    4. Applies PASS→met=True, FAIL→met=False, INDETERMINATE→met=None
       to the freshly loaded contract (in memory).
    5. For accept/reject: writes finalization intent, calls bounty_review().
    6. For indeterminate: does NOT call bounty_review().

    Does NOT accept a caller-mutated contract object.
    Does NOT persist criterion.met independently before review.
    """
```

**Why this must not mutate a contract copy and call existing `bounty_review()` unchanged:** `bounty_review()` (`bounty_review.py:266`) reloads
its own contract via `_load_contract()`. If a caller sets `criterion.met`
on an in-memory copy and passes it through, those mutations are invisible
to `bounty_review()`. The application of criterion outcomes MUST happen
inside the final review function, after contract reload.

## 5. Exact canonical review-policy hash projection

Computed at submission time and stored in the submission record. Includes
every decision-relevant immutable policy field:

```python
def compute_review_policy_hash(contract: VillageContract) -> str:
    projection = {
        "auto_review_enabled": contract.auto_review_enabled,
        "criteria": [
            {
                "criterion_id": c.criterion_id,
                "criterion_definition_hash": c.criterion_definition_hash,
                "required": c.required,
                "evaluator": c.evaluator.value if c.evaluator else None,
                "evaluator_params": c.evaluator_params,
            }
            for c in sorted(contract.success_criteria, key=lambda c: c.criterion_id)
        ],
        "policy_schema_version": 1,
    }
    return sha256(canonical_json_dumps(projection))
```

**Explicitly excluded:** `met`, `name`, `description`, `weight` — these
are mutable result or display fields that do not control evaluation.

## 6. Idempotent finalization protocol (accept and reject)

### 6.1 Finalization intent journal

```python
# New persistence file: data/village/finalization_journal.json
# Append-only array of FinalizationIntent records.

@dataclass
class FinalizationIntent:
    intent_id: str             # "finalize:<submission_id>:<uuid>"
    submission_id: str
    bounty_id: str
    contract_id: str
    decision: str              # "accept" | "reject"
    final_evaluation: dict     # serialized FinalEvaluation
    created_at: float
    status: str                # "pending" | "applied" | "failed"
```

Written BEFORE any bounty/contract mutation. Contains enough immutable
information to reconstruct and finish the transition after process loss.

### 6.2 Crash recovery matrix

| Crash point | Detectable state | Reconciliation |
|---|---|---|
| Before intent write | No intent exists. Submission unreviewed. | Retry evaluation from scratch (evaluator is pure). |
| After intent write, before `bounty_review()` | Intent exists with status `pending`. No `review` on submission. Bounty `submitted`. Contract `ACTIVE`. | Replay: call `finalize_review()` with the stored FinalEvaluation. It will detect the existing intent and continue from where it left off. |
| After `bounty_review()`, before contract save | `review` record exists. Contract not yet mutated. | Replay: `finalize_review()` detects `review` exists, calls `_save_contract()` to persist FULFILLED state. |
| After contract save, before bounty save | Contract FULFILLED. Bounty still `submitted`. | Replay: `finalize_review()` detects inconsistency, persists bounty `done` state. |
| Reject: review written, bounty still `submitted` | `review` record with `decision="reject"`. Bounty still `submitted`. | Replay: `finalize_review()` detects `review` exists with reject, persists bounty `claimed` reset. |
| Canonical completion done, GitHub effects missing | Bounty `done`, contract FULFILLED. Issue still open. | `reconcile_review_issue_effects()` (see §7) runs idempotently on next heartbeat. |

**Retry guard:** `_attach_review()` already refuses to overwrite an
existing review. A retry that reaches the `bounty_review()` call after a
prior successful review will get `None` and MUST detect this as
"already completed" rather than "failed."

**Intent cleanup:** After successful finalization (all mutations persisted),
the intent status is set to `applied`. Intents with status `applied` are
pruned after a configurable retention period.

## 7. GitHub downstream delivery

### 7.1 Separate function

```python
def reconcile_review_issue_effects() -> int:
    """Idempotent delivery of review verdicts to GitHub Issues.

    Reads bounty_submissions.json for reviewed submissions, reconciles
    against review_requests.json and the current GitHub Issue state.
    Posts evaluation-attempt comments, final-verdict comments, updates
    labels, and closes/reopens Issues as needed.

    GitHub API failure does not retry mutation — it leaves the local
    delivery state unchanged for the next cycle.

    Returns the number of successfully delivered effects.
    """
```

### 7.2 Stable markers

```python
# Evaluation-attempt comment marker:
# <!-- agent-village-eval-attempt:submission_id=<sid>:attempt=<uuid> -->

# Final verdict comment marker:
# <!-- agent-village-review-verdict:submission_id=<sid>:decision=<accept|reject> -->
```

### 7.3 Delivery state persistence

```python
# In review_requests.json, per submission:
{
    "issue_number": 42,
    "issue_url": "...",
    "created_at": 1234567890.0,
    "delivery": {
        "eval_attempts_delivered": ["<attempt_uuid>", ...],
        "verdict_delivered": True,
        "issue_closed": True,
        "last_delivery_attempt": 1234567890.0,
        "last_delivery_error": None
    }
}
```

### 7.4 Reconciliation flows

**After GitHub API success but local persistence failure:** On next
heartbeat, `reconcile_review_issue_effects()` fetches the Issue, detects
the marker comment already present, and updates local delivery state to
match (no duplicate comment).

**After canonical completion but GitHub API failure:** Delivery state
shows `verdict_delivered: False`. Next heartbeat retries the API call.
The comment marker ensures idempotency — posting the same marker twice
is harmless (or detected and skipped).

**GitHub remains non-authoritative.** The canonical state is in
`bounties.json`, `contracts.json`, and `bounty_submissions.json`.
GitHub Issues are downstream effects only.

## 8. Tamper-evident evidence binding at submission time

Captured during `bounty_submit()` and stored in the submission record:

| Binding | How computed |
|---|---|
| `output_canonical_hash` | `sha256(canonical_json_dumps(output))` |
| `review_policy_hash` | `compute_review_policy_hash(contract)` (see §5) |
| `contract_id` | From `WorkResult.contract_id` |
| `contract_version` | `VillageContract.version` |
| `criterion_ids` | `[c.criterion_id for c in contract.success_criteria]` |
| `criterion_definition_hashes` | `[c.criterion_definition_hash for c in ...]` |
| `work_result_id` | `WorkResult.work_result_id` |
| `execution_id` | `WorkResult.execution_id` |
| `submission_id` | Generated by `_next_submission_id()` |

**`canonical_json_dumps`:** sorted keys, no trailing whitespace, UTF-8,
no `NaN`/`Infinity` (rejected by existing `load_json_object`).

## 9. Automatic-accept policy

Automatic acceptance requires ALL of:

1. `contract.auto_review_enabled == True`.
2. At least one required criterion with a machine evaluator.
3. Every required criterion returning `PASS`.

Contracts with **no criteria**, **only optional criteria**, **required
criteria with `evaluator=None`**, **legacy contracts** (no
`auto_review_enabled` field), or **`auto_review_enabled=False`** MUST
result in `INDETERMINATE`.

## 10. Decision table

| Condition | Outcome |
|---|---|
| `auto_review_enabled` absent or `False` | **INDETERMINATE** |
| No criteria, only optional criteria, or required `evaluator=None` | **INDETERMINATE** |
| All required machine criteria → `PASS` | **accept** |
| Any required machine criterion → `FAIL` | **reject** |
| Any required machine criterion → `INDETERMINATE` | **INDETERMINATE** |
| Unknown evaluator type, invalid params, missing/wrong-type field | **INDETERMINATE** (per-criterion) |
| Contract version or policy hash mismatch | **INDETERMINATE** |
| Stale submission (already reviewed) | Existing guard → `None` |

## 11. Authority and ownership matrix

| Operation | Who | Authority mechanism |
|---|---|---|
| Define criteria | Contract creator | `contract_terms` in bounty record |
| Run evaluator | `village/evaluator.py` (pure, no I/O) | Deterministic rule code |
| Produce FinalEvaluation | Evaluator | Immutable frozen dataclass |
| Write finalization intent | `village/review_authority.py` | Before any mutation |
| Apply criterion outcomes to contract | `finalize_review()` only | After fresh contract load |
| Call `bounty_review()` | `finalize_review()` only | After intent write |
| Call `contract.fulfill()` | `bounty_review()` accept path | Existing guard |
| Human override | `scripts/bounty_review_cli.py` | Interim, not authenticated |
| GitHub Issue delivery | `reconcile_review_issue_effects()` | Non-authoritative, idempotent |

## 12. Recommended smallest implementation Issue

**Title:** `village: deterministic review authority — evaluator + finalization protocol + GitHub reconciliation`

**Scope:** `village/evaluator.py` (new), `village/review_authority.py` (new),
SuccessCriterion extension (criterion_id, criterion_definition_hash),
`auto_review_enabled` on VillageContract, binding capture in
`bounty_submit()`, `FinalEvaluation` dataclass, finalization intent journal,
`finalize_review()`, `reconcile_review_issue_effects()`, heartbeat wiring,
tests for all evaluator types × outcomes, crash recovery at every point,
authority boundaries, downstream reconciliation. No production activation.

**Affected files (estimated):** `village/evaluator.py` (new),
`village/review_authority.py` (new), `village/contracts.py`,
`village/bounty_review.py`, `village/heartbeat.py`,
`tests/test_evaluator.py` (new), `tests/test_review_authority.py` (new),
`docs/BEFUND.md`.
