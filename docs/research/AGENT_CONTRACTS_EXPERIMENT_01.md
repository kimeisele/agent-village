# Experiment 01 — `ai-agent-contracts` for Gap 3 (bounty governance)

Status: **isolated evaluation experiment, not production code.** This
approval covers a standalone experiment only, not adoption as a
production dependency. No file outside `experiments/agent_contracts_01/`
was changed to produce this experiment. Follows up on
`docs/research/CAPABILITY_SURVEY_01.md`'s shortlist item 1.

**Hard limits respected:** no fork, no contact with
`flyersworder/agent-contracts` or its maintainer, no code copied from
that repo into this one (only `pip install`ed into a local, gitignored
venv), no production wiring, no slice beyond this experiment.

---

## Step 1 — Re-verification (not trusted from CAPABILITY_SURVEY_01)

CAPABILITY_SURVEY_01 was written 2026-07-19 against the GitHub repo only.
Re-checked here, independently, against what actually installs:

- **PyPI package:** `ai-agent-contracts`, queried directly via
  `https://pypi.org/pypi/ai-agent-contracts/json`.
- **Installed/tested version:** `0.3.2` (latest on PyPI at time of
  writing; full release history: `0.1.0, 0.2.0, 0.3.0, 0.3.1, 0.3.2`).
- **License (PyPI metadata):** `Apache-2.0`.
- **PyPI → GitHub linkage:** `project_urls.Repository` =
  `https://github.com/flyersworder/agent-contracts`, confirmed matching
  the candidate from `CAPABILITY_SURVEY_01`.
- **Exact source commit for the installed version:** GitHub tag `v0.3.2`
  resolves to commit `273d9e02e136759445ea18e2fda94ffd609f0221`
  (`gh api repos/flyersworder/agent-contracts/tags`), published
  2026-05-20 — confirmed by reading `pyproject.toml` **at that exact
  tag** (`gh api .../contents/pyproject.toml?ref=v0.3.2`): `version =
  "0.3.2"`, `license = { text = "Apache-2.0" }`. `LICENSE` at the same
  ref confirmed as the genuine Apache 2.0 text.
- **Provenance note:** the GitHub repo's `main` branch has moved on
  since (100+ commits, last push 2026-07-18 per CAPABILITY_SURVEY_01) —
  the installed/tested package is ~2 months behind the repo's HEAD.
  Everything in this experiment is tested against **v0.3.2 exactly**, not
  current `main`.
- **Only real runtime dependency:** `python-dotenv` (checked via
  `pip show ai-agent-contracts`). No LangChain/LiteLLM hard dependency —
  those are optional extras, gated behind `LANGCHAIN_AVAILABLE`/
  `LITELLM_AVAILABLE` flags in the package's own `__init__.py`.
- **Python requirement:** `>=3.12`. This repo's own interpreter is 3.11
  (confirmed: `python3 --version` → `3.11.13`), so the experiment venv
  runs on a separately-available Python 3.13 interpreter, isolated to
  `experiments/agent_contracts_01/.venv/` (gitignored, not committed).

---

## Step 2 — Isolation

- New directory `experiments/agent_contracts_01/` only.
- `village_core.py`, `heartbeat.py`, `brain.py`, `moltbook_captcha.py`,
  `.github/workflows/*` — **zero changes** (confirmed: `git diff --stat`
  against `main` for this PR touches only files under `experiments/` and
  this doc).
- No central `requirements.txt`/`pyproject.toml` existed in the repo
  before this experiment (verified: `ls` at repo root shows neither) and
  none was added centrally — `experiments/agent_contracts_01/
  requirements.txt` is scoped to the experiment folder only.
- No new GitHub Actions workflow added. All test output below is from a
  **local** `pytest` run, pasted verbatim, not a CI badge/claim.

---

## Step 3 — Modeled use case

The real `b001` bounty from `data/village/bounties.json`
(`village/heartbeat.py::bounty_create/claim/complete()` shape:
`id/title/description/reward/status/claimed_by/claimed_at/completed_at`),
modeled as an `agent_contracts.Contract` with:

- a unique task (`id="b001"`, title/description copied verbatim),
- allowed resources (`Capabilities(tools=["Read", "Grep"])` — b001 is a
  *review* bounty, so write/execute tools are deliberately excluded),
- a token/API-call/cost budget (`ResourceConstraints`),
- a hard deadline (`TemporalConstraints`),
- one required, checkable success criterion,
- one explicit termination condition.

See `experiments/agent_contracts_01/contract_experiment.py`.

---

## Step 4 — Test cases and real output

All 6 required cases, plus one exploratory case (multi-agent delegation,
informs step 5). Run locally, **verbatim** output below (not
paraphrased, not "trust me it passed"):

```
$ ./.venv/bin/python -m pytest tests/ -v
============================= test session starts ==============================
platform darwin -- Python 3.13.3, pytest-9.1.1, pluggy-1.6.0 -- .../experiments/agent_contracts_01/.venv/bin/python
cachedir: .pytest_cache
rootdir: /Users/ss/dev/kimeisele/agent-village/experiments/agent_contracts_01
collecting ... collected 16 items

tests/test_agent_contracts_experiment.py::test_valid_contract_is_accepted PASSED [  6%]
tests/test_agent_contracts_experiment.py::test_budget_overrun_is_detected PASSED [ 12%]
tests/test_agent_contracts_experiment.py::test_disallowed_resource_is_rejected PASSED [ 18%]
tests/test_agent_contracts_experiment.py::test_deadline_overrun_raises_on_timezone_aware_deadline PASSED [ 25%]
tests/test_agent_contracts_experiment.py::test_deadline_overrun_is_detected_with_naive_datetime_workaround PASSED [ 31%]
tests/test_agent_contracts_experiment.py::test_deadline_not_yet_overrun_when_future_with_naive_datetime_workaround PASSED [ 37%]
tests/test_agent_contracts_experiment.py::test_success_criterion_is_deterministically_checkable PASSED [ 43%]
tests/test_agent_contracts_experiment.py::test_library_error_on_invalid_input_is_caught_cleanly PASSED [ 50%]
tests/test_agent_contracts_experiment.py::test_multi_agent_delegation_conservation_exists PASSED [ 56%]
tests/test_stdlib_baseline.py::test_valid_contract_is_accepted PASSED    [ 62%]
tests/test_stdlib_baseline.py::test_budget_overrun_is_detected PASSED    [ 68%]
tests/test_stdlib_baseline.py::test_disallowed_resource_is_rejected PASSED [ 75%]
tests/test_stdlib_baseline.py::test_deadline_overrun_is_detected PASSED  [ 81%]
tests/test_stdlib_baseline.py::test_deadline_not_yet_overrun_when_future PASSED [ 87%]
tests/test_stdlib_baseline.py::test_success_criterion_is_deterministically_checkable PASSED [ 93%]
tests/test_stdlib_baseline.py::test_library_error_on_invalid_input_is_caught_cleanly PASSED [100%]

============================== 16 passed in 0.22s ===============================
```

### A real bug found while writing these tests (not a synthetic error case)

Building the contract with a **timezone-aware** deadline — the correct
modern Python practice, and what a village integration would naturally
do — makes `TemporalMonitor.is_past_deadline()` raise:

```
TypeError: can't compare offset-naive and offset-aware datetimes
```

Root cause, confirmed by reading
`agent_contracts/core/monitor.py` directly (not guessed): `TemporalMonitor`
compares the deadline against a **naive** `datetime.now()` at every call
site (`is_past_deadline`, `get_remaining_seconds`, `is_over_duration`,
elapsed-time tracking — at least 6 separate call sites). The type hint on
`TemporalConstraints.deadline` accepts any `datetime`, tz-aware included,
but the monitor code was never updated to match. Confirmed reproducible
(`test_deadline_overrun_raises_on_timezone_aware_deadline`) and confirmed
that a naive-datetime workaround avoids it
(`test_deadline_overrun_is_detected_with_naive_datetime_workaround`).
This is a real defect in the pinned 0.3.2 release, not a usage mistake on
our side — worth being aware of before depending on this library for
anything deadline-related, and worth checking whether it's fixed on a
later, unreleased `main` commit before ever revisiting this decision.

Also found: `Contract.violate()` raises `ValueError` unless the contract
has already been activated via `enforcer.start()` — undocumented in the
README's quick-start example, only found by actually running the first
version of the budget-overrun test and watching it crash instead of
recording a violation.

---

## Step 5 — Comparison with a minimal stdlib-only baseline

`stdlib_baseline.py`: **78 lines**, zero external dependencies, covers
the identical 6 test cases (`tests/test_stdlib_baseline.py`, 70 lines).
`contract_experiment.py`: **140 lines** (including extensive comments
explaining library quirks) wrapping `ai-agent-contracts`.

| Question | Answer |
|---|---|
| **Real functionality saved?** | Yes, for the parts we didn't build ourselves: `ResourceMonitor` (percentage/remaining-budget helpers, per-tool granular limits, two token-accounting modes), an event/callback/hook system for enforcement, and — the one clearly substantial piece — a working multi-agent **budget conservation** subsystem (`ConservationViolationError`, `ResourceAllocation`, `plan_resource_allocation`, `prioritize_tasks`) that would be real, non-trivial work to build ourselves if Agent Village ever needs it. For the *single-bounty, single-agent* case tested here, the saved functionality is modest — our 78-line baseline covers the same 6 cases. |
| **Complexity imported?** | Significant. The library's actual design center is wrapping **live LLM calls** (`ContractedLLM`, `ContractedChain`, LangChain/LiteLLM integrations, prompt-budget generation, strategy recommendation) — a much larger problem than "track a budget and a deadline for a bounty." We'd be depending on a large, actively-evolving surface to use a small fraction of it. |
| **How strongly does the API couple us?** | Moderately. `Contract`/`ResourceConstraints`/`TemporalConstraints`/`SuccessCriterion` would appear directly in our data model if adopted. The package is self-described `Development Status :: 4 - Beta` (pre-1.0) — API stability is not guaranteed, and we found a real bug in the current release, which is a live signal of exactly that immaturity. |
| **Multi-agent work orders, or single-agent-focused?** | Genuinely multi-agent-capable — confirmed via direct API inspection (`ConservationViolationError`/`ResourceAllocation`/`DelegationSummary`/`plan_resource_allocation` all exist specifically to prevent a parent budget being over-allocated across delegated sub-agents). Not just a single-agent budget tracker with a marketing claim; this is real, structural. |
| **Contracts serializable/versionable, later NADI/federation-transportable?** | **No, not out of the box — a real, concrete blocker.** `Contract` has no `to_dict()`/`to_json()`. `dataclasses.asdict()` fails outright (`TypeError: cannot pickle 'mappingproxy' object`, tested directly). Worse: the natural way to use `SuccessCriterion.condition`/`TerminationCondition.condition` is a Python **callable** — which is fundamentally not JSON-serializable, and NADI messages (`docs/SPEC.md` §2.3) must be JSON + Ed25519-signable. Depending on this library as-is would require designing our own serialization layer on top of it, which erodes a chunk of the "we get this for free" argument. |
| **Cognitive/CBR-style adaptive budgets expressible?** | Partially. `ResourceConstraints` has `reasoning_tokens`/`text_tokens`/`reasoning_effort` fields and the package exposes `estimate_quality_cost_time`/`recommend_strategy`/`generate_adaptive_instruction` helpers — real support for adaptive, LLM-cost-aware budgeting exists, more than we'd build ourselves quickly. This is the closest the library comes to matching Gap 3 *and* touching `VALUE_MODEL.md`'s "adaptive per-step cost/trust/complexity profile" language for Outbound Capability Intelligence. |
| **Behavior on unknown/future contract fields?** | Not tested directly (would require constructing a Contract from a dict with an unrecognized key), but the classes are plain `@dataclass`es with fixed fields, not a schema-tolerant parser — adding a village-specific field later would most likely require subclassing or a separate side-channel (e.g. the existing `metadata: dict[str, Any]` field), not free extensibility. |

**Net read:** the library's *design* — treating resource budget, deadline,
success criteria, termination condition, and (for later) multi-agent
budget conservation as explicit, typed, first-class fields on a
contract/bounty record — is a genuinely good pattern, better structured
than what we improvised in 78 lines. But the library *itself*, today, has
three concrete costs that matter specifically for Agent Village: no
serialization path (blocks the NADI-transport requirement outright
without extra work), a real bug in exactly the deadline-handling code
this experiment needed, and a much larger dependency surface than the
single-bounty use case requires (its actual center of gravity is live-LLM
call wrapping, which Agent Village doesn't do today — no LLM calls are
made in the current production path except the isolated, already-gated
CAPTCHA solver).

---

## Decision

**ADAPT_CONCEPT.**

Not `ADOPT_DEPENDENCY`: the concrete blockers (no JSON serialization —
directly conflicts with the NADI-transport requirement in `SPEC.md`
§2.3 — plus a real bug found in the pinned release, plus pre-1.0 API
instability) are not hypothetical risks, they're things this experiment
actually hit while building the smallest possible use case. Depending on
it in production today would mean carrying that risk for a governance
mechanism that `SPEC.md` §A.5 requires to be deterministic and
trustworthy — the opposite of "depend on an actively-changing beta
library with a known live bug in the exact code path we'd use."

Not `REJECT` either: the underlying *design* — explicit typed
`resources`/`temporal`/`success_criteria`/`termination_conditions`/
`capabilities` fields, and the multi-agent budget-conservation concept —
is worth adapting into Agent Village's own future bounty/Contribution
governance model (`SPEC.md` §D "complex reputation/governance", §A.11
Marketplace), built as plain, JSON-native dataclasses (like
`stdlib_baseline.py`, not like `contract_experiment.py`), with no
external dependency, so it stays trivially serializable for NADI and
free of an external beta library's release cadence and bugs.

**What to take, explicitly:** the *shape* of a governance record —
budget fields, a typed deadline with a hard/soft distinction, a list of
named+weighted+required success criteria, an explicit termination-reason
field, a tool/resource allowlist — and, if/when Agent Village ever needs
multi-agent bounty delegation, the *concept* of conservation checking
(a delegated sub-budget must not exceed the parent's remaining budget).

**What to leave behind, explicitly:** the `ai-agent-contracts` package
itself as a runtime dependency; its LLM-call-wrapping machinery
(`ContractedLLM`/`ContractedChain`/LangChain integration), which Agent
Village has no use for; its callable-based success-criteria pattern
(replace with JSON-serializable string/data conditions from the start).

## Recommendation for next slice

Not proposed here (out of scope for this experiment) — but if Kim wants
to pursue Gap 3 further, the natural next step is a small, original
design note (or a `village_core.py` schema addition) for a
`ContributionBudget`/`BountyGovernance` shape informed by this
comparison, written from scratch as plain JSON-serializable dataclasses,
not a dependency-adoption slice.
