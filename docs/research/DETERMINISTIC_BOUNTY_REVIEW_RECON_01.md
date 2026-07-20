# Deterministic Bounty Review Recon 01 — evaluator architecture and typed criteria

Status: Phase 1 recon per Issue #27. Read-only specification — no evaluator
implementation, no automatic `bounty_review()` caller.

## 1. Current contract between criteria, evidence, and review

### 1.1 `VillageContract.success_criteria` (village/contracts.py:181-201)

A contract carries zero or more `SuccessCriterion` records:

| Field | Type | Meaning |
|---|---|---|
| `name` | `str` | Human-readable label |
| `description` | `str` (default `""`) | Free-text explanation — NOT an executable predicate |
| `required` | `bool` (default `False`) | If `True`, MUST be `met=True` for `fulfill()` to succeed |
| `weight` | `float` (default `1.0`) | Must be in `[0, 1]`. Currently unused in any decision logic. |
| `met` | `bool \| None` (default `None`) | `None` = not yet evaluated; `True` = passed; `False` = failed |

**Current state:** `met` is NEVER set by any production code path. The only
assignments to `.met` exist in test code (`tests/test_contracts.py`,
`tests/test_bounty_review.py`). This means `contract.fulfill()` always
fails for any contract with `required=True` criteria — the met field
stays `None` forever.

**Structural gap:** There is no evaluator. The `met` field is a write-only
slot waiting for a component that does not exist.

### 1.2 `contract.fulfill()` (village/contracts.py:256-261)

```python
def fulfill(self) -> None:
    self._require_non_terminal()
    unmet_required = [c.name for c in self.success_criteria if c.required and c.met is not True]
    if unmet_required:
        raise ValueError(f"required success criteria unmet: {unmet_required}")
    self.state = ContractState.FULFILLED
```

**Behavior:** Iterates all criteria, collects names of required ones where
`met is not True`, refuses fulfillment if any exist. Optional criteria
(`required=False`) are completely ignored — they can never block fulfillment
but also can never contribute to it.

### 1.3 `WorkResult` structure (village/work_result.py)

| Field | Type | Purpose |
|---|---|---|
| `output` | `dict[str, Any] \| None` | Worker-produced structured result |
| `evidence` | `dict[str, Any]` | Sanitized work evidence (redacted before persistence) |
| `usage` | `dict[str, Any]` | Token/cost/time accounting |

The `output` field is the primary input for criteria evaluation. Its
structure is entirely determined by the worker prompt — no schema
constrains what fields exist, what types they have, or how they map to
contract criteria.

### 1.4 Submission and review data flow

```
WorkResult (worker output)
    → bounty_submit() → bounty_submissions.json
    → publish_pending_review_requests() → GitHub Issue
    → [GAP: no evaluator]
    → bounty_review() → contract.fulfill() → bounty done
```

The evaluator slot between "submission exists" and "review verdict" is
completely empty. `bounty_review()` has only the manual CLI caller.

## 2. All mutation paths for SuccessCriterion.met

| Location | Sets `.met`? | Context |
|---|---|---|
| `village/contracts.py` | NO | `SuccessCriterion` is a pure dataclass; `__post_init__` only validates weight |
| `village/bounty_review.py` | NO | Docstring explicitly states "nothing here ever sets `criterion.met`" |
| `village/worker.py` | NO | Worker never imports contracts or touches criteria |
| `village/execution_orchestrator.py` | NO | Orchestrator calls `bounty_submit()`, never touches criteria |
| `scripts/operator_execute.py` | NO | Delegates to orchestrator |
| `scripts/bounty_review_cli.py` | NO | Delegates to `bounty_review()` |
| `tests/test_contracts.py` | YES (test only) | Direct assignment: `crit.met = True` |
| `tests/test_bounty_review.py` | YES (test only) | Direct assignment on contract fixture |

**Conclusion:** A production evaluator that sets `criterion.met` does not
exist. This is the single missing piece in the lifecycle.

## 3. Typed success-criterion protocol

### 3.1 Evaluator types (allowlisted)

```python
from enum import Enum

class EvaluatorType(str, Enum):
    """Allowlisted deterministic evaluator kinds."""
    FIELD_PRESENT = "field_present"
    FIELD_VALUE = "field_value"
    FIELD_COUNT = "field_count"
    # Future (separate authorization):
    # PATTERN_MATCH = "pattern_match"
    # RANGE_CHECK = "range_check"
    # FILE_EXISTS = "file_exists"
```

Each evaluator type has a fixed, typed parameter schema. The evaluator
receives the `WorkResult.output` dict as input and returns a boolean.

### 3.2 Evaluator parameter schemas

```python
# FIELD_PRESENT: checks that output contains a specific key
# Parameters: {"field": "gaps"}
# Passes if: output.get("gaps") is not None

# FIELD_VALUE: checks that a field equals a specific value
# Parameters: {"field": "status", "value": "ok"}
# Passes if: output.get("status") == "ok"

# FIELD_COUNT: checks that a list field has at least N elements
# Parameters: {"field": "gaps", "min_count": 1}
# Passes if: isinstance(output.get("gaps"), list) and len(...) >= 1
```

### 3.3 Extended SuccessCriterion

```python
@dataclass
class SuccessCriterion:
    name: str
    description: str = ""
    required: bool = False
    weight: float = 1.0
    met: bool | None = None  # set by evaluator, not by human

    # New fields for deterministic evaluation:
    evaluator: EvaluatorType | None = None  # None = manual/human-only
    evaluator_params: dict[str, Any] = field(default_factory=dict)
```

**Key constraint:** `evaluator=None` means the criterion can only be
evaluated by a human (or is purely documentary). Free-text `description`
remains explanatory metadata — it MUST NOT be parsed as an instruction.

### 3.4 Evaluator function contract

```python
def evaluate_criterion(
    criterion: SuccessCriterion,
    output: dict[str, Any],
) -> bool:
    """Deterministic, side-effect-free evaluation of one criterion.

    Returns True if the criterion is met, False otherwise.
    Raises ValueError for unknown evaluator types (fail-closed).
    Never mutates criterion.met — the caller decides whether to persist.
    """
```

## 4. Evidence binding and provenance

Every evaluation must be bound to exact, immutable identifiers:

| Binding | Source | Required |
|---|---|---|
| `submission_id` | `bounty_submissions.json` key | YES |
| `work_result_id` | `WorkResult.work_result_id` | YES |
| `execution_id` | `WorkResult.execution_id` | YES |
| `contract_id` | `WorkResult.contract_id` | YES |
| `bounty_id` | `Submission.bounty_id` | YES |
| `contract_version` | `VillageContract.version` | YES |
| `output_hash` | `sha256(json.dumps(output))` | YES |
| `evaluated_at` | `time.time()` (UTC) | YES |

The `output_hash` ensures that a verdict computed against one output
cannot be replayed against a modified output with the same submission ID.

## 5. Three fail-closed outcomes

### accept

- All `required=True` criteria evaluate to `True`.
- All `required=False` criteria may be `True`, `False`, or `None` — no
  effect on the decision.
- `contract.fulfill()` succeeds.
- `bounty_review(decision="accept")` is called exactly once.

### reject

- At least one `required=True` criterion evaluates to `False`.
- OR: any evaluator raises `ValueError` for an unknown type.
- OR: required evidence fields are missing or malformed.
- Bounty resets to `claimed`. Contract stays `ACTIVE`.

### indeterminate

- At least one `required=True` criterion has `met=None` after all
  evaluators have run (e.g., evaluator type requires data that is not
  present in the output, but the criterion is still valid).
- OR: output structure does not match the evaluator's expected schema
  but the mismatch is recoverable (e.g., optional field missing).
- MUST NOT call `bounty_review()` — the submission stays pending.
- The review-request Issue remains open as an exception surface.
- A human or a future evaluator version may resolve it.

## 6. Reviewer authority separation

```
                    ┌─────────────────┐
                    │  External text  │  (untrusted data)
                    └────────┬────────┘
                             │ never defines evaluators
                             ▼
┌──────────┐    ┌────────────────────────┐    ┌──────────────┐
│  Worker  │───▶│  WorkResult.output     │───▶│  Evaluator   │
│(read-only│    │  (structured evidence) │    │(deterministic│
│ for state)│    └────────────────────────┘    │  rule code)  │
└──────────┘                                   └──────┬───────┘
                                                     │ sets criterion.met
                                                     ▼
                                            ┌────────────────┐
                                            │ bounty_review()│
                                            │ (sole authority│
                                            │  for fulfill)  │
                                            └────────────────┘
```

The evaluator:
- Is separate from `village/worker.py`.
- Is separate from `village/execution_orchestrator.py`.
- Is separate from `village/cognitive_provider.py`.
- Reads `WorkResult.output` and `VillageContract.success_criteria`.
- Writes `SuccessCriterion.met` (in memory, on a copy of the contract).
- NEVER calls `contract.fulfill()` or `bounty_review()`.
- NEVER imports `village/heartbeat.py`.

The review caller (future implementation):
- Calls the evaluator for all criteria.
- Calls `bounty_review()` with the decision.
- Is the ONLY production caller of `bounty_review()` beyond the manual CLI.

## 7. Idempotency, retry, and edge cases

| Scenario | Behavior |
|---|---|
| Same submission evaluated twice | Evaluator is pure — same result. `bounty_review()` refuses duplicate review (already guarded). |
| Evaluator run crashes mid-evaluation | No state mutated. Criterion.met set in memory on a contract copy. Retry from scratch. |
| Submission already reviewed | `bounty_review()` returns `None` (existing guard). |
| Contract modified between submission and review | Contract version in evidence binding. Mismatch → `indeterminate`. |
| Output structure changed (worker prompt change) | Evaluator sees new structure, may produce `indeterminate` if schema mismatches. |
| Concurrent review of same submission | `_attach_review()` refuses overwrite (existing guard). Second caller gets `None`. |
| All criteria optional (`required=False`) | `fulfill()` trivially succeeds. All evaluators still run and record results. |
| No criteria defined | `fulfill()` trivially succeeds. No evaluator needed. |

## 8. Durable review record schema

```python
@dataclass
class ReviewVerdict:
    submission_id: str
    work_result_id: str
    execution_id: str
    contract_id: str
    bounty_id: str
    contract_version: str
    output_hash: str
    decision: str  # "accept" | "reject" | "indeterminate"
    reason_codes: list[str]  # e.g. ["field_present:gaps:false", "field_count:gaps:0"]
    criteria_results: dict[str, bool | None]  # criterion name → result
    evaluated_at: float
    evaluator_version: str  # git SHA of evaluator code
```

This record is attached to the submission via `_attach_review()` as the
`review_record`, alongside the existing `reviewer_actor_id` and `decision`
fields.

## 9. GitHub review-request Issue transition

Current state: Issues are created for every unreviewed submission as a
human gate.

Future state:

| Role | Trigger |
|---|---|
| **Audit surface** | Every review verdict creates an Issue comment with the full `ReviewVerdict` (collapsed). |
| **Exception surface** | `indeterminate` verdicts keep the Issue open and add a label `needs-human-review`. |
| **Break-glass surface** | The manual CLI (`scripts/bounty_review_cli.py`) remains available for human override. |

The Issue is no longer the primary decision channel — it becomes the
transparency and exception channel.

## 10. Recommended smallest implementation Issue

**Title:** `village: deterministic review evaluator — typed criteria, accept/reject/indeterminate`

**Scope:**

1. Add `EvaluatorType` enum and evaluator parameter schemas to
   `village/contracts.py` or a new `village/evaluator.py`.
2. Extend `SuccessCriterion` with `evaluator` and `evaluator_params` fields
   (backward-compatible: `None` defaults preserve existing behavior).
3. Implement `evaluate_criterion()` for `FIELD_PRESENT`, `FIELD_VALUE`,
   `FIELD_COUNT`.
4. Implement `evaluate_submission()` — evaluates all criteria against a
   `WorkResult.output`, produces a `ReviewVerdict`.
5. Implement `scan_pending_reviews()` in `village/heartbeat.py` — calls the
   evaluator, calls `bounty_review()` for `accept`/`reject`, updates the
   review-request Issue with the verdict.
6. Update review-request Issues: add verdict comment, close for
   `accept`/`reject`, label for `indeterminate`.
7. Tests: every evaluator type (valid, invalid params, missing field,
   wrong type), accept/reject/indeterminate decision table, idempotency,
   crash recovery, Issue update behavior.
8. Authority-boundary tests: evaluator never calls `fulfill()` or
   `bounty_review()`; only the review caller may.

**Explicitly NOT in scope:** automatic execution after claim, deadline
enforcement, contract expiry, production Moltbook activation, new
evaluator types beyond the initial three.

**Affected files (estimated):**
- `village/evaluator.py` (new)
- `village/contracts.py` (SuccessCriterion extension)
- `village/heartbeat.py` (scan_pending_reviews)
- `tests/test_evaluator.py` (new)
- `docs/BEFUND.md`

## 11. Authority and ownership matrix (review subsystem)

| Operation | Who | Authority mechanism |
|---|---|---|
| Define criteria | Contract creator (human/Issue) | `contract_terms` in bounty record |
| Set `criterion.met` | Evaluator only | Deterministic rule code, AST-verified |
| Call `bounty_review()` | Review caller only | Separate from worker/orchestrator/evaluator |
| Call `contract.fulfill()` | `bounty_review()` only | Existing guard, unchanged |
| Override evaluator | Manual CLI (`bounty_review_cli.py`) | Break-glass, audit-logged |

## 12. Decision table

| Required criteria state | Optional criteria state | Outcome |
|---|---|---|
| All `required` → `True` | Any | **accept** |
| Any `required` → `False` | Any | **reject** |
| Any `required` → `None` (evaluator error) | Any | **indeterminate** |
| No criteria | — | **accept** (trivially) |
| Unknown evaluator type | — | **indeterminate** (fail-closed) |
| Output missing required field | — | **indeterminate** |
| Output field has wrong type | — | **indeterminate** |
| Contract version mismatch | — | **indeterminate** |
