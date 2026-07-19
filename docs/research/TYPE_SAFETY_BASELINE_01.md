# Type Safety Baseline 01 — pure recon, no code change

Status: read-only survey, requested alongside `docs/research/
OPERATOR_EXECUTION_01.md`. No mass change, no new type-checker
configuration, no `pyproject.toml`/`mypy.ini` added in this PR.

## `Any` usages in `village/` and `scripts/`

21 occurrences, all in 4 files, grepped directly (`grep -rn "\bAny\b"
village/ scripts/`):

- `village/cognitive_provider.py`: 1 (`CognitiveResponse.raw:
  dict[str, Any]` — genuinely unstructured, a provider's raw JSON
  response; legitimate).
- `village/work_result.py`: 6 (`output`/`evidence`/`usage` fields,
  `to_dict`/`from_dict` — all genuinely free-form JSON payloads by
  design, per the schema's own "neutral, JSON-native" goal).
- `village/contracts.py`: 9 (`extra: dict[str, Any]` — deliberately
  schema-tolerant unknown-field bucket, SPEC.md §C.3.1's own design;
  plus `to_dict`/`from_dict` boundary methods).
- `village/bounty_review.py`: 4 (`_safe_evidence()`'s recursive
  `Any -> Any` cleaner, and `evidence: dict[str, Any] | None` on
  `bounty_review()` — genuinely arbitrary caller-supplied review
  evidence).

**Pattern, not scattered:** every `Any` in this codebase sits at a
JSON-serialization boundary (`to_dict`/`from_dict`, a `raw` provider
payload, a schema-tolerant `extra` bucket, free-form evidence) --
exactly where `Any` is the honest type for "arbitrary JSON," not a
placeholder for something that should have been typed but wasn't. No
`Any` was found inside actual business logic (state transitions, budget
math, prompt construction, the agent loop).

## Untyped functions

Rough proxy: return-type annotation coverage per file (`def ... ->`),
counted directly, not estimated:

| File | Annotated / Total |
|---|---|
| `village/contracts.py` | 29/29 |
| `village/interpreter.py` | 5/5 |
| `village/work_result.py` | 5/5 |
| `village/nadi_bridge.py` | 2/2 |
| `village/brain.py` | 2/2 |
| `village/moltbook_captcha.py` | 38/39 |
| `village/village_core.py` | 11/12 |
| `village/bounty_review.py` | 10/11 |
| `village/worker.py` | 7/8 |
| `village/heartbeat.py` | 24/31 |
| `village/cognitive_provider.py` | 1/2 (the other is an `@abstractmethod` stub, arguably fine as-is) |
| `village/deepseek_provider.py` | 1/2 |
| `village/execution_orchestrator.py` | 1/2 |
| `scripts/operator_execute.py` | 2/2 |
| `scripts/worker_proof_01.py` | 1/1 |

`village/heartbeat.py` is the clear outlier (24/31, 7 unannotated) --
consistent with it being the oldest, most-evolved file in the repo,
grown across many slices (PR #9 through #15) rather than written fresh
against a typing discipline.

## Existing type-checker configuration

**None.** No `pyproject.toml`, no `mypy.ini`, no `pyrightconfig.json`
anywhere in the repo (`find . -maxdepth 2` confirms). No CI step runs a
type checker today (`tests.yml` only runs `pytest`). `mypy` (1.18.2) is
available locally; `pyright` is not installed in this environment.

## Real findings from a one-off, uncommitted `mypy` run

Run informationally only (`python3 -m mypy village/ scripts/
--ignore-missing-imports --explicit-package-bases`), not added to CI,
not fixed here — this is recon, not a fix pass:

```
village/contracts.py:205: error: Incompatible types in assignment
  (expression has type "datetime | None", variable has type "datetime")
village/contracts.py:216: error: Unsupported operand types for < ("datetime" and "None")
village/work_result.py:54: error: Incompatible types in assignment
  (expression has type "datetime | None", variable has type "datetime")
village/heartbeat.py:834: error: Incompatible types in assignment
  (expression has type "dict[Any, Any] | None", variable has type "dict[Any, Any]")
village/heartbeat.py:871: error: Incompatible types in assignment
  (expression has type "dict[Any, Any] | None", variable has type "dict[Any, Any]")
village/bounty_review.py:321: error: Argument 1 to "_attach_review" has
  incompatible type "Any | None"; expected "str"
village/bounty_review.py:335: error: Argument 1 to "_attach_review" has
  incompatible type "Any | None"; expected "str"
```

7 errors, 4 files. All are the same underlying shape: a field/variable
annotated as non-`Optional` (`datetime`, `dict[Any, Any]`, `str`) that
is in practice assigned a value coming from a function that can return
`None` (`normalize_datetime()`, a dict-or-None default, `bounty.get(
"current_submission_id")`). None of these are runtime bugs discovered by
this recon -- the actual code paths already guard against the `None`
case at runtime (e.g. `bounty_review()` already returns early if
`submission_id` is falsy) -- but the *static* type declarations don't
reflect that guard, so a checker can't confirm it. Worth narrowing the
declared types (`datetime | None` where that's actually possible,
`Optional[str]` for `_attach_review`'s parameter) in a future, focused
pass -- not attempted here.

## Where `Any` is legitimate (boundary catalogue)

Based on the pattern above, the legitimate-`Any` boundary in this
codebase is narrow and consistent:
1. `to_dict()`/`from_dict()` methods on every JSON-native schema
   (`VillageContract`, `WorkResult`, `Budget`, `SuccessCriterion`).
2. A provider's raw, unstructured response payload
   (`CognitiveResponse.raw`).
3. Explicitly schema-tolerant "unknown field" buckets
   (`VillageContract.extra`).
4. Caller-supplied free-form evidence/metadata dicts (`bounty_review()`'s
   `evidence` parameter, `WorkResult.evidence`/`.usage`).

Everywhere else in `village/` -- state machines, budget arithmetic,
prompt construction, the agent loop's phase transitions, contract/bounty
field access -- uses concrete types (dataclasses, enums, `str`, `float`,
`bool`) already, without `Any`.

## Smallest stepwise path to stricter typing (proposal only, not started)

1. Add a minimal `pyproject.toml` `[tool.mypy]` section (or
   `mypy.ini`), scoped to `village/` only first (not `scripts/`, not
   `experiments/`), with `--explicit-package-bases` (needed today
   because `village/heartbeat.py` collides under mypy's default module
   resolution — the "Source file found twice" error hit during this
   recon).
2. Fix the 7 concrete errors above (all narrow, mechanical
   `Optional`/guard-type fixes, no logic change) as their own small,
   focused PR.
3. Add `mypy village/` as a new, separate, non-blocking CI job (report
   only, don't fail the build) for a cycle or two, so new errors are
   visible without gating merges on a checker the team hasn't lived with
   yet.
4. Only after that: consider making it blocking, and only then consider
   widening scope to `scripts/`.

No step above was executed in this PR -- this document is the complete
deliverable for this track, per Kim's explicit "keine Massenänderung und
keine neuen Type-Checker-Regeln in diesem PR."
