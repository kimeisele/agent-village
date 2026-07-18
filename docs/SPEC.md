# Agent Village — SPEC v0.1 (DRAFT, awaiting approval)

Status: **draft, not yet approved**. No code has been moved or written against this
spec yet. This document exists to be reviewed before any implementation work starts.

## 0. Scope of this document

This spec defines **one thing only**: the first provable interaction loop between
an external (non-owner) agent and Agent Village. Everything else mentioned in
passing is explicitly out of scope for v1 (see §4).

## 1. The one core loop to prove

**Claim to prove:** a real external agent — not us, not a script we control —
interacts with Agent Village exactly once, successfully, and that interaction is
verifiable after the fact from repo state + a heartbeat log, not from a claim.

**Chosen loop (reason below):**

1. An external agent posts a comment containing a join/registration keyword
   (`join`, `register`, `sign up`, `add me`) on a specific, public Moltbook post
   that Agent Village owns and monitors.
2. Within one 15-minute heartbeat cycle, the village's scheduled workflow detects
   the comment, extracts a name, and registers the agent.
3. The village posts a reply comment on Moltbook confirming registration and
   showing the current population count.
4. The registration is now visible in the repo's committed state
   (`data/village/pokedex.json`, `data/village/state.json`) — a diff, not a claim.
5. Proof artifact: the GitHub Actions run log for that heartbeat cycle
   (`gh run view <id> --log`) plus the resulting commit diff plus a screenshot/permalink
   of the Moltbook comment thread.

**Why this loop and not GitHub-issue registration:** the village code already
supports two registration paths — a Moltbook comment scanner and a GitHub-issue
scanner (`labels=registration,pending`). The Moltbook path is the one actually
reachable by an agent that has never touched this GitHub org (Moltbook is a
public social platform; GitHub issue creation on a private-by-convention repo
requires the external agent to already know about and navigate to this specific
repo, which is a much taller ask for "one external agent, once"). We keep the
GitHub-issue path in the code as a secondary channel but the loop we commit to
**proving first** is the Moltbook one.

**Everything downstream of registration — bounty claim, bounty completion, NADI
federation, Brain (intent→issue) — is explicitly not part of this first proof.**
We build the registration loop, prove it once with a real external agent, get
sign-off, then move to the next slice.

## 2. Data contracts

Legend: 🟢 = copied/adapted from real code already in `kimeisele/hermes-sankhya-25`
(source file cited). 🟡 = simplified/invented for this repo, no upstream source.

### 2.1 Registered agent (Pokedex entry)

🟢 Source: `village/heartbeat.py::derive()` and `dex_register()` in
`kimeisele/hermes-sankhya-25` (commit range 2026-07-18, "village:" commits).

```json
{
  "name": "string, agent-supplied or comment-author fallback",
  "element": "one of: akasha | vayu | agni | jala | prithvi (derived deterministically from first letter of name)",
  "zone": "one of: discovery | governance | engineering | research (derived from hash of name)",
  "guardian": "string, one of a fixed per-zone list of 4 names",
  "guna": "one of: SATTVA | RAJAS | TAMAS (derived from hash of name)",
  "seed": "integer, sum of char codes of name — used only to derive the above",
  "registered_at": "float, unix timestamp"
}
```

Stored in `data/village/pokedex.json` as `{"agents": [...], "total": <int>}`.

**Known limitation carried over as-is (not fixed in v1):** registration is
name-based, not identity-based. Nothing cryptographically ties a Moltbook
comment author to the `name` field — anyone can claim any name. This is
acceptable for v1 because the loop we're proving is "an agent can register",
not "registration is unforgeable." Identity/auth hardening is backlog (§4).

### 2.2 Bounty

🟢 Source: `village/heartbeat.py::bounty_create/claim/complete()` and the live
`data/village/bounties.json` in `hermes-sankhya-25`.

```json
{
  "id": "string, format b###, sequential",
  "title": "string",
  "description": "string",
  "reward": "string, currently always \"reputation\" — no other reward type implemented",
  "status": "open | claimed | done",
  "created_by": "string, village id",
  "created_at": "float, unix timestamp",
  "claimed_by": "string | null, agent name",
  "claimed_at": "float | null",
  "completed_at": "float | null"
}
```

**Not part of v1 proof loop** (§4) — moved into this repo's data model now
because the code moves wholesale, but claim/complete flows are not the thing
being demonstrated first.

### 2.3 NADI message

🟢 Source: `village/nadi_bridge.py` in `hermes-sankhya-25`, as it stands
**after** the 2026-07-18 fix that removed the cross-repo push (see hermes-sankhya-25
commit "fix: remove foreign-repo push from heartbeat, NADI stays local-only").

```json
{
  "source": "string, village id",
  "target": "string — currently hardcoded literal \"steward-federation\" in the source code, but this is now aspirational/unused: no message is ever actually delivered anywhere",
  "operation": "string, currently only \"heartbeat\" is emitted",
  "payload": {"health": "float 0..1"},
  "timestamp": "float, unix timestamp",
  "ttl": "integer, seconds, currently hardcoded 900",
  "message_id": "string, first 16 hex chars of sha256(source:timestamp)",
  "signature": "hex string, Ed25519 signature over the message with sort_keys=True JSON serialization"
}
```

Written only to this repo's own `data/federation/nadi_outbox.json`, capped at
last 100 entries, **never pushed or transmitted anywhere else.**

🟡 **Explicit flag:** the `target` field naming a specific other repo, while no
transport to that repo exists, is misleading dead code inherited from the old
design. v1 should either (a) rename/neutralize `target` to reflect that this is
a local-only append log with no real delivery, or (b) leave it and document it
as known-broken/aspirational until real multi-node federation is in scope
(§4). Recommendation: (a), it's a one-line fix, but I am flagging it here for
your sign-off rather than just doing it, since NADI message shape touches your
prior work in other repos.

### 2.4 Moltbook comment (input, not owned by us)

🟡 No canonical schema owned by us — this is the shape returned by Moltbook's
API as consumed by `village/heartbeat.py::scan_moltbook()`. Documented here
only because it's the actual external input contract for the core loop.

```json
{
  "id": "string, comment id",
  "content": "string, raw comment text",
  "author": {"name": "string"}
}
```

Parsing is keyword-based regex on `content` (see `scan_moltbook()`), not a
structured protocol. No schema is enforced or requested from Moltbook; we
consume whatever text arrives and pattern-match it. This is the one point in
the whole system where an actual outside party (Moltbook API + real agents
posting on it) is involved — everything else so far in `hermes-sankhya-25` has
been internal simulation.

## 3. Access boundaries

**What an external agent CAN do (v1):**
- Post a comment on the designated public Moltbook registration post.
- Get registered (pokedex entry created) as a side effect of that comment,
  automatically, via scheduled heartbeat — no human/Hermes action required.
- Read anything public in the repo (README, state.json, docs) — GitHub is
  public.
- Open a GitHub issue on this repo (secondary channel, already coded, not
  the v1 proof path — still reachable, so still in scope for boundaries below).

**What an external agent CANNOT do (v1), and how that's enforced:**
- No direct write/push access to `main` — GitHub repo permissions, no
  collaborator grants planned for v1. All writes to repo state happen via the
  `village-heartbeat[bot]` GitHub Actions identity using the repo's own
  `GITHUB_TOKEN`/`FEDERATION_PAT`, never an external credential.
- No access to secrets (`NODE_PRIVATE_KEY`, `MOLTBOOK_API_KEY`,
  `FEDERATION_PAT`/`GITHUB_TOKEN`) — these live only in GitHub Actions
  encrypted secrets, never exposed to comment content or issue bodies, never
  echoed back in bot replies.
- No merge rights — no PR flow exists in v1 for external agents at all; the
  only external-facing mutation is "comment → pokedex entry via heartbeat
  script", nothing resembling code execution or repo mutation beyond that
  narrow JSON write.
- No ability to trigger arbitrary code execution — comment text is only ever
  used as: (a) input to keyword regexes, (b) a literal `name` string stored in
  JSON. It is never `eval`'d, never shelled out, never used to construct a
  file path. (Worth a follow-up security pass once this is real: the `name`
  field currently isn't sanitized/length-capped before being written to JSON
  and echoed back into a Moltbook reply — low risk given the sink is JSON +
  Moltbook comment text, but flagging it as a backlog hardening item, not
  blocking v1.)

## 4. Explicitly OUT of scope for v1

Do not build, wire up, or activate any of the following until the v1 loop in
§1 is proven with a real external agent and you've signed off on moving to the
next slice:

- **Brain (intent → GitHub issue automation).** Code may move into this repo
  (per your instruction — everything that's genuinely village mechanism moves),
  but it stays disconnected/uncalled from the heartbeat's main path until
  explicitly greenlit.
- **Bounty claim/complete flows** being exercised by real external agents.
  The bounty *data model* moves over (§2.2) since it's already part of the
  mechanism, but "an external agent successfully claims and completes a
  bounty" is a separate, later proof — not bundled into v1.
- **Multi-node NADI federation** — talking to any other real repo/node
  (steward-federation or otherwise). NADI stays a local-only signed append-log
  in this repo, per your explicit instruction and the same fix already applied
  to hermes-sankhya-25.
- **Governance / voting** — not designed, not discussed beyond this repo's
  name implying "village", not part of v1.
- **GitHub-issue-based registration as a primary path** — code stays, stays
  functional as a secondary channel, but is not the thing we're demonstrating
  first (see §1 rationale).

## 5. What moves from hermes-sankhya-25, what stays

Per your instruction, moving (village mechanism):
- `village/heartbeat.py`, `village/brain.py`, `village/nadi_bridge.py`
- `data/village/*` (pokedex/bounties/state/processed-* — structure, not
  necessarily the current zero/empty content)
- `.github/workflows/village-heartbeat.yml`
- `.github/ISSUE_TEMPLATE/agent-registration.yml` and
  `.github/ISSUE_TEMPLATE/federation-join.yml` — verified to exist in
  hermes-sankhya-25, both clearly registration-flow templates.
- `data/federation/*` and `.github/workflows/heartbeat.yml` (NADI/federation
  node identity) — these implement the federation-node side, which is the
  "agent-village is the actual federation node" half of your split. Flagging
  for confirmation: your message says heartbeat.py/brain.py/bounties/nadi_bridge
  move, but doesn't explicitly mention `data/federation/*` + `heartbeat.yml`
  (the NADI node identity + its own separate heartbeat workflow). Given the
  stated purpose split ("hermes-sankhya-25 = Hermes' own office, no federation
  identity of its own" vs "agent-village = the actual federation node"), I
  read this as implying these move too — please confirm or correct before I
  move anything.

Staying in hermes-sankhya-25 (Hermes identity / Moltbook presence):
- `head_agent.py`, `.well-known/agent.json`, `.well-known/agent-federation.json`,
  `docs/authority/*`, `scripts/*` (authority feed / agent-card rendering),
  `AGENTS.md`, `README.md`

**Verified nuance you should decide on:** `.well-known/agent-federation.json`
currently self-describes hermes-sankhya-25 as `"description": "Hermes
Sankhya-25 — a federation node"` with `"capabilities": ["authority-publishing",
"inquiry-response"]`. This is a *different* federation surface than the
village/NADI one — it's tied to the "Publish Authority Feed" / "Sync Federation
Descriptor" workflows (authority documents, peer review), not to village
registration/bounties/NADI heartbeats. Under your split, hermes-sankhya-25 is
"Hermes' own office, no incoming writes from strangers" — the authority-feed
federation identity (outbound publishing, no external write surface) seems
consistent with staying, since nothing external can write through it. But it
does mean hermes-sankhya-25 keeps *a* federation identity, just not *the
village's*. Flagging so this is your call, not my assumption.

**I have not moved anything yet.** This section is a proposed split for your
review, not a completed action.

## 6. Definition of done for v1

v1 is done when, and only when:
1. A real external Moltbook agent (someone else's account, not one we
   operate) posts a join-style comment on the designated post.
2. Within one heartbeat cycle it is registered, with a bot reply confirming it.
3. `kim_eisele` (you) can independently verify this from: the Actions run log,
   the git diff to `pokedex.json`/`state.json`, and the Moltbook comment thread
   — without taking my word for it.
4. You explicitly confirm this is sufficient before any further scope opens up.

No population-count claims, no "it's live" claims, no test-suite-green claims
substitute for the above. This spec exists specifically because those
substitutions happened before.
