# Operator Execution 01 — claimed bounty → orchestrated worker run → submitted

Status: closes the gap left by `docs/research/BOUNTY_REVIEW_GATE_01.md`
("first time a `WorkResult` can lead anywhere at all") from the other
side: this slice is what actually *produces* a real submission from a
real claimed bounty, rather than a test constructing a `WorkResult` by
hand. `submitted -> review -> done` remains out of scope, unchanged.

## Recon (read-only, before any code)

Reviewed: `village/worker.py`, `village/work_result.py`, `village/
bounty_review.py`, `village/heartbeat.py`, `scripts/worker_proof_01.py`,
`.github/workflows/worker-proof-01.yml`, `SPEC.md` §A.5/§A.8, and the
relevant test files.

**Smallest integration point outside `worker.py`/`interpreter.py`:**
neither of those two modules is it, by design (SPEC.md §A.5 -- cognition
never gets write authority, structurally enforced). `village/
bounty_review.py` already exists and already does the one authority-
bearing thing this slice needs (`bounty_submit()`) -- it does not need
to change at all. The actual gap is that **nothing currently calls
`bounty_submit()` with a real `WorkResult` produced by a real worker
run against a real claimed bounty** -- every existing caller is a test
constructing a `WorkResult` by hand. `scripts/worker_proof_01.py` is the
closest existing precedent (drives the worker, writes evidence) but
deliberately keeps its `Contract` in-memory-only and never touches a
real bounty -- it was built to prove the worker in isolation, not to
integrate with the bounty lifecycle.

**Conclusion:** the smallest correct piece is a new, narrow module whose
only job is wiring `heartbeat`'s bounty/contract state to `worker.
run_work_order()` to `bounty_review.bounty_submit()`, in that order,
with nothing else able to reach it that shouldn't. `village/
execution_orchestrator.py` (name chosen per Kim's suggested pattern,
kept as suggested since nothing in recon argued for a different one).

## Full new run path

```
claimed bounty (data/village/bounties.json)
      |
      v
village.execution_orchestrator.run_operator_execution(request, provider, file_content)
      |  loads the bounty + its contract_id-derived VillageContract (heartbeat._load_contract)
      |  rejects (no worker call at all) if: bounty missing/not claimed,
      |  actor_id mismatch, contract missing/not ACTIVE
      v
village.worker.run_work_order(contract, order, file_content, provider)   <- UNCHANGED
      |  its own bounded GENERATE/INTERPRET/EVALUATE/REPAIR loop, MAX_LLM_CALLS_PER_EXECUTION=4
      v
WorkResult
      |
      +-- status != SUCCEEDED --> ExecutionOutcome(accepted=False), bounty stays claimed, no submission
      |
      +-- status == SUCCEEDED --> village.bounty_review.bounty_submit(bounty_id, actor_id, work_result)
                                          |
                                          +-- refused --> ExecutionOutcome(accepted=False), honest, not success
                                          |
                                          +-- accepted --> bounty "submitted", ExecutionOutcome(accepted=True)
```

`village/worker.py` itself is **byte-for-byte unchanged** by this slice
-- confirmed via `git diff village/worker.py` being empty for this PR.
Its own internal repair loop, call cap, and budget accounting are
untouched; the orchestrator calls it exactly once per operator
execution and does not add a second layer of retry on top.

## Authority boundaries

- `village/execution_orchestrator.py` **may**: read bounty/contract
  state, run the worker, call `bounty_submit()`.
- It **may not**, and does not, per AST-verified source inspection
  (`tests/test_worker_no_write_authority.py`, extended this slice):
  call `bounty_review()`, call `bounty_complete()`, call `.fulfill()`
  anywhere in its own source; contain `subprocess`/`os.system`/`eval(`/
  `exec(`/`__import__`; contain any git/`gh`/HTTP-push-shaped construct
  (`git `, `gh pr`, `gh api`, `requests.post`, `urllib.request`).
- `village/worker.py` and `village/interpreter.py` **may not** import
  `village.execution_orchestrator` or call `run_operator_execution` --
  also AST-verified, symmetric to the existing guarantee that they can't
  reach `village.bounty_review` either.

## Input contract (explicit, reproducible, never inferred)

`ExecutionRequest(bounty_id, actor_id, target_file, instruction)` --
every field supplied directly by the caller of `scripts/
operator_execute.py` (a human via `workflow_dispatch` inputs, or a
future explicit caller). No Moltbook comment, GitHub issue body, or any
other external content is ever parsed as an instruction here (SPEC.md
§A.8) -- the `instruction` string is operator-authored, not derived from
ingress. No bounty is ever auto-selected from the open/claimed list;
`bounty_id` must be given explicitly (or, for the disposable proof path
only, a dedicated bounty is explicitly created and claimed by the script
itself before running -- never an existing real bounty picked
automatically).

## Error handling — controlled rejection at every stage

| Condition | Result |
|---|---|
| Bounty not `claimed` | `ExecutionOutcome(accepted=False, ...)`, worker never invoked |
| `actor_id` mismatch | same, worker never invoked |
| Contract missing or not `ACTIVE` | same, worker never invoked |
| Worker returns `FAILED`/`INVALID_OUTPUT`/`PROVIDER_ERROR`/`BUDGET_EXCEEDED` | `accepted=False`, bounty stays `claimed`, no submission, no automatic retry of the whole mission |
| `bounty_submit()` itself refuses | `accepted=False`, `work_result.status == SUCCEEDED` but `submission is None` -- reported honestly, never treated as success |

The contract's real usage is persisted (`heartbeat._save_contract()`)
after every worker run regardless of outcome -- a failed attempt still
spent real tokens/cost against the bounty's real budget, and that must
be reflected, not silently discarded.

## `scripts/operator_execute.py` — the actual operator entry point

Mirrors `scripts/worker_proof_01.py`'s security posture exactly
(`workflow_dispatch`-only trigger via `.github/workflows/
operator-execute-01.yml`, `permissions: contents: read`, 7-day evidence
artifact retention) with one deliberate difference: it **does** read/
write real `data/village/*.json` files in the runner's own checkout --
that's the entire point of this slice (worker_proof_01.py kept its
Contract in-memory-only specifically to avoid this). Safe by
construction, not convention: `permissions: contents: read` makes it
structurally impossible for anything this script does locally to ever
be committed or pushed back to the repository, regardless of what the
script does.

If `OPERATOR_BOUNTY_ID` is left blank, the script creates and claims a
clearly-labeled, disposable proof bounty itself
(`"[OPERATOR PROOF -- safe to ignore] ..."`) before running -- chosen
over a separate isolated data directory so the *actual* production code
path (`heartbeat.bounty_create/claim`, the real `bounty_submit()`) is
what gets exercised, not a parallel test-only path.

## What's deliberately still missing

- `submitted -> review -> done` (unchanged from `docs/research/
  BOUNTY_REVIEW_GATE_01.md` -- `bounty_review()` exists, nothing calls
  it automatically, by design).
- Any automatic retry of a whole failed mission (a human decides whether
  to re-claim/re-run, not this module).
- Any selection logic for *which* open bounty to work on -- every
  execution names its bounty explicitly.
- Any tie to Moltbook/GitHub ingress -- this is a purely internal,
  operator-invoked path today, not reachable from any external content.

## Target file path validation (Kim's review of PR #16)

Real finding: `permissions: contents: read` on `operator-execute-01.yml`
prevents this workflow from writing back to the repository, but does
**not** prevent it from *reading* arbitrary files on the runner's local
filesystem and sending their content to DeepSeek -- `OPERATOR_TARGET_FILE`
is a `workflow_dispatch` input, and nothing stopped it from being an
absolute path or a `../` traversal before this fix.

`scripts/operator_execute.py::resolve_target_file()` closes this:
**target files are restricted to regular, non-`.git` files that resolve
inside the repository root.** Rejected, before any file is read or any
`DeepSeekProvider` is constructed: absolute paths (regardless of where
they'd resolve), anything resolving outside the repo root (`../`
traversal or a symlink whose real target escapes the root), anything
under `.git/`, the evidence output file itself, directories, and
non-existent paths. Error messages name only the requested (relative)
input, never a resolved runner filesystem path. Reason: prevent
accidental exfiltration of runner/checkout metadata (or anything else
reachable on the runner's filesystem) to the external provider via a
mistyped or malicious workflow input.
