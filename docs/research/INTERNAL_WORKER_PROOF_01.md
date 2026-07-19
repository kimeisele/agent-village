# Internal Worker Proof 01 — first real LLM execution against a VillageContract

Status: security-critical slice. Code is merged; the workflow
(`.github/workflows/worker-proof-01.yml`) is **not activated** —
`workflow_dispatch` only, and `DEEPSEEK_API_KEY` is not yet a repo
secret. Both the workflow trigger and the secret are separate, explicit,
later decisions Kim makes — merging this PR does not turn anything on.

## Step 0 — Model verification (done before writing any HTTP call)

Verified directly against DeepSeek's own docs, 2026-07-19 (not taken
from a secondary source):

- `https://api-docs.deepseek.com/quick_start/pricing/`: two current
  models, `deepseek-v4-flash` and `deepseek-v4-pro`. Legacy names
  `deepseek-chat`/`deepseek-reasoner` (still used by
  `village/moltbook_captcha.py::_deepseek_solve()`, unrelated/out of
  scope for this slice) are deprecated **2026-07-24 15:59 UTC** — 5 days
  after this document was written.
- `https://api-docs.deepseek.com/updates/`: confirms the same
  deprecation date and the two new model names, both reachable via the
  same OpenAI-compatible ChatCompletions endpoint already used by the
  existing captcha code, so no request-shape change was needed.

`deepseek-v4-flash` chosen deliberately: cheaper of the two current
models, appropriate for Proof 1's shallow structural-analysis task (no
deep reasoning needed), and — critically — not one of the names being
deprecated 5 days from now. Configurable via `DEEPSEEK_MODEL` env var/
workflow input, not hardcoded.

## 1) Boundary between Contract, Worker, Cognitive Provider, Review

- **`village/contracts.py::VillageContract`** — governance data only:
  budget, deadline, allowed resources, success criteria, state
  (`drafted/active/fulfilled/violated/expired/terminated/failed`). Owns
  no execution logic. `record_usage()` is its only mutation surface a
  worker touches.
- **`village/cognitive_provider.py`** — the neutral I/O boundary to any
  LLM/cognition backend. Knows nothing about contracts, bounties, or
  budgets. One method: `complete(prompt, max_tokens, timeout_seconds) ->
  ProviderResponse`. `village/deepseek_provider.py` is the one concrete
  implementation.
- **`village/worker.py`** — the only module that knows about both a
  Contract and a Provider. Orchestrates exactly one bounded call,
  checks real usage against the contract's real budget, validates the
  provider's output structurally (never its quality), produces a
  `WorkResult`. **Structurally forbidden from write authority**: does
  not import `village.heartbeat`, never calls `.fulfill(` or
  `bounty_complete(` — enforced by
  `tests/test_worker_no_write_authority.py`, which parses the module's
  own AST (not a substring grep, which its own explanatory docstrings
  would false-positive) to prove neither call exists in the code at all.
- **Review** — does not exist yet, deliberately. A `WorkResult` is
  submitted/review-pending evidence; nothing in this slice reads it and
  decides the contract is fulfilled. That decision stays a human's (or,
  later, an explicitly separate, still-undesigned review step) — never
  automatic, never inside the worker.

## 2) Identity of this internal agent/execution

No new agent identity concept was introduced. An execution is identified
by `execution_id` (a UUID, or `"worker-proof-01"` for this specific
proof run) plus the `contract_id` it ran against — together forming
`work_result_id = f"workresult:{contract_id}:{execution_id}"`. There is
no persistent "worker agent" registered anywhere (not in `pokedex.json`,
not anywhere else) — this is a stateless, one-shot execution, not a
new kind of village participant. Whether/how an internal executor
should ever get a stable identity comparable to an external agent's
`actor_id` (SPEC.md §C.1) is an open question, not decided here.

## 3) What exactly constitutes the work result

A `WorkResult` (`village/work_result.py`) is:
- **Identity**: `work_result_id`, `contract_id`, `execution_id`.
- **Provenance**: `provider`, `model` (neutral strings, e.g.
  `"deepseek"`/`"deepseek-v4-flash"` — no provider-specific fields
  elsewhere in the schema).
- **Outcome**: `status` (`succeeded | failed | budget_exceeded |
  invalid_output | provider_error`), `output` (the parsed, structurally-
  valid JSON the model produced — `None` unless `status == succeeded`),
  `error` (a short message on any non-success status).
- **Accounting**: `usage` (prompt/completion/total tokens, cost_usd,
  duration_seconds — the *real* numbers from the API response, not an
  estimate).
- **Evidence**: `evidence` (what was asked — target file, instruction;
  on `invalid_output`, a bounded excerpt of the raw response for
  debugging).
- **Timing**: `started_at`/`finished_at`, both UTC-aware (via
  `village.contracts.normalize_datetime` — the same fix used everywhere
  else in this codebase for the naive/aware datetime bug class).

It is explicitly **not**: a claim that the analysis is *good*, a
contract fulfillment, or a bounty completion. The model never
authoritatively judges its own success — `village/worker.py`'s
`_validate_output()` checks only that the output has the requested JSON
shape (`{"gaps": [...]}` with required sub-fields), never whether the
gaps found are real, useful, or complete.

## 4) What evidence a later reviewer can check

The workflow's uploaded artifact (`worker_proof_evidence.json`, 7-day
retention, non-secret) contains: the normalized work order (target file,
instruction), `contract_id`/`execution_id`, provider/model, the budget
decision (limits + which dimensions were exceeded, if any), real usage
numbers, the full validated `WorkResult` (including `output` on success
or a bounded raw-response excerpt on `invalid_output`), and the error
class on any failure. A reviewer can reconstruct exactly what was asked,
what came back, what it cost, and why it was accepted or rejected —
without needing to trust a prose summary of the run.

## 5) Rights the worker provably did NOT have

- **No contract/bounty fulfillment authority** — `tests/
  test_worker_no_write_authority.py` proves (via AST inspection, not a
  behavioral test that could pass by luck) that `village/worker.py`
  contains no call to `.fulfill(` or `bounty_complete(`, and does not
  import `village.heartbeat` at all.
- **No shell/process execution** — same test file asserts the module's
  source contains none of `subprocess`, `os.system`, `eval(`, `exec(`,
  `__import__`. Nothing the model returns is ever executed; it is parsed
  as JSON and checked structurally, nothing more.
- **No repository write access at the CI level** — `.github/workflows/
  worker-proof-01.yml` declares `permissions: contents: read`. Even if
  every application-level guard above were somehow bypassed, the
  workflow's own GitHub token cannot push, commit, or open a PR.
- **No trigger reachable by external/untrusted content** —
  `workflow_dispatch` only. No `push`, no `pull_request`/
  `pull_request_target`, no `schedule`, nothing an issue comment or a
  fork PR could ever invoke.
- **No access to any secret beyond `DEEPSEEK_API_KEY`** — the workflow's
  `env:` block only ever passes that one secret through; no other
  `secrets.*` reference exists in the file.
- **No persistent state mutation** — `scripts/worker_proof_01.py`
  constructs its `VillageContract` **in-memory only**; it is never
  loaded from or saved to `data/village/contracts.json`. Running this
  proof, even repeatedly, cannot mutate any real bounty or contract on
  disk.

## 6) How budget and secret were secured

**Budget:** `village/worker.py::run_work_order()` calls
`contract.record_usage()` with the **real** usage numbers from the
provider response (not an estimate) immediately after the call, then
`contract.check_budget()` before accepting anything as a success. A
budget-exceeded result rejects even a structurally perfect output
(`tests/test_worker.py::test_budget_exceeded_rejects_even_a_
structurally_valid_result`) — real spend caps what's accepted, not the
other way around. Exactly one provider call is made per execution
(`provider.calls == 1`, asserted in tests); no retry-on-content exists,
and a transient-error retry was considered and deliberately **not**
implemented in this proof — the explicit "max. 1 API-Aufruf" constraint
takes precedence, at the cost of resilience a future slice might add
back with its own budget accounting for the retry itself.

**Secret:** `DEEPSEEK_API_KEY` is read once, in
`village/deepseek_provider.py::DeepSeekProvider.__init__()`, from the
environment (or an explicit constructor arg, used only by tests with a
fake value). It appears in exactly one place at runtime: the outgoing
request's `Authorization` header. Every error path was checked (`tests/
test_deepseek_provider.py::test_api_key_never_appears_in_any_raised_
exception`, `..._in_the_provider_response_object`) to confirm it never
leaks into an exception message, a log line, or anything persisted as
evidence — including the HTTPError body-parsing path, which only ever
surfaces DeepSeek's own structured `error.message` field, never the raw
response body verbatim. A missing key raises `ProviderAuthError`
*before* any network call is attempted (`tests/test_deepseek_provider.py
::test_missing_api_key_raises_auth_error_without_calling_network`) —
never a fake success, never a silent no-op.

## 7) Smallest sensible next step after a successful proof

Not proposed as a slice to execute — a candidate, for Kim to decide on
separately, after reviewing an actual successful run's evidence
artifact:

A **manual review step** that reads a `SUCCEEDED` `WorkResult`'s
`output`, lets a human decide whether it's actually good, and — only
then, only by explicit human action — sets the relevant
`SuccessCriterion.met = True` on the contract (the field this whole
schema was built to support, per PR #12) and calls `contract.fulfill()`
separately from any worker code. That closes the loop this proof
deliberately leaves open, without ever letting the model's own output
be self-authorizing.

## What this proof deliberately does not do

Per the hard limits in the task and unchanged from every prior slice in
this thread: no shell execution of model output, no repository writes,
no autonomous follow-up work, no reputation-tier change, no automatic
`bounty_complete()`/`fulfill()`, no access to any secret beyond
`DEEPSEEK_API_KEY`, and — new for this slice — no execution reachable by
anything other than a human explicitly running `workflow_dispatch`.
