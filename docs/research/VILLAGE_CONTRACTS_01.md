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
- Nothing in this module is called from `village/heartbeat.py`'s
  production path (`scan_moltbook`/`scan_github`/`scan_brain`) or from
  any GitHub Actions workflow. It is dead code from the production
  system's point of view today, by design — see "Integration point"
  below for why forcing that connection now would have been premature.

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
