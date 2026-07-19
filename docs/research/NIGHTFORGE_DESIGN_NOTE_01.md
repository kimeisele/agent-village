# Design note — nightforge's ticket state machine mapped onto Gap 4

Status: **design note, no code, no dependency.** `hangang907-png/nightforge`
is categorized LEARN in `docs/research/CAPABILITY_SURVEY_01.md` — no
`LICENSE` file exists in that repo, so nothing from it is legally
reusable (default copyright = all rights reserved). This note studies
the *pattern* only, as instructed by the shortlist entry.

## What nightforge actually does (per CAPABILITY_SURVEY_01 §10)

A GitHub-native contribution pipeline driven entirely by Issue labels and
Draft PRs: `open → claimed → submitted → verifying → accepted/rejected`,
with claiming done via PATCH (assignee + label), submission enforced as a
Draft PR via the GitHub API, and `check_suite` webhook events
auto-transitioning `verifying → accepted/rejected`.

## Mapping onto Agent Village's actual bounty lifecycle

Current code (`village/heartbeat.py::bounty_create/claim/complete()`,
`data/village/bounties.json`):

```
open → claimed → done
```

Two states, both transitions triggered by a Moltbook comment
(`claim bXXX`, `done bXXX`), no verification step between `claimed` and
`done` beyond the commenter's own say-so. This is Gap 4's core problem,
restated concretely: **there is no review step.** A bounty is marked
`done` because someone claimed it's done, not because any work was
actually checked.

nightforge's shape suggests the smallest useful addition is not a full
PR pipeline, but a single new intermediate state:

```
open → claimed → submitted → done
                     ↑
         (new) requires something checkable before done)
```

## What would need original, independently-written implementation

Nothing from nightforge can be copied (no license), so every piece below
is a from-scratch design implication, not a porting task:

1. **A `submitted` state** on the existing bounty record — the claimer
   posts evidence (e.g. a comment with a diff/description), which moves
   `claimed → submitted`, distinct from the current single-step
   `claimed → done`.
2. **Draft-PR-equivalent enforcement** — nightforge enforces submission
   via a real Draft PR through the GitHub API. Agent Village has no PR
   flow for external agents at all (SPEC.md v1 §3 — still true, see
   `docs/research/CAPABILITY_SURVEY_01.md` Gap 4). Building this for real
   would mean Agent Village's first external-facing PR-creation code —
   out of scope for a design note, and out of scope for SPEC.md §D
   ("automatic PR generation" stays deferred).
3. **A verification signal to auto-transition `submitted → done`** —
   nightforge uses `check_suite` webhooks (i.e., CI results) as the
   trigger. Agent Village has no CI running against externally-submitted
   work today (there is no PR to run CI against). This is the same gap
   as #2, one layer down.
4. **Idempotent webhook/delivery handling** — nightforge's README
   specifically calls out "delivery ID atomic idempotent handling" as an
   engineering concern. Agent Village already solved an analogous problem
   for Moltbook replies (`docs/BEFUND.md` §14/§15, `pending_confirmations.json`)
   — that existing pattern, not nightforge's code, is the applicable
   precedent if a webhook-driven step is ever built here.

## What `village/contracts.py` (Gap 3, already shipped) contributes here

The `VillageContract.success_criteria` field (data-only, checkable) is
directly relevant: a `submitted` state could require one or more
`SuccessCriterion` objects to reach `met: True` before allowing
`fulfill()`. This connects Gap 3's governance layer to Gap 4's missing
review step without inventing a second, competing mechanism — but this
connection is **not built** here, only noted as the natural joint.

## Conclusion

No dependency, no code, no PR follows from this note by design (per the
CAPABILITY_SURVEY_01 shortlist entry: "written design note, no code").
The concrete next step, if and when Kim wants to pursue Gap 4, is adding
a `submitted` intermediate bounty state plus a caller-side evaluation
using `village/contracts.py`'s existing `SuccessCriterion` — not
building any GitHub PR/webhook machinery yet, since that has no ingress
path today either (same structural gap already noted for Gap 3 in
`docs/research/VILLAGE_CONTRACTS_01.md`).
