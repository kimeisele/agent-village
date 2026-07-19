# Agent Loop Worker 02 — from one-shot JSON caller to a bounded Cognitive Worker

Status: security-critical slice, extends PR #13 (`docs/research/
INTERNAL_WORKER_PROOF_01.md`). All hard limits from PR #13 remain in
force; this document only records what changed and why. At time of
writing, `DEEPSEEK_API_KEY` is a live repo secret (set 2026-07-19,
Kim's separate decision after PR #13's proof run) — the workflow is
runnable, but still only via manual `workflow_dispatch`.

## Why this rebuild happened

PR #13's live proof run came back `INVALID_OUTPUT`: `completion_tokens`
hit exactly the 2000-token cap while `content` was empty.
`village/worker.py` v1 had no way to distinguish "the model said
nothing" from "the model said something, just not where I was looking."
This slice fixes that at the root, not with a bigger `max_tokens` and a
shrug.

## Step 0 — recon in kimeisele/steward (read-only, targeted)

Read (not copied): `steward/loop/engine.py` (AgentLoop), `steward/buddhi.py`
(outcome evaluation / phase-driven decisions), `steward/cbr.py`
(cognitive token budgeting). Findings:

- `steward/loop/engine.py` is a full async tool-calling agent loop —
  `MahaAttention` router, `ToolRegistry`, parallel tool-dependency
  scheduling, `MAX_TOOL_ROUNDS = 50`. This is the "full Steward autonomy"
  Kim named explicitly to exclude — a general-purpose coding agent, not
  a bounded analysis worker. **Not ported.**
- `steward/buddhi.py` ("Discriminative Intelligence... doesn't perceive,
  doesn't store, doesn't detect. It DISCRIMINATES and DECIDES") —
  wrapped in heavy Vedic/Sankhya naming (`Antahkarana`, `Chitta`,
  `Gandha`, `ToolNamespace` mapped to `JNANA`/`PALANA`/`KSHATRA`/
  `UDDHARA`). **Names not ported** (Kim's explicit instruction). The one
  transferable *idea*, stripped of all of that: a phase transition should
  be decided from an observed, concrete signal (what actually happened),
  not a blind round counter. That idea is what `village/worker.py`'s
  `_evaluate_failure_reason()` does — reads `finish_reason` and the
  actual text, names a specific reason, and that reason (not a generic
  "try again") goes into the next repair prompt.
- `steward/cbr.py` — an actual DSP-style signal-processing chain
  (normalize → compress → limit) computing a dynamic per-call token
  budget from three weighted signals. **Not ported** — this is real
  unnecessary abstraction for a single bounded task; Agent Village's
  budget is `VillageContract.budget`, already built (PR #11/#12), and a
  fixed `DEFAULT_CALL_MAX_TOKENS` constant is all this slice needs. The
  one thing worth keeping from the *idea*: a floor/ceiling on token spend
  chosen deliberately, not left to drift to whatever the provider allows
  — already satisfied by `MAX_LLM_CALLS_PER_EXECUTION` and
  `DEFAULT_CALL_MAX_TOKENS` being named constants instead of magic
  numbers inline.
- `steward/loop/json_parser.py` (context/response processing) —
  informed the shape of `village/interpreter.py`'s tolerant-parse stage
  (strip a markdown fence, then look for a JSON object), but the actual
  code is new, not copied — steward's parser is entangled with its tool
  dispatch protocol, which this slice has no equivalent of.
- Provider normalization (`steward/provider/adapters.py`) and
  provider-failover machinery: **glanced at, not adopted** — Kim
  explicitly excluded provider failover, and Agent Village has exactly
  one provider (DeepSeek) with no failover requirement.

**Net: one small idea taken (evaluate-from-signal, not
blind-round-count), everything else in the recon list was either full
autonomy machinery (excluded per Kim) or real but unnecessary generality
for a task this small.**

## Step 0.5 — DeepSeek response fidelity, verified against primary docs

Root cause of PR #13's `INVALID_OUTPUT`, confirmed directly against
`https://api-docs.deepseek.com/api/create-chat-completion`,
`https://api-docs.deepseek.com/quick_start/pricing/`, and
`https://api-docs.deepseek.com/guides/thinking_mode` (not inferred, not
taken from a secondary source):

- `deepseek-v4-flash` **defaults to thinking mode enabled**.
- In thinking mode, reasoning goes into `message.reasoning_content`, a
  field **separate** from `message.content`.
- `finish_reason` can be `stop | length | content_filter | tool_calls |
  insufficient_system_resource`; PR #13's run almost certainly hit
  `length` with the model still mid-reasoning.
- `usage.completion_tokens_details.reasoning_tokens` exists specifically
  to let a caller tell "spent the whole budget thinking" apart from
  "wrote a long answer."
- Disabling thinking mode is a request-body field:
  `{"thinking": {"type": "disabled"}}`.

Two fixes applied in `village/deepseek_provider.py`:
1. Thinking mode is **disabled by default** (Proof 1's task is shallow
   structural analysis, not a task thinking mode is for) — configurable
   (`thinking_enabled=True`) for a future task that needs it.
2. `reasoning_content` and `finish_reason` are read and surfaced
   regardless, defensively, even with thinking off — the module never
   again assumes empty `content` means an empty answer without checking.

## Architecture

### 1. `village/cognitive_provider.py` — `CognitiveResponse`

No JSON-shape assumption at this layer (that moved to
`village/interpreter.py`). A provider hands back exactly what the model
said: `visible_text`, `reasoning_text` (`None` if absent), `finish_reason`,
full `ProviderUsage` (now including `reasoning_tokens`, a sub-count of
`completion_tokens`, not additional to it).

### 2. `village/deepseek_provider.py`

Full-fidelity parsing of the raw response (§0.5 above). Thinking mode
off by default. Same error hierarchy as PR #13, unchanged.

### 3. `village/interpreter.py` — three ordered, cheapest-first stages

- **(a) `extract_marked_block()`** — deterministic, no LLM: looks for an
  explicit `===RESULT_BEGIN===`/`===RESULT_END===` pair (the model is
  instructed to use these in `village/worker.py::build_prompt()`) and
  parses/validates the JSON inside.
- **(b) `tolerant_parse()`** — deterministic, no LLM: tries the whole
  text as JSON, then scans for the first balanced `{...}` span anywhere
  that validates against the required shape. For when the model produced
  the right content without the exact markers.
- **(c) `build_interpretation_prompt()`** — constructs (does not itself
  call) a prompt for a second LLM call whose *sole* job is reformatting
  an already-produced answer. The no-new-analysis constraint is written
  into the prompt text itself, twice, not just documented in a comment:
  *"Do NOT perform any new analysis. Do NOT add any gap, fact, or file
  reference that is not already present, in substance, in the prior
  answer below."* — `tests/test_interpreter.py::
  test_interpretation_prompt_forbids_new_analysis_explicitly` asserts
  this text is actually in the constructed prompt, not just believed to
  be.

### 4. `village/worker.py` — the bounded Agent Loop

`GENERATE → INTERPRET → EVALUATE → optional REPAIR (capped) → FINISHED`.

- **GENERATE**: one provider call.
- **INTERPRET**: stage (a), then (b). If both fail AND the response had
  *substantive* content (non-empty, `finish_reason != "length"`) AND the
  single interpretation-call budget hasn't been spent yet this
  execution, spend it (stage c) and re-run tolerant parsing on its
  output. **Design correction made during testing** (see below):
  empty/truncated responses skip the interpretation call entirely and go
  straight to repair — there's nothing substantive to reformat, and
  spending the one interpretation-call slot there would waste it.
- **EVALUATE**: if parsed output exists, `SUCCEEDED`. Otherwise,
  `_evaluate_failure_reason()` names one of exactly three reasons —
  `truncated_output` (finish_reason == length), `empty_response` (no
  text at all), or a specific interpretation-stage error string — fed
  verbatim into the next repair prompt.
- **REPAIR**: `build_repair_prompt()` restates the original task PLUS an
  explicit `RETRY NOTICE` naming the concrete reason and three concrete
  correctness rules (include the markers, keep reasoning concise, ensure
  valid JSON). Never a blind repeat.
- **FINISHED**: `SUCCEEDED`, `INVALID_OUTPUT` (repairs exhausted or call
  cap reached), `BUDGET_EXCEEDED`, `PROVIDER_ERROR`, or `FAILED`
  (contract not ACTIVE).

**Hard cap, named constants:**
```python
MAX_REPAIR_ATTEMPTS = 2
MAX_LLM_CALLS_PER_EXECUTION = 1 + MAX_REPAIR_ATTEMPTS + 1  # == 4
```
1 GENERATE + up to 2 REPAIR-regenerates + up to 1 interpretation-only
call. `tests/test_worker.py::
test_repair_ceiling_stops_the_execution_deterministically` proves the
loop terminates in exactly 4 calls with `INVALID_OUTPUT`, not a hang or
an exception, when every response is unusable.

### 5. Budget — cumulative across all calls in an execution

`village/worker.py`'s internal `call_provider()` helper calls
`contract.record_usage()` and `contract.check_budget()` after **every**
provider call, not just the last one — `tests/test_worker.py::
test_multiple_calls_cumulate_against_the_same_budget` proves two calls'
usage sums correctly against one `VillageContract`. A budget breach mid-
loop stops the execution immediately (`BUDGET_EXCEEDED`), never
attempting a further call. `DEFAULT_CALL_MAX_TOKENS = 4096` — realistic
for a non-thinking structural answer, not knapped at the wall PR #13 hit,
but still a deliberately bounded default, not "whatever the provider
allows."

### 6. `SUCCEEDED` requires real content

Unchanged in principle from PR #13, strengthened in practice: `EVALUATE`
only accepts a `parsed` result that passed `_validate_structure()` (has
a `gaps` list, each entry has `description`+`file`). A technically
successful HTTP call with no extractable content is `INVALID_OUTPUT`,
never `SUCCEEDED` — same as before, but now the loop gets a bounded
number of real chances to produce that content before giving up, instead
of judging a single attempt in isolation.

## A design correction found while writing the tests (not a fix, evidence of the process working)

Writing `test_truncated_response_triggers_repair_then_succeeds` first
failed a real assertion: with the initial logic, an empty/truncated
response would still trigger the interpretation call (spending it on
nothing) before falling through to repair, silently consuming the
execution's one interpretation-call budget on a case it could never help
with. Fixed in `village/worker.py` by gating the interpretation call on
`candidate_is_substantive = bool(candidate_text.strip()) and
response.finish_reason != "length"` — the interpretation call is now
reserved specifically for "real content, wrong/missing structure," the
one case where a pure reformat actually helps. Documented here rather
than silently corrected, per this project's established practice of not
hiding a caught-in-testing mistake.

## The seven report points (carried forward from PR #13's format)

### 1) Boundary: Contract / Worker / Cognitive Provider / Review

Unchanged in kind from PR #13, extended to cover the new interpretation
call site: `VillageContract` — governance data only. `CognitiveProvider`
— neutral I/O, no JSON assumption now (moved to `interpreter.py`).
`village/worker.py` — the only module orchestrating calls against a
budget; structurally forbidden from write authority, now verified via
AST inspection of **both** `worker.py` and `interpreter.py` (the second
call site v2 introduces). Review still does not exist — a `WorkResult`,
however many calls it took to produce, is still only submitted evidence.

### 2) Identity of this execution

Unchanged: `execution_id` + `contract_id` → `work_result_id`. No new
identity concept for a "worker agent" — still stateless, still not a
village participant with a `pokedex.json` entry.

### 3) What exactly constitutes the work result

Unchanged schema (`village/work_result.py` untouched by this slice).
`evidence` now additionally carries a `phase_log` — the sequence of
GENERATE/INTERPRET/EVALUATE/REPAIR phase transitions with their concrete
reasons, so a reviewer can see *how many attempts* and *why*, not just
the final outcome. `usage` is now the sum across every call in the
execution, not one call's numbers.

### 4) What evidence a later reviewer can check

Same artifact shape as PR #13 (`worker_proof_evidence.json`,
`workflow_dispatch`-only, 7-day retention), now additionally showing the
full `phase_log` inside `work_result.evidence` — a reviewer can see
whether the loop succeeded on the first try, needed a repair, or needed
the interpretation call, and exactly what reason drove each transition.

### 5) Rights the worker provably did NOT have

Everything from PR #13, unchanged and re-verified for the new module:
no `.fulfill(`/`bounty_complete(` call and no `village.heartbeat` import
in **either** `worker.py` or `interpreter.py` (AST-checked, both
modules); no `subprocess`/`os.system`/`eval(`/`exec(`/`__import__` in
either module; CI-level `permissions: contents: read`; `workflow_dispatch`
only; only `DEEPSEEK_API_KEY` ever referenced; `scripts/worker_proof_01.py`'s
Contract still in-memory only. **New this slice**: `MAX_LLM_CALLS_PER_EXECUTION`
caps total spend-authority per execution at a fixed 4 calls, regardless
of how much budget headroom the contract has — "budget allows it" is
explicitly not sufficient license for unbounded iteration.

### 6) How budget and secret were secured

Budget: real, cumulative usage checked after every call, not just once
at the end — a mid-loop breach stops the execution before any further
spend. Secret: identical guarantee as PR #13, re-verified for the new
call sites — `tests/test_deepseek_provider.py::
test_api_key_never_appears_in_any_raised_exception` now scripts four
separate error scenarios across what would be multiple call sites in a
real execution (GENERATE, repair-regenerate, interpretation call), not
just one.

### 7) Smallest sensible next step

Unchanged from PR #13's answer, still not started: a manual review step
that reads a `SUCCEEDED` `WorkResult`, lets a human judge it, and only
then, separately from any worker code, sets `SuccessCriterion.met = True`
and calls `contract.fulfill()`.

## Test result

```
$ python3 -m pytest tests/ -q
........................................................................ [ 33%]
........................................................................ [ 66%]
........................................................................ [ 99%]
..                                                                       [100%]
218 passed in 2.15s
```
172 existing (146 pre-PR#13 + 46 from PR #13, since fully rewritten for
the v2 shapes where the module changed) + new coverage across
`test_interpreter.py` (12), an expanded `test_worker.py` (20, up from
17), an expanded `test_deepseek_provider.py` (17, up from 12), and two
new tests in `test_worker_no_write_authority.py`. No real API calls
anywhere in the suite. No regressions in `test_work_result.py` or
`test_worker_proof_script.py` (both unaffected by this slice's changes).
