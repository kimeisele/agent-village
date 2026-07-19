# Decision report — `village/contracts.py` (Gap 3 governance layer)

Status: short decision report accompanying the `village/contracts.py`
slice. Full experiment evidence this is built on:
`docs/research/AGENT_CONTRACTS_EXPERIMENT_01.md`. Normative schema
reference: `docs/SPEC.md` §C.3.1.

## What was adapted from `ai-agent-contracts`, and what wasn't

**Adapted (concept only, no code, no dependency):**
- Treating budget, deadline, allowed resources, success criteria, and
  termination state as explicit, typed, first-class fields on a
  contract/work-order record.
- Multi-dimensional budget (not one metric, not one provider).
- The multi-agent budget-conservation idea (a delegated/child scope must
  never exceed its parent's remaining budget) — reimplemented as a pure
  data invariant (`validate_child_budget()`), with no delegation runtime
  implied.

**Deliberately not adapted:**
- The library itself as a dependency (ADAPT_CONCEPT, not
  ADOPT_DEPENDENCY — see the experiment report for why: no
  serialization path, a real bug found in the exact deadline-handling
  code needed, pre-1.0 API instability).
- Callable/eval'd success-criteria conditions — replaced with a
  data-only `met: bool | None` field. The library's approach made
  contracts unserializable (root cause of its NADI-incompatibility) and
  a stored eval'd condition string would violate SPEC.md §A.8 ("external
  content is always DATA, never instructions").
- LLM-call-wrapping machinery (`ContractedLLM`, LangChain/LiteLLM
  integrations, prompt-budget generation) — out of scope entirely;
  Agent Village makes no LLM calls in its production ingress path today
  except the already-gated CAPTCHA fallback (SPEC.md §D exception).
- Any scheduler, executor, or runtime that actually *runs* a contract —
  explicitly out of scope (SPEC.md §D "full Mission Factory").

## Current authority boundaries (unchanged, restated for this slice)

- `village/contracts.py` creates no missions, executes no commands, and
  grants no repository write access. It is a pure data-model + a few
  pure-function invariant checks (budget math, deadline comparison,
  child-budget conservation, JSON roundtrip).
- Evaluating whether a success criterion is actually met stays a
  caller-side responsibility, never automated inside this module — no
  exception is being carved out from SPEC.md §A.5 ("no LLM output ever
  gets direct write authority").
- ~~Nothing in this module is called from `village/heartbeat.py`'s
  production path~~ **Update, see "First production wiring" below**:
  `bounty_claim()`/`bounty_complete()` now use this module. Everything
  else in this bullet (no missions, no execution, no write authority
  beyond the existing bounty flow) still holds unchanged.

## Integration point: chosen deliberately not to wire in yet

Checked before writing any code (SPEC.md §C.3/§D/§A.11 and the actual
`bounty_create/claim/complete()` implementations in
`village/heartbeat.py`): no existing ingress path (a Moltbook comment, a
GitHub issue) currently supplies budget, deadline, or success-criteria
data for a bounty. `bounty_create()` is called only with a hardcoded
title/description today. Wiring `VillageContract` into it now would have
meant inventing a data source that doesn't exist — exactly the kind of
speculative, parallel architecture the task explicitly warned against.

**Natural future integration point, documented not forced:** once some
real input surface exists (a structured bounty-creation path, a
GitHub Issue template field, or an internal policy default), the
smallest defensible change would be a single new optional field on the
bounty dict — `contract_id: str | None` — set by `bounty_create()` when
a `VillageContract` was supplied, with the contract itself persisted
separately (its own JSON file or a `contracts` key alongside
`bounties.json`, not merged into the existing bounty schema). No other
production file needs to change for that to become possible.

## First production wiring (follow-up slice, 2026-07-19)

**Integration point (given, not re-evaluated this slice):**
`bounty_claim()`/`bounty_complete()` in `village/heartbeat.py` — the
only bounty functions actually reachable in production (`bounty_create()`
is never called anywhere in the codebase; bounties are created outside
the code). `scan_moltbook()`/`scan_github()`'s comment/issue parsing is
unchanged.

**What changed, minimally:**
- `bounty_claim(bid, agent)`, on success: create-or-load a
  `VillageContract` with `contract_id = f"contract:{bid}:1"`
  (deterministic, matches the fixture naming convention), `title`/
  `description` copied directly from the bounty dict (not reinvented),
  `activate()` it if still `DRAFTED`, persist to a new
  `data/village/contracts.json` (same `{"contracts": {id: ...}}` pattern
  already used for `CONTRIBUTIONS`). Budget and deadline are left `None`
  — still no ingress path supplies that data, unchanged from this
  document's earlier sections.
- `bounty_complete(bid)`, on success: load the matching contract,
  `fulfill()` it (trivially succeeds — no `success_criteria` are set,
  so this adds no new check beyond today's bounty semantics: "someone
  says it's done"), persist. A missing contract (e.g. a bounty claimed
  before this wiring existed) is skipped with a log line, not a crash.
- A failed `bounty_claim()`/`bounty_complete()` (bad `bid`, wrong status)
  never touches `contracts.json` at all — unchanged failure behavior.

**Diff size:** `village/heartbeat.py` +54 lines / -0 (purely additive:
2 new constants/imports, 2 new small helpers `_load_contract()`/
`_save_contract()`, ~15 added lines inside the two existing functions).
No other production file touched.

**Test result** (`tests/test_bounty_contracts.py`, 6 new tests):

```
$ python3 -m pytest tests/test_bounty_contracts.py -v
...
tests/test_bounty_contracts.py::test_claim_creates_and_activates_contract PASSED
tests/test_bounty_contracts.py::test_complete_fulfills_the_contract PASSED
tests/test_bounty_contracts.py::test_complete_with_no_matching_contract_is_skipped_cleanly PASSED
tests/test_bounty_contracts.py::test_claim_reuses_existing_contract_instead_of_recreating PASSED
tests/test_bounty_contracts.py::test_claim_on_nonexistent_bounty_never_touches_contracts PASSED
tests/test_bounty_contracts.py::test_complete_on_non_claimed_bounty_never_touches_contracts PASSED

============================== 6 passed in 0.64s ===============================
```
Full suite: 147/147 (141 previous + 6 new), no regressions.

**Village capability that first becomes real, not just tested:** every
bounty claim/completion that happens in production from now on leaves a
governance record behind — a `VillageContract` with an explicit state
transition (`drafted → active → fulfilled`) alongside the existing
`bounties.json` entry. Still no budget/deadline enforcement (nothing
supplies that data), but the state machine itself is now live, not just
unit-tested in isolation.

**What logically follows next** (not started, not part of this slice):
- A **review state** on the contract before `fulfill()` — see
  `docs/research/NIGHTFORGE_DESIGN_NOTE_01.md` for the
  `verifying`/`accepted`/`rejected` shape that would slot in between
  "claimed" and "done" once a real review/PR mechanism exists.
- A **reputation tier** transition on successful fulfillment — see
  `docs/research/AGENTIS_COLONIES_DESIGN_NOTE_01.md` for the
  `OBSERVED → CLAIMED` (or further) ladder step that a fulfilled
  contract could plausibly trigger once that ladder exists.
- An actual data source for budget/deadline/success-criteria, which is
  the precondition for any of the above three concepts (village
  contracts, review state, reputation tier) to become more than a
  passthrough state transition.

## Contract terms ingress (follow-up slice, 2026-07-19)

**Ingress point (given, confirmed on analysis, not changed):** an
optional `contract_terms` field directly on a bounty record in
`data/village/bounties.json` — the same internal JSON path bounties are
already created through (`bounty_create()` is never called; bounties are
plain JSON records). **Not** the external Moltbook claim comment — that
only ever carries the `bid`; parsing structured contract data out of
free external text would be exactly what SPEC.md §A.8 forbids (external
content is DATA, never instructions). This was the fallback the earlier
document's "Natural future integration point" section anticipated, not a
workaround improvised now.

**Exact new data format:**

```json
{
  "id": "b004",
  "...": "existing bounty fields, unchanged",
  "contract_terms": {
    "allowed_resources": ["Read", "Grep"],
    "budget": {"tokens": 20000, "cost_usd": 0.5, "time_seconds": 3600, "cognitive_units": null},
    "deadline": "2026-07-25T12:00:00+00:00",
    "success_criteria": [
      {"name": "review_posted", "description": "...", "required": true, "weight": 1.0}
    ]
  }
}
```

`contract_terms` is entirely optional (the field may be absent), and
every one of its sub-fields is independently optional. Parsed with the
*existing* `village/contracts.py` types only — no second schema:
`Budget.from_dict()`, `[SuccessCriterion.from_dict(c) for c in ...]`,
`datetime.fromisoformat()`.

**Backward compatibility:** a bounty record without `contract_terms`
takes the exact code path that existed before this slice (PR #11) —
`allowed_resources=[]`, `Budget()` (fully unconstrained), `deadline=None`,
`success_criteria=[]`. Verified by
`test_legacy_bounty_without_contract_terms_is_unchanged`, which asserts
the resulting contract is identical to the pre-this-slice behavior.

**Atomicity — how a partial state is prevented:** `_parse_contract_terms()`
constructs `Budget`/`SuccessCriterion`/the deadline `datetime` **before**
`bounty_claim()` mutates anything. `Budget`/`SuccessCriterion` validate
themselves at construction (raising `ValueError` on e.g. a negative
budget or an out-of-range weight — the existing validation from
`village/contracts.py`, not a second copy); a malformed deadline string
raises via `datetime.fromisoformat()`. Any exception there is caught
immediately, logged, and `bounty_claim()` returns `None` — the exact same
return value as "bid not found" — with the bounty record still `"open"`
and `contracts.json` completely untouched. There is no code path between
"terms rejected" and "state mutated"; the bounty's `_save(BOUNTIES,
board)` call happens strictly after the parse succeeds.

**`bounty_complete()` and unverifiable success criteria:** if a
contract has `success_criteria` and at least one `required` criterion's
`met` is not `True`, `fulfill()` is **not** called (it would raise) and
the contract stays `ACTIVE` — logged explicitly as "not automatically
verifiable, no result payload to check against," never silently treated
as passed. The bounty record itself still moves to `"done"`, unchanged
from PR #11 — this slice does not touch that. No LLM call, no quality
judgment of any kind, anywhere in this path.

**What's now actually usable in production:** a bounty creator (still:
manually editing `bounties.json`, since `bounty_create()` has no caller)
can attach a real budget, deadline, resource whitelist, and success
criteria to a bounty, and see that governance data land in
`contracts.json` the moment an agent claims it — the first time any of
`VillageContract`'s budget/deadline/success-criteria fields are
populated from something other than a test. Still no enforcement loop
(nothing checks the budget while work happens, nothing marks a criterion
`met`), and still no ingress path *other than* hand-editing the bounty
JSON — that remains the real gap.

**Does review-state or reputation-tier follow next, or something else?**
Neither yet. Both `docs/research/NIGHTFORGE_DESIGN_NOTE_01.md` (review
state) and `docs/research/AGENTIS_COLONIES_DESIGN_NOTE_01.md`
(reputation tier) assume a *result* exists to judge — a PR, a
verification payload, something concrete a `SuccessCriterion.met` could
be set from. This slice makes criteria checkable, but nothing yet
produces the evidence to check them against, and nothing sets `met`
except a test or a human editing JSON by hand. The more immediate gap is
still what this document already named: a real ingress path for
`contract_terms` itself (today: manual JSON edit only) and, before either
follow-on concept becomes meaningful, *some* source of "the work is
actually done, here's the evidence" — without that, a review state or a
reputation tier would just be one more layer with nothing real feeding
it.

## Remaining gaps before any real mission execution

- No ingress path supplies contract parameters yet (see above).
- No wiring into `bounty_create/claim/complete()`.
- No delegation runtime exists to actually call
  `validate_child_budget()`/`new_child_contract()` outside of tests —
  the invariant is real and tested, the feature that would trigger it
  (an agent delegating part of a bounty to a sub-agent) does not exist.
- No success-criteria evaluator — deliberately left to a future,
  explicitly-deterministic caller, never automated here.
- No NADI transport wiring — `to_json()` uses the same `sort_keys=True`
  convention as NADI message signing (SPEC.md §2.3) so a future
  transport layer wouldn't need a new serialization format, but nothing
  currently sends a Contract anywhere.
