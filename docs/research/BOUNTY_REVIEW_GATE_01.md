# Bounty Review Gate 01 — WorkResult Submission and Review

Status: closes the loop left open by `docs/research/
AGENT_LOOP_WORKER_02.md`'s "smallest sensible next step." Follows the
minimal review-state shape from `docs/research/
NIGHTFORGE_DESIGN_NOTE_01.md`. No reputation tiers, no automatic LLM
reviewer, no multi-reviewer quorum, no appeals, no new external
dependency, no new secret, no new workflow trigger.

## 1) Exact new bounty state machine

```
open --claim()--> claimed --submit()--> submitted --review(accept)--> done
                      ^                      |
                      +----review(reject)----+
```

- `open -> claimed`: unchanged, `village/heartbeat.py::bounty_claim()`.
- `claimed -> submitted`: **new**, `village/bounty_review.py::
  bounty_submit()`. Requires a `SUCCEEDED` `WorkResult` from the same
  actor/contract.
- `submitted -> done`: **new**, `village/bounty_review.py::
  bounty_review(..., decision="accept")`. Only transition that calls
  `contract.fulfill()`.
- `submitted -> claimed`: **new**, `village/bounty_review.py::
  bounty_review(..., decision="reject")`. Same `claimed_by`, so a
  resubmit is possible; contract stays `ACTIVE`.
- `claimed -> done` (direct): **removed**. `village/heartbeat.py::
  bounty_complete()` now refuses this transition unconditionally (see
  §6).

## 2) Persisted submission/review schema

New file: `data/village/bounty_submissions.json`, shape
`{"submissions": {submission_id: {...}}}` (same dict-keyed-by-id pattern
as `contracts.json`/`contributions.json`).

```json
{
  "submission_id": "submission:<bounty_id>:<execution_id>",
  "bounty_id": "b001",
  "work_result_id": "workresult:contract:b001:1:exec-1",
  "contract_id": "contract:b001:1",
  "execution_id": "exec-1",
  "actor_id": "SomeAgent",
  "provider": "deepseek",
  "model": "deepseek-v4-flash",
  "status": "succeeded",
  "output": { "gaps": [...] },
  "evidence": { "target_file": "...", "instruction": "...", "phase_log": [...] },
  "submitted_at": 1784472820.55,
  "review": null
}
```

`review` is `null` until `bounty_review()` sets it to:
```json
{
  "reviewer_actor_id": "reviewer-1",
  "decision": "accept",
  "evidence": {},
  "reviewed_at": 1784472830.12
}
```

Not just a boolean -- every field Kim's task listed is present
(`work_result_id`, `contract_id`, `execution_id`, `actor_id`,
`provider`, `model`, `status`, evidence, `submitted_at`). No secrets, no
raw provider payload -- `_safe_evidence()` strips any key containing
`api_key`/`secret`/`authorization`/`bearer`/`raw`/`token` (case-
insensitive, recursively) and truncates any string over 4000 chars,
regardless of what `WorkResult.evidence` happens to contain (defense in
depth -- a `SUCCEEDED` result's evidence only ever holds
`{target_file, instruction, phase_log}` today by construction in
`village/worker.py`, but this normalizer doesn't trust that by
convention alone, it re-checks structurally on every call).

The bounty record itself gains one new field, `current_submission_id`,
set by `bounty_submit()` and used by `bounty_review()` to find which
submission a review decision applies to.

## 3) Authority for submit and review

- `bounty_submit()`: callable by anything that holds a `SUCCEEDED`
  `WorkResult` for the actor that claimed the bounty. Not itself
  restricted to a specific caller identity beyond the `actor_id`/
  `claimed_by` match -- the real restriction is structural: **the
  cognitive worker and interpreter cannot reach it** (see below).
- `bounty_review()`: an explicit, separately-invoked call. Per Kim's
  instruction, this slice allows it to be called by an explicit
  internal/human-authorized caller -- there is no second LLM acting as
  an autonomous quality judge. Nothing in this codebase currently calls
  `bounty_review()` automatically; it is a capability, not yet wired
  into any ingress path.
- **Structural enforcement** (`tests/test_worker_no_write_authority.py`,
  extended this slice): AST inspection proves `village/worker.py` and
  `village/interpreter.py` neither call `bounty_submit`/`bounty_review`
  by name nor import `village.bounty_review` at all -- not a behavioral
  guarantee that could pass by luck, a structural one.

## 4) Atomicity strategy

Both `bounty_submit()` and `bounty_review()` follow the same pattern
established in PR #11/#12's `bounty_claim()`: **all validation happens
before the first file write.** Concretely:

- `bounty_submit()`: bounty-exists/status/actor/work-result-status/
  contract-id-match/contract-exists-and-active are all checked by
  reading (never writing) `bounties.json` and `contracts.json` first.
  Only after every check passes does the function write
  `bounty_submissions.json`, then `bounties.json`. An invalid submission
  therefore leaves all three files byte-identical to before the call --
  tested directly (`test_invalid_submission_does_not_partially_mutate_
  any_file`, comparing full file contents before/after a rejected call).
- `bounty_review()` accept path: `contract.fulfill()` itself is the
  validation gate (it raises `ValueError` internally, before mutating
  its own state, if any required success criterion isn't `met is True`
  -- see `village/contracts.py`, unchanged by this slice). Only if that
  in-memory call succeeds does the function write the submission's
  review record, then the contract, then the bounty.
- Known limitation, stated plainly rather than engineered around: this
  is "validate fully in-memory, then perform the writes assuming they
  succeed" -- the same practical atomicity guarantee every prior slice
  in this project has used for local JSON files, not a multi-file
  transaction. A process crash between the second and third write in a
  multi-file sequence (e.g. contract saved `FULFILLED` but the bounty
  write never happens) is a real, if practically negligible, gap given
  single-process/single-threaded local file writes -- flagged here
  rather than silently assumed away.
- **Duplicate calls**: per Kim's explicit alternative ("sauber
  idempotent behandeln oder explizit deterministisch ablehnen"), this
  slice chose **explicit deterministic rejection**, not idempotency
  matching. A second `bounty_submit()` call once the bounty has left
  `claimed` returns `None`, full stop -- no attempt to detect "is this
  the same submission retried" and special-case it. Simpler, and no
  behavior to get subtly wrong.

## 5) Non-automatically-checkable success criteria

Nothing in `bounty_review()` ever sets `SuccessCriterion.met`. The
accept path relies entirely on `VillageContract.fulfill()`'s own,
already-tested rule (`village/contracts.py`, unchanged since PR #12):
a `required` criterion whose `met` is not exactly `True` blocks
fulfillment. Since no code path in this repository sets `met` from a
`WorkResult`'s content (that remains a human/separate-mechanism
responsibility, per SPEC.md §A.5), a contract with any `required`
criterion that was never explicitly marked `met=True` by something
outside this module simply cannot be accepted -- deterministically, not
by any judgment made inside `bounty_review()`. Tested directly:
`test_accept_with_unmet_required_criterion_is_refused` (criterion
present, `met` left at its default `None` -- accept refused, no
mutation) and its symmetric case `test_accept_with_met_required_
criterion_fulfills_contract` (criterion pre-set to `True` by the test,
simulating a future external mechanism -- accept proceeds).

## 6) `bounty_complete()` — narrowed, not removed

Chose the second of Kim's two offered options: **the old direct path is
controlled and refused, not turned into a silent wrapper.**
`village/heartbeat.py::bounty_complete()` keeps its exact signature
(`bounty_complete(bid) -> dict | None`) for backward compatibility at
the call-site level, but now unconditionally returns `None` for a
`claimed` bounty (same rejection semantics as "bid not found"), logging
why. It does not attempt to construct a `bounty_review()` call itself
(that would require an `actor_id`/`WorkResult` it was never given, and
silently inventing one is exactly the kind of covert bypass Kim's
instructions ruled out).

**Real, documented side effect**: `scan_moltbook()`'s existing `"done
bXXX"` comment handler calls `bounty_complete(bid)` directly
(unchanged, out of scope to touch this slice). With the narrowed
function, a bare Moltbook `"done bXXX"` comment can no longer complete a
bounty on its own -- it falls through to the already-existing "nothing
to complete" branch (marks the comment processed, no reply attempted,
no crash). This is a deliberate consequence of closing the gate, not an
oversight: real completion now requires actual submitted work evidence,
not a chat message claiming completion. Flagged explicitly here and in
BEFUND.md §32 rather than left to be discovered later.

## 7) Diff scope

New: `village/bounty_review.py`, `tests/test_bounty_review.py`,
`docs/research/BOUNTY_REVIEW_GATE_01.md`. Modified: `village/
heartbeat.py` (`bounty_complete()` narrowed, ~25 lines net change, no
other function touched), `tests/test_bounty_contracts.py` (4 tests
updated for the new `bounty_complete()` behavior, 2 removed as
superseded -- their coverage moved to `test_bounty_review.py`),
`tests/test_worker_no_write_authority.py` (+4 tests: worker/interpreter
never call or import `bounty_submit`/`bounty_review`). `docs/BEFUND.md`
§32 (single new append-only section, no existing section changed).
`village/worker.py`, `village/interpreter.py`, `village/contracts.py`,
`village/work_result.py`, `village/cognitive_provider.py`, `village/
deepseek_provider.py`, all `.github/workflows/*` -- **unchanged**.

## 8) Test count and CI

```
$ python3 -m pytest tests/ -q
........................................................................ [ 28%]
........................................................................ [ 57%]
........................................................................ [ 86%]
.................................                                        [100%]
249 passed in 3.40s
```
218 existing (post-PR #14) + 29 new in `tests/test_bounty_review.py` +
2 new in `tests/test_worker_no_write_authority.py` (the other 2 new
authority tests offset by consolidating; see file diff for the exact
+/-). No regressions, no real API call anywhere, `git status --short
data/` clean after the local run. CI: [run 29691868757](https://github.com/kimeisele/agent-village/actions/runs/29691868757),
green.

## 9) Village capability that first exists because of this

A bounty's worker-produced output now has a real, auditable path from
"the model said something" to "a human (or an explicit, separately-
authorized mechanism) decided whether that counts" -- with the model's
own output never able to self-certify success, a rejected submission
never silently discarded (it stays as audit history, re-submittable),
and a contract never fulfilled by anything other than an explicit
accept decision that itself refuses to fabricate a passed criterion.
This is the first time in the project a `WorkResult` produced by
`village/worker.py` (PR #13/#14) can lead anywhere at all beyond sitting
in `contracts.json` -- the review gate is that path, deliberately still
requiring a human/explicit decision at its one authority-bearing step.

## Corrections (Kim's independent review of PR #15)

Three real, non-stylistic issues found; no architecture change made to
address them -- all three are fixes within the existing design.

### Blocker 1 — submissions were not actually immutable

`submission_id = f"submission:{bounty_id}:{execution_id}"` plus an
upserting `_save_submission()` meant a `submit -> reject -> claimed ->
submit` cycle for the *same* `execution_id` (e.g. a naive retry with the
same `WorkResult`) would silently overwrite the first record -- losing
exactly the reject review the design claimed to preserve.

Fixed by splitting storage into two operations:
- `_next_submission_id()` always returns a fresh id -- the common case
  keeps the plain `submission:<bounty_id>:<execution_id>` form; a
  same-execution resubmit gets a numbered revision suffix (`:r2`, `:r3`,
  ...) instead of colliding.
- `_insert_submission()` is insert-only: raises `RuntimeError` if the id
  already exists, at the storage layer, not just relying on the caller
  computing a fresh one correctly.
- `_attach_review()` is the one legitimate in-place update (adding a
  review verdict to an unreviewed submission) -- it refuses to run twice
  on the same record (`RuntimeError` if `review` is already set).

Regression tests: `test_resubmit_of_the_same_execution_after_reject_
does_not_overwrite_the_first_record` (the exact scenario from the
review), `test_multiple_reject_resubmit_cycles_preserve_full_history`
(three cycles, not just one), plus direct storage-layer tests for both
new guard functions.

### Blocker 2 — evidence filtering only checked keys, not values

`_safe_evidence()` stripped credential-*shaped keys* but did nothing if
a model happened to echo a secret-*shaped value* back under an
innocuous key (e.g. `evidence["notes"]` containing a stray `sk-...`
token).

Fixed: every string value (not just ones under a suspicious key) is now
scanned against `_SECRET_VALUE_PATTERNS` (`sk-...`, `Bearer <token>`,
`Authorization: <scheme> <token>`) and redacted to `[REDACTED]` in
place, recursively through nested dicts/lists, before the length cap is
applied. Real bug caught while testing this fix itself: the
`Authorization:` pattern originally matched only one whitespace-
delimited token after the colon, leaving the actual credential exposed
right after a redacted scheme word (`Authorization: [REDACTED]
dXNlcjpwYXNz`) -- widened to consume up to a few tokens after the colon.

Regression tests: one per pattern (`sk-`, `Bearer`, `Authorization:`),
one proving redaction reaches nested structures (`phase_log` entries),
one proving ordinary text without a secret pattern is left byte-for-byte
unchanged (the redaction must not be so aggressive it mangles normal
evidence).

### Blocker 3 — no protection against a single corrupted JSON file

`village/heartbeat.py::_save()` wrote directly to the target path.
A process crash mid-write could leave a truncated file that doesn't even
parse as JSON on the next read -- worse than the already-accepted
multi-file-sequence limitation, and avoidable with a small, standard
fix.

Fixed: `_save()` now writes to a `<name>.tmp<pid>` file in the same
directory, then `Path.replace()`s it onto the target -- atomic on POSIX.
This is a change to the one shared `_save()` used by every JSON write in
`village/heartbeat.py` (bounties, pokedex, contracts, contributions,
etc.), not just the bounty-review path, so the protection is universal,
not bolted on locally. The cross-file-sequence limitation (submission ->
contract -> bounty writes are still three separate atomic single-file
writes, not one transaction) remains, documented above, and was
explicitly accepted by Kim as out of scope for "no new database, no
architecture change."

Regression tests (new file, `tests/test_atomic_save.py`): a simulated
write failure (monkeypatched `Path.write_text`) leaves an existing
target file byte-for-byte unchanged, and leaves no file at all for a
target that didn't exist yet -- either way, never a half-written,
unparseable file. Plus a check that a successful save leaves no stray
`.tmp*` files behind, and a plain round-trip sanity check.

### Result

```
$ python3 -m pytest tests/ -q
........................................................................ [ 27%]
........................................................................ [ 54%]
........................................................................ [ 82%]
..............................................                           [100%]
262 passed in 4.40s
```
249 (PR #15 as originally opened) + 13 new (9 in `test_bounty_review.py`
covering Blockers 1/2, 4 in the new `test_atomic_save.py` covering
Blocker 3). No regressions, no real API call anywhere, `git status
--short data/` clean after the local run. No new feature, no scope
expansion beyond the three requested corrections.
