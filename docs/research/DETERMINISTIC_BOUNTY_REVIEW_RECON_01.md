# Deterministic Bounty Review Recon 01 — evaluator architecture and typed criteria

Status: Phase 1 recon per Issue #27. Read-only specification — no evaluator
implementation, no automatic review activation.

## 1. Current state

### 1.1 `SuccessCriterion` (village/contracts.py:150-177)

`met` is `bool | None`, default `None`. NEVER set by production code —
only test code assigns to it. `contract.fulfill()` raises `ValueError`
for any required criterion where `met is not True`.

### 1.2 `bounty_review()` accept path (village/bounty_review.py:339-348)

Directly calls `contract.fulfill()` at line 343. This is a direct
production call, not indirect.

### 1.3 Mutation path inventory

| Location | Sets `.met`? | Calls `contract.fulfill`? |
|---|---|---|
| `bounty_review.py:343` (accept path) | NO | YES — direct call |
| `bounty_review.py` (reject path) | NO | NO |
| Every other production module | NO | NO (AST-verified for worker/orchestrator) |

### 1.4 The gap

The evaluator is the most visible missing piece, but the authoritative
application of evaluation results, immutable submission binding, a
crash-safe finalization protocol, and non-authoritative downstream
delivery are equally absent.

## 2. Evaluator (pure, side-effect-free)

### 2.1 Result type

```python
class EvalResult(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    INDETERMINATE = "INDETERMINATE"
```

### 2.2 Evaluator types and exact parameter bounds

#### FIELD_PRESENT

```python
# field: str (required, max 128 chars, max 4 dot-separated segments,
#   each segment max 32 chars, [a-zA-Z0-9_], dict-only traversal)
# Unknown keys → INDETERMINATE. Missing required key → INDETERMINATE.

# _SENTINEL distinct from None:
#   key absent (_SENTINEL) → INDETERMINATE
#   key present, value is not None → PASS
#   key present, value is None → FAIL
```

#### FIELD_VALUE

```python
# field: str (required, same path rules)
# value: str|int|float|bool (required)
#   - str: max 256 chars
#   - int|float: must be finite
#   - bool: strict (True/False only, never 0/1)
# Unknown keys → INDETERMINATE.

#   key absent → INDETERMINATE
#   type(output_val) is not type(value) → INDETERMINATE
#   output_val == value → PASS
#   output_val != value → FAIL
```

#### FIELD_COUNT

```python
# field: str (required, same path rules)
# min_count: int (required, 0 <= min_count <= 1_000_000)
# Unknown keys → INDETERMINATE.

#   key absent or val not a list → INDETERMINATE
#   len(val) >= min_count → PASS
#   len(val) < min_count → FAIL
```

#### Reason codes

`"<evaluator_type>:<field>:<code>"` — bounded (max 128 chars), constructed
from validated type/field names, never from raw output data.

### 2.3 Evaluator contract

```python
def evaluate_criterion(
    criterion: SuccessCriterion,
    output: dict[str, Any],
) -> EvalResult:
    """Pure. Never raises (returns INDETERMINATE). Never mutates state.
    Never imports review/contract/heartbeat modules."""
```

## 3. Criterion identity and definition

### 3.1 `criterion_id`

- System-generated at creation time.
- Opaque, persisted once, unique within the contract.
- Never recomputed from mutable fields (name, evaluator, params).
- Never accepted as authoritative merely because external `contract_terms`
  supplied it — the system assigns the ID.
- Identical criterion definitions receive distinct IDs.

### 3.2 `criterion_definition_hash`

- System-computed: `sha256(canonical_json_dumps({evaluator, evaluator_params}))`.
- Verified during deserialization and policy validation.
- Never trusted from stored external data without recomputation.

### 3.3 Extended `SuccessCriterion`

```python
@dataclass
class SuccessCriterion:
    criterion_id: str              # system-assigned, opaque, stable
    criterion_definition_hash: str # system-computed, verified on load
    name: str                      # display label (mutable, non-authoritative)
    description: str = ""
    required: bool = False
    weight: float = 1.0
    met: bool | None = None        # set ONLY by apply_review_decision()

    evaluator: EvaluatorType | None = None
    evaluator_params: dict[str, Any] = field(default_factory=dict)
```

## 4. Immutable FinalEvaluation artifact

```python
from enum import Enum

class ReviewDecision(str, Enum):
    ACCEPT = "accept"
    REJECT = "reject"
    INDETERMINATE = "indeterminate"

@dataclass(frozen=True)
class FinalEvaluation:
    """Immutable artifact: evaluator → review authority. Hash covers all fields."""
    submission_id: str
    bounty_id: str
    contract_id: str
    contract_version: str
    work_result_id: str
    execution_id: str
    output_canonical_hash: str
    review_policy_hash: str
    criterion_ids: tuple[str, ...]
    criterion_definition_hashes: tuple[str, ...]
    criteria_results: tuple[tuple[str, EvalResult], ...]
      # (criterion_id, result) for ALL criteria (required + optional)
    overall_decision: ReviewDecision
    reason_codes: tuple[str, ...]
    evaluator_version: str
    evaluated_at: float
    evaluation_hash: str  # sha256 of all above fields (canonical serialization)

    def compute_hash(self) -> str:
        """sha256(canonical_json_dumps(self) excluding evaluation_hash itself)"""
```

`criteria_results` covers ALL criteria. Required criteria with
non-PASS results determine the decision. Optional criteria results
are audit-only.

## 5. Canonical review-policy hash

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

Excludes: `met`, `name`, `description`, `weight` (mutable result/display fields).

## 6. Tamper-evident bindings (captured at `bounty_submit()`)

| Binding | Computation |
|---|---|
| `output_canonical_hash` | `sha256(canonical_json_dumps(output))` |
| `review_policy_hash` | `compute_review_policy_hash(contract)` |
| `contract_id` | From `WorkResult.contract_id` |
| `contract_version` | `VillageContract.version` |
| `criterion_ids` | `[c.criterion_id for c in contract.success_criteria]` |
| `criterion_definition_hashes` | `[c.criterion_definition_hash for c in ...]` |
| `work_result_id` | `WorkResult.work_result_id` |
| `execution_id` | `WorkResult.execution_id` |
| `submission_id` | `_next_submission_id()` |

## 7. `bounty_review()` — sole terminal authority for all review paths

`bounty_review()` (village/bounty_review.py:266) is extended to accept a
typed discriminated review input. It becomes the single canonical
final-review transition for both deterministic automatic review and
explicit manual review. No second independent terminal mutation function
is created.

### 7.1 Discriminated review input

```python
@dataclass(frozen=True)
class ManualReviewRequest:
    """Explicit human review via the manual CLI."""
    reviewer_actor_id: str
    decision: str  # "accept" | "reject"
    evidence: dict[str, Any] | None = None
    # No evaluation fields — human decision, not machine.

# ReviewInput = FinalEvaluation | ManualReviewRequest
```

### 7.2 Extended `bounty_review()` contract

```python
def bounty_review(
    review_input: FinalEvaluation | ManualReviewRequest,
) -> dict[str, Any] | None:
    """Sole function authorized to attach the final review, fulfill the
    contract, and terminally mutate the bounty.

    For FinalEvaluation (automatic review):
      1. Freshly loads the current submission, bounty, and contract.
      2. Validates every immutable binding in the evaluation against
         loaded state.
      3. Validates that submission_id == bounty.current_submission_id.
      4. Validates that the submission has no existing final review
         with a conflicting decision, submission_id, evaluation_hash,
         or reviewer identity. A matching existing review is resumable
         success. A conflicting review fails closed.
      5. Applies criteria results to the freshly loaded contract:
         PASS->met=True, FAIL->met=False, INDETERMINATE->met=None.
      6. Writes or resumes the finalization record
         (key: 'finalize:<submission_id>').
      7. For accept: fulfills the contract (FULFILLED), updates bounty
         to 'done', records completed_at.
      8. For reject: leaves contract ACTIVE, resets bounty to 'claimed'
         (same claimed_by).
      9. Attaches exactly one matching final review.
      10. Marks finalization record 'complete' (or 'failed_closed').

    For ManualReviewRequest (human CLI):
      Existing behavior preserved — validates decision, loads bounty
      and contract, attaches review, fulfills (accept) or resets
      (reject). No evaluation bindings. No finalization record.

    For INDETERMINATE FinalEvaluation:
      Does NOT call this function. Indeterminate evaluations are
      persisted as evaluation attempts only.

    Returns the review result dict on success, None on failure.
    """
```

### 7.3 Authority architecture

```
┌──────────────────────┐
│ Manual CLI           │──ManualReviewRequest──┐
│ (caller only)        │                       │
└──────────────────────┘                       │
                                               ▼
┌──────────────────────┐    FinalEvaluation    ┌──────────────────┐
│ Evaluator (pure)     │──────────────────────▶│ bounty_review()  │
│ never mutates state  │                       │ (sole authority  │
└──────────────────────┘                       │ for fulfill,     │
                                               │ review attachment,│
                                               │ bounty completion)│
                                               └──────────────────┘
```

**`apply_review_decision()` is NOT created as a separate authority.**
A review-authority helper may exist as a non-authoritative private adapter
that prepares the `FinalEvaluation` and calls `bounty_review()`, but it
must never call `contract.fulfill()`, `_attach_review()`, or mutate
contract/bounty persistence itself. All terminal mutation goes through
`bounty_review()`.

## 8. Finalization record (one mutable record per submission)

Key: `finalize:<submission_id>` (deterministic, one per submission).

```python
# Persisted in data/village/finalization_journal.json
# One record per submission_id (not append-only — mutable stages).

{
    "finalize:<submission_id>": {
        "submission_id": "...",
        "bounty_id": "...",
        "evaluation_hash": "...",     # sha256 of the FinalEvaluation
        "decision": "accept" | "reject",
        "stage": "prepared"
            | "review_attached"
            | "contract_applied"
            | "bounty_applied"
            | "complete"
            | "failed_closed",
        "review": { ... },            # populated at review_attached
        "created_at": 1234567890.0,
        "updated_at": 1234567890.0,
        "error": null | "error message"
    }
}
```

The `evaluation_hash` and `decision` are set once at `prepared` and
never changed. Only `stage`, `review`, `updated_at`, and `error` are
updated during progression.

## 9. Crash recovery bound to exact write order

`bounty_review()` (automatic path) writes in this order:

```
1. finalization record stage: "prepared"
2. attach review → stage: "review_attached"
3. contract mutation → stage: "contract_applied"
4. bounty mutation → stage: "bounty_applied"
5. mark stage: "complete"
```

| Detectable state | Reconciliation |
|---|---|
| Record exists at `prepared`, no review | Resume from step 2 |
| Record at `review_attached`, contract unchanged | Resume from step 3 |
| Record at `contract_applied`, bounty unchanged | Resume from step 4 |
| Record at `bounty_applied`, not `complete` | Resume from step 5 |
| Matching review already exists, same evaluation_hash | Resumable success — skip to bounty/contract check |
| Existing review with different evaluation_hash or decision | **Fail closed** — conflicting review |
| Canonical completion exists, GitHub delivery missing | `reconcile_review_issue_effects()` handles downstream |

**Both accept and reject** follow the same stage progression. For reject:
step 3 leaves contract ACTIVE, step 4 resets bounty to `claimed`.

## 10. GitHub downstream delivery

```python
def reconcile_review_issue_effects() -> int:
    """Idempotent, non-authoritative. Reads finalization records and
    review-request Issues, posts verdict comments, updates labels,
    closes/reopens Issues. GitHub API failure is non-fatal — delivery
    state is persisted and retried next cycle."""
```

Stable markers in Issue comments:
- `<!-- agent-village-review-verdict:submission_id=<sid> -->`

Delivery state persisted in `review_requests.json` per submission.
GitHub remains non-authoritative — canonical state is in bounties.json,
contracts.json, bounty_submissions.json, and finalization_journal.json.

## 11. Automatic-accept policy

Automatic acceptance requires ALL of:

1. `contract.auto_review_enabled == True`.
2. At least one required criterion with `evaluator is not None`.
3. Every required criterion returning `PASS`.

INDETERMINATE for: no criteria, only optional criteria, required
`evaluator=None` criteria, legacy contracts (no `auto_review_enabled`
field), `auto_review_enabled=False`.

## 12. Policy-authority invariant

`auto_review_enabled` and evaluator configuration may only be enabled by
owner-authorized canonical repository state (bounty `contract_terms`
persisted via a protected merge or explicit owner action).

Moltbook comments, GitHub Issue bodies, WorkResult output, LLM output,
worker output, and other external ingress **cannot create, modify, or
enable evaluator policy.** Legacy or externally supplied policy without
verified authorization results in `INDETERMINATE`.

## 13. Decision table

| Condition | Outcome |
|---|---|
| `auto_review_enabled` absent or `False` | INDETERMINATE |
| No criteria / only optional / required `evaluator=None` | INDETERMINATE |
| Policy from unauthorized external source | INDETERMINATE |
| All required machine criteria → PASS | **accept** |
| Any required machine criterion → FAIL | **reject** |
| Any required machine criterion → INDETERMINATE | INDETERMINATE |
| Unknown evaluator / invalid params / missing field | INDETERMINATE (per-criterion) |
| Binding or policy hash mismatch | INDETERMINATE |
| Conflicting existing review | fail closed |

## 14. Authority matrix

| Operation | Who | Mechanism |
|---|---|---|
| Define criteria + evaluator config | Contract creator (authorized repo state) | `contract_terms` via protected merge |
| Enable `auto_review_enabled` | Owner-authorized canonical state only | Never from external ingress |
| Assign `criterion_id` | System at creation | Opaque, never trusted from external data |
| Compute `criterion_definition_hash` | System, verified on load | Never trusted from stored external data |
| Run evaluator | `village/evaluator.py` (pure) | Deterministic rule code, never mutates state |
| Produce `FinalEvaluation` | Evaluator | Immutable frozen dataclass with self-hash |
| Call `bounty_review()` for automatic review | Review-authority helper (non-authoritative private adapter) | Prepares FinalEvaluation, delegates to `bounty_review()` |
| Attach final review, fulfill contract, mutate bounty | `bounty_review()` **only** | Sole terminal authority for all review paths |
| Call `contract.fulfill()` | `bounty_review()` accept path | Direct call within `bounty_review()` |
| Human review | `scripts/bounty_review_cli.py` → `bounty_review(ManualReviewRequest)` | Caller only, delegates to `bounty_review()` |
| GitHub delivery | `reconcile_review_issue_effects()` | Non-authoritative, idempotent |

## 15. Recommended implementation Issue

**Title:** `village: deterministic review authority — FinalEvaluation, apply_review_decision, finalization protocol`

**Scope:** `village/evaluator.py` (new), `village/review_authority.py` (new, non-authoritative helper that delegates to `bounty_review()`), `bounty_review()` extension (accepts `FinalEvaluation | ManualReviewRequest`), `SuccessCriterion` extension (`criterion_id`, `criterion_definition_hash`), `VillageContract.auto_review_enabled`, binding capture in `bounty_submit()`, `FinalEvaluation` + `ManualReviewRequest` dataclasses, finalization journal, `reconcile_review_issue_effects()`, heartbeat wiring, tests for all evaluator types × outcomes, crash recovery at every stage, authority boundaries (only `bounty_review()` fulfills/completes), policy-authority invariant tests, downstream reconciliation. No production activation.

**Affected files (estimated):** `village/evaluator.py` (new), `village/review_authority.py` (new), `village/contracts.py`, `village/bounty_review.py`, `village/heartbeat.py`, `tests/test_evaluator.py` (new), `tests/test_review_authority.py` (new), `docs/BEFUND.md`.
