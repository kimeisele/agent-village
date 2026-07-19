# Capability Survey 01 — external ecosystem, no code changes

Status: **Research slice. No implementation, no code change, no fork, no
issue, no contact with any external maintainer.** All findings below are
from read-only inspection (GitHub REST API, `gh api`, public repo content)
performed 2026-07-19. Not a normative document — `docs/SPEC.md` remains
authoritative on scope; this survey only informs a possible future
experiment-slice decision.

## Method

For each of the 10 candidate repos Hermes flagged: confirmed the repo is
real (not an empty scaffold), read the actual `LICENSE` file content (not
just the GitHub UI badge), listed the core implementation directory (not
just the README), checked commit count/recency, CI workflow presence and
latest run conclusions, release history, and cross-checked README claims
against what is actually present in the repo. No repo was forked, starred,
or otherwise interacted with beyond anonymous/read-only GET requests via
the GitHub API.

---

## Step 1 — Five capability gaps (verified against current repo state)

Kim's five gaps, checked against `docs/SPEC.md`, `docs/BEFUND.md`,
`docs/ARCHITECTURE_VISION.md`, `docs/VALUE_MODEL.md`, and
`village/*.py` as they stand on `main` (7ff8a49) at time of writing.
All five confirmed accurate; two get a small correction/addition below.

1. **Discovery.** Confirmed as stated. `SPEC.md` §A.10 defines the domain
   (identify/assess/prioritize agents, repos, projects, communities);
   `VALUE_MODEL.md` names "Outbound Capability Intelligence" as a second
   value source. Grep of `village/*.py` confirms 0% code — `scan_moltbook()`
   and `scan_github()` are both reactive ingress, nothing that searches
   outward. No BEFUND entry describes an executed Discovery action before
   this survey itself (this survey is, in a loose sense, the first manual
   instance of it — worth naming explicitly, not just leaving implicit).

2. **Cross-platform cryptographic agent identity.** Confirmed. `SPEC.md`
   §C.1 documents the Moltbook `actor_id` as a name-fallback, not a real
   platform ID (`BEFUND.md` §23-Nachtrag). Correction: this gap is **not
   Moltbook-specific** — it's the general problem of "what makes an actor
   the same actor across two logins/sessions/platforms," which will
   recur for GitHub-side identity too the moment cross-repo/cross-node
   federation (NADI, §A.7) becomes real, not just for Moltbook. Framed
   narrowly as "the Moltbook actor_id problem," a future slice might
   wrongly treat a GitHub-only fix as sufficient.

3. **Post-registration governance (budgets, deadlines, success
   criteria).** Confirmed accurate as stated. `SPEC.md` §D defers "full
   Mission Factory" and "complex reputation/governance" explicitly. Code
   reality: `bounty_create/claim/complete()` in `village/heartbeat.py` is
   the entire governance surface — no budget field, no deadline field, no
   machine-checkable success criterion beyond a human-typed title/
   description string.

4. **Review/PR flow for externally contributed work.** Confirmed, and
   still true after PR #3/#4/#5 merges checked on `main`: no workflow,
   script, or template in this repo creates a PR from external input.
   `SPEC.md` v1 §3 ("No merge rights — no PR flow exists in v1 for
   external agents at all") is unchanged in substance by `SPEC.md` v2.
   The dependency Kim names (`§A.4`/`§A.9`, "GitHub is the system of
   record" / "long-term ecosystem") is correctly identified — PRs are
   listed in §A.9's ecosystem enumeration but have zero implementation.

5. **Reputation/trust progression.** Confirmed. `data/village/pokedex.json`
   entries carry `status: "observed"` permanently — grepped
   `village/heartbeat.py::derive()`, confirmed no code path ever writes
   any value other than `"observed"` to that field.
   `ARCHITECTURE_VISION.md` §5's OBSERVED→CLAIMED→VERIFIED→RESIDENT ladder
   is aspirational text only. `SPEC.md` §A.11 names
   "reputation/settlement" as an unbuilt future Marketplace component,
   consistent with this gap.

**No sixth gap added.** Five is what the evidence supports; padding the
list wasn't done just to look thorough.

---

## Step 2/3 — Ten candidates: verification and license assessment

Each entry: existence/substance check, license (file content, not just
badge), CI/test signal, and honest note on where a README claim did or
didn't hold up under a repo-content check.

### 1. `jielabsdev/BeaconNode`

- **Exists, real code.** C++20 engine (`src/beacon_engine`, `src/beacon_net`)
  + Python SDK (`src/beacon`), `tests/{cpp,python}` present. Not a stub.
- **License:** MIT (`LICENSE` file confirmed, standard MIT text).
- **Activity:** created 2026-06-29, 8 commits total, last push same day
  as creation (`2026-06-29T21:59:40Z`) — **no activity since**, 3 weeks
  dormant at time of writing. 0 stars, 0 releases, **no CI workflow at
  all** (`actions/workflows` empty).
- **Gap overlap — real, not just thematic:** README claims "Identity
  Hardening: Ed25519-signed registry entries" and "gossip-based
  decentralized discovery" — both are architecturally exactly Gap 1
  (Discovery) and Gap 2 (cross-platform identity). Not verified line-by-
  line that the crypto is implemented correctly (would require a security
  audit, out of scope here), but the code structure (proto definitions,
  `beacon-engine` signature verification path) is consistent with the
  claim, not just a README promise with empty `src/`.
- **Risk:** young (3 weeks), dormant since creation, zero CI/test
  execution evidence, heavy native toolchain (CMake, MSYS2/UCRT64,
  libsodium, boost::asio, protobuf) — a large, unproven dependency
  surface for what is currently a ~150-line Python heartbeat script.

### 2. `flyersworder/agent-contracts`

- **Exists, real code, real package.** Published on PyPI as
  `ai-agent-contracts`. `tests/{core,evaluation,integrations}` present.
- **License:** Apache-2.0 (confirmed via GitHub API `license.spdx_id`,
  consistent with README badge).
- **Activity:** 100+ commits, last push 2026-07-18 (yesterday relative to
  this survey), 5 releases, 3 CI workflows. Latest CI runs: 4 of last 5
  `success`, 1 `failure` (a dependency-bot branch, not the main CI job).
  Actively maintained.
- **Gap overlap — exact, not just adjacent:** README states its purpose
  as "Resource Budgets," "Temporal Constraints," "Success Criteria,"
  "Lifecycle Management" — this is Gap 3 named almost word-for-word.
  Code example in README shows a working `Contract`/`ContractedLLM` API
  that wraps LLM calls with token/API-call/cost budgets — matches the
  claim, not just aspirational text.
- **Risk:** low. Small, focused, permissively licensed, real package on
  PyPI (installable via `pip install ai-agent-contracts` without touching
  their source at all).

### 3. `Replikanti/agentis-colonies`

- **Exists, real code**, but README states outright: "**Agentis** is a
  **proprietary** AI-native platform for agent emergence (runtime,
  language, evolution engine, distributed infrastructure). **This repo**
  hosts the open-source federations built on that runtime." Hermes' raw
  note ("Runtime evtl. proprietär") is **confirmed, not speculative** —
  the repo's own README says so directly.
- **License:** Apache-2.0 for this repo's contents (federation configs,
  ADRs, tooling) — but those contents cannot run standalone; they require
  the closed `Replikanti/agentis` runtime, which is not published here.
- **Activity:** very active (100+ commits, 30 releases, 4 CI workflows,
  latest run same day as this survey), real CI green.
- **Gap overlap:** the README documents a "four-tier confidence ladder —
  `shadow` → `propose` → `review-gated` → `autonomous`" that agents climb
  based on "measured experience, not hand-tuned thresholds" — this is a
  concrete, already-engineered design for Gap 5 (reputation/trust
  progression), directly comparable to our own dormant OBSERVED→CLAIMED→
  VERIFIED→RESIDENT ladder in `ARCHITECTURE_VISION.md` §5.
- **Risk:** the actual runtime is closed-source — nothing here is
  DEPEND/VENDOR-able. Only the documented *pattern* (the ladder design,
  described in `doc/adr/ADR-0001-confidence-tiers.md`) is usable, and
  only as a design reference, not as code.

### 4. `8dp6brm9hp-svg/agentlink`

- **Exists but minimal.** 1 commit total, repo size 29 KB, created and
  last pushed the same 20 seconds
  (`2026-06-07T04:28:34Z`→`2026-06-07T04:28:54Z`) — essentially a single
  initial commit, never iterated on since (6 weeks dormant).
  `mcp-server/` has only 2 subdirectories (`data`, `src`), contents not
  further inspected once the single-commit/no-iteration signal was clear.
- **License:** MIT (confirmed).
- **Gap overlap:** thematically Discovery (Gap 1) — "decentralized...
  registry and discovery protocol for MCP tools." But architecturally
  it's an **on-chain (Base blockchain) + IPFS** design with "no payments,
  no wallets" claimed in prose while the core mechanism (`publish_tool`
  returns a `tx_hash`) is inherently wallet/transaction-based — a mismatch
  with this project's no-token-economy stance (`SPEC.md` §D explicitly
  defers "token economy").
- **Verdict:** too immature (1 commit, 6 weeks untouched) to evaluate
  further, and the on-chain mechanism doesn't fit our architecture even
  if it matured. Documented and set aside, not sped past.

### 5. `Matrixx0070/sudo-ai`

- **Exists, substantial, real code** (116,767 size units, 100+ commits,
  very active — last push during this survey itself).
- **License:** MIT (confirmed).
- **CI: currently failing.** All 5 most recent workflow runs at time of
  check are `failure`, including two runs from the same minute as this
  survey. Not a transient blip — the tip of the branch does not build
  clean right now.
- **Gap overlap — weak, not what the name suggests.** README describes a
  **single-owner, full-system-privilege** personal agent platform
  ("designed for a single trusted owner on their own machine"). Its
  "federation" and "multi-agent orchestration" claims refer to internal
  sub-agent role decomposition and multi-channel messaging (Telegram,
  Discord, Slack, ...), not cross-node/cross-repo federation comparable
  to NADI or any of our 5 gaps. No meaningful overlap found on inspection
  beyond surface vocabulary.
- **Verdict:** scope mismatch (personal full-privilege assistant, not
  village/federation infrastructure) plus currently-broken CI. Not worth
  further attention for any of the 5 gaps.

### 6. `Aureliolo/synthorg`

- **Exists, large, serious engineering effort.** 57,845 size units,
  100+ commits, 30 releases, 30 CI workflow definitions (mostly
  `success`/`skipped`, one `Docker` job with `null` conclusion — pending/
  in-progress at check time, not a failure). OpenSSF Scorecard, SLSA
  level 3, Codecov, CodSpeed badges present and linked to real accounts.
  README claims "40,000+ tests, 80%+ coverage" for the tested subset,
  explicitly marked pre-alpha for the rest.
- **License:** Business Source License 1.1 (confirmed via `LICENSE` file,
  not just the badge) — **not OSI open source**. Converts to Apache 2.0
  on **2029-07-08** (Change Date, read directly from the license
  parameters block). Production use is permitted only for organizations
  under 500 employees/contractors that don't offer it as a hosted/managed
  competing service — Agent Village would qualify today, but this is a
  license condition to track, not a permanent Apache grant.
- **Gap overlap:** README explicitly targets "roles, departments,
  hierarchies, persistent memory, **budgets, governance**, structured
  communication" — direct match to Gap 3, partial match to Gap 5
  (hierarchies/roles as a reputation-adjacent structure).
- **Risk:** explicitly pre-alpha by its own README, BSL production
  conditions, Python 3.14+ requirement (bleeding edge), and a scope far
  larger than anything Agent Village needs today (a full autonomous
  "product studio," not a bounty-budget mechanism). High engineering
  quality, wrong size for adoption right now.

### 7. `EricLortie/laborforge-release`

- **Exists**, but the `NOTICE` file states outright: *"This is being
  released as an art project, and should not be used for super-serious
  business purposes..."* — the author's own framing, not a Hermes
  guess.
- **License:** PolyForm Small Business License 1.0.0, explicitly
  source-available: the `LICENSE` file itself states *"This is a
  SOURCE-AVAILABLE license. It is NOT an OSI-approved open-source
  license and must not be described as 'open source.'"* Confirmed via
  file content, not inferred.
- **Activity:** 1 commit in the visible history at the `release` branch
  head (this is a `-release` mirror repo, so a shallow/squashed history
  is expected and not itself suspicious), but CI is **failing**: latest
  "Fresh install gate" run is `failure`, and an earlier plain `CI` run
  also `failure`. Combined with the author's own "released as an art
  project," "should not be used for super-serious business purposes"
  framing — self-described unmaintained/experimental status, not a
  Hermes speculation.
- **Gap overlap:** thematically Gap 3 (multi-tenant governance/
  federation platform) but the license terms (non-OSI, restricts
  commercial redistribution) and the author's own abandonment/quality
  disclaimer make this a non-starter regardless of technical content.
- **Verdict:** documented and set aside. Not worth deeper code review —
  license and maintainer-stated status alone are disqualifying for
  DEPEND/VENDOR, and self-described "a mess" undercuts LEARN value too.

### 8. `kalai-labs/MAGENTRA`

- **Exists, real code**, TypeScript, npm-workspace monorepo
  (`engine/{core,host,protocol,providers,tools}`, `app/` Electron
  desktop shell). Very new: created 2026-07-14 (5 days before this
  survey), but already 20 commits and **16 releases** — unusually fast
  release cadence for a 5-day-old repo, worth noting as a signal (could
  be a squashed/migrated history, not verified further).
- **License:** Apache-2.0 (confirmed).
- **CI:** all recent runs `success`.
- **Gap overlap — could not be verified as claimed.** The README's
  `engine/core/crew/` directory (described as "the multi-agent team:
  roster, per-member endpoints, experience, service record, cost
  ledger") was the specific piece that would matter for Gap 5
  (reputation) — **direct lookup of that path returned HTTP 404**, i.e.
  either the README's documented layout doesn't match the actual current
  tree, or the directory is named/nested differently than described.
  Not pursued further within this survey's scope; flagging the
  discrepancy honestly rather than asserting overlap I couldn't verify.
- **Verdict:** MAGENTRA is a real, actively-developed desktop coding-agent
  product (a Claude-Code-style tool with a crew of sub-agents), which is
  a different problem shape than a multi-agent *village/federation*
  reputation system. Given the unverified directory claim and the scope
  mismatch (single-user desktop tool vs. cross-agent trust), not pursued
  further.

### 9. `division-sh/swarm`

- **Exists, large, real Go codebase** (58,275 size units,
  `internal/{runtime,store,dashboard,mailbox,...}`, Go Report Card and a
  separate docs site `docs.division.sh` — both real, external signals,
  not just repo self-description).
- **License — needs a note.** GitHub API reports `license.spdx_id:
  "NOASSERTION"` despite the visible badge claiming Apache 2.0. Read the
  actual `LICENSE` file directly: it **is** standard Apache License 2.0
  boilerplate (158 lines, matches the canonical text through "END OF
  TERMS AND CONDITIONS"), but it's **missing the trailing "APPENDIX: How
  to apply..." section** that GitHub's license-detection template
  matches against — which is the most likely reason for the
  `NOASSERTION` mismatch. Practically: the license IS Apache-2.0 by its
  own text, but this is worth a direct confirmation from the maintainer
  before depending on it in anything beyond a private experiment — GitHub's
  own tooling isn't confident enough to assert it.
- **Activity:** very active — multiple CI runs on the day of this survey
  alone, all `success`. 188 open issues, which combined with the docs
  site and Go Report Card badge reads as a real, moderately-sized user
  base with active issue triage, not neglect.
- **Gap overlap:** direct match to Gap 3 — "durable state machine,"
  "meters spend" (budgets), deterministic routing, replay/audit. Partial
  match to Gap 4 — "durable mailbox" for human approvals/rejections is
  structurally similar to what a PR-review gate would need (a pending
  decision waiting for a handler).
- **Risk:** adopting it means adopting an entire external "operating
  system" (own Go binary, own YAML platform-spec DSL, own persistence
  layer) — not a library you `pip install`, a full runtime you'd run
  Agent Village *inside*. Too large an architectural commitment to adopt
  wholesale; valuable only as a design reference at this stage.

### 10. `hangang907-png/nightforge`

- **Exists, small but real code** (87 KB, `src/nightforge`, CLI with
  working subcommands per the README's own status list — not vaporware
  prose: `validate`, `claim`, `submit`, `webhook`, `github-list`,
  `github-claim`, `github-draft`, `publish`, `webhook-state`, all listed
  under a "현재 이터레이션" (current iteration) status section with concrete
  claims like "HMAC-SHA256 상수시간 검증" (constant-time HMAC
  verification) and "delivery ID 원자적 멱등 처리" (atomic idempotent
  delivery-ID handling) — specific engineering claims, not marketing
  copy).
- **License: none.** No `LICENSE` file in the repo at all — confirmed by
  listing the root tree, not just checking the API's `license` field.
  **This means default copyright applies: all rights reserved.** Nothing
  here can legally be copied, forked, adapted, or vendored without asking
  the author — which the task's hard limits explicitly forbid doing.
- **Activity:** 13 commits, all within roughly a 12-hour window on
  2026-07-11, then **zero activity for 8 days** up to this survey.
  0 stars. One CI workflow (`proof-engine`), all runs `success`, but the
  last run is also from 2026-07-11 — dormant, not actively broken.
- **Gap overlap — the closest match of all ten candidates to Gap 4.**
  The documented design is a GitHub-native contribution pipeline: RFC →
  reproduce → vote+resource-commitment → ticket → competitive
  development → staged merge (`docs/01-governance.md`, per the README's
  own doc index), implemented as a ticket state machine
  (`open → claimed → submitted → verifying → accepted/rejected`) driven
  entirely by GitHub Issue labels, PATCH-based claiming (assignee +
  label), **Draft PR submission enforced via the GitHub API**, and
  `check_suite` webhook events converting `verifying → accepted/rejected`
  automatically. This is structurally almost exactly the shape of what
  `SPEC.md` §D defers as "automatic PR generation" for Gap 4 — closer
  than anything else surveyed, including the much larger/better-funded
  candidates.
- **Verdict:** legally unusable for DEPEND/VENDOR/fork (no license, and
  the task's hard limits forbid contacting the author to ask for one).
  High value as a **pattern to study**, not as code to take. Dormant
  8 days at time of writing — no signal on whether it will be
  maintained or ever gets a license added.

---

## Step 4 — Categorization

| # | Candidate | Category | Why |
|---|---|---|---|
| 1 | BeaconNode | **LEARN** | Real Ed25519-identity + gossip-discovery pattern (Gap 1+2), but 3 weeks old, dormant since creation, no CI, heavy native dependency stack — too unproven for DEPEND/INTEROPERATE yet. |
| 2 | agent-contracts | **DEPEND** | Exact match to Gap 3, real PyPI package, Apache-2.0, active CI, low integration cost (`pip install`, no source copied). |
| 3 | agentis-colonies | **LEARN** | Confidence-tier ladder (Gap 5) is a genuinely useful *design*, but the runtime it depends on is admitted-proprietary — nothing here is runnable standalone. |
| 4 | agentlink | **IGNORE** | 1 commit, 6 weeks untouched, on-chain/wallet mechanism conflicts with our no-token-economy stance (SPEC.md §D). |
| 5 | sudo-ai | **IGNORE** | Scope mismatch (single-owner full-privilege personal assistant, not federation infra); CI currently failing on the branch tip. |
| 6 | synthorg | **LEARN** | Serious engineering (40k+ tests, SLSA 3) on Gap 3/5-relevant governance/hierarchy design, but pre-alpha, BSL-licensed, and far larger in scope than anything needed here. |
| 7 | laborforge-release | **IGNORE** | Non-OSI source-available license; author's own NOTICE calls it an art project not for serious use; CI failing. |
| 8 | MAGENTRA | **IGNORE** | Claimed Gap 5 overlap (crew/service-record) could not be verified (404 on the documented path); scope mismatch (desktop coding tool vs. village reputation). |
| 9 | swarm | **LEARN** | Strong Gap 3 (and partial Gap 4) design reference, real and actively maintained, but adopting it means adopting an entire external runtime — too large a commitment to do more than study. License technically Apache-2.0 by file content, but GitHub's own detector flags NOASSERTION — worth a direct check before ever depending on it. |
| 10 | nightforge | **LEARN** | Closest structural match to Gap 4 of all ten candidates, but no LICENSE file at all (all rights reserved) — legally nothing to take, only a pattern to study. Dormant 8 days. |

No candidate was categorized ADAPT, VENDOR, or INTEROPERATE in this
round — none is both close enough in fit and low-risk/mature enough to
justify code-level adaptation, partial code adoption, or a live protocol
integration today. That is itself a finding: the ecosystem has relevant
*patterns*, not yet a relevant *dependency* beyond `agent-contracts`.

---

## Shortlist (max 3) and next experiment-slice per candidate

### 1. `flyersworder/agent-contracts` — Gap 3 (post-registration governance)

**Category:** DEPEND. **Next experiment-slice (proposed, not executed
here):** a small, throwaway, non-production script (not wired into
`village/heartbeat.py`) that adds `ai-agent-contracts` as a pinned
dependency and wraps a *read-only* simulation of one existing bounty
(e.g. `b001`) in a `Contract` with a token/time budget and a deadline,
purely to verify locally that the library's budget/deadline enforcement
actually behaves as documented against a realistic Agent Village
scenario. No change to `bounty_claim()`/`bounty_complete()`, no
production wiring, easily discarded if it doesn't hold up.

### 2. `hangang907-png/nightforge` — Gap 4 (review/PR flow)

**Category:** LEARN (no code reuse possible — no license). **Next
experiment-slice:** a written design note (no code) mapping nightforge's
ticket state machine (`open → claimed → submitted → verifying →
accepted/rejected`, GitHub Issues + labels + Draft PRs as the entire
mechanism, `check_suite` webhook for auto-transition) onto Agent
Village's own `bounty_claim()`/`bounty_complete()` lifecycle, explicitly
identifying which specific pieces (Draft PR creation via API, CI-gated
verification, idempotent webhook delivery handling) would need original,
independently-written implementation — since nothing from nightforge can
be copied.

### 3. `Replikanti/agentis-colonies` — Gap 5 (reputation/trust progression)

**Category:** LEARN (runtime is proprietary, only the ladder design is
usable). **Next experiment-slice:** a written proposal (no code) for
extending `pokedex.json`'s permanent `status: "observed"` field into (at
minimum) a second real tier — `OBSERVED → CLAIMED` — as a small, additive
schema change plus one migration test, using agentis-colonies' four-tier
naming/threshold philosophy ("measured experience, not hand-tuned
thresholds") only as a design reference for what should gate a tier
transition, not as code.

## Recommendation: which experiment-slice first

**#1, `agent-contracts` (Gap 3), first.** Reasoning: it is the only one
of the three with a real, legally clean, low-risk external dependency
already available (Apache-2.0, on PyPI, actively maintained, small) —
the other two shortlist items require us to *originate* our own design
from a studied pattern, which is inherently higher-effort and higher-
judgment work. Gap 3 (bounty budgets/deadlines/success criteria) is also
the most concretely and narrowly scoped of the three gaps as already
named in `SPEC.md` §D, making it the lowest-risk place to test whether
"depend on a small external library" is even a pattern Agent Village
wants to adopt at all, before committing to the larger, originate-our-own
-design experiments for Gap 4 and Gap 5.
